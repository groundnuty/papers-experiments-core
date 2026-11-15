# Provenance: new
"""What a run's pre-run health check leaves for the same try's run admission (O35).

A run's pre-run check and its run admission can be far apart: the run waits for a lease, then for the platform's
admission, while other runs, checks and repairs go on. A platform whose admission must know what the check saw (the
fault gate's state at the check, `expcore_onedata.fault_gate`) notes it here. The attempt runner opens one note for each
try of a run it checks before it runs (`opened`, around steps 1 to 7 of `expcore.campaign.attempt`). The health gate
runs the check in a thread of its own, in a copy of the worker's context (`expcore.campaign.healthgate`), and that copy
refers to the same note, so what the check writes there, the admission, entered in the worker's own context, reads.

A note is a dictionary keyed by names the platform chooses. There is none (`current()` is None) outside a try: the
dispatcher's own check, and a caller that is no campaign. A try of a campaign with no health check has a note too, with
the one key `UNCHECKED`, since nothing will write to it: an admission can tell such a try from a caller that is no
campaign. A platform whose admission depends on the note declares that it needs a health check
(`expcore.campaign.health_required`), so the scheduler refuses a campaign without one when it is built; the mark is what
that platform's admission reads when the refusal at load never saw the declaration (an admission a study wrapped
without it). The post-run check runs inside the same try and sees the note too, so a platform notes from its pre-run
check alone.
"""

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

_NOTE: ContextVar[dict[str, Any] | None] = ContextVar("expcore_prerun_note", default=None)
UNCHECKED = "unchecked"  # the key of a note whose campaign has no health check: no pre-run check will write to it


@contextlib.contextmanager
def opened(*, checked: bool = True) -> Iterator[dict[str, Any]]:
    """Open a fresh note for one try of a run, for the body of a `with`. With `checked=False` (the campaign has no
    health check, so nothing will write to it) the note holds `UNCHECKED`: True."""
    note: dict[str, Any] = {} if checked else {UNCHECKED: True}
    token = _NOTE.set(note)
    try:
        yield note
    finally:
        _NOTE.reset(token)


def current() -> dict[str, Any] | None:
    """The note of the try the running context is in, or None outside one."""
    return _NOTE.get()


def note(key: str, value: Any) -> bool:
    """Note `value` under `key` for the try the running context is in; whether there was one to note it in."""
    found = _NOTE.get()
    if found is None:
        return False
    found[key] = value
    return True
