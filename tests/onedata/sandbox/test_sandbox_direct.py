"""O40: a direct mount on the in-memory host: its storages bound into the sidecar alone, and a start handed out only
once the check shows direct I/O in effect (the probe in the agent, then the sidecar's log)."""

import dataclasses
import json
from pathlib import Path

import pytest
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import directio as D
from expcore_onedata.sandbox.api import ReadOnlyInput, SandboxError, StartRefused
from expcore_onedata.sandbox.containers import DIRECT_STORAGE_ROOT, SIDECAR_LOG_DIR
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

STORAGE_IDS = ("0a1b2c3d4e5f", "6a7b8c9d0e1f")  # invented, in the file's order
CHECK = "space-invented/checks/direct.bin"
START_LINES = """Log file created at: 2026/11/13 10:00:00
I1113 10:00:00.100300     1 logging.cc:84] Forced direct IO: {direct}
I1113 10:00:00.100400     1 logging.cc:85] Forced proxy IO: 0
"""
FALLBACK = "E1113 10:00:02.000000    12 fsLogic.cc:1184] Reading from 0123 failed due to insufficient permissions\n"
GOOD_PROBE = {"read": 8192, "size": 8192, "storage_id": STORAGE_IDS[1], "held": "100%", "seconds": 0.05}


class Direct:
    """What the stand-in sidecar logs and what the probe answers; a test changes them before the start."""

    def __init__(self, world):
        self.log = START_LINES.format(direct="1")
        self.probe = dict(GOOD_PROBE)
        self.log_readable = True
        self.calls: list[tuple[str, list[str]]] = []
        self.order: list[str] = []
        default = world.host.exec_handler

        def handler(container, argv, stdin):
            self.calls.append((container.name, list(argv)))
            if stdin and D.PROBE_MARK in stdin:
                self.order.append("probe")
                return CmdResult(0, json.dumps(self.probe) + "\n")
            if argv[:1] == ["cat"] and argv[-1].endswith(D.LOG_FILE):
                self.order.append("log")
                return CmdResult(0, self.log) if self.log_readable else CmdResult(1, "", "No such file")
            return default(container, argv, stdin)

        world.host.exec_handler = handler


@pytest.fixture
def direct(world, tmp_path):
    """The `world` with a direct mount over two invented storage directories outside the work root."""
    paths = []
    for name in ("storage-one", "storage-two"):
        (tmp_path / name).mkdir()
        paths.append(str(tmp_path / name))
    mount = dataclasses.replace(
        world.s.mount, io_mode="direct", storages=tuple(zip(STORAGE_IDS, paths, strict=True)), direct_check_file=CHECK
    )
    world.s = dataclasses.replace(world.s, mount=mount)
    world.direct = Direct(world)
    world.storage_paths = paths
    return world


def start(world, **spec):
    return DockerSandbox.start(world.host, world.s, dataclasses.replace(make_spec(world, "mounted"), **spec),
                               **start_options(world))  # fmt: skip


def test_o40_a_direct_mount_starts_once_its_check_shows_direct_io_and_records_it_without_an_id(direct):
    """O40: the probe runs in the agent, after the read-only checks, and then the sidecar's log is read; the record
    keeps the mode, the number of storages and the check's facts, the check file's storage by its position, and no
    storage id, host path or check file path (O12)."""
    sbx = start(direct)
    meta = sbx.metadata()["mount"]
    assert meta["io_mode"] == "direct"
    assert meta["options"] == [
        "-o", "ro,allow_other", "--force-direct-io", "--log-dir", SIDECAR_LOG_DIR, "--config", "/dev/null",
        "--disable-log-buffering",
    ]  # fmt: skip
    assert meta["direct"]["storages"] == 2
    assert meta["direct"]["check"] == {
        "forced_direct": "1", "forced_proxy": "0", "size": 8192, "read": 8192, "seconds": 0.05, "held": "100%",
        "storage": 1, "lines": [],
    }  # fmt: skip
    assert direct.direct.order[:2] == ["probe", "log"]  # then `sidecar_errors` reads it again for the metadata
    text = json.dumps(sbx.metadata(), default=str)
    for value in (*STORAGE_IDS, *direct.storage_paths, CHECK, DIRECT_STORAGE_ROOT):
        assert value not in text
    sbx.stop()


