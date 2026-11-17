# Provenance: new
"""The root half of a copy out of the sandbox: a standalone script, run as `sudo PYTHON -I -B -c SOURCE OP` (O19).

`copyout.py` passes this file's source to the interpreter it runs under, so the script imports nothing but the standard
library and nothing of the package. Its request comes on stdin as JSON; it never takes a path of the agent's tree as
an argument. Every name is opened from its parent directory's descriptor with `O_NOFOLLOW` (`openat`), so no link is
ever followed, on the way to an output or in it, and nothing is resolved by path below the root the harness names.

`list`: the tree under `root`, pre-order, one record per entry, `kind links device:inode size depth name` ended by
NUL, as `find -printf '%y %n %D:%i %s %d %f\\0'` writes it; it does not descend into a directory of another file system,
nor into one whose path from the root is already longer than `max_path_bytes` (everything below it would be refused).
An entry the agent locked (it owns everything under `/work` and can clear the owner's bits of a file or directory) is
not left to fail the run's collection, when the request sets `restore_locked` (only the collection of the agent's
`/work` does: the copy of a file of the harness's own never restores anything). Where the helper's own uid owns a
file or directory that lacks the owner's read bit (a directory also its search bit) it gives that bit back, nothing
else, through a descriptor of the entry opened without following a link and checked against what was listed, on the
listed file system only, and never to a file with another link (a mode belongs to the inode, which another name may
hold outside the listed tree): such a locked file is listed as kind `x` on every backend, root included, so that Docker
and Apptainer give the same outcome. An entry the helper cannot read, its root included, is listed as kind `x` and not
descended into: the caller refuses it as the agent's own unreadable output (O71). The copy then finds every listed
object readable. As root, the helper otherwise reads whatever the modes say and changes none.

`copy`: one output, from `root` through the names `parts`, to the new path `dest`. Every directory on the way and every
entry copied must be the object the listing showed: its device, inode, kind and size are compared with `ancestors`
and `expect` (counted, so two names of one file are two entries), and a file is read up to its listed size only. The
copy is written through descriptors too, each entry created anew (`O_EXCL`), given to `uid:gid` with `fchown` and
mode 0644 or 0755. Anything else (a link, a special file, an object the listing did not show, one missing) stops it
with exit code 3; an OS error with 1. The caller removes what was written either way.
"""

import json
import os
import resource
import stat
import sys

NO_FOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
O_PATH = getattr(os, "O_PATH", 0o10000000)  # Linux's, for a Python that does not name it
CHUNK = 1 << 20
UNREADABLE = "x"  # a record's kind for an entry the helper could not read, listed and never descended into
OWNER_BITS = {stat.S_IFDIR: 0o500, stat.S_IFREG: 0o400}  # what the owner needs to search a directory, to read a file
KINDS = {
    stat.S_IFREG: "f",
    stat.S_IFDIR: "d",
    stat.S_IFLNK: "l",
    stat.S_IFIFO: "p",
    stat.S_IFSOCK: "s",
    stat.S_IFBLK: "b",
    stat.S_IFCHR: "c",
}


class Mismatch(Exception):
    """The tree is not what the listing showed."""


def kind_of(mode):
    return KINDS.get(stat.S_IFMT(mode), "U")


