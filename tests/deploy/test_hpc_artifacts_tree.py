"""O74: the digest of a study's artifacts directory, `hpcidentity.tree_sha256`, which `hpcfed.py up` puts into the
render identity as `study_artifacts_tree_sha256` when the site declares `HPC_STUDY_ARTIFACTS`. It is the SHA-256 of one
canonical listing: a fixed version prefix, then a record for every regular file (relative path, size, SHA-256), every
directory (relative path) and every link (relative path, target string, never followed), sorted by the path's bytes.
Mode, owner and time are not in it, and no path of the directory itself is, in the digest or in an error. A link whose
target leaves the directory is refused (the directory is self-contained: its interpreter is in it), resolved as the
kernel would, link by link, against the tree's own entries and never touching what is outside. Every tree here is made
by hand under `tmp_path`; nothing runs a real venv."""

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
    """What a study's artifacts directory holds: its tool server's clone (a `.git` in it), the interpreter installed in
    it, a venv made by hand with the links a real one has (`bin/python3` is a link to the interpreter, `lib64` to
    `lib`), the data manifests, and an empty directory. Every link stays inside the tree."""
    put(root, {
        "clone/server.py": b"print('serve')\n",
        "clone/.git/HEAD": b"ref: refs/heads/main\n",
        "clone/.git/index": b"an index\n",
        "python/bin/python3.12": b"an interpreter\n",
        "python/lib/libpython3.12.so.1.0": b"a library\n",
        "venv/pyvenv.cfg": b"home = ../python/bin\n",
        "venv/lib/python3.12/site-packages/pkg/__init__.py": b"VALUE = 1\n",
        "manifests/space.json": b'{"version": 1}\n',
    })  # fmt: skip
    (root / "python" / "bin" / "python3").symlink_to("python3.12")  # the interpreter's own links
    (root / "python" / "lib" / "libpython3.so").symlink_to("libpython3.12.so.1.0")
    (root / "venv" / "bin").mkdir()
    (root / "venv" / "bin" / "python3").symlink_to("../../python/bin/python3.12")
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
    link.symlink_to("../../python/bin/python3")  # another way to the same interpreter: another listing


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


# -- links: recorded by their target string, never followed; one that leaves the tree is refused --------------------

# The digest of `study_tree`, as the code before links were checked (3531d3cc) took it: the canonical form of a tree
# whose links all stay inside did not change when the check was added.
STUDY_TREE_DIGEST = "ce01443fadbd22e880de62b992578ef503509a1194e777bb1102377b4683809a"


def test_o74_a_tree_with_only_inside_links_has_the_digest_the_code_before_the_check_gave(identity, tmp_path):
    """O74: pinned. `study_tree` holds `lib64 -> lib`, the venv's `bin/python3` into the interpreter installed in the
    tree and the interpreter's own links: all inside, all recorded by their target strings, so its digest is the one
    3531d3cc computed. A change here is a change of the identity every pinned plan holds."""
    assert identity.tree_sha256(study_tree(tmp_path / "artifacts")) == STUDY_TREE_DIGEST


def test_o74_a_link_inside_the_tree_is_recorded_by_its_target_string_and_not_descended(identity, tmp_path):
    """O74: a link to a directory inside is recorded by its target and not descended (its files are counted once,
    under their own paths); a link that dangles inside is recorded as it is, and a name that appears where it points is
    a change of the tree, so the digest moves."""
    root = put(tmp_path / "artifacts", {"d/f": b"in"})
    (root / "in-dir").symlink_to("d")
    (root / "dangling").symlink_to("nowhere")
    records = [
        b"D\0d\0",
        b"F\0d/f\0" + b"2\0" + sha(b"in").encode() + b"\0",
        b"L\0dangling\0nowhere\0",
        b"L\0in-dir\0d\0",
    ]
    before = identity.tree_sha256(root)
    assert before == sha(PREFIX + b"".join(records))
    (root / "nowhere").write_bytes(b"now it is there")
    assert identity.tree_sha256(root) != before


