"""The read-log channel on the Docker backend, on the fake host with a stand-in for the collector's container.

A study's canonical scripts log every read they make, from inside the agent's container, to a collector the harness runs
in a container of its own: no network, a read-only root, no capability, the driver's uid. The agent gets its socket
directory and the client module read-only and never the log. The collection stops the collector with SIGTERM (so it
writes its end line), reads its log once, scans it for the token and exposes it as `read_log()`. A collector that was
killed, or a log that cannot be read, is an UNKNOWN log, never a run that read nothing. Linux only. The real collector
process, client and reader are `test_readlog_docker_real.py`'s; a real container is
`tests/live/test_readlog_docker.py`'s.
"""

import dataclasses
import json
import os
import stat
from pathlib import Path

import pytest
from fake_host import bound_files
from readlog_docker_host import LOG, Collector
from readlog_logs import read
from sandbox_support import TOKEN, fail_copies, make_spec, start_options

from expcore.errors import InfraError
from expcore_onedata.readlog import facts, files, paths, reader
from expcore_onedata.readlog.instrument import ReadLog
from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox import readlog_channel as RL
from expcore_onedata.sandbox import sandbox as sandbox_module
from expcore_onedata.sandbox import teardown as TD
from expcore_onedata.sandbox.api import ReadOnlyInput, SandboxError, StartRefused
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.tokens import Secret


@pytest.fixture
def collector(world):
    stand_in = Collector(world.host)
    yield stand_in
    stand_in.close()


def pin(settings):
    """The platform's `readlog_collector_sha256` for this core's own collector: the channel is on exactly then."""
    return dataclasses.replace(settings, readlog_collector_sha256=files.sha256_of(files.collector_file()))


def start(world, level="isolated", *, pinned=True, settings=None, **spec):
    settings = settings if settings is not None else (pin(world.s) if pinned else world.s)
    return DockerSandbox.start(
        world.host, settings, dataclasses.replace(make_spec(world, level), **spec), **start_options(world)
    )


def nothing_left(world) -> bool:
    return not world.host.containers and not world.host.networks and not world.host.mounts


def reading(sbx, uid=None):
    return reader.read_log(sbx.read_log(), sbx.readlog_nonce, expected_peer_uid=uid or sbx.readlog_expected_peer_uid)


def terms(world) -> list[tuple]:
    return [c for c in world.host.calls_of("signalled") if c[2] == "TERM"]


def test_a_run_without_the_channel_has_no_collector_no_bind_and_no_log(world, collector, tmp_path):
    sbx = start(world, pinned=False)
    assert set(world.host.containers) == {sbx.names.agent}
    destinations = {dst for _, dst in bound_files(world.host.containers[sbx.names.agent].options)}
    assert paths.SOCKET_DIR not in destinations
    assert paths.CLIENT_DIR not in destinations
    assert sbx.read_log() is None
    assert "read_log" not in sbx.metadata()
    assert "read_log" not in sbx.collect(tmp_path / "out")
    assert "readlog" not in json.dumps(sbx.metadata()["infrastructure_facts"])
    sbx.stop()


def test_o91_the_collector_container_is_the_gates_sibling_started_before_the_agent(world, collector):
    """O91: the collector container is the gate's sibling started before the agent."""
    sbx = start(world)
    assert list(world.host.containers) == [sbx.names.readlog, sbx.names.agent]
    container = world.host.containers[sbx.names.readlog]
    options = container.options
    assert options["--network"] == "none"
    assert options["--read-only"] is True
    assert options["--cap-drop"] == ["ALL"]
    assert options["--security-opt"] == "no-new-privileges"
    assert options["--user"] == f"{os.getuid()}:{os.getgid()}"
    assert container.image == world.s.gate.id
    assert set(container.labels) == set(world.host.containers[sbx.names.agent].labels)
    assert container.command[-2:] == [sbx.readlog_nonce, str(world.s.agent_uid)]
    sbx.stop()


