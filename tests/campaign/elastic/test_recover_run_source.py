"""`recover-run` and the sealed final: it is told from the working copy by device and inode before any connection to
the working copy is opened (a connection sets the journal mode, so a sealed file reached through the wrong path would
be written and get a `-wal` and `-shm`), and its bytes are read once into a private copy, so the SHA-256 recorded and
the rows judged are the same bytes; the path is looked at again at the decision under the write lock."""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from recover_support import another_fingerprint, lost_run
from recover_support import recover_found as _recover
from reopen_support import dump, sha256_of

from expcore.campaign.elastic import recoverrun as module
from expcore.campaign.elastic import schema
from expcore.campaign.elastic.records import RecoverRefused


def _forbid_connection(monkeypatch) -> None:
    """Fail the test the moment the working copy is opened: the refusal is decided before it."""

    def forbidden(path):
        raise AssertionError(f"{path} was opened for writing before the sealed final was told from it")

    monkeypatch.setattr(schema, "connect", forbidden)


def _directory(path: Path) -> list[str]:
    return sorted(os.listdir(path))


def _as_itself(found, how: str) -> Path:
    """A path to the working copy, as the operator might give it for the sealed final."""
    if how == "itself":
        return found.path
    if how == "hard link":
        link = found.root / "linked.sqlite"
        os.link(found.path, link)
        return link
    if how == "dotted path":
        return found.path.parent / ".." / found.path.parent.name / found.path.name
    link = found.root / "link.sqlite"
    link.symlink_to(found.path)
    return link


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("how", ["itself", "hard link", "dotted path", "symbolic link"])
def test_the_working_copy_under_any_name_is_refused_before_it_is_opened_and_stays_as_it_was(
    tmp_path, monkeypatch, how, dry_run
):
    found = lost_run(tmp_path)
    source = _as_itself(found, how)
    before, listing = found.path.read_bytes(), _directory(found.path.parent)
    _forbid_connection(monkeypatch)
    with pytest.raises(
        RecoverRefused, match=r"^condition 4 \(sealed final\): .*(the ledger file itself|symbolic link)"
    ):
        _recover(found, source=source, dry_run=dry_run)
    assert found.path.read_bytes() == before
    assert _directory(found.path.parent) == listing  # no `-wal`, no `-shm`, nothing made beside the file


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_working_copy_that_is_a_link_to_the_sealed_final_is_refused_before_the_final_is_opened(
    tmp_path, monkeypatch, dry_run
):
    """The path the ledger is given names the sealed final: opening it would set the final's journal mode, and a `-wal`
    and `-shm` would appear beside the final."""
    found = lost_run(tmp_path)
    found.path.unlink()
    found.path.symlink_to(found.source)
    before, listing = found.source.read_bytes(), _directory(found.source.parent)
    _forbid_connection(monkeypatch)
    with pytest.raises(RecoverRefused, match=r"^condition 4 \(sealed final\): .*the ledger file itself"):
        _recover(found, dry_run=dry_run)
    assert found.source.read_bytes() == before
    assert _directory(found.source.parent) == listing


def test_the_digest_and_every_fact_are_of_the_bytes_of_one_private_copy(tmp_path, monkeypatch):
    """The sealed final is read once, into a copy that is hashed and is the file the rows are read from."""
    found = lost_run(tmp_path)
    seen = {}
    copy_and_hash, read_file = module._copy_and_hash, module._read_ledger_file

    def copying(handle, destination):
        seen["copy"], seen["digest"] = destination, copy_and_hash(handle, destination)
        return seen["digest"]

    def reading(path, run_key, shown):
        seen["read"], seen["read_sha256"] = path, hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return read_file(path, run_key, shown)

    monkeypatch.setattr(module, "_copy_and_hash", copying)
    monkeypatch.setattr(module, "_read_ledger_file", reading)
    done = _recover(found)
    assert seen["read"] == seen["copy"] != found.source
    assert seen["read_sha256"] == seen["digest"] == done.source_sha256 == sha256_of(found.source)
    assert not Path(seen["copy"]).exists()  # removed on return


@pytest.mark.parametrize("how", ["recovered", "refused after the copy", "dry run"])
def test_the_private_copy_is_made_under_tmpdir_and_is_gone_whatever_the_outcome(tmp_path, monkeypatch, how):
    found = lost_run(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    copies = []
    copy_and_hash = module._copy_and_hash

    def copying(handle, destination):
        copies.append(Path(destination))
        return copy_and_hash(handle, destination)

    monkeypatch.setattr(module, "_copy_and_hash", copying)
    if how == "refused after the copy":
        another_fingerprint(found.source)
        with pytest.raises(RecoverRefused, match=r"^condition 4"):
            _recover(found)
    else:
        _recover(found, dry_run=how == "dry run")
    (copied,) = copies
    assert scratch in copied.parents
    assert os.listdir(scratch) == []


def _after_reading(monkeypatch, action) -> None:
    """Run `action` once the sealed final has been read and before the decision under the write lock."""
    reader = module.read_source

    def interleaved(*args):
        source = reader(*args)
        action()
        return source

    monkeypatch.setattr(module, "read_source", interleaved)


def _replace(found) -> None:
    other = found.root / "other.sqlite"
    shutil.copy2(found.source, other)
    os.replace(other, found.source)  # another file of the same bytes: another inode


def _touch(found) -> None:
    info = found.source.stat()
    os.utime(found.source, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))


def _grow(found) -> None:
    with open(found.source, "ab") as handle:
        handle.write(b"\0" * 16)


def _wal(found) -> None:
    found.source.with_name(found.source.name + "-wal").write_bytes(b"x" * 32)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("change", "phrase"),
    [
        (_replace, "changed after it was read"),
        (_touch, "changed after it was read"),
        (_grow, "changed after it was read"),
        (_wal, "-wal holds bytes"),
    ],
)
def test_a_sealed_final_that_changes_between_the_read_and_the_decision_under_the_lock_is_refused(
    tmp_path, monkeypatch, change, phrase, dry_run
):
    found = lost_run(tmp_path)
    before = dump(found.path)
    _after_reading(monkeypatch, lambda: change(found))
    with pytest.raises(RecoverRefused, match=rf"^condition 4 \(sealed final\): .*{phrase}"):
        _recover(found, dry_run=dry_run)
    assert dump(found.path) == before


def test_a_sealed_final_that_is_replaced_while_it_is_copied_is_refused(tmp_path, monkeypatch):
    """The copy is of the file that was open, and its path now names another: what was hashed is not what is there."""
    found = lost_run(tmp_path)
    copy_and_hash = module._copy_and_hash

    def swapped(handle, destination):
        digest = copy_and_hash(handle, destination)
        _replace(found)
        return digest

    monkeypatch.setattr(module, "_copy_and_hash", swapped)
    before = dump(found.path)
    with pytest.raises(RecoverRefused, match=r"^condition 4 \(sealed final\): .*changed after it was read"):
        _recover(found)
    assert dump(found.path) == before
