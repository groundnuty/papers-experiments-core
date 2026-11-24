"""A retry of the check for the Couchbase values the images ship (deploy/hpc-federation/bundle/baked.py) must walk the
tree the first pass walked, and never certifies a different one.

A pass that finds a file gone is discarded and the scan starts again from the top. If the file went because a volume
dropped out of the tree, the retry walks the empty mount point, completes, and would exit 0 while the detached volume
still holds a value. The root's identity (st_dev, st_ino) is therefore read before the first pass and again before every
new pass and before a pass decides, and the devices of the directories each pass walked are kept: a changed or vanished
root, or a filesystem walked by a discarded pass and not by the deciding one, is an error (exit 2), never a pass.

The races are simulated deterministically: the module's lstat deletes a file the first time it is asked for it and
raises FileNotFoundError, as for a listed file that is gone, and then does what the case needs (swap the root, empty a
directory); a directory's st_dev is reported as another one's to stand for a mount. Sleeping is recorded, never done.
The helpers mirror test_hpc_baked_vanished.py's. The value is a stand-in and its hash; the real ones never appear here.
"""

import errno
import hashlib
import importlib.util
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

BAKED = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle" / "baked.py"
VALUE = b"qzjtwkcpmvbnrhsl"  # 16 lowercase letters, the cookies' form; not a real value
ENTRIES = [f"{len(VALUE)}:{hashlib.sha256(VALUE).hexdigest()}"]
HARMLESS = b"compacted bucket content, nothing shipped\n"
HOLDING = b"\x00" * 8 + VALUE + b"\x00" * 8  # a file with the value in it, in the binary form a bucket file has


@pytest.fixture
def baked(monkeypatch):
    """The script's module, its sleeping recorded and never done and its passes counted (`baked.watch`: `passes`, the
    number of scans started, and `sleeps`, the seconds each pause was asked for)."""
    spec = importlib.util.spec_from_file_location("baked_root_under_test", BAKED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    watch = SimpleNamespace(passes=0, sleeps=[])
    real_scan = module.scan

    def scan(*args, **kwargs):
        watch.passes += 1
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(module, "scan", scan)
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=watch.sleeps.append))
    module.watch = watch
    return module


class Tree:
    """A data directory: a bucket of three files (the middle one the file that will vanish) and `vol`, a directory
    that stands for a volume mounted below the root, with one file `vol.couch`."""

    def __init__(self, top: Path, volume_holds: bytes = HARMLESS):
        self.top = top
        self.bucket = top / "data" / "default"
        self.vol = top / "data" / "vol"
        self.bucket.mkdir(parents=True)
        self.vol.mkdir()
        for name in ("40.couch.3", "50.couch.15", "60.couch.2"):
            (self.bucket / name).write_bytes(HARMLESS)
        (self.vol / "vol.couch").write_bytes(volume_holds)

    @property
    def victim(self) -> str:
        return str(self.bucket / "50.couch.15")


def losing(monkeypatch, module, victims, after=lambda path: None):
    """The first time the module lstat's a path that victims(path, n) names (n: the number of the pass running), the
    file is deleted, after(path) runs and FileNotFoundError is raised. Returns the paths lost, in order."""
    lost, real_lstat = [], module.os.lstat

    def lstat(path, *args, **kwargs):
        path = str(path)
        if path not in lost and victims(path, module.watch.passes):
            lost.append(path)
            os.unlink(path)
            after(path)
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "lstat", lstat)
    return lost


def mounted(monkeypatch, module, directory, passes):
    """A filesystem of its own at `directory` during the given passes: the module's lstat of it says another st_dev."""
    real_lstat = module.os.lstat

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if str(path) == str(directory) and module.watch.passes in passes:
            fields = tuple(info)  # st_dev is the third
            return os.stat_result((*fields[:2], info.st_dev + 1, *fields[3:]))
        return info

    monkeypatch.setattr(module.os, "lstat", lstat)


def remove(directory: Path):
    """Remove a directory of plain files if it is there; shutil.rmtree compares the lstat the module patches."""
    if directory.exists():
        for entry in directory.iterdir():
            entry.unlink()
        directory.rmdir()


def swap_root(top: Path, detached: Path, replacement: bool = True):
    """Move the tree at top away to `detached` and, with `replacement`, put an empty directory in its place."""
    top.rename(detached)
    if replacement:
        top.mkdir()


@pytest.mark.parametrize("mount", [False, True])
@pytest.mark.parametrize("holding", [False, True])
def test_o98_a_compaction_with_the_same_root_and_devices_decides_as_before(
    tmp_path, monkeypatch, capsys, baked, mount, holding
):
    """O98: the control of the guard: 50.couch.15 is gone, its replacement is written, and the root and the devices are
    those of the first pass (with a mount below the root on both, or none): the second pass decides as it did. Exit 1
    names the file holding a value (the one on the mount, when there is one) and exit 0 prints nothing."""
    tree = Tree(tmp_path / "volumes", HOLDING if holding and mount else HARMLESS)
    if holding and not mount:
        (tree.bucket / "60.couch.2").write_bytes(HOLDING)
    if mount:
        mounted(monkeypatch, baked, tree.vol, passes=(1, 2))
    new = tree.bucket / "50.couch.16"
    lost = losing(monkeypatch, baked, lambda path, n: path == tree.victim, lambda path: new.write_bytes(HARMLESS))
    code = baked.main([str(tree.top), *ENTRIES])
    shown = capsys.readouterr()
    assert lost == [tree.victim]
    assert baked.watch.passes == 2
    assert VALUE.decode() not in shown.out + shown.err
    if holding:
        found = tree.vol / "vol.couch" if mount else tree.bucket / "60.couch.2"
        assert (code, shown.out.splitlines()) == (1, [str(found)])
    else:
        assert (code, shown.out) == (0, "")


