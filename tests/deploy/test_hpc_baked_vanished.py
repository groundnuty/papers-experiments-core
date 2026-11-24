"""A tree that changes under the check for the Couchbase values the images ship (deploy/hpc-federation/bundle/baked.py)
does not stop it, and the check still fails closed.

A live Couchbase compacts its bucket files while a provider's restart is checked, so data/<bucket>/50.couch.15 is
replaced by 50.couch.16 after os.walk listed it. A pass over the tree that finds a file or directory gone is discarded
whole and the scan starts again from the top, in SCAN_PASSES passes at most; only a pass that ends with no error
decides. The race is simulated deterministically: the module's lstat (or open) deletes a file the first time it is
asked for it, creates what a compaction would have written, and raises FileNotFoundError, as for a file that was listed
and is gone by the time it is read; fstat, mmap and the directory listing are made to lose a file the same way. The
values are a stand-in and its hash, as in test_hpc_baked_values.py; the real values never appear here. Sleeping is
recorded, never done, and every pass and pause is counted (`baked.watch`).
"""

import builtins
import errno
import hashlib
import importlib.util
import mmap
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

BAKED = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle" / "baked.py"
VALUE = b"qzjtwkcpmvbnrhsl"  # 16 lowercase letters, the cookies' form; not a real value
ENTRIES = [f"{len(VALUE)}:{hashlib.sha256(VALUE).hexdigest()}"]
HARMLESS = b"compacted bucket content, nothing shipped\n"
HOLDING = b"\x00" * 8 + VALUE + b"\x00" * 8  # a file with the value in it, in the binary form a bucket file has
NUMBERED = re.compile(r"/50\.couch\.(\d+)$")
PAUSE = 0.125  # the retry pause the tests patch in, so that a pause that is used is told from one that is not


def load():
    """The script's module, loaded under its own name."""
    spec = importlib.util.spec_from_file_location("baked_vanish_under_test", BAKED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def baked(monkeypatch):
    """The module with its sleeping recorded and never done, and its passes counted: `baked.watch` has `passes` (the
    number of scans started), `pruned` (the number of times the prune paths were worked out), `sleeps` (the seconds each
    pause was asked for) and `events` (("pass", n) and ("sleep", seconds), in order). The retry settings are given when
    the module has none, so that a version of the script without them runs the tests and fails them on what they check.
    """
    module = load()
    watch = SimpleNamespace(passes=0, pruned=0, sleeps=[], events=[])
    real_scan, real_pruned = module.scan, module.pruned

    def scan(*args, **kwargs):
        watch.passes += 1
        watch.events.append(("pass", watch.passes))
        return real_scan(*args, **kwargs)

    def pruned(*args, **kwargs):
        watch.pruned += 1
        return real_pruned(*args, **kwargs)

    def sleep(seconds):
        watch.sleeps.append(seconds)
        watch.events.append(("sleep", seconds))

    monkeypatch.setattr(module, "scan", scan)
    monkeypatch.setattr(module, "pruned", pruned)
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=sleep), raising=False)
    monkeypatch.setattr(module, "RETRY_PAUSE_S", PAUSE, raising=False)
    monkeypatch.setattr(module, "SCAN_PASSES", getattr(module, "SCAN_PASSES", 5), raising=False)
    module.watch = watch
    return module


def bucket_tree(top: Path, victim: str = "50.couch.15") -> Path:
    """A Couchbase data directory with a bucket of three files, the middle one the file that will vanish."""
    bucket = top / "data" / "default"
    bucket.mkdir(parents=True)
    for name in ("40.couch.3", victim, "60.couch.2"):
        (bucket / name).write_bytes(HARMLESS)
    (top / "var").mkdir()
    (top / "var" / "clean").write_bytes(HARMLESS)
    return bucket


