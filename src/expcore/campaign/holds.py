# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/campaign/scheduler.py:1307-1522
"""The hold registry: why runs are not starting, said once when it changes.

The imported dispatcher interleaved about fifteen study holds in one function. Here every reason a run may not start
is a `Hold` on a *subject*: `all` (every run), a model, or a unit. The dispatcher sets or clears the hold of a subject
as it looks at the runs, and the registry writes an event only when a subject's hold appears, changes or goes: a
campaign waiting out a pause does not fill its log with the same sentence each pass. A caller that wants to say so as
it happens (a command line) passes `on_change`, which is called at the same moments, once per change, never per pass.

A hold is **temporary** when it ends by itself (a unit's pause, an outage the dispatcher re-checks) and permanent
otherwise (a stop, a budget, an unpriced model, a model not admitted): a permanent hold needs someone to look.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass

from expcore.campaign.store import Store

ALL = "all"


@dataclass(frozen=True)
class Hold:
    """A reason a subject's runs do not start: its kind (a short word), the sentence, and whether it ends by itself."""

    kind: str
    reason: str
    temporary: bool = False


class HoldRegistry:
    """The holds in force, with an event in the store whenever one begins, changes or ends."""

    def __init__(
        self,
        store: Store,
        stamp: Callable[[], str],
        on_change: Callable[[str, Hold | None, Hold | None], None] | None = None,
    ):
        self.store, self._stamp, self._on_change = store, stamp, on_change
        self._lock = threading.Lock()
        self._held: dict[str, Hold] = {}

    def note(self, subject: str, hold: Hold | None) -> None:
        """Set the subject's hold, or clear it with None; write an event if that changes what was in force."""
        with self._lock:
            before = self._held.get(subject)
            if hold == before:
                return
            if hold is None:
                del self._held[subject]
            else:
                self._held[subject] = hold
        model = None if subject == ALL or ":" in subject else subject
        if hold is None and before is not None:
            self.store.event(self._stamp(), "hold_lifted", model, f"{subject}: {before.kind} hold ended")
        elif hold is not None:
            self.store.event(self._stamp(), "hold", model, f"{subject}: {hold.kind}: {hold.reason}")
        if self._on_change is not None:
            self._on_change(subject, before, hold)

    def current(self) -> dict[str, Hold]:
        """Return the holds in force now."""
        with self._lock:
            return dict(self._held)

    def blocked(self) -> bool:
        """Tell whether something holds runs that will not end by itself: someone has to look."""
        with self._lock:
            return any(not h.temporary for h in self._held.values())
