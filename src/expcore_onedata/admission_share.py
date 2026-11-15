# Provenance: new
"""A worker thread's share of the run's admission (O11).

A run holds the federation's admission (`expcore_onedata.admission`) from before its infrastructure starts until after
its post-run check, but a worker thread the run starts can outlive the coroutine that awaits it: a cancelled launch
stops waiting for its thread and the run leaves its admission, and the thread goes on acting on the federation. So a
worker that touches the federation takes a share of the admission (`Admission.retain`) before it starts, in the
coroutine that holds the context's admission, and gives it up itself when it ends, last: the lock goes only when the
last share does.

`ThreadShare` is that share, kept by the sandbox's start thread and by every check thread of the tool server;
`to_thread_holding_admission` is the call a coroutine uses to run a function in a thread that way.
"""

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from expcore_onedata.admission import retain_admission


class ThreadShare:
    """A worker thread's share of the run's admission (`admission.retain_admission`): the thread gives it up when it
    ends, never the launch that may stop waiting for it; a thread that never began has it given up for it."""

    def __init__(self, give_up: Callable[[], None]) -> None:
        self._give_up, self._state, self._lock = give_up, "pending", threading.Lock()

    def begin(self) -> bool:
        """In the thread, first: whether it may run, holding its share."""
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "running"
            return True

    def end(self) -> None:
        """In the thread, last: give the share up."""
        with self._lock:
            self._state = "ended"
        self._give_up()

    def never_ran(self) -> None:
        """When the thread's task is done: give the share up if the thread never began; one that runs gives up its
        own."""
        with self._lock:
            if self._state != "pending":
                return
            self._state = "ended"
        self._give_up()


async def to_thread_holding_admission[**P, R](func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run `func` in a worker thread (`asyncio.to_thread`) holding a share of the running context's admission, and
    return its result or raise what it raised.

    The share is taken here, before the thread exists, and the thread gives it up when `func` ends, on success, on an
    exception and on a cancellation of the awaiting task alike: the cancelled task stops waiting, the thread does not,
    and the federation stays held until it has ended. When the thread never began (the task was cancelled before the
    executor picked the work up), the task's end gives the share up for it. A context that holds no admission has
    nothing to share, and the call is `asyncio.to_thread`."""
    share = ThreadShare(retain_admission())

    def work() -> R | None:
        if not share.begin():  # given up for it already: the call was cancelled before it began
            return None
        try:
            return func(*args, **kwargs)
        finally:
            share.end()

    future: asyncio.Future[Any] = asyncio.ensure_future(asyncio.to_thread(work))
    future.add_done_callback(lambda _: share.never_ran())
    return await future
