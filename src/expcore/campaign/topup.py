# Provenance: new
"""The top-up ledger: what a run in flight pays for the sends its token cap cannot account for (C26).

A run is reserved the most its caps let it cost (`attempt_bounds.bound_usd`, C25), which holds while the token cap
counts every token billed. Two kinds of send it does not count, and each is charged a **top-up** of one request's bound
(`attempt_bounds.request_bound_usd`):

- **a send that failed after it may have been billed** (a connection error or a 5xx, `expcore.models.spend`): its
  top-up is reserved before the retrier waits to send the request again, and the last such send of a request, after
  which nothing is sent again, is charged too, so no failed send is left out of what was spent;
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
sent only while the campaign is within its ceilings, else that run ends as a budget hold too. Which bound a run was
reserved is the decision of the assessment that admitted it, carried into its guard by the scheduler and recorded on
its attempt's row; the guard never reads it back from the model's status, which another attempt may have changed in
between.

A resend is cleared again right before it goes out, after the retrier's wait (`Ledger.revalidate`): if the model was
taken off the maximum during the wait, the rest of its top-up's bound by the window is reserved, or the resend is
refused, and either way the campaign must still be within its ceilings. Marks are made under the ledger's locks, so a
bound read there and the charge made from it never straddle one.

The ledger also pays for admission's probes (`reserve_probe`, `settle_probe`; `expcore.campaign.probe_spend`): each
send of a probe is reserved one request's bound by the window before it goes out and charged in the store, and the
answered one is settled to what its reply's counts cost.

The ledger's work reads and writes the store, so the guard does it in a thread, never on the attempt's event loop
(L15).
"""

import asyncio
import contextlib
from collections.abc import Callable, Collection, Iterator, Sequence
from typing import Any

