# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:933-936
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:636-651
"""The run's clock: injected, recorded at every reading, and charging the agent only for its own time (L04).

The agent's time is the elapsed time minus the time spent waiting on infrastructure and minus the time spent
cleaning up after a cut. Waits are measured live: a provider (or a model backend) wraps each one in
`infrastructure_wait(kind)`, with a kind from the closed `WAIT_KINDS`, and the agent's clock is paused while any is
open, so a slow queue or a starting sandbox never ends a run as a time cap and nothing has to be reported afterwards.
The seconds are kept per kind as well as in total. The ceiling on the whole elapsed time, waits included,
catches an infrastructure that hangs; it is checked after the agent's own cap, so the agent's cap decides first.

When the loop cuts a call it begins the cleanup: from the moment of the cut, the time the cancelled work takes to
stop is the harness's own (`cleanup_s`) and is never added to the agent's time. So the agent's time, the waits and
the cleanup always add up to the elapsed time.

Waits belong to their call: when a call ends the loop closes any wait it left open (`close_open_waits`), at that
moment and as infrastructure time, and the call's record says how many it was.

Every clock reading is logged with a label, the live waits' and the cleanup's included. A replay serves the readings
back in order instead of reading a clock. Whether the cleanup of a cancelled call ran out of its deadline is decided by
an injectable `cleanup_waiter`, and the reading that ends the cleanup carries the answer in its label (`cleanup_end`,
or `cleanup_end_timed_out`), so a replay can inject a waiter that serves it.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from expcore.plugin import check_wait_kind

# How the loop waits for the tasks it cancelled: given the tasks and the cleanup deadline in seconds, return True when
# the deadline ran out with some task still running. It may await the tasks, and it must not hide that one is running.
CleanupWaiter = Callable[[Collection[asyncio.Future], float], Awaitable[bool]]


async def real_time_cleanup_waiter(tasks: Collection[asyncio.Future], deadline_s: float) -> bool:
    """The default cleanup waiter: wait up to `deadline_s` of real time; True when some task is still running."""
    if not tasks:
        return False
    _, pending = await asyncio.wait(tasks, timeout=deadline_s)
    return bool(pending)


def waited_since(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    """Return the seconds waited per kind between two readings of `AgentTimer.infra_wait_by_kind`, kinds that grew."""
    return {kind: after[kind] - before.get(kind, 0.0) for kind in after if after[kind] > before.get(kind, 0.0)}


@dataclass(frozen=True)
class Tick:
    """One reading of the clock and what the run's time looks like at that moment."""

    now: float
    elapsed_s: float
    agent_s: float
    remaining_s: float  # the agent's time left under its cap; zero or less means the cap is reached
    ceiling_passed: bool
    ceiling_left_s: float | None = None  # the whole-time ceiling minus the elapsed time; None without a ceiling
    paused: bool = False  # a wait on infrastructure is open at this reading: the agent's clock is not running

    @property
    def watch_s(self) -> float:
        """How long until a call under watch could next be over a limit: the agent's time left (not while the agent's
        clock is paused) or the ceiling left, whichever comes first. A call is checked again at least then (L04)."""
        left = [] if self.paused else [self.remaining_s]
        if self.ceiling_left_s is not None:
            left.append(self.ceiling_left_s)
        return max(min(left), 0.0) if left else float("inf")


