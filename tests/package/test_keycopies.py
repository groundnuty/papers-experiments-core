"""The walk for copies of a declared key file: complete whatever the content scanner refuses, attested or not, in
every wheel, and refusing what it cannot finish; it never prints a digest."""

import base64
import gzip
import hashlib
import io
import tarfile
import zipfile
from types import SimpleNamespace

import pytest
from package_support import wheel_of

from expcore.config.key_locations import location_finders
from expcore.package import views
from expcore.package.leaks import FOUND_COPY, UNCHECKED, find_locations
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


def tarred(*members: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


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


def test_k13_nothing_is_walked_for_a_copy_when_no_key_file_can_be_read(tmp_path):
    """K13: with no key file to compare (none declared, or none there), nothing is walked, so nothing it cannot finish
    refuses the build."""
    finders = location_finders([SimpleNamespace(key_file=str(tmp_path / "absent.secret"), key_env=None, key="gamma")])
    assert find_locations({"data/a.bin": encrypted()}, finders, opaque={"data/a.bin"}) == []


def test_k13_an_attested_archive_with_no_copy_in_it_passes(finders):
    """K13: an attested archive the content scanner would refuse (a PNG, a compressed tar inside), which holds no copy
    of the key file, passes the walk; so do bytes that hold the key among others."""
    inner = gzip.compress(tarred(("notes.txt", b"plain notes\n"), ("plot.png", PNG)))
    bundle = zipped(("figures/plot.png", PNG), ("inner.tar.gz", inner), ("near.bin", b"\0" + KEY))
    assert find_locations({"data/bundle.zip": bundle}, finders, opaque={"data/bundle.zip"}) == []


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
