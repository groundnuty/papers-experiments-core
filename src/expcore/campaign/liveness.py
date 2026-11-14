# Provenance: new
"""Which attempt a context runs, and whether the attempt a token names is still live.

A platform that takes a hold for a run (a progressive ingest's baseline held open from its start to its reset) writes
who holds it, so a later reader can tell a run in progress from a hold its attempt left behind. The token is
`owner()`: the process, as the lease pool names a holder (`lease.process_owner`: its boot id, pid and start time, so
a reused pid is not mistaken for it), and, inside an attempt, the campaign it belongs to and the store's id of its
row (`attempt_id`). No run key, path or name is in it.

An attempt is **live** from the moment its 'running' row is written until the store holds its result, finished or
set aside: the attempt runner enters `running` around exactly that, so the executor, its instruments and the post-run
check run inside it, and the pre-run check and the dispatcher's own check do not (`current` tells which). `state`
answers for a token:

- `ended`: its process is gone (another boot, no such pid, the pid reused); or the process is this one and the
  campaign's store says the attempt is not running (finished, set aside, interrupted, or no such row), or this process
  runs that campaign no more;
- `live`: the store says it is running; or the token names another process that still runs, whose store this process
  cannot read, or names no attempt (a hold taken outside a campaign) and its process still runs;
- None: the process's liveness cannot be read.

The scheduler registers its store when it opens (`register`) and takes it back when it closes (`unregister`).
"""

import contextlib
import os
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from typing import Any

from expcore.campaign.lease import process_gone, process_owner

LIVE, ENDED = "live", "ended"
_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar("expcore_attempt", default=None)
_LOCK = threading.Lock()
_CAMPAIGNS: dict[str, Callable[[int], str | None]] = {}  # campaign key -> the state of an attempt row, by its id
_OWNER: tuple[int, str] | None = None  # (pid, process_owner()) of this process, read once per pid


def this_process() -> str:
    """This process as `lease.process_owner` names it, read once per pid (a forked child reads its own)."""
    global _OWNER
    cached = _OWNER
    if cached is None or cached[0] != os.getpid():
        cached = _OWNER = (os.getpid(), process_owner())
    return cached[1]


def register(state_of: Callable[[int], str | None]) -> str:
    """Register a campaign's store: `state_of(attempt_id)` returns the state of that attempt's row, or None when there
    is none. Return the campaign's key, which `running` writes into its tokens."""
    key = uuid.uuid4().hex
    with _LOCK:
        _CAMPAIGNS[key] = state_of
    return key


def unregister(key: str) -> None:
    """Take a campaign's store back: its attempts read as ended in this process from now on."""
    with _LOCK:
        _CAMPAIGNS.pop(key, None)


@contextlib.contextmanager
def running(campaign: str, attempt_id: int) -> Iterator[dict[str, Any]]:
    """Run the body as the attempt with this row id in the campaign registered under `campaign`."""
    token = {"process": this_process(), "campaign": campaign, "attempt_id": attempt_id}
    reset = _CURRENT.set(token)
    try:
        yield token
    finally:
        _CURRENT.reset(reset)


def current() -> dict[str, Any] | None:
    """The token of the attempt this context runs, or None outside an attempt (a pre-run check, the dispatcher's own
    check, a script beside the campaign)."""
    token = _CURRENT.get()
    return dict(token) if token is not None else None


def owner() -> dict[str, Any]:
    """Who takes a hold now: the attempt this context runs, or, outside one, this process alone."""
    return current() or {"process": this_process()}


def _attempt_state(token: Mapping[str, Any], attempt_id: int) -> str | None:
    with _LOCK:
        state_of = _CAMPAIGNS.get(str(token.get("campaign")))
    if state_of is None:
        return ENDED  # this process ran that campaign and runs it no more
    try:
        row = state_of(attempt_id)
    except Exception:  # a store that cannot be read: not known, never live
        return None
    return LIVE if row == "running" else ENDED


def state(token: Any) -> str | None:
    """Whether the attempt, or the process, a token of `owner` names is still live: `live`, `ended` or None (unknown);
    see the module docstring."""
    if not isinstance(token, Mapping):
        return None
    process = token.get("process")
    if not isinstance(process, str) or not process:
        return None
    gone = process_gone(process)
    if gone is True:
        return ENDED
    if "attempt_id" not in token:
        return LIVE if gone is False else None
    attempt_id = token["attempt_id"]
    if isinstance(attempt_id, bool) or not isinstance(attempt_id, int):
        return None
    if process == this_process():
        return _attempt_state(token, attempt_id)
    return LIVE if gone is False else None


__all__ = ["ENDED", "LIVE", "current", "owner", "register", "running", "state", "this_process", "unregister"]