def test_o74_an_absolute_link_into_the_tree_is_allowed_by_the_roots_real_path_or_the_path_it_was_given_by(
    identity, tmp_path
):
    """O74: a venv built on the directory holds absolute targets. They are inside when they start, component by
    component, with the directory's real path or with the path it is declared by (`HPC_STUDY_ARTIFACTS`, when a parent
    of it is a link); each is recorded as written."""
    real = tmp_path / "real"
    put(real / "artifacts", {"venv/lib/x": b"x"})
    (tmp_path / "alias").symlink_to(real)
    declared = tmp_path / "alias" / "artifacts"
    by_real = os.path.realpath(declared) + "/venv/lib"
    by_declared = f"{declared}/venv/lib"
    assert by_real != by_declared
    (declared / "venv" / "lib64-real").symlink_to(by_real)
    (declared / "venv" / "lib64-declared").symlink_to(by_declared)
    records = {
        b"venv": b"D\0venv\0",
        b"venv/lib": b"D\0venv/lib\0",
        b"venv/lib/x": b"F\0venv/lib/x\0" + b"1\0" + sha(b"x").encode() + b"\0",
        b"venv/lib64-declared": b"L\0venv/lib64-declared\0" + by_declared.encode() + b"\0",
        b"venv/lib64-real": b"L\0venv/lib64-real\0" + by_real.encode() + b"\0",
    }
    expected = sha(PREFIX + b"".join(records[name] for name in sorted(records)))
    assert identity.tree_sha256(declared) == expected


def test_o74_a_link_to_the_directory_itself_or_to_a_parent_inside_it_is_allowed(identity, tmp_path):
    """O74: a link that resolves to the root, or to a directory above it inside, stays in the tree (it is recorded by
    its target, and the directory is not descended through it)."""
    root = study_tree(tmp_path / "artifacts")
    (root / "venv" / "bin" / "up").symlink_to("../..")
    (root / "venv" / "bin" / "up-to-venv").symlink_to("..")
    (root / "here").symlink_to(os.path.realpath(root))
    assert HEX.fullmatch(identity.tree_sha256(root))


def test_o74_a_link_resolves_as_the_kernel_resolves_it_not_by_collapsing_dots(identity, tmp_path):
    """O74: `a/b/d -> ..` is a link to `a`, so `a/b/d/..` is the root, not `a/b`: `fine -> a/b/d/../a/b` reaches
    `a/b` of the root and is allowed."""
    root = put(tmp_path / "artifacts", {"a/b/f": b"f"})
    (root / "a" / "b" / "d").symlink_to("..")
    (root / "fine").symlink_to("a/b/d/../a/b")
    assert HEX.fullmatch(identity.tree_sha256(root))


def leaves(
    identity, root: Path, tmp_path: Path, name: str, target: str | None, says: str = "leaves the directory"
) -> str:
    """The digest of `root` is refused with a message that starts with the link's path relative to it, says `says`,
    holds no absolute path and does not echo the link's target (`target`, unless it is a name too short to tell from
    the message's own text); it never blocks (nothing outside is opened)."""
    with deadline(), pytest.raises(identity.TreeError) as refused:
        identity.tree_sha256(root)
    message = str(refused.value)
    assert message.startswith(f"{name} ")
    assert says in message
    assert str(tmp_path) not in message
    assert target is None or target not in message
    assert message.isprintable()
    return message


def outside_place(tmp_path: Path) -> Path:
    return put(tmp_path / "outside", {"python3.12": b"an interpreter", "lib/x": b"x"})


