"""O63: an agent's cell cannot reach the key files a shard step stages into its dispatcher's private home.

On HPC the cells run as the same user as the dispatcher, so what keeps them out is the cell's command line and its
binds: `apptainer exec --containall --cleanenv --no-home --no-mount hostfs,bind-paths,cwd` (its own PID, IPC and mount
namespaces, none of the node's file systems, no home) and binds whose sources all lie below the run's own directory. The
step's home is `<tmpfs>/expcore-shard-<label>.<random>/home`, a sibling of the sandbox's `work_root` under the same
tmpfs; the operator's source tree is elsewhere. Everything is invented; the cell runs on the fake node, which parses the
real `apptainer exec` line back and fails on any flag the backend does not write."""

import shutil
from pathlib import Path

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import FakeNode

from expcore_onedata.sandbox.apptainer.argv import NO_MOUNT

KEY = ".zqsecrets/alpha-zq/key-zq"


def overlaps(a: Path, b: Path) -> bool:
    """One path is the other, below it or above it: a bind of either would show the other to a cell."""
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


@pytest.mark.parametrize(
    "level",
    [
        "isolated",
        pytest.param(
            "via-gate",
            marks=pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the run CA"),
        ),
    ],
)
def test_o63_no_bind_of_a_cell_shows_it_the_steps_home_or_the_operators_tree(tmp_path, level):
    """O63: every bind of every `apptainer exec` the sandbox makes (the harness's one-shots and the agent's cell) has a
    source below the run's own directory, which is below the work root, a sibling of the step's directory; none is, is
    below or is above the step's home (the key copy) or the operator's source tree, at the level with no data
    path and at the one that adds the run's CA and the gate's file."""
    tmpfs, home, copy, source = lay_out(tmp_path)
    node = FakeNode()
    box = start(node, make_settings(tmpfs), make_spec(level))
    try:
        box.exec("print(1)", 30)
        assert node.execs
        work_root = tmpfs / "root"
        for done in node.execs:
            assert done.binds
            for target, (bound, _) in done.binds.items():
                bound = Path(bound)
                assert bound.is_relative_to(box.dir), f"{target} is bound from outside the run's directory"
                assert box.dir.is_relative_to(work_root)
                assert not overlaps(bound, home), f"{target}: a bind shows the step's home"
                assert not overlaps(bound, copy), f"{target}: a bind shows the key copy"
                assert not overlaps(bound, source), f"{target}: a bind shows the operator's source tree"
        assert not overlaps(work_root, home)  # the work root is a sibling of the step's directory, never above it
        assert not overlaps(box.dir, home)
    finally:
        box.stop()


def test_o63_every_cell_is_contained_with_no_home_and_no_host_file_system(tmp_path):
    """O63: each exec carries `--containall` (its own PID, IPC and mount namespaces, so a cell cannot read the
    dispatcher's `/proc/<pid>/environ`, `mem` or `cwd`), `--cleanenv`, `--no-home` and `--no-mount
    hostfs,bind-paths,cwd`, and no other option that could add a path: no `--home`, `--bind`, `--mount`, `--scratch`,
    `--no-pid`, `--nopid` or `--userns`; the fake node parses the line back and refuses any flag it does not know."""
    tmpfs, *_ = lay_out(tmp_path)
    node = FakeNode()
    box = start(node, make_settings(tmpfs), make_spec("isolated"))
    try:
        box.exec("print(1)", 30)
        for done in node.execs:
            at = done.argv.index("exec")
            options = done.argv[at + 1 :]
            assert options[:4] == ["--containall", "--cleanenv", "--no-home", "--no-mount"]
            assert options[4] == NO_MOUNT == "hostfs,bind-paths,cwd"
            image = done.argv.index(done.image)
            words = [w for w in done.argv[at + 1 : image] if w.startswith("-")]
            for forbidden in ("--home", "-H", "--bind", "--mount", "--scratch", "-S", "--no-pid", "--nopid",
                              "--userns", "-u", "--fakeroot", "--writable", "--no-mount=" + NO_MOUNT):  # fmt: skip
                assert forbidden not in words
    finally:
        box.stop()


def test_o63_the_detector_sees_a_bind_that_would_show_the_home(tmp_path):
    """O63: the search above would fail where it must: a step home below the run's directory, or a work root that is the
    step's directory, is an overlap."""
    tmpfs, home, copy, source = lay_out(tmp_path)
    run = tmpfs / "root" / "run-1"
    assert overlaps(run, run / "work" / "home")  # a home below a bound directory
    assert overlaps(tmpfs / "expcore-shard-fed-a.k3j9xq", home)  # a work root that is the step's directory
    assert overlaps(home.parent, copy)
    assert not overlaps(run / "work", home)
    assert not overlaps(run / "work", source)
