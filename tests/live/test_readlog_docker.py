"""The read-log channel against a real docker daemon, skipped unless EXPCORE_LIVE_DOCKER=1 (and Linux).

    EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_PLATFORM=/path/to/platform.toml uv run pytest tests/live/test_readlog_docker.py

These are the claims no fake can show, which the first live slot proves: that an agent of ANOTHER uid, in another
container, sends through the 0666 socket in the read-only bound directory, with the uid the kernel attests; what the
peer pid reads across the two PID namespaces (recorded here as a property, never relied on); that the agent's container
cannot see the collector's log directory, write the socket directory or remove the socket; that the collector's
container is what `docker inspect` says it is; and that a collector killed mid-run gives an unknown log while the
client fails closed. They start a sandbox with no data path (no token, nothing written to a space) from the platform
file's `[sandbox]` table, and stop it.
"""

import dataclasses
import json
import os
import socket
import stat
import sys

import pytest

if os.environ.get("EXPCORE_LIVE_DOCKER") != "1":
    pytest.skip("EXPCORE_LIVE_DOCKER is not set; these tests use a real docker daemon", allow_module_level=True)
if not sys.platform.startswith("linux"):
    pytest.skip("the read-log channel passes a Unix socket through a bind: Linux only", allow_module_level=True)

from expcore_onedata.readlog import files, paths, reader
from expcore_onedata.readlog.instrument import ReadLog
from expcore_onedata.sandbox import hostops
from expcore_onedata.sandbox.api import DataPathMode, SandboxSpec
from expcore_onedata.sandbox.hostops import DockerHost
from expcore_onedata.sandbox.sandbox import DockerSandbox

PLATFORM = os.environ.get("EXPCORE_LIVE_PLATFORM")
needs_platform = pytest.mark.skipif(not PLATFORM, reason="EXPCORE_LIVE_PLATFORM is not set")
FOREIGN_UID = 12345  # a uid that is not the declared agent uid, for a send from "another agent"
UNIX_SOCKET_PATH_MAX = 107  # a Unix socket's address, without its NUL: what Apptainer's collector must fit under
LONG_ROOT_COMPONENT = "w" * 100  # a work root's last directory, long enough that the run's socket path passes it
SEND = (
    "import importlib.util as u\n"
    f"spec = u.spec_from_file_location({paths.CLIENT_MODULE!r}, {paths.CLIENT!r})\n"
    "client = u.module_from_spec(spec)\n"
    "spec.loader.exec_module(client)\n"
    "try:\n"
    "    client.log_read({item!r})\n"
    "except client.ReadLogError as e:\n"
    "    print('refused', e)\n"
    "else:\n"
    "    print('sent')\n"
)


@pytest.fixture(scope="module")
def config():
    from expcore_onedata.config import load_platform

    loaded = load_platform(PLATFORM)
    if loaded.sandbox is None:
        pytest.skip("the platform file has no [sandbox] table")
    return loaded


@pytest.fixture(scope="module")
def host(config):
    return DockerHost(sudo_command=config.sandbox.sudo)


@pytest.fixture
def sandbox(config, host):
    pinned = dataclasses.replace(config.sandbox, readlog_collector_sha256=files.sha256_of(files.collector_file()))
    spec = SandboxSpec(run_key=f"live-readlog-{os.getpid()}", mode=DataPathMode.NONE)
    started = DockerSandbox.start(host, pinned, spec)
    try:
        yield started
    finally:
        started.stop()


def log_lines(sandbox) -> list[dict]:
    return [json.loads(line) for line in sandbox.read_log().splitlines()]


@needs_platform
def test_a_send_from_the_agents_uid_reaches_the_collector_through_the_read_only_bind(
    sandbox, config, tmp_path, record_property
):
    sent = sandbox.exec(SEND.format(item="file-1"), 60)
    assert sent.stdout.strip() == "sent", sent.stderr
    sandbox.collect(tmp_path)
    found = reader.read_log(sandbox.read_log(), sandbox.readlog_nonce, expected_peer_uid=config.sandbox.agent_uid)
    assert found.intact, found.problems
    assert [r["item"] for r in found.reads] == ["file-1"]
    peers = [line["peer"] for line in log_lines(sandbox) if line["event"] == "record"]
    assert {peer["uid"] for peer in peers} == {config.sandbox.agent_uid}
    record_property(
        "peer_pid_across_pid_namespaces", sorted({peer["pid"] for peer in peers})
    )  # expected 0; not relied on
    snapshot = ReadLog(sandbox, sandbox.readlog_nonce, sandbox.readlog_expected_peer_uid).finish()
    assert snapshot["log"] == "intact"
    assert snapshot["counts"]["reads"] == 1


