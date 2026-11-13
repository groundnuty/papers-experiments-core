# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:636-651
"""Running one call under watch: it is cut when the agent's own time or the whole-time ceiling is used up (L04).

The watch reads the injected clock through the timer, so every reading is logged, and waits on the injected sleep. It
sleeps until the next moment a limit could be passed and then reads the clock and judges: the agent's time (which
leaves out waits on infrastructure that are open, because the agent's clock is paused in them) against what is left of
its cap, and the total time against the ceiling. Waking early, for instance because a wait paused the agent's clock,
only costs another reading; a limit is never passed unseen, because the agent's deadline can move later but not earlier.

A call that finishes first returns its result, whatever the clock says after the fact. The exception is a call that
ended by the expiry of its own bound, `CallTimedOut`, at the bound it was handed (the agent's time left, L05): that
expiry and this watch's cut are one moment, so it is judged as the cut when a reading shows a limit passed (L16). A call
that is cut is cancelled
and awaited, and the time that takes is cleanup, which the timer keeps apart from the agent's time. The same cleanup
runs when the whole run is cancelled from outside, and then the cancellation goes on to the caller.

Stopping a call may take for ever (a cleanup that waits on something that never comes), and a cancellation must always
end. So the wait for a cancelled call has a deadline; past it the call is given up on and left pending, the timer
records `cleanup_timed_out`, and the cut or the cancellation goes on. The event loop's own shutdown cancels what is
left. Whether the deadline ran out is decided by the timer's `cleanup_waiter`: real time by default, and a replay
injects one that serves the recorded answer, which the reading that ends the cleanup carries.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from expcore.errors import CallTimedOut
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

        The watch looks again whenever a wait on infrastructure opens or closes, because that moves the moment the
        agent's cap would be reached. Whichever way this ends, every child task has been cancelled and awaited before
        it returns or raises, so none is left pending and a cancelled call has had its chance to clean up.
        """
        changed = asyncio.Event()  # set when a wait opens or closes the pause of the agent's clock
        stop_listening = self._timer.on_wait_change(changed.set)
        job = asyncio.ensure_future(work)
        sleeper = asyncio.ensure_future(self._sleep(tick.watch_s))  # asked for before the work starts
        nudge = asyncio.ensure_future(changed.wait())
        spent: list[asyncio.Future] = []  # sleepers replaced before they ended
        try:
            while True:
                await asyncio.wait({job, sleeper, nudge}, return_when=asyncio.FIRST_COMPLETED)
                if job.done():
                    if _ended_at_the_bound_it_was_given(job, tick):
                        now = self._timer.check("watch")  # the reading the cut takes, so a replay reads alike
                        if now.remaining_s <= 0 or now.ceiling_passed:
                            self._timer.begin_cleanup(now)
                            raise Cut(now)
                    return job.result()
                if sleeper.done():
                    sleeper.result()
                changed.clear()  # before the reading, so a change after it wakes the next look
                now = self._timer.check("watch")
                if now.remaining_s <= 0 or now.ceiling_passed:
                    self._timer.begin_cleanup(now)
                    raise Cut(now)
                if not sleeper.done():
                    sleeper.cancel()
                    spent.append(sleeper)
                if nudge.done():
                    nudge = asyncio.ensure_future(changed.wait())
                sleeper = asyncio.ensure_future(self._sleep(now.watch_s))
        except asyncio.CancelledError:
            if not self._timer.cleaning:  # cancelled from outside: stopping the call is cleanup too
                self._timer.begin_cleanup(self._timer.check("cancelled"))
            raise
        finally:
            stop_listening()
            await self._reap(job, sleeper, nudge, *spent)

    async def _reap(self, *tasks: asyncio.Future) -> None:
        """Cancel the tasks and wait, up to the cleanup deadline, until they have stopped; give up on any that has not.

        When a cut or a cancellation began a cleanup, the timer's cleanup waiter decides whether the deadline ran out
        (in real time unless a replay injected another), and the cleanup ends with that answer on its reading.
        """
        for task in tasks:
            task.cancel()
        waiting = [task for task in tasks if not task.done()]
        cleaning = self._timer.cleaning
        if cleaning:
            timed_out = await self._timer.cleanup_waiter(waiting, self._cleanup_deadline_s)
        elif waiting:
            timed_out = bool((await asyncio.wait(waiting, timeout=self._cleanup_deadline_s))[1])
        else:
            timed_out = False
        gave_up = [task for task in tasks if not task.done()]
        for task in tasks:
            if not task.done():
                task.add_done_callback(_consume)  # given up on: whatever it ends with, nobody is waiting for it
            elif not task.cancelled():
                task.exception()  # retrieved, so that it is not reported as never retrieved
        if gave_up:
            log.warning(
                "%d cancelled task(s) did not stop within %g s and were given up on",
                len(gave_up),
                self._cleanup_deadline_s,
            )
        if cleaning:
            self._timer.end_cleanup(timed_out=timed_out)
        elif gave_up:
            self._timer.cleanup_timed_out = True


def _ended_at_the_bound_it_was_given(job: asyncio.Future, tick: Tick) -> bool:
    """Tell whether a finished call ended by the expiry of a bound as long as the agent's time it was handed (L16).

    A provider that bounds its own call is handed the agent's time left (L05), so its expiry and this watch's cut are
    one moment, and which of the two the event loop runs first is chance. The ending must not be: a call that outlasted
    the agent's time or the ceiling was cut by it, whoever noticed first, so the watch judges such an expiry as a cut
    when a reading shows a limit passed. An expiry of a shorter bound (a provider's own, tighter one) is the provider's
    failure, and takes no reading, so a replay, which raises the recorded failure, reads the clock as the run did.
    """
    if job.cancelled():
        return False
    error = job.exception()
    return isinstance(error, CallTimedOut) and error.bound_s >= tick.remaining_s


def _consume(task: asyncio.Future) -> None:
    """Retrieve the outcome of a task that was given up on, so that it is not reported as never retrieved."""
    if not task.cancelled():
        task.exception()