def losing(monkeypatch, module, where, victims, replace=lambda path: None):
    """Make the module's lstat (where="lstat") or open (where="open") lose a file the way a compaction does: when
    victims(path, n) says so (n is the number of the pass running), and for each path once, the file is deleted,
    replace(path) runs (it writes the replacement) and FileNotFoundError is raised. Returns the paths lost, in order."""
    lost = []
    real_lstat, real_open = module.os.lstat, getattr(module, "open", builtins.open)

    def lose(path):
        path = str(path)
        if path not in lost and victims(path, module.watch.passes):
            os.unlink(path)
            lost.append(path)
            replace(path)
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)

    def lstat(path, *args, **kwargs):
        if where == "lstat":
            lose(path)
        return real_lstat(path, *args, **kwargs)

    def open_(path, *args, **kwargs):
        if where == "open":
            lose(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "lstat", lstat)
    monkeypatch.setattr(module, "open", open_, raising=False)
    return lost


def ghost(monkeypatch, module, where, target):
    """The first time the module opens target, its fstat (where="fstat") or its mmap (where="mmap") raises
    FileNotFoundError, once: a file that is gone after a successful open. The file itself stays where it is."""
    doomed, raised = set(), []
    real_open, real_fstat, real_mmap = getattr(module, "open", builtins.open), module.os.fstat, mmap.mmap

    def open_(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if str(path) == str(target) and not raised:
            doomed.add(handle.fileno())
        return handle

    def trip(fileno, name):
        if where == name and fileno in doomed:
            doomed.discard(fileno)
            raised.append(fileno)
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(target))

    def fstat(fileno):
        trip(fileno, "fstat")
        return real_fstat(fileno)

    def mapped(fileno, *args, **kwargs):
        trip(fileno, "mmap")
        return real_mmap(fileno, *args, **kwargs)

    monkeypatch.setattr(module.os, "fstat", fstat)
    monkeypatch.setattr(module, "mmap", SimpleNamespace(mmap=mapped, ACCESS_READ=mmap.ACCESS_READ))
    monkeypatch.setattr(module, "open", open_, raising=False)
    return raised


def failing(monkeypatch, module, where, target, err=errno.EACCES):
    """Make the module's lstat or open raise OSError(err) for the file at target, which stays where it is."""
    real_lstat, real_open = module.os.lstat, getattr(module, "open", builtins.open)

    def check(path, name):
        if where == name and str(path) == str(target):
            raise OSError(err, os.strerror(err), str(path))

    def lstat(path, *args, **kwargs):
        check(path, "lstat")
        return real_lstat(path, *args, **kwargs)

    def open_(path, *args, **kwargs):
        check(path, "open")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "lstat", lstat)
    monkeypatch.setattr(module, "open", open_, raising=False)


def opened(monkeypatch, module):
    """Record the paths the module opens, by the number of the pass that opened them."""
    reads = {}
    real_open = getattr(module, "open", builtins.open)

    def open_(path, *args, **kwargs):
        reads.setdefault(module.watch.passes, []).append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(module, "open", open_, raising=False)
    return reads


def read_by_any_pass(reads):
    """The paths `opened` recorded, of every pass."""
    return [path for paths in reads.values() for path in paths]


def listing(monkeypatch, module, directory, how):
    """Make the module's directory listing of `directory` lose it (how="gone": the directory is removed before it is
    listed, once) or refuse it (how="denied": PermissionError, every time)."""
    real_scandir = module.os.scandir

    def scandir(path="."):
        if str(path) == str(directory):
            if how == "denied":
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
            shutil.rmtree(path)
        return real_scandir(path)

    monkeypatch.setattr(module.os, "scandir", scandir)


def chain(bucket: Path, lost_count: int):
    """victims and replace for a bucket whose file 50.couch.K is lost for each K below lost_count, each loss writing
    50.couch.K+1, a compaction that goes on; 50.couch.<lost_count> is the file that stays and holds the value."""

    def victims(path, number):
        match = NUMBERED.search(path)
        return bool(match) and int(match.group(1)) < lost_count

    def replace(path):
        following = int(NUMBERED.search(path).group(1)) + 1
        (bucket / f"50.couch.{following}").write_bytes(HOLDING if following == lost_count else HARMLESS)

    return victims, replace


def test_o98_the_defaults_give_a_compaction_time_to_finish_between_several_passes():
    """O98: more than one pass, and a pause between two of them that is not zero."""
    module = load()
    assert module.SCAN_PASSES >= 3
    assert module.RETRY_PAUSE_S > 0


