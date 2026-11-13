"""Make a deterministic, invented dataset to import: the directory a storage-import layout's variable names.

    uv run python deploy/compose-federation/make_import_tree.py OUT [--small N] [--dirs D] [--large-bytes B]

OUT must not exist or must be empty. The script writes N small files (default 1000), spread over D directories named
d00, d01, and so on (default 10), each of a size between 1 and 65536 bytes that depends on its path alone, and, when
B is above 0, one file `large.bin` of B bytes at the top, written in pieces of 16 MiB and never held whole. Every
file's bytes are a SHA-256 counter stream keyed by its path, as the federation's baseline files are
(fedlayout.BaselineFile.content), so two runs make the same tree. Files get mode 0664 and directories 0775, as the
uploads of op-worker have, whatever the umask. It prints the number of files and their total bytes.

It imports nothing of this repository, so it runs on any machine that has Python 3.12, to make a dataset that never
came from anywhere: nothing in it is real data.
"""

import argparse
import hashlib
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

PIECE = 16 << 20  # the most a file's writer holds at once
SMALL_SIZES = (1, 65536)  # the bounds of a small file's size, in bytes
FILE_MODE = 0o664
DIR_MODE = 0o775
LARGE = "large.bin"


def stream(path: str, size: int, piece: int | None = None) -> Iterator[bytes]:
    """The bytes of a file in pieces of at most `piece` (default PIECE): SHA-256 digests of `path:counter` in order,
    cut to `size`. The pieces are cut from one running buffer, so the bytes are the same whatever the piece size."""
    limit = piece or PIECE
    buffer, counter, left = bytearray(), 0, size
    while left > 0:
        wanted = min(limit, left)
        while len(buffer) < wanted:
            buffer += hashlib.sha256(f"{path}:{counter}".encode()).digest()
            counter += 1
        yield bytes(buffer[:wanted])
        del buffer[:wanted]
        left -= wanted


def small_size(path: str) -> int:
    """A small file's size, from its path: between the bounds, and different from file to file."""
    low, high = SMALL_SIZES
    return low + int.from_bytes(hashlib.sha256(f"size:{path}".encode()).digest()[:8], "big") % (high - low + 1)


def small_path(index: int, directories: int) -> str:
    """The path of the index-th small file: files go round the directories in turn."""
    return f"d{index % directories:02d}/file-{index:06d}.bin"


def write_file(out: Path, path: str, size: int) -> int:
    """Write one file piece by piece and give it the upload mode; return its size."""
    target = out / path
    with target.open("xb") as handle:
        for piece in stream(path, size):
            handle.write(piece)
    target.chmod(FILE_MODE)
    return size


def make_tree(out: Path, small: int, directories: int, large_bytes: int) -> tuple[int, int]:
    """Write the dataset into `out`, an empty directory; return the number of files and their total bytes."""
    out.chmod(DIR_MODE)
    for index in range(min(directories, small)):
        (out / f"d{index:02d}").mkdir()
        (out / f"d{index:02d}").chmod(DIR_MODE)
    total = sum(write_file(out, path, small_size(path)) for path in (small_path(i, directories) for i in range(small)))
    files = small
    if large_bytes:
        total += write_file(out, LARGE, large_bytes)
        files += 1
    return files, total


def claim(out: Path) -> None:
    """Make `out` if it is not there; refuse one that is a link, is not a directory, or holds anything."""
    if out.is_symlink():
        raise ValueError("the output directory is a symbolic link")
    if out.exists() and not out.is_dir():
        raise ValueError("the output path exists and is not a directory")
    if out.is_dir() and any(out.iterdir()):
        raise ValueError("the output directory is not empty")
    out.mkdir(parents=True, exist_ok=True)


def count(low: int):
    """An argparse type: an integer of at least `low`."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
        if value < low:
            raise argparse.ArgumentTypeError(f"must be at least {low}")
        return value

    return parse


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path, help="the directory to write: it must not exist or must be empty")
    parser.add_argument("--small", type=count(0), default=1000, help="the number of small files (default 1000)")
    parser.add_argument("--dirs", type=count(1), default=10, help="the directories they are spread over (default 10)")
    parser.add_argument("--large-bytes", type=count(0), default=0, help="the size of large.bin; 0 makes none")
    args = parser.parse_args(argv)
    try:
        claim(args.out)
        files, total = make_tree(args.out, args.small, args.dirs, args.large_bytes)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"{files} files, {total} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
