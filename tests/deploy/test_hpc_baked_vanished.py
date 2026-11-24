"""A file that vanishes between the listing of its directory and its read does not stop the check for the Couchbase
values the images ship (deploy/hpc-federation/bundle/baked.py), and the check still fails closed.

A live Couchbase compacts its bucket files while a provider's restart is checked, so data/<bucket>/50.couch.15 is
replaced by 50.couch.16 after os.walk listed it. The race is simulated deterministically: the module's lstat (or open)
deletes a file the first time it is asked for it, creates what a compaction would have written, and raises
FileNotFoundError, as for a file that was listed and is gone by the time it is read. The values are a stand-in and its
hash, as in test_hpc_baked_values.py; the real values never appear here.
"""

import builtins
import errno
import hashlib
import importlib.util
import os
import re
import shutil
from pathlib import Path

import pytest

BAKED = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle" / "baked.py"
VALUE = b"qzjtwkcpmvbnrhsl"  # 16 lowercase letters, the cookies' form; not a real value
ENTRIES = [f"{len(VALUE)}:{hashlib.sha256(VALUE).hexdigest()}"]
HARMLESS = b"compacted bucket content, nothing shipped\n"
HOLDING = b"\x00" * 8 + VALUE + b"\x00" * 8  # a file with the value in it, in the binary form a bucket file has
NUMBERED = re.compile(r"/50\.couch\.(\d+)$")


@pytest.fixture
def baked():
    """The script's module, loaded under its own name; each test patches its own copy of the module's os and open."""
    spec = importlib.util.spec_from_file_location("baked_vanish_under_test", BAKED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bucket_tree(tmp_path: Path, victim: str = "50.couch.15") -> Path:
    """A Couchbase data directory with a bucket of three files, the middle one the file that will vanish."""
    bucket = tmp_path / "data" / "default"
    bucket.mkdir(parents=True)
    for name in ("40.couch.3", victim, "60.couch.2"):
        (bucket / name).write_bytes(HARMLESS)
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "clean").write_bytes(HARMLESS)
    return bucket


def losing(monkeypatch, module, where, victims, replace=lambda path: None):
    """Make the module's lstat (where="lstat") or open (where="open") lose a file the way a compaction does: when
    victims(path) says so, and for each path once, the file is deleted, replace(path) runs (it writes the replacement)
    and FileNotFoundError is raised. Returns the list of the paths lost, in order."""
    lost = []
    real_lstat, real_open = module.os.lstat, getattr(module, "open", builtins.open)

    def lose(path):
        path = str(path)
        if path not in lost and victims(path):
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
    """Record the paths the module opens, in order."""
    paths = []
    real_open = getattr(module, "open", builtins.open)

    def open_(path, *args, **kwargs):
        paths.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(module, "open", open_, raising=False)
    return paths


def chain(bucket: Path, lost_count: int):
    """victims and replace for a bucket whose file 50.couch.K is lost for each K below lost_count, each loss writing
    50.couch.K+1, a compaction that goes on; 50.couch.<lost_count> is the file that stays and holds the value."""

    def victims(path):
        match = NUMBERED.search(path)
        return bool(match) and int(match.group(1)) < lost_count

    def replace(path):
        following = int(NUMBERED.search(path).group(1)) + 1
        (bucket / f"50.couch.{following}").write_bytes(HOLDING if following == lost_count else HARMLESS)

    return victims, replace


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_value_in_the_replacement_of_a_vanished_file_is_found(tmp_path, monkeypatch, capsys, baked, where):
    """O98: 50.couch.15 is gone when it is read and the compaction's 50.couch.16 holds a shipped value: the scan reads
    the replacement from a fresh listing of the directory, finds the value (exit 1, the replacement named, the value
    not) and does not pass the tree because the first file vanished."""
    bucket = bucket_tree(tmp_path)
    old, new = bucket / "50.couch.15", bucket / "50.couch.16"
    lost = losing(monkeypatch, baked, where, lambda path: path == str(old), lambda path: new.write_bytes(HOLDING))
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    shown = capsys.readouterr()
    assert lost == [str(old)]
    assert shown.out.splitlines() == [str(new)]
    assert VALUE.decode() not in shown.out + shown.err