def test_o91_the_sockets_host_path_is_a_bind_source_only_so_a_work_root_of_any_length_starts_the_run(world, collector):
    """O91: a Unix socket's address holds 107 bytes, and the Apptainer backend refuses a work root that would put its
    collector's socket past that. On Docker the collector binds `READLOG_SOCKET_DIR/sock` inside its own container and
    the agent connects to `paths.SOCKET`: both are short constants, and the socket directory's path on the host is only
    the source of a bind, bounded by the path limit and not by a socket's. So a run whose host path is over 107 bytes
    starts with its canary sent, and its collector is never told that path."""
    settings = dataclasses.replace(pin(world.s), work_root=world.tmp_path / ("w" * 120))
    sbx = DockerSandbox.start(world.host, settings, make_spec(world, "isolated"), **start_options(world))
    host_socket = sbx.dir / RL.SOCKET_DIRECTORY / paths.SOCKET_NAME
    assert len(os.fsencode(host_socket)) > 107  # the case this test is about
    container = world.host.containers[sbx.names.readlog]
    assert container.command[-4] == C.READLOG_SOCKET_DIR  # the socket directory it is told: the container's own
    assert all(str(sbx.dir) not in part for part in container.command)  # no word of its command line is a host path
    assert (str(sbx.dir / RL.SOCKET_DIRECTORY), C.READLOG_SOCKET_DIR) in bound_files(container.options)
    assert len(f"{C.READLOG_SOCKET_DIR}/{paths.SOCKET_NAME}") <= 107  # what the collector binds
    assert len(paths.SOCKET) <= 107  # what the agent's client connects to
    assert sbx.metadata()["read_log"]["canary"] is True
    sbx.stop()


def test_o91_the_expected_peer_is_the_declared_agent_uid_and_the_nonce_is_this_runs(world, collector):
    """O91: the expected peer is the declared agent uid and the nonce is this run's."""
    sbx = start(world)
    assert sbx.readlog_expected_peer_uid == world.s.agent_uid == 10001
    assert len(sbx.readlog_nonce) == 32
    assert sbx.metadata()["read_log"] == {"nonce": sbx.readlog_nonce, "expected_peer_uid": 10001, "canary": True}
    other = start(world)
    assert other.readlog_nonce != sbx.readlog_nonce
    other.stop()
    sbx.stop()


def test_o91_the_run_records_both_hashes_the_placement_and_the_expected_uid_through_the_one_writer(world, collector):
    """O91: the run records both hashes the placement and the expected uid through the one writer."""
    sbx = start(world)
    recorded = sbx.metadata()["infrastructure_facts"][facts.KEY]
    assert recorded == {
        "collector_sha256": files.sha256_of(files.collector_file()),
        "client_sha256": files.sha256_of(files.client_file()),
        "placement": "docker_container",
        "expected_peer_uid": 10001,
    }
    sbx.stop()


def test_o91_the_agent_has_the_socket_directory_and_the_client_read_only_and_never_the_log(world, collector):
    """O91: the agent has the socket directory and the client read-only and never the log."""
    sbx = start(world)
    agent = world.host.containers[sbx.names.agent]
    specs = {dict(p.split("=", 1) for p in m.split(",") if "=" in p)["dst"]: m for m in agent.options["--mount"]}
    own = sbx.dir / "readlog"
    assert f"src={sbx.dir / 'readlog-sock'}," in specs[paths.SOCKET_DIR]
    assert specs[paths.SOCKET_DIR].endswith(",readonly")
    assert f"src={sbx.dir / 'readlog-client'}," in specs[paths.CLIENT_DIR]
    assert specs[paths.CLIENT_DIR].endswith(",readonly")
    for source, _ in bound_files(agent.options):
        bound = Path(source)
        assert bound != own / "log"
        assert own / "log" not in bound.parents
        assert bound not in (own / "log").parents  # not a directory above the log either
        assert bound != own / "program"
    assert not any("readlog" in name.lower() for name in agent.options["_env"])  # no variable: the target is fixed
    sbx.stop()


def test_o91_the_collector_binds_its_program_read_only_and_its_directories_and_nothing_else(world, collector):
    """O91: the collector binds its program read-only and its directories and nothing else."""
    sbx = start(world)
    binds = {dst: src for src, dst in bound_files(world.host.containers[sbx.names.readlog].options)}
    own = sbx.dir / "readlog"
    assert binds == {
        C.READLOG_PROGRAM_DIR: str(own / "program"),
        C.READLOG_SOCKET_DIR: str(sbx.dir / "readlog-sock"),
        C.READLOG_LOG_DIR: str(own / "log"),
    }
    sbx.stop()