def test_o98_a_tree_that_holds_still_is_scanned_once_and_never_waited_for(tmp_path, baked, capsys):
    """O98: the control of the retry: no file lost is one pass and no pause, with or without a value in the tree."""
    bucket = bucket_tree(tmp_path)
    assert baked.main([str(tmp_path), *ENTRIES]) == 0
    assert (baked.watch.events, capsys.readouterr().out) == ([("pass", 1)], "")
    (bucket / "50.couch.16").write_bytes(HOLDING)
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(bucket / "50.couch.16")]
    assert baked.watch.passes == 2  # one more, from the second main


def test_o98_a_shipped_value_is_found_as_the_control_of_the_vanish_tests(tmp_path, baked, capsys):
    """O98: the stand-in is a wanted value: the same file where the race did not happen is found, so the vanish tests'
    verdicts rest on the file's content and on nothing else."""
    bucket = bucket_tree(tmp_path)
    (bucket / "50.couch.16").write_bytes(HOLDING)
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(bucket / "50.couch.16")]


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_value_in_the_replacement_of_a_vanished_file_is_found_by_the_next_pass(
    tmp_path, monkeypatch, capsys, baked, where
):
    """O98: 50.couch.15 is gone when it is read and the compaction's 50.couch.16 holds a shipped value: the pass is
    discarded, the next one lists the replacement and reads it (exit 1, the replacement named, the value not), after
    one pause, and the tree is not passed because the first file vanished."""
    bucket = bucket_tree(tmp_path)
    old, new = bucket / "50.couch.15", bucket / "50.couch.16"
    lost = losing(monkeypatch, baked, where, lambda path, n: path == str(old), lambda path: new.write_bytes(HOLDING))
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    shown = capsys.readouterr()
    assert lost == [str(old)]
    assert shown.out.splitlines() == [str(new)]
    assert VALUE.decode() not in shown.out + shown.err
    assert baked.watch.events == [("pass", 1), ("sleep", PAUSE), ("pass", 2)]


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_harmless_replacement_of_a_vanished_file_passes(tmp_path, monkeypatch, capsys, baked, where):
    """O98: the same race with a replacement that holds nothing shipped is a pass (exit 0, nothing printed), from the
    second pass."""
    bucket = bucket_tree(tmp_path)
    old, new = bucket / "50.couch.15", bucket / "50.couch.16"
    lost = losing(monkeypatch, baked, where, lambda path, n: path == str(old), lambda path: new.write_bytes(HARMLESS))
    assert baked.main([str(tmp_path), *ENTRIES]) == 0
    assert lost == [str(old)]
    assert capsys.readouterr().out == ""
    assert baked.watch.passes == 2


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_vanished_file_with_no_replacement_is_gone_and_holds_nothing(tmp_path, monkeypatch, capsys, baked, where):
    """O98: a file that vanished and was replaced by nothing is simply gone: the second pass passes (exit 0)."""
    bucket = bucket_tree(tmp_path)
    lost = losing(monkeypatch, baked, where, lambda path, n: path == str(bucket / "50.couch.15"))
    assert baked.main([str(tmp_path), *ENTRIES]) == 0
    assert lost == [str(bucket / "50.couch.15")]
    assert capsys.readouterr().out == ""
    assert baked.watch.passes == 2


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_vanished_name_that_is_back_with_a_value_is_found(tmp_path, monkeypatch, capsys, baked, where):
    """O98: the name itself comes back (a file written over its old path): the next pass lists it and reads it."""
    bucket = bucket_tree(tmp_path)
    same = bucket / "50.couch.15"
    losing(monkeypatch, baked, where, lambda path, n: path == str(same), lambda path: same.write_bytes(HOLDING))
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(same)]


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_value_in_a_file_that_appears_only_on_the_last_allowed_pass_is_found(
    tmp_path, monkeypatch, capsys, baked, where
):
    """O98: a compaction that replaces the file on every pass but the last: the file that stays, listed only by the
    last of SCAN_PASSES passes, holds a shipped value, and the bound lets that pass decide: exit 1, it is named, with a
    pause before each pass but the first."""
    bucket = bucket_tree(tmp_path, "50.couch.0")
    last = baked.SCAN_PASSES
    victims, replace = chain(bucket, last - 1)
    lost = losing(monkeypatch, baked, where, victims, replace)
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    shown = capsys.readouterr()
    assert len(lost) == last - 1  # each pass but the last lost one
    assert shown.out.splitlines() == [str(bucket / f"50.couch.{last - 1}")]
    assert VALUE.decode() not in shown.out + shown.err
    assert baked.watch.passes == last
    assert baked.watch.sleeps == [PAUSE] * (last - 1)


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_value_in_a_file_one_pass_past_the_bound_is_never_passed(tmp_path, monkeypatch, capsys, baked, where):
    """O98: the same compaction one pass longer: the file with the value would be listed by the pass after the last
    allowed one, so the scan does not pass the tree (exit 2, it did not hold still) and prints no value."""
    bucket = bucket_tree(tmp_path, "50.couch.0")
    last = baked.SCAN_PASSES
    victims, replace = chain(bucket, last)
    lost = losing(monkeypatch, baked, where, victims, replace)
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert len(lost) == last
    assert "did not hold still" in shown.err
    assert shown.out == ""
    assert VALUE.decode() not in shown.err
    assert baked.watch.passes == last


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_tree_that_loses_a_file_on_every_pass_fails_the_scan_after_the_last_one(
    tmp_path, monkeypatch, capsys, baked, where
):
    """O98: a tree that will not hold still is never passed: a compaction that never ends loses a listed file on each
    of SCAN_PASSES passes, with the pause recorded between two passes and none after the last, and the scan fails
    (exit 2), saying that the tree did not hold still, with nothing on stdout."""
    bucket = bucket_tree(tmp_path, "50.couch.0")
    last = baked.SCAN_PASSES
    victims, replace = chain(bucket, 10**6)
    lost = losing(monkeypatch, baked, where, victims, replace)
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert len(lost) == last
    assert "did not hold still" in shown.err
    assert str(last) in shown.err
    assert shown.out == ""
    assert baked.watch.passes == last
    assert baked.watch.sleeps == [PAUSE] * (last - 1)


