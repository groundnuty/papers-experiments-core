"""The package's token check fails closed: only text, SQLite databases and archives of them may enter a package, every
one is read in full, with no exemption, and anything else is refused unless the author attests it by hash."""

import bz2
import gzip
import io
import lzma
import sqlite3
import struct
import tarfile
import zipfile
import zlib

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


def png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def found(files: dict[str, bytes]) -> list[str]:
    findings = scan_tokens(files, HASHES)
    assert not any(PRIVATE in finding for finding in findings)  # a finding never shows the name
    return findings


def refused(files: dict[str, bytes], kind: str) -> str:
    (finding,) = found(files)
    assert "refused" in finding
    assert kind in finding
    return finding


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


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be"])
def test_k03_utf16_text_with_a_byte_order_mark_is_read_as_text(encoding):
    """K03: UTF-16 text with a byte-order mark, little- or big-endian, is text and is read in full."""

    def with_bom(text: str) -> bytes:
        data = text.encode(encoding)
        return data if data[:2] in (b"\xff\xfe", b"\xfe\xff") else b"\xfe\xff" + data

    assert found({"notes.txt": with_bom(TEXT.decode())})
    assert found({"clean.txt": with_bom("nothing private\n")}) == []


def test_k03_utf16_without_a_byte_order_mark_and_utf8_with_nul_bytes_are_refused():
    """K03: text the check cannot tell from binary is refused, never read in part."""
    refused({"notes.txt": TEXT.decode().encode("utf-16-le")}, "not text")
    refused({"notes.txt": b"plain\x00text\n"}, "not text")


def test_k03_text_inside_an_sqlite_database_is_found_in_utf8_and_utf16(tmp_path):
    """K03: a SQLite database stores text uncompressed, so it is read raw and as UTF-16, whatever its encoding."""
    for encoding in ("UTF-8", "UTF-16le", "UTF-16be"):
        path = tmp_path / f"{encoding}.sqlite"
        connection = sqlite3.connect(path)
        connection.execute(f"PRAGMA encoding = '{encoding}'")
        connection.execute("CREATE TABLE t (x TEXT)")
        connection.execute("INSERT INTO t VALUES (?)", (f"ran on {PRIVATE}",))
        connection.commit()
        connection.close()
        assert found({"store.sqlite": path.read_bytes()}), encoding


def test_k03_a_zip_member_and_a_member_name_are_found_and_the_name_is_redacted():
    """K03: zip members are opened; a member whose own name holds the private name is shown redacted."""
    assert found({"bundle.zip": zipped({"inner/notes.txt": TEXT})})
    named = found({"bundle.zip": zipped({f"{PRIVATE}/notes.txt": b"plain\n"})})
    assert any(finding.startswith("<path redacted>") for finding in named)  # the member, besides the raw zip


def test_k03_nested_archives_are_opened_down_to_the_depth_cap():
    """K03: a tar inside gzip holding a zip holding bzip2 content is read all the way down."""
    nested = gzip.compress(tarred({"a/b.zip": zipped({"c.bz2": bz2.compress(TEXT)})}))
    assert found({"results.tar.gz": nested})


def test_k03_a_pdf_with_a_compressed_stream_is_refused():
    """K03: a PDF's FlateDecode stream hides its text from any scan of the bytes, so a PDF is refused."""
    pdf = b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode >>\nstream\n" + zlib.compress(TEXT) + b"\nendstream\n%%EOF\n"
    refused({"figure.pdf": pdf}, "PDF")


def test_k03_a_png_with_a_compressed_text_chunk_is_refused():
    """K03: a PNG's zTXt chunk holds compressed text, so a PNG is refused."""
    ztxt = png_chunk(b"zTXt", b"Author\x00\x00" + zlib.compress(TEXT))
    png = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", b"\x00" * 13) + ztxt + png_chunk(b"IEND", b"")
    refused({"figure.png": png}, "PNG")


def test_k03_a_raw_zlib_stream_and_unknown_binary_are_refused():
    """K03: a zlib stream has no container the check opens, and binary content of no allowed kind is refused."""
    refused({"blob.bin": zlib.compress(TEXT)}, "zlib")
    refused({"blob.bin": bytes(range(256)) * 4}, "binary")


