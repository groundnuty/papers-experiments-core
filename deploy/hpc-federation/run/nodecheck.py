"""nodecheck.py: the read-only check of one node after teardown (python3.11 -I, in a step on that node).

  nodecheck.py ROLE      print one JSON object: counts of what this uid still has on the node. It changes nothing.

  leftover_tmp   entries named hpcop-* or sifbuild-* of this uid under /tmp, to depth 3
  tmp_top        entries of this uid at the top level of /tmp
  jobtmp         entries in the step's TMPDIR (null when the step has none, which fails the run's summary)
  listeners      TCP listeners with a process of this uid (ss -p names only our own processes)
  procs_other    processes of this uid besides the allocation's own and this check itself
  procs_other_names   their names, for the person reading a failed item
  ps_rc, ss_rc   the exit statuses of the two listings behind procs_other and listeners (127: not found); a count
                 from a listing that did not exit 0 fails the run's summary
The node's name is never printed: the role says which node it was.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# The allocation's holding script and the `ps` that lists the processes. This check's own process is left out by
# its PID, not by its name, so a service of ours that outlived its teardown (also python3.11) is still counted.
ALLOWED = {"ps", "sleep", "slurm_script", "slurmstepd", "srun"}


def mine(path):
    try:
        return os.lstat(path).st_uid == os.getuid()
    except OSError:
        return False


def leftovers(top, depth):
    found = 0
    base = top.rstrip("/").count("/")
    for here, dirs, files in os.walk(top):
        if here.count("/") - base >= depth:
            dirs[:] = []
        for name in dirs + files:
            if name.startswith(("hpcop-", "sifbuild-")) and mine(os.path.join(here, name)):
                found += 1
    return found


def run(argv):
    """(exit status, stdout). A command that failed prints nothing, which would read as nothing found: its status is
    recorded beside the count, and the summary fails a count whose command did not exit 0."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except OSError:
        return 127, ""  # not found or not runnable, as a shell reports it
    except subprocess.TimeoutExpired:
        return 124, ""  # as timeout(1) reports it
    return done.returncode, done.stdout


def main(argv):
    if len(argv) != 1 or not argv[0].isalnum():
        sys.exit(__doc__)
    tmpdir = os.environ.get("TMPDIR")
    tmp_entries = os.listdir("/tmp") if os.path.isdir("/tmp") else []
    ps_rc, ps_out = run(["ps", "-u", str(os.getuid()), "-o", "pid=,comm="])
    ss_rc, ss_out = run(["ss", "-Hltnup"])
    rows = [line.split(None, 1) for line in ps_out.splitlines()]
    other = sorted(
        row[1].strip() for row in rows if len(row) == 2 and int(row[0]) != os.getpid() and row[1].strip() not in ALLOWED
    )
    doc = {
        "role": argv[0],
        "leftover_tmp": leftovers("/tmp", 3),
        "tmp_top": sum(1 for name in tmp_entries if mine(os.path.join("/tmp", name))),
        "jobtmp": len(os.listdir(tmpdir)) if tmpdir and os.path.isdir(tmpdir) else None,
        "listeners": sum(1 for line in ss_out.splitlines() if "pid=" in line),
        "procs_other": len(other),
        "procs_other_names": other,
        "ps_rc": ps_rc,
        "ss_rc": ss_rc,
    }
    print(json.dumps(doc, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
