# Provenance: new
"""The top-up ledger: what a run in flight pays for the sends its token cap cannot account for (C26).

A run is reserved the most its caps let it cost (`attempt_bounds.bound_usd`, C25), which holds while the token cap
counts every token billed. Two kinds of send it does not count, and each is charged a **top-up** of one request's bound
(`attempt_bounds.request_bound_usd`):

- **a send that failed after it may have been billed** (a connection error or a 5xx, `expcore.models.spend`): its
  top-up is reserved before the retrier sends the request again, and the last such send of a request, after which
  nothing is sent again, is charged too, so no failed send is left out of what was spent;
- **every request after a reply that left its input or its output unreported**: the loop counts the missing count as
  0, so from that reply on the cap no longer bounds the run. The reply is recorded as a `usage_unreported` event.

A top-up is reserved against the campaign's ceilings with the figure of the budget gate (`BudgetGate.refusal`), under
the dispatcher's own locks, so a dispatch and a top-up, or two top-ups, never both take the last of the budget. One
that fits is charged at once (`Store.add_topup`, with a `topup` event) and never refunded, since what that send cost is
unknown; one that does not fit is refused (a `topup_refused` event), the send is not made, and the attempt ends as a
budget hold (`BudgetHold`), set aside and not charged an attempt. The loop's token counting and its `token_cap` are
not touched: the cap is part of the experiment. Without a budget there is no ledger, and nothing changes.

A model whose runs were reserved by the reply maximum and one of whose replies reports more output than it is taken off
it (`expcore.campaign.reply_cap`, C27): the reply is a `reply_over_maximum` event, the model's runs are reserved by the
window from then on, those in flight included, and each later request of a run that was reserved the smaller bound is
sent only while the campaign is within its ceilings, else that run ends as a budget hold too.

The ledger's work reads and writes the store, so the guard does it in a thread, never on the attempt's event loop
(L15).
"""

import asyncio
import contextlib
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from expcore.campaign import reply_cap
from expcore.campaign.gate import BudgetGate
from expcore.campaign.store import Store
from expcore.errors import BudgetHold


class Ledger:
    """Reserve and charge top-ups for the campaign's runs in flight; one per campaign with a budget."""

    def __init__(
        self,
        gate: BudgetGate,
        store: Store,
        stamp: Callable[[], str],
        locks: Sequence[Any],
        in_flight: Callable[[], list[str]],
    ):
        """Take the budget gate, the store, the clock the store's times come from, the dispatcher's locks in the order
        it takes them, and what returns the model of each run in flight, read while those locks are held."""
        self.gate, self.store, self.stamp, self.locks, self.in_flight = gate, store, stamp, tuple(locks), in_flight

    @contextlib.contextmanager
    def _held(self) -> Iterator[None]:
        """Hold the dispatcher's locks, in its order, so nothing is dispatched or topped up meanwhile."""
        with contextlib.ExitStack() as stack:
            for lock in self.locks:
                stack.enter_context(lock)
            yield

    def bound(self, model: str) -> float | None:
        """What one top-up of the model is charged: one request's bound, by the reply maximum only while the model is
        held to it (C27); 0 when free, None when the price is unknown."""
        return self.gate.request_bound(model)

    def maximum(self, model: str) -> int:
        """The reply maximum of the model's requests: the study's `max_output_tokens`, or the model's own when lower."""
        return self.gate.models[model].output_tokens(self.gate.limits.max_output_tokens)

    def reply_capped(self, model: str) -> bool:
        """Tell whether the model's runs are reserved by the reply maximum now (C27)."""
        return model in self.gate.capped()

    def reply_over(self, run_key: str, model: str, output_tokens: int) -> None:
        """Take a model off the reply maximum for the rest of the campaign: one of its replies went past it (C27)."""
        detail = (
            f"{run_key}: a reply reported {output_tokens} output tokens, over the reply maximum of "
            f"{self.maximum(model)}; the model's runs are reserved by its context window from now on"
        )
        reply_cap.mark_unheld(self.store, model, self.stamp(), "reply_over_maximum", detail)

    def check(self, run_key: str, model: str, why: str) -> str | None:
        """Tell why a run in flight may not send its next request, or None: the campaign is past a ceiling now that its
        runs are reserved by the window (C27). Nothing is charged."""
        with self._held():
            refusal = self.gate.refusal(model, self.in_flight(), 0.0, "this request")
            if refusal is not None:
                self.store.event(self.stamp(), "topup_refused", model, f"{run_key}: {why}: {refusal}")
        return refusal

    def reserve(self, attempt_id: int, run_key: str, model: str, why: str) -> str | None:
        """Reserve one top-up for a run in flight and charge it; return None, or why it does not fit, uncharged."""
        usd = self.bound(model)
        if usd == 0.0:
            return None  # a free model's request costs nothing
        with self._held():
            refusal = (
                f"model {model} has no price: its request has no bound"
                if usd is None
                else self.gate.refusal(model, self.in_flight(), usd)
            )
            if refusal is not None:
                self.store.event(self.stamp(), "topup_refused", model, f"{run_key}: {why}: {refusal}")
                return refusal
            assert usd is not None
            self._add(attempt_id, run_key, model, why, usd)
        return None

    def charge(self, attempt_id: int, run_key: str, model: str, why: str) -> None:
        """Charge one top-up for a send already made, whether or not it fits: what it may have cost is spent."""
        usd = self.bound(model)
        if not usd:  # free, or unpriced: a model without a price never runs under a budget (C12)
            return
        with self._held():
            self._add(attempt_id, run_key, model, why, usd)

    def _add(self, attempt_id: int, run_key: str, model: str, why: str, usd: float) -> None:
        """Charge the attempt one top-up, with its event; call with the locks held."""
        self.store.add_topup(attempt_id, usd, self.stamp(), ("topup", model, f"{run_key}: {why}; {usd:.6f} USD"))

    def note(self, model: str, kind: str, detail: str) -> None:
        """Append an event about one of the model's requests."""
        self.store.event(self.stamp(), kind, model, detail)


