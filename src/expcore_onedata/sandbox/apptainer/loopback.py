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
from collections.abc import Callable, Iterable, Iterator

from expcore_onedata.sandbox.cellhost import OwnNames

LOOPBACK = "127.0.0.1"
FQDN_TIMEOUT_S = 5.0  # the most a resolver may delay a start: the lookup of the node's own fully qualified name


def free_port() -> int:
    """A TCP port on 127.0.0.1 that was free when asked (another process may take it before it is bound)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


def _bounded[T](lookup: Callable[[], T], timeout_s: float) -> tuple[T | None, str | None]:
    """`(answer, None)` of `lookup`, run in a thread of its own that is not waited for past `timeout_s`; `(None,
    "fqdn_timeout")` when it did not answer in time (the thread is left to end by itself: it is a daemon's), and `(None,
    "fqdn_failed")` when it raised. A resolver that hangs thus costs a start the bound and no more."""
    box: dict[str, T] = {}

    def run() -> None:
        with contextlib.suppress(Exception):  # whatever it raised, the reason recorded is `fqdn_failed`, never its text
            box["answer"] = lookup()

    thread = threading.Thread(target=run, name="own-fqdn-lookup", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return None, "fqdn_timeout"
    return (box["answer"], None) if "answer" in box else (None, "fqdn_failed")


def own_names(timeout_s: float | None = None) -> OwnNames:
    """The node's own names as the system says them: its host name (`gethostname`) and the names its resolver gives that
    host name (`gethostbyaddr`: the primary name and every alias), each once in any case. The lookup is bounded
    (`FQDN_TIMEOUT_S`): on a timeout or a failure the host name stands alone, and the reason, never a name, is in
    `unread`. A resolver that gives no name with a domain answers without an error, and its names are returned as they
    are: that the scan then holds a one-label name is said by the run's facts (`cellhost.lacks_domain`), where the names
    the scan keeps are known. (`getfqdn` was not used: it returns the first name with a dot, or else the bare host name
    without an error, so a node of several dotted names had one scanned and one with none looked read.) For the gate's
    scan (`cellhost.scan_names`), never recorded: a site's name."""
    bound = FQDN_TIMEOUT_S if timeout_s is None else timeout_s
    names: list[str] = []
    unread: list[str] = []

    def add(found: Iterable[str]) -> None:  # the names as said, in order, none blank and none twice in any case
        for name in found:
            if name and name.lower() not in map(str.lower, names):
                names.append(name)

    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    if not host:
        return OwnNames((), ("host_name_failed", "fqdn_failed"))
    add([host])
    answer, why = _bounded(lambda: socket.gethostbyaddr(host), bound)
    if why is not None:
        unread.append(why)
    elif answer is not None:
        primary, aliases, _addresses = answer
        add([primary, *aliases])
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
