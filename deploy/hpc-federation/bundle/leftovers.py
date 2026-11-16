"""leftovers.py: how many work directories of one service kind this uid holds in the node's tmpfs, for the HPC
federation driver's `down`, in a read-only control step (python3.11 -I, stdlib only).

`down` runs it for a service whose step ended before its log named a work directory (one that failed in node-init
before making one, say): such a service is clean only when this counts none. A directory counts when it is named as
the bundle's `make_workdir` names one (`hpcop-<oz|op>.XXXXXX`) directly in the tmpfs and this uid owns it, whatever
federation made it, so another of ours on the node keeps the service unverified rather than passed. Prints one JSON
object, `{"kind": ..., "count": N}`; anything it cannot read (the tmpfs, an entry) fails it, never a count of 0, and so
does a tmpfs that is a link: `lib.sh` then makes its work directories under another directory (its TMPDIR fallback).

Usage: leftovers.py <oz|op> <tmpfs>
"""

import json
import os
import re
import sys


def main(argv) -> int:
    if len(argv) != 2 or argv[0] not in ("oz", "op") or not os.path.isabs(argv[1]):
        print("usage: leftovers.py <oz|op> <tmpfs>", file=sys.stderr)
        return 2
    kind, tmpfs = argv
    if os.path.islink(tmpfs):  # lib.sh makes its work directories elsewhere then (its TMPDIR fallback): not here
        print("leftovers.py: the tmpfs is a link, so no work directory of the bundle's is in it", file=sys.stderr)
        return 1
    named = re.compile(r"hpcop-%s\.[A-Za-z0-9]{6}" % kind)
    uid = os.getuid()
    count = 0
    for name in os.listdir(tmpfs):  # an OSError ends the step with a traceback: no count is printed
        if named.fullmatch(name) and os.lstat(os.path.join(tmpfs, name)).st_uid == uid:
            count += 1
    print(json.dumps({"kind": kind, "count": count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
