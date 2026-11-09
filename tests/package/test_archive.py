"""The checksum manifest and the deterministic archive of a replication package."""

import gzip
import hashlib
import os
import tarfile

import pytest

from expcore.package.archive import SUMS_FILE, parse_sums, render_sums, verify_sums, write_archive

FILES = {"b/two.json": b'{"x": 1}\n', "a.md": b"# notes\n", "c/d/e.txt": b"deep\n"}


def test_k06_two_writes_of_the_same_files_are_byte_identical_whatever_the_order_or_the_moment(tmp_path):
    """K06: archives are deterministic; members are sorted and carry no time, owner or name of the build."""
    first = write_archive(tmp_path / "one" / "pkg.tar.gz", "pkg", FILES)
    os.utime(tmp_path / "one", (1, 1))
    second = write_archive(tmp_path / "two" / "pkg.tar.gz", "pkg", dict(reversed(list(FILES.items()))))
    assert first.read_bytes() == second.read_bytes()


def test_k06_members_are_sorted_with_time_zero_owner_zero_no_owner_names_and_one_mode(tmp_path):
    """K06: sorted names, uid and gid 0, a fixed mode and mtime; gzip without a timestamp or an original name."""
    archive = write_archive(tmp_path / "pkg.tar.gz", "pkg", FILES)
    header = archive.read_bytes()[:10]
    assert header[3] & 0x08 == 0  # no FNAME field: the gzip header names no file
    assert header[4:8] == b"\0\0\0\0"  # MTIME is zero
    with tarfile.open(archive) as members:
        infos = members.getmembers()
        assert [i.name for i in infos] == ["pkg/a.md", "pkg/b/two.json", "pkg/c/d/e.txt"]
        assert {(i.mtime, i.uid, i.gid, i.uname, i.gname, i.mode, i.isfile()) for i in infos} == {
            (0, 0, 0, "", "", 0o644, True)
        }
        assert {i.name: members.extractfile(i).read() for i in infos} == {f"pkg/{k}": v for k, v in FILES.items()}
    with gzip.open(archive) as stream:
        assert stream.read(1)  # a valid gzip stream


def test_k06_the_archive_is_written_by_rename_so_a_failure_leaves_no_partial_file(tmp_path, monkeypatch):
    """K06: publish by rename after the stream closes; a failed write leaves neither the target nor a scratch file."""

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        write_archive(tmp_path / "pkg.tar.gz", "pkg", FILES)
    assert list(tmp_path.iterdir()) == []


def test_the_manifest_lists_every_file_but_itself_sorted_in_the_format_sha256sum_reads():
    text = render_sums({**FILES, SUMS_FILE: b"old"}).decode()
    assert text.splitlines() == [f"{hashlib.sha256(FILES[p]).hexdigest()}  {p}" for p in sorted(FILES)]
    assert parse_sums(text) == {p: hashlib.sha256(FILES[p]).hexdigest() for p in FILES}


@pytest.mark.parametrize("line", ["nothex  a.md", f"{'a' * 64} a.md", f"{'a' * 64}  ", f"{'A' * 64}  a.md"])
def test_a_manifest_line_that_is_not_a_digest_and_a_path_is_refused(line):
    with pytest.raises(ValueError, match=SUMS_FILE):
        parse_sums(line + "\n")


def unpack(root, files):
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    (root / SUMS_FILE).write_bytes(render_sums(files))


def test_a_package_that_matches_its_manifest_verifies(tmp_path):
    unpack(tmp_path, FILES)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"cache")  # what importing the plugin leaves is not a change
    assert verify_sums(tmp_path) == []


def test_a_changed_a_missing_and_an_unlisted_file_are_each_reported(tmp_path):
    unpack(tmp_path, FILES)
    (tmp_path / "a.md").write_bytes(b"# changed\n")
    (tmp_path / "c" / "d" / "e.txt").unlink()
    (tmp_path / "extra.txt").write_bytes(b"added")
    assert verify_sums(tmp_path) == [
        f"a.md: its bytes differ from {SUMS_FILE}",
        f"c/d/e.txt: listed in {SUMS_FILE} but missing",
        f"extra.txt: not listed in {SUMS_FILE}",
    ]


def test_k01_a_package_without_a_manifest_or_with_an_empty_one_does_not_verify(tmp_path):
    """K01: a check with no input fails."""
    assert verify_sums(tmp_path) == [f"{SUMS_FILE}: missing, so no file can be verified"]
    (tmp_path / SUMS_FILE).write_bytes(b"")
    assert verify_sums(tmp_path) == [f"{SUMS_FILE}: lists no file"]
