"""The late steps of a verb the fault relay answered `timed_out` (`hpcfed.py late-steps`; DESIGN.md, section 7).

The relay ends a timed-out verb's process group (`expcore_onedata.fault_relay_group`), so no `srun` client of it
survives. One gap is left: the controller, which can stall silently for its message timeout (about 200 s on the site),
may already have been asked for the step, and may make it afterwards. This command closes it. It is started by the
relay alone (never by a request: it is no verb of `fault_hpc.VERBS`), with the tag the relay gave the verb.

- It looks for a step whose name is one of the verb's exact control-step names, `<allocation>-<service>-<word>-<tag>`
  for the verb's words (`hpcfed_control.CONTROL_WORDS`), in the federation's own job (`hpclaunch.listings`) alone, every
  `poll_s` for `window_s`. Nothing else is looked at: not another tag, service or word, nor a step of the service.
- Each such step is cancelled by its exact `<job>.<step>` id, through the launcher, which refuses an id of any other job
  of the federation's, never by user, name or pattern. A cancel that fails is tried again at the next look.
- At the window's end it reads the accounting for the same names: a step that was made and ended between two looks is
  recorded as `accounted_late` (with how it ended) when its accounted Start is not before the moment the relay
  answered (`--since`; a Start the accounting does not hold is counted, since the step may have run late), and as
  `accounted_in_time` when it started before: a step that ran inside the verb is no late step. Nothing is cancelled
  that is over.
- What it saw goes to the relay's journal (`late-steps.jsonl`, `expcore_onedata.fault_relay_late`). It closes with
  `none_seen` only when at least one look and the accounting were read: a window the controller never answered in is
  `unreadable`, never "nothing came".

A start's service step is launched detached and recorded in the state file by the verb itself; it is never looked for.
"""

import contextlib
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import hpcfed_control
from hpclaunch import listings
from hpcslurm import SlurmError
from hpcstate import StateError

from expcore_onedata.fault_relay_late import record


def names(prefix: str, service: str, verb: str, tag: str) -> List[str]:
    """The control-step names one verb of the relay's makes: the verb's words, each stamped with the request's tag."""
    return [hpcfed_control.control_step_name(prefix, service, word, tag) for word in hpcfed_control.CONTROL_WORDS[verb]]


def watch(
    launcher: Any,
    store: Any,
    service: str,
    verb: str,
    tag: str,
    *,
    request: str,
    journal: Path,
    window_s: float,
    poll_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall: Callable[[], float] = time.time,
    since: Optional[float] = None,
) -> int:
    """Watch for the late steps of one verb for `window_s`, cancel each by its exact id, journal what was seen; 0 once
    the window has been watched, 1 when the federation's state cannot be read at the start. `since`: the wall-clock
    moment (epoch seconds) the relay answered the verb; a step the accounting shows started before it ran inside the
    verb, and is no late step (None: every step the accounting shows is counted)."""

    def emit(event: str, **fields: Any) -> None:
        try:
            record(journal, {"event": event, "request": request, **fields, "wall": wall()})
        except OSError as error:  # the journal is gone (the federation was taken down): said once on stderr
            print("late-steps: the journal could not be written (%s)" % type(error).__name__, file=sys.stderr)

    try:
        doc = store.read()
        prefix, job = doc["allocation"]["name"], doc["allocation"]["job"]
        if doc["services"][service]["kind"] != "op":
            raise KeyError(service)
        wanted = names(prefix, service, verb, tag)
    except (StateError, KeyError, TypeError, ValueError, OSError):
        emit("watch_closed", outcome="unreadable", polls_read=0, polls_unread=0, accounting="unread", late_steps=0,
             still_listed=[])  # fmt: skip
        return 1
    slurm = launcher.slurm
    handled: Dict[str, bool] = {}  # a late step's id: whether it is settled (cancelled, or refused as another job's)
    late = read = unread = 0
    still: List[str] = []
    outcome: Optional[str] = None
    deadline = clock() + window_s
    while True:
        found: Dict[str, List[str]] = {}  # kept whatever fails later in this look: a step found is cancelled
        try:
            doc = store.read()
            for each in listings(doc):
                for name, step_id in slurm.steps(each).items():
                    if name in wanted:
                        found.setdefault(name, []).append(step_id)
            read += 1
        except StateError:  # the federation was taken down: nothing is left to start a step in
            outcome = "state_gone"
            break
        except (SlurmError, OSError, KeyError, TypeError, ValueError):
            unread += 1
        try:
            gone = slurm.gone(job)
        except (SlurmError, OSError, TypeError):  # not known to be gone (nor to be there): the window runs on
            gone = False
        for name, ids in found.items():
            for step_id in ids:
                if handled.get(step_id) is True:
                    continue
                try:
                    launcher.cancel(step_id)
                    cancelled, error, settled = True, None, True
                except StateError as refusal:  # another job's id: never cancelled, and said once, not at every look
                    cancelled, error, settled = False, type(refusal).__name__, True
                except Exception as failure:  # a cancel that failed: said, tried again at the next look
                    cancelled, error, settled = False, type(failure).__name__, False
                if step_id not in handled:
                    late += 1
                handled[step_id] = settled
                emit("late_step", step=name, id=step_id, cancelled=cancelled, error=error)
        still = sorted(found)
        if gone:
            outcome = "job_gone"
            break
        if clock() >= deadline:
            break
        sleep(min(poll_s, max(0.0, deadline - clock())))
    accounting, accounted = "unreadable", {}
    if outcome != "state_gone":
        # the accounting stays "unreadable" (never read as an empty one) when it cannot be read
        with contextlib.suppress(SlurmError, OSError, TypeError):
            accounted, accounting = slurm.accounted(job), "read"
    for name in wanted:
        row = accounted.get(name)
        if row is None or str(row["id"]) in handled:
            continue
        began = row.get("start")
        # a Start before the answer is a step that ran inside the verb; a Start the accounting lacks may be a late one
        if since is not None and isinstance(began, (int, float)) and int(began) < int(since):
            emit("accounted_in_time", step=name, id=row["id"], exit=row["exit"], state=row["state"])
            continue
        late += 1
        emit(
            "accounted_late",
            step=name,
            id=row["id"],
            exit=row["exit"],
            state=row["state"],
            start_known=began is not None,
        )
    if outcome is None:
        outcome = "late_steps" if late else ("none_seen" if read and accounting == "read" else "unreadable")
    elif late and outcome == "job_gone":
        outcome = "late_steps"
    emit("watch_closed", outcome=outcome, polls_read=read, polls_unread=unread, accounting=accounting, late_steps=late,
         still_listed=still)  # fmt: skip
    return 0