def test_o91_the_directories_have_the_modes_the_peer_rule_needs_and_the_copies_are_read_only_pinned_files(
    world, collector
):
    """O91: the directories have the modes the peer rule needs and the copies are read-only pinned files."""
    sbx = start(world)
    own = sbx.dir / "readlog"
    places = {
        "program": own / "program", "sock": sbx.dir / "readlog-sock", "log": own / "log",
        "client": sbx.dir / "readlog-client",
    }  # fmt: skip
    mode = {name: stat.S_IMODE(place.stat().st_mode) for name, place in places.items()}
    assert stat.S_IMODE(own.stat().st_mode) == 0o700  # the collector's directory: nothing but the driver's uid in it
    assert mode["log"] == 0o700  # the collector's own, never the agent's
    assert mode["sock"] == (0o700 if os.getuid() == world.s.agent_uid else 0o755)
    assert mode["client"] == 0o755  # the agent's uid reads it through the bind
    for copy, source in (
        (own / "program" / C.READLOG_PROGRAM_NAME, files.collector_file()),
        (places["client"] / paths.CLIENT_NAME, files.client_file()),
    ):
        assert stat.S_IMODE(copy.stat().st_mode) == 0o444
        assert copy.read_bytes() == source.read_bytes()
    assert sorted(p.name for p in places["client"].iterdir()) == [paths.CLIENT_NAME]
    sbx.stop()


def test_o91_a_driver_that_is_the_agent_uid_gets_an_owner_only_socket_directory(world, collector):
    """O91: a driver that is the agent uid gets an owner-only socket directory."""
    sbx = start(world, settings=dataclasses.replace(pin(world.s), agent_uid=os.getuid()))
    assert stat.S_IMODE((sbx.dir / "readlog-sock").stat().st_mode) == 0o700
    assert world.host.containers[sbx.names.readlog].command[-1] == str(os.getuid())
    sbx.stop()


def test_o91_the_canary_is_sent_from_a_cell_of_the_agents_uid_before_any_cell_of_the_agents(world, collector):
    """O91: the canary is sent from a cell of the agent's uid before any cell of the agent's."""
    sbx = start(world)
    sbx.exec("print('agent')", 30)
    canaries = [i for i, (_, stdin) in enumerate(collector.execs) if stdin and "log_canary" in stdin]
    agents = [i for i, (_, stdin) in enumerate(collector.execs) if stdin == "print('agent')"]
    assert len(canaries) == 1
    assert canaries[0] < agents[0]
    args, stdin = collector.execs[canaries[0]]
    assert args[args.index("-u") + 1] == "10001:10001"
    assert paths.CLIENT in stdin  # the client by its explicit path, as a study's script imports it
    assert stdin == RL.CANARY_SOURCE
    sbx.stop()


def test_o91_a_canary_that_could_not_be_sent_refuses_the_start_and_leaves_nothing(world, collector):
    """O91: a canary that could not be sent refuses the start and leaves nothing."""
    collector.canary = CmdResult(1, "", "ReadLogError: no such socket")
    with pytest.raises(SandboxError, match="canary"):
        start(world)
    assert nothing_left(world)


def test_o91_a_canary_cell_that_says_nothing_is_no_canary(world, collector):
    """O91: a canary cell that says nothing is no canary."""
    collector.canary = CmdResult(0, "")
    with pytest.raises(SandboxError, match="canary"):
        start(world)
    assert nothing_left(world)


def test_o91_a_collector_that_exits_before_it_is_ready_refuses_the_start_and_leaves_nothing(world, collector):
    """O91: a collector that exits before it is ready refuses the start and leaves nothing."""
    collector.exits_before_ready = True
    with pytest.raises(SandboxError, match="exited before it was ready"):
        start(world)
    assert nothing_left(world)


