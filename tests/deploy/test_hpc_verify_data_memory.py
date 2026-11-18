"""O57, C06: `hpcfed.py verify-data` reads an ingested space back in memory that does not grow with the data.

It ran on the login node, whose account shares one 5 GiB cgroup with the federations' fault relays, and read 17,538
files (12.8 GB) back through their owner with a resident size that climbed to about 4 GB: each file was fetched as a
whole body (`Rest.call`, then `.content`) before it was hashed, 16 at a time. A kill of the account takes a relay down
with it. These tests use the real REST client (`Rest.stream` over httpx), the real `Content` and the real
`hpcdata.content_reader`, over a provider that makes each file's bytes as they are asked for and holds none; only the
transport is a stand-in. The peak is `tracemalloc`'s, not a process's resident size: that is a lifetime high-water mark,
and any heavier test before this one would hide it."""

import hashlib
import tracemalloc
from collections.abc import Iterator
from functools import cache
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from hpc_fed_support import LAYOUT, hpc_on_path

from expcore_onedata.importmanifest import Manifest, ManifestFile

MIB = 1024 * 1024
BOUND = 64 * MIB  # what reading a space may hold at its peak, whatever the space holds
BLOCK = 64 * 1024  # what a socket hands over at a time
SPACE = "alpha-space"  # a study space: not the probe space, so every file of it is read
NOTHING = hashlib.sha256(b"").hexdigest()  # a file that cannot be read is read as no bytes


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedcontent
        import fedrest
        import fedspec
        import hpcdata

        modules.content, modules.rest, modules.spec, modules.data = fedcontent, fedrest, fedspec, hpcdata
        yield modules


def blocks(value: int, size: int, cut_after: int | None = None) -> Iterator[bytes]:
    """One file's bytes, made as they are asked for: `size` bytes of `value`, in blocks a socket would hand over, and a
    reset of the connection after `cut_after` bytes when that is given. Nothing of the file is kept here."""
    block, sent = bytes([value]) * BLOCK, 0
    while sent < size:
        if cut_after is not None and sent >= cut_after:
            raise httpx.ReadError("the connection was reset")
        piece = min(BLOCK, size - sent)
        yield block if piece == BLOCK else block[:piece]
        sent += piece


@cache
def sha256_of(value: int, size: int) -> str:
    hasher = hashlib.sha256()
    for piece in blocks(value, size):
        hasher.update(piece)
    return hasher.hexdigest()


class Provider:
    """A provider's content endpoint over `files`, the byte a file holds and its size by the file's id; `bad` names the
    ids it fails on: `status` answers 500 with a long body, `cut` resets the connection part-way."""

    def __init__(self, files: dict[str, tuple[int, int]], bad: dict[str, str] | None = None) -> None:
        self.files, self.bad = files, bad or {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        file_id = request.url.path.split("/")[-2]
        value, size = self.files[file_id]
        failure = self.bad.get(file_id)
        if failure == "status":
            return httpx.Response(500, content=blocks(0, 4 * MIB), request=request)
        stream = blocks(value, size, cut_after=2 * BLOCK if failure == "cut" else None)
        return httpx.Response(200, content=stream, request=request)


def reader_over(hpc, provider: Provider):
    """`content_reader` over a bootstrap that lists the space's files and reads them through the real REST client."""
    rest = object.__new__(hpc.rest.Rest)  # the real client over a stand-in transport: no CA, no network
    rest._http = httpx.Client(transport=httpx.MockTransport(provider.handle), trust_env=False)
    listing = [
        {"name": f"{file_id}.bin", "type": "REG", "file_id": file_id, "size": size}
        for file_id, (_, size) in provider.files.items()
    ]

    def call(service: Any, method: str, path: str, label: str, **options: Any) -> Any:
        if path.startswith("/spaces/"):
            return {"fileId": "root"}
        assert path.endswith("/children"), path
        return {"children": listing, "isLast": True}

    boot = SimpleNamespace(
        layout=SimpleNamespace(spec=hpc.spec.load_spec(LAYOUT)),
        ids=SimpleNamespace(spaces={SPACE: "space-id"}),
        provider=call,
        rest=rest,
        admin_token=lambda: "token",
    )
    return hpc.data.content_reader(boot)


def test_o57_a_space_far_larger_than_the_bound_is_read_back_in_bounded_memory(hpc):
    """O57: the files of a space add up to over nine times what its read may hold, one of them to more than the bound
    alone, and 16 are read at a time: the peak stays under the bound, and every file's size and digest is the one of
    the bytes the provider made."""
    sizes = [32 * MIB] * 16 + [96 * MIB]
    files = {f"id-{i}": (i % 3, size) for i, size in enumerate(sizes)}
    reader = reader_over(hpc, Provider(files))
    expected = {f"id-{i}.bin": (size, sha256_of(value, size)) for i, (value, size) in enumerate(files.values())}
    tracemalloc.start()
    try:
        tree, directories = reader(SPACE)
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    assert sum(sizes) > 9 * BOUND
    assert peak < BOUND, f"the read held {peak / MIB:.0f} MiB at its peak, over the {BOUND // MIB} MiB bound"
    assert tree == expected
    assert directories == set()


def test_o57_a_file_that_cannot_be_read_is_read_as_no_bytes_and_counted_never_a_traceback(hpc):
    """O57: a file whose read is refused (500, with a body of its own) or is cut part-way is read as no bytes, never as
    the part that arrived and never an error out of the pool: the verdict is the manifest's counts, as it was when the
    whole body was fetched first."""
    files = {"id-ok": (7, 3 * BLOCK), "id-refused": (1, 5 * BLOCK), "id-cut": (2, 9 * BLOCK), "id-empty": (3, 0)}
    reader = reader_over(hpc, Provider(files, bad={"id-refused": "status", "id-cut": "cut"}))
    tree, _ = reader(SPACE)
    assert tree == {
        "id-ok.bin": (3 * BLOCK, sha256_of(7, 3 * BLOCK)),
        "id-refused.bin": (5 * BLOCK, NOTHING),
        "id-cut.bin": (9 * BLOCK, NOTHING),
        "id-empty.bin": (0, NOTHING),
    }
    declared = Manifest(
        tuple(
            sorted(
                (
                    ManifestFile(f"{file_id}.bin", size, sha256_of(value, size))
                    for file_id, (value, size) in files.items()
                ),
                key=lambda entry: entry.path,
            )
        ),
        (),
    )
    assert hpc.data.verify({SPACE: declared}, reader) == {SPACE: "2 file(s) of another size or other bytes"}