def test_o98_a_pass_that_found_a_value_and_then_lost_a_file_is_discarded_and_a_later_pass_finds_it_again(
    tmp_path, monkeypatch, capsys, baked
):
    """O98: 40.couch.3 holds a value and is read before 50.couch.15 vanishes: the first pass is discarded, the second
    reads 40.couch.3 again and decides (exit 1, the path once)."""
    bucket = bucket_tree(tmp_path)
    early = bucket / "40.couch.3"
    early.write_bytes(HOLDING)
    losing(monkeypatch, baked, "lstat", lambda path, n: path == str(bucket / "50.couch.15"))
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    shown = capsys.readouterr()
    assert shown.out.splitlines() == [str(early)]
    assert VALUE.decode() not in shown.out + shown.err
    assert baked.watch.passes == 2


def test_o98_what_a_discarded_pass_found_is_never_reported_unless_the_deciding_pass_finds_it(
    tmp_path, monkeypatch, capsys, baked
):
    """O98: the value found in 40.couch.3 before the loss is moved by the compaction into 50.couch.16: only the file
    the complete pass reads it in is named, and what the discarded pass found is not kept."""
    bucket = bucket_tree(tmp_path)
    early, new = bucket / "40.couch.3", bucket / "50.couch.16"
    early.write_bytes(HOLDING)

    def compact(path):
        early.write_bytes(HARMLESS)
        new.write_bytes(HOLDING)

    losing(monkeypatch, baked, "lstat", lambda path, n: path == str(bucket / "50.couch.15"), compact)
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(new)]
    assert baked.watch.passes == 2


