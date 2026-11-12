"""The walk for copies of a declared key file: complete whatever the content scanner refuses, attested or not, in
every wheel, and refusing what it cannot finish; it never prints a digest."""

import base64
import gzip
import hashlib
import io
import sqlite3
import tarfile
import zipfile
from types import SimpleNamespace

import pytest
from package_support import wheel_of

from expcore.config.key_locations import location_finders
from expcore.package import views
from expcore.package.leaks import FOUND_COPY, UNCHECKED, find_copies, find_locations
from expcore.package.spec import Opaque
from expcore.package.wheels import Wheel
from expcore.package.wheelscan import scan_wheels
from expcore.provenance.environment import CORE_DISTRIBUTION
from expcore.provenance.hashfile import hashes_from_tokens

KEY = b"sk-lantern-" + bytes(range(65, 91)) * 2 + b"\n"  # invented; long enough for its base64 run to be decoded
BINARY_KEY = bytes(range(48))  # a key that is not text
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64  # what the content scanner refuses, and stopped at
HASHES = hashes_from_tokens(["zarnothwick"])  # invented
KEPT = ("vault-of-embers", "lantern.secret")  # the parts of the key file's path, which no message may hold


def finders_of(directory, data: bytes = KEY):
    """What finds a key file holding `data`, kept where no packaged file's name agrees with it."""
    path = directory.joinpath(*KEPT)
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return location_finders([SimpleNamespace(key_file=str(path), key_env=None, key="gamma")])


@pytest.fixture
def finders(tmp_path):
    return finders_of(tmp_path)


