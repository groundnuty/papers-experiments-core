"""The package's token check fails closed: only text, SQLite databases and archives of them may enter a package, every
one is read in full, with no exemption, and anything else is refused unless the author attests it by hash."""

import base64
import bz2
import gzip
import hashlib
import io
import lzma
import random
import sqlite3
import struct
import tarfile
import time
import zipfile
import zlib

import pytest

from expcore.package.build import scan_tokens
from expcore.package.views import hidden_name
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
    member = hidden_name(1, f"bundle.zip!{PRIVATE}/notes.txt")  # the member, named by place and hash, besides the zip
    assert any(finding.startswith(member) for finding in named)


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


def test_k03_a_pdf_or_postscript_file_is_refused_even_when_it_is_ascii():
    """K03: a format signature is checked before the text test: an ASCIIHex-encoded Flate stream keeps a PDF pure
    ASCII, and a PDF or PostScript file is refused all the same."""
    stream = zlib.compress(TEXT).hex().encode() + b">"
    pdf = b"%PDF-1.4\n<< /Filter [/ASCIIHexDecode /FlateDecode] >>\nstream\n" + stream + b"\nendstream\n%%EOF\n"
    refused({"figure.pdf": pdf}, "PDF")
    refused({"figure.ps": b"%!PS-Adobe-3.0\n(a page) show\n"}, "PostScript")


def b64(data: bytes) -> bytes:
    return base64.b64encode(data)


