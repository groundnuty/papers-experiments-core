"""sitecustomize.py: a Python tool server resolves exactly the declared names of the federation's address map (O73).

A federation reached by the names its layout declares has names that resolve nowhere: the services register under them
and the nodes' own names and addresses stay out of everything the agent can read. The login node's clients dial a
declared name at its node's address through the root's address map; the tool server (onedata-mcp: Python, httpx over
anyio) is a process of the driver's on the node, with no root to put a name in the node's resolver, so this module does
the same for it. Python imports it at start (`sitecustomize`, found first on `PYTHONPATH`, which `up` names in the
platform file and the platform layer puts in the server's environment); it reads the file `EXPCORE_ADDRESS_MAP` names
and wraps `socket.getaddrinfo` so that **exactly the names of that map** resolve, to their node's address, and every
other host as before. The URL stays the declared name, so the TLS handshake asks for it, the certificate is checked
against it and `Host` is it: only the connection goes to the address.

The map is read as the platform layer reads it, and refused as it refuses it: a regular file of this uid, not a link,
mode 0600, no larger than a map of a federation can be, JSON of the format `expcore-address-map/1`. A map it refuses (or
cannot read) maps no name, silently: the server then cannot resolve a declared name, which is the failure, never a name
resolved elsewhere. It never raises at import, and never prints, since the server's stdout is its protocol.

It serves a Python server that does not run isolated (`-I` ignores `PYTHONPATH`); it replaces another `sitecustomize`
found later on the path; and it resolves through `socket.getaddrinfo`, which httpx, httpcore and anyio use. Python 3.8
compatible, standard library only: it runs inside the server's own interpreter.
"""

import json
import os
import socket
import stat

FORMAT = "expcore-address-map/1"
MAX_BYTES = 1 << 20
ENV = "EXPCORE_ADDRESS_MAP"


def load(path):
    """The `{declared name: address}` of the map file, or an empty one when it is no map this process may use."""
    try:
        if not path:
            return {}
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            return {}
        if info.st_size > MAX_BYTES:
            return {}
        with open(path, "rb") as handle:
            document = json.loads(handle.read(MAX_BYTES + 1).decode("utf-8"))
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            return {}
        found = {}
        for name, entry in document["hosts"].items():
            address = entry["address"]
            if isinstance(name, str) and isinstance(address, str) and address:
                found[name.lower()] = address
        return found
    except Exception:  # a map that cannot be used maps nothing: the declared names then fail to resolve
        return {}


def install(mapped, real):
    """`real` wrapped so that the names of `mapped` resolve to their addresses; every other host is `real`'s."""

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        name = host.decode("ascii", "ignore") if isinstance(host, (bytes, bytearray)) else host
        address = mapped.get(name.lower()) if isinstance(name, str) else None
        return real(host if address is None else address, port, family, type, proto, flags)

    return getaddrinfo


MAP = load(os.environ.get(ENV))
if MAP:
    socket.getaddrinfo = install(MAP, socket.getaddrinfo)
