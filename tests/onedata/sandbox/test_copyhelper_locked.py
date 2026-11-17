"""O71: an entry the agent locked in `/work` never makes its own run unscored (the copy helper, `copyhelper.py`).

The agent owns everything under `/work` and can clear the owner's bits of a file or directory (`chmod 000`). The copy
helper runs as the agent's own uid on the Apptainer backend, so such an entry made its listing or copy exit 1 and the
whole collection a set-aside infrastructure failure: an agent's own action escaping scoring. The helper now gives the
owner back the read (and, for a directory, search) bit of what it owns and what refuses it, through a descriptor of
the entry that never follows a link and only within the listed file system; an entry that still refuses is listed as
the agent's unreadable output, refused for the scorers as every other object the agent made that cannot be copied.
"""

import importlib.util
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from locked_work import LocalHost, lock, locked_afterwards, needs_unprivileged, not_the_owner

from expcore_onedata.sandbox import copyout
from expcore_onedata.sandbox.hostops import run_bounded

pytestmark = needs_unprivileged


@pytest.fixture(autouse=True)
def _unlocked_afterwards(tmp_path):
    yield from locked_afterwards(lambda: tmp_path)


def _helper_module():
    """The copy helper's file as a module of its own, never put on `sys.path` or in `sys.modules`."""
    spec = importlib.util.spec_from_file_location("copyhelper_under_test", copyout.HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _list(root: Path, source: str | None = None):
    done = run_bounded(
        [sys.executable, "-I", "-B", "-c", source or copyout.HELPER.read_text(encoding="utf-8"), "list"], 60,
        input=json.dumps({"root": str(root), "max_path_bytes": copyout.MAX_PATH_BYTES}),
    )  # fmt: skip
    return done, (copyout.parse_listing(done.stdout) if done.returncode == 0 else [])


TEXTS = {
    "a.txt": "a\n", "ok.txt": "ok\n", "locked/inner.txt": "inner\n", "locked/deep/x.txt": "x\n",
    "nosearch/n.txt": "n\n", "noread/r.txt": "r\n",
}  # fmt: skip


def _tree(root: Path, *, halves: bool = False) -> None:
    """`out` with a locked file, a locked directory that holds a file and a locked directory, and a readable file
    beside them; with `halves`, a directory the agent left without search and one without read as well."""
    out = root / "out"
    (out / "locked" / "deep").mkdir(parents=True)
    (out / "nosearch").mkdir()
    (out / "noread").mkdir()
    for rel, text in TEXTS.items():
        (out / rel).write_text(text, encoding="utf-8")
    lock(out / "a.txt")
    lock(out / "locked" / "deep")
    lock(out / "locked")
    if halves:
        (out / "nosearch").chmod(0o600)  # read and write, no search: its entries cannot be examined
        (out / "noread").chmod(0o300)  # search and write, no read: it cannot be listed


def test_o71_a_file_and_directories_the_agent_locked_are_listed_whole_and_given_back_only_the_owners_read_bits(
    tmp_path,
):
    """O71: the listing exits 0 and shows the locked directories' entries; what it restored is the owner's read (and
    search) bit and nothing else: no write bit, no bit of the group or others."""
    _tree(tmp_path, halves=True)
    done, entries = _list(tmp_path)
    assert done.returncode == 0, done.stderr
    outputs, refused = copyout.check(entries, ["out"], max_bytes=None)
    assert refused == []
    assert (outputs["out"].files, outputs["out"].dirs) == (len(TEXTS), 5)
    out = tmp_path / "out"
    modes = {p: stat.S_IMODE((out / p).lstat().st_mode) for p in
             ("a.txt", "locked", "locked/deep", "nosearch", "noread")}  # fmt: skip
    assert modes == {"a.txt": 0o400, "locked": 0o500, "locked/deep": 0o500, "nosearch": 0o700, "noread": 0o700}


def test_o71_the_locked_outputs_are_copied_with_their_content_and_the_copy_is_the_drivers(tmp_path):
    """O71: the whole collection through the copy helper: a mode-000 file and directory come out with what they hold."""
    _tree(tmp_path, halves=True)
    dest = tmp_path / "dest"
    got = copyout.copy_tree(LocalHost(), tmp_path, [("out", dest / "out")], max_bytes=None)
    assert got["out"] == {"files": len(TEXTS), "bytes": sum(len(t) for t in TEXTS.values())}
    for rel, text in TEXTS.items():
        assert (dest / "out" / rel).read_text(encoding="utf-8") == text


def test_o71_the_root_of_the_listing_the_agent_locked_is_restored(tmp_path):
    """O71: `chmod 000 /work` is the cheapest of the agent's actions: the root is its own, restored like the rest."""
    root = tmp_path / "work"
    (root / "out").mkdir(parents=True)
    (root / "out" / "r.txt").write_text("r\n", encoding="utf-8")
    lock(root)
    done, entries = _list(root)
    assert done.returncode == 0, done.stderr
    assert [e.name for e in entries] == ["work", "out", "r.txt"]
    assert stat.S_IMODE(root.lstat().st_mode) == 0o500


def test_o71_an_entry_the_helper_cannot_restore_is_the_agents_unreadable_output_refused_not_a_failure(tmp_path):
    """O71: where the owner's bits cannot be given back (here, a helper that is not the owner), the entry is listed
    with a kind of its own, the listing exits 0, and the check refuses an output that holds one or has one on its way,
    naming it by its path and what it is; a locked entry outside every output costs the readable ones nothing."""
    _tree(tmp_path)
    (tmp_path / "other.txt").write_text("elsewhere\n", encoding="utf-8")
    lock(tmp_path / "other.txt")
    source = not_the_owner(copyout.HELPER.read_text(encoding="utf-8"))
    done, entries = _list(tmp_path, source)
    assert done.returncode == 0, done.stderr
    kinds = {e.name: e.kind for e in entries}
    assert (kinds["a.txt"], kinds["locked"], kinds["other.txt"], kinds["ok.txt"]) == ("x", "x", "x", "f")
    _, refused = copyout.check(entries, ["out"], max_bytes=None)
    assert sorted((r.path, r.what) for r in refused) == [
        ("out/a.txt", copyout.UNREADABLE), ("out/locked", copyout.UNREADABLE),
    ]  # fmt: skip
    _, refused = copyout.check(entries, ["out/ok.txt"], max_bytes=None)
    assert refused == []  # a sibling's lock never blocks a readable output
    _, refused = copyout.check(entries, ["out/locked/inner.txt"], max_bytes=None)
    assert [(r.path, r.what) for r in refused] == [("out/locked", copyout.UNREADABLE)]  # on the way to the output


def test_o71_an_entry_whose_restore_failed_is_listed_unreadable_and_the_run_is_not_failed(tmp_path, monkeypatch):
    """O71: the helper is the owner but `restore` could not change the entry (no `/proc`, say): the same record."""
    _tree(tmp_path, halves=True)
    module = _helper_module()
    monkeypatch.setattr(module, "restore", lambda *args: False)
    out = io.BytesIO()
    module.listing({"root": str(tmp_path), "max_path_bytes": copyout.MAX_PATH_BYTES}, out)
    kinds = {e.name: e.kind for e in copyout.parse_listing(out.getvalue().decode())}
    assert (kinds["a.txt"], kinds["locked"], kinds["nosearch"], kinds["noread"], kinds["ok.txt"]) == (
        "x", "x", "x", "x", "f",
    )  # fmt: skip
    assert stat.S_IMODE((tmp_path / "out" / "a.txt").lstat().st_mode) == 0


def test_o71_a_locked_root_that_cannot_be_restored_is_listed_alone_as_unreadable(tmp_path):
    """O71: the root is the agent's `/work`: where it cannot be opened, the listing is its one record, `x`, so every
    output is refused as the agent's doing and none is a set-aside."""
    root = tmp_path / "work"
    (root / "out").mkdir(parents=True)
    lock(root)
    done, entries = _list(root, not_the_owner(copyout.HELPER.read_text(encoding="utf-8")))
    assert done.returncode == 0, done.stderr
    assert [(e.name, e.kind) for e in entries] == [("work", "x")]
    _, refused = copyout.check(entries, ["out"], max_bytes=None)
    assert [(r.path, r.what) for r in refused] == [(".", copyout.UNREADABLE)]


def test_o71_restoring_never_follows_a_link_swapped_for_the_entry(tmp_path):
    """O71: the entry is pinned by a descriptor opened with `O_NOFOLLOW`, and checked against what was listed: a link
    put where a locked directory was changes nothing of what it points at."""
    module = _helper_module()
    outside = tmp_path / "outside"
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "d").mkdir()
    outside.mkdir()
    lock(outside)
    lock(tmp_path / "work" / "d")
    directory = os.open(tmp_path / "work", os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.stat("d", dir_fd=directory, follow_symlinks=False)
        os.chmod(tmp_path / "work" / "d", 0o700)
        os.rmdir(tmp_path / "work" / "d")
        os.symlink(outside, tmp_path / "work" / "d")  # swapped after the listing
        assert module.restore(directory, "d", info, 0o500) is False
    finally:
        os.close(directory)
    assert stat.S_IMODE(outside.lstat().st_mode) == 0


def test_o71_a_link_to_a_locked_entry_is_refused_as_a_link_and_the_entry_is_never_changed(tmp_path):
    """O71: the listing does not follow a link, so a locked file or directory outside the output that one names is
    never restored."""
    (tmp_path / "work" / "out").mkdir(parents=True)
    (tmp_path / "locked_dir").mkdir()
    (tmp_path / "locked.txt").write_text("secret\n", encoding="utf-8")
    os.symlink(tmp_path / "locked_dir", tmp_path / "work" / "out" / "d")
    os.symlink(tmp_path / "locked.txt", tmp_path / "work" / "out" / "f")
    lock(tmp_path / "locked_dir")
    lock(tmp_path / "locked.txt")
    done, entries = _list(tmp_path / "work")
    assert done.returncode == 0, done.stderr
    assert [(r.path, r.what) for r in copyout.check(entries, ["out"], max_bytes=None)[1]] == [
        ("out/d", "symbolic link"), ("out/f", "symbolic link"),
    ]  # fmt: skip
    assert [stat.S_IMODE(p.lstat().st_mode) for p in (tmp_path / "locked_dir", tmp_path / "locked.txt")] == [0, 0]


def test_o71_nothing_on_another_file_system_is_restored(tmp_path):
    """O71: only what lies on the listed file system is changed: an entry whose device is not the root's keeps its
    mode (the listing refuses it as another file system)."""
    module = _helper_module()
    (tmp_path / "d").mkdir()
    (tmp_path / "f").write_text("f\n", encoding="utf-8")
    lock(tmp_path / "d")
    lock(tmp_path / "f")
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        module.walk(directory, -1, 1, 0, copyout.MAX_PATH_BYTES, io.BytesIO())  # the root's device is another
    finally:
        os.close(directory)
    assert [stat.S_IMODE(p.lstat().st_mode) for p in (tmp_path / "d", tmp_path / "f")] == [0, 0]