def zipped(*members: tuple[str, bytes], stored: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


def tarred(*members: tuple[str, bytes], kind: bytes = tarfile.REGTYPE) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.type = kind
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def tar_in_v7_format() -> bytes:
    """A tar in the old v7 format: no "ustar" magic in its header (the checksum is made again), which the tar reader
    and GNU tar both read all the same."""
    data = bytearray(tarred(("k.bin", KEY)))
    data[257:265] = bytes(8)
    data[148:156] = b" " * 8
    data[148:156] = b"%06o\0 " % sum(data[:512])
    assert not views._is_tar(bytes(data))
    with tarfile.open(fileobj=io.BytesIO(bytes(data))) as archive:
        assert archive.extractfile("k.bin").read() == KEY
    return bytes(data)


def tar_after_a_junk_block() -> bytes:
    """A tar with a block of junk before its first header, which GNU tar skips to extract the member after it."""
    return b"G" * 512 + tarred(("k.bin", KEY))


def tar_with_bad_header() -> bytes:
    """A tar whose second header's checksum is wrong: the tar reader stops there, as it does at the end of an archive,
    while GNU tar skips to the next header and extracts the key file."""
    data = bytearray(tarred(("a.txt", b"first\n"), ("b.txt", b"second\n"), ("k.bin", KEY)))
    data[1024 + 148] ^= 0x1
    return bytes(data)


def tar_with_a_lone_zero_block() -> bytes:
    """A tar with one zero block (not the two that end an archive) after its first member, and a member after it."""
    first, key = tarred(("a.txt", b"first\n"))[:1024], tarred(("k.bin", KEY))
    return first + bytes(512) + key


def tar_of_links() -> bytes:
    """A tar of a directory, a symlink and a hard link (both to nothing in the archive): members with no data."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, kind in (("d", tarfile.DIRTYPE), ("s", tarfile.SYMTYPE), ("h", tarfile.LNKTYPE)):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, "absent"
            archive.addfile(info)
    return buffer.getvalue()


def sqlite_of(directory, *blobs: bytes, wal: bool = False) -> bytes:
    """The bytes of an SQLite database file, in WAL mode or not, with one row per BLOB."""
    path = directory / "runs.db"
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE runs (id INTEGER, note TEXT, blob BLOB)")
    connection.executemany(
        "INSERT INTO runs VALUES (?, ?, ?)", [(n, "a result row", b) for n, b in enumerate(blobs, 1)]
    )
    connection.commit()
    connection.close()
    return path.read_bytes()


def wal_of(directory, *blobs: bytes) -> bytes:
    """The write-ahead log of a database in WAL mode, read while a connection to it is open (closing it would
    checkpoint the log into the database and delete it)."""
    connection = sqlite3.connect(directory / "runs.db")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.execute("CREATE TABLE runs (id INTEGER, blob BLOB)")
        connection.executemany("INSERT INTO runs VALUES (?, ?)", list(enumerate(blobs, 1)))
        connection.commit()
        return (directory / "runs.db-wal").read_bytes()
    finally:
        connection.close()


def nested(levels: int) -> bytes:
    """The key as `k.bin` in a zip, that zip in another, `levels` zips in all."""
    data = zipped(("k.bin", KEY))
    for level in range(1, levels):
        data = zipped((f"l{level}.zip", data))
    return data


def encrypted() -> bytes:
    """A zip whose one member is marked encrypted, as a password-protected zip's is."""
    data = bytearray(zipped(("k.bin", b"locked away\n"), stored=True))
    data[6] |= 0x1  # the member's flags, in its local header
    data[data.find(b"PK\x01\x02") + 8] |= 0x1  # and in the central directory
    return bytes(data)


def damaged() -> bytes:
    """A zip whose one member's bytes no longer match its CRC."""
    data = bytearray(zipped(("k.bin", b"0123456789\n"), stored=True))
    data[data.find(b"0123456789")] ^= 0x1
    return bytes(data)


def digests_printed(lines) -> bool:
    return any(hashlib.sha256(key).hexdigest() in str(line) for line in lines for key in (KEY, BINARY_KEY))


def run_of(data: bytes) -> bytes:
    return base64.b64encode(data)


ENCODED = b"A = '" + run_of(PNG) + b"'\nB = '" + run_of(KEY) + b"'\n"
BEHIND_A_REFUSAL = [  # what it is, the packaged path, its bytes, the label of the copy in it
    ("a zip member after a PNG", "data/a.zip", zipped(("a.png", PNG), ("k.bin", KEY)), "data/a.zip!k.bin"),
    ("a tar member after a PNG", "data/a.tar", tarred(("a.png", PNG), ("k.bin", KEY)), "data/a.tar!k.bin"),
    ("a base64 run after a PNG's", "src/t.py", ENCODED, f"src/t.py!(base64@{ENCODED.index(run_of(KEY))})"),
]


@pytest.mark.parametrize(
    ("path", "data", "label"), [c[1:] for c in BEHIND_A_REFUSAL], ids=[c[0] for c in BEHIND_A_REFUSAL]
)
def test_k13_a_copy_of_the_key_file_behind_a_refused_stream_is_found_attested_or_not(finders, path, data, label):
    """K13: the content scanner stops at the first stream it refuses (a PNG), and an attested file is not given to it,
    but the walk for copies of a key file is its own: a copy archived or encoded after a refused stream is refused by
    its path inside the file, attested or not, and no line prints a digest or the key file's path."""
    for opaque in (frozenset({path}), frozenset()):
        found = find_locations({path: data}, finders, opaque=opaque)
        assert f"{label}: {FOUND_COPY}" in found, found
        assert not digests_printed(found)
        assert not any(part in line for line in found for part in KEPT)


CANNOT_FINISH = [  # what it is, the file's bytes, the scanner's limits for the test, why, where it stopped
    ("archives nested too deep", nested(6), {}, "archives nested deeper than 4 levels", "!l5.zip!l4.zip!l3.zip!l2.zip"),
    ("too many members", zipped(("a", b"a"), ("b", b"b"), ("c", b"c")), {"MAX_MEMBERS": 2}, "more than 2 archive", ""),
    ("too many bytes", gzip.compress(b"\0" * 4096), {"MAX_TOTAL_BYTES": 1024}, "over 1024 bytes", "!(gzip)"),
    ("an encrypted member", encrypted(), {}, "it is encrypted", "!k.bin"),
    ("a damaged member", damaged(), {}, "it fails to decompress (BadZipFile)", "!k.bin"),
    ("a tar header damaged", tar_with_bad_header(), {}, "bytes follow its last member", ""),
    ("a tar with a lone zero block", tar_with_a_lone_zero_block(), {}, "bytes follow its last member", ""),
]


@pytest.mark.parametrize(
    ("data", "limits", "why", "where"), [c[1:] for c in CANNOT_FINISH], ids=[c[0] for c in CANNOT_FINISH]
)
def test_k13_a_walk_for_a_copy_that_cannot_finish_refuses_naming_where_it_stopped(
    finders, monkeypatch, data, limits, why, where
):
    """K13: when the walk for a copy of the key file cannot finish (archives nested past the scanner's depth, more
    members or decompressed bytes than its limits, a member encrypted or damaged), the build is refused saying the file
    could not be checked for a copy and naming where the walk stopped, attested or not: never a silent pass."""
    for name, value in limits.items():
        monkeypatch.setattr(views, name, value)
    for opaque in (frozenset({"data/a.bin"}), frozenset()):
        found = [line for line in find_locations({"data/a.bin": data}, finders, opaque=opaque) if UNCHECKED in line]
        assert len(found) == 1, found
        assert found[0].startswith(f"data/a.bin{where}: could not be checked for a copy of the key file"), found
        assert why in found[0], found


def test_k13_what_can_be_read_of_a_tar_with_a_damaged_header_is_still_compared(finders):
    """K13: a tar the reader stops in before its end (GNU tar skips a damaged header and goes on) is refused as
    unchecked, and a copy of the key file among the members before that is still found."""
    data = bytearray(tarred(("k.bin", KEY), ("b.txt", b"second\n"), ("c.txt", b"third\n")))
    data[data.index(b"c.txt") + 148] ^= 0x1
    found = find_locations({"data/a.tar": bytes(data)}, finders, opaque={"data/a.tar"})
    assert f"data/a.tar!k.bin: {FOUND_COPY}" in found, found
    assert any(UNCHECKED in line and line.startswith("data/a.tar:") for line in found), found


def test_k13_a_copy_of_the_key_file_in_a_tar_member_of_an_unknown_type_is_found(finders):
    """K13: a tar member whose type flag the reader does not know is read as a regular file, as GNU tar extracts it, so
    a copy of the key file in one is refused by its path, attested or not."""
    data = tarred(("k.bin", KEY), kind=b"Z")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert not archive.getmembers()[0].isfile()
    for opaque in (frozenset({"data/a.tar"}), frozenset()):
        found = find_locations({"data/a.tar": data}, finders, opaque=opaque)
        assert f"data/a.tar!k.bin: {FOUND_COPY}" in found, found


def test_k13_a_tar_of_directories_and_links_passes_the_walk(finders):
    """K13: members that hold no data (a directory, a symlink, a hard link to nothing in the archive) are not read, and
    do not stop the walk."""
    assert find_locations({"data/a.tar": tar_of_links()}, finders, opaque={"data/a.tar"}) == []


def test_k13_a_wal_mode_sqlite_database_is_read_for_a_copy_of_the_key_file(finders, tmp_path):
    """K13: a database in WAL mode (which SQLite will not deserialise as it is) with no copy of the key file passes,
    attested; one with the key file in a BLOB is refused by the BLOB's path."""
    clean = sqlite_of(tmp_path, KEY[1:], wal=True)
    assert clean[18:20] == b"\x02\x02"
    assert find_locations({"out/runs.db": clean}, finders, opaque={"out/runs.db"}) == []
    (tmp_path / "runs.db").unlink()
    holding = sqlite_of(tmp_path, b"one", KEY, wal=True)
    found = find_locations({"out/runs.db": holding}, finders, opaque={"out/runs.db"})
    assert found == [f"out/runs.db!runs.blob#2: {FOUND_COPY}"], found


def test_k13_nothing_is_walked_for_a_copy_when_no_key_file_can_be_read(tmp_path):
    """K13: with no key file to compare (none declared, or none there), nothing is walked, so nothing it cannot finish
    refuses the build."""
    finders = location_finders([SimpleNamespace(key_file=str(tmp_path / "absent.secret"), key_env=None, key="gamma")])
    assert find_locations({"data/a.bin": encrypted()}, finders, opaque={"data/a.bin"}) == []


def test_k13_an_attested_archive_with_no_copy_in_it_passes(finders):
    """K13: an attested archive the content scanner would refuse (a PNG, a compressed tar inside), which holds no copy
    of the key file, passes the walk; so do bytes that hold all but the key's first byte."""
    inner = gzip.compress(tarred(("notes.txt", b"plain notes\n"), ("plot.png", PNG)))
    bundle = zipped(("figures/plot.png", PNG), ("inner.tar.gz", inner), ("near.bin", b"\0" + KEY[1:]))
    assert find_locations({"data/bundle.zip": bundle}, finders, opaque={"data/bundle.zip"}) == []


def copy_found(finders, path: str, data: bytes) -> list[str]:
    """The findings for one file, attested and not, each of which must name the path alone and no digest."""
    for opaque in (frozenset({path}), frozenset()):
        found = find_locations({path: data}, finders, opaque=opaque)
        assert f"{path}: {FOUND_COPY}" in found, found
        assert not digests_printed(found)
        assert not any(part in line for line in found for part in KEPT)
    return found


def test_k13_a_copy_of_the_key_file_in_a_tar_in_v7_format_is_found_attested_or_not(finders):
    """K13: a tar with no "ustar" magic (the v7 format) is not recognised as a tar, but GNU tar extracts the key file
    from it: the bytes are searched for the key file's content, whatever their format, so it is refused by its path."""
    copy_found(finders, "data/old.tar", tar_in_v7_format())


def test_k13_a_copy_of_the_key_file_in_a_tar_after_a_junk_block_is_found_attested_or_not(finders):
    """K13: a tar behind a block of junk is no tar to the reader, and GNU tar extracts the key file from it all the
    same: the bytes are searched for the key file's content, so it is refused by its path."""
    copy_found(finders, "data/junk.tar", tar_after_a_junk_block())


def test_k13_a_copy_of_the_key_file_in_a_wal_sidecar_is_found_attested_or_not(finders, tmp_path):
    """K13: the -wal file of a database in WAL mode holds the pages not yet in the database, with no SQLite header of
    its own; a key file written to one is found by searching its bytes for the key file's content."""
    wal = wal_of(tmp_path, KEY)
    assert KEY in wal
    copy_found(finders, "out/runs.db-wal", wal)


def test_k13_a_database_page_the_key_file_was_deleted_from_is_found_by_its_bytes(finders, tmp_path):
    """K13: a value deleted from an SQLite database stays in its file until the space is reused, and no row holds it:
    the walk of values cannot see it, but the search of the file's bytes does."""
    path = tmp_path / "runs.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA secure_delete = OFF")
    connection.execute("CREATE TABLE runs (id INTEGER, blob BLOB)")
    connection.executemany("INSERT INTO runs VALUES (?, ?)", [(1, KEY), (2, b"kept")])
    connection.commit()
    connection.execute("DELETE FROM runs WHERE id = 1")
    connection.commit()
    connection.close()
    data = path.read_bytes()
    assert KEY in data
    copy_found(finders, "out/runs.db", data)


