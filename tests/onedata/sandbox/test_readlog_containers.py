"""The read-log channel's docker arguments: the collector's container (a sibling of the gate's: no network, read-only,
no capability, the driver's uid) and the two read-only binds the agent gets, and none of the collector's own.

Linux only: a Unix socket cannot be passed through a bind by Docker Desktop's file sharing.
"""

import dataclasses
from pathlib import Path

import pytest

from expcore_onedata.readlog import paths
from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox import inputs
from expcore_onedata.sandbox.api import DataPathMode, ReadOnlyInput, StartRefused

LABELS = ["--label", "org.test.sandbox=1"]
NONCE = "0123456789abcdef0123456789abcdef"
DRIVER_UID, DRIVER_GID = 1234, 4321
DIRS = C.ReadLogDirs(
    program=Path("/w/readlog-bin"), sockets=Path("/w/readlog-sock"), log=Path("/w/readlog-log"),
    client=Path("/w/readlog-client"),
)  # fmt: skip


@pytest.fixture
def s(world):
    return world.s


@pytest.fixture
def names(s):
    return C.Names.make(s.name_prefix, "k1-0123abcd")


def pairs(args):
    """Flag, value pairs of a docker command line (for flags that take a value)."""
    return [(a, args[i + 1]) for i, a in enumerate(args[:-1]) if a.startswith("-")]


def mounts(args):
    return [v for f, v in pairs(args) if f == "--mount"]


def agent(s, names, mode, **kw):
    defaults = {
        "image_id": s.agent.id, "mode": mode, "work": Path("/w/work"), "resolv": Path("/w/resolv.conf"),
        "share": None, "gate_ip": None,
        "gate_hosts": [], "ca": None, "trust_bundles": {}, "zone_host": None,
    }  # fmt: skip
    return C.agent_run_args(s, names, LABELS, **(defaults | kw))


def collector(s, names, **kw):
    given = {"uid": DRIVER_UID, "gid": DRIVER_GID, "nonce": NONCE} | kw
    return C.collector_run_args(s, names, LABELS, DIRS, s.gate.id, **given)


def mount_fields(spec: str) -> dict[str, str]:
    return {k: v for k, _, v in (part.partition("=") for part in spec.split(","))}


def by_destination(args) -> dict[str, dict[str, str]]:
    return {fields["dst"]: fields for fields in map(mount_fields, mounts(args))}


def test_the_collector_container_is_named_for_its_agent(s, names):
    assert names.readlog == names.agent + "-readlog"
    assert ("--name", names.readlog) in pairs(collector(s, names))


def test_the_collector_has_no_network_a_read_only_root_and_no_capability(s, names):
    args = collector(s, names)
    assert ("--network", "none") in pairs(args)
    assert "--read-only" in args
    assert ("--cap-drop", "ALL") in pairs(args)
    assert ("--security-opt", "no-new-privileges") in pairs(args)
    assert "--cap-add" not in args
    assert "--privileged" not in args
    assert "--add-host" not in args
    assert "-p" not in args
    assert "--publish" not in args


def test_the_collector_runs_as_the_drivers_uid_and_gid(s, names):
    assert ("--user", f"{DRIVER_UID}:{DRIVER_GID}") in pairs(collector(s, names))


def test_the_collector_is_labelled_like_every_container_of_the_sandbox_so_the_reaper_finds_it(s, names):
    args = collector(s, names)
    assert ("--label", "org.test.sandbox=1") in pairs(args)


def test_the_collector_is_started_by_the_pinned_image_id_of_the_gates_image(s, names):
    args = collector(s, names)
    assert s.gate.id in args
    assert s.gate.name not in args
    assert args.index(s.gate.id) > max(i for i, a in enumerate(args) if a.startswith("--"))  # after every option


def test_the_collector_has_bounds_of_its_own_so_the_agent_cannot_make_it_eat_the_host(s, names):
    args = dict(pairs(collector(s, names)))
    assert int(args["--pids-limit"]) <= 64
    assert args["--memory"] == args["--memory-swap"]
    assert 0 < int(args["--memory"]) <= 1 << 30


