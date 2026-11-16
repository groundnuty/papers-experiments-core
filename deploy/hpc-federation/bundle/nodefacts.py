"""nodefacts.py: one node's facts for the HPC federation driver, in a control step (python3.11 -I, stdlib only).

Prints one JSON object on stdout: the node's FQDN as `hostname -f` gives it (the name every service's test web
certificate is made for, `lib.sh`'s node_fqdn), its IPv4 address on that name (the address the shim's peer lists
hold), and the step's uid and gid (the posix storage's owner, finish.sh). No secret, and nothing is written.

Usage: nodefacts.py
"""

import json
import os
import re
import socket
import subprocess
import sys

FQDN = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def main() -> int:
    fqdn = subprocess.run(["hostname", "-f"], capture_output=True, text=True, check=True).stdout.strip()
    if not FQDN.fullmatch(fqdn):
        print("nodefacts: hostname -f gave no fully qualified name", file=sys.stderr)
        return 1
    found = sorted({info[4][0] for info in socket.getaddrinfo(fqdn, None, socket.AF_INET, socket.SOCK_STREAM)})
    if len(found) != 1:
        print(f"nodefacts: the node's name has {len(found)} IPv4 addresses, not one", file=sys.stderr)
        return 1
    print(json.dumps({"fqdn": fqdn, "address": found[0], "uid": os.getuid(), "gid": os.getgid()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