def test_o91_a_collector_that_never_says_ready_refuses_the_start_and_leaves_nothing(world, collector):
    """O91: a collector that never says ready refuses the start and leaves nothing."""
    collector.ready = False
    with pytest.raises(SandboxError, match="did not become ready"):
        start(world)
    assert nothing_left(world)


def test_o91_the_collection_stops_the_collector_with_sigterm_and_reads_its_log_once(world, collector, tmp_path):
    """O91: the collection stops the collector with SIGTERM and reads its log once."""
    sbx = start(world)
    collector.records = [read("file-7", bytes=9)]
    assert sbx.read_log() is None  # before the collection there is none
    done = sbx.collect(tmp_path / "out")
    data = sbx.read_log()
    assert terms(world) == [("signalled", sbx.names.readlog, "TERM")]
    assert done["read_log"] == RL.COPY_NAME == "read-log.jsonl"
    assert (tmp_path / "out" / RL.COPY_NAME).read_bytes() == data
    stop = sbx.metadata()["readlog_stop"]
    assert stop["ran_until_stop"] is True
    assert (stop["term"], stop["ended_on_term"], stop["killed"], stop["stopped"]) == (True, True, False, True)
    found = reading(sbx)
    assert found.intact, found.problems
    assert [r["item"] for r in found.reads] == ["file-7"]
    collector.log_path.write_bytes(b"changed afterwards")  # read once: what was read is what is kept
    assert sbx.read_log() == data
    sbx.stop()


def test_o91_a_second_collection_reuses_the_log_it_read_and_signals_nothing_again(world, collector, tmp_path):
    """O91: a second collection reuses the log it read and signals nothing again."""
    sbx = start(world)
    sbx.collect(tmp_path / "one")
    done = sbx.collect(tmp_path / "two")
    assert len(terms(world)) == 1
    assert done["read_log"] == RL.COPY_NAME
    assert (tmp_path / "two" / RL.COPY_NAME).read_bytes() == (tmp_path / "one" / RL.COPY_NAME).read_bytes()
    assert (tmp_path / "two" / RL.COPY_NAME).read_bytes() == sbx.read_log()
    sbx.stop()


def test_o91_the_harness_s_own_stop_of_the_collector_is_no_death_and_the_collector_is_watched(
    world, collector, tmp_path
):
    """O91: the harness's own stop of the collector is no death and the collector is watched."""
    sbx = start(world)
    assert "readlog" in sbx.infra_check()["containers"]
    done = sbx.collect(tmp_path / "out")
    assert done["deaths"] == []
    assert sbx.infra_check()["death"] == []
    sbx.stop()


def test_o91_a_collector_killed_during_the_run_gives_an_unknown_log_and_is_a_death(world, collector, tmp_path):
    """O91: a collector killed during the run gives an unknown log and is a death."""
    sbx = start(world)
    killed = world.host.docker("kill", "-s", "KILL", sbx.names.readlog)
    assert killed.returncode == 0
    done = sbx.collect(tmp_path / "out")
    assert [d["role"] for d in done["deaths"]] == ["readlog"]
    assert done["deaths"][0]["category"] == "infrastructure"
    assert sbx.metadata()["readlog_stop"]["ran_until_stop"] is False
    assert done["read_log"] == RL.COPY_NAME  # a log that already reads unknown is kept as evidence
    assert (tmp_path / "out" / RL.COPY_NAME).read_bytes() == sbx.read_log()
    found = reading(sbx)
    assert not found.intact  # and still reads unknown: no end line
    assert reader.NOT_CLOSED in found.problems
    sbx.stop()