def test_o98_a_root_replaced_between_two_passes_is_an_error_not_an_empty_tree(tmp_path, monkeypatch, capsys, baked):
    """O98: pass 1 loses a file; before pass 2 the tree is moved away (with a value in it) and an empty directory takes
    its place, as a dropped mount leaves an empty mount point. Pass 2 would complete over the empty directory and pass
    it (exit 0): the root's identity changed, so it is exit 2, saying so, and no second pass is started."""
    tree = Tree(tmp_path / "volumes")
    (tree.bucket / "60.couch.2").write_bytes(HOLDING)
    detached = tmp_path / "detached"
    losing(monkeypatch, baked, lambda path, n: path == tree.victim, lambda path: swap_root(tree.top, detached))
    assert baked.main([str(tree.top), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert re.search(r"root .* changed", shown.err)
    assert shown.out == ""
    assert VALUE.decode() not in shown.err
    assert (detached / "data" / "default" / "60.couch.2").read_bytes() == HOLDING  # the value is where it was
    assert baked.watch.passes == 1


def test_o98_a_root_replaced_while_the_only_pass_ran_is_no_pass_either(tmp_path, monkeypatch, capsys, baked):
    """O98: the check is made before a pass decides too, not only before a new one: the tree is replaced during the
    first pass, which completes (it found nothing and lost nothing): exit 2 and not 0."""
    tree = Tree(tmp_path / "volumes")
    detached, run_pass = tmp_path / "detached", baked.scan

    def scan(*args, **kwargs):
        result = run_pass(*args, **kwargs)
        swap_root(tree.top, detached)
        return result

    monkeypatch.setattr(baked, "scan", scan)
    assert baked.main([str(tree.top), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert re.search(r"root .* changed", shown.err)
    assert shown.out == ""


@pytest.mark.parametrize("replacement", [False, True])
def test_o98_a_top_that_vanishes_between_two_passes_is_an_error(tmp_path, monkeypatch, capsys, baked, replacement):
    """O98: the tree is gone (or, with `replacement`, a file is in its place) when the retry is due: exit 2 on the
    check of the root, naming the system's error or the change, and no second pass."""
    tree = Tree(tmp_path / "volumes")

    def vanish(path):
        swap_root(tree.top, tmp_path / "detached", replacement=False)
        if replacement:
            tree.top.write_bytes(HARMLESS)

    losing(monkeypatch, baked, lambda path, n: path == tree.victim, vanish)
    assert baked.main([str(tree.top), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert ("changed" if replacement else "No such file or directory") in shown.err
    assert shown.out == ""
    assert baked.watch.passes == 1


@pytest.mark.parametrize("after", ["empty", "absent"])
def test_o98_a_filesystem_dropped_below_the_root_is_an_error_not_an_empty_directory(
    tmp_path, monkeypatch, capsys, baked, after
):
    """O98: `vol` is a mount below the root (its st_dev is another) in pass 1, which loses a file in it, and the volume
    is gone in pass 2: its mount point is an empty directory (the value is on the detached volume) or the directory is
    not there. Pass 2 completes without the device pass 1 walked: exit 2, naming the device and no value, not exit 0."""
    tree = Tree(tmp_path / "volumes", HOLDING)
    dev = os.stat(tree.vol).st_dev + 1
    detached = tmp_path / "detached"
    detached.mkdir()

    def drop(path):
        (detached / "vol.couch").write_bytes(HOLDING)
        if after == "absent":
            remove(tree.vol)

    mounted(monkeypatch, baked, tree.vol, passes=(1,))
    losing(monkeypatch, baked, lambda path, n: path == str(tree.vol / "vol.couch"), drop)
    assert baked.main([str(tree.top), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert "walked by an earlier pass was not walked by the pass that decides" in shown.err
    assert f"device {dev}" in shown.err
    assert shown.out == ""
    assert VALUE.decode() not in shown.err
    assert baked.watch.passes == 2


def test_o98_the_devices_of_every_discarded_pass_are_kept_not_only_the_last_ones(tmp_path, monkeypatch, capsys, baked):
    """O98: pass 1 walks the mount and loses a file in it, pass 2 does not list it (it is gone) and loses another file,
    pass 3 completes without it: the device pass 1 walked is still owed by pass 3, so exit 2, after three passes."""
    tree = Tree(tmp_path / "volumes", HOLDING)
    mounted(monkeypatch, baked, tree.vol, passes=(1,))

    def victims(path, number):
        return (number == 1 and path == str(tree.vol / "vol.couch")) or (number == 2 and path == tree.victim)

    losing(monkeypatch, baked, victims, lambda path: remove(tree.vol))
    assert baked.main([str(tree.top), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert "walked by an earlier pass was not walked by the pass that decides" in shown.err
    assert shown.out == ""
    assert baked.watch.passes == 3
