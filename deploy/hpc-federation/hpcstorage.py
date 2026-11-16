"""The provider storage of an HPC federation on the cluster's shared scratch filesystem (DESIGN.md, section 11): what
`up` makes and records, and what `down` removes and shows gone.

With `HPC_STORAGE_ROOT` set, `up` makes below it one directory of its own, named for its allocation
(`<root>/hpcop-<project>-<8 hex>`, 0700), and in that one directory per provider (0700), each by an exclusive `mkdir`
that fails on anything already there, never adopting it. The first holds an owner marker (`.hpcop-storage-owner`, 0600)
that names the allocation and a random token the state file also holds. The state file records the paths before the
first `mkdir`, so a crash after it leaves enough for `down`. The providers use the directories as their storage once
the bundle binds them (not in this release: the driver makes, records and removes them, and guards their inode quota).

`down` removes them only once the held job and every service are gone, and removes exactly what `up` made:

- the recorded record must be, character for character, the form `up` writes: the recorded scratch root (the site's
  own), the allocation's own name below it, a directory of each provider of the layout below that (`checked`);
- the storage's directory must be a real directory (never a link) of this user holding this up's marker, and each
  provider's path a real directory of this user, all checked before any removal, so a bad one stops the whole removal;
- each provider's directory is removed whole by `hpcremove.remove_tree`, which never follows a link, then the marker,
  then the storage's directory by `rmdir`: anything in it `up` did not make is kept, with the marker, and fails `down`;
- what remains of the recorded paths is then counted, and any is a failure, naming the count.

Nothing is removed for a path whose proof is missing: a directory already at the path when `up` ran (it refused it), a
marker that is not this up's, a link in a directory's place. `down` says so and keeps the root and the state.
"""

import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from hpcremove import remove_tree
from hpcsettings import Site

OWNER = ".hpcop-storage-owner"  # in the storage's own directory: its name and this up's token
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


def _marker(record: Mapping[str, Any]) -> bytes:
    owner = {"allocation": os.path.basename(record["parent"]), "token": record["token"]}
    return (json.dumps(owner, sort_keys=True) + "\n").encode("utf-8")