def more_descriptors():
    """One descriptor per level of a deep tree: take what the hard limit allows, up to a bound."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 65536 if hard == resource.RLIM_INFINITY else min(hard, 65536)
    if soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))


def listing(request, out):
    path = request["root"]
    restoring = bool(request.get("restore_locked"))
    first = os.stat(path, follow_symlinks=False)
    name = os.fsencode(os.path.basename(path.rstrip("/")) or "/")
    limit = int(request["max_path_bytes"])
    if not readable(None, path, first, first.st_dev, restoring):
        write(out, first, 0, name, UNREADABLE)
        return
    try:
        root = os.open(path, NO_FOLLOW_DIR)
    except PermissionError:  # not the helper's to restore: the agent's own unreadable /work, alone
        write(out, first, 0, name, UNREADABLE)
        return
    info = os.fstat(root)
    write(out, info, 0, name)
    walk(root, info.st_dev, 1, 0, limit, out, restoring)


def write(out, info, depth, name, kind=None):
    head = f"{kind or kind_of(info.st_mode)} {info.st_nlink} {info.st_dev}:{info.st_ino} {info.st_size} {depth} "
    out.write(head.encode() + name + b"\0")


def readable(directory, name, info, device, restoring):
    """Whether the helper can read the file or directory `name` of `directory` (the root's path, `directory` None),
    which `info` describes. With `restoring` (the agent's own `/work`), a file the agent locked (no owner read bit) that
    has another link is unreadable here whoever runs the helper, root included: a mode change belongs to the inode,
    which another name may hold outside the tree, so it is never given back, and root's reading it is not a reason to
    treat it differently from the unprivileged helper (the same outcome on every backend). An entry the helper's own
    uid owns that lacks the owner's read bit (a directory's search bit too) otherwise first gets it back (`restore`).
    Anything but a file or a directory, an entry of another file system and every other entry as root are not this
    function's to judge: True. An entry of another uid is not the helper's to change: a file is tried; a directory is
    judged by the open that follows (one that opens but cannot be searched is not covered: no agent can make an entry
    of another uid)."""
    bits = OWNER_BITS.get(stat.S_IFMT(info.st_mode))
    if bits is None or info.st_dev != device:
        return True
    if restoring and stat.S_ISREG(info.st_mode) and info.st_nlink > 1 and not info.st_mode & 0o400:
        return False
    if os.geteuid() == 0:
        return True
    if info.st_uid != os.geteuid():
        return stat.S_ISDIR(info.st_mode) or opens(directory, name)
    return info.st_mode & bits == bits or (restoring and restore(directory, name, info, bits))


def opens(directory, name):
    try:
        os.close(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory))
    except PermissionError:
        return False
    return True


def restore(directory, name, info, bits):
    """Give the owner `bits` of the file or directory `name` that `info` described when it was listed. The entry is
    pinned by a descriptor opened without following a link, shown to be the object listed (device, inode and kind),
    and changed through that descriptor, never by its name: a name swapped for a link since the listing is not
    followed, and the link's target is not touched. Whether the bits were given; any error is a refusal."""
    try:
        held = os.open(name, O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    except OSError:
        return False
    try:
        now = os.fstat(held)
        if (now.st_dev, now.st_ino, stat.S_IFMT(now.st_mode)) != (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)):
            return False
        os.chmod(f"/proc/self/fd/{held}", stat.S_IMODE(now.st_mode) | bits)
    except OSError:
        return False
    finally:
        os.close(held)
    return True


def walk(directory, device, depth, length, limit, out, restoring=False):
    """List a directory's entries, sorted by name, descending into each directory of the same device."""
    for name in sorted(os.listdir(directory), key=os.fsencode):
        raw = os.fsencode(name)
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        path = length + bool(length) + len(raw)
        descend = stat.S_ISDIR(info.st_mode) and info.st_dev == device and path <= limit
        refused = (descend or stat.S_ISREG(info.st_mode)) and not readable(directory, name, info, device, restoring)
        child = None
        if descend and not refused:
            try:
                child = os.open(name, NO_FOLLOW_DIR, dir_fd=directory)
            except PermissionError:
                refused = True
        write(out, info, depth, raw, UNREADABLE if refused else None)
        if child is not None:
            try:
                if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (info.st_dev, info.st_ino):
                    raise Mismatch("a directory changed while it was listed")
                walk(child, device, depth + 1, path, limit, out, restoring)
            finally:
                os.close(child)


def copy(request):
    expect = {}
    for device, inode, kind, size, count in request["expect"]:
        expect[(device, inode)] = [kind, size, count]
    ancestors = [tuple(a) for a in request["ancestors"]]
    ids = (int(request["uid"]), int(request["gid"]))
    parts = [os.fsencode(p) for p in request["parts"]]
    dest = os.fsencode(request["dest"])
    parent = os.open(os.path.dirname(dest), NO_FOLLOW_DIR)
    directory = os.open(request["root"], NO_FOLLOW_DIR)
    for depth, part in enumerate(parts[:-1]):
        same(directory, ancestors[depth])
        child = os.open(part, NO_FOLLOW_DIR, dir_fd=directory)
        os.close(directory)
        directory = child
    if parts:
        same(directory, ancestors[len(parts) - 1])
        entry(directory, parts[-1], parent, os.path.basename(dest), expect, ids)
    else:  # the output is the root itself
        copy_directory(directory, os.fstat(directory), parent, os.path.basename(dest), expect, ids)
    if any(value[2] for value in expect.values()):
        raise Mismatch("the tree holds fewer objects than the listing showed")


def same(directory, wanted):
    info = os.fstat(directory)
    if (info.st_dev, info.st_ino) != tuple(wanted):
        raise Mismatch("a directory on the way to the output is not the one listed")


def expected(info, expect):
    """The listing's record of an object, counted off once: refuse one the listing did not show as it is now."""
    found = expect.get((info.st_dev, info.st_ino))
    if found is None or found[0] != kind_of(info.st_mode) or found[2] < 1:
        raise Mismatch("an object of the tree is not one the listing showed")
    if found[0] == "f" and found[1] != info.st_size:
        raise Mismatch("a file's size is not the size listed")
    found[2] -= 1
    return found


def entry(src_dir, name, dst_dir, dst_name, expect, ids):
    """Copy one entry and, for a directory, all it holds; every object opened without following a link."""
    lstat = os.stat(name, dir_fd=src_dir, follow_symlinks=False)
    if stat.S_ISREG(lstat.st_mode):
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_dir)
        try:
            copy_file(source, lstat, dst_dir, dst_name, expect, ids)
        finally:
            os.close(source)
    elif stat.S_ISDIR(lstat.st_mode):
        source = os.open(name, NO_FOLLOW_DIR, dir_fd=src_dir)
        try:
            copy_directory(source, lstat, dst_dir, dst_name, expect, ids)
        finally:
            os.close(source)
    else:
        raise Mismatch("an object of the tree is neither a regular file nor a directory")


