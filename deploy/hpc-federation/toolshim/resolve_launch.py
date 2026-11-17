"""resolve_launch.py: start a Python tool server so that it resolves exactly the declared names of the map (O73).

    python -I -B resolve_launch.py <address map> -- -m <server module> [server arguments ...]
    python -I -B resolve_launch.py <address map> -- <server script> [server arguments ...]

A federation reached by the names its layout declares has names that resolve nowhere: the services register under them,
and the nodes' own names and addresses stay out of everything the agent can read. The login node's clients dial a
declared name at its node's address through the root's address map; the tool server (onedata-mcp: Python, httpx over
anyio) is a process of the driver's on the node, and there is no root to put a name in the node's resolver. So the
platform layer starts it through this launcher, with the interpreter the study declared: it reads the map, wraps
`socket.getaddrinfo` so that **exactly the names of that map** resolve, to their node's address, and every other host
as before, then runs the server as `python -m` or `python <script>` would (`runpy`), with `sys.argv` as the server would
see it. The URL stays the declared name, so the TLS handshake asks for it, the certificate is checked against it and
`Host` is it: only the connection goes to the address.

It is started like every Python of the driver's, `-I -B`: no `PYTHONPATH`, no `sitecustomize`, no environment variable
consulted (the map is an argument), and nothing is shadowed. The map is read as the platform layer reads it: a regular
file of this uid, not a link, mode 0600, no larger than a map of a federation can be, JSON of the format
`expcore-address-map/1`. A map it may not use stops the start (exit 78) with a message that names no path, name or
address, so the server never runs with a name unresolved by accident. It prints nothing otherwise: the server's stdout
is its protocol. It covers Python servers only, and `-I` applies to the server too. Python 3.8 compatible, standard
library only: it runs inside the server's own interpreter, which does not hold the core.
"""

import json
import os
import runpy
import socket
import stat
import sys

FORMAT = "expcore-address-map/1"
MAX_BYTES = 1 << 20
REFUSED = 78  # EX_CONFIG: the start was refused for its configuration


def refuse(why):
    sys.stderr.write("resolve_launch: " + why + "\n")
    raise SystemExit(REFUSED)


def load(path):
    """The `{declared name: address}` of the map file; SystemExit when it is not a map this process may use."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            refuse("the address map must be a private (0600) regular file of this user, never a link")
        if info.st_size > MAX_BYTES:
            refuse("the address map is larger than a map of a federation can be")
        with open(path, "rb") as handle:
            document = json.loads(handle.read(MAX_BYTES + 1).decode("utf-8"))
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            refuse("the address map is not a document of the expected format")
        found = {}
        for name, entry in document["hosts"].items():
            address = entry["address"]
            if not isinstance(name, str) or not isinstance(address, str) or not address:
                refuse("the address map holds an entry that is not a name and an address")
            found[name.lower()] = address
    except SystemExit:
        raise
    except Exception:
        refuse("the address map cannot be read")
    if not found:
        refuse("the address map names no host")
    return found


def install(mapped, real):
    """`real` wrapped so that the names of `mapped` resolve to their addresses; every other host is `real`'s."""

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        name = host.decode("ascii", "ignore") if isinstance(host, (bytes, bytearray)) else host
        address = mapped.get(name.lower()) if isinstance(name, str) else None
        return real(host if address is None else address, port, family, type, proto, flags)

    return getaddrinfo


def main(argv):
    """`argv` is this script's own: [script, map, "--", ...the server as python would be given it]. That is four words
    at least: the shortest server is a bare script with no arguments (`-m` alone is refused below)."""
    if len(argv) < 4 or argv[2] != "--":
        refuse("usage: resolve_launch.py <address map> -- -m <module> [arguments] | <script> [arguments]")
    mapped = load(argv[1])
    server = argv[3:]
    if server[0] == "-m":
        if len(server) < 2 or not server[1]:
            refuse("-m needs a module")
        target, arguments, run = server[1], server[2:], runpy.run_module
        options = {"run_name": "__main__", "alter_sys": True}
    elif server[0].startswith("-"):
        refuse("the server is started by -m <module> or by a script, with no other interpreter option")
    else:
        target, arguments, run = server[0], server[1:], runpy.run_path
        options = {"run_name": "__main__"}
    socket.getaddrinfo = install(mapped, socket.getaddrinfo)
    sys.argv = [target, *arguments]
    run(target, **options)


if __name__ == "__main__":
    main(sys.argv)
