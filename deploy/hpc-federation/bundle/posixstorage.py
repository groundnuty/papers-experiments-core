"""posixstorage.py: a provider's posix storage on the cluster's shared scratch filesystem (O57). Run with python3.11 -I,
stdlib only.

With `HPC_STORAGE_ROOT` set in site.env, `hpcfed.py up` makes each provider a directory of its own on scratch
(`<root>/hpcop-<project>-<8 hex>/<provider>`, 0700, holding the owner marker `down` proves it by) and in it, beside the
marker, the directory the provider stores in (`.../<provider>/posix`, 0700, empty), and gives that one to the provider's
job as `--storage=PATH`. This script is what the node does with it:

  posixstorage.py prepare --workdir W --provider NAME --storage=PATH
      configure.sh's step, at the first start. Check the directory and the rootfs's mountpoint (below), then write the
      record `W/posix-storage` (0600, one `NAME<TAB>PATH` line). Nothing is written when either is refused.
  posixstorage.py binds --workdir W --storage=PATH
      run_service's step, at every start: the first (provider.sbatch) and every start again (restart.sh, which
      `hpcfed.py start` gives the same argument, read from the federation's state). The record must name exactly the
      PATH this start was given, so a record edited or put in place since cannot redirect the bind, and one that is
      missing refuses the start rather than leave the provider on tmpfs. Check the directory and the mountpoint again
      and print the arguments for `apptainer exec`, one per line: `--bind` and `PATH:/volumes/posix:rw`. A start given
      no storage binds nothing (lib.sh's storage_binds, which also refuses a work directory that records one): the
      provider then stores in its rootfs's own /volumes/posix, on the node's tmpfs, as without the key.

The directory is accepted only as a plain absolute path of characters that cannot end a bind option, with no symbolic
link on the way (a link could be re-pointed between the check and the bind), of the form `up` makes
(`.../hpcop-<project>-<8 hex>/<NAME>/posix`: this provider's, below an allocation's directory), the bound directory, its
provider's and its allocation's each a directory of this user with mode 0700 that this user can write (the provider
runs as this user in its cell, so it needs no other), its provider's directory holding `up`'s owner marker for NAME and
that allocation (a regular file of this user, never read through a link), and neither inside nor holding the work
directory. At the first start it must also be empty and not on node-local memory (tmpfs, ramfs: the bind is there to
keep the data off the node). The rootfs's /volumes/posix, which the bind covers, must be a real directory that holds
nothing.

The marker stays beside the bound directory, so the provider never sees it. The cell sees its storage at the path it
always did, so finish.sh declares the posix storage to Onedata as before. A refusal names what was refused and never the
path. The script reads the directory and writes only the record, in the work directory: nothing is removed (`down`
removes the storage from the login node once the provider is gone, and the bind lives only in the container's mount
namespace, so removing the work directory never reaches it).
"""

import importlib.util
import json
import os
import re
import secrets
import stat
import sys

MOUNT = "/volumes/posix"  # where the provider's posix storage is in its cell: finish.sh declares it there
RECORD = "posix-storage"  # in the work directory: NAME<TAB>PATH
BOUND = "posix"  # the bound directory's name in its provider's directory (hpcstorage.BOUND)
OWNER = ".hpcop-storage-owner"  # up's marker in the provider's directory (hpcstorage.OWNER)
ALLOCATION = re.compile(r"hpcop-[a-z0-9][a-z0-9_-]{0,39}-[0-9a-f]{8}")  # the allocation's name (hpcstorage)
PROVIDER = re.compile(r"[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?")  # a provider's name (hpcstorage.PROVIDER_NAME)
TOKEN = re.compile(r"[0-9a-f]{32}")
OWNER_BYTES = 4096  # the most of the marker that is read
PREFIX = "--storage="