@pytest.mark.parametrize("where", ["fstat", "mmap"])
def test_o98_a_file_gone_at_its_fstat_or_its_mmap_after_a_good_open_discards_the_pass(
    tmp_path, monkeypatch, capsys, baked, where
):
    """O98: FileNotFoundError after the open succeeded is no reason to skip the file: the pass is discarded and the
    next one reads the file (it holds a value: exit 1, named). Skipping it would pass the tree (exit 0)."""
    bucket = bucket_tree(tmp_path)
    holding = bucket / "50.couch.15"
    holding.write_bytes(HOLDING)
    raised = ghost(monkeypatch, baked, where, holding)
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    shown = capsys.readouterr()
    assert len(raised) == 1
    assert shown.out.splitlines() == [str(holding)]
    assert VALUE.decode() not in shown.out + shown.err
    assert baked.watch.events == [("pass", 1), ("sleep", PAUSE), ("pass", 2)]


@pytest.mark.parametrize("where", ["fstat", "mmap"])
def test_o98_a_value_beside_a_file_gone_at_its_fstat_or_its_mmap_is_found_by_the_next_pass(
    tmp_path, monkeypatch, capsys, baked, where
):
    """O98: the file that is gone after its open holds nothing and a file after it in the walk holds a value: the
    scan neither stops at the first nor passes the tree; the second pass reads both."""
    bucket = bucket_tree(tmp_path)
    (bucket / "60.couch.2").write_bytes(HOLDING)
    ghost(monkeypatch, baked, where, bucket / "50.couch.15")
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(bucket / "60.couch.2")]
    assert baked.watch.passes == 2


@pytest.mark.parametrize("holding", [False, True])
def test_o98_a_directory_gone_mid_walk_discards_the_pass_and_a_later_pass_decides(
    tmp_path, monkeypatch, capsys, baked, holding
):
    """O98: a directory the walk listed and cannot open because it is gone discards the pass; the next pass does not
    list it and reads the rest, so a value in a file of the tree is found (exit 1, the file once) and a tree without
    one passes (exit 0)."""
    bucket = bucket_tree(tmp_path)
    (tmp_path / "data" / "sub").mkdir()
    if holding:
        (bucket / "50.couch.15").write_bytes(HOLDING)
    listing(monkeypatch, baked, tmp_path / "data" / "sub", "gone")
    assert baked.main([str(tmp_path), *ENTRIES]) == (1 if holding else 0)
    assert capsys.readouterr().out.splitlines() == ([str(bucket / "50.couch.15")] if holding else [])
    assert baked.watch.passes == 2


def test_o98_a_directory_that_is_gone_on_every_pass_fails_the_scan(tmp_path, monkeypatch, capsys, baked):
    """O98: a tree whose directories go on vanishing is never passed: a directory made again before each pass is gone
    when it is listed, and the scan fails (exit 2) after SCAN_PASSES passes, naming none of what it read."""
    bucket = bucket_tree(tmp_path)
    (bucket / "50.couch.15").write_bytes(HOLDING)
    sub = tmp_path / "data" / "sub"
    sub.mkdir()
    real_scandir = baked.os.scandir

    def scandir(path="."):
        if str(path) == str(tmp_path):  # the top is listed once per pass: the directory is there again
            sub.mkdir(exist_ok=True)
        if str(path) == str(sub):
            shutil.rmtree(path)
        return real_scandir(path)

    monkeypatch.setattr(baked.os, "scandir", scandir)
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert "did not hold still" in shown.err
    assert shown.out == ""
    assert baked.watch.passes == baked.SCAN_PASSES


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_another_error_on_a_file_fails_the_scan_at_once(tmp_path, monkeypatch, capsys, baked, where):
    """O98: only FileNotFoundError is a reason for another pass: a file that cannot be read (EACCES) at its lstat or
    its open is an error (exit 2) on the first pass, with no pause and no second pass."""
    bucket = bucket_tree(tmp_path)
    failing(monkeypatch, baked, where, bucket / "50.couch.15")
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert "Permission denied" in shown.err
    assert shown.out == ""
    assert baked.watch.passes == 1
    assert baked.watch.sleeps == []


