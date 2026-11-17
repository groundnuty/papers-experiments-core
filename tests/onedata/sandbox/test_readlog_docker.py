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
from sandbox_support import TOKEN, make_spec, start_options

from expcore.errors import InfraError
from expcore_onedata.readlog import facts, files, paths, reader
from expcore_onedata.readlog.instrument import ReadLog
from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox import readlog_channel as RL
from expcore_onedata.sandbox import sandbox as sandbox_module
from expcore_onedata.sandbox.api import ReadOnlyInput, SandboxError, StartRefused
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox


@pytest.fixture
def collector(world):
    stand_in = Collector(world.host)
    yield stand_in
    stand_in.close()


def start(world, level="isolated", *, read_log=True, **spec):
    made = dataclasses.replace(make_spec(world, level), read_log=read_log, **spec)
    return DockerSandbox.start(world.host, world.s, made, **start_options(world))


def nothing_left(world) -> bool:
    return not world.host.containers and not world.host.networks and not world.host.mounts


def reading(sbx, uid=None):
    return reader.read_log(sbx.read_log(), sbx.readlog_nonce, expected_peer_uid=uid or sbx.readlog_expected_peer_uid)


def terms(world) -> list[tuple]:
    return [c for c in world.host.calls_of("signalled") if c[2] == "TERM"]


def test_a_run_without_the_channel_has_no_collector_no_bind_and_no_log(world, collector, tmp_path):
    sbx = start(world, read_log=False)
    assert set(world.host.containers) == {sbx.names.agent}
    destinations = {dst for _, dst in bound_files(world.host.containers[sbx.names.agent].options)}
    assert paths.SOCKET_DIR not in destinations
    assert paths.CLIENT_DIR not in destinations
    assert sbx.read_log() is None
    assert "read_log" not in sbx.metadata()
    assert "readlog_log" not in sbx.collect(tmp_path / "out")
    sbx.stop()


def test_o91_the_collector_container_is_the_gates_sibling_started_before_the_agent(world, collector):
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


def test_o91_the_expected_peer_is_the_declared_agent_uid_and_the_nonce_is_this_runs(world, collector):
    sbx = start(world)
    assert sbx.readlog_expected_peer_uid == world.s.agent_uid == 10001
    assert len(sbx.readlog_nonce) == 32
    assert sbx.metadata()["read_log"] == {"nonce": sbx.readlog_nonce, "expected_peer_uid": 10001, "canary": True}
    other = start(world)
    assert other.readlog_nonce != sbx.readlog_nonce
    other.stop()
    sbx.stop()


def test_o91_the_run_records_both_hashes_the_placement_and_the_expected_uid_through_the_one_writer(world, collector):
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
    sbx = start(world)
    agent = world.host.containers[sbx.names.agent]
    specs = {dict(p.split("=", 1) for p in m.split(",") if "=" in p)["dst"]: m for m in agent.options["--mount"]}
    own = sbx.dir / "readlog"
    assert f"src={own / 'sock'}," in specs[paths.SOCKET_DIR]
    assert specs[paths.SOCKET_DIR].endswith(",readonly")
    assert f"src={own / 'client'}," in specs[paths.CLIENT_DIR]
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
    sbx = start(world)
    binds = {dst: src for src, dst in bound_files(world.host.containers[sbx.names.readlog].options)}
    own = sbx.dir / "readlog"
    assert binds == {
        C.READLOG_PROGRAM_DIR: str(own / "program"),
        C.READLOG_SOCKET_DIR: str(own / "sock"),
        C.READLOG_LOG_DIR: str(own / "log"),
    }
    sbx.stop()


def test_o91_the_directories_have_the_modes_the_peer_rule_needs_and_the_copies_are_read_only_pinned_files(
    world, collector
):
    sbx = start(world)
    own = sbx.dir / "readlog"
    mode = {name: stat.S_IMODE((own / name).stat().st_mode) for name in ("program", "sock", "log", "client")}
    assert mode["log"] == 0o700  # the collector's own, never the agent's
    assert mode["sock"] == (0o700 if os.getuid() == world.s.agent_uid else 0o755)
    assert mode["client"] == 0o755  # the agent's uid reads it through the bind
    for copy, source in (
        (own / "program" / C.READLOG_PROGRAM_NAME, files.collector_file()),
        (own / "client" / paths.CLIENT_NAME, files.client_file()),
    ):
        assert stat.S_IMODE(copy.stat().st_mode) == 0o444
        assert copy.read_bytes() == source.read_bytes()
    assert sorted(p.name for p in (own / "client").iterdir()) == [paths.CLIENT_NAME]
    sbx.stop()