def test_the_collector_command_is_the_cores_command_line_with_the_expected_peer_the_declared_agent_uid(s, names):
    """`python -I -B collector.py <socket dir> <log file> <nonce> <expected peer uid>`, with the container's paths."""
    args = collector(s, names)
    assert ("--entrypoint", "python") in pairs(args)
    command = args[args.index(s.gate.id) + 1 :]
    assert command == [
        "-I",
        "-B",
        f"{C.READLOG_PROGRAM_DIR}/collector.py",
        C.READLOG_SOCKET_DIR,
        f"{C.READLOG_LOG_DIR}/{C.READLOG_LOG_NAME}",
        NONCE,
        str(s.agent_uid),
    ]


def test_the_expected_peer_uid_is_the_agents_not_the_drivers(s, names):
    other = dataclasses.replace(s, agent_uid=10555)
    assert collector(other, names)[-1] == "10555"
    assert str(DRIVER_UID) != collector(other, names)[-1]


def test_the_collectors_binds_are_its_program_read_only_and_its_two_directories(s, names):
    bound = by_destination(collector(s, names))
    assert set(bound) == {C.READLOG_PROGRAM_DIR, C.READLOG_SOCKET_DIR, C.READLOG_LOG_DIR}
    assert bound[C.READLOG_PROGRAM_DIR]["src"] == str(DIRS.program)
    assert "readonly" in bound[C.READLOG_PROGRAM_DIR]
    assert bound[C.READLOG_SOCKET_DIR]["src"] == str(DIRS.sockets)
    assert "readonly" not in bound[C.READLOG_SOCKET_DIR]
    assert bound[C.READLOG_LOG_DIR]["src"] == str(DIRS.log)
    assert "readonly" not in bound[C.READLOG_LOG_DIR]
    assert all(fields["type"] == "bind" for fields in bound.values())  # `--mount`, which refuses a missing source


def test_no_secret_reaches_the_collector_not_the_token_the_tls_keys_or_the_environment(s, names):
    args = collector(s, names)
    assert "-e" not in args
    assert "--env" not in args
    assert "-v" not in args
    assert not any(word in " ".join(args) for word in ("token", "tls", "gate.json", "federation"))


def test_the_agent_gets_the_socket_directory_and_the_client_read_only_at_the_fixed_targets(s, names):
    args, _ = agent(s, names, DataPathMode.NONE, readlog=DIRS)
    bound = by_destination(args)
    assert bound[paths.SOCKET_DIR]["src"] == str(DIRS.sockets)
    assert "readonly" in bound[paths.SOCKET_DIR]
    assert bound[paths.CLIENT_DIR]["src"] == str(DIRS.client)
    assert "readonly" in bound[paths.CLIENT_DIR]
    assert bound[paths.SOCKET_DIR]["type"] == bound[paths.CLIENT_DIR]["type"] == "bind"


@pytest.mark.parametrize("mode", list(DataPathMode))
def test_the_agent_never_gets_the_log_the_program_or_the_collectors_containers_paths(s, names, mode):
    extra = {
        DataPathMode.REST: {"gate_ip": "10.1.0.2", "gate_hosts": ["h.test"], "ca": Path("/w/ca.pem"), "zone_host": "z"},
        DataPathMode.MOUNT: {"share": Path("/w/mount")},
        DataPathMode.NONE: {},
    }[mode]
    args, env = agent(s, names, mode, readlog=DIRS, **extra)
    text = " ".join(args)
    assert str(DIRS.log) not in text
    assert str(DIRS.program) not in text
    assert C.READLOG_LOG_DIR not in text
    assert C.READLOG_PROGRAM_DIR not in text
    assert {fields["dst"] for fields in map(mount_fields, mounts(args))} >= {paths.SOCKET_DIR, paths.CLIENT_DIR}
    assert not any("readlog" in name.lower() for name in env)  # no environment variable: the target is fixed


def test_an_agent_without_the_channel_has_neither_bind(s, names):
    args, _ = agent(s, names, DataPathMode.NONE)
    assert paths.SOCKET_DIR not in " ".join(args)
    assert paths.CLIENT_DIR not in " ".join(args)


@pytest.mark.parametrize("target", [paths.SOCKET_DIR, paths.CLIENT_DIR, f"{paths.SOCKET_DIR}/x", paths.SOCKET])
def test_a_study_may_neither_bind_nor_name_an_input_at_the_channels_targets(s, target, tmp_path):
    """The channel's targets are the harness's: a read-only input there would shadow the socket or the client."""
    source = tmp_path / "scripts"
    source.mkdir()
    (source / "f.py").write_text("x")
    with pytest.raises(StartRefused):
        inputs.validate([ReadOnlyInput(source, target)], inputs.taken_paths(s))
