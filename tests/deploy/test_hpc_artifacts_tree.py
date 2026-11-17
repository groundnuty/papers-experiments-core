"""O74: the digest of a study's artifacts directory, `hpcidentity.tree_sha256`, which `hpcfed.py up` puts into the
render identity as `study_artifacts_tree_sha256` when the site declares `HPC_STUDY_ARTIFACTS`. It is the SHA-256 of one
canonical listing: a fixed version prefix, then a record for every regular file (relative path, size, SHA-256), every
directory (relative path) and every link (relative path, target string, never followed), sorted by the path's bytes.
Mode, owner and time are not in it, and no path of the directory itself is, in the digest or in an error. Every tree
here is made by hand under `tmp_path`; nothing runs a real venv."""

import contextlib
import hashlib
import os
import re
import shutil
import signal
import socket
from pathlib import Path

import pytest
from hpc_fed_support import hpc_on_path

PREFIX = b"expcore-artifacts-tree/1\0"
HEX = re.compile(r"[0-9a-f]{64}")


@pytest.fixture(scope="module")
def identity():
    with hpc_on_path():
        import hpcidentity

        yield hpcidentity


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(root: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def study_tree(root: Path) -> Path:
    """What a study's artifacts directory holds: its tool server's clone (a `.git` in it), a venv made by hand with the
    links a real one has (`bin/python` is a link to the interpreter), the data manifests, and an empty directory."""
    put(root, {
        "clone/server.py": b"print('serve')\n",
        "clone/.git/HEAD": b"ref: refs/heads/main\n",
        "clone/.git/index": b"an index\n",
        "venv/pyvenv.cfg": b"home = /an/interpreter/bin\n",
        "venv/lib/python3.12/site-packages/pkg/__init__.py": b"VALUE = 1\n",
        "manifests/space.json": b'{"version": 1}\n',
    })  # fmt: skip
    (root / "venv" / "bin").mkdir()
    (root / "venv" / "bin" / "python3").symlink_to("/an/interpreter/bin/python3.12")
    (root / "venv" / "bin" / "python").symlink_to("python3")
    (root / "venv" / "lib64").symlink_to("lib")
    (root / "empty").mkdir()
    return root


@contextlib.contextmanager
def deadline(seconds: int = 20):
    """A digest that opens a special file blocks for good: the test fails instead of holding a slot of the host."""

    def expired(signum, frame):
        raise TimeoutError("the digest blocked on a special file")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


# -- the canonical form -------------------------------------------------------------------------------------------


def test_o74_the_digest_is_the_sha256_of_the_canonical_listing_sorted_by_the_paths_bytes(identity, tmp_path):
    """O74: the prefix, then in the order of the relative paths' bytes: `F NUL path NUL size NUL sha NUL` for a file,
    `D NUL path NUL` for a directory, `L NUL path NUL target NUL` for a link. `a-b` sorts before `a/b`, so the order
    is that of the whole path (a walk's own order would put `a/b` first), and the root itself has no record."""
    root = put(tmp_path / "tree", {"a/b": b"beta", "a-b": b"dash", "z": b""})
    (root / "e").mkdir()
    (root / "l").symlink_to("a/b")
    records = [
        b"D\0a\0",
        b"F\0a-b\0" + b"4\0" + sha(b"dash").encode() + b"\0",
        b"F\0a/b\0" + b"4\0" + sha(b"beta").encode() + b"\0",
        b"D\0e\0",
        b"L\0l\0a/b\0",
        b"F\0z\0" + b"0\0" + sha(b"").encode() + b"\0",
    ]
    assert identity.tree_sha256(root) == sha(PREFIX + b"".join(records))
    assert identity.TREE_VERSION == "expcore-artifacts-tree/1"


def test_o74_an_empty_directory_has_the_digest_of_the_prefix_alone(identity, tmp_path):
    """O74: no record for the root: an empty directory is the version prefix and nothing."""
    (tmp_path / "tree").mkdir()
    assert identity.tree_sha256(tmp_path / "tree") == sha(PREFIX)


def test_o74_a_name_with_a_newline_a_space_or_bytes_that_are_no_text_is_in_the_listing_by_its_bytes(identity, tmp_path):
    """O74: a path is its raw bytes between NULs, so a newline, a space or a non-UTF-8 byte in a name changes nothing
    of the form, and a file `x<newline>y` is another tree than a directory `x` holding `y`."""
    one = put(tmp_path / "one", {"x\ny": b"c", "a b": b"d"})
    other = put(tmp_path / "other", {"x/y": b"c", "a b": b"d"})
    odd = tmp_path / "odd"
    odd.mkdir()
    with open(os.path.join(os.fsencode(odd), b"\xff\xfename"), "wb") as handle:
        handle.write(b"c")
    assert len({identity.tree_sha256(one), identity.tree_sha256(other), identity.tree_sha256(odd)}) == 3
    listing = b"F\0\xff\xfename\0" + b"1\0" + sha(b"c").encode() + b"\0"
    assert identity.tree_sha256(odd) == sha(PREFIX + listing)


def test_o74_a_file_is_hashed_whole_across_the_read_chunks(identity, tmp_path, monkeypatch):
    """O74: a file longer than a read chunk has the SHA-256 of all of its bytes, whatever the chunk."""
    data = bytes(range(251)) * 3
    root = put(tmp_path / "tree", {"big": data})
    whole = identity.tree_sha256(root)
    monkeypatch.setattr(identity, "CHUNK", 7)
    listing = PREFIX + b"F\0big\0" + str(len(data)).encode() + b"\0" + sha(data).encode() + b"\0"
    assert identity.tree_sha256(root) == whole == sha(listing)


# -- the same tree, and a different one ---------------------------------------------------------------------------


def test_o74_two_copies_of_one_tree_at_other_paths_with_other_times_give_one_digest(identity, tmp_path):
    """O74: a copy of the tree made elsewhere (links kept, every mtime another) has the digest of the original: no
    path of the directory, and no time, is in it."""
    one = study_tree(tmp_path / "one" / "artifacts")
    other = tmp_path / "somewhere" / "else" / "copy"
    shutil.copytree(one, other, symlinks=True)
    for directory, _, names in os.walk(other):
        for name in names:
            if not os.path.islink(os.path.join(directory, name)):
                os.utime(os.path.join(directory, name), (1_000_000_000, 1_000_000_000))
    digest = identity.tree_sha256(one)
    assert HEX.fullmatch(digest)
    assert identity.tree_sha256(other) == digest


def _flip_a_byte(root: Path) -> None:
    (root / "clone" / "server.py").write_bytes(b"print('sErve')\n")  # the same size


def _a_file_added(root: Path) -> None:
    (root / "clone" / "added.py").write_bytes(b"x = 1\n")


def _an_empty_file_added(root: Path) -> None:
    (root / "clone" / "empty.py").write_bytes(b"")


def _a_file_removed(root: Path) -> None:
    (root / "manifests" / "space.json").unlink()


def _an_empty_directory_added(root: Path) -> None:
    (root / "new").mkdir()


def _an_empty_directory_removed(root: Path) -> None:
    (root / "empty").rmdir()


def _a_file_renamed(root: Path) -> None:
    (root / "clone" / "server.py").rename(root / "clone" / "serve.py")


def _a_file_moved(root: Path) -> None:
    (root / "clone" / "server.py").rename(root / "manifests" / "server.py")


def _a_venv_file_changed(root: Path) -> None:
    (root / "venv" / "lib" / "python3.12" / "site-packages" / "pkg" / "__init__.py").write_bytes(b"VALUE = 2\n")


def _a_venv_package_added(root: Path) -> None:
    put(root, {"venv/lib/python3.12/site-packages/other/__init__.py": b""})


def _a_venv_link_retargeted(root: Path) -> None:
    link = root / "venv" / "bin" / "python3"
    link.unlink()
    link.symlink_to("/another/interpreter/bin/python3.12")


def _a_link_added(root: Path) -> None:
    (root / "venv" / "bin" / "py").symlink_to("python")


def _a_link_made_a_file(root: Path) -> None:
    link = root / "venv" / "lib64"
    link.unlink()
    link.write_bytes(b"lib")  # the target's text, as a file


CHANGES = [
    _flip_a_byte, _a_file_added, _an_empty_file_added, _a_file_removed, _an_empty_directory_added,
    _an_empty_directory_removed, _a_file_renamed, _a_file_moved, _a_venv_file_changed, _a_venv_package_added,
    _a_venv_link_retargeted, _a_link_added, _a_link_made_a_file,
]  # fmt: skip


@pytest.mark.parametrize("change", CHANGES, ids=[change.__name__.strip("_") for change in CHANGES])
def test_o74_any_change_of_the_tree_gives_another_digest(identity, tmp_path, change):
    """O74: one changed byte, a file or an empty directory added or removed, a file renamed or moved, a changed or a
    retargeted file of the venv, a link added or made a file: each is another digest."""
    root = study_tree(tmp_path / "artifacts")
    before = identity.tree_sha256(root)
    change(root)
    assert identity.tree_sha256(root) != before


# -- links: recorded by their target string, never followed ---------------------------------------------------------


def test_o74_a_link_is_recorded_by_its_target_string_and_what_it_points_at_is_never_read(identity, tmp_path):
    """O74: the venv's `bin/python` is a link to an interpreter outside the tree. The digest holds the target string,
    not the interpreter's bytes: changing, or removing, what the link points at changes nothing, and a link to a
    directory outside is not descended (nor is one inside, whose files are counted once, under their own paths)."""
    outside = put(tmp_path / "outside", {"python3.12": b"an interpreter", "lib/x": b"x"})
    root = put(tmp_path / "artifacts", {"d/f": b"in"})
    (root / "py").symlink_to(outside / "python3.12")
    (root / "out-dir").symlink_to(outside / "lib")
    (root / "in-dir").symlink_to("d")
    (root / "dangling").symlink_to("nowhere")
    before = identity.tree_sha256(root)
    (outside / "python3.12").write_bytes(b"another interpreter")
    (outside / "lib" / "y").write_bytes(b"y")
    assert identity.tree_sha256(root) == before
    (outside / "python3.12").unlink()
    assert identity.tree_sha256(root) == before


# -- what is refused, and how it is said -------------------------------------------------------------------------------


def test_o74_a_fifo_is_refused_naming_its_relative_path_and_never_opened(identity, tmp_path):
    """O74: a special file is refused, with the relative path and no absolute one in the message, and it is never
    opened (a read-only open of a FIFO blocks until a writer comes)."""
    root = study_tree(tmp_path / "artifacts")
    os.mkfifo(root / "venv" / "run")
    with deadline(), pytest.raises(identity.TreeError) as refused:
        identity.tree_sha256(root)
    assert "venv/run" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)