class AttemptSpend:
    """The spend guard of one attempt (`expcore.models.spend.SpendGuard`), installed by the attempt runner."""

    def __init__(self, ledger: Ledger, attempt_id: int, run_key: str, model: str):
        """Take the ledger and the attempt; read, before the attempt's loop starts, whether its run was reserved by
        the reply maximum."""
        self.ledger, self.attempt_id, self.run_key, self.model = ledger, attempt_id, run_key, model
        self.unaccounted = False  # a reply left a count out: from then on the cap does not bound the run
        self.maximum = ledger.maximum(model)
        self.capped_at_start = ledger.reply_capped(model)  # the run was reserved by the reply maximum

    async def before_request(self) -> None:
        """Top up a request that follows a reply without usage, or end the attempt as a budget hold. A run reserved
        by the reply maximum of a model since taken off it sends its next request only within the ceilings (C27)."""
        if self.unaccounted:
            await asyncio.to_thread(self._reserve_or_hold, "a request after a reply that did not report its usage")
        elif self.capped_at_start:
            await asyncio.to_thread(self._check_if_unheld)

    async def after_reply(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Record a reply past the reply maximum of a model held to it (C27), and a reply that left a count out, which
        takes every later request of the attempt through a top-up."""
        if output_tokens is not None and output_tokens > self.maximum and self.capped_at_start:
            await asyncio.to_thread(self._past_maximum, output_tokens)
        if input_tokens is not None and output_tokens is not None:
            return
        self.unaccounted = True
        detail = (
            f"{self.run_key}: a reply reported input {_count(input_tokens)} and output {_count(output_tokens)} tokens; "
            "every later request of the attempt is topped up"
        )
        await asyncio.to_thread(self.ledger.note, self.model, "usage_unreported", detail)

    async def after_failed_send(self, kind: str, *, resend: bool) -> None:
        """Charge a send that failed after it may have been billed; clear the resend, or end as a budget hold."""
        await asyncio.to_thread(self._failed_send, kind, resend)

    def _past_maximum(self, output_tokens: int) -> None:
        self.ledger.reply_over(self.run_key, self.model, output_tokens)  # each is an event; the first marks the model

    def _check_if_unheld(self) -> None:
        if self.ledger.reply_capped(self.model):
            return
        why = "a request of a run reserved by the reply maximum of a model since taken off it"
        refusal = self.ledger.check(self.run_key, self.model, why)
        if refusal is not None:
            raise BudgetHold(f"{why} was not sent: {refusal}")

    def _reserve_or_hold(self, why: str) -> None:
        refusal = self.ledger.reserve(self.attempt_id, self.run_key, self.model, why)
        if refusal is not None:
            raise BudgetHold(f"{why} was not sent: {refusal}")

    def _failed_send(self, kind: str, resend: bool) -> None:
        why = f"a send that failed ({kind}) after it may have been billed"
        if not resend:
            self.ledger.charge(self.attempt_id, self.run_key, self.model, f"{why}, the last of its request")
            return
        refusal = self.ledger.reserve(self.attempt_id, self.run_key, self.model, f"{why}, before it is sent again")
        if refusal is None:
            return
        self.ledger.charge(self.attempt_id, self.run_key, self.model, f"{why}, not sent again")
        raise BudgetHold(f"the request was not sent again after a {kind} failure: {refusal}")


def _count(value: int | None) -> str:
    return "no" if value is None else str(value)
