"""baked.py: find the Couchbase values the pinned images ship pre-initialised, by their SHA-256, never by value.

Both images carry the same Couchbase state (two cookies and the memcached _admin password). prep removes it and
check-defaults.sh checks that none of it is left or came back. The values are listed in lib.sh only as LENGTH:SHA256
(BAKED_CB_SHA256), so no password-like literal sits in the bundle, and this script compares hashes at run time.

  baked.py [--prune REL]... [--prune-each REL]... DIR LENGTH:SHA256...

DIR is walked without following links, and every regular file is read as bytes. The values are alphanumeric, so each
maximal run of [A-Za-z0-9] in a file is a place one could sit, at any offset within the run: every window of each
listed length in the run is hashed and compared. Binary files count as text does (Couchbase's config.dat is one).

--prune REL leaves the entry at REL below DIR out of the walk, and --prune-each REL every entry directly below REL: a
provider's storage (posix, import/*) holds the study's data, which the images never ship, and with a tmpfs storage it is
the provider's whole data and takes minutes to read in pure Python. REL is a plain relative path of DIR. Nothing else is
left out. A REL that is not there prunes nothing (a zone has no storage); one that is a link, or whose parent below DIR
is a link, or an entry under --prune-each that is a link, is an error, never pruned: what a link stands for is not
known to be what was meant. (Python 3.8 runs this on the images: no annotations, no new syntax.)

A file the walk listed that is gone when it is read is not an error: a live Couchbase compacts its bucket files, so
data/<bucket>/N.couch.K is replaced by N.couch.K+1 between the listing and the read. The directory is listed again
(after the walk, up to RELIST_PASSES times) and every regular file in it that was not read in its current form is read,
a name that came back included; the prune rules and the handling of links are the walk's. A file with no replacement
holds nothing. Only FileNotFoundError on a file is tolerated: another error on it, or a directory gone mid-walk, still
fails the scan, and so does a directory that still loses a listed file on the last listing (it is never passed). The
listing reads files only: a directory that appeared since the walk listed its parent is not walked, as before.

Exit 0 when no file holds one; 1 when one does, with one line per such file on stdout (its path, never the value);
2 on an error (a file or its metadata that cannot be read, a directory that will not hold still, a prune path that is a
link, bad arguments), so a caller that tests for 0 fails closed.
"""

import hashlib
import mmap
import os
import re
import stat
import sys

RELIST_PASSES = 5  # listings of a directory that lost a listed file, before it counts as one that will not hold still
RUN = re.compile(rb"[A-Za-z0-9]+")
ENTRY = re.compile(r"^([1-9][0-9]*):([0-9a-f]{64})$")


def parse(entries):
    """{length: {sha256 hex, ...}} from LENGTH:SHA256 arguments."""
    wanted = {}
    for entry in entries:
        m = ENTRY.match(entry)
        if not m:
            raise ValueError("not LENGTH:SHA256 (64 lowercase hex): %r" % entry)
        wanted.setdefault(int(m.group(1)), set()).add(m.group(2))
    if not wanted:
        raise ValueError("no LENGTH:SHA256 given")
    return wanted


def holds(data, wanted):
    """True when some window of a run of alphanumerics in data hashes to one of the wanted values."""
    for run in RUN.finditer(data):
        text = run.group()
        for length, digests in wanted.items():
            for i in range(len(text) - length + 1):
                if hashlib.sha256(text[i : i + length]).hexdigest() in digests:
                    return True
    return False


def file_holds(path, wanted):
    with open(path, "rb") as f:
        if os.fstat(f.fileno()).st_size == 0:
            return False
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return holds(data, wanted)


def below(top, rel):
    """The path `rel` below top, or None when it is not there. OSError when rel is no plain relative path, or when it
    or any directory on the way to it is a link: lstat on every component, so no link is followed."""
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise OSError("not a plain relative path: %r" % rel)
    path = top
    for part in parts:
        path = os.path.join(path, part)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(mode):
            raise OSError("a prune path is a link: %s" % path)
    return path


def pruned(top, prune, prune_each):
    """The paths left out of the walk: each of `prune` that is there, and each entry below each of `prune_each` that is
    there. OSError as `below` says, for an entry that is a link, and for a `prune_each` that is not a directory."""
    skip = set()
    for rel in prune:
        path = below(top, rel)
        if path is not None:
            skip.add(path)
    for rel in prune_each:
        path = below(top, rel)
        if path is None:
            continue
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OSError("a prune-each path is not a directory: %s" % path)
        for name in os.listdir(path):
            entry = os.path.join(path, name)
            if stat.S_ISLNK(os.lstat(entry).st_mode):
                raise OSError("a prune path is a link: %s" % entry)
            skip.add(entry)
    return skip


