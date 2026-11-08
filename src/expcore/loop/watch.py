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

Stopping a call may take for ever (a cleanup that waits on something that never comes), and a cancellation must always
end. So the wait for a cancelled call has a deadline in real time; past it the call is given up on and left pending,
the timer records `cleanup_timed_out`, and the cut or the cancellation goes on. The event loop's own shutdown cancels
what is left.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from expcore.loop.timer import AgentTimer, Tick

log = logging.getLogger(__name__)


class Cut(Exception):
    """A call was cancelled because a limit passed. `tick` is the reading that showed it."""

    def __init__(self, tick: Tick):
        super().__init__("a limit passed while the call was running")
        self.tick = tick


class Watcher:
    """Runs calls under watch, on the run's timer and its injected sleep."""

    def __init__(self, timer: AgentTimer, sleep: Callable[[float], Awaitable[None]], *, cleanup_deadline_s: float):
        self._timer = timer
        self._sleep = sleep
        self._cleanup_deadline_s = cleanup_deadline_s

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
        """Cancel the tasks and wait, up to the cleanup deadline, until they have stopped; give up on any that has not.

        Then close the cleanup that the cut or the cancellation began.
        """
        for task in tasks:
            task.cancel()
        pending = {task for task in tasks if not task.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self._cleanup_deadline_s)
        for task in tasks:
            if task in pending:
                task.add_done_callback(_consume)  # given up on: whatever it ends with, nobody is waiting for it
            elif not task.cancelled():
                task.exception()  # retrieved, so that it is not reported as never retrieved
        if pending:
            self._timer.cleanup_timed_out = True
            log.warning(
                "%d cancelled task(s) did not stop within %g s and were given up on",
                len(pending),
                self._cleanup_deadline_s,
            )
        self._timer.end_cleanup()


def _consume(task: asyncio.Future) -> None:
    """Retrieve the outcome of a task that was given up on, so that it is not reported as never retrieved."""
    if not task.cancelled():
        task.exception()