def test_o40_the_probe_runs_in_the_agent_on_the_check_file_and_the_log_is_read_in_the_sidecar(direct, monkeypatch):
    """O40: the probe is a cell of the agent's on the check file under the agent's mount path; the log is oneclient's
    INFO log in its log directory, read in the sidecar, where the agent has no access."""
    probed = []
    monkeypatch.setattr(D, "probe_source", lambda path, *a: probed.append(path) or f"# {D.PROBE_MARK}\n")
    sbx = start(direct)
    assert probed == [f"{direct.s.mount.path}/{CHECK}"]
    assert [name for name, argv in direct.direct.calls if D.PROBE_MARK in json.dumps(argv)] == []  # code is on stdin
    log_reads = [(name, argv) for name, argv in direct.direct.calls if argv[:1] == ["cat"]]
    assert [name for name, _ in log_reads] == [sbx.names.sidecar]  # the start's one read
    assert log_reads[0][1][-1] == f"{SIDECAR_LOG_DIR}/{D.LOG_FILE}"  # the sandbox's own log directory (O05)
    sbx.stop()


def test_o40_a_direct_mount_records_the_study_extra_arguments_by_name_and_hash_as_a_proxy_one_does(direct):
    """O40, O36, O12: the study's extra arguments are recorded by name and hash in a direct mount as in a proxy one."""
    import hashlib

    extra = ("--provider-timeout", "45", "--read-buffer-max-size=4096")
    direct.s = dataclasses.replace(direct.s, mount=dataclasses.replace(direct.s.mount, extra_args=extra))
    sbx = start(direct)
    meta = sbx.metadata()["mount"]
    assert meta["io_mode"] == "direct"
    assert meta["extra_args"] == {
        "names": ["--provider-timeout", "--read-buffer-max-size"],
        "sha256": hashlib.sha256("\0".join(extra).encode()).hexdigest(),
    }
    sbx.stop()


def test_o40_each_storage_is_bound_into_the_sidecar_alone_and_never_into_the_agent(direct):
    """O40: the sidecar has each storage's host directory, read-only, at its place; the agent's container has none of
    them, in any argument (the agent and the services may share a uid on HPC, so the path must not be there at all)."""
    sbx = start(direct)
    sidecar, agent = direct.host.containers[sbx.names.sidecar], direct.host.containers[sbx.names.agent]
    binds = [m for m in sidecar.options["--mount"] if "readonly" in m and DIRECT_STORAGE_ROOT in m]
    assert binds == [
        f"type=bind,src={direct.storage_paths[0]},dst={DIRECT_STORAGE_ROOT}/0,readonly",
        f"type=bind,src={direct.storage_paths[1]},dst={DIRECT_STORAGE_ROOT}/1,readonly",
    ]
    agent_text = json.dumps({k: v for k, v in agent.options.items() if not k.startswith("_")}) + " ".join(agent.command)
    for path in direct.storage_paths:
        assert path not in agent_text
    assert DIRECT_STORAGE_ROOT not in agent_text
    sbx.stop()


@pytest.mark.parametrize(
    ("change", "words"),
    [
        (lambda d: setattr(d, "log", d.log + FALLBACK), "proxy_fallback"),
        (lambda d: setattr(d, "log", START_LINES.format(direct="0")), "Forced direct IO"),
        (lambda d: setattr(d, "log_readable", False), "log could not be read"),
        (lambda d: d.probe.update(read=100), "read 100 of 8192"),
        (lambda d: d.probe.update(held="99%", read=None), "holds 99%"),
        (lambda d: d.probe.update(storage_id="ffff0000ffff"), "not one of [sandbox.mount.storages]"),
        (lambda d: setattr(d, "probe", {"read": None, "error": "ENOENT"}), "ENOENT"),
    ],
)
def test_o40_a_start_whose_check_does_not_show_direct_io_is_refused_and_leaves_nothing(direct, change, words):
    """O40: a fallback to the provider, start lines that do not say forced direct, an unreadable log, a partial read,
    a file the local provider does not hold in full or one of an unmapped storage: the sandbox is never handed out,
    and what the start made is torn down."""
    change(direct.direct)
    with pytest.raises(SandboxError, match="direct I/O is not in effect") as caught:
        start(direct)
    assert words in str(caught.value)
    for value in (*STORAGE_IDS, *direct.storage_paths, CHECK, "ffff0000ffff"):
        assert value not in str(caught.value)
    assert not direct.host.containers
    assert not direct.host.mounts