def test_o98_a_directory_that_cannot_be_listed_fails_the_scan_at_once(tmp_path, monkeypatch, capsys, baked):
    """O98: a directory refused with EACCES is an error on the first pass, never another pass."""
    bucket_tree(tmp_path)
    (tmp_path / "data" / "sub").mkdir()
    listing(monkeypatch, baked, tmp_path / "data" / "sub", "denied")
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    assert "Permission denied" in capsys.readouterr().err
    assert (baked.watch.passes, baked.watch.sleeps) == (1, [])


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_replacement_that_cannot_be_read_fails_the_scan_too(tmp_path, monkeypatch, capsys, baked, where):
    """O98: the second pass reads as the first does: a replacement that raises EACCES is an error (exit 2) at once, not
    skipped and not tried again."""
    bucket = bucket_tree(tmp_path)
    new = bucket / "50.couch.16"
    failing(monkeypatch, baked, where, new)
    losing(
        monkeypatch,
        baked,
        where,
        lambda path, n: path == str(bucket / "50.couch.15"),
        lambda path: new.write_bytes(HARMLESS),
    )
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    assert "Permission denied" in capsys.readouterr().err
    assert (baked.watch.passes, baked.watch.sleeps) == (2, [PAUSE])


def test_o98_a_missing_directory_is_one_error_not_a_tree_that_will_not_hold_still(tmp_path, capsys, baked):
    """O98: a DIR that is not there is no compaction: exit 2 on the spot, without a pause, with the system's words."""
    assert baked.main([str(tmp_path / "missing"), *ENTRIES]) == 2
    assert "No such file or directory" in capsys.readouterr().err
    assert baked.watch.sleeps == []
    assert baked.watch.passes <= 1


def test_o98_the_deciding_pass_reads_every_regular_file_as_a_single_pass_does(tmp_path, monkeypatch, capsys, baked):
    """O98: what a complete pass reads is what a scan of a tree that holds still reads: every regular file os.walk
    lists, once, the discarded pass's reads not counted."""
    bucket = bucket_tree(tmp_path)
    old, new = bucket / "50.couch.15", bucket / "50.couch.16"
    reads = opened(monkeypatch, baked)
    losing(monkeypatch, baked, "lstat", lambda path, n: path == str(old), lambda path: new.write_bytes(HARMLESS))
    assert baked.main([str(tmp_path), *ENTRIES]) == 0
    assert capsys.readouterr().out == ""
    assert sorted(reads[2]) == sorted(str(path) for path in tmp_path.rglob("*") if path.is_file())
    assert len(reads[2]) == 4