def _write_marker(record: Mapping[str, Any]) -> None:
    descriptor = os.open(
        os.path.join(record["parent"], OWNER), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        os.write(descriptor, _marker(record))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create(record: Mapping[str, Any]) -> None:
    """Make the storage the record names: the storage's own directory by an exclusive `mkdir` (anything already there
    refuses `up`, and is never adopted), its owner marker, and each provider's directory. OSError becomes a refusal that
    names the cause and never the path. A failure between the first `mkdir` and the marker (the quota this guards may
    run out right there) removes the empty directory this call just made, so `down` is never left with a directory it
    cannot prove; after the marker, what was made is the state's, and `down` removes it."""
    parent = record["parent"]
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        raise StorageRefused(
            "the provider storage's directory already exists: it was not made by this up, and is never adopted"
        ) from None
    except OSError as error:
        raise StorageRefused(f"the provider storage could not be made ({error.strerror})") from None
    try:
        _write_marker(record)
    except OSError as error:
        _unmake(parent)
        raise StorageRefused(f"the provider storage could not be made ({error.strerror})") from None
    try:
        for path in record["providers"].values():
            os.mkdir(path, 0o700)
    except OSError as error:
        raise StorageRefused(f"the provider storage could not be made ({error.strerror})") from None


def _unmake(parent: str) -> None:
    """Remove the directory `create` made a moment ago and wrote nothing in, by `rmdir`: only an empty one goes."""
    try:
        os.rmdir(parent)
    except OSError as error:
        raise StorageRefused(
            f"the provider storage's directory could not be made, and its empty remains could not be removed "
            f"({error.strerror}): remove it by hand"
        ) from None


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


def _made_by_us(info: Optional[os.stat_result], uid: int) -> bool:
    """A real directory (never a link) of this user."""
    return info is not None and stat.S_ISDIR(info.st_mode) and info.st_uid == uid


def _proven(record: Mapping[str, Any], uid: int) -> None:
    """The storage's own directory is a real directory of this user holding this up's marker, else StorageRefused."""
    if not _made_by_us(_lstat(record["parent"]), uid):
        raise StorageRefused("the provider storage's own directory is not a directory up made: nothing was removed")
    marker = os.path.join(record["parent"], OWNER)
    info = _lstat(marker)
    held = b""
    if info is not None and stat.S_ISREG(info.st_mode) and info.st_uid == uid:
        try:
            descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            descriptor = None
        if descriptor is not None:
            try:
                held = os.read(descriptor, OWNER_BYTES)
            finally:
                os.close(descriptor)
    if held != _marker(record):
        raise StorageRefused(
            "the provider storage's own directory holds no owner marker of this up (a directory up refused to adopt, "
            "or a marker that is not its own): nothing was removed; look at it, and remove the state's record or the "
            "directory by hand"
        )


def remaining(record: Mapping[str, Any]) -> int:
    """How many entries of the recorded storage are still there: its own directory and everything below it, counting a
    link as one entry and never following it; 0 once it is gone."""
    info = _lstat(record["parent"])
    if info is None:
        return 0
    if not stat.S_ISDIR(info.st_mode):
        return 1
    return 1 + sum(len(dirs) + len(files) for _, dirs, files in os.walk(record["parent"], followlinks=False))


def remove(record: Any, state: Mapping[str, Any], *, uid: Optional[int] = None, **calls: Any) -> str:
    """Remove the provider storage `up` recorded and made, and show that none of it remains; what `down` prints. `calls`
    are the file calls of `hpcremove.remove_tree` (`unlink`, `rmdir`, `sleep`, `clock`), a stand-in's in the tests. The
    caller has ended every service and the held job. StorageRefused for anything that is not provably up's own, or that
    remains: the root and the state are then kept."""
    record, uid = checked(record, state), os.getuid() if uid is None else uid
    made = 0
    if _lstat(record["parent"]) is not None:
        _proven(record, uid)
        present = []
        for name, path in record["providers"].items():
            info = _lstat(path)
            if info is None:
                continue
            if not _made_by_us(info, uid):
                raise StorageRefused(f"the storage of {name} is not a directory up made: nothing was removed")
            present.append(path)
        for path in present:
            remove_tree(Path(path), **calls)
        made = len(present)
        if any(_lstat(path) is not None for path in present):  # a removal that raised nothing and left its directory
            raise StorageRefused(f"{remaining(record)} entries of the provider storage remain after its removal")
        others = [name for name in os.listdir(record["parent"]) if name != OWNER]
        if others:
            raise StorageRefused(
                f"{len(others)} entries of the provider storage's own directory were not made by up and are kept, "
                "with its owner marker: remove them, then run down again"
            )
        _remove_own_directory(record, calls)
    left = remaining(record)
    if left:
        raise StorageRefused(f"{left} entries of the provider storage remain after its removal")
    return f"provider storage: {made} directories removed, 0 entries remain"


def _remove_own_directory(record: Mapping[str, Any], calls: Mapping[str, Any]) -> None:
    """The marker, then the storage's own directory by `rmdir`, which removes only an empty one. A failed `rmdir` puts
    the marker back, so a second `down` can still prove the directory's own; the failure says whether it could."""
    unlink, rmdir = calls.get("unlink", os.unlink), calls.get("rmdir", os.rmdir)
    unlink(os.path.join(record["parent"], OWNER))
    try:
        rmdir(record["parent"])
    except OSError as error:
        kept = "its owner marker is kept"
        try:
            _write_marker(record)
        except OSError as again:
            kept = f"its owner marker could not be put back ({again.strerror}): remove the empty directory by hand"
        raise StorageRefused(
            f"the provider storage's own directory could not be removed ({error.strerror}); {kept}"
        ) from None