def test_o91_a_driver_that_is_the_agent_uid_gets_an_owner_only_socket_directory(world, collector):
    settings = dataclasses.replace(world.s, agent_uid=os.getuid())
    spec = dataclasses.replace(make_spec(world, "isolated"), read_log=True)
    sbx = DockerSandbox.start(world.host, settings, spec, **start_options(world))
    assert stat.S_IMODE((sbx.dir / "readlog" / "sock").stat().st_mode) == 0o700
    assert world.host.containers[sbx.names.readlog].command[-1] == str(os.getuid())
    sbx.stop()


def test_o91_the_canary_is_sent_from_a_cell_of_the_agents_uid_before_any_cell_of_the_agents(world, collector):
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
    collector.canary = CmdResult(1, "", "ReadLogError: no such socket")
    with pytest.raises(SandboxError, match="canary"):
        start(world)
    assert nothing_left(world)


def test_o91_a_canary_cell_that_says_nothing_is_no_canary(world, collector):
    collector.canary = CmdResult(0, "")
    with pytest.raises(SandboxError, match="canary"):
        start(world)
    assert nothing_left(world)


def test_o91_a_collector_that_exits_before_it_is_ready_refuses_the_start_and_leaves_nothing(world, collector):
    collector.exits_before_ready = True
    with pytest.raises(SandboxError, match="exited before it was ready"):
        start(world)
    assert nothing_left(world)


def test_o91_a_collector_that_never_says_ready_refuses_the_start_and_leaves_nothing(world, collector):
    collector.ready = False
    with pytest.raises(SandboxError, match="did not become ready"):
        start(world)
    assert nothing_left(world)


def test_o91_the_collection_stops_the_collector_with_sigterm_and_reads_its_log_once(world, collector, tmp_path):
    sbx = start(world)
    collector.records = [read("file-7", bytes=9)]
    assert sbx.read_log() is None  # before the collection there is none
    done = sbx.collect(tmp_path / "out")
    data = sbx.read_log()
    assert terms(world) == [("signalled", sbx.names.readlog, "TERM")]
    assert done["readlog_log"] == LOG
    assert (tmp_path / "out" / LOG).read_bytes() == data
    stop = sbx.metadata()["readlog_stop"]
    assert (stop["term"], stop["ended_on_term"], stop["killed"], stop["stopped"]) == (True, True, False, True)
    found = reading(sbx)
    assert found.intact, found.problems
    assert [r["item"] for r in found.reads] == ["file-7"]
    collector.log_path.write_bytes(b"changed afterwards")  # read once: what was read is what is kept
    assert sbx.read_log() == data
    sbx.stop()


def test_o91_a_second_collection_reuses_the_log_it_read_and_signals_nothing_again(world, collector, tmp_path):
    sbx = start(world)
    sbx.collect(tmp_path / "one")
    done = sbx.collect(tmp_path / "two")
    assert len(terms(world)) == 1
    assert done["readlog_log"] == LOG
    assert (tmp_path / "two" / LOG).read_bytes() == (tmp_path / "one" / LOG).read_bytes() == sbx.read_log()
    sbx.stop()


def test_o91_the_harness_s_own_stop_of_the_collector_is_no_death_and_the_collector_is_watched(
    world, collector, tmp_path
):
    sbx = start(world)
    assert "readlog" in sbx.infra_check()["containers"]
    done = sbx.collect(tmp_path / "out")
    assert done["deaths"] == []
    assert sbx.infra_check()["death"] == []
    sbx.stop()


def test_o91_a_collector_killed_during_the_run_gives_an_unknown_log_and_is_a_death(world, collector, tmp_path):
    sbx = start(world)
    killed = world.host.docker("kill", "-s", "KILL", sbx.names.readlog)
    assert killed.returncode == 0
    done = sbx.collect(tmp_path / "out")
    assert [d["role"] for d in done["deaths"]] == ["readlog"]
    assert done["deaths"][0]["category"] == "infrastructure"
    found = reading(sbx)
    assert not found.intact
    assert reader.NOT_CLOSED in found.problems
    sbx.stop()


def test_o91_a_collector_that_ignores_sigterm_is_killed_and_its_log_reads_unknown(world, collector, tmp_path):
    collector.ignores_term = True
    sbx = start(world)
    sbx.collect(tmp_path / "out")
    stop = sbx.metadata()["readlog_stop"]
    assert (stop["term"], stop["ended_on_term"], stop["killed"], stop["stopped"]) == (True, False, True, True)
    assert not reading(sbx).intact
    assert reader.NOT_CLOSED in reading(sbx).problems
    sbx.stop()


def test_o91_a_collector_that_wrote_no_end_line_on_sigterm_gives_an_unknown_log(world, collector, tmp_path):
    collector.complete = False
    sbx = start(world)
    sbx.collect(tmp_path / "out")
    assert reader.NOT_CLOSED in reading(sbx).problems
    sbx.stop()


