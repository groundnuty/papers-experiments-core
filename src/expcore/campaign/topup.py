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

The ledger's work reads and writes the store, so the guard does it in a thread, never on the attempt's event loop
(L15).
"""

import asyncio
import contextlib
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from expcore.campaign.attempt_bounds import request_bound_usd
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
        """What one top-up of the model is charged: one request's bound; 0 when free, None when the price is unknown."""
        return request_bound_usd(self.gate.models[model], self.gate.limits)

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
        self.ledger, self.attempt_id, self.run_key, self.model = ledger, attempt_id, run_key, model
        self.unaccounted = False  # a reply left a count out: from then on the cap does not bound the run

    async def before_request(self) -> None:
        """Top up a request that follows a reply without usage, or end the attempt as a budget hold."""
        if self.unaccounted:
            await asyncio.to_thread(self._reserve_or_hold, "a request after a reply that did not report its usage")

    async def after_reply(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Record a reply that left a count out, and take every later request of the attempt through a top-up."""
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