def test_o98_a_shipped_value_is_found_as_the_control_of_the_vanish_tests(tmp_path, baked, capsys):
    """O98: the stand-in is a wanted value: the same file where the race did not happen is found, so the vanish tests'
    verdicts rest on the file's content and on nothing else."""
    bucket = bucket_tree(tmp_path)
    (bucket / "50.couch.16").write_bytes(HOLDING)
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(bucket / "50.couch.16")]


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_harmless_replacement_of_a_vanished_file_passes(tmp_path, monkeypatch, capsys, baked, where):
    """O98: the same race with a replacement that holds nothing shipped is a pass (exit 0, nothing printed)."""
    bucket = bucket_tree(tmp_path)
    old, new = bucket / "50.couch.15", bucket / "50.couch.16"
    lost = losing(monkeypatch, baked, where, lambda path: path == str(old), lambda path: new.write_bytes(HARMLESS))
    assert baked.main([str(tmp_path), *ENTRIES]) == 0
    assert lost == [str(old)]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_vanished_file_with_no_replacement_is_gone_and_holds_nothing(tmp_path, monkeypatch, capsys, baked, where):
    """O98: a file that vanished and was replaced by nothing is simply gone: the scan passes (exit 0)."""
    bucket = bucket_tree(tmp_path)
    lost = losing(monkeypatch, baked, where, lambda path: path == str(bucket / "50.couch.15"))
    assert baked.main([str(tmp_path), *ENTRIES]) == 0
    assert lost == [str(bucket / "50.couch.15")]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_vanished_name_that_is_back_with_a_value_is_found(tmp_path, monkeypatch, capsys, baked, where):
    """O98: the name itself comes back (a file written over its old path): the listing shows it, it was never read in
    that form, and it is read."""
    bucket = bucket_tree(tmp_path)
    same = bucket / "50.couch.15"
    losing(monkeypatch, baked, where, lambda path: path == str(same), lambda path: same.write_bytes(HOLDING))
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(same)]


def test_o98_a_file_read_before_the_vanish_and_rewritten_since_is_read_again_in_its_new_form(
    tmp_path, monkeypatch, capsys, baked
):
    """O98: a file read earlier in the directory and rewritten before the fresh listing is not "read in its current
    form": it is read again and its value is found; one that is unchanged is not read a second time."""
    bucket = bucket_tree(tmp_path)
    early, untouched = bucket / "40.couch.3", bucket / "60.couch.2"
    reads = opened(monkeypatch, baked)
    losing(
        monkeypatch,
        baked,
        "lstat",
        lambda path: path == str(bucket / "50.couch.15"),
        lambda path: early.write_bytes(HARMLESS + HOLDING),
    )
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(early)]
    assert reads.count(str(early)) == 2
    assert reads.count(str(untouched)) == 1


def test_o98_a_rewrite_that_keeps_the_size_and_the_modification_time_is_read_again_too(
    tmp_path, monkeypatch, capsys, baked
):
    """O98: a file read earlier and rewritten in place with the same size, its modification time put back, still differs
    by its change time: the fresh listing reads it and finds its value."""
    bucket = bucket_tree(tmp_path)
    early = bucket / "40.couch.3"
    before = early.stat()

    def rewrite(path):
        early.write_bytes(VALUE + b"." * (len(HARMLESS) - len(VALUE)))
        os.utime(early, ns=(before.st_atime_ns, before.st_mtime_ns))

    losing(monkeypatch, baked, "lstat", lambda path: path == str(bucket / "50.couch.15"), rewrite)
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(early)]


