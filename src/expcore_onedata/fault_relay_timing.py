# Provenance: new
"""Where a relayed verb's time went: the time from its issue to each of its steps' accounted Start, and from its
answer to each step's accounted End (O58).

A step the Slurm controller is slow to make (it can stall silently for its message timeout, about 200 s on the site)
starts long after the verb was issued, and a step that ends after the relay has answered ran late. Both are the site's,
not the run's, and a record that does not say so reads as a fault of the study. So after each verb ends, the relay reads
the accounting once (`sacct -j <job>`, bounded, never polled) and gives each of the verb's steps, found by its exact
name, two figures in the answer's `timing`:

- `start_after_issue_s`: the step's accounted Start minus the moment the relay issued the verb;
- `end_after_answer_s`: the step's accounted End minus the moment the relay's answer was ready (negative when the step
  ended first, the usual case).

A verb's steps are the control steps it names with the relay's tag (`fault_relay_late.CONTROL_WORDS`: a stop's
teardown, a status and its read-only check, an exec, which is a link restart or a ping) and, for a start that launched,
the service step it recorded in the state file (`<allocation>-<service>-run-<n>`). sacct prints whole seconds in the
login node's local time, so the figures are good to about a second.

A figure that cannot be given is None, and `unknown` says why (`not_recorded`, `no_such_step`, `unparsable`); a reading
that cannot be made says so for the whole answer (`accounting`: `unreadable`, `skipped`; `reason`). A timing never
fails a verb and never changes its answer: nothing here raises.
"""

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from expcore_onedata.compose import CommandResult
from expcore_onedata.fault_relay_late import CONTROL_WORDS, step_name

SACCT_TIMEOUT_S = 10.0  # the one accounting call's bound
MIN_BUDGET_S = 1.0  # less time than this before the client's last look: the reading is skipped, never started
RESOLUTION_S = 1  # sacct prints whole seconds
SACCT_FIELDS = "JobID,JobName,Start,End,State,ExitCode"
_STEP_ID = re.compile(r"[0-9]{1,12}(?:\+[0-9]{1,6})?\.[0-9]{1,9}")
_STATE = re.compile(r"[A-Z_]{1,32}")
_NOT_RECORDED = ("", "Unknown", "None", "N/A")

Sacct = Callable[[Sequence[str], float], CommandResult]  # (argv, bound) -> the command's end; raises what a runner does


def step_names(
    prefix: str, service: str, verb: str, tag: str, service_step: str | None = None
) -> list[tuple[str, str]]:
    """`(word, name)` of each step the verb is known to make: its control words stamped with the tag, then, for a start
    that launched, the service step (`run`)."""
    found = [(word, step_name(prefix, service, word, tag)) for word in CONTROL_WORDS[verb]]
    return found if service_step is None else [*found, ("run", service_step)]


def _moment(text: str) -> float | None:
    """A sacct time (`2026-11-17T09:41:13`, the login node's local time) as epoch seconds; None when it holds none."""
    if text in _NOT_RECORDED:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").timestamp()
    except ValueError:
        return None


def _figure(moment: float | None, since: float) -> float | None:
    return None if moment is None else round(moment - since, 3)


def _state_word(text: str) -> str | None:
    """A step's state as a word (`CANCELLED by 1234` is `CANCELLED`), None for anything else."""
    words = text.split()
    return words[0] if words and _STATE.fullmatch(words[0]) else None


def _missing(word: str, name: str, why: str) -> dict[str, Any]:
    """A step with no figure at all: it is not in the accounting, or the accounting could not be read."""
    return {
        "word": word,
        "name": name,
        "id": None,
        "state": None,
        "start_after_issue_s": None,
        "end_after_answer_s": None,
        "unknown": {"start_after_issue_s": why, "end_after_answer_s": why},
    }


def _row(word: str, name: str, record: Sequence[str], issued: float, answered: float) -> dict[str, Any]:
    """A step from its accounting record (`SACCT_FIELDS`): both figures, or None for each that is not recorded."""
    step_id, _, start, end, state, _ = record
    begun, ended = _moment(start), _moment(end)
    unknown: dict[str, str] = {}
    if begun is None:
        unknown["start_after_issue_s"] = "not_recorded" if start in _NOT_RECORDED else "unparsable"
    if ended is None:
        unknown["end_after_answer_s"] = "not_recorded" if end in _NOT_RECORDED else "unparsable"
    row: dict[str, Any] = {
        "word": word,
        "name": name,
        "id": step_id,
        "state": _state_word(state),
        "start_after_issue_s": _figure(begun, issued),
        "end_after_answer_s": _figure(ended, answered),
    }
    return {**row, **({"unknown": unknown} if unknown else {})}


def read_timing(
    sacct: Sacct,
    *,
    job: str | None,
    names: Sequence[tuple[str, str]],
    issued: float,
    answered: float,
    budget_s: float = SACCT_TIMEOUT_S,
) -> dict[str, Any]:
    """The answer's `timing` for a verb issued at `issued` and answered at `answered` (wall clock, epoch seconds): one
    accounting call, bounded by `budget_s` (at most `SACCT_TIMEOUT_S`), then each of `names` found in it by its exact
    name. Never raises."""
    timing: dict[str, Any] = {
        "resolution_s": RESOLUTION_S,
        "issued_wall": round(issued, 3),
        "answered_wall": round(answered, 3),
        "accounting": "read",
        "reason": None,
        "steps": [],
    }

    def unread(accounting: str, reason: str) -> dict[str, Any]:
        steps = [_missing(word, name, reason) for word, name in names]
        return {**timing, "accounting": accounting, "reason": reason, "steps": steps}

    if job is None or not re.fullmatch(r"[0-9]{1,12}", job):
        return unread("skipped", "no_job")
    if budget_s < MIN_BUDGET_S:
        return unread("skipped", "no_time_left")
    try:
        done = sacct(["sacct", "-j", job, "-n", "-P", "-o", SACCT_FIELDS], min(budget_s, SACCT_TIMEOUT_S))
    except subprocess.TimeoutExpired:
        return unread("unreadable", "sacct_timeout")
    except OSError:
        return unread("unreadable", "sacct_not_run")
    except Exception as error:  # a runner that fails some other way: a reason by its type, never its message
        return unread("unreadable", f"sacct_{type(error).__name__}"[:60])
    if done.returncode != 0:
        return unread("unreadable", "sacct_failed")
    records: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        parts = line.split("|")
        if len(parts) == 6 and _STEP_ID.fullmatch(parts[0]):
            records.setdefault(parts[1], parts)
    for word, name in names:
        record = records.get(name)
        timing["steps"].append(
            _missing(word, name, "no_such_step") if record is None else _row(word, name, record, issued, answered)
        )
    return timing


def is_timing(value: Any) -> bool:
    """Whether an answer's `timing` is the object `read_timing` writes (a client reads another's file: it checks)."""
    return isinstance(value, Mapping) and isinstance(value.get("steps"), list) and "accounting" in value