def _sibling():
    """importstorage.py of this bundle, loaded by its path (`-I` keeps the script's directory off sys.path): its mount
    table, its check of a provider's work directory and its path form are this script's too."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "importstorage.py")
    spec = importlib.util.spec_from_file_location("hpcop_importstorage_of_posixstorage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


imp = _sibling()


class StorageRefused(Exception):
    """A storage, an argument or a work directory this script will not use; the message names no path."""


def parse_arg(arg):
    """`--storage=PATH` as its path, or refused."""
    if not arg.startswith(PREFIX):
        raise StorageRefused("the storage is written --storage=PATH")
    path = arg[len(PREFIX) :]
    if not imp.PATH.fullmatch(path):
        raise StorageRefused("the storage's path is an absolute path of A-Z a-z 0-9 . _ + / - alone, of at most 4096")
    return path


def _no_link_on_the_way(path):
    """Every step of the path, from the first below / to the path itself, each lstat'ed: refused at a link."""
    parts = path.split("/")[1:]
    for depth in range(1, len(parts) + 1):
        step = "/" + "/".join(parts[:depth])
        try:
            mode = os.lstat(step).st_mode
        except FileNotFoundError:
            raise StorageRefused("the storage does not exist") from None
        except OSError as error:
            raise StorageRefused(f"the storage cannot be read ({type(error).__name__})") from None
        if stat.S_ISLNK(mode):
            raise StorageRefused("the storage passes through a symbolic link: name its real path")


def _private(path, what):
    """A directory of this user, mode 0700, that this user can write. The permission bits alone are compared: a setgid
    bit, which a project's scratch passes down to every directory made in it, is no access for anyone."""
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o777 != 0o700
        or not os.access(path, os.R_OK | os.W_OK | os.X_OK)
    ):
        raise StorageRefused(f"{what} is not a directory of this user with mode 0700 that this user can write")


