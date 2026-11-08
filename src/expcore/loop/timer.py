# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:933-936
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:636-651
"""The run's clock: injected, recorded at every reading, and charging the agent only for its own time (L04).

The agent's time is the elapsed time minus the time its tool calls spent waiting on infrastructure. The wall cap
is checked against that, so a slow store or a busy slot never ends a run as a time cap. An optional ceiling on the
whole elapsed time, waits included, catches an infrastructure that hangs; it is checked after the agent's own cap,
so the agent's cap decides first when both are passed.

Every clock reading is logged with a label. A replay serves the readings back in order instead of reading a clock.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Tick:
    """One reading of the clock and what the run's time looks like at that moment."""

    now: float
    elapsed_s: float
    agent_s: float
    remaining_s: float  # the agent's time left under its cap; zero or less means the cap is reached
    ceiling_passed: bool


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
        self._infra_wait_s = 0.0
        self.readings: list[tuple[str, float]] = []

    @property
    def started(self) -> bool:
        """Tell whether the run has started."""
        return self._start is not None

    @property
    def infra_wait_s(self) -> float:
        """The time tool calls reported waiting on infrastructure, not charged to the agent."""
        return self._infra_wait_s

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

    def add_infra_wait(self, seconds: float) -> None:
        """Take time a tool call spent waiting on infrastructure off the agent's account."""
        self._infra_wait_s += seconds

    def check(self, label: str) -> Tick:
        """Read the clock once and report the agent's time, what is left of the cap, and the ceiling."""
        if self._start is None:
            raise RuntimeError("the timer has not started")
        now = self.read(label)
        elapsed = now - self._start
        agent = elapsed - self._infra_wait_s
        return Tick(
            now,
            elapsed,
            agent,
            self.time_cap_s - agent,
            self.ceiling_s is not None and elapsed >= self.ceiling_s,
        )

    def time_left(self) -> float | None:
        """Seconds of the agent's time left; None before the run starts. This is what a backend's retry policy reads."""
        return None if self._start is None else self.check("time_left").remaining_s

    def since_start(self, now: float) -> float:
        """Return a reading as seconds since the run started."""
        if self._start is None:
            raise RuntimeError("the timer has not started")
        return now - self._start