def test_o98_an_unchanged_file_is_not_read_again_by_the_fresh_listing(tmp_path, monkeypatch, baked):
    """O98: only what was not read in its current form is read on a fresh listing: three files, one lost, a replacement
    written, and each of the others is opened once."""
    bucket = bucket_tree(tmp_path)
    reads = opened(monkeypatch, baked)
    new = bucket / "50.couch.16"
    losing(
        monkeypatch,
        baked,
        "lstat",
        lambda path: path == str(bucket / "50.couch.15"),
        lambda p: new.write_bytes(HARMLESS),
    )
    assert baked.main([str(tmp_path), *ENTRIES]) == 0
    expected = [str(bucket / name) for name in ("40.couch.3", "50.couch.16", "60.couch.2")]
    assert sorted(reads) == sorted([*expected, str(tmp_path / "var" / "clean")])


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_value_in_a_file_that_appears_only_on_the_last_allowed_listing_is_found(
    tmp_path, monkeypatch, capsys, baked, where
):
    """O98: a compaction that replaces the file at the walk and again at every fresh listing but the last: the file
    that stays, listed only at RELIST_PASSES, holds a shipped value, and the bound lets it be read: exit 1, it is named.
    """
    bucket = bucket_tree(tmp_path, "50.couch.0")
    last = baked.RELIST_PASSES
    victims, replace = chain(bucket, last)
    lost = losing(monkeypatch, baked, where, victims, replace)
    assert baked.main([str(tmp_path), *ENTRIES]) == 1
    shown = capsys.readouterr()
    assert len(lost) == last  # the walk and each listing before the last lost one
    assert shown.out.splitlines() == [str(bucket / f"50.couch.{last}")]
    assert VALUE.decode() not in shown.out + shown.err


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_value_in_a_file_one_listing_past_the_bound_is_never_passed(tmp_path, monkeypatch, capsys, baked, where):
    """O98: the same compaction one listing longer: the file with the value would be the one after the last allowed
    listing, so the scan does not pass the directory (exit 2, the directory named) and prints no value."""
    bucket = bucket_tree(tmp_path, "50.couch.0")
    victims, replace = chain(bucket, baked.RELIST_PASSES + 1)
    lost = losing(monkeypatch, baked, where, victims, replace)
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert len(lost) == baked.RELIST_PASSES + 1
    assert str(bucket) in shown.err
    assert shown.out == ""
    assert VALUE.decode() not in shown.err


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_directory_that_loses_a_listed_file_on_every_pass_fails_the_scan_naming_it(
    tmp_path, monkeypatch, capsys, baked, where
):
    """O98: a directory that will not hold still is never passed: a compaction that never ends loses a listed file at
    the walk and at each of the RELIST_PASSES listings, then the scan fails (exit 2) with the directory in its message.
    """
    bucket = bucket_tree(tmp_path, "50.couch.0")
    victims, replace = chain(bucket, 10**6)
    lost = losing(monkeypatch, baked, where, victims, replace)
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert len(lost) == baked.RELIST_PASSES + 1
    assert str(bucket) in shown.err
    assert "does not hold still" in shown.err
    assert shown.out == ""


@pytest.mark.parametrize(
    ("options", "left_out"),
    [(("--prune", "data/default/cache.bin"), "cache.bin"), (("--prune-each", "data/default/shards"), "shards/entry")],
)
def test_o98_a_pruned_path_beside_a_vanished_file_stays_pruned_on_the_fresh_listing(
    tmp_path, monkeypatch, capsys, baked, options, left_out
):
    """O98: the directory is listed again and a pruned entry in it, holding a shipped value, is not read: the prune
    rules of the walk are the re-listing's. The same value in the replacement, which is not pruned, is found."""
    bucket = bucket_tree(tmp_path)
    (bucket / left_out).parent.mkdir(exist_ok=True)
    (bucket / left_out).write_bytes(HOLDING)
    new = bucket / "50.couch.16"
    reads = opened(monkeypatch, baked)
    old = str(bucket / "50.couch.15")
    lost = losing(monkeypatch, baked, "lstat", lambda path: path == old, lambda path: new.write_bytes(HARMLESS))
    code = baked.main([*options, str(tmp_path), *ENTRIES])
    shown = capsys.readouterr()
    assert lost == [old]
    assert (code, shown.out) == (0, ""), shown.err
    assert str(bucket / left_out) not in reads
    new.write_bytes(HOLDING)  # the control: a file of the listing that is not pruned is read
    assert baked.main([*options, str(tmp_path), *ENTRIES]) == 1
    assert capsys.readouterr().out.splitlines() == [str(new)]