@needs_platform
def test_a_send_from_another_uid_reaches_the_socket_and_makes_the_log_unknown(sandbox, config, host, tmp_path):
    done = host.docker(
        *("exec", "-u", f"{FOREIGN_UID}:{FOREIGN_UID}", "-w", "/tmp", sandbox.names.agent, config.sandbox.python),
        *("-I", "-c", SEND.format(item="file-x")),
        check=False,
    )
    assert done.stdout.strip() == "sent", done.stderr  # the socket is 0666: another uid can send; the reader judges it
    sandbox.collect(tmp_path)
    found = reader.read_log(sandbox.read_log(), sandbox.readlog_nonce, expected_peer_uid=config.sandbox.agent_uid)
    assert not found.intact
    assert reader.FOREIGN in found.problems
    assert FOREIGN_UID in {line["peer"]["uid"] for line in log_lines(sandbox) if line["event"] == "record"}


@needs_platform
def test_the_agent_cannot_see_the_log_directory_write_the_socket_directory_or_remove_the_socket(sandbox):
    source = (
        "import errno, os\n"
        "print('log_dir_visible', os.path.exists('/var/readlog'), os.path.exists('/run/readlog'))\n"
        "print('socket_dir', sorted(os.listdir('/expcore-readlog')))\n"
        "for name, act in (('write', lambda: open('/expcore-readlog/x', 'w')),\n"
        "                  ('unlink', lambda: os.unlink('/expcore-readlog/sock')),\n"
        "                  ('client_write', lambda: open('/expcore-readlog-client/x', 'w'))):\n"
        "    try:\n"
        "        act()\n"
        "    except OSError as e:\n"
        "        print(name, errno.errorcode[e.errno])\n"
        "    else:\n"
        "        print(name, 'ALLOWED')\n"
    )
    said = sandbox.exec(source, 60).stdout.splitlines()
    assert said == [
        "log_dir_visible False False",
        "socket_dir ['sock']",
        "write EROFS",
        "unlink EROFS",
        "client_write EROFS",
    ]


@needs_platform
def test_the_collector_container_is_what_docker_inspect_says(sandbox, config, host):
    info = hostops.inspect(host, sandbox.names.readlog)
    assert info is not None
    settings = info["HostConfig"]
    assert settings["NetworkMode"] == "none"
    assert settings["ReadonlyRootfs"] is True
    assert settings["CapDrop"] == ["ALL"]
    assert info["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
    agent = hostops.inspect(host, sandbox.names.agent)
    mounts = {m["Destination"]: m for m in agent["Mounts"]}
    assert mounts[paths.SOCKET_DIR]["RW"] is False
    assert mounts[paths.CLIENT_DIR]["RW"] is False
    assert not any(m["Destination"] in ("/var/readlog", "/run/readlog") for m in agent["Mounts"])
    assert dataclasses.is_dataclass(config.sandbox)


@needs_platform
def test_a_collector_killed_during_the_run_gives_an_unknown_log_and_the_client_fails_closed(sandbox, host, tmp_path):
    assert sandbox.exec(SEND.format(item="before"), 60).stdout.strip() == "sent"
    assert host.docker("kill", "-s", "KILL", sandbox.names.readlog, check=False).returncode == 0
    after = sandbox.exec(SEND.format(item="after"), 60).stdout.strip()
    assert after.startswith("refused"), after  # nothing listens: the script gets no data
    done = sandbox.collect(tmp_path)
    assert [d["role"] for d in done["deaths"]] == ["readlog"]
    assert sandbox.read_log() is not None  # it already reads unknown, so it is kept as evidence
    found = reader.read_log(
        sandbox.read_log(), sandbox.readlog_nonce, expected_peer_uid=sandbox.readlog_expected_peer_uid
    )
    assert not found.intact
    assert reader.NOT_CLOSED in found.problems


@needs_platform
def test_a_host_socket_path_over_107_bytes_still_starts_with_the_canary_sent(config, host, tmp_path):
    root = tmp_path / LONG_ROOT_COMPONENT
    root.mkdir()
    pinned = dataclasses.replace(
        config.sandbox, work_root=root, readlog_collector_sha256=files.sha256_of(files.collector_file())
    )
    spec = SandboxSpec(run_key=f"live-readlog-long-{os.getpid()}", mode=DataPathMode.NONE)
    started = DockerSandbox.start(host, pinned, spec)  # raises when the canary cannot be sent from the agent's cell
    try:
        host_socket = started.dir / "readlog-sock" / "sock"
        assert len(os.fsencode(host_socket)) > UNIX_SOCKET_PATH_MAX
        # the collector bound it at its own short path in its container: the host path is only a bind source
        assert stat.S_ISSOCK(os.stat(host_socket).st_mode)
        # the premise: the kernel refuses a socket address as long as the host path
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as probe, pytest.raises(OSError, match="too long"):
            probe.bind(os.fspath(host_socket) + ".probe")
        assert started.metadata()["read_log"]["canary"] is True
        collected = tmp_path / "collected"
        collected.mkdir()
        started.collect(collected)
        found = reader.read_log(started.read_log(), started.readlog_nonce, expected_peer_uid=config.sandbox.agent_uid)
        assert found.intact, found.problems
        assert not found.reads  # the canary alone: the agent read nothing
    finally:
        started.stop()