def opened_as_listed(source, lstat, expect):
    info = os.fstat(source)
    if (info.st_dev, info.st_ino) != (lstat.st_dev, lstat.st_ino) or kind_of(info.st_mode) != kind_of(lstat.st_mode):
        raise Mismatch("an object changed while it was opened")
    return expected(info, expect)


def copy_file(source, lstat, dst_dir, dst_name, expect, ids):
    size = opened_as_listed(source, lstat, expect)[1]
    target = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_dir)
    try:
        left = size
        while left > 0:
            block = os.read(source, min(CHUNK, left))
            if not block:
                raise Mismatch("a file is shorter than listed")
            os.write(target, block)
            left -= len(block)
        if os.read(source, 1):
            raise Mismatch("a file is longer than listed")
        os.fchown(target, *ids)
        os.fchmod(target, 0o644)
    finally:
        os.close(target)


def copy_directory(source, lstat, dst_dir, dst_name, expect, ids):
    opened_as_listed(source, lstat, expect)
    os.mkdir(dst_name, 0o700, dir_fd=dst_dir)
    target = os.open(dst_name, NO_FOLLOW_DIR, dir_fd=dst_dir)
    try:
        os.fchown(target, *ids)
        os.fchmod(target, 0o755)
        for child in sorted(os.listdir(source), key=os.fsencode):
            entry(source, child, target, child, expect, ids)
    finally:
        os.close(target)


def main():
    operation = sys.argv[-1]
    request = json.loads(sys.stdin.read())
    sys.setrecursionlimit(20000)
    more_descriptors()
    try:
        if operation == "list":
            listing(request, sys.stdout.buffer)
        elif operation == "copy":
            copy(request)
        else:
            raise Mismatch("an unknown operation")
    except Mismatch as mismatch:
        sys.stderr.write(f"not as listed: {mismatch}\n")
        sys.exit(3)
    except OSError as error:
        sys.stderr.write(f"{type(error).__name__} ({error.errno})\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
