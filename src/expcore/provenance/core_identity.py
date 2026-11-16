# Provenance: new
"""Which core code is running: its commit, its git tree, whether it is dirty, and a digest of its Python source.

    {"kind": "core", "commit": "<40 hex>", "tree": "<40 hex>", "dirty": false, "python_sha256": "<64 hex>"}

The core is 0.1.0 at many commits with identical dependencies, so its version names no code, and how it was installed
(editable, from a git URL, from a wheel or from a directory) says less still. This entry names the code itself, the
same way however one commit is installed, and the campaign's environment records it (`provenance.environment`):

- **From a checkout** (an editable install, or `src/` on the path): when the imported `expcore` package is the
  `src/expcore` of a project named `papers-experiments-core` whose directory is the top of a git work tree, the entry
  is read live from git there: `HEAD`, `HEAD^{tree}`, and dirty when a file a build reads (`builds_from`: `src/`,
  the example, the project and build files, the token hashes) differs from `HEAD` or is new and not ignored, or a
  Python file git ignores lies in the packages (it is imported, and no commit or wheel holds it), or the packages
  hold a link. A change elsewhere in the repository (a note, a test, the session log this project's hooks keep
  writing, the marker uv leaves at the top of its git checkouts) reaches no build and no import, and is not dirt. A
  checkout git cannot read is dirty.
- **From a build** (a wheel, a git-URL or directory install, a wheel built from an sdist): the build hook
  (`hatch_build.py`) writes `expcore/_build_identity.json` (`BUILD_FILE`) into the wheel and the sdist, read the same
  way from the checkout it builds from, or taken from the sdist it builds from. The installed Python source is hashed
  again on every read and compared with the digest the build recorded: an installed core edited since is stale, and
  stale is dirty.
- Neither: the commit is `unknown`, and unknown is dirty (P01).

`python_sha256` is over every `*.py` file of the packages `expcore` and `expcore_onedata`, by its path below the
directory holding them (`expcore/cli.py`) and its content, without the example study the wheel carries under
`expcore/examples/` (the study's own code once written out). A running core walks its directories, a checkout's
`src/` as an installed core's site directory, following links (a cycle once): the digest is of what is imported. A build
leaves out the files git ignores there, as the wheel does, so it records what its install will walk. The two agree on
every checkout that is not dirty, and a link in a checkout's packages is dirt, since git holds where it points and not
what it leads to. Two dirty trees at one commit therefore differ when their code does.

This module imports nothing of the core, so the build hook loads it by its path, without installing anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

CORE_PROJECT = "papers-experiments-core"
BUILD_FILE = "_build_identity.json"
BUILD_FORMAT = "expcore-core-identity/1"
KIND = "core"
UNKNOWN = "unknown"
PACKAGES = ("expcore", "expcore_onedata")
EXAMPLES = "expcore/examples/"  # the example study the wheel carries: data the core writes out, not its code
BUILD_DIRS = ("src/", "examples/tiny_study/")  # what a wheel ships from, and what an editable core imports
BUILD_FILES = frozenset(
    {"pyproject.toml", "hatch_build.py", "README.md", ".gitignore", "tests/data/study_tokens.sha256"}
)
_HEX = frozenset("0123456789abcdef")
_SHA_CHARS = 40
_SKIPPED_DIRS = frozenset({"__pycache__"})


@dataclass(frozen=True)
class CoreIdentity:
    """The identity of the core in use, and how it was found.

    `source` is `checkout`, `build` or `none`; `why` says why it is dirty (None when it is clean); `stale` that an
    installed core's source differs from its build's. Only `entry()` is recorded, so the same code installed two ways
    records one entry.
    """

    commit: str
    tree: str | None
    dirty: bool
    python_sha256: str
    source: str
    why: str | None = None
    stale: bool = False

    @property
    def known(self) -> bool:
        """Whether the commit is known."""
        return is_sha(self.commit)

    def entry(self) -> dict:
        """What the environment records of the core: the same for one commit however it was installed."""
        return {
            "kind": KIND,
            "commit": self.commit,
            "tree": self.tree,
            "dirty": self.dirty,
            "python_sha256": self.python_sha256,
        }


def is_sha(value: object) -> bool:
    """Whether a value is a full git sha: forty lowercase hexadecimal characters."""
    return isinstance(value, str) and len(value) == _SHA_CHARS and not set(value) - _HEX


def _git(root: Path, *args: str, raw: bool = False) -> str | None:
    """Run git in `root`; None when git cannot run or fails. Stripped unless `raw` (NUL-separated listings)."""
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout if raw else done.stdout.strip()


def _digest(files: list[tuple[str, Path]]) -> str:
    """The digest over (name, sha256 of content) of each file, in name order; a file that cannot be read counts as
    its name with no content, so a digest always comes out and a vanished file still changes it."""
    digest = hashlib.sha256()
    for name, path in sorted(files):
        try:
            content = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            content = b""
        digest.update(name.encode() + b"\0" + content)
    return digest.hexdigest()


def _counted(name: str) -> bool:
    """Whether a file, by its name below the directory holding the packages, is part of `python_sha256`."""
    return name.endswith(".py") and name.split("/", 1)[0] in PACKAGES and not name.startswith(EXAMPLES)


def package_files(parent: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """The Python files of the packages below `parent` (a site directory, or a checkout's `src/`), each by its name
    below `parent`, and the links among them, files or directories, by the same names.

    A linked directory is walked like any other, under its own name, since its code is imported under that name; a
    directory is left only when it is one of the directories on its own path (a link back up the tree), so a cycle ends
    and every other path is hashed, however many lead to one directory. The example study under `expcore/examples/` is
    not walked: it is no part of the digest, and a link inside it cannot stand in for a package.
    """
    files: list[tuple[str, Path]] = []
    links: list[str] = []

    def walk(directory: Path, name: str, ancestors: frozenset[Path]) -> None:
        real = directory.resolve()
        if real in ancestors:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            child, path = f"{name}/{entry.name}", Path(entry.path)
            if entry.is_symlink():
                links.append(child)
            if entry.is_dir():  # a link to a directory too
                if entry.name not in _SKIPPED_DIRS and not f"{child}/".startswith(EXAMPLES):
                    walk(path, child, ancestors | {real})
            elif _counted(child) and entry.is_file():
                files.append((child, path))

    for package in PACKAGES:
        if (parent / package).is_dir():
            walk(parent / package, package, frozenset())
    return files, sorted(links)


def installed_digest(parent: Path) -> str:
    """`python_sha256` of the packages below `parent`, as they are imported: by walking them, links followed."""
    return _digest(package_files(parent)[0])


def checkout_digest(root: Path) -> str | None:
    """`python_sha256` of what a wheel built from a checkout ships: its packages walked, without the files git ignores
    there, which a build leaves out; None when git cannot list them."""
    ignored = _ignored(root)
    if ignored is None:
        return None
    return _digest([(name, path) for name, path in package_files(root / "src")[0] if f"src/{name}" not in ignored])


def _tracked_links(root: Path) -> set[str] | None:
    """The links git tracks in a checkout's packages (mode 120000), by path from its top; None when git cannot say."""
    listing = _git(root, "ls-files", "-z", "-s", "--", *(f"src/{p}" for p in PACKAGES), raw=True)
    if listing is None:
        return None
    rows = (item.split("\t", 1) for item in listing.split("\0") if "\t" in item)
    return {path for stage, path in rows if stage.startswith("120000 ")}


def _ignored(root: Path) -> set[str] | None:
    """The files git ignores in a checkout's packages, by path from its top; None when git cannot list them."""
    listing = _git(root, "ls-files", "-z", "-oi", "--exclude-standard", "--", *(f"src/{p}" for p in PACKAGES), raw=True)
    return None if listing is None else {item for item in listing.split("\0") if item}


def builds_from(path: str) -> bool:
    """Whether a file, by its path from the top of a checkout, is one a build of the core reads or an editable core
    imports: below `BUILD_DIRS`, or one of `BUILD_FILES` (the project, the build hook, the readme the metadata holds,
    the ignore rules that decide what a wheel ships, the token hashes it carries)."""
    return path.startswith(BUILD_DIRS) or path in BUILD_FILES


def _dirt(root: Path) -> str | None:
    """Why a checkout is dirty, else None: a file a build reads differs from HEAD, or is new and git does not ignore
    it; a Python file git ignores lies in the packages; the packages hold a link (git holds a link's target path,
    never what it leads to); or git cannot say."""
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", raw=True)
    ignored = _ignored(root)
    if status is None or ignored is None:
        return "git status could not be read"
    entries = [item for item in status.split("\0") if item]
    changed, untracked, index = [], [], 0
    while index < len(entries):
        code, paths = entries[index][:2], [entries[index][3:]]
        if "R" in code or "C" in code:  # --porcelain -z writes a rename's or a copy's source path as the next field
            index += 1
            paths += entries[index : index + 1]
        index += 1
        if any(builds_from(path) for path in paths):
            (untracked if code == "??" else changed).append(paths[0])
    if changed:
        more = f" and {len(changed) - 3} more" if len(changed) > 3 else ""
        return f"uncommitted changes to what a build reads: {', '.join(changed[:3])}{more}"
    if untracked:
        more = f" and {len(untracked) - 3} more" if len(untracked) > 3 else ""
        return f"files a build would ship that git does not track: {', '.join(untracked[:3])}{more}"
    hidden = sorted(item for item in ignored if _counted(item.removeprefix("src/")))
    if hidden:
        more = f" and {len(hidden) - 3} more" if len(hidden) > 3 else ""
        return f"Python files in the packages that git ignores: {', '.join(hidden[:3])}{more}"
    links = sorted(
        {f"src/{name}" for name in package_files(root / "src")[1]}  # walked: what the packages import through
        | (_tracked_links(root) or set())  # held by git, under the example's place too
        | {item for item in ignored if (root / item).is_symlink()}  # a link git ignores imports like any other
    )
    if links:
        more = f" and {len(links) - 3} more" if len(links) > 3 else ""
        return f"links in the packages, whose targets no commit holds: {', '.join(links[:3])}{more}"
    return None


def is_core_project(root: Path) -> bool:
    """Whether `root` holds the core's own `pyproject.toml`."""
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
    except (OSError, ValueError):
        return False
    return isinstance(project, dict) and project.get("name") == CORE_PROJECT


def is_checkout(root: Path) -> bool:
    """Whether `root` is the core's project at the top of its own git work tree (never a repository around it)."""
    if not is_core_project(root):
        return False
    top = _git(root, "rev-parse", "--show-toplevel")
    try:
        return top is not None and Path(top).resolve() == root.resolve()
    except OSError:
        return False


def from_checkout(root: Path, *, shipped: bool = False) -> CoreIdentity:
    """The identity of the core's checkout at `root`, read from git now: its digest of what is imported, or with
    `shipped` of what a wheel built from it ships (`checkout_digest`), as the build hook records it."""
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    digest = checkout_digest(root) if shipped else installed_digest(root / "src")
    if not is_sha(head) or not is_sha(tree) or digest is None:
        why = "the checkout's commit cannot be read from git"
        return CoreIdentity(UNKNOWN, None, True, digest or installed_digest(root / "src"), "none", why)
    why = _dirt(root)
    return CoreIdentity(str(head), str(tree), why is not None, digest, "checkout", why)


def build_identity(root: Path) -> dict:
    """What the build hook writes into a wheel and an sdist built from the project at `root` (`BUILD_FILE`).

    From git when `root` is a checkout; else the file an sdist carries (`src/expcore/<BUILD_FILE>`), as it is; else an
    unknown, dirty identity. Deterministic: no time and no path, so one tree always builds the same bytes.
    """
    if is_checkout(root):
        found = from_checkout(root, shipped=True)
        return _build_data(found.commit, found.tree, found.dirty, found.python_sha256)
    carried = read_build_file(root / "src" / "expcore" / BUILD_FILE)
    if carried is not None:
        return carried
    return _build_data(UNKNOWN, None, True, installed_digest(root / "src"))


def _build_data(commit: str, tree: str | None, dirty: bool, digest: str) -> dict:
    return {"format": BUILD_FORMAT, "commit": commit, "tree": tree, "dirty": dirty, "python_sha256": digest}


def build_bytes(data: dict) -> bytes:
    """The bytes of `BUILD_FILE`: sorted keys, one key per line, a final newline."""
    return (json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8")


def read_build_file(path: Path) -> dict | None:
    """The build identity in `path`; None when it is absent or not one (an anonymised package's is withheld)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != BUILD_FORMAT:
        return None
    commit, tree, dirty, digest = data.get("commit"), data.get("tree"), data.get("dirty"), data.get("python_sha256")
    known = is_sha(commit) and is_sha(tree)
    unknown = commit == UNKNOWN and tree is None
    if not (known or unknown) or not isinstance(dirty, bool) or not isinstance(digest, str):
        return None
    return _build_data(str(commit), tree, dirty, digest)


def from_build(package: Path) -> CoreIdentity | None:
    """The identity an installed core's build recorded beside it, checked against its source now; None without one."""
    data = read_build_file(package / BUILD_FILE)
    if data is None:
        return None
    digest = installed_digest(package.parent)
    stale = digest != data["python_sha256"]
    if data["commit"] == UNKNOWN:
        why = "it was built without git and without an sdist's build identity"
        return CoreIdentity(UNKNOWN, None, True, digest, "none", why, stale)
    if stale:
        why = f"its Python source differs from the build of {data['commit'][:12]} that names it"
    elif data["dirty"]:
        why = f"it was built from a checkout of {data['commit'][:12]} with uncommitted changes"
    else:
        why = None
    return CoreIdentity(data["commit"], data["tree"], data["dirty"] or stale, digest, "build", why, stale)


def identity_of(package: Path) -> CoreIdentity:
    """The identity of the core whose `expcore` package directory is `package`; see the module docstring."""
    root = package.parent.parent
    if package.name == "expcore" and package.parent.name == "src" and is_checkout(root):
        return from_checkout(root)
    built = from_build(package)
    if built is not None:
        return built
    why = f"no git checkout of the core and no {BUILD_FILE} beside it"
    return CoreIdentity(UNKNOWN, None, True, installed_digest(package.parent), "none", why)


def current() -> CoreIdentity:
    """The identity of the core this process imported. Read anew on each call: a checkout can change."""
    return identity_of(Path(__file__).resolve().parent.parent)