EMBEDDED = [  # what it is, the packaged path, the bytes with the key file's content in them
    ("without its trailing newline", "src/t.py", b"TOKEN = '" + KEY.rstrip() + b"'\n"),
    ("with its trailing newline", "notes/log.txt", b"before\n" + KEY + b"after\n"),
    ("in bytes that are not text", "data/blob.bin", b"\x00\xff" + KEY.rstrip() + b"\x00\x80"),
]


@pytest.mark.parametrize(("path", "data"), [c[1:] for c in EMBEDDED], ids=[c[0] for c in EMBEDDED])
def test_k13_the_key_files_content_is_found_inside_other_bytes_with_or_without_its_newline(finders, path, data):
    """K13: a key file usually ends with a newline that its copy in a script, a note or a binary lacks, and the stream
    is then not the key file by size and SHA-256: the content, stripped of the white space around it, is searched
    for too, so the stream is refused by its path."""
    assert not finders.is_key_file(data)
    copy_found(finders, path, data)


def test_k13_a_copy_nested_in_a_container_is_named_by_its_member_not_by_the_container_holding_it(finders):
    """K13: a stored zip holds the key file's bytes as they are, so the zip's own bytes hold them too; only the member
    is named, once, not the zip as well."""
    found = find_locations({"data/a.zip": zipped(("k.bin", KEY), stored=True)}, finders, opaque={"data/a.zip"})
    assert found == [f"data/a.zip!k.bin: {FOUND_COPY}"], found


