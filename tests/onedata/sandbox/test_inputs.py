"""O20: a read-only input, files a study supplies to the agent, is read-only in its container and in the run's record.

The mount itself is docker's; these tests show what the platform layer does around it: the input is copied, hashed and
mounted read-only from the copy, the start proves from inside the agent that every change is refused as a read-only
file system refuses it (the fake host answers the probe; one test runs the probe for real against a directory that
only permissions protect), and an input that changed between its hash and its mount refuses the start.
`tests/live/test_sandbox.py` runs the same against a real docker daemon.
"""

import dataclasses
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from sandbox_support import MOUNT_PATH, make_spec, start_options

from expcore_onedata.sandbox import inputs
from expcore_onedata.sandbox.api import ReadOnlyInput, SandboxError, StartRefused
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

TARGET = "/opt/analysis"
EROFS_REPLY = {
    "read": "done",
    "run": True,
    "write": 30,
    "beside": 30,
    "mkdir": 30,
    "rename": 30,
    "chmod": 30,
    "delete": 30,
    "work": "done",
}


@pytest.fixture
def scripts(tmp_path) -> Path:
    """A study's scripts: an executable one and a helper in a subdirectory."""
    root = tmp_path / "supplied"
    (root / "helpers").mkdir(parents=True)
    (root / "analyse.py").write_text("print('analysed')\n", encoding="utf-8")
    (root / "analyse.py").chmod(0o750)
    (root / "helpers" / "common.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "helpers" / "common.py").chmod(0o600)
    return root


def start(world, items, level="isolated"):
    spec = dataclasses.replace(make_spec(world, level), inputs=tuple(items))
    return DockerSandbox.start(world.host, world.s, spec, **start_options(world))


def nothing_left(world) -> bool:
    return not world.host.containers and not world.host.networks and not world.host.mounts


def agent_mounts(world, sbx) -> list[str]:
    return world.host.containers[sbx.names.agent].options.get("--mount", [])


def test_o20_the_input_is_copied_and_mounted_read_only_from_the_copy_and_its_hash_is_recorded(world, scripts):
    """O20: the agent's container mounts the run's own copy of the input at its path with `readonly`; the copy is the
    driver's, readable by the agent and executable where the source was; the metadata records the path in the agent,
    the content's SHA-256, its files and bytes, and never the host's path."""
    sbx = start(world, [ReadOnlyInput(scripts, TARGET)])
    copy = sbx.dir / "inputs" / "0"
    assert f"type=bind,src={copy},dst={TARGET},readonly" in agent_mounts(world, sbx)
    assert (copy / "analyse.py").read_text(encoding="utf-8") == "print('analysed')\n"
    modes = {p.relative_to(copy).as_posix(): stat.S_IMODE(p.lstat().st_mode) for p in [copy, *copy.rglob("*")]}
    assert modes == {".": 0o755, "analyse.py": 0o755, "helpers": 0o755, "helpers/common.py": 0o644}
    assert all(p.lstat().st_uid == os.getuid() for p in [copy, *copy.rglob("*")])
    meta = sbx.metadata()
    [recorded] = meta["read_only_inputs"]
    assert set(recorded) == {"path", "sha256", "files", "bytes"}
    assert recorded["path"] == TARGET
    assert len(recorded["sha256"]) == 64
    assert (recorded["files"], recorded["bytes"]) == (2, len("print('analysed')\n") + len("VALUE = 1\n"))
    assert meta["read_only_verified"] == {"root": True, "inputs": True}
    assert str(scripts) not in json.dumps(meta)
    sbx.stop()
    assert not sbx.dir.exists()


def test_o20_the_start_proves_from_inside_the_agent_that_every_change_is_refused(world, scripts):
    """O20: the start runs a probe as the agent that reads the input, checks the executable script can be run, tries to
    write, rename, chmod and delete a file and to create a file and a directory beside it, and writes in /work. The
    sandbox is handed out only when each change failed with EROFS and the reads and /work succeeded."""
    sent = []

    def answers(container, argv, stdin):
        if stdin and inputs.PROBE_MARK in stdin:
            sent.append(stdin)
            return CmdResult(0, json.dumps(EROFS_REPLY) + "\n")
        return world.host._default_exec(container, argv, stdin)

    world.host.exec_handler = answers
    sbx = start(world, [ReadOnlyInput(scripts, TARGET)])
    [probe] = sent
    for attempt in ("os.rename", "os.chmod", "os.unlink", "os.mkdir", "os.access", '"x"', '"w"', '"work": "/work/'):
        assert attempt in probe
    assert f"{TARGET}/analyse.py" in probe
    sbx.stop()


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"write": "done"}, "write"),
        ({"rename": "done"}, "rename"),
        ({"chmod": "done"}, "chmod"),
        ({"delete": "done"}, "delete"),
        ({"beside": "done"}, "beside"),
        ({"mkdir": 13}, "mkdir"),  # EACCES: refused by permissions only, which an owner could change
        ({"chmod": 1}, "chmod"),  # EPERM: not the owner, but nothing shows the file system read-only
        ({"read": 13}, "read"),
        ({"run": False}, "execute"),
        ({"work": 30}, "working directory"),
    ],
)
def test_o20_an_input_the_agent_could_change_or_not_use_refuses_the_start_and_leaves_nothing(
    world, scripts, change, why
):
    """O20: a probe answer that shows a change succeeding, refused for any reason but a read-only file system, the
    input unreadable or its script not executable, or /work not writable refuses the start, and nothing is left."""

    def answers(container, argv, stdin):
        if stdin and inputs.PROBE_MARK in stdin:
            return CmdResult(0, json.dumps({**EROFS_REPLY, **change}) + "\n")
        return world.host._default_exec(container, argv, stdin)

    world.host.exec_handler = answers
    with pytest.raises(SandboxError, match=why):
        start(world, [ReadOnlyInput(scripts, TARGET)])
    assert nothing_left(world)


