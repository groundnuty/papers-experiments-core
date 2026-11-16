"""The provider storage of an HPC federation on the cluster's shared scratch filesystem (DESIGN.md, section 11): what
`up` makes and records, and what `down` removes and shows gone.

With `HPC_STORAGE_ROOT` set, `up` makes below it one directory of its own, named for its allocation
(`<root>/hpcop-<project>-<8 hex>`, 0700), and in that one directory per provider (0700), each by an exclusive `mkdir`
that fails on anything already there, never adopting it. Each of them holds an owner marker (`.hpcop-storage-owner`,
0600) that names the allocation and a random token the state file also holds (a provider's also names the provider),
so each directory proves itself and the parent's marker is no proof of its children. The state file records the paths
before the first `mkdir`, so a crash after it leaves enough for `down`. The providers use the directories as their
storage once the bundle binds them (not in this release: the driver makes, records and removes them, and guards their
inode quota).

`down` removes them only once the held job and every service are gone, and removes exactly what `up` made:

- the recorded record must be, character for character, the form `up` writes: the recorded scratch root (the site's
  own), the allocation's own name below it, a directory of each provider of the layout below that (`checked`);
- the storage's directory and each provider's are opened without following a link, held open, and proved (this user's,
  holding this up's marker for it), all before any removal, so a bad one stops the whole removal;
- each provider's directory is emptied through its descriptor (`hpcremove.remove_tree` on `/proc/self/fd/<n>`, which
  never follows a link), then retired (its marker, then the directory by `rmdir` through its parent's descriptor), then
  the storage's own directory the same way: a path swapped for a link or another directory after the proof is never
  followed, and anything in the storage's directory `up` did not make is kept, with the marker, and fails `down`;
- what remains of the recorded paths is then counted, and any is a failure, naming the count.

Nothing is removed for a path whose proof is missing: a directory already at the path when `up` ran (it refused it), a
marker that is not this up's, a link in a directory's place. `down` says so and keeps the root and the state. A
concurrent change inside the 0700 tree by this same user is not defended against.
"""

import contextlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from hpcremove import remove_tree
from hpcsettings import Site

OWNER = ".hpcop-storage-owner"  # in the storage's own directory and in each provider's: whose, and this up's token
PARENT_NAME = re.compile(r"hpcop-[a-z0-9][a-z0-9_-]{0,39}-[0-9a-f]{8}")  # the allocation's name (initial_state)
PROVIDER_NAME = re.compile(r"[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?")  # a Compose service name (fedspec.SERVICE_NAME)
TOKEN = re.compile(r"[0-9a-f]{32}")
OWNER_BYTES = 4096  # the most of the marker that is read


class StorageRefused(RuntimeError):
    """The storage cannot be made, or what is recorded cannot be removed as up's own; names why, never a path."""


def plan(site: Site, allocation: str, providers: Sequence[str]) -> Optional[Dict[str, Any]]:
    """The state's `storage` record, written before anything is made; None without a scratch root."""
    if not site.storage_root:
        return None
    root = os.path.normpath(site.storage_root)
    parent = f"{root}/{allocation}"
    return {
        "root": root,
        "parent": parent,
        "token": secrets.token_hex(16),
        "providers": {name: f"{parent}/{name}" for name in providers},
    }


def check_root(site: Site, federation_root: Path) -> None:
    """The scratch root, in the preflight: a directory this user can write, and neither the federation's root nor inside
    it, which `down` empties. A shared tree is the driver's (`hpcimport.check_separate`, with this root among its
    places). Names the variable, never its value."""
    if not site.storage_root:
        return
    where = Path(site.storage_root)
    if not where.is_dir():
        raise StorageRefused("HPC_STORAGE_ROOT is not a directory")
    if not os.access(where, os.W_OK | os.X_OK):
        raise StorageRefused("HPC_STORAGE_ROOT is not a directory this user can write")
    real, root = Path(os.path.realpath(where)), Path(os.path.realpath(federation_root))
    if real == root or root in real.parents:
        raise StorageRefused("HPC_STORAGE_ROOT is, or lies inside, the federation's root, which down empties")


def _marker(record: Mapping[str, Any], provider: Optional[str] = None) -> bytes:
    """What an owner marker holds: the allocation, this up's token, and for a provider's directory whose it is."""
    owner = {"allocation": os.path.basename(record["parent"]), "token": record["token"]}
    if provider is not None:
        owner["provider"] = provider
    return (json.dumps(owner, sort_keys=True) + "\n").encode("utf-8")


