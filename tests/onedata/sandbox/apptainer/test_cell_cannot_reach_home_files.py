"""O63: an agent's cell cannot reach the key files a shard step stages into its dispatcher's private home.

On HPC the cells run as the same user as the dispatcher, so what keeps them out is the cell's command line and its
binds: `apptainer exec --containall --cleanenv --no-home --no-mount hostfs,bind-paths,cwd` (its own PID, IPC and mount
namespaces, none of the node's file systems, no home) and binds whose sources all lie below the run's own directory. The
step's home is `<tmpfs>/expcore-shard-<label>.<random>/home`, a sibling of the sandbox's `work_root` under the same
tmpfs; the operator's source tree is elsewhere. Everything is invented; the cell runs on the fake node, which parses the
real `apptainer exec` line back and fails on any flag the backend does not write."""

import os
import shutil
from pathlib import Path

import pytest
from apptainer_support import make_settings, make_spec, options, pinned_bundle, start
from fake_mount_node import FakeMountNode
from fake_node import FakeNode

from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.argv import NO_MOUNT
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox

KEY = ".zqsecrets/alpha-zq/key-zq"


def real(path: str | Path) -> Path:
    """The path with every link resolved: a bind source that is a link to the key tree shows the tree."""
    return Path(os.path.realpath(path))


def overlaps(a: str | Path, b: str | Path) -> bool:
    """One path is the other, below it or above it, links resolved: a bind of either would show the other to a cell."""
    a, b = real(a), real(b)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def lay_out(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """The node's tmpfs holding the sandbox's work root (`<tmpfs>/root`) and the step's directory beside it, the key
    copy in the step's home, and the operator's source tree outside the tmpfs: (tmpfs, step home, copy, source)."""
    tmpfs = tmp_path / "tmpfs"
    tmpfs.mkdir()
    home = tmpfs / "expcore-shard-fed-a.k3j9xq" / "home"
    copy, source = home / KEY, tmp_path / "operator-keys" / KEY
    for path in (copy, source):
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_text("sk-zq-first-1111\n")
        path.chmod(0o600)
    return tmpfs, home, copy, source.parents[2]


def started(tmpfs: Path, level: str):
    """A sandbox of `level` started on a fake node over `tmpfs`, one cell run so that the harness's one-shots and the
    agent's cell are all recorded: (box, node, settings)."""
    if level == "mounted":
        node = FakeMountNode()
        settings = make_settings(tmpfs, oneclient_bundle=pinned_bundle(tmpfs / "files"))
        factory = HM.host_mount_factory(mountinfo=node.mountinfo)
        box = ApptainerSandbox.start(node, settings, make_spec(level), **options(mount_helper=factory))
    else:
        node, settings = FakeNode(), make_settings(tmpfs)
        box = start(node, settings, make_spec(level))
    box.exec("print(1)", 30)
    assert node.execs
    return box, node, settings


LEVELS = [
    "isolated",
    "mounted",
    pytest.param(
        "via-gate",
        marks=pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the run CA"),
    ),
]


@pytest.mark.parametrize("level", LEVELS)
def test_o63_no_bind_of_a_cell_shows_it_the_steps_home_or_the_operators_tree(tmp_path, level):
    """O63: every bind of every `apptainer exec` the sandbox makes (the harness's one-shots and the agent's cell) has a
    source, links resolved, below the run's own directory, which is below the work root, a sibling of the step's
    directory; none is, is below or is above the step's home (the key copy) or the operator's source tree, at the
    level with no data path, the one that adds the mount's share and the one that adds the run's CA and the gate's
    file. The exec's image is the configured SIF (the fake node parses an unknown option as the image, so a stray
    `--bind` would otherwise hide there)."""
    tmpfs, home, copy, source = lay_out(tmp_path)
    box, node, settings = started(tmpfs, level)
    try:
        work_root = real(tmpfs / "root")
        for done in node.execs:
            assert done.binds
            assert done.image == str(settings.apptainer.image.path)
            for target, (bound, _) in done.binds.items():
                assert real(bound).is_relative_to(real(box.dir)), f"{target} is bound from outside the run's directory"
                assert real(box.dir).is_relative_to(work_root)
                assert not overlaps(bound, home), f"{target}: a bind shows the step's home"
                assert not overlaps(bound, copy), f"{target}: a bind shows the key copy"
                assert not overlaps(bound, source), f"{target}: a bind shows the operator's source tree"
        assert not overlaps(work_root, home)  # the work root is a sibling of the step's directory, never above it
        assert not overlaps(box.dir, home)
    finally:
        box.stop()


@pytest.mark.parametrize("level", LEVELS)
def test_o63_every_cell_is_contained_with_no_home_and_no_host_file_system(tmp_path, level):
    """O63: each exec carries `--containall` (its own PID, IPC and mount namespaces, so a cell cannot read the
    dispatcher's `/proc/<pid>/environ`, `mem` or `cwd`), `--cleanenv`, `--no-home` and `--no-mount
    hostfs,bind-paths,cwd`, and no other option that could add a path: no `--home`, `--bind`, `--mount`, `--scratch`,
    `--no-pid`, `--nopid` or `--userns`; the image is the configured SIF, so no option is taken for it."""
    tmpfs, *_ = lay_out(tmp_path)
    box, node, settings = started(tmpfs, level)
    try:
        for done in node.execs:
            assert done.image == str(settings.apptainer.image.path)
            at = done.argv.index("exec")
            options = done.argv[at + 1 :]
            assert options[:4] == ["--containall", "--cleanenv", "--no-home", "--no-mount"]
            assert options[4] == NO_MOUNT == "hostfs,bind-paths,cwd"
            words = [w for w in done.argv[at + 1 : done.argv.index(done.image)] if w.startswith("-")]
            for forbidden in ("--home", "-H", "--bind", "--mount", "--scratch", "-S", "--no-pid", "--nopid",
                              "--userns", "-u", "--fakeroot", "--writable", "--no-mount=" + NO_MOUNT):  # fmt: skip
                assert forbidden not in words
    finally:
        box.stop()


def test_o63_the_detector_sees_a_bind_that_would_show_the_home(tmp_path):
    """O63: the search above would fail where it must: a step home below the run's directory, a work root that is the
    step's directory, and a bind source that is only a link to the home or to the operator's tree are overlaps."""
    tmpfs, home, copy, source = lay_out(tmp_path)
    run = tmpfs / "root" / "run-1"
    (run / "work").mkdir(parents=True)
    assert overlaps(run, run / "work" / "home")  # a home below a bound directory
    assert overlaps(tmpfs / "expcore-shard-fed-a.k3j9xq", home)  # a work root that is the step's directory
    assert overlaps(home.parent, copy)
    (run / "work" / "keys").symlink_to(source)  # looks like a path of the run's, leads to the operator's tree
    (run / "work" / "home").symlink_to(home)
    assert overlaps(run / "work" / "keys", source)
    assert overlaps(run / "work" / "home", copy)
    assert not overlaps(run / "work", home)
    assert not overlaps(run / "work", source)
