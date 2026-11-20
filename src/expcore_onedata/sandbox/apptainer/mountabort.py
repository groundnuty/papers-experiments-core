# Provenance: new
"""The one action that frees a cell blocked in a read of the run's mount: abort its FUSE connection (O34, O97).

A request the daemon has already read is waited for uninterruptibly once SIGKILL is pending, so a cell blocked in a
read of a stopped provider's file stays in the kernel until oneclient answers or the connection ends. Ending the
connection ends every request on it with an error (`ECONNABORTED`), the killed process returns and dies. The mount's
owner can do it without privilege, in two ways, tried in this order:

1. **fusectl**: write `1` to the connection's `abort` file, when the start found it writable for this uid
   (`fusectl.state`). It is one write to a pseudo-file, it depends on nothing in oneclient's state, and it is done only
   when the mount table, read now, still shows this uid's mount at the mount point on the device the mount came up with:
   connection numbers are reused after an unmount, so a number alone is no identity. The table is the only thing read;
   a `stat` of the mount point would wait on the wedged connection.
2. **The daemon's death**: SIGKILL to the run's own oneclient by pid and start time (`NodeOps.signal`, which never
   signals a process that took the pid over). The kernel ends the connection when oneclient's last `/dev/fuse`
   descriptor closes. It is waited for gone, within the time left, since a oneclient with a thread in uninterruptible
   sleep may not finish exiting.

**Only this freeing action runs here**, inside the deadline's margin: oneclient's orderly stop (SIGTERM, then SIGKILL)
and the unmount of the dead mount are the restore's (`HostMount.restore`) or the stop's. The record
`{"ok", "method", "waiting_before", "waiting_after", "abort_s"}` is the deadline's `abort` step: `waiting_*` are the
connection's counters before and after (None when it has no usable fusectl), `method` is `fusectl` or `daemon_kill`,
and `why` (a fixed phrase, never an error's text) says why `ok` is not True. `fusectl_skipped` says why a writable
connection was not aborted through fusectl (`identity_changed`, `write_failed`). It never raises.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox import fusectl as FC
from expcore_onedata.sandbox.apptainer import helpers
from expcore_onedata.sandbox.apptainer.node import NodeOps, Spawned

FUSECTL, DAEMON_KILL = "fusectl", "daemon_kill"
NO_TIME = "no time was left"
NOT_SIGNALLED = "oneclient could not be signalled"
STAYED = "oneclient did not end within the time left"
END_UNKNOWN = "oneclient's end could not be told"
RAISED = "the abort raised"
NOT_ATTEMPTED = "no oneclient is running"


def not_attempted() -> dict[str, Any]:
    """The record of an abort that could not be tried: `ok` None, as the deadline reads it."""
    return {"ok": None, "why": NOT_ATTEMPTED}


def abort(
    node: NodeOps,
    process: Spawned,
    *,
    connection: str | None,
    state: str | None,
    root: Path,
    identity_ok: Callable[[], bool],
    timeout_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Free what waits on the connection of oneclient `process` within `timeout_s` (the module's docstring).
    `connection` is its fusectl name, `state` the fusectl state the start recorded for it, `identity_ok` whether the
    mount table still shows this uid's mount at the mount point on the device the mount came up with."""
    began = clock()
    record: dict[str, Any] = {
        "ok": False,
        "method": None,
        "waiting_before": None,
        "waiting_after": None,
        "abort_s": 0.0,
    }
    try:
        if timeout_s <= 0:
            record["why"] = NO_TIME
        else:
            _free(record, node, process, connection, state, root, identity_ok, began + timeout_s, sleep, clock)
    except Exception:  # the deadline reads the record: whatever went wrong is a failed abort, never an exception
        record.update(ok=False, why=RAISED)
    record["abort_s"] = round(max(clock() - began, 0.0), 3)
    return record


def _free(
    record: dict[str, Any],
    node: NodeOps,
    process: Spawned,
    connection: str | None,
    state: str | None,
    root: Path,
    identity_ok: Callable[[], bool],
    until: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    usable = connection is not None and state == FC.WRITABLE
    if connection is not None:
        record["waiting_before"] = FC.waiting(root, connection)
    if usable:
        skipped = _by_fusectl(root, connection, identity_ok)
        if skipped is None:
            record.update(ok=True, method=FUSECTL)
        else:
            record["fusectl_skipped"] = skipped
    if record["method"] is None:
        record["method"] = DAEMON_KILL
        _by_kill(record, node, process, until, sleep, clock)
    if connection is not None:
        record["waiting_after"] = FC.waiting(root, connection)


def _by_fusectl(root: Path, connection: str | None, identity_ok: Callable[[], bool]) -> str | None:
    """None when the connection was aborted through fusectl, else why it was not."""
    if connection is None or not identity_ok():
        return "identity_changed"
    try:
        FC.write_abort(root, connection)
    except OSError:
        return "write_failed"
    return None


def _by_kill(
    record: dict[str, Any],
    node: NodeOps,
    process: Spawned,
    until: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """SIGKILL to oneclient by pid and start, and the wait for it to be gone, at most until `until`."""
    sent = node.signal(process.pid, process.start, signal.SIGKILL)
    if sent is None:
        record.update(ok=False, why=NOT_SIGNALLED)
        return
    gone = helpers.wait_gone(node, process, max(until - clock(), 0.0), sleep=sleep, clock=clock)
    if gone is True:
        record["ok"] = True
    else:
        record.update(ok=False, why=STAYED if gone is False else END_UNKNOWN)


__all__ = ["DAEMON_KILL", "FUSECTL", "abort", "not_attempted"]
