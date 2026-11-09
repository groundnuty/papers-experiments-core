"""The package's token check reads every byte stream a file holds, under every text encoding, with no exemption."""

import bz2
import gzip
import io
import lzma
import tarfile
import zipfile

import pytest

from expcore.package.build import scan_tokens
from expcore.provenance.hashfile import hashes_from_tokens

PRIVATE = "zarnothwick"  # invented; private everywhere in these tests
HASHES = hashes_from_tokens([PRIVATE])
TEXT = f"the campaign ran on {PRIVATE} last week\n".encode()


def zipped(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def tarred(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def found(files: dict[str, bytes]) -> list[str]:
    findings = scan_tokens(files, HASHES)
    assert not any(PRIVATE in finding for finding in findings)  # a finding never shows the name
    return findings


def test_k03_plain_text_is_found_as_before():
    """K03: the private name list never enters the output: the plain case still holds."""
    assert found({"notes.txt": TEXT})
    assert found({"clean.txt": b"nothing private here\n"}) == []


@pytest.mark.parametrize(
    ("name", "compress"),
    [
        ("data.json.gz", gzip.compress),
        ("data.bz2", bz2.compress),
        ("data.xz", lzma.compress),
        ("misnamed.txt", gzip.compress),
    ],
)
def test_k03_a_private_name_inside_compressed_content_is_found(name, compress):
    """K03: gzip, bzip2 and xz content is opened, by its magic bytes and whatever the file is called."""
    assert found({name: compress(TEXT)})


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_k03_a_private_name_in_utf16_text_is_found(encoding):
    """K03: UTF-16 text, with or without a byte-order mark, little- or big-endian, is read as text."""
    assert found({"notes.txt": TEXT.decode().encode(encoding)})


def test_k03_utf16_text_at_an_odd_offset_inside_binary_content_is_found():
    """K03: a UTF-16 string embedded at an odd byte offset of a binary file is found too."""
    assert found({"store.sqlite": b"\x00\x01\x02" + TEXT.decode().encode("utf-16-le") + b"\x00" * 9})


def test_k03_a_zip_member_and_a_member_name_are_found_and_the_name_is_redacted():
    """K03: zip members are opened; a member whose own name holds the private name is shown redacted."""
    assert found({"bundle.zip": zipped({"inner/notes.txt": TEXT})})
    named = found({"bundle.zip": zipped({f"{PRIVATE}/notes.txt": b"plain\n"})})
    assert named
    assert any(finding.startswith("<path redacted>") for finding in named)  # the member, besides the raw zip


def test_k03_nested_archives_are_opened_down_to_the_depth_cap():
    """K03: a tar inside gzip holding a zip holding bzip2 content is read all the way down."""
    nested = gzip.compress(tarred({"a/b.zip": zipped({"c.bz2": bz2.compress(TEXT)})}))
    assert found({"results.tar.gz": nested})


def test_k03_another_compressed_or_archive_format_is_refused_as_unscannable():
    """K03: content the check cannot read is refused, not passed: a zstd frame, a 7z archive, a rar archive."""
    for name, magic in (
        ("x.zst", b"\x28\xb5\x2f\xfd"),
        ("x.7z", b"7z\xbc\xaf\x27\x1c"),
        ("x.rar", b"Rar!\x1a\x07\x00"),
    ):
        (finding,) = found({name: magic + b"\x00" * 64})
        assert "unscannable" in finding
    (inner,) = found({"bundle.zip": zipped({"deep.zst": b"\x28\xb5\x2f\xfd" + b"\x00" * 64})})
    assert "bundle.zip!deep.zst" in inner


def test_k03_content_over_the_size_cap_or_below_the_depth_cap_is_refused(monkeypatch):
    """K03: a stream larger than the cap, or nested deeper than the cap, is refused rather than read in part."""
    from expcore.package import views

    monkeypatch.setattr(views, "MAX_STREAM_BYTES", 100)
    (finding,) = found({"big.gz": gzip.compress(b"x" * 1000)})
    assert "unscannable" in finding
    assert "over the cap" in finding
    monkeypatch.setattr(views, "MAX_STREAM_BYTES", 2**20)
    deep = TEXT
    for _ in range(views.MAX_DEPTH + 1):
        deep = gzip.compress(deep)
    assert any("unscannable" in f and "nested deeper" in f for f in found({"deep.gz": deep}))


def test_k03_a_python_files_provenance_header_is_scanned_like_any_other_line():
    """K03: the package scan applies no exemption; the repository's own scan skips the header of its modules,
    which name their sources, but a package's file is read in full."""
    extra = f"# Provenance:\n#   source: {PRIVATE} @ 0123abc, x.py, blob {'a' * 40}\nVALUE = 1\n".encode()
    assert found({"extra.py": extra})