def test_o91_a_collector_stopped_from_outside_with_sigterm_gives_an_unknown_log_not_an_intact_one(
    world, collector, tmp_path
):
    """Codex round 1: SIGTERM from outside makes the collector drain and write a valid end line, which the reader
    calls intact; the log is withheld, the collector not having ended under the harness's own stop."""
    sbx = start(world)
    collector.records = [read("file-1")]
    assert world.host.docker("kill", "-s", "TERM", sbx.names.readlog).returncode == 0
    assert collector.log_path.exists()  # the stand-in wrote a complete log, end line included
    done = sbx.collect(tmp_path / "out")
    assert [d["role"] for d in done["deaths"]] == ["readlog"]
    assert terms(world) == [("signalled", sbx.names.readlog, "TERM")]  # the outside one: the harness sent none
    stop = sbx.metadata()["readlog_stop"]
    assert (stop["ran_until_stop"], stop["term"], stop["ended_on_term"], stop["stopped"]) == (False, False, False, True)
    assert done["read_log"] is None
    assert sbx.read_log() is None
    snapshot = ReadLog(sbx, sbx.readlog_nonce, sbx.readlog_expected_peer_uid).finish()
    assert snapshot["log"] == "unknown"
    assert snapshot["counts"] is None
    sbx.stop()


def test_o91_a_collector_that_ignores_sigterm_is_killed_and_its_log_reads_unknown_and_is_kept(
    world, collector, tmp_path
):
    """O91: a collector that ignores SIGTERM is killed and its log reads unknown and is kept."""
    collector.ignores_term = True
    sbx = start(world)
    done = sbx.collect(tmp_path / "out")
    stop = sbx.metadata()["readlog_stop"]
    assert stop["ran_until_stop"] is True
    assert (stop["term"], stop["ended_on_term"], stop["killed"], stop["stopped"]) == (True, False, True, True)
    assert not reading(sbx).intact
    assert reader.NOT_CLOSED in reading(sbx).problems
    assert done["read_log"] == RL.COPY_NAME  # it already reads unknown: kept as evidence
    sbx.stop()


def test_o91_a_collector_that_needed_the_kill_and_left_a_clean_looking_log_has_it_withheld(world, collector, tmp_path):
    """One rule for both backends: a stop that was not clean never gives a log that reads intact."""
    collector.ignores_term = True
    collector.end_on_kill = True  # the stand-in writes a complete log, end line included, when it is killed
    sbx = start(world)
    done = sbx.collect(tmp_path / "out")
    assert sbx.metadata()["readlog_stop"]["killed"] is True
    assert collector.log_path.exists()  # the file is complete: it would read intact
    assert reader.read_log(collector.log_path.read_bytes(), sbx.readlog_nonce, expected_peer_uid=10001).intact
    assert done["read_log"] is None
    assert sbx.read_log() is None
    assert not (tmp_path / "out" / RL.COPY_NAME).exists()
    assert reader.MISSING in reading(sbx).problems
    sbx.stop()


def test_o91_a_collector_that_wrote_no_end_line_on_sigterm_gives_an_unknown_log(world, collector, tmp_path):
    """O91: a collector that wrote no end line on SIGTERM gives an unknown log."""
    collector.complete = False
    sbx = start(world)
    sbx.collect(tmp_path / "out")
    assert reader.NOT_CLOSED in reading(sbx).problems
    sbx.stop()


def test_o91_a_log_that_cannot_be_read_is_none_and_the_reader_calls_it_unknown(world, collector, tmp_path):
    """O91: a log that cannot be read is none and the reader calls it unknown."""
    collector.writes_log = False
    sbx = start(world)
    done = sbx.collect(tmp_path / "out")
    assert done["read_log"] is None
    assert sbx.read_log() is None
    assert not (tmp_path / "out" / RL.COPY_NAME).exists()
    assert reader.MISSING in reader.read_log(sbx.read_log(), sbx.readlog_nonce, expected_peer_uid=10001).problems
    sbx.stop()


def test_o91_a_log_that_is_a_link_is_not_read(world, collector, tmp_path):
    """A link where the log is (never the collector's doing) is not followed: the log is unknown."""
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text("{}\n")
    collector.writes_log = False
    sbx = start(world)
    (sbx.dir / "readlog" / "log" / LOG).symlink_to(decoy)
    done = sbx.collect(tmp_path / "out")
    assert done["read_log"] is None
    assert sbx.read_log() is None
    sbx.stop()


def test_o91_a_log_larger_than_a_collector_writes_is_not_read_as_a_prefix(world, collector, tmp_path, monkeypatch):
    """O91: a log larger than a collector writes is not read as a prefix."""
    monkeypatch.setattr(RL, "MAX_READ_BYTES", 64)
    sbx = start(world)
    done = sbx.collect(tmp_path / "out")
    assert done["read_log"] is None
    assert sbx.read_log() is None
    sbx.stop()


