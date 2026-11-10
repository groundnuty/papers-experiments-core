# Provenance: new
"""What a request the token cap cannot account for must clear before it is sent (C26).

The token cap counts what each reply reports, so a run's reservation covers the requests the cap accounts for. Two
kinds of send it cannot account for:

- a send that failed after it may have been billed, a connection error or a 5xx (`POSSIBLY_BILLED`): its tokens are
  never reported, and the retrier sends the request again;
- every request after a reply that did not report its input or its output: the loop counts the missing count as 0,
  so from then on the cap no longer bounds the run.

The campaign installs a `SpendGuard` for the length of an attempt (`watching`). The guard is told of each failed send
that may have been billed, before the retrier sends again, and of each request and reply as the recording sees them
(`expcore.records.recording.RecordingBackend`), so a refusal is recorded as the request's failure and replays as one.
A guard refuses by raising `BudgetHold`, an infrastructure failure of kind `budget_hold`: the request is not sent and
the attempt ends, set aside. Without a guard, as in a campaign without a budget, in a replay or outside a campaign,
every call here does nothing.

The guard reaches the core's retrier through a context variable, not through the backend factory, whose signature a
study's plugin implements: so it covers every core adapter of the attempt, one a plugin's backend wraps included, and
whatever path the study's executor takes to the loop. A backend of the study's own that resends on its own is not
seen; its replies are still seen by the recording.
"""

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Protocol

from expcore.errors import BudgetHold

__all__ = ["POSSIBLY_BILLED", "BudgetHold", "SpendGuard", "after_failed_send", "after_reply", "before_request"]

# The retry kinds (`expcore.models.retry.classify`) after which the request may have reached the model and been billed.
# A rate limit, a 408, a 409 and a timeout before the request was written are refused before any work is done.
POSSIBLY_BILLED = frozenset({"connection", "server"})


class SpendGuard(Protocol):
    """What the campaign decides for one attempt's sends; a refusal raises `BudgetHold`."""

    async def before_request(self) -> None:
        """Clear the next request of the attempt before it is built, or raise `BudgetHold`."""
        ...

    async def after_reply(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Take note of the counts a reply reported (None for a count it left out)."""
        ...

    async def after_failed_send(self, kind: str, *, resend: bool) -> None:
        """Charge a send that failed after it may have been billed; with `resend`, clear the send that follows it, or
        raise `BudgetHold`, in which case it is not sent."""
        ...


_guard: ContextVar[SpendGuard | None] = ContextVar("expcore_spend_guard", default=None)


@contextlib.contextmanager
def watching(guard: SpendGuard) -> Iterator[None]:
    """Install the guard for the block: every request made in it, and in the tasks and threads it starts, is seen."""
    token = _guard.set(guard)
    try:
        yield
    finally:
        _guard.reset(token)


async def before_request() -> None:
    """Ask the attempt's guard, if there is one, to clear the next request."""
    guard = _guard.get()
    if guard is not None:
        await guard.before_request()


async def after_reply(input_tokens: int | None, output_tokens: int | None) -> None:
    """Tell the attempt's guard, if there is one, what a reply reported."""
    guard = _guard.get()
    if guard is not None:
        await guard.after_reply(input_tokens, output_tokens)


async def after_failed_send(kind: str, *, resend: bool) -> None:
    """Tell the attempt's guard, if there is one, of a send that failed with a retry kind of `POSSIBLY_BILLED`."""
    guard = _guard.get()
    if guard is not None and kind in POSSIBLY_BILLED:
        await guard.after_failed_send(kind, resend=resend)