SHORT = [  # the key, how long it is, whether a copy of it inside other bytes is found
    ("16 bytes", b"0123456789abcdef", True),
    ("15 bytes", b"0123456789abcde", False),
    ("a short key and a trailing newline", b"abcdefghijkl\n", False),
]


@pytest.mark.parametrize(("key", "found_inside"), [c[1:] for c in SHORT], ids=[c[0] for c in SHORT])
def test_k13_a_key_shorter_than_16_bytes_is_not_searched_for_inside_other_bytes(tmp_path, key, found_inside):
    """K13: a short key would occur by chance in other bytes, so it is searched for from 16 bytes up; a shorter one is
    compared by whole stream, size and SHA-256, as before, and nothing prints it."""
    finders = finders_of(tmp_path, key)
    inside = {"src/t.py": b"x = '" + key.strip() + b"'\n", "data/blob.bin": b"\xff\0" + key.strip() + b"\0\x80"}
    found = find_locations(inside, finders, opaque=set(inside))
    assert bool(found) == found_inside, found
    whole = find_locations({"data/k.txt": key}, finders, opaque={"data/k.txt"})
    assert whole == [f"data/k.txt: {FOUND_COPY}"], whole
    assert not any(key.strip().decode() in line for line in [*found, *whole])


def test_k13_a_vendored_wheel_holding_the_key_files_content_is_found_by_its_own_bytes(finders):
    """K13: a wheel in `unopened` (its members are walked by `scan_wheels`) is compared by its own bytes, and now
    searched for the key file's content too: one the zip reader does not open, but which holds the key, is refused."""
    holding = b"PK\x03\x04 not a wheel the zip reader opens " + KEY.rstrip() + b" ..."
    found = find_copies({"vendor/x.whl": holding}, finders, unopened={"vendor/x.whl"})
    assert found == [f"vendor/x.whl: {FOUND_COPY}"], found