def test_o05_o91_a_log_that_holds_the_token_is_removed_with_its_source_and_fails_the_collection(
    world, collector, tmp_path
):
    """O91: a log that holds the token is removed with its source and fails the collection."""
    sbx = start(world, "via-gate")
    collector.records = [read(f"file-{TOKEN}")]
    with pytest.raises(InfraError) as failed:
        sbx.collect(tmp_path / "out")
    assert failed.value.kind == "output_collection"
    assert not (tmp_path / "out" / RL.COPY_NAME).exists()
    assert not collector.log_path.exists()  # the source goes too: a failed collection keeps the run directory
    assert sbx.read_log() is None
    with pytest.raises(InfraError):  # and it fails the same way again
        sbx.collect(tmp_path / "again")
    sbx.stop()
    kept = world.s.work_root / "_kept_evidence" / sbx.sid
    assert kept.is_dir()
    held = [p for p in kept.rglob("*") if p.is_file() and TOKEN.encode() in p.read_bytes()]
    assert held == []
    assert not (tmp_path / "again" / RL.COPY_NAME).exists()


def test_o05_the_collector_s_configuration_shows_no_token_on_a_rest_run(world, collector):
    sbx = start(world, "via-gate")
    config = json.dumps(world.host.containers[sbx.names.readlog].options, default=str)
    assert TOKEN not in config
    assert TOKEN not in " ".join(world.host.containers[sbx.names.readlog].command)
    sbx.stop()


@pytest.mark.parametrize("level", ["isolated", "via-gate", "mounted"])
def test_o91_the_teardown_removes_the_collector_and_its_directories_on_every_data_path(world, collector, level):
    """O91: the teardown removes the collector and its directories on every data path."""
    sbx = start(world, level)
    assert sbx.names.readlog in world.host.containers
    sbx.stop()
    assert nothing_left(world)
    assert not sbx.dir.exists()


def test_o91_a_start_that_fails_after_the_collector_and_the_gate_stops_them_all(world, collector):
    """O91: a start that fails after the collector and the gate stops them all."""
    collector.canary = CmdResult(1, "", "boom")
    with pytest.raises(SandboxError):
        start(world, "via-gate")
    assert nothing_left(world)


def test_o13_o91_a_moved_gate_tag_refuses_a_run_with_the_channel_before_anything_exists(world, collector):
    """O91: a moved gate tag refuses a run with the channel before anything exists."""
    world.host.images[world.s.gate.name] = "sha256:" + "f" * 64
    with pytest.raises(StartRefused, match="gate image"):
        start(world, "isolated")
    assert nothing_left(world)
    assert collector.container is None


def test_o91_the_channel_is_refused_on_a_host_that_is_not_linux(world, collector, monkeypatch):
    """O91: the channel is refused on a host that is not Linux."""
    monkeypatch.setattr(sandbox_module, "_linux", lambda: False)
    with pytest.raises(StartRefused, match="Linux"):
        start(world)
    assert nothing_left(world)
    start(world, pinned=False).stop()  # a run without the channel is not affected