@pytest.mark.parametrize(
    ("options", "left_out"),
    [(("--prune", "data/default/cache.bin"), "cache.bin"), (("--prune-each", "data/default/shards"), "shards/entry")],
)
def test_o98_a_pruned_path_beside_a_vanished_file_stays_pruned_on_every_pass(
    tmp_path, monkeypatch, capsys, baked, options, left_out
):
    """O98: the prune rules are worked out once per pass and a pruned entry, holding a shipped value, is read by none.
    The same value in the replacement, which is not pruned, is found."""
    bucket = bucket_tree(tmp_path)
    (bucket / left_out).parent.mkdir(exist_ok=True)
    (bucket / left_out).write_bytes(HOLDING)
    new = bucket / "50.couch.16"
    reads = opened(monkeypatch, baked)
    old = str(bucket / "50.couch.15")
    lost = losing(monkeypatch, baked, "lstat", lambda path, n: path == old, lambda path: new.write_bytes(HARMLESS))
    code = baked.main([*options, str(tmp_path), *ENTRIES])
    shown = capsys.readouterr()
    assert lost == [old]
    assert (code, shown.out) == (0, ""), shown.err
    assert (baked.watch.passes, baked.watch.pruned) == (2, 2)
    assert str(bucket / left_out) not in read_by_any_pass(reads)
    new.write_bytes(HOLDING)  # the control: a file of the listing that is not pruned is read
    assert baked.main([*options, str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(new)]


@pytest.mark.parametrize(
    ("options", "left_out"),
    [(("--prune", "data/default/50.couch.15"), "50.couch.15"), (("--prune-each", "data/default/shards"), "shards/old")],
)
def test_o98_a_file_under_a_pruned_path_is_never_touched_by_any_pass(
    tmp_path, monkeypatch, capsys, baked, options, left_out
):
    """O98: the race is armed for a pruned file (it holds a shipped value) and for a file of the same directory that
    is not pruned. Only the second vanishes: no pass lstat's or reads the pruned one, so it cannot vanish, and the
    pass that follows the other's loss leaves it out again: exit 0, the pruned file never opened."""
    bucket = bucket_tree(tmp_path)
    (bucket / left_out).parent.mkdir(exist_ok=True)
    (bucket / left_out).write_bytes(HOLDING)
    walking, counted = [], baked.pruned

    def pruned(*args, **kwargs):
        # The prune paths are checked with lstat, and that is no read: the race is armed once they are, for each pass.
        walking.clear()
        result = counted(*args, **kwargs)
        walking.append(True)
        return result

    monkeypatch.setattr(baked, "pruned", pruned)
    reads = opened(monkeypatch, baked)
    armed = {str(bucket / left_out), str(bucket / "60.couch.2")}
    lost = losing(
        monkeypatch,
        baked,
        "lstat",
        lambda path, n: bool(walking) and path in armed,
        lambda path: (bucket / "61.couch.3").write_bytes(HARMLESS),
    )
    code = baked.main([*options, str(tmp_path), *ENTRIES])
    assert (code, capsys.readouterr().out) == (0, "")
    assert lost == [str(bucket / "60.couch.2")]
    assert (bucket / left_out).is_file()
    assert str(bucket / left_out) not in read_by_any_pass(reads)
    assert str(bucket / "61.couch.3") in reads[2]


def test_o98_a_prune_path_that_is_a_link_is_refused_at_once(tmp_path, capsys, baked):
    """O98: as without any race, a prune path that is a link is an error on the first pass (exit 2), never another."""
    bucket = bucket_tree(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (bucket / "cache.bin").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert baked.main(["--prune", "data/default/cache.bin", str(tmp_path), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert "is a link" in shown.err
    assert shown.out == ""
    assert (baked.watch.passes, baked.watch.sleeps) == (1, [])


def test_o98_a_pruned_path_that_becomes_a_link_between_two_passes_is_refused_not_skipped(
    tmp_path, monkeypatch, capsys, baked
):
    """O98: the second pass works the prune paths out again: a pruned entry that was a file on the first pass and is a
    link by the second is the link error (exit 2), not an entry quietly left out of the walk."""
    top = tmp_path / "volumes"
    bucket = bucket_tree(top)
    (bucket / "cache.bin").write_bytes(HARMLESS)
    (tmp_path / "elsewhere").write_bytes(HOLDING)

    def swap(path):
        (bucket / "cache.bin").unlink()
        (bucket / "cache.bin").symlink_to(tmp_path / "elsewhere")

    losing(monkeypatch, baked, "lstat", lambda path, n: path == str(bucket / "50.couch.15"), swap)
    assert baked.main(["--prune", "data/default/cache.bin", str(top), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert "is a link" in shown.err
    assert shown.out == ""
    assert baked.watch.passes == 2


def test_o98_a_directory_swapped_for_a_link_between_two_passes_is_not_followed(tmp_path, monkeypatch, capsys, baked):
    """O98: the second pass walks without following links: a directory that became a link to a tree outside, which
    holds a shipped value, is not entered (exit 0), as a link never is."""
    top = tmp_path / "volumes"
    bucket = bucket_tree(top)
    (top / "data" / "sub").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(HOLDING)

    def swap(path):
        (top / "data" / "sub").rmdir()
        (top / "data" / "sub").symlink_to(outside, target_is_directory=True)

    reads = opened(monkeypatch, baked)
    losing(monkeypatch, baked, "lstat", lambda path, n: path == str(bucket / "50.couch.15"), swap)
    assert baked.main([str(top), *ENTRIES]) == 0
    assert capsys.readouterr().out == ""
    assert str(outside / "secret") not in read_by_any_pass(reads)
    assert baked.watch.passes == 2
