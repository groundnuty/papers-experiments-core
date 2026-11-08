# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:636-651
"""Running one call under watch: it is cut when the agent's own time or the whole-time ceiling is used up (L04).

The watch reads the injected clock through the timer, so every reading is logged, and waits on the injected sleep. It
sleeps until the next moment a limit could be passed and then reads the clock and judges: the agent's time (which
leaves out waits on infrastructure that are open, because the agent's clock is paused in them) against what is left of
its cap, and the total time against the ceiling. Waking early, for instance because a wait paused the agent's clock,
only costs another reading; a limit is never passed unseen, because the agent's deadline can move later but not earlier.

A call that finishes first returns its result, whatever the clock says after the fact. A call that is cut is cancelled
and awaited, and the time that takes is cleanup, which the timer keeps apart from the agent's time. The same cleanup
runs when the whole run is cancelled from outside, and then the cancellation goes on to the caller.
"""

import asyncio
from collections.abc import Awaitable, Callable

from expcore.loop.timer import AgentTimer, Tick


class Cut(Exception):
    """A call was cancelled because a limit passed. `tick` is the reading that showed it."""

    def __init__(self, tick: Tick):
        super().__init__("a limit passed while the call was running")
        self.tick = tick


class Watcher:
    """Runs calls under watch, on the run's timer and its injected sleep."""

    def __init__(self, timer: AgentTimer, sleep: Callable[[float], Awaitable[None]]):
        self._timer = timer
        self._sleep = sleep

    async def run[T](self, work: Awaitable[T], tick: Tick) -> T:
        """Await `work`, started right after the reading `tick`; raise `Cut` when a limit passes first.

        Whichever way this ends, both child tasks have been cancelled and awaited before it returns or raises, so none
        is left pending and a cancelled call has had its chance to clean up.
        """
        job = asyncio.ensure_future(work)
        sleeper = asyncio.ensure_future(self._sleep(tick.watch_s))  # asked for before the work starts
        try:
            while True:
                await asyncio.wait({job, sleeper}, return_when=asyncio.FIRST_COMPLETED)
                if job.done():
                    return job.result()
                sleeper.result()
                now = self._timer.check("watch")
                if now.remaining_s <= 0 or now.ceiling_passed:
                    self._timer.begin_cleanup(now)
                    raise Cut(now)
                sleeper = asyncio.ensure_future(self._sleep(now.watch_s))
        except asyncio.CancelledError:
            if not self._timer.cleaning:  # cancelled from outside: stopping the call is cleanup too
                self._timer.begin_cleanup(self._timer.check("cancelled"))
            raise
        finally:
            await self._reap(job, sleeper)

    async def _reap(self, *tasks: asyncio.Future) -> None:
        """Cancel the tasks and wait until they have stopped, then close the cleanup the cut or the cancel began."""
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._timer.end_cleanup()