@pytest.mark.parametrize("target", [paths.SOCKET_DIR, paths.CLIENT_DIR, paths.SOCKET, paths.CLIENT])
def test_o91_a_study_input_at_either_target_of_the_channel_refuses_the_start(world, collector, tmp_path, target):
    """O91: a study input at either target of the channel refuses the start."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "a.py").write_text("x = 1\n")
    with pytest.raises(StartRefused):
        start(world, inputs=(ReadOnlyInput(scripts, target),))
    assert nothing_left(world)


def test_o91_the_instrument_reads_the_log_the_collection_kept(world, collector, tmp_path):
    """O91: the instrument reads the log the collection kept."""
    sbx = start(world)
    collector.records = [read("file-1", bytes=3), read("file-2", bytes=4)]
    sbx.collect(tmp_path / "out")
    snapshot = ReadLog(sbx, sbx.readlog_nonce, sbx.readlog_expected_peer_uid).finish()
    assert snapshot["log"] == "intact"
    assert snapshot["counts"]["reads"] == 2
    assert snapshot["side_sha256"] is not None
    sbx.stop()


def test_o91_a_collector_that_never_wrote_a_log_reads_as_unknown_through_the_instrument(world, collector, tmp_path):
    """O91: a collector that never wrote a log reads as unknown through the instrument."""
    collector.writes_log = False
    sbx = start(world)
    sbx.collect(tmp_path / "out")
    snapshot = ReadLog(sbx, sbx.readlog_nonce, sbx.readlog_expected_peer_uid).finish()
    assert snapshot["log"] == "unknown"
    assert snapshot["counts"] is None
    sbx.stop()


def test_o91_a_platform_that_pins_another_collector_refuses_the_start_before_anything_exists(world, collector):
    """O91: a platform that pins another collector refuses the start before anything exists."""
    other = dataclasses.replace(world.s, readlog_collector_sha256="0" * 64)
    with pytest.raises(StartRefused, match="readlog_collector_sha256 is not the read-log collector of this core"):
        start(world, settings=other)
    assert nothing_left(world)
    assert collector.container is None


def test_o91_without_the_pin_there_is_no_collector_no_bind_no_fact_and_no_metadata(world, collector, tmp_path):
    """O91: without the pin there is no collector no bind no fact and no metadata."""
    sbx = start(world, "via-gate", pinned=False)
    assert set(world.host.containers) == {sbx.names.agent, sbx.names.gate}
    assert not (sbx.dir / "readlog").exists()
    assert not (sbx.dir / "readlog-sock").exists()
    meta = sbx.metadata()
    assert "read_log" not in meta
    assert "readlog_stop" not in meta
    assert "read_log" not in meta.get("infrastructure_facts", {})
    sbx.collect(tmp_path / "out")
    sbx.stop()


def failed_collection(world, sbx, tmp_path):
    """A collection that fails at its copy of the outputs, before it reaches the collector."""
    (sbx.dir / "work" / "out.txt").write_text("x", encoding="utf-8")
    undo = fail_copies(world.host)
    with pytest.raises(InfraError, match="copying"):
        sbx.collect(tmp_path / "e", outputs=["out.txt"])
    undo()
    assert sbx._readlog_collected is False  # it never got to the collector


def test_o05_o91_a_failed_collection_never_keeps_a_log_that_holds_the_token_in_the_evidence(world, collector, tmp_path):
    """Codex round 1: the log is scanned when the collector is stopped and read, which a collection that fails
    earlier never reaches; the kept run must still hold no secret, so the teardown scans the log and removes one
    that holds it."""
    sbx = start(world, "via-gate")
    collector.log_path.write_bytes(b'{"event": "record", "rec": {"item": "' + TOKEN.encode() + b'"}}\n')
    failed_collection(world, sbx, tmp_path)
    sbx.stop()
    kept = world.s.work_root / "_kept_evidence" / sbx.sid
    assert (kept / "work" / "out.txt").read_text() == "x"  # the rest of the evidence is kept
    assert not (kept / "readlog" / "log" / LOG).exists()
    assert [p for p in kept.rglob("*") if p.is_file() and TOKEN.encode() in p.read_bytes()] == []


def test_o05_o91_a_log_the_teardown_cannot_scan_is_not_kept_either(world, collector, tmp_path):
    """A link where the log is cannot be read without following it: a secret is never evidence, so it goes."""
    sbx = start(world, "via-gate")
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("{}\n")
    collector.log_path.symlink_to(target)
    failed_collection(world, sbx, tmp_path)
    sbx.stop()
    assert not os.path.lexists(world.s.work_root / "_kept_evidence" / sbx.sid / "readlog" / "log" / LOG)
    assert target.read_text() == "{}\n"  # only the link went


def test_o91_a_failed_collection_keeps_a_log_without_the_token_with_the_evidence(world, collector, tmp_path):
    """O91: a failed collection keeps a log without the token with the evidence."""
    sbx = start(world, "via-gate")
    collector.log_path.write_bytes(b'{"event": "start"}\n')
    failed_collection(world, sbx, tmp_path)
    sbx.stop()
    kept = world.s.work_root / "_kept_evidence" / sbx.sid / "readlog" / "log" / LOG
    assert kept.read_bytes() == b'{"event": "start"}\n'


def test_o91_a_collector_that_ends_by_itself_just_before_the_harness_s_term_is_not_a_clean_stop(
    world, collector, tmp_path
):
    """Opus round 2: it ran when the harness came to stop it, but the TERM was not delivered: a log that would read
    intact is withheld, as when something else stopped it earlier."""
    collector.term_loses_race = True
    sbx = start(world)
    done = sbx.collect(tmp_path / "out")
    stop = sbx.metadata()["readlog_stop"]
    assert (stop["ran_until_stop"], stop["term"], stop["ended_on_term"], stop["killed"]) == (True, False, False, False)
    assert stop["stopped"] is True
    assert reader.read_log(collector.log_path.read_bytes(), sbx.readlog_nonce, expected_peer_uid=10001).intact
    assert done["read_log"] is None
    assert sbx.read_log() is None
    sbx.stop()


class FailsOnce(Secret):
    """A token whose `reveal` raises once when armed: a failure, or an interrupt, between the collection's read of the
    log and its scan for the token."""

    armed = False

    def reveal(self) -> str:
        if self.armed:
            self.armed = False
            raise RuntimeError("interrupted before the scan")
        return super().reveal()


def test_o05_o91_a_collection_that_fails_after_reading_the_log_and_before_its_scan_exposes_nothing_and_scans_again(
    world, collector, tmp_path, monkeypatch
):
    """O91: the log's bytes are exposed (`read_log()`, the copy) only once they have passed the token scan, so a
    collection that fails between the read and the scan leaves the next one to read and scan again: a log that holds the
    token is never copied or handed on, and the collector is stopped once."""
    token = FailsOnce(TOKEN)
    sbx = start(world, "via-gate", token=token)
    collector.records = [read(f"file-{TOKEN}")]
    real_read, reads = sbx._read_stopped_log, []

    def read_then_arm():
        data = real_read()
        if not reads:  # the first collection only: its scan then fails
            token.armed = True
        reads.append(data)
        return data

    monkeypatch.setattr(sbx, "_read_stopped_log", read_then_arm)
    with pytest.raises(RuntimeError, match="before the scan"):
        sbx.collect(tmp_path / "one")
    assert sbx.read_log() is None  # nothing was exposed before the scan
    with pytest.raises(InfraError) as failed:  # the next collection reads again and scans
        sbx.collect(tmp_path / "two")
    assert failed.value.kind == "output_collection"
    assert len(reads) == 2
    assert not (tmp_path / "two" / RL.COPY_NAME).exists()
    assert not collector.log_path.exists()  # removed with its source
    assert sbx.read_log() is None
    assert terms(world).count(("signalled", sbx.names.readlog, "TERM")) == 1  # the collector was stopped once
    sbx.stop()


def test_o05_o91_a_teardown_that_holds_the_agent_and_sidecar_still_scrubs_the_read_log(
    world, collector, tmp_path, monkeypatch
):
    """O91: a teardown that leaves the agent and the sidecar running (a FUSE mount that will not release) leaves the run
    directory where it is; a read log in it that was never scanned, because the collection failed first, is scanned
    and removed when it holds the token, as at a teardown that goes on."""
    sbx = start(world, "mounted")
    collector.log_path.write_bytes(b'{"event": "record", "rec": {"item": "' + TOKEN.encode() + b'"}}\n')
    failed_collection(world, sbx, tmp_path)
    held = TD.TeardownResult(fuse_safe=False, removed=[], errors=["a mount that will not release"], log=[])
    monkeypatch.setattr(TD, "ordered_fuse_teardown", lambda *args, **kwargs: held)
    with pytest.raises(SandboxError, match="FUSE mount that could not be released"):
        sbx.stop()
    assert sbx.dir.is_dir()  # held: nothing of the run directory moved or removed
    assert not collector.log_path.exists()
    assert [p for p in sbx.dir.rglob("*") if p.is_file() and TOKEN.encode() in p.read_bytes()] == []