@pytest.mark.parametrize(
    ("options", "left_out"),
    [(("--prune", "data/default/50.couch.15"), "50.couch.15"), (("--prune-each", "data/default/shards"), "shards/old")],
)
def test_o98_a_file_under_a_pruned_path_is_never_touched_by_the_walk_or_the_fresh_listing(
    tmp_path, monkeypatch, capsys, baked, options, left_out
):
    """O98: the race is armed for a pruned file (it holds a shipped value) and for a file of the same directory that
    is not pruned. Only the second vanishes: the walk never lstat's or reads the pruned one, so it cannot vanish, and
    the fresh listing that follows the other's loss leaves it out again: exit 0, the pruned file never opened."""
    bucket = bucket_tree(tmp_path)
    (bucket / left_out).parent.mkdir(exist_ok=True)
    (bucket / left_out).write_bytes(HOLDING)
    walking = []  # the prune paths are checked with lstat before the walk, and a link check is no read: arm at the walk
    real_walk = baked.os.walk

    def walk(*args, **kwargs):
        walking.append(True)
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(baked.os, "walk", walk)
    reads = opened(monkeypatch, baked)
    armed = {str(bucket / left_out), str(bucket / "60.couch.2")}
    lost = losing(
        monkeypatch,
        baked,
        "lstat",
        lambda path: bool(walking) and path in armed,
        lambda path: (bucket / "61.couch.3").write_bytes(HARMLESS),
    )
    code = baked.main([*options, str(tmp_path), *ENTRIES])
    assert (code, capsys.readouterr().out) == (0, "")
    assert lost == [str(bucket / "60.couch.2")]
    assert (bucket / left_out).is_file()
    assert str(bucket / left_out) not in reads
    assert str(bucket / "61.couch.3") in reads


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_another_error_on_a_file_still_fails_the_scan(tmp_path, monkeypatch, capsys, baked, where):
    """O98: only FileNotFoundError is tolerated: a file that cannot be read (EACCES) at its lstat or its open is an
    error (exit 2), with no fresh listing to excuse it."""
    bucket = bucket_tree(tmp_path)
    failing(monkeypatch, baked, where, bucket / "50.couch.15")
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    shown = capsys.readouterr()
    assert "Permission denied" in shown.err
    assert shown.out == ""


@pytest.mark.parametrize("where", ["lstat", "open"])
def test_o98_a_replacement_that_cannot_be_read_fails_the_scan_too(tmp_path, monkeypatch, capsys, baked, where):
    """O98: the fresh listing reads as the walk does: a replacement that raises EACCES is an error, not skipped."""
    bucket = bucket_tree(tmp_path)
    new = bucket / "50.couch.16"
    failing(monkeypatch, baked, where, new)
    losing(
        monkeypatch, baked, where, lambda path: path == str(bucket / "50.couch.15"), lambda p: new.write_bytes(HARMLESS)
    )
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    assert "Permission denied" in capsys.readouterr().err


def test_o98_a_subdirectory_that_vanishes_mid_walk_still_fails_the_scan(tmp_path, monkeypatch, capsys, baked):
    """O98: only files are tolerated: a directory the walk listed and cannot open is an error (exit 2) as before."""
    bucket_tree(tmp_path)
    (tmp_path / "data" / "sub").mkdir()
    real_scandir = baked.os.scandir

    def scandir(path="."):
        if str(path) == str(tmp_path / "data" / "sub"):
            shutil.rmtree(path)
        return real_scandir(path)

    monkeypatch.setattr(baked.os, "scandir", scandir)
    assert baked.main([str(tmp_path), *ENTRIES]) == 2
    assert "No such file or directory" in capsys.readouterr().err
