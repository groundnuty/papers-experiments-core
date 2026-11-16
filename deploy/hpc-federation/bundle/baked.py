"""baked.py: find the Couchbase values the pinned images ship pre-initialised, by their SHA-256, never by value.

Both images carry the same Couchbase state (two cookies and the memcached _admin password). prep removes it and
check-defaults.sh checks that none of it is left or came back. The values are listed in lib.sh only as LENGTH:SHA256
(BAKED_CB_SHA256), so no password-like literal sits in the bundle, and this script compares hashes at run time.

  baked.py DIR LENGTH:SHA256...

DIR is walked without following links, and every regular file is read as bytes. The values are alphanumeric, so each
maximal run of [A-Za-z0-9] in a file is a place one could sit, at any offset within the run: every window of each
listed length in the run is hashed and compared. Binary files count as text does (Couchbase's config.dat is one).
Exit 0 when no file holds one; 1 when one does, with one line per such file on stdout (its path, never the value);
2 on an error (a file or its metadata that cannot be read, bad arguments), so a caller that tests for 0 fails closed.
"""

import hashlib
import mmap
import os
import re
import stat
import sys

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


def scan(top, wanted):
    """The regular files under top that hold a wanted value. Raises OSError on one that cannot be read."""
    if not stat.S_ISDIR(os.lstat(top).st_mode):  # raises when top cannot be stat'ed; a link to a directory is refused
        raise OSError("not a directory: %s" % top)
    found = []

    def fail(err):
        raise err

    for root, _dirs, files in os.walk(top, onerror=fail):
        for name in sorted(files):
            path = os.path.join(root, name)
            # lstat, not os.path.isfile: an error must fail the scan, never skip the file as not regular.
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):  # os.walk could not tell it was a directory, so it was never walked
                raise OSError("could not walk the directory %s" % path)
            if stat.S_ISREG(mode) and file_holds(path, wanted):
                found.append(path)
    return found


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: baked.py DIR LENGTH:SHA256...\n")
        return 2
    try:
        found = scan(argv[0], parse(argv[1:]))
    except (OSError, ValueError) as err:
        sys.stderr.write("baked.py: %s\n" % err)
        return 2
    for path in found:
        print(path)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