def stamp(st):
    """What tells one form of a file from another: a replacement of the same name or a rewritten file differs (ctime, so
    a rewrite that restores the modification time and the size differs too)."""
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def read_entry(path, wanted, found, done, walked):
    """Read the entry at path when it is a regular file not read in its current form (`done` maps path to the stamp of
    what was read; it and `found` are accumulators, updated in place). False when the entry is gone at its lstat or at
    its open: FileNotFoundError and nothing else, so the caller can list its directory again; True otherwise. A
    directory is an error when `walked` (os.walk listed it as a file, so it was never walked) and left alone when not.
    lstat, not os.path.isfile: an error must fail the scan, never skip the file as not regular."""
    try:
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode):
            if walked:
                raise OSError("could not walk the directory %s" % path)
        elif stat.S_ISREG(st.st_mode) and done.get(path) != stamp(st):
            if file_holds(path, wanted) and path not in found:
                found.append(path)
            done[path] = stamp(st)
    except FileNotFoundError:
        return False
    return True


def settled(directory, done, skip, wanted, found):
    """List `directory` again and read each regular file in it that `done` does not hold in its current form: a
    replacement, or a vanished name that is back. True when no entry the listing named was gone by its turn, so
    everything present at this listing was read. An entry of `skip` stays out, and a link is not followed."""
    whole = True
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if path not in skip and not read_entry(path, wanted, found, done, False):
            whole = False
    return whole


def relist(unsettled, skip, wanted, found):
    """List again each directory of `unsettled` ({directory: what was read in it}) until a listing of it loses no file,
    at most RELIST_PASSES times. OSError naming the directories that lost a listed file on the last listing too: a
    directory that will not hold still is never passed."""
    for _ in range(RELIST_PASSES):
        unsettled = {d: done for d, done in unsettled.items() if not settled(d, done, skip, wanted, found)}
        if not unsettled:
            return
    raise OSError(
        "a directory does not hold still: a file listed in it was gone before it was read, on each of %d listings: %s"
        % (RELIST_PASSES, ", ".join(sorted(unsettled)))
    )


def scan(top, wanted, prune=(), prune_each=()):
    """The regular files under top that hold a wanted value, but what `prune` and `prune_each` leave out. Raises
    OSError on one that cannot be read, on a prune path that is a link, and on a directory that keeps losing a file."""
    if not stat.S_ISDIR(os.lstat(top).st_mode):  # raises when top cannot be stat'ed; a link to a directory is refused
        raise OSError("not a directory: %s" % top)
    skip = pruned(top, prune, prune_each)
    found = []
    unsettled = {}  # directory -> what was read in it, for each that lost a file listed before it was read

    def fail(err):
        raise err

    for root, dirs, files in os.walk(top, onerror=fail):
        dirs[:] = [name for name in dirs if os.path.join(root, name) not in skip]  # in place: the walk goes no further
        done, whole = {}, True
        for name in sorted(files):
            path = os.path.join(root, name)
            if path not in skip and not read_entry(path, wanted, found, done, True):
                whole = False
        if not whole:
            unsettled[root] = done
    relist(unsettled, skip, wanted, found)
    return found


def options(argv):
    """(prune, prune_each, the arguments left) from argv; ValueError for an option that is unknown or has no value."""
    prune, prune_each, rest = [], [], list(argv)
    while rest and rest[0].startswith("--"):
        name = rest.pop(0)
        if name not in ("--prune", "--prune-each") or not rest:
            raise ValueError("unknown option, or one with no value: %s" % name)
        (prune if name == "--prune" else prune_each).append(rest.pop(0))
    return prune, prune_each, rest


def main(argv):
    try:
        prune, prune_each, rest = options(argv)
    except ValueError as err:
        sys.stderr.write("baked.py: %s\n" % err)
        return 2
    if len(rest) < 2:
        sys.stderr.write("usage: baked.py [--prune REL]... [--prune-each REL]... DIR LENGTH:SHA256...\n")
        return 2
    try:
        found = scan(rest[0], parse(rest[1:]), prune, prune_each)
    except (OSError, ValueError) as err:
        sys.stderr.write("baked.py: %s\n" % err)
        return 2
    for path in found:
        print(path)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
