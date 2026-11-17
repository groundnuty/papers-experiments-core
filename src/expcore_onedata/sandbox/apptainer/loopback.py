# Provenance: new
"""The Apptainer backend's uses of the node's own network stack, kept apart: a free port for the gate, and the canary's
listener (O56), and the node's own host name (O73). Nothing here connects anywhere or sends a byte: a port is bound and
released, or bound and listened on while a cell connects to it, on 127.0.0.1 alone; the host name is the system's answer
to its own name, a lookup through the node's resolver that no service of the federation sees. HTTP stays the client's
(O09)."""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Iterator

LOOPBACK = "127.0.0.1"


def free_port() -> int:
    """A TCP port on 127.0.0.1 that was free when asked (another process may take it before it is bound)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


def own_names() -> tuple[str, ...]:
    """The node's own host name and its fully qualified form, as the system says them (`gethostname`, `getfqdn`); one
    whose lookup fails is left out. For the gate's scan (`cellhost.scan_names`), never recorded: a site's name."""
    found: list[str] = []
    for lookup in (socket.gethostname, socket.getfqdn):
        try:
            found.append(lookup())
        except OSError:
            continue
    return tuple(name for name in found if name)


@contextlib.contextmanager
def listener() -> Iterator[int]:
    """A listening socket on 127.0.0.1 at a free port, for as long as the block runs: its port. It accepts nothing;
    a connect completes in the kernel's backlog."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((LOOPBACK, 0))
        held.listen(4)
        yield int(held.getsockname()[1])


__all__ = ["LOOPBACK", "free_port", "listener", "own_names"]