def test_o20_the_probe_run_for_real_does_not_take_permissions_for_a_read_only_mount(scripts, tmp_path):
    """O20: the probe, run for real as this user against a directory that only its modes protect (0555 and 0444, as
    the agent could set them on files it owned), reports what succeeded and what permissions refused, and the judge
    refuses it: only EROFS, the read-only file system's error, proves the mount."""
    copy = tmp_path / "protected"
    inputs.snapshot(scripts, copy)
    name, runnable = inputs.probe_targets(copy)
    work = tmp_path / "work"
    work.mkdir()
    for path in [*copy.rglob("*")]:
        path.chmod(0o555 if path.is_dir() or os.access(path, os.X_OK) else 0o444)
    copy.chmod(0o555)
    source = inputs.probe_source(str(copy), name, runnable, work=str(work))
    done = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, check=False, timeout=60)
    said = json.loads(done.stdout)
    assert said["read"] == "done"
    assert said["run"] is True
    assert said["work"] == "done"
    assert said["chmod"] == "done"  # the owner may change the modes: permissions alone protect nothing
    assert inputs.judge(done.stdout, runnable is not None) is not None
    for path in [copy, *copy.rglob("*")]:
        path.chmod(0o755)


def test_o20_the_recorded_hash_follows_the_content_the_names_and_the_executable_bit(world, scripts, tmp_path):
    """O20: two inputs with the same files have the same hash; a changed byte, an added file, a renamed file or a
    script that is no longer executable each change it, so a changed input can never pass for the same one."""

    def recorded(source):
        return inputs.digest(source)["sha256"]

    twin = tmp_path / "twin"
    inputs.snapshot(scripts, twin)
    assert recorded(twin) == recorded(scripts)
    first = recorded(scripts)
    changes = [
        lambda: (scripts / "helpers" / "common.py").write_text("VALUE = 2\n", encoding="utf-8"),
        lambda: (scripts / "extra.txt").write_text("", encoding="utf-8"),
        lambda: (scripts / "extra.txt").rename(scripts / "renamed.txt"),
        lambda: (scripts / "analyse.py").chmod(0o640),
    ]
    seen = {first}
    for change in changes:
        change()
        now = recorded(scripts)
        assert now not in seen
        seen.add(now)
    sbx = start(world, [ReadOnlyInput(scripts, TARGET)])
    assert sbx.metadata()["read_only_inputs"][0]["sha256"] == recorded(scripts)
    sbx.stop()


