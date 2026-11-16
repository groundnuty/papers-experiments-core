# Provenance: new
"""The Apptainer backend's two uses of the node's loopback, kept apart: a free port for the gate, and the canary's
listener (O56). Nothing here connects anywhere or sends a byte: a port is bound and released, or bound and listened on
while a cell connects to it, on 127.0.0.1 alone. HTTP stays the client's (O09)."""

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


@contextlib.contextmanager
def listener() -> Iterator[int]:
    """A listening socket on 127.0.0.1 at a free port, for as long as the block runs: its port. It accepts nothing;
    a connect completes in the kernel's backlog."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((LOOPBACK, 0))
        held.listen(4)
        yield int(held.getsockname()[1])


__all__ = ["LOOPBACK", "free_port", "listener"]