def _write_marker(content: bytes, *, path: Optional[str] = None, dir_fd: Optional[int] = None) -> None:
    """Make the marker (exclusively: never over anything) in a directory, by its path or by a descriptor held open."""
    target = OWNER if path is None else os.path.join(path, OWNER)
    descriptor = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd
    )
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fail(error: OSError) -> StorageRefused:
    return StorageRefused(f"the provider storage could not be made ({error.strerror})")


def _mark(directory: str, content: bytes) -> None:
    """The marker in a directory this call has just made: a failure (the quota this guards may run out right here)
    removes that empty directory by `rmdir`, so `down` is never left with a directory it cannot prove."""
    try:
        _write_marker(content, path=directory)
    except OSError as error:
        try:
            os.rmdir(directory)
        except OSError as again:
            raise StorageRefused(
                f"the provider storage could not be made ({error.strerror}), and the empty directory it left could not "
                f"be removed ({again.strerror}): remove it by hand"
            ) from None
        raise _fail(error) from None


def create(record: Mapping[str, Any]) -> None:
    """Make the storage the record names: the storage's own directory by an exclusive `mkdir` (anything already there
    refuses `up`, and is never adopted) with its owner marker, and each provider's directory with its own. OSError
    becomes a refusal that names the cause and never the path. What is made before a failure is the state's, and `down`
    removes it; a directory whose marker could not be written is removed at once (`_mark`)."""
    parent = record["parent"]
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        raise StorageRefused(
            "the provider storage's directory already exists: it was not made by this up, and is never adopted"
        ) from None
    except OSError as error:
        raise _fail(error) from None
    _mark(parent, _marker(record))
    for name, path in record["providers"].items():
        try:
            os.mkdir(path, 0o700)
        except OSError as error:
            raise _fail(error) from None
        _mark(path, _marker(record, name))


def checked(record: Any, state: Mapping[str, Any]) -> Dict[str, Any]:
    """The state's storage record, accepted only as the form `plan` writes it, over this state's own allocation, scratch
    root and providers; StorageRefused when it is anything else, before anything is touched."""

    def refused(why: str) -> StorageRefused:
        return StorageRefused(f"the recorded provider storage is not what up records ({why}): nothing was removed")

    if not isinstance(record, dict) or not isinstance(record.get("providers"), dict):
        raise refused("it is not a record")
    allocation = state["allocation"]["name"]
    site_root = state.get("site", {}).get("storage_root")
    root, parent, token = record.get("root"), record.get("parent"), record.get("token")
    if not isinstance(root, str) or not os.path.isabs(root) or os.path.normpath(root) != root or root == "/":
        raise refused("the scratch root")
    if not isinstance(site_root, str) or os.path.normpath(site_root) != root:
        raise refused("the scratch root is not the site's")
    if PARENT_NAME.fullmatch(allocation) is None or parent != f"{root}/{allocation}":
        raise refused("the storage's directory")
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
        raise refused("the owner token")
    wanted = {name for name, service in state["services"].items() if service["kind"] == "op"}
    found = record["providers"]
    if set(found) != wanted or any(PROVIDER_NAME.fullmatch(name) is None for name in found):
        raise refused("the providers")
    if any(path != f"{parent}/{name}" for name, path in found.items()):
        raise refused("a provider's directory")
    return {"root": root, "parent": parent, "token": token, "providers": dict(found)}