def _marker_of(directory):
    """The owner marker in a directory, as a dict: a regular file of this user, opened without following a link and
    read whole up to OWNER_BYTES; None when there is none or it is anything else."""
    try:
        fd = os.open(os.path.join(directory, OWNER), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        text = os.read(fd, OWNER_BYTES)
    finally:
        os.close(fd)
    try:
        held = json.loads(text.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return held if isinstance(held, dict) else None


def check_storage(path, provider, workdir, mountinfo=None, first=False):
    """Refuse a directory that is not the one up made for this provider; `mountinfo`, the mount table's text, and
    `first` (emptiness) only at the first start. Nothing is created or changed."""
    if not path.startswith("/") or os.path.normpath(path) != path or path == "/":
        raise StorageRefused("the storage's path is not a plain absolute path: no '.', '..' or '//', and not '/'")
    parts = path.split("/")
    if len(parts) < 5 or parts[-1] != BOUND or parts[-2] != provider or not ALLOCATION.fullmatch(parts[-3]):
        raise StorageRefused(
            "the storage is not of the form up makes: <its allocation's directory>/<this provider>/posix"
        )
    _no_link_on_the_way(path)
    directory, allocation = os.path.dirname(path), os.path.dirname(os.path.dirname(path))
    _private(path, "the storage")
    _private(directory, "its provider's directory")
    _private(allocation, "its allocation's directory")
    held = _marker_of(directory)
    if (
        held is None
        or held.get("provider") != provider
        or held.get("allocation") != parts[-3]
        or not isinstance(held.get("token"), str)
        or not TOKEN.fullmatch(held["token"])
    ):
        raise StorageRefused(
            "the storage's provider directory holds no owner marker of up for this provider and allocation: it is not "
            "a directory up made for this provider"
        )
    work = os.path.realpath(workdir)
    if path == work or path.startswith(work + "/") or work.startswith(path + "/"):
        raise StorageRefused("the storage is inside or holds this service's work directory")
    if first:
        if os.listdir(path):
            raise StorageRefused("the storage holds files before the provider's first start: it is not the one up made")
        if imp.filesystem_of(path, imp.parse_mountinfo(mountinfo or "")) in imp.LOCAL_FS:
            raise StorageRefused("the storage is on node-local memory, which the bind exists to keep the data off")


def check_mountpoint(workdir):
    """The rootfs's /volumes/posix, which the bind covers: a real directory at each step, holding nothing (a bind
    would hide what it holds, and the provider must never have stored there)."""
    walked = os.path.join(workdir, "rootfs")
    for part in MOUNT.split("/")[1:]:
        walked = os.path.join(walked, part)
        try:
            mode = os.lstat(walked).st_mode
        except FileNotFoundError:
            raise StorageRefused("the rootfs has no /volumes/posix to bind the storage over") from None
        if not stat.S_ISDIR(mode):
            raise StorageRefused("the rootfs's /volumes/posix is a link or not a directory")
    if os.listdir(walked):
        raise StorageRefused("the rootfs's /volumes/posix holds files: the bind would hide them")


def private_workdir(workdir):
    """A provider's private work directory with its rootfs unpacked (importstorage's own check)."""
    try:
        imp.private_workdir(workdir)
    except imp.ImportRefused as refusal:
        raise StorageRefused(str(refusal)) from None


def write_record(workdir, provider, path):
    """The record, by a draft made exclusively and never through a link, then put in place whole."""
    target = os.path.join(workdir, RECORD)
    draft = "%s.draft.%d.%s" % (target, os.getpid(), secrets.token_hex(4))
    fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("%s\t%s\n" % (provider, path))
    os.replace(draft, target)


def prepare(workdir, provider, arg, mountinfo=None):
    """Check the storage (at its first start) and the mountpoint, then write the record; nothing when one is refused."""
    if not PROVIDER.fullmatch(provider):
        raise StorageRefused("--provider is not a provider's name")
    path = parse_arg(arg)
    try:
        table = imp.read_mounts() if mountinfo is None else mountinfo
    except imp.ImportRefused:
        raise StorageRefused(
            "the node's mount table cannot be read, so it cannot be told whether the storage is on node-local memory"
        ) from None
    check_storage(path, provider, workdir, table, first=True)
    private_workdir(workdir)
    check_mountpoint(workdir)
    write_record(workdir, provider, path)


def recorded(workdir):
    """The record as (provider, path), read as strictly as the arguments; None when there is none. It must be a regular
    file of this user that no one else can read, and is never opened through a link."""
    private_workdir(workdir)
    try:
        fd = os.open(os.path.join(workdir, RECORD), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise StorageRefused("the record of the storage cannot be opened (a link, or unreadable)") from None
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StorageRefused(
                "the record of the storage is not a regular file of this user, readable by no one else"
            )
        lines = handle.read().splitlines()
    provider, tab, path = (lines[0] if len(lines) == 1 else "").partition("\t")
    if not tab or not PROVIDER.fullmatch(provider):
        raise StorageRefused("the record of the storage is not one NAME<TAB>PATH line")
    return provider, parse_arg(PREFIX + path)


def binds(workdir, arg):
    """The `apptainer exec` arguments that bind the storage this start was given (`--storage=PATH`, from the
    federation's state) onto /volumes/posix, writable: only when the work directory records exactly that path, after
    checking it and the mountpoint again (with no mount table and no emptiness: the provider's data is in it after a
    first start). A missing record refuses: the provider is never started on the node's tmpfs instead."""
    expected = parse_arg(arg)
    found = recorded(workdir)
    if found is None:
        raise StorageRefused(
            "this start names a storage on scratch, but the work directory records none (configure.sh never prepared "
            "it): the provider is not started, and never on the node's tmpfs instead"
        )
    provider, path = found
    if path != expected:
        raise StorageRefused(
            "the work directory records another storage than the federation's state names for this start: the record "
            "is not the one configure.sh wrote for it"
        )
    check_storage(path, provider, workdir)
    check_mountpoint(workdir)
    return ["--bind", "%s:%s:rw" % (path, MOUNT)]


def options(argv):
    """`--workdir W`, `--provider NAME` and the rest, as (named, rest)."""
    named, rest, index = {}, [], 0
    while index < len(argv):
        if argv[index] in ("--workdir", "--provider"):
            if index + 1 >= len(argv):
                raise StorageRefused("%s needs a value" % argv[index])
            named[argv[index][2:]] = argv[index + 1]
            index += 2
        else:
            rest.append(argv[index])
            index += 1
    return named, rest


def main(argv):
    try:
        named, rest = options(argv[1:])
        verb = argv[0] if argv else ""
        if "workdir" not in named or verb not in ("prepare", "binds"):
            raise StorageRefused(
                "usage: posixstorage.py prepare --workdir W --provider NAME --storage=PATH | binds --workdir W "
                "--storage=PATH"
            )
        if verb == "prepare":
            if "provider" not in named or len(rest) != 1:
                raise StorageRefused("prepare takes --workdir, --provider and one --storage=PATH")
            prepare(named["workdir"], named["provider"], rest[0])
            print("ok: the provider's storage on scratch is prepared, to be bound onto %s" % MOUNT, file=sys.stderr)
            return 0
        if len(rest) != 1 or "provider" in named:
            raise StorageRefused("binds takes --workdir and one --storage=PATH")
        for argument in binds(named["workdir"], rest[0]):
            print(argument)
        return 0
    except StorageRefused as refusal:
        print("posixstorage: ERROR: %s" % refusal, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
