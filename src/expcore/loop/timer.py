# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:933-936
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:636-651
"""The run's clock: injected, recorded at every reading, and charging the agent only for its own time (L04).

The agent's time is the elapsed time minus the time spent waiting on infrastructure and minus the time spent
cleaning up after a cut. Waits are measured live: a provider (or a model backend) wraps each one in
`infrastructure_wait()`, and the agent's clock is paused while any is open, so a slow store or a busy slot never ends a
run as a time cap and nothing has to be reported afterwards. The ceiling on the whole elapsed time, waits included,
catches an infrastructure that hangs; it is checked after the agent's own cap, so the agent's cap decides first.

When the loop cuts a call it begins the cleanup: from the moment of the cut, the time the cancelled work takes to
stop is the harness's own (`cleanup_s`) and is never added to the agent's time. So the agent's time, the waits and
the cleanup always add up to the elapsed time.

Every clock reading is logged with a label, the live waits' and the cleanup's included. A replay serves the readings
back in order instead of reading a clock.
"""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass


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
    ):
        if time_cap_s <= 0:
            raise ValueError(f"the time cap must be positive, got {time_cap_s!r}")
        if ceiling_s is not None and ceiling_s < time_cap_s:
            raise ValueError("the ceiling on the whole elapsed time cannot be below the agent's time cap")
        self.time_cap_s = time_cap_s
        self.ceiling_s = ceiling_s
        self._clock = clock
        self._start: float | None = None
        self._wait_depth = 0  # open waits; the agent's clock runs only at zero
        self._wait_since: float | None = None  # when the first open wait began
        self._infra_wait_s = 0.0  # closed waits
        self._cleanup_from: float | None = None  # when the cleanup after a cut began, while it lasts
        self._cleanup_s = 0.0
        self._over = False  # a cut began its cleanup: no wait is counted from then on
        self.cleanup_timed_out = False  # the cancelled work did not stop within the cleanup deadline
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

    def infrastructure_wait(self) -> AbstractAsyncContextManager[None]:
        """Return a context manager that pauses the agent's clock while it is open (L04).

        This is what a provider reaches through its call context, and a model backend can use it directly. Waits may
        nest or overlap; the clock is paused for their union. Before the run starts, or after a cut has begun its
        cleanup, it measures nothing.
        """
        return self._wait()

    @asynccontextmanager
    async def _wait(self) -> AsyncIterator[None]:
        counted = self._enter_wait()
        try:
            yield
        finally:
            if counted:
                self._exit_wait()

    def _enter_wait(self) -> bool:
        if self._start is None or self._over:
            return False
        if self._wait_depth == 0:
            self._wait_since = self.read("wait_start")
        self._wait_depth += 1
        return True

    def _exit_wait(self) -> None:
        if self._over or self._wait_depth == 0:  # a cut closed every wait at the cut
            return
        self._wait_depth -= 1
        if self._wait_depth == 0 and self._wait_since is not None:
            self._infra_wait_s += self.read("wait_end") - self._wait_since
            self._wait_since = None

    # --- the cleanup after a cut ----------------------------------------------------------------------------------

    def begin_cleanup(self, tick: Tick) -> None:
        """Mark the moment a call was cut: waits still open end here, and the time from here until `end_cleanup` is
        harness time. Takes no reading of its own; the cut's reading is the one that decided it."""
        if self._over:
            return
        self._over = True
        if self._wait_since is not None:
            self._infra_wait_s += tick.now - self._wait_since
            self._wait_since, self._wait_depth = None, 0
        self._cleanup_from = tick.now

    def end_cleanup(self) -> None:
        """Mark the moment the cancelled work has stopped, or been given up on."""
        if self._cleanup_from is not None:
            self._cleanup_s += self.read("cleanup_end") - self._cleanup_from
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
            self._wait_depth > 0,
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

    def infrastructure_wait(self) -> AbstractAsyncContextManager[None]:
        """Pause the agent's clock while the returned context manager is open (L04)."""
        return self._timer.infrastructure_wait()
