"""The read-log channel on the Docker backend with the REAL collector process, client and reader (the container alone is
the fake host's): the collector the sandbox started runs as `python -I -B collector.py` on the bound host directories,
the canary goes through the real client to its real socket, and the reader judges the log the collection read.

What one process can show: a send from the declared agent uid (here this process's own, set as the agent uid) appears
with that uid, and a send from any other uid than the declared one makes the log unknown. That an agent of ANOTHER
uid, in another container, reaches the 0666 socket through the read-only bind, the peer pid across PID namespaces, and
the agent's container never seeing the log are `tests/live/test_readlog_docker.py`'s (a real Docker daemon).
"""

import dataclasses
import json
import os
import shutil
import stat
from types import SimpleNamespace

import pytest
from apptainer_support import short_dir
from readlog_docker_host import Collector
from readlog_rig import load_client, needs_unix
from sandbox_support import make_spec, start_options

from expcore_onedata.readlog import files, reader
from expcore_onedata.readlog.instrument import ReadLog
from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox.apptainer import sockets
from expcore_onedata.sandbox.sandbox import DockerSandbox

pytestmark = needs_unix

# Here the real collector runs as a HOST process (a container binds its own path, so in production the host path is no
# socket address), and binds `<root>/w/<run id>/readlog-sock/sock`: `root` may hold what is left of a Unix socket's 107
# bytes after "/w/", the longest run id and the socket's own suffix. A pytest path is too long on a batch node, where
# the runner's TMPDIR is a per-run directory, so `short_dir` tries another base, and fails (never skips) when none fits.
ROOT_BYTES = (
    sockets.SOCKET_PATH_MAX_BYTES
    - len("/w/")
    - sockets.RUN_ID_MAX_BYTES
    - len(f"/{sockets.READLOG_SOCK_DIR}/{sockets.SOCKET_NAME}")
)


@pytest.fixture
def short_world(world):
    """The `world` with a work root of a short path: the collector's socket lives under the run's directory, and a Unix
    socket's path is at most 107 bytes."""
    root = short_dir(limit=ROOT_BYTES)
    yield SimpleNamespace(
        host=world.host,
        config=world.config,
        s=dataclasses.replace(world.s, work_root=root / "w"),
        registry=world.registry,
        tmp_path=world.tmp_path,
    )
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def real(short_world):
    collector = Collector(short_world.host, real=True)
    yield collector
    collector.close()


def start(world, *, agent_uid=None):
    pinned = dataclasses.replace(world.s, readlog_collector_sha256=files.sha256_of(files.collector_file()))
    settings = pinned if agent_uid is None else dataclasses.replace(pinned, agent_uid=agent_uid)
    return DockerSandbox.start(world.host, settings, make_spec(world, "isolated"), **start_options(world))


def records(sbx) -> list[dict]:
    return [json.loads(line) for line in sbx.read_log().splitlines()]


def test_o91_a_send_from_the_declared_agent_uid_appears_with_that_uid_and_the_log_is_intact(
    short_world, real, tmp_path
):
    """O91: a send from the declared agent uid appears with that uid and the log is intact."""
    sbx = start(short_world, agent_uid=os.getuid())  # this process is the agent: the declared uid is its own
    client = load_client(real.socket_path)
    client.log_read("file-1", range=[0, 5], bytes=5, sha256="ab" * 32)
    client.log_read("file-2")
    done = sbx.collect(tmp_path / "out")
    found = reader.read_log(sbx.read_log(), sbx.readlog_nonce, expected_peer_uid=os.getuid())
    assert found.intact, found.problems
    assert [r["item"] for r in found.reads] == ["file-1", "file-2"]
    lines = records(sbx)
    assert [line["event"] for line in lines] == ["start", "record", "record", "record", "end"]  # the canary came first
    assert lines[1]["rec"]["op"] == "canary"
    assert {line["peer"]["uid"] for line in lines if line["event"] == "record"} == {os.getuid()}
    assert lines[0]["peer_uid"] == os.getuid()
    assert lines[0]["nonce"] == sbx.readlog_nonce
    assert (tmp_path / "out" / done["read_log"]).read_bytes() == sbx.read_log()
    snapshot = ReadLog(sbx, sbx.readlog_nonce, sbx.readlog_expected_peer_uid).finish()
    assert snapshot["log"] == "intact"
    assert snapshot["counts"]["reads"] == 2
    sbx.stop()


