"""The proofs that a service's launch failed before anything of the service ran, so `up` may launch it once more
(O57, DESIGN.md section 5, "A launch that failed at launch"). Pure functions: nothing here reads Slurm; the driver
reads the three sources (srun's client log, sacct, the run log) and asks here whether they prove it.

Seen live twice on 17 Nov 2026: srun's fan-out on the login node could not find the nodes' addresses, and every
provider's step was accounted `CANCELLED`, exit `0:116`, within 12 s, its run log made and empty. The client's log
(`<run log>.client`) held, with the node's name, `srun: error: Task launch for StepId=<id> failed on node <node>: Can't
find an address, check slurm.conf`. A step a user cancels reads `CANCELLED by <uid>` with `0:9` (the live probe); a
job's batch step cancelled with the job reads plain `CANCELLED` with `0:15`. So the state alone is not the proof: the
exit code `0:116`, the exact id and the client's own line are. A launch is proved failed only when BOTH hold:

- (a) the step, by the exact id the client's log names, is in sacct as exactly `CANCELLED`, exit `0:116`, under the
  step's own name, and the client's log shows that one step's task launch failing for want of an address, which srun's
  message reads as a request that was never sent (that is its message, not something seen on the node). A task launch
  failing otherwise (a socket that timed out) may have reached the node, and is no proof;
- (b) the run log is a file of zero bytes. The bundle's batch script writes to it first, so a single byte means the
  service may have started.

The same row's End says when the step ended, which the retry's backoff counts from (`failure_time`, `backoff_wait`):
providers that failed together then wait one backoff in all. An End that cannot be read, or lies outside the time
between the launch and now, is no time: the whole backoff is waited from now, never none.

Nothing here names a node: a failure is recorded as a class (`NO_ADDRESS`), never as the client's line.
"""

import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

RETRY_LIMIT = 1  # a service's launches that may be repeated; read from the state file, not from this process
STATE = "CANCELLED"  # sacct's State of the failed step, exactly: `CANCELLED by <uid>` is a step a user cancelled
EXIT = "0:116"  # sacct's ExitCode of it (exit:signal), as the site printed it both times
NO_ADDRESS = "task-launch-no-address"  # the one failure class that is proof: the launch request was never sent
STEP_ID = r"[0-9]{1,12}(?:\+[0-9]{1,6})?\.[0-9]{1,9}"  # `<job>.<n>`, or a heterogeneous job's `<leader>+<k>.<n>`
TASK_LAUNCH = re.compile(rf"srun: error: Task launch for StepId=({STEP_ID}) failed on node \S+: (.*)")
ADDRESS_REASON = "Can't find an address"
CLIENT_LIMIT = 65536  # the most of a client's log read: a larger one is no proof
END_FORMAT = "%Y-%m-%dT%H:%M:%S"  # sacct's End, in the local time of the node that prints it ("Unknown" until it ends)
SKEW_S = 10.0  # what an End may lie outside the time between the launch and now, for the clocks' skew


def client_failure(text: str) -> Optional[Tuple[str, str]]:
    """(step id, class) of the one task launch failure srun's client log shows, or None: no such line, a failure
    other than the missing address, or lines naming more than one step or reason (which step failed is not shown)."""
    found = set()
    for line in text.splitlines():
        match = TASK_LAUNCH.fullmatch(line.strip())
        if match:
            found.add((match.group(1), NO_ADDRESS if match.group(2).startswith(ADDRESS_REASON) else "other"))
    if len(found) != 1:
        return None
    ident, failure = next(iter(found))
    return (ident, failure) if failure == NO_ADDRESS else None


def accounted(rows: str, step_id: str, name: str) -> Optional[Tuple[str, str, str]]:
    """(State, ExitCode, End), raw, of the one `JobID|JobName|State|ExitCode|End` row of this exact step id, when that
    row is of the step's own name; None when no row, two rows, another name or a row of another shape."""
    found = []
    for line in rows.splitlines():
        parts = line.split("|")
        if len(parts) == 5 and parts[0] == step_id:
            found.append(parts)
    if len(found) != 1 or found[0][1] != name:
        return None
    return found[0][2], found[0][3], found[0][4]


def failure_time(end: str, launched_at: Optional[float], now: float) -> Optional[float]:
    """When the step ended, as epoch seconds, from the End sacct printed for it; None when that is not to be believed.
    sacct prints End in the local time of the node that prints it, so it is read as the local time of this node, and
    believed only when it falls between this process's launch of the step (less `SKEW_S`) and now (plus it): a time in
    another zone, another clock or a field that is not a time ("Unknown", empty) lies outside that and is no time.
    `launched_at` and `now` are this process's wall clock; None when the step was launched by no process of this one."""
    if launched_at is None:
        return None
    try:
        ended = time.mktime(time.strptime(end, END_FORMAT))
    except (ValueError, OverflowError):
        return None
    return ended if launched_at - SKEW_S <= ended <= now + SKEW_S else None


def backoff_wait(backoff_s: float, failed_at: Optional[float], now: float) -> float:
    """What is left of the backoff, counted from the failure's own time: the whole of it from now when that time is
    unknown (never zero then), never below zero when it is known."""
    return backoff_s if failed_at is None else max(0.0, failed_at + backoff_s - now)


def run_log_size(path: Path) -> Optional[int]:
    """The run log's size in bytes by a fresh stat, None when it is not a regular file there (no log at all proves
    nothing)."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def read_client_log(path: Path) -> Optional[str]:
    """The client's log, up to `CLIENT_LIMIT` bytes; None when it cannot be read or is larger."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(CLIENT_LIMIT + 1)
    except OSError:
        return None
    return None if len(data) > CLIENT_LIMIT else data.decode("utf-8", "replace")


def prove(
    name: str, known_id: Optional[str], client: Optional[str], rows: Optional[str], log_bytes: Optional[int]
) -> Optional[Dict[str, Any]]:
    """The record of a launch's proofs, or None when either does not hold. `name` is the step's own name; `known_id`
    the id the state holds for it, when `up` saw the step listed, which must be the one the client names (the later
    steps of a federation that fails together are never seen: the client's log is where their id is read); `client`
    the client's log, `rows` sacct's rows of the job's steps and `log_bytes` the run log's size, each None when it
    could not be read."""
    if client is None or rows is None or log_bytes != 0:
        return None
    failure = client_failure(client)
    if failure is None or (known_id is not None and known_id != failure[0]):
        return None
    row = accounted(rows, failure[0], name)
    if row is None or row[:2] != (STATE, EXIT):
        return None
    return {
        "step": name,
        "step_id": failure[0],
        "sacct_state": STATE,
        "sacct_exit": EXIT,
        "run_log_bytes": 0,
        "failure": failure[1],
        "step_end": row[2],
    }


def evidence(service: str, entry: Dict[str, Any]) -> str:
    """One line of `up`'s output for a retry: the service, which launch, and each proof as it was read."""
    return (
        f"launch retried: {service} launch {entry['launch']} ({entry['step']}, step {entry['step_id']}): sacct "
        f"{entry['sacct_state']} {entry['sacct_exit']}, ended {entry['step_end']}, run log "
        f"{entry['run_log_bytes']} bytes, client log {entry['failure']}, waited {entry['waited_s']:g} s"
    )