@pytest.mark.parametrize("where", ["source", "copy"])
def test_o20_an_input_that_changes_between_its_hash_and_its_mount_refuses_the_start(world, scripts, where):
    """O20: the input is hashed again once the agent runs, its source and the copy mounted alike: one that changed after
    it was hashed refuses the start, and nothing is left."""
    original = world.host.docker

    def docker(*args, **kwargs):
        if args[0] == "run" and "--init" in args:  # the agent's container: the input is mounted now
            target = scripts if where == "source" else next((world.s.work_root).glob("*/inputs/0"))
            script = target / "analyse.py"
            script.chmod(0o700)
            script.write_text("print('changed')\n", encoding="utf-8")
        return original(*args, **kwargs)

    world.host.docker = docker
    with pytest.raises(SandboxError, match=f"changed between its hash and its mount \\({where}\\)"):
        start(world, [ReadOnlyInput(scripts, TARGET)])
    assert nothing_left(world)


def link_inside(root: Path) -> None:
    (root / "elsewhere").symlink_to("/etc/hostname")


def fifo_inside(root: Path) -> None:
    os.mkfifo(root / "pipeline")


@pytest.mark.parametrize(
    ("target", "spoil"),
    [
        ("analysis", None),  # not absolute
        ("/", None),
        ("/work", None),
        ("/work/analysis", None),
        ("/tmp/analysis", None),
        (MOUNT_PATH, None),
        (MOUNT_PATH + "/analysis", None),
        ("/opt/../work", None),
        ("/opt/two words", None),
        ("/opt/comma,separated", None),
        ("/etc", None),  # holds the files docker writes itself
        (TARGET, link_inside),
        (TARGET, fifo_inside),
        (TARGET, "empty"),
        (TARGET, "missing"),
        (TARGET, "file"),
    ],
)
def test_o20_an_input_that_cannot_be_delivered_safely_is_refused_before_anything_exists(
    world, scripts, tmp_path, target, spoil
):
    """O20: a path in the agent that is not plain and absolute, or that is, holds or lies under another mount of the
    agent, and a source that holds a link or a special file, holds no file, or is no directory, refuse the start before
    any container, network or mount exists, naming the input by its position, never a path."""
    source = scripts
    if spoil == "empty":
        source = tmp_path / "hollow"
        source.mkdir()
    elif spoil == "missing":
        source = tmp_path / "absent"
    elif spoil == "file":
        source = scripts / "analyse.py"
    elif spoil is not None:
        spoil(scripts)
    with pytest.raises(StartRefused, match="read-only input 0"):
        start(world, [ReadOnlyInput(source, target)], level="mounted")
    assert nothing_left(world)
    assert not world.host.containers_with(world.s.label("lease"))
    assert not any(world.s.work_root.glob("*/inputs"))


def test_o20_two_inputs_whose_paths_overlap_are_refused(world, scripts):
    """O20: two inputs, one at or under the other's path, would hide one another: refused before anything exists."""
    with pytest.raises(StartRefused, match="read-only input 1"):
        start(world, [ReadOnlyInput(scripts, TARGET), ReadOnlyInput(scripts, TARGET + "/nested")])
    assert nothing_left(world)


def test_o20_two_inputs_are_each_copied_mounted_proved_and_recorded(world, scripts, tmp_path):
    """O20: each of several inputs gets its own copy, mount, probe and record, in the order given."""
    other = tmp_path / "tables"
    other.mkdir()
    (other / "lookup.csv").write_text("a,b\n", encoding="utf-8")
    sbx = start(world, [ReadOnlyInput(scripts, TARGET), ReadOnlyInput(other, "/opt/tables")])
    mounts = agent_mounts(world, sbx)
    for n, target in enumerate((TARGET, "/opt/tables")):
        assert f"type=bind,src={sbx.dir / 'inputs' / str(n)},dst={target},readonly" in mounts
    assert [r["path"] for r in sbx.metadata()["read_only_inputs"]] == [TARGET, "/opt/tables"]
    sbx.stop()
