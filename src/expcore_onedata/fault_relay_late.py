# Provenance: new
"""The late steps of a verb the fault relay answered `timed_out` (O58): the tag that names them, the watch that cancels
them, and the journal that records them.

Ending a verb's process group at its bound (`fault_relay_group`) leaves one gap: an `srun` the controller has already
been asked for may still be made into a step. So the relay does two things for each verb.

- **It stamps the verb's steps.** The relay derives a tag of 12 hex digits from its own nonce and the request's id and
  gives it to the verb (`hpcfed.py --step-tag <tag> <verb> <service>`), which names each control step it launches
  `<allocation>-<service>-<word>-<tag>` (`word`: status, mounts, stop or exec). The name is unique to the request: no
  other request, and no verb run by hand (whose steps are stamped with the clock), can share it.
- **After a timed-out verb it starts a watch.** `hpcfed.py late-steps` (outside `fault_hpc.VERBS`: no request can name
  it) looks for a step of one of those exact names, in the federation's own job alone, for `LATE_WINDOW_S` (twice the
  site's controller message timeout) from the bound, cancels each by its exact `<job>.<step>` id, and reads the
  accounting at the window's end for a step that came and went between two looks. The answer stays `timed_out`: the
  injector still reads the verb's outcome as unknown.

**The journal** is `late-steps.jsonl` in the relay's directory (0600, one JSON object a line, appended and fsynced).
The relay writes `watch_opened` before it starts the watch, so a relay that dies in the window still leaves the record
that a watch was owed, and `watch_failed` when it cannot start it; the watch writes `late_step` (a step it found, and
whether its cancel worked), `accounted_late` (a step that ended uncancelled before a look) and `watch_closed` (its
outcome: `late_steps`, `none_seen` only when it read at least one look and the accounting, `unreadable` otherwise,
`job_gone`, `state_gone`). A `watch_opened` with no `watch_closed` is a watch that was cut short (its process
killed): its window is unwatched from then on. The cancel is best-effort, a step may have run before it was found, and
the record is what is guaranteed.

A start's service step (`<allocation>-<service>-run-<n>`) is launched detached by the verb, in a session of its own, and
recorded in the federation's state file: it is the start itself arriving, never a step to cancel by name.

This module imports nothing of the core: the watch (`hpcfed.py late-steps`) imports it for `record`, and stays light.
"""

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

STEP_TAG_OPTION = "--step-tag"  # hpcfed.py's option before a verb: the relay's tag for the steps the verb launches
JOURNAL = "late-steps.jsonl"
WATCH_COMMAND = "late-steps"  # a command of `hpcfed.py` the relay alone starts; never a verb a request can name
LATE_WINDOW_S = 400.0  # twice the site's controller message timeout (about 200 s), counted from the verb's bound
LATE_POLL_S = 5.0  # between two looks at the job's steps
# hpcfed ends one of its own `srun` clients only after the step's own bound has passed (`Slurm._finish`: the read-only
# check's 30 s is the shortest), and the controller may still hold that step: so a verb that ran this long, whatever its
# exit status, may have left a step pending, and is watched as a verb that timed out (a test keeps this below those
# bounds, `hpcfed_control.*_TIMEOUT_S` and the stop's margin).
LATE_ELAPSED_S = 25.0
# The words of the control steps each verb launches (`hpcfed_control.CONTROL_WORDS`, which a test holds equal): a step
# is named `<allocation>-<service>-<word>-<tag>`. A start's service step (`...-run-<n>`) is no control step.
CONTROL_WORDS: dict[str, tuple[str, ...]] = {
    "status": ("status", "mounts"),
    "stop": ("stop",),
    "start": ("status",),
    "exec": ("exec",),
}
_RELAY_DIR = "fault-relay"  # `fault_relay.RELAY_DIR`; a test holds the two together


def step_tag(nonce: str, key: str) -> str:
    """The tag of one request's steps: 12 hex digits of a hash of the relay's nonce and the request's id."""
    return hashlib.sha256(f"{nonce}:{key}".encode()).hexdigest()[:12]


def step_name(prefix: str, service: str, word: str, tag: str) -> str:
    """The name hpcfed gives a control step it launches for a verb with the relay's tag."""
    return f"{prefix}-{service}-{word}-{tag}"


def journal_path(root: Path) -> Path:
    return Path(root) / _RELAY_DIR / JOURNAL


def watch_argv(
    prefix: Sequence[str], tag: str, verb: str, service: str, key: str, window_s: float, poll_s: float
) -> list[str]:
    """The watch as `hpcfed.py` is given it: the relay's prefix, the tag, then `late-steps <service>` and what it
    needs. No journal path: `hpcfed.py` knows the relay's journal from the root it was given."""
    return [
        *prefix,
        STEP_TAG_OPTION,
        tag,
        WATCH_COMMAND,
        service,
        "--for-verb",
        verb,
        "--request",
        key,
        "--window-s",
        str(float(window_s)),
        "--poll-s",
        str(float(poll_s)),
    ]


def record(journal: Path, event: Mapping[str, Any]) -> None:
    """Append one event to the journal (a line, 0600, no link followed) and force it to disk."""
    data = (json.dumps(dict(event), sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def spawn_watch(argv: Sequence[str], env: Mapping[str, str]) -> "subprocess.Popen[bytes]":
    """Start the watch detached, in a session of its own: it outlives a relay that stops or dies, since a step can still
    be made after either, and it journals for itself."""
    return subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(env),
        start_new_session=True,
    )
