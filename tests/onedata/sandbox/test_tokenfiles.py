"""What a teardown finds where a copy of the data-path token should be gone: examined, never read (O05)."""

import os
from pathlib import Path

from fake_host import FakeHost

from expcore_onedata.sandbox import tokenfiles

SECRET = "tok-invented-0123456789"


def test_o05_each_kind_of_thing_at_a_token_path_is_named_by_its_own_metadata(tmp_path):
    """O05: file, directory, symlink and other (a FIFO), with the size and owner `lstat` shows, never followed."""
    (tmp_path / "file").write_text(SECRET, encoding="utf-8")
    (tmp_path / "directory").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "file")
    os.mkfifo(tmp_path / "fifo")
    owner = os.getuid()
    assert tokenfiles.facts(tmp_path / "file") == {"type": "file", "size": len(SECRET), "uid": owner}
    assert tokenfiles.facts(tmp_path / "directory")["type"] == "directory"
    assert tokenfiles.facts(tmp_path / "link")["type"] == "symlink"  # not "file": the target is not followed
    assert tokenfiles.facts(tmp_path / "fifo")["type"] == "other"
    assert tokenfiles.facts(tmp_path / "missing") == {"type": "unknown", "size": None, "uid": None}


def test_o05_only_a_directory_that_lists_with_no_entry_is_empty(tmp_path):
    """O05: one non-recursive listing of names; a directory with an entry, one that cannot be listed and what is not a
    directory are never empty."""
    (tmp_path / "empty").mkdir()
    (tmp_path / "with-entry").mkdir()
    (tmp_path / "with-entry" / "x").write_text(SECRET, encoding="utf-8")
    (tmp_path / "file").write_text("", encoding="utf-8")
    assert tokenfiles.is_empty_directory(tmp_path / "empty")
    assert not tokenfiles.is_empty_directory(tmp_path / "with-entry")
    assert not tokenfiles.is_empty_directory(tmp_path / "file")
    assert not tokenfiles.is_empty_directory(tmp_path / "missing")


def test_o05_a_removal_that_leaves_nothing_returns_none_and_a_missing_path_is_not_an_error(tmp_path):
    """O05: `rm -f` of a file removes it; of a path that is not there, nothing happens."""
    host = FakeHost()
    (tmp_path / "token").write_text(SECRET, encoding="utf-8")
    assert tokenfiles.remove(host, tmp_path / "token", tmp_path) is None
    assert not (tmp_path / "token").exists()
    assert tokenfiles.remove(host, tmp_path / "token", tmp_path) is None


def test_o05_a_removal_that_leaves_something_says_what_by_a_path_in_the_run_directory_and_reads_nothing(tmp_path):
    """O05: the record is the path relative to the run's directory, its kind, size, owner and the action; a directory
    with an entry is not removed (nothing but `rm -f` ran) and its entry is not read."""
    host = FakeHost()
    run = tmp_path / "run"
    (run / "secret").mkdir(parents=True)
    kept = run / "secret" / "token"
    kept.mkdir()
    (kept / "entry").write_text(SECRET, encoding="utf-8")
    found = tokenfiles.remove(host, kept, run)
    assert found is not None
    assert found["path"] == "secret/token"
    assert (found["type"], found["action"]) == ("directory", tokenfiles.DIRECTORY_LEFT)
    assert SECRET not in repr(found)
    assert [call[1][0] for call in host.calls if call[0] == "run"] == ["rm"]  # no `rmdir`, nothing recursive
    assert tokenfiles.stuck([found]) == [found]
    assert (kept / "entry").read_text(encoding="utf-8") == SECRET


def test_o05_an_empty_directory_is_removed_by_rmdir_and_is_not_stuck(tmp_path):
    """O05: the record of an empty directory removed is `empty directory left by a bind; removed`, and it is no
    failure."""
    host = FakeHost()
    run = tmp_path / "run"
    (run / "secret" / "token").mkdir(parents=True)
    found = tokenfiles.remove(host, run / "secret" / "token", run)
    assert found is not None
    assert found["action"] == tokenfiles.EMPTY_DIRECTORY_REMOVED == "empty directory left by a bind; removed"
    assert not (run / "secret" / "token").exists()
    assert tokenfiles.stuck([found]) == []


def test_o05_a_path_outside_the_run_directory_is_named_by_its_last_component_only(tmp_path):
    """O05: a path that is not under the run's directory never puts its parents, which are configured values, in a
    record."""
    host = FakeHost()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "token").mkdir()
    (elsewhere / "token" / "entry").write_text(SECRET, encoding="utf-8")
    found = tokenfiles.remove(host, elsewhere / "token", Path(tmp_path / "run"))
    assert found is not None
    assert found["path"] == "token"