class AgentTimer:
    """Counts the agent's time against its cap, from an injected clock."""

    def __init__(
        self,
        time_cap_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        ceiling_s: float | None = None,
        cleanup_waiter: CleanupWaiter | None = None,
    ):
        if time_cap_s <= 0:
            raise ValueError(f"the time cap must be positive, got {time_cap_s!r}")
        if ceiling_s is not None and ceiling_s < time_cap_s:
            raise ValueError("the ceiling on the whole elapsed time cannot be below the agent's time cap")
        self.time_cap_s = time_cap_s
        self.ceiling_s = ceiling_s
        self._clock = clock
        self._start: float | None = None
        self._open_waits: dict[_Wait, None] = {}  # in the order they opened; the agent's clock runs only while none is
        self._wait_since: float | None = None  # when the first open wait began
        self._kind_open: dict[str, int] = {}  # open waits per kind
        self._kind_since: dict[str, float] = {}  # when the first open wait of each open kind began
        self._by_kind: dict[str, float] = {}  # seconds in waits of each kind that have ended: a kind's own union
        self._listeners: list[Callable[[], None]] = []  # told when a wait opens or closes the pause
        self._infra_wait_s = 0.0  # closed waits
        self._cleanup_from: float | None = None  # when the cleanup after a cut began, while it lasts
        self._cleanup_s = 0.0
        self._over = False  # a cut began its cleanup: no wait is counted from then on
        self.cleanup_timed_out = False  # the cancelled work did not stop within the cleanup deadline
        self.cleanup_waiter: CleanupWaiter = cleanup_waiter or real_time_cleanup_waiter
        self.readings: list[tuple[str, float]] = []

    @property
    def started(self) -> bool:
        """Tell whether the run has started."""
        return self._start is not None

    @property
    def infra_wait_s(self) -> float:
        """The time spent in waits on infrastructure that have ended, which is not charged to the agent."""
        return self._infra_wait_s

    @property
    def infra_wait_by_kind(self) -> dict[str, float]:
        """The seconds of waits that have ended, by kind. A kind's figure is the union of its own waits, so two kinds
        that overlap are each counted for the whole overlap and the figures can add up to more than `infra_wait_s`,
        which is the union of all (L04)."""
        return dict(self._by_kind)

    @property
    def cleanup_s(self) -> float:
        """The time spent stopping cancelled work after a cut, which is harness time and not the agent's."""
        return self._cleanup_s

    def read(self, label: str) -> float:
        """Read the clock and log the reading under a label."""
        value = self._clock()
        self.readings.append((label, value))
        return value

    def start(self) -> None:
        """Start the run's time. Starting twice is a programming error."""
        if self._start is not None:
            raise RuntimeError("the timer has already started")
        self._start = self.read("start")

    # --- waits on infrastructure, measured live -------------------------------------------------------------------

    def infrastructure_wait(self, kind: str) -> AbstractAsyncContextManager[None]:
        """Return a context manager that pauses the agent's clock while it is open, for a wait of one of the
        `WAIT_KINDS`; any other kind raises ValueError here, at the call, naming it (L04).

        This is what a provider reaches through its call context, and a model backend can use it directly. Waits may
        nest or overlap; the clock is paused for their union, and each kind is also counted on its own. Every wait
        takes a reading when it opens and when it closes, labelled `wait_start:<kind>` and `wait_end:<kind>`. Before
        the run starts, or after a cut has begun its cleanup, it measures nothing. A wait still open when its call
        ends is closed by the loop (`close_open_waits`).
        """
        return _Wait(self, check_wait_kind(kind))

    def _open_wait(self, wait: "_Wait") -> None:
        if self._start is None or self._over:
            return
        now = self.read(f"wait_start:{wait.kind}")
        first = not self._open_waits
        self._open_waits[wait] = None
        if not self._kind_open.get(wait.kind):
            self._kind_since[wait.kind] = now
        self._kind_open[wait.kind] = self._kind_open.get(wait.kind, 0) + 1
        if first:
            self._wait_since = now
            self._notify()

    def _release(self, wait: "_Wait", now: float) -> None:
        """Take a wait out of the open ones at `now`; the last of its kind ends the kind's span."""
        del self._open_waits[wait]
        self._kind_open[wait.kind] -= 1
        if not self._kind_open[wait.kind]:
            del self._kind_open[wait.kind]
            self._by_kind[wait.kind] = self._by_kind.get(wait.kind, 0.0) + now - self._kind_since.pop(wait.kind)

    def _close_wait(self, wait: "_Wait") -> None:
        if wait not in self._open_waits:  # never opened, or closed already by the end of its call or by a cut
            return
        now = self.read(f"wait_end:{wait.kind}")
        self._release(wait, now)
        if not self._open_waits and self._wait_since is not None:
            self._infra_wait_s += now - self._wait_since
            self._wait_since = None
            self._notify()

    def close_open_waits(self) -> int:
        """Close every wait still open, now, and return how many there were. The time they were open is infrastructure
        time, since the provider declared it so, and the agent's clock runs again. The loop does this when a call ends,
        so a wait is scoped to its call; the one that closes it later finds it closed and does nothing. Each wait takes
        a reading of its own, in the order they opened."""
        left = len(self._open_waits)
        now = 0.0
        for wait in list(self._open_waits):
            now = self.read(f"wait_end:{wait.kind}")
            self._release(wait, now)
        if left and self._wait_since is not None:
            self._infra_wait_s += now - self._wait_since
            self._wait_since = None
        return left

    def on_wait_change(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` whenever a wait pauses or resumes the agent's clock; return the function that removes it.

        The loop's watcher uses it to look again, since a pause moves the moment the agent's cap would be reached."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # --- the cleanup after a cut ----------------------------------------------------------------------------------

    def begin_cleanup(self, tick: Tick) -> None:
        """Mark the moment a call was cut: waits still open end here, and the time from here until `end_cleanup` is
        harness time. Takes no reading of its own; the cut's reading is the one that decided it."""
        if self._over:
            return
        self._over = True
        for wait in list(self._open_waits):
            self._release(wait, tick.now)
        if self._wait_since is not None:
            self._infra_wait_s += tick.now - self._wait_since
            self._wait_since = None
        self._cleanup_from = tick.now

    def end_cleanup(self, *, timed_out: bool = False) -> None:
        """Mark the moment the cancelled work has stopped, or been given up on when `timed_out`. The reading's label
        says which (`cleanup_end` or `cleanup_end_timed_out`), so a replay can serve the answer back."""
        if self._cleanup_from is not None:
            self.cleanup_timed_out = self.cleanup_timed_out or timed_out
            self._cleanup_s += self.read("cleanup_end_timed_out" if timed_out else "cleanup_end") - self._cleanup_from
            self._cleanup_from = None

    @property
    def cleaning(self) -> bool:
        """Tell whether a cut has begun its cleanup and it has not ended."""
        return self._cleanup_from is not None

    # --- readings -------------------------------------------------------------------------------------------------

    def check(self, label: str) -> Tick:
        """Read the clock once and report the agent's time, what is left of the cap, and the ceiling."""
        if self._start is None:
            raise RuntimeError("the timer has not started")
        now = self.read(label)
        elapsed = now - self._start
        waited = self._infra_wait_s + (now - self._wait_since if self._wait_since is not None else 0.0)
        cleaned = self._cleanup_s + (now - self._cleanup_from if self._cleanup_from is not None else 0.0)
        agent = elapsed - waited - cleaned
        return Tick(
            now,
            elapsed,
            agent,
            self.time_cap_s - agent,
            self.ceiling_s is not None and elapsed >= self.ceiling_s,
            None if self.ceiling_s is None else self.ceiling_s - elapsed,
            bool(self._open_waits),
        )

    def time_left(self) -> float | None:
        """Seconds of the agent's time left; None before the run starts. This is what a backend's retry policy reads."""
        return None if self._start is None else self.check("time_left").remaining_s

    def since_start(self, now: float) -> float:
        """Return a reading as seconds since the run started."""
        if self._start is None:
            raise RuntimeError("the timer has not started")
        return now - self._start


class WaitContext:
    """The call context the loop hands a provider: it can mark waits on infrastructure and nothing else of the timer."""

    def __init__(self, timer: AgentTimer):
        self._timer = timer

    def infrastructure_wait(self, kind: str) -> AbstractAsyncContextManager[None]:
        """Pause the agent's clock while the returned context manager is open, for a wait of a known kind (L04)."""
        return self._timer.infrastructure_wait(kind)


class _Wait:
    """One wait on infrastructure: an async context manager that pauses the agent's clock while it is open."""

    def __init__(self, timer: AgentTimer, kind: str):
        self._timer = timer
        self.kind = kind

    async def __aenter__(self) -> None:
        self._timer._open_wait(self)

    async def __aexit__(self, *exc: object) -> None:
        self._timer._close_wait(self)
