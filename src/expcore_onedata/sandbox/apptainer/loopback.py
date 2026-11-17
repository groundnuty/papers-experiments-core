# Provenance: new
"""The Apptainer backend's uses of the node's own network stack, kept apart: a free port for the gate, and the canary's
listener (O56), and the node's own host name (O73). Nothing here connects anywhere or sends a byte: a port is bound and
released, or bound and listened on while a cell connects to it, on 127.0.0.1 alone; the host name is the system's answer
to its own name, a lookup through the node's resolver that no service of the federation sees. HTTP stays the client's
(O09)."""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable, Iterator

from expcore_onedata.sandbox.cellhost import OwnNames

LOOPBACK = "127.0.0.1"
FQDN_TIMEOUT_S = 5.0  # the most a resolver may delay a start: the lookup of the node's own fully qualified name


def free_port() -> int:
    """A TCP port on 127.0.0.1 that was free when asked (another process may take it before it is bound)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


def _bounded(lookup: Callable[[], str], timeout_s: float) -> tuple[str | None, str | None]:
    """`(answer, None)` of `lookup`, run in a thread of its own that is not waited for past `timeout_s`; `(None,
    "fqdn_timeout")` when it did not answer in time (the thread is left to end by itself: it is a daemon's), and `(None,
    "fqdn_failed")` when it raised. A resolver that hangs thus costs a start the bound and no more."""
    box: dict[str, str] = {}

    def run() -> None:
        try:
            box["name"] = lookup()
        except Exception:  # whatever it was, the reason recorded is `fqdn_failed`, never the exception's text
            box["failed"] = "yes"

    thread = threading.Thread(target=run, name="own-fqdn-lookup", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return None, "fqdn_timeout"
    return (box["name"], None) if "name" in box else (None, "fqdn_failed")


def own_names(timeout_s: float | None = None) -> OwnNames:
    """The node's own host name and its fully qualified form, as the system says them (`gethostname`, `getfqdn`). The
    second is a resolver lookup, bounded (`FQDN_TIMEOUT_S`): on a timeout or a failure the host name stands alone, and
    the reason, never a name, is in `unread`. For the gate's scan (`cellhost.scan_names`), never recorded: a site's
    name."""
    bound = FQDN_TIMEOUT_S if timeout_s is None else timeout_s
    names: list[str] = []
    unread: list[str] = []
    try:
        host = socket.gethostname()
    except OSError:
        unread.append("host_name_failed")
    else:
        names += [host] if host else []
    full, why = _bounded(socket.getfqdn, bound)
    if why is not None:
        unread.append(why)
    elif full:
        names.append(full)
    return OwnNames(tuple(names), tuple(unread))


@contextlib.contextmanager
def listener() -> Iterator[int]:
    """A listening socket on 127.0.0.1 at a free port, for as long as the block runs: its port. It accepts nothing;
    a connect completes in the kernel's backlog."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((LOOPBACK, 0))
        held.listen(4)
        yield int(held.getsockname()[1])


__all__ = ["LOOPBACK", "free_port", "listener", "own_names"]