def test_o40_a_direct_run_whose_log_stays_clean_is_collected_and_records_it(direct, tmp_path):
    """O40: at collection the sidecar's log is read again; with no detection or fallback line since the start, the
    run is collected and its record says so (`mount.direct.during_run`, the names of the lines found: none)."""
    sbx = start(direct)
    sbx.collect(tmp_path / "evidence")
    written = json.loads((tmp_path / "evidence" / "metadata.json").read_text())
    assert written["mount"]["direct"]["during_run"] == []
    sbx.stop()


@pytest.mark.parametrize(
    ("change", "words"),
    [
        (lambda d: setattr(d, "log", d.log + FALLBACK), "proxy_fallback"),
        (lambda d: setattr(d, "log_readable", False), "could not be read"),
    ],
)
def test_o40_a_direct_run_that_read_through_the_provider_is_set_aside_at_collection(direct, tmp_path, change, words):
    """O40: a file of the study's data whose direct read is refused for permissions falls back to the provider after
    the start's check passed (`fsLogic.cc`, `readInternal`); the run then did not read directly, so its collection
    fails as the harness's (`output_collection`) and it is never scored as a direct run. An unreadable log is the
    same: unknown is never direct."""
    from expcore.errors import InfraError

    sbx = start(direct)
    change(direct.direct)
    with pytest.raises(InfraError) as caught:
        sbx.collect(tmp_path / "evidence")
    assert caught.value.kind == "output_collection"
    assert words in str(caught.value)
    sbx.stop()


def test_o40_the_lines_of_a_direct_mount_can_be_read_at_any_time_and_none_means_unknown(direct):
    """O40: `direct_io_lines` gives the names of the marker lines the sidecar's log holds now, for a harness that checks
    a pass after its reads, and None when the log could not be read."""
    sbx = start(direct)
    assert sbx.direct_io_lines() == []
    direct.direct.log += FALLBACK
    assert sbx.direct_io_lines() == ["proxy_fallback"]
    direct.direct.log_readable = False
    assert sbx.direct_io_lines() is None
    sbx.stop()


def test_o40_a_proxy_mount_runs_no_direct_check(world):
    """O40: proxy reads through the provider; the start reads no oneclient log and runs no probe of a check file."""
    handled = []
    default = world.host.exec_handler
    world.host.exec_handler = lambda c, argv, stdin: handled.append(stdin or "") or default(c, argv, stdin)
    sbx = start(world)
    assert not any(D.PROBE_MARK in code for code in handled)
    assert "direct" not in sbx.metadata()["mount"]
    sbx.stop()


def test_o40_a_storage_directory_that_is_not_on_the_host_refuses_the_start_before_anything_exists(direct, tmp_path):
    """O40: `--mount` would refuse it at the sidecar's start; the start refuses first, by the entry's position."""
    missing = str(tmp_path / "not-there")
    mount = dataclasses.replace(direct.s.mount, storages=((STORAGE_IDS[0], missing),))
    direct.s = dataclasses.replace(direct.s, mount=mount)
    with pytest.raises(StartRefused, match="entry 0") as caught:
        start(direct)
    assert missing not in str(caught.value)
    assert not direct.host.containers


@pytest.mark.parametrize("where", ["inside", "same", "around"])
def test_o40_a_read_only_input_from_a_storage_directory_is_refused(direct, tmp_path, where):
    """O40: an input is copied into the run and bound into the agent, so one taken from a provider's storage would put
    the storage's files in the agent's container: refused, by the input's position, whatever the nesting."""
    storage = Path(direct.storage_paths[0])
    (storage / "sub").mkdir()
    (storage / "sub" / "f.txt").write_text("invented\n", encoding="utf-8")
    source = {"inside": storage / "sub", "same": storage, "around": tmp_path}[where]
    with pytest.raises(StartRefused, match="read-only input 0") as caught:
        start(direct, inputs=(ReadOnlyInput(source, "/opt/invented"),))
    assert "storage" in str(caught.value)
    assert str(storage) not in str(caught.value)