def test_o74_a_socket_is_refused_naming_its_relative_path(identity, tmp_path, monkeypatch):
    """O74: a socket file in the tree is refused, as a FIFO is."""
    root = study_tree(tmp_path / "artifacts")
    monkeypatch.chdir(root / "clone")  # a name is short and relative, a socket's path is limited to about 100 bytes
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind("agent.sock")
        with deadline(), pytest.raises(identity.TreeError) as refused:
            identity.tree_sha256(root)
    assert "clone/agent.sock" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)


def test_o74_a_refused_name_is_shown_escaped_never_as_a_raw_newline(identity, tmp_path):
    """O74: a path in a message is printable ASCII: a newline or a byte that is no text in the name is escaped."""
    root = tmp_path / "artifacts"
    root.mkdir()
    os.mkfifo(os.path.join(os.fsencode(root), b"a\nb\xff"))
    with pytest.raises(identity.TreeError) as refused:
        identity.tree_sha256(root)
    shown = str(refused.value)
    assert shown.isprintable()
    assert "a\\nb" in shown


@pytest.mark.parametrize("what", ["missing", "a file", "a link to a directory"])
def test_o74_a_root_that_is_not_a_directory_of_its_own_is_refused_without_its_path(identity, tmp_path, what):
    """O74: the directory must exist, be a directory and not be a link itself: each other root is refused, and the
    message holds no absolute path."""
    real = study_tree(tmp_path / "real")
    root = {"missing": tmp_path / "missing", "a file": real / "clone" / "server.py",
            "a link to a directory": tmp_path / "link"}[what]  # fmt: skip
    if what == "a link to a directory":
        root.symlink_to(real)
    with pytest.raises(identity.TreeError) as refused:
        identity.tree_sha256(root)
    assert "directory" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="the superuser reads a directory its mode shuts")