from expcore.campaign import probe_spend, reply_cap
from expcore.campaign.attempt_bounds import probe_usd, request_bound_usd
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
        live: Callable[[], Collection[str]] | None = None,
    ):
        """Take the budget gate, the store, the clock the store's times come from, the dispatcher's locks in the order
        it takes them, what returns the model of each run in flight, and what returns those runs' keys (`live`, C17),
        both read while those locks are held. Without `live` every running attempt is taken as one a worker settles."""
        self.gate, self.store, self.stamp, self.locks, self.in_flight = gate, store, stamp, tuple(locks), in_flight
        self.live = live

    def _refusal(self, model: str, extra_usd: float, label: str = "this top-up") -> str | None:
        """The gate's refusal of `extra_usd` more for a run of `model` in flight; call with the locks held."""
        live = None if self.live is None else self.live()
        return self.gate.refusal(model, self.in_flight(), extra_usd, label, live=live)

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
        """Take a model off the reply maximum for the rest of the campaign: one of its replies went past it (C27). The
        mark is made under the locks, so no top-up is charged by a bound read before it and recorded after it."""
        detail = (
            f"{run_key}: a reply reported {output_tokens} output tokens, over the reply maximum of "
            f"{self.maximum(model)}; the model's runs are reserved by its context window from now on"
        )
        with self._held():
            reply_cap.mark_unheld(self.store, model, self.stamp(), "reply_over_maximum", detail)

    def check(self, run_key: str, model: str, why: str) -> str | None:
        """Tell why a run in flight may not send its next request, or None: the campaign is past a ceiling now that its
        runs are reserved by the window (C27). Nothing is charged."""
        with self._held():
            refusal = self._refusal(model, 0.0, "this request")
            if refusal is not None:
                self.store.event(self.stamp(), "topup_refused", model, f"{run_key}: {why}: {refusal}")
        return refusal

    def reserve(self, attempt_id: int, run_key: str, model: str, why: str) -> str | None:
        """Reserve one top-up for a run in flight and charge it; return None, or why it does not fit, uncharged."""
        return self.reserve_send(attempt_id, run_key, model, why)[1]

    def reserve_send(self, attempt_id: int, run_key: str, model: str, why: str) -> tuple[float | None, str | None]:
        """Reserve one top-up for a send of a run in flight at the model's bound, read under the locks, and charge it.
        Return the bound, which is what the send is cleared with (0 for a free model, whose send costs nothing), and
        None, or why it does not fit, uncharged."""
        with self._held():
            usd = self.bound(model)
            if usd == 0.0:
                return 0.0, None
            refusal = (
                f"model {model} has no price: its request has no bound" if usd is None else self._refusal(model, usd)
            )
            if refusal is not None:
                self.store.event(self.stamp(), "topup_refused", model, f"{run_key}: {why}: {refusal}")
                return usd, refusal
            assert usd is not None
            self._add(attempt_id, run_key, model, why, usd)
        return usd, None

    def revalidate(
        self, attempt_id: int, run_key: str, model: str, why: str, cleared: float
    ) -> tuple[float, str | None]:
        """Clear a send again right before it goes out, against the bound and the ceilings as they are now (C26, C27).

        `cleared` is what the send's top-up was reserved at. The model may have been taken off its reply maximum since,
        so its bound grew: the difference is reserved and charged. Whether or not it grew, the campaign must still be
        within its ceilings. Return what the send is cleared with now, and None, or why it may not go out (nothing more
        is charged then).
        """
        with self._held():
            usd = self.bound(model)
            if usd == 0.0:
                return cleared, None  # a free model's send costs nothing
            extra = 0.0 if usd is None else max(usd - cleared, 0.0)
            refusal = (
                f"model {model} has no price: its request has no bound"
                if usd is None
                else self._refusal(model, extra, "this resend")
            )
            if refusal is not None:
                self.store.event(self.stamp(), "topup_refused", model, f"{run_key}: {why}: {refusal}")
                return cleared, refusal
            if extra > 0.0:
                rest = f"{why}: the rest of its bound, the model taken off its reply maximum since it was cleared"
                self._add(attempt_id, run_key, model, rest, extra)
        return cleared + extra, None

    def charge(self, attempt_id: int, run_key: str, model: str, why: str) -> None:
        """Charge one top-up for a send already made, whether or not it fits: what it may have cost is spent."""
        with self._held():
            usd = self.bound(model)
            if usd:  # free, or unpriced: a model without a price never runs under a budget (C12)
                self._add(attempt_id, run_key, model, why, usd)

    def probe_bound(self, model: str) -> float | None:
        """What one send of an admission probe can cost: one request's bound by the window, whatever the model is held
        to, since a probe is what would show it held (C26, C27); 0 when free, None when the price is unknown."""
        return request_bound_usd(self.gate.models[model], self.gate.limits)

    def reserve_probe(self, model: str, what: str) -> tuple[float | None, str | None]:
        """Reserve one send of an admission probe against the ceilings and charge it in the store (C26); return its
        bound and None, or why it does not fit, uncharged."""
        with self._held():
            usd = self.probe_bound(model)
            if usd == 0.0:
                return 0.0, None  # a free model's probe costs nothing
            refusal = (
                f"model {model} has no price: its probe has no bound"
                if usd is None
                else self._refusal(model, usd, "this probe")
            )
            if refusal is not None:
                self.store.event(self.stamp(), "probe_refused", model, f"{model}: {what} was not sent: {refusal}")
                return usd, refusal
            assert usd is not None
            detail = f"{model}: {what}; {usd:.6f} USD reserved before it is sent"
            probe_spend.add(self.store, model, usd, self.stamp(), ("probe_charged", model, detail))
        return usd, None

    def settle_probe(self, model: str, what: str, reserved: float, input_tokens: int, output_tokens: int) -> None:
        """Replace what an answered send of a probe was reserved by what its reply's counts cost (C26)."""
        cost = probe_usd(self.gate.models[model], self.gate.limits, input_tokens, output_tokens)
        detail = (
            f"{model}: {what} reported {input_tokens} input and {output_tokens} output tokens; charged {cost:.6f} USD "
            f"in place of the {reserved:.6f} reserved"
        )
        with self._held():
            probe_spend.add(self.store, model, cost - reserved, self.stamp(), ("probe_settled", model, detail))

    def _add(self, attempt_id: int, run_key: str, model: str, why: str, usd: float) -> None:
        """Charge the attempt one top-up, with its event; call with the locks held."""
        self.store.add_topup(attempt_id, usd, self.stamp(), ("topup", model, f"{run_key}: {why}; {usd:.6f} USD"))

    def note(self, model: str, kind: str, detail: str) -> None:
        """Append an event about one of the model's requests."""
        self.store.event(self.stamp(), kind, model, detail)