def _lstat(path: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def remaining(record: Mapping[str, Any]) -> int:
    """How many entries of the recorded storage are still there: its own directory and everything below it, counting a
    link as one entry and never following it; 0 once it is gone."""
    info = _lstat(record["parent"])
    if info is None:
        return 0
    if not stat.S_ISDIR(info.st_mode):
        return 1
    return 1 + sum(len(dirs) + len(files) for _, dirs, files in os.walk(record["parent"], followlinks=False))


def _open(name: str, what: str, *, dir_fd: Optional[int] = None, follow: bool = False) -> Optional[int]:
    """A directory held open, by `name` below `dir_fd`: a link at its last step is refused, never followed (`follow`
    only for the site's own scratch root). None when it is not there; StorageRefused when it is not a directory."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW)
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as error:  # a link (ELOOP), a file (ENOTDIR), a directory this user cannot open
        raise StorageRefused(f"{what} is not a directory up made ({error.strerror}): nothing was removed") from None


def _prove(descriptor: int, what: str, content: bytes, uid: int) -> None:
    """The directory held open is this user's and holds this up's marker for it (a regular file of this user, read
    through the descriptor, without following a link), else StorageRefused: it is not proof that `up` made it."""
    held = b""
    if os.fstat(descriptor).st_uid == uid:
        try:
            marker = os.open(OWNER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
        except OSError:
            marker = None
        if marker is not None:
            try:
                info = os.fstat(marker)
                held = os.read(marker, OWNER_BYTES) if stat.S_ISREG(info.st_mode) and info.st_uid == uid else b""
            finally:
                os.close(marker)
    if held != content:
        raise StorageRefused(
            f"{what} holds no owner marker of this up (a directory up refused to adopt, one put in place of up's, or a "
            "marker that is not its own): nothing was removed; look at it, and remove the state's record or the "
            "directory by hand"
        )


def _retire(directory: int, name: str, container: int, content: bytes, calls: Mapping[str, Any]) -> None:
    """The directory held open (`directory`, `name` below `container`) holds nothing but its marker: remove the marker,
    then the directory by `rmdir`, which removes only an empty one. A failed `rmdir` puts the marker back, so a second
    `down` can still prove the directory's own; the refusal says whether it could."""
    unlink, rmdir = calls.get("unlink", os.unlink), calls.get("rmdir", os.rmdir)
    unlink(OWNER, dir_fd=directory)
    try:
        rmdir(name, dir_fd=container)
    except OSError as error:
        kept = "its owner marker is kept"
        try:
            _write_marker(content, dir_fd=directory)
        except OSError as again:
            kept = f"its owner marker could not be put back ({again.strerror}): remove the empty directory by hand"
        raise StorageRefused(
            f"a directory of the provider storage could not be removed ({error.strerror}); {kept}"
        ) from None


def remove(record: Any, state: Mapping[str, Any], *, uid: Optional[int] = None, **calls: Any) -> str:
    """Remove the provider storage `up` recorded and made, and show that none of it remains; what `down` prints. The
    caller has ended every service and the held job. Every directory is opened without following a link, proved (this
    user's, holding this up's marker for it) before any is touched, and held open throughout: what is removed is the
    proved directory's contents through its descriptor (`remove_tree` on `/proc/self/fd/<n>`), and each directory is
    retired by its parent's descriptor, so a path swapped for a link or another directory after the proof is never
    followed. `calls` are the file calls of `hpcremove.remove_tree` (`unlink`, `rmdir`, `sleep`, `clock`), a stand-in's
    in the tests. StorageRefused for anything that is not provably up's own, or that remains; the root and the state are
    then kept. Concurrent changes inside the 0700 tree by this same user are not defended against."""
    record, uid = checked(record, state), os.getuid() if uid is None else uid
    parent_name, made = os.path.basename(record["parent"]), 0
    with contextlib.ExitStack() as held:
        root = _open(record["root"], "the scratch root", follow=True)
        parent = None
        if root is not None:
            held.callback(os.close, root)
            parent = _open(parent_name, "the provider storage's own directory", dir_fd=root)
        if parent is not None:
            held.callback(os.close, parent)
            _prove(parent, "the provider storage's own directory", _marker(record), uid)
            open_dirs = {}
            for name in record["providers"]:
                descriptor = _open(name, f"the storage of {name}", dir_fd=parent)
                if descriptor is None:
                    continue
                held.callback(os.close, descriptor)
                _prove(descriptor, f"the storage of {name}", _marker(record, name), uid)
                open_dirs[name] = descriptor
            for name, descriptor in open_dirs.items():
                directory = Path(f"/proc/self/fd/{descriptor}")
                remove_tree(directory, keep_root=True, marker_name=OWNER, **calls)
                if sorted(os.listdir(descriptor)) != [OWNER]:  # a removal that raised nothing and left entries
                    raise StorageRefused(
                        f"{remaining(record)} entries of the provider storage remain after its removal"
                    )
                _retire(descriptor, name, parent, _marker(record, name), calls)
            made = len(open_dirs)
            others = [name for name in os.listdir(parent) if name != OWNER]
            if others:
                raise StorageRefused(
                    f"{len(others)} entries of the provider storage's own directory were not made by up and are kept, "
                    "with its owner marker: remove them, then run down again"
                )
            _retire(parent, parent_name, root, _marker(record), calls)
    left = remaining(record)
    if left:
        raise StorageRefused(f"{left} entries of the provider storage remain after its removal")
    return f"provider storage: {made} directories removed, 0 entries remain"