def test_o74_a_directory_that_cannot_be_read_is_refused_naming_it(identity, tmp_path):
    """O74: a directory (or a file) the account cannot read is refused with its relative path: the digest would
    otherwise be of a part of the tree."""
    root = study_tree(tmp_path / "artifacts")
    shut = root / "venv" / "lib"
    shut.chmod(0)
    try:
        with pytest.raises(identity.TreeError) as refused:
            identity.tree_sha256(root)
    finally:
        shut.chmod(0o755)
    assert "venv/lib" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)


def test_o74_a_tree_deeper_than_the_cap_is_refused_naming_where(identity, tmp_path):
    """O74: a directory nested past `MAX_DEPTH` is refused (a walk holds one descriptor per level) with its relative
    path, not walked on."""
    root = tmp_path / "artifacts"
    deepest = root.joinpath(*["d"] * (identity.MAX_DEPTH + 1))
    deepest.mkdir(parents=True)
    with pytest.raises(identity.TreeError) as refused:
        identity.tree_sha256(root)
    assert "nested deeper" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)


# -- read, never written ---------------------------------------------------------------------------------------------


def snapshot(root: Path) -> dict[str, tuple]:
    """Every entry's identity and times but its access time (a read may move that on a filesystem that keeps it)."""
    found = {}
    for directory, names, files in os.walk(root):
        for name in [*names, *files]:
            info = os.lstat(os.path.join(directory, name))
            found[os.path.relpath(os.path.join(directory, name), root)] = (
                info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino
            )  # fmt: skip
    return found


def test_o74_a_read_only_tree_gives_its_writable_copys_digest_and_is_left_exactly_as_it_was(identity, tmp_path):
    """O74: the pilot makes its artifacts directory read-only before `up`: the digest reads it, writes nothing into it
    (no entry made, none changed: modes, sizes, mtimes and ctimes as they were) and is the writable copy's."""
    root = study_tree(tmp_path / "artifacts")
    shutil.copytree(root, tmp_path / "writable", symlinks=True)
    for directory, _, files in os.walk(root, topdown=False):
        for name in files:
            if not os.path.islink(os.path.join(directory, name)):
                os.chmod(os.path.join(directory, name), 0o444)
        os.chmod(directory, 0o555)
    try:
        before = snapshot(root)
        digest = identity.tree_sha256(root)
        assert snapshot(root) == before
    finally:
        for directory, _, _ in os.walk(root):
            os.chmod(directory, 0o755)
    assert digest == identity.tree_sha256(tmp_path / "writable")