def b64url_unpadded(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def hexed(data: bytes) -> bytes:
    return data.hex().encode()


@pytest.mark.parametrize("encode", [b64, b64url_unpadded, hexed])
def test_k03_a_name_in_compressed_bytes_written_as_base64_or_hex_in_text_is_found(encode):
    """K03: base64 (either alphabet, padded or not) and hex runs of 40 characters or more are decoded, and what they
    decode to goes through the same allow list: here a zlib stream, decompressed."""
    assert found({"notes.txt": b"blob: " + encode(zlib.compress(TEXT)) + b"\n"})


def test_k03_a_data_uri_in_an_svg_is_decoded_wrapped_or_not():
    """K03: a data: URI's base64, also when it is wrapped over lines, is decoded and read."""
    inline = b'<svg><image href="data:application/octet-stream;base64,' + b64(zlib.compress(TEXT)) + b'"/></svg>\n'
    assert found({"figure.svg": inline})
    encoded = b64(gzip.compress(TEXT * 8, mtime=0))
    wrapped = b"\n".join(encoded[i : i + 64] for i in range(0, len(encoded), 64))
    assert found({"figure.svg": b'<svg><image href="data:application/gzip;base64,\n' + wrapped + b'\n"/></svg>\n'})


def test_k03_a_decoded_payload_the_check_cannot_read_refuses_the_file():
    """K03: an image inside a data: URI is content the check cannot read, so the file holding it is refused."""
    png = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", b"\x00" * 13) + png_chunk(b"IEND", b"")
    refused({"figure.svg": b'<svg><image href="data:image/png;base64,' + b64(png) + b'"/></svg>\n'}, "PNG")


def test_k03_digests_and_long_identifiers_decode_harmlessly_and_refuse_nothing():
    """K03: a SHA-256 or a git commit in hex, a digest in base64 and a long identifier are decoded too; they are not
    an encoding of anything, so they yield nothing and refuse nothing."""
    text = (
        '{"sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", '
        '"commit": "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3", '
        f'"b64": "{base64.b64encode(bytes(range(32))).decode()}", '
        '"name": "test_the_contrast_that_left_a_task_out_names_it_in_its_row"}\n'
    )
    assert found({"record.json": text.encode()}) == []


def sqlite_with(path, *values) -> bytes:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (x)")
    connection.executemany("INSERT INTO t VALUES (?)", [(value,) for value in values])
    connection.commit()
    connection.close()
    return path.read_bytes()


def test_k03_sqlite_values_are_read_a_compressed_blob_and_an_escaped_text_value_found(tmp_path):
    """K03: besides its raw bytes, a database is opened read-only and every value read: TEXT through the text views
    and their escape decoding, BLOB through the byte-stream allow list (a zlib BLOB is decompressed)."""
    assert found({"store.sqlite": sqlite_with(tmp_path / "blob.sqlite", zlib.compress(TEXT))})
    escaped = f"ran on \\u{ord(PRIVATE[0]):04x}{PRIVATE[1:]}"
    assert found({"store.sqlite": sqlite_with(tmp_path / "text.sqlite", escaped)})


def test_k03_an_sqlite_blob_the_check_cannot_read_refuses_the_database(tmp_path):
    """K03: a BLOB is a byte stream like any other: one the check cannot read refuses the file."""
    refused({"store.sqlite": sqlite_with(tmp_path / "zstd.sqlite", b"\x28\xb5\x2f\xfd" + b"\x00" * 32)}, "zstd")


def test_k03_a_continuation_with_indentation_is_joined():
    """K03: a YAML or shell continuation joins the backslash, the newline and the indentation that follows."""
    assert found({"meta.yml": f'author: "{PRIVATE[:4]}\\\n    {PRIVATE[4:]}"\n'.encode()})


def corrupt_crc(archive: bytes) -> bytes:
    """Flip the CRC-32 of every member in its local header and in the central directory."""
    data = bytearray(archive)
    for signature, offset in ((b"PK\x03\x04", 14), (b"PK\x01\x02", 16)):
        start = data.find(signature)
        while start != -1:
            data[start + offset] ^= 0xFF
            start = data.find(signature, start + 4)
    return bytes(data)


def test_k03_a_member_that_fails_to_decompress_refuses_the_file_at_once():
    """K03: a member whose CRC does not match, or a stream cut short, refuses the whole file the moment it fails; the
    walk does not go on to the next member or archive."""
    from expcore.package import views

    inner = corrupt_crc(zipped({"bad.txt": b"z" * 5000}))
    outer = zipped({f"inner{n}.zip": inner for n in range(6)})
    streams, problems = views.streams("outer.zip", outer)
    assert len(problems) == 1
    label, why = problems[0]
    assert label == "outer.zip!inner0.zip!bad.txt"
    assert "BadZipFile" in why
    assert [s.label for s in streams] == ["outer.zip", "outer.zip!inner0.zip"]
    streams, problems = views.streams("cut.gz", gzip.compress(TEXT * 50)[:-30])
    assert len(problems) == 1
    assert [s.label for s in streams] == ["cut.gz"]


def test_k03_refused_files_under_a_redacted_directory_are_each_named_by_position_and_path_hash():
    """K03: a refusal whose path holds a private name is never shown, and never folded into another: each names the
    file's position in the package and the start of its path's SHA-256."""
    first, second = f"{PRIVATE}/a.pdf", f"{PRIVATE}/b.pdf"
    findings = found({"README.md": b"plain\n", first: b"%PDF-1.4\n", second: b"%PDF-1.4\n"})
    named = [finding.split(": refused", 1)[0] for finding in findings if "refused" in finding]
    assert named == [
        f"file 2 (path sha256 {hashlib.sha256(first.encode()).hexdigest()[:12]})",
        f"file 3 (path sha256 {hashlib.sha256(second.encode()).hexdigest()[:12]})",
    ]


def test_k03_a_long_base64_run_that_looks_compressed_but_fails_to_decompress_refuses_the_file():
    """K03: a decoded run with a gzip, bzip2 or xz magic, or a zlib header, that fails to decompress is treated as an
    archive member that fails: here a truncated gzip in base64 inside JSON."""
    truncated = gzip.compress(TEXT * 4, mtime=0)[:-12]
    refused({"record.json": b'{"blob": "' + b64(truncated) + b'"}\n'}, "fails to decompress")


def test_k03_runs_from_16_characters_are_decoded_and_short_ones_refused_for_nothing_weak():
    """K03: a 28-character base64 run of a zlib stream is decoded and decompressed; a short run is never refused for
    a magic of fewer than four bytes, nor for a zlib header that does not inflate."""
    assert found({"config.json": b'{"host":"eJyrSizKyy/JKM9MzgYAHJYEtQ=="}\n'})
    weak = base64.b64encode(b"\x1f\x9d" + bytes(10)).decode()
    no_inflate = base64.b64encode(b"\x78\x9c" + b"\xff" * 10).decode()
    assert found({"x.json": f'{{"a": "{weak}", "b": "{no_inflate}"}}\n'.encode()}) == []


@pytest.mark.parametrize("size", [17 * 1024, 2**20])
def test_k03_base64_output_is_scanned_in_linear_time(size):
    """K03: the run finder is one linear pass: base64 output of 17 KB and of 1 MB, wrapped as base64 tools wrap it,
    scans in well under two seconds."""
    output = base64.encodebytes(random.Random(size).randbytes(size * 3 // 4))
    start = time.perf_counter()
    scan_tokens({"out.b64": output}, HASHES)
    assert time.perf_counter() - start < 2


def test_k03_a_decoded_run_that_is_text_is_read_as_text_and_never_refused_for_a_signature():
    """K03: only decoded bytes that are not text are checked against format signatures: a sentence about the PDF
    header, in base64, is text, read as text, and refuses nothing."""
    sentence = b"%PDF- is the standard PDF header that every PDF file starts with, by convention."
    assert found({"notes.md": b"Encoded: " + b64(sentence) + b"\n"}) == []
    assert found({"notes.md": b"Encoded: " + b64(sentence + b" " + TEXT) + b"\n"})
