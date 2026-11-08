# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/replay.py:262-290
"""The recorded clock, and what replaying it asks of the pieces that stand in for the outside world.

The loop reads the clock at fixed points, and also while it watches a call and while a call waits on infrastructure
(`watch`, `wait_start:<kind>`, `wait_end:<kind>`, `cleanup_end`). A replay serves the recorded readings in order, so it
has to make the same things happen in the same order. The recorded labels say what: after a `wait_start:<kind>` the
call was in a wait of that kind, a `watch` is the loop looking again at the call, a `cleanup_end_timed_out` says the
cancelled work did not stop in time. The replay follows them:

- the call-serving piece (a recorded tool provider, or a recorded backend) walks the waits it finds at the start of its
  call (`replay_waits`), entering and leaving them through the same context the live provider used, and waits for the
  loop's watch to take each `watch` reading in turn;
- the replay's sleep ends exactly when the next recorded reading is a `watch` (`ReplayClock.watch_due`), so the loop's
  watch looks when it looked, and not otherwise;
- the replay's cleanup waiter answers from the recorded label of the reading that ends the cleanup.

Readings labelled `time_left` are not served: the live backend's retry policy took them inside a request, and a
recorded backend never retries.
"""

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from expcore.plugin import check_wait_kind
from expcore.replay.divergence import stop

# The label under which `AgentTimer.time_left` logs its readings.
BACKEND_CLOCK_LABELS = frozenset({"time_left"})
WAIT_START, WAIT_END, WATCH = "wait_start:", "wait_end:", "watch"
CLEANUP_LABELS = ("cleanup_end", "cleanup_end_timed_out")
_YIELDS_FOR_A_READING = 100  # loop iterations the watch is given to take a reading that is due before the replay stops


class ReplayClock:
    """Serves the recorded clock readings one by one, and knows what the next one is for. Used as the loop's clock.

    Create it on the event loop the replay runs on: it holds an `asyncio.Event`.
    """

    def __init__(self, readings: Sequence[Sequence[Any]]):
        kept = [(label, float(value)) for label, value in readings if label not in BACKEND_CLOCK_LABELS]
        self._labels = [label for label, _ in kept]
        self._values = [value for _, value in kept]
        self._read = 0
        self.watch_due = asyncio.Event()  # set while the next recorded reading is a watch reading
        self._signal()

    def _signal(self) -> None:
        if self.next_label() == WATCH:
            self.watch_due.set()
        else:
            self.watch_due.clear()

    def __call__(self) -> float:
        """Return the next recorded reading; the loop reading more often than it did when recorded is a divergence."""
        if self._read >= len(self._values):
            stop("clock", f"the loop read the clock {self._read + 1} times; the record holds {len(self._values)}")
        value = self._values[self._read]
        self._read += 1
        self._signal()
        return value

    @property
    def read_count(self) -> int:
        """How many readings have been served."""
        return self._read

    @property
    def unused(self) -> int:
        """How many recorded readings the replay did not read."""
        return len(self._values) - self._read

    def next_label(self) -> str | None:
        """The label of the reading that will be served next, or None when the record holds no more."""
        return self._labels[self._read] if self._read < len(self._labels) else None

    def waits_ahead(self) -> int:
        """How many `wait_end` readings lie ahead before the first reading that is not a wait or a watch."""
        count = 0
        for label in self._labels[self._read :]:
            if not (label == WATCH or label.startswith((WAIT_START, WAIT_END))):
                break
            count += label.startswith(WAIT_END)
        return count

    def cleanup_timed_out(self) -> bool:
        """Whether the next reading that ends a cleanup is the one that says the cancelled work did not stop."""
        for label in self._labels[self._read :]:
            if label in CLEANUP_LABELS:
                return label == "cleanup_end_timed_out"
        return False

    async def let_watch_read(self) -> None:
        """Yield until the loop's watch has taken the reading that is due; stop the replay if it never does."""
        before = self._read
        for _ in range(_YIELDS_FOR_A_READING):
            await asyncio.sleep(0)
            if self._read != before:
                return
        stop("clock", f"a recorded {WATCH!r} reading (number {before + 1}) was never taken by the loop")


async def replay_waits(
    clock: ReplayClock, enter: Callable[[str], AbstractAsyncContextManager[None]], left_open: int
) -> None:
    """Perform the waits the record shows at the start of a call, and return when the call's own readings are next.

    Each `wait_start:<kind>` enters a wait of that kind through `enter`, each `wait_end:<kind>` leaves the newest wait
    of that kind, and each `watch` waits for the loop to take it. The last `left_open` `wait_end` readings were taken by
    the loop, which closes a wait its call left open: those waits are left open here too, so the loop closes them.
    """
    open_waits: list[tuple[str, AbstractAsyncContextManager[None]]] = []
    while True:
        label = clock.next_label() or ""
        if label == WATCH:
            await clock.let_watch_read()
        elif label.startswith(WAIT_START):
            kind = label[len(WAIT_START) :]
            try:
                wait = enter(check_wait_kind(kind))
            except ValueError as exc:
                stop("clock", f"the recorded wait has a kind the core does not know ({exc})")
            await wait.__aenter__()
            open_waits.append((kind, wait))
        elif label.startswith(WAIT_END):
            if clock.waits_ahead() <= left_open:
                return
            kind = label[len(WAIT_END) :]
            match = next((i for i in range(len(open_waits) - 1, -1, -1) if open_waits[i][0] == kind), None)
            if match is None:
                stop("clock", f"the record ends a {kind!r} wait that it never started")
            await open_waits.pop(match)[1].__aexit__(None, None, None)
        else:
            return