def test_o91_a_log_that_cannot_be_read_is_none_and_the_reader_calls_it_unknown(world, collector, tmp_path):
    collector.writes_log = False
    sbx = start(world)
    done = sbx.collect(tmp_path / "out")
    assert done["readlog_log"] is None
    assert sbx.read_log() is None
    assert not (tmp_path / "out" / LOG).exists()
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
    assert done["readlog_log"] is None
    assert sbx.read_log() is None
    sbx.stop()


def test_o91_a_log_larger_than_a_collector_writes_is_not_read_as_a_prefix(world, collector, tmp_path, monkeypatch):
    monkeypatch.setattr(RL, "MAX_READ_BYTES", 64)
    sbx = start(world)
    done = sbx.collect(tmp_path / "out")
    assert done["readlog_log"] is None
    assert sbx.read_log() is None
    sbx.stop()


def test_o05_o91_a_log_that_holds_the_token_is_removed_with_its_source_and_fails_the_collection(
    world, collector, tmp_path
):
    sbx = start(world, "via-gate")
    collector.records = [read(f"file-{TOKEN}")]
    with pytest.raises(InfraError) as failed:
        sbx.collect(tmp_path / "out")
    assert failed.value.kind == "output_collection"
    assert not (tmp_path / "out" / LOG).exists()
    assert not collector.log_path.exists()  # the source goes too: a failed collection keeps the run directory
    assert sbx.read_log() is None
    with pytest.raises(InfraError):  # and it fails the same way again
        sbx.collect(tmp_path / "again")
    sbx.stop()
    kept = world.s.work_root / "_kept_evidence" / sbx.sid
    assert kept.is_dir()
    held = [p for p in kept.rglob("*") if p.is_file() and TOKEN.encode() in p.read_bytes()]
    assert held == []
    assert not (tmp_path / "again" / LOG).exists()


def test_o05_the_collector_s_configuration_shows_no_token_on_a_rest_run(world, collector):
    sbx = start(world, "via-gate")
    config = json.dumps(world.host.containers[sbx.names.readlog].options, default=str)
    assert TOKEN not in config
    assert TOKEN not in " ".join(world.host.containers[sbx.names.readlog].command)
    sbx.stop()


@pytest.mark.parametrize("level", ["isolated", "via-gate", "mounted"])
def test_o91_the_teardown_removes_the_collector_and_its_directories_on_every_data_path(world, collector, level):
    sbx = start(world, level)
    assert sbx.names.readlog in world.host.containers
    sbx.stop()
    assert nothing_left(world)
    assert not sbx.dir.exists()


def test_o91_a_start_that_fails_after_the_collector_and_the_gate_stops_them_all(world, collector):
    collector.canary = CmdResult(1, "", "boom")
    with pytest.raises(SandboxError):
        start(world, "via-gate")
    assert nothing_left(world)


def test_o13_o91_a_moved_gate_tag_refuses_a_run_with_the_channel_before_anything_exists(world, collector):
    world.host.images[world.s.gate.name] = "sha256:" + "f" * 64
    with pytest.raises(StartRefused, match="gate image"):
        start(world, "isolated")
    assert nothing_left(world)
    assert collector.container is None


def test_o91_the_channel_is_refused_on_a_host_that_is_not_linux(world, collector, monkeypatch):
    monkeypatch.setattr(sandbox_module, "_linux", lambda: False)
    with pytest.raises(StartRefused, match="Linux"):
        start(world)
    assert nothing_left(world)
    start(world, read_log=False).stop()  # a run without the channel is not affected


@pytest.mark.parametrize("target", [paths.SOCKET_DIR, paths.CLIENT_DIR, paths.SOCKET, paths.CLIENT])
def test_o91_a_study_input_at_either_target_of_the_channel_refuses_the_start(world, collector, tmp_path, target):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "a.py").write_text("x = 1\n")
    with pytest.raises(StartRefused):
        start(world, inputs=(ReadOnlyInput(scripts, target),))
    assert nothing_left(world)


def test_o91_the_instrument_reads_the_log_the_collection_kept(world, collector, tmp_path):
    sbx = start(world)
    collector.records = [read("file-1", bytes=3), read("file-2", bytes=4)]
    sbx.collect(tmp_path / "out")
    snapshot = ReadLog(sbx, sbx.readlog_nonce, sbx.readlog_expected_peer_uid).finish()
    assert snapshot["log"] == "intact"
    assert snapshot["counts"]["reads"] == 2
    assert snapshot["side_sha256"] is not None
    sbx.stop()


def test_o91_a_collector_that_never_wrote_a_log_reads_as_unknown_through_the_instrument(world, collector, tmp_path):
    collector.writes_log = False
    sbx = start(world)
    sbx.collect(tmp_path / "out")
    snapshot = ReadLog(sbx, sbx.readlog_nonce, sbx.readlog_expected_peer_uid).finish()
    assert snapshot["log"] == "unknown"
    assert snapshot["counts"] is None
    sbx.stop()