class AttemptSpend:
    """The spend guard of one attempt (`expcore.models.spend.SpendGuard`), installed by the attempt runner."""

    def __init__(self, ledger: Ledger, attempt_id: int, run_key: str, model: str, *, reserved_capped: bool):
        """Take the ledger, the attempt, and whether the assessment that admitted its run reserved it by the reply
        maximum (C27). That decision is carried in, never read from the model's status here: another attempt may
        have taken the model off the maximum since, and the run must still be checked as the run it was admitted as."""
        self.ledger, self.attempt_id, self.run_key, self.model = ledger, attempt_id, run_key, model
        self.unaccounted = False  # a reply left a count out: from then on the cap does not bound the run
        self.maximum = ledger.maximum(model)
        self.reserved_capped = reserved_capped  # the run was admitted with the reservation by the reply maximum
        # what the next send of the current request was cleared with by a top-up; None: the run's reservation covers it
        self.cleared: float | None = None

    async def before_request(self) -> None:
        """Top up a request that follows a reply without usage, or end the attempt as a budget hold. A run reserved
        by the reply maximum of a model since taken off it sends its next request only within the ceilings (C27)."""
        self.cleared = None
        if self.unaccounted:
            await asyncio.to_thread(self._reserve_or_hold, "a request after a reply that did not report its usage")
        elif self.reserved_capped:
            await asyncio.to_thread(self._check_if_unheld)

    async def after_reply(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Record a reply past the reply maximum of a model held to it (C27), and a reply that left a count out, which
        takes every later request of the attempt through a top-up. A reply past the maximum counts whether this run was
        reserved by it or by the window: while the campaign holds the model to it, the reply is evidence against every
        other run of the model."""
        if output_tokens is not None and output_tokens > self.maximum:
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

    async def before_resend(self, kind: str) -> None:
        """Clear a resend again right before it goes out, after the retrier's wait: a top-up given before the wait is
        checked against the bound and the ceilings now (`Ledger.revalidate`); a send the run's reservation covers is
        checked as a request is, within the ceilings for a run reserved by the maximum of a model since taken off it."""
        if self.cleared is not None:
            await asyncio.to_thread(self._revalidate, kind)
        elif self.reserved_capped:
            await asyncio.to_thread(self._check_if_unheld)

    def _past_maximum(self, output_tokens: int) -> None:
        if not (self.reserved_capped or self.ledger.reply_capped(self.model)):
            return  # neither this run nor the campaign holds the model to the maximum: the window covers the reply
        self.ledger.reply_over(self.run_key, self.model, output_tokens)  # each is an event; the first marks the model

    def _check_if_unheld(self) -> None:
        if self.ledger.reply_capped(self.model):
            return
        why = "a request of a run reserved by the reply maximum of a model since taken off it"
        refusal = self.ledger.check(self.run_key, self.model, why)
        if refusal is not None:
            raise BudgetHold(f"{why} was not sent: {refusal}")

    def _reserve_or_hold(self, why: str) -> None:
        usd, refusal = self.ledger.reserve_send(self.attempt_id, self.run_key, self.model, why)
        if refusal is not None:
            raise BudgetHold(f"{why} was not sent: {refusal}")
        self.cleared = usd

    def _revalidate(self, kind: str) -> None:
        assert self.cleared is not None
        why = f"a resend after a {kind} failure"
        self.cleared, refusal = self.ledger.revalidate(self.attempt_id, self.run_key, self.model, why, self.cleared)
        if refusal is not None:
            raise BudgetHold(f"{why} was not sent: {refusal}")

    def _failed_send(self, kind: str, resend: bool) -> None:
        why = f"a send that failed ({kind}) after it may have been billed"
        if not resend:
            self.ledger.charge(self.attempt_id, self.run_key, self.model, f"{why}, the last of its request")
            return
        usd, refusal = self.ledger.reserve_send(
            self.attempt_id, self.run_key, self.model, f"{why}, before it is sent again"
        )
        if refusal is None:
            self.cleared = usd
            return
        self.ledger.charge(self.attempt_id, self.run_key, self.model, f"{why}, not sent again")
        raise BudgetHold(f"the request was not sent again after a {kind} failure: {refusal}")


def _count(value: int | None) -> str:
    return "no" if value is None else str(value)