@pytest.mark.parametrize(
    "what", ["a file", "a directory", "a missing name", "the filesystem's root", "the tree's parent"]
)
def test_o74_an_absolute_link_to_a_place_outside_the_tree_is_refused_naming_its_relative_path(identity, tmp_path, what):
    """O74: the digest holds where a link points and not what is there, so a target outside is a part of the study
    that is not pinned. Refused, whatever the target is (even a name that does not exist), naming the link's path
    relative to the directory: never the target and never an absolute path."""
    outside = outside_place(tmp_path)
    targets = {"a file": outside / "python3.12", "a directory": outside / "lib", "a missing name": outside / "none",
               "the filesystem's root": Path("/"), "the tree's parent": tmp_path}  # fmt: skip
    root = study_tree(tmp_path / "artifacts")
    (root / "venv" / "bin" / "out").symlink_to(targets[what])
    leaves(identity, root, tmp_path, "venv/bin/out", None if what == "the filesystem's root" else str(targets[what]))


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("out", "../outside/python3.12"),
        ("out", ".."),
        ("out", "nowhere/../../outside/lib"),
        ("venv/out", "../../outside/lib"),
        ("venv/out", "lib/../../../outside/lib"),
        ("venv/bin/out", "./../../../outside/python3.12"),
        ("venv/bin/out", "../../../.."),
    ],
    ids=["file", "parent", "after a missing name", "from a directory", "after a directory", "dot first", "deep"],
)
def test_o74_a_link_that_escapes_by_dot_dot_is_refused_naming_its_relative_path(identity, tmp_path, link, target):
    """O74: a relative target is read from the link's own directory: each `..` goes one directory up, and one that
    would leave the tree is an escape, whatever lies on the way (a directory, a missing name)."""
    outside_place(tmp_path)
    root = study_tree(tmp_path / "artifacts")
    (root / link).symlink_to(target)
    leaves(identity, root, tmp_path, link, target)


def test_o74_an_absolute_link_that_enters_the_tree_and_leaves_by_dot_dot_is_refused(identity, tmp_path):
    """O74: the root's path as a prefix is no proof: `<root>/../outside/python3.12` starts inside and ends outside."""
    outside_place(tmp_path)
    root = study_tree(tmp_path / "artifacts")
    target = f"{os.path.realpath(root)}/../outside/python3.12"
    (root / "out").symlink_to(target)
    leaves(identity, root, tmp_path, "out", target)


@pytest.mark.parametrize("sibling", ["2", "-old", ".bak"])
def test_o74_an_absolute_link_into_a_sibling_whose_name_starts_with_the_roots_is_refused(identity, tmp_path, sibling):
    """O74: `/p/artifacts2/f` is not inside `/p/artifacts`: the comparison is by whole components, never by the
    string's prefix."""
    root = study_tree(tmp_path / "artifacts")
    other = put(tmp_path / f"artifacts{sibling}", {"f": b"x"})
    target = os.path.realpath(other) + "/f"
    assert target.startswith(os.path.realpath(root))
    (root / "venv" / "bin" / "sibling").symlink_to(target)
    leaves(identity, root, tmp_path, "venv/bin/sibling", target)


def test_o74_a_chain_of_links_that_ends_outside_is_refused_naming_the_first_link(identity, tmp_path):
    """O74: `a -> b -> <outside>`: `b` itself leaves the tree, and so does `a`, which reaches the outside through it;
    the first of them by path is named."""
    outside = outside_place(tmp_path)
    root = study_tree(tmp_path / "artifacts")
    (root / "a").symlink_to("b")
    (root / "b").symlink_to(outside / "python3.12")
    leaves(identity, root, tmp_path, "a", str(outside))


def test_o74_a_link_that_goes_through_a_link_to_a_parent_leaves_when_the_kernel_would_leave(identity, tmp_path):
    """O74: `a/b/d -> ..` is a link to `a` (it stays inside), so `esc -> a/b/d/../../x` is `<root's parent>/x`, though
    collapsing the dots as text gives `a/x`, inside the tree: the link's target is followed as the kernel follows it."""
    root = put(tmp_path / "artifacts", {"a/b/f": b"f", "a/x": b"x"})
    (root / "a" / "b" / "d").symlink_to("..")
    (root / "esc").symlink_to("a/b/d/../../x")
    leaves(identity, root, tmp_path, "esc", "a/b/d/../../x")