def test_o91_a_send_from_another_uid_than_the_declared_one_makes_the_log_unknown(short_world, real, tmp_path):
    """O91: a send from another uid than the declared one makes the log unknown."""
    if os.getuid() == 10001:
        pytest.skip("this process runs as the declared agent uid: nothing is foreign")
    sbx = start(short_world)  # the declared agent uid is 10001; the sender here is not
    load_client(real.socket_path).log_read("file-1")
    sbx.collect(tmp_path / "out")
    found = reader.read_log(sbx.read_log(), sbx.readlog_nonce, expected_peer_uid=10001)
    assert not found.intact
    assert reader.FOREIGN in found.problems
    snapshot = ReadLog(sbx, sbx.readlog_nonce, sbx.readlog_expected_peer_uid).finish()
    assert snapshot["log"] == "unknown"
    assert snapshot["counts"] is None  # unknown is never zero
    sbx.stop()


def test_o91_the_socket_is_world_writable_in_a_directory_others_can_search_and_the_log_is_owner_only(
    short_world, real, tmp_path
):
    """For an agent uid that is not the driver's the collector makes its directory 0755 and its socket 0666, so another
    uid in another container can send through the read-only bind, and keeps the log 0600."""
    if os.getuid() == 10001:
        pytest.skip("the declared agent uid is the driver's: the directory is owner-only")
    sbx = start(short_world)
    own = sbx.dir / "readlog"
    assert stat.S_IMODE((sbx.dir / "readlog-sock").stat().st_mode) == 0o755
    assert stat.S_IMODE(os.stat(real.socket_path).st_mode) == 0o666
    assert stat.S_IMODE(real.log_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((own / "log").stat().st_mode) == 0o700  # nothing but the collector's uid can list it
    sbx.stop()


def test_o91_the_collector_that_runs_is_the_pinned_copy_the_run_recorded(short_world, real):
    """O91: the collector that runs is the pinned copy the run recorded."""
    sbx = start(short_world)
    ran = real.proc.args
    assert ran[1:3] == ["-I", "-B"]  # isolated, no bytecode (O58), as the sandbox's own command asks
    assert ran[3] == str(sbx.dir / "readlog" / "program" / C.READLOG_PROGRAM_NAME)
    recorded = sbx.metadata()["infrastructure_facts"]["read_log"]
    assert recorded["collector_sha256"] == files.sha256_of(files.collector_file())
    assert files.sha256_of(sbx.dir / "readlog" / "program" / C.READLOG_PROGRAM_NAME) == recorded["collector_sha256"]
    assert ran[-2:] == [sbx.readlog_nonce, str(sbx.readlog_expected_peer_uid)]
    sbx.stop()


def test_o91_a_collector_killed_during_the_run_gives_an_unknown_log_and_the_client_fails_closed(
    short_world, real, tmp_path
):
    """O91: a collector killed during the run gives an unknown log and the client fails closed."""
    sbx = start(short_world, agent_uid=os.getuid())
    client = load_client(real.socket_path)
    client.log_read("file-1")
    assert short_world.host.docker("kill", "-s", "KILL", sbx.names.readlog).returncode == 0
    with pytest.raises(client.ReadLogError):  # nothing listens: the script gets no data
        client.log_read("file-2")
    done = sbx.collect(tmp_path / "out")
    assert [d["role"] for d in done["deaths"]] == ["readlog"]
    assert sbx.read_log() is not None  # it already reads unknown, so it is kept as evidence
    found = reader.read_log(sbx.read_log(), sbx.readlog_nonce, expected_peer_uid=os.getuid())
    assert not found.intact
    assert reader.NOT_CLOSED in found.problems
    sbx.stop()


def test_o91_after_the_collection_the_socket_is_gone_and_a_late_send_raises(short_world, real, tmp_path):
    """O91: after the collection the socket is gone and a late send raises."""
    sbx = start(short_world, agent_uid=os.getuid())
    client = load_client(real.socket_path)
    sbx.collect(tmp_path / "out")
    assert not os.path.exists(real.socket_path)  # the collector removed it on its way out
    with pytest.raises(client.ReadLogError):
        client.log_read("late")
    assert reader.read_log(sbx.read_log(), sbx.readlog_nonce, expected_peer_uid=os.getuid()).intact
    sbx.stop()