def scan(wheel: Wheel, finders, *, attested=None, exempt=()):
    return scan_wheels(
        [wheel],
        HASHES,
        terms=None,
        hidden=(frozenset(), frozenset()),
        attested=attested or {},
        exempt=exempt,
        locations=finders,
    )


ARCHIVED = [  # what it is, the member's path and bytes, the copy's label under it
    ("a zip member", "toy_kit/data.zip", zipped(("k.bin", KEY)), "toy_kit/data.zip!k.bin"),
    ("a tar member", "toy_kit/data.tar", tarred(("k.bin", KEY)), "toy_kit/data.tar!k.bin"),
    ("a v7 tar", "toy_kit/old.tar", tar_in_v7_format(), "toy_kit/old.tar"),
]


@pytest.mark.parametrize(("member", "data", "label"), [c[1:] for c in ARCHIVED], ids=[c[0] for c in ARCHIVED])
def test_k13_a_copy_of_the_key_file_archived_in_a_member_of_the_authors_wheel_is_refused(finders, member, data, label):
    """K13: a member of the author's own wheel that is not text, a zip or a tar, is opened for a copy of the key file:
    one inside is refused by its path, attested or not, with no attestation hint and no entry in `binaries`, which
    would carry a digest."""
    filename, archive = wheel_of("toy-kit", "1.0", {member: data})
    wheel = Wheel("toy-kit", "1.0", filename, archive, from_index=False, direct=True)
    path = f"{wheel.path}/{member}"
    for attested in ({}, {path: Opaque(path, hashlib.sha256(data).hexdigest(), "read by hand")}):
        found = scan(wheel, finders, attested=attested)
        assert f"{wheel.path}/{label}: {FOUND_COPY}" in found.findings, found.findings
        assert not any("attest" in line for line in found.findings), found.findings
        assert found.binaries[wheel.path] == []
        assert not digests_printed(found.findings)


@pytest.mark.parametrize("source", ["an index", "a release the lock proves", "the core"])
def test_k13_a_copy_of_the_key_file_in_a_wheel_not_searched_for_key_locations_is_still_refused(tmp_path, source):
    """K13: a wheel from an index, a release the lock proves and the core's are not searched for key locations, but
    every member of every wheel is walked for a copy of the key file: one, or one archived in a member, is refused by
    its path, and neither `binaries` nor any line holds its digest."""
    finders = finders_of(tmp_path, BINARY_KEY)
    name = CORE_DISTRIBUTION if source == "the core" else "toy-kit"
    module = name.replace("-", "_")
    members = {f"{module}/blob.bin": BINARY_KEY, f"{module}/data.zip": zipped(("k.bin", BINARY_KEY))}
    filename, data = wheel_of(name, "1.0", members)
    wheel = Wheel(name, "1.0", filename, data, from_index=source == "an index")
    found = scan(wheel, finders, exempt={wheel.path} if source == "a release the lock proves" else ())
    assert f"{wheel.path}/{module}/blob.bin: {FOUND_COPY}" in found.findings, found.findings
    assert f"{wheel.path}/{module}/data.zip!k.bin: {FOUND_COPY}" in found.findings, found.findings
    assert found.binaries[wheel.path] == []
    assert not digests_printed([*found.findings, *found.binaries[wheel.path]])