def test_o74_a_link_through_a_directory_link_that_leaves_is_refused_where_the_link_that_leaves_is(identity, tmp_path):
    """O74: `venv/lib64 -> lib` stays inside; `venv/bin/ext -> ../lib64/../../../outside` goes through it, up past the
    root, and is refused."""
    outside_place(tmp_path)
    root = study_tree(tmp_path / "artifacts")
    (root / "venv" / "bin" / "ext").symlink_to("../lib64/../../../outside/lib")
    leaves(identity, root, tmp_path, "venv/bin/ext", "../lib64/../../../outside/lib")


def chain(root: Path, length: int) -> None:
    """`c00 -> c01 -> ... -> target`: `length` links in a row ending at a file of the tree."""
    names = [f"c{number:02d}" for number in range(length)]
    for name, following in zip(names, [*names[1:], "target"], strict=True):
        (root / name).symlink_to(following)


def test_o74_a_chain_of_forty_links_is_followed_and_one_of_forty_one_is_refused(identity, tmp_path):
    """O74: the bound is `MAX_LINK_HOPS` (40, the kernel's own limit): the link itself is the first of the links a
    path passes through. 40 in a row ending at a file are allowed; 41 are refused, naming the first, the only one that
    needs 41 (the second needs 40)."""
    assert identity.MAX_LINK_HOPS == 40
    allowed = put(tmp_path / "allowed", {"target": b"t"})
    chain(allowed, 40)
    assert HEX.fullmatch(identity.tree_sha256(allowed))
    refused = put(tmp_path / "refused", {"target": b"t"})
    chain(refused, 41)
    leaves(identity, refused, tmp_path, "c00", "c01", says="more than 40 links")


@pytest.mark.parametrize("loop", ["itself", "two"])
def test_o74_a_loop_of_links_is_refused_by_the_bound(identity, tmp_path, loop):
    """O74: a link to itself, or two to each other, never resolve: the bound refuses them, naming the first."""
    root = put(tmp_path / "artifacts", {"f": b"f"})
    (root / "a").symlink_to("a" if loop == "itself" else "b")
    if loop == "two":
        (root / "b").symlink_to("a")
    leaves(identity, root, tmp_path, "a", None, says="more than 40 links")


def test_o74_a_link_to_a_fifo_outside_is_refused_and_the_fifo_is_never_opened(identity, tmp_path):
    """O74: nothing outside the tree is read to decide: a link to a FIFO (a read-only open of which blocks until a
    writer comes) is refused at once."""
    outside = outside_place(tmp_path)
    os.mkfifo(outside / "pipe")
    root = study_tree(tmp_path / "artifacts")
    (root / "pipe").symlink_to(outside / "pipe")
    leaves(identity, root, tmp_path, "pipe", str(outside))


def test_o74_a_refused_link_is_shown_escaped_never_as_a_raw_newline(identity, tmp_path):
    """O74: the link's path in the message is printable ASCII, as every other path is."""
    root = tmp_path / "artifacts"
    root.mkdir()
    os.symlink("/", os.path.join(os.fsencode(root), b"a\nb\xff"))
    with pytest.raises(identity.TreeError) as refused:
        identity.tree_sha256(root)
    shown = str(refused.value)
    assert shown.isprintable()
    assert shown.startswith("a\\nb")
    assert "leaves the directory" in shown


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


def test_o74_a_file_that_changes_while_it_is_read_is_refused_naming_it(identity, tmp_path, monkeypatch):
    """O74: a file appended to between the first and the last of its reads is no file the digest can name: it is
    refused with its relative path, as is one whose mtime moved, never hashed in two states."""
    root = put(tmp_path / "artifacts", {"clone/f": b"x" * 20})
    monkeypatch.setattr(identity, "CHUNK", 7)
    real, grown = os.read, []

    def appending(descriptor, length):
        if not grown:
            grown.append(True)
            with open(root / "clone" / "f", "ab") as handle:
                handle.write(b"more")
        return real(descriptor, length)

    monkeypatch.setattr(identity.os, "read", appending)
    with pytest.raises(identity.TreeError) as refused:
        identity.tree_sha256(root)
    assert "clone/f" in str(refused.value)
    assert "changed while it was read" in str(refused.value)
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