def test_k03_a_zip_that_does_not_start_at_offset_zero_is_refused():
    """K03: a zip appended to or prefixed by other bytes is still a zip to unzip tools, so it is refused rather than
    read as the bytes in front of it."""
    refused({"notes.dat": b"%PDF-1.4 cover page\n" + zipped({"inner.txt": TEXT})}, "zip")
    refused({"notes.dat": b"\xff\xfe" + "cover\n".encode("utf-16-le") + zipped({"inner.txt": TEXT})}, "zip")


def test_k03_another_compressed_or_archive_format_is_refused():
    """K03: content the check cannot read is refused, not passed: a zstd frame, a 7z archive, a rar archive."""
    for name, magic, kind in (
        ("x.zst", b"\x28\xb5\x2f\xfd", "zstd"),
        ("x.7z", b"7z\xbc\xaf\x27\x1c", "7z"),
        ("x.rar", b"Rar!\x1a\x07\x00", "rar"),
    ):
        refused({name: magic + b"\x00" * 64}, kind)
    inner = refused({"bundle.zip": zipped({"deep.zst": b"\x28\xb5\x2f\xfd" + b"\x00" * 64})}, "zstd")
    assert "bundle.zip!deep.zst" in inner


def test_k03_content_over_the_size_cap_or_below_the_depth_cap_is_refused(monkeypatch):
    """K03: a stream larger than the cap, or nested deeper than the cap, is refused rather than read in part."""
    from expcore.package import views

    monkeypatch.setattr(views, "MAX_STREAM_BYTES", 100)
    assert "over the cap" in refused({"big.gz": gzip.compress(b"x" * 1000)}, "big.gz")
    monkeypatch.setattr(views, "MAX_STREAM_BYTES", 2**20)
    deep = TEXT
    for _ in range(views.MAX_DEPTH + 1):
        deep = gzip.compress(deep)
    assert any("refused" in f and "nested deeper" in f for f in found({"deep.gz": deep}))


def test_k03_the_total_budget_stops_the_scan_at_once_when_many_members_exhaust_it(monkeypatch):
    """K03: a bomb of many small members each under the stream cap is stopped when the total budget runs out, with
    one refusal, and no member after it is read."""
    from expcore.package import views

    monkeypatch.setattr(views, "MAX_STREAM_BYTES", 1000)
    monkeypatch.setattr(views, "MAX_TOTAL_BYTES", 2000)
    bomb = zipped({f"m{n:03d}.txt": b"y" * 900 for n in range(40)} | {"z-last.txt": TEXT})
    streams, problems = views.streams("bomb.zip", bomb)
    assert len(problems) == 1
    label, why = problems[0]
    assert label.startswith("bomb.zip!m")
    assert "in total" in why
    assert len(streams) <= 4  # the zip itself and the members read before the budget ran out


def test_k03_a_python_files_provenance_header_is_scanned_like_any_other_line():
    """K03: the package scan applies no exemption; the repository's own scan skips the header of its modules,
    which name their sources, but a package's file is read in full."""
    extra = f"# Provenance:\n#   source: {PRIVATE} @ 0123abc, x.py, blob {'a' * 40}\nVALUE = 1\n".encode()
    assert found({"extra.py": extra})


@pytest.mark.parametrize(
    "hidden",
    [
        f"{PRIVATE[:4]}\\\n{PRIVATE[4:]}",  # a backslash-newline continuation splits it
        "\\x" + PRIVATE[0].encode().hex() + PRIVATE[1:],
        f"\\u{ord(PRIVATE[0]):04x}{PRIVATE[1:]}",
        f"\\U{ord(PRIVATE[0]):08x}{PRIVATE[1:]}",
    ],
)
def test_k03_a_name_split_by_a_continuation_or_written_with_escapes_is_found(hidden):
    """K03: every text file is also read normalised: continuations joined and backslash escapes decoded."""
    assert found({"run.sh": f'echo "{hidden}"\n'.encode()})
