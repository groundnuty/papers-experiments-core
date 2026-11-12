# Provenance: new
"""What the campaign's admission probes were charged, kept in the store so the budget counts it (C26).

With a budget, admission's probes are requests the campaign pays for like any other: the one-token completion (B07)
and the probe of the reply maximum (C27), each of their sends, retries included, at every start of the campaign. Each
send is reserved one request's bound by the window before it goes out (`topup.Ledger.reserve_probe`), since a probe
is what would show the endpoint holds replies to a smaller maximum, and is charged here at once: a kill during the send
leaves the bound charged. A reply that reports both counts settles the charge to what they cost at the dearest rates;
a send that failed, or a reply that left a count out, keeps its bound, since what it cost is unknown. A probe that
does not fit is not sent, and its model is not admitted.

The charges are one mapping, model to dollars, in the store's meta, changed with an event in one transaction. The
budget gate adds them to what was spent, the model's own ceiling included. A mapping that cannot be read is no charge
of 0: the total spent is then unknown, and the gate holds every model, as it does for an attempt of an unpriced model.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from expcore.campaign.store import Store
from expcore.errors import BudgetHold
from expcore.models import spend
from expcore.models.adapters import ProbeReply

if TYPE_CHECKING:
    from expcore.campaign.topup import Ledger

PROBES_KEY = "probe_spend_usd"  # model -> dollars charged for the sends of its admission probes


def _read(raw: str | None) -> dict[str, float] | None:
    """The charges in the stored text, or None when it is not a mapping of model keys to finite dollars."""
    try:
        value: Any = json.loads(raw) if raw else {}
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    out = {}
    for model, usd in value.items():
        if (
            isinstance(usd, bool)
            or not isinstance(usd, int | float)
            or usd != usd
            or usd in (float("inf"), -float("inf"))
        ):
            return None
        out[str(model)] = float(usd)
    return out


def charged(store: Store) -> dict[str, float] | None:
    """Return the dollars charged for each model's probes; None when the store's record cannot be read."""
    return _read(store.get_meta(PROBES_KEY))


def add(store: Store, model: str, usd: float, now: str, event: tuple[str, str | None, str]) -> None:
    """Add `usd` (below 0 to settle a reservation down) to the model's charges, with its event, in one transaction.
    A record that cannot be read is refused, never overwritten: what it held would be lost."""

    def change(current: str | None) -> tuple[str, list[tuple[str, str | None, str]], None]:
        value = _read(current)
        if value is None:
            raise ValueError(f"the store's {PROBES_KEY} cannot be read, so no probe charge is added to it")
        value[model] = value.get(model, 0.0) + usd
        return json.dumps(value, sort_keys=True), [event], None

    store.update_meta(PROBES_KEY, change, now)


class ProbeSpend:
    """The spend guard of one admission probe (`expcore.models.spend.SpendGuard`): every send of it, a resend after a
    failure that may have been billed included, is reserved before it goes out, and a failed send keeps its bound."""

    def __init__(self, ledger: "Ledger", model: str, what: str):
        self.ledger, self.model, self.what = ledger, model, what
        self.refusal: str | None = None  # why a send was not made: the budget could not cover it
        self.cleared: float | None = None  # what the send going out now was reserved at

    def reserve(self) -> None:
        """Reserve the next send, or raise `BudgetHold`, in which case it is not sent."""
        usd, refusal = self.ledger.reserve_probe(self.model, self.what)
        if refusal is not None:
            self.refusal = f"{self.what} was not sent: {refusal}"
            raise BudgetHold(self.refusal)
        self.cleared = usd

    def settle(self, reply: ProbeReply | None) -> None:
        """Settle the answered send to what its reply's counts cost; a reply without both counts keeps its bound."""
        if not self.cleared or reply is None or reply.input_tokens is None or reply.output_tokens is None:
            return
        self.ledger.settle_probe(self.model, self.what, self.cleared, reply.input_tokens, reply.output_tokens)

    async def before_request(self) -> None:
        """Nothing: a probe is no request of a run, and no recording sees it."""

    async def after_reply(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Nothing: the probe's reply is settled by `settle`."""

    async def after_failed_send(self, kind: str, *, resend: bool) -> None:
        """Keep the failed send's bound; reserve the resend, if any, before the wait, or end the probe as a budget
        hold. The last failed send was reserved before it went out, so nothing more is charged for it."""
        if resend:
            await asyncio.to_thread(self.reserve)

    async def before_resend(self, kind: str) -> None:
        """Nothing to clear again: a probe's bound is the window's whatever the model is held to, and admission runs
        alone, before any run is dispatched."""


class MeteredProbes:
    """A backend whose admission probes are paid from the campaign's budget (C26): each send of the one-token
    completion and of the probe of the reply maximum goes through a `ProbeSpend`. Everything else is the backend's."""

    def __init__(self, backend: Any, ledger: "Ledger", model: str):
        self.backend, self.ledger, self.model = backend, ledger, model
        self.refusal: str | None = None  # why a send of a probe was not made: the budget could not cover it

    def __getattr__(self, name: str) -> Any:
        if name == "backend":  # not set yet: never look for it on itself
            raise AttributeError(name)
        return getattr(self.backend, name)

    async def probe(self) -> ProbeReply | None:
        """The backend's one-token completion, paid for."""
        return await self._metered("the one-token completion", self.backend.probe)

    async def probe_reply_cap(self) -> ProbeReply:
        """The backend's probe of the reply maximum, paid for."""
        return await self._metered("the probe of the reply maximum", self.backend.probe_reply_cap)

    async def _metered(self, what: str, send: Callable[[], Awaitable[Any]]) -> Any:
        guard = ProbeSpend(self.ledger, self.model, what)
        try:
            await asyncio.to_thread(guard.reserve)
            with spend.watching(guard):
                reply = await send()
        finally:
            self.refusal = self.refusal or guard.refusal
        await asyncio.to_thread(guard.settle, reply)
        return reply
