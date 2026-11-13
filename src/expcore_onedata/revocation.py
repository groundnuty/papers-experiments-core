# Provenance: new
"""Revoke a leased slot's tokens, confirm it, and journal when it has settled (C19).

Without it a slot is free only once its tokens' journalled expiry has passed, and a run's tokens live for its whole-time
ceiling plus their margins, about a day for a long run, whatever the run took. Onezone checks a temporary token by its
user's generation, so the slot's two users each revoke all their temporary tokens with their own credential
(`TokenIssuer.revoke_own`): the run's tokens, those an earlier run on the slot dropped, and any a provider derived from
them, and nothing of another slot or of the fixed principals, since `users.load_slots` keeps every user to one slot and
plane. Every check must pass before anything is journalled:

1. for each user, its generation read, its revocation sent and its generation read again, which must have risen;
2. every token value the run still holds verifies as 400 `tokenRevoked` (`TokenIssuer.require_revoked`), never merely
   as an error: the verification refuses a caveated token for its caveats anyway;
3. then one **revoked** line, durably: `settle_until` is the clock then plus the settle time, by which the change has
   reached every provider (pushed over Graph Sync at once; at worst the 300 s purge after a provider's lost connection
   to Onezone, plus the 15 s an open session waits before checking its token again).

Any failure raises before the line, so the journal keeps the expiry rule. A line of the slot journalled while this ran
can only be a mint that started meanwhile, which may have finished after a bump at the new generation and so holds a
valid token: the revocation line must not cap it, so an indeterminate line follows it, which holds the slot until an
operator clears it, and the revocation is reported unconfirmed.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from expcore_onedata.tokens import RevocationUnconfirmed, RunTokens, TokenIssuer

if TYPE_CHECKING:
    from expcore_onedata.users import Slot, TokenJournal


@dataclass(frozen=True)
class Revoked:
    """A confirmed revocation of a slot's tokens: when it was confirmed, when it has settled, each plane's user's
    generation before and after, and how many token values verified as revoked. It holds no token and no user."""

    revoked_at: float
    settle_until: float
    generations: dict[str, tuple[int, int]]
    verified: int

    def record(self) -> dict[str, Any]:
        """The revocation as a run's metadata keeps it."""
        return {
            "revoked": True,
            "revoked_at": self.revoked_at,
            "settle_until": self.settle_until,
            "generations": {plane: list(pair) for plane, pair in self.generations.items()},
            "verified": self.verified,
        }


def revoke_slot(
    issuer: TokenIssuer,
    slot: Slot,
    journal: TokenJournal,
    tokens: RunTokens | None,
    *,
    settle_s: float,
    clock: Callable[[], float],
) -> Revoked:
    """Revoke both users' temporary tokens of a slot, confirm it, and journal the settle end; raise on any failure.

    The caller makes sure nothing mints on the slot meanwhile (its run ended, no start thread of it runs, or the
    lease's lock is held); a line of the slot journalled meanwhile all the same turns the revocation into an
    indeterminate line and `RevocationUnconfirmed`.
    """
    if isinstance(settle_s, bool) or not isinstance(settle_s, int | float) or not 0 <= settle_s < float("inf"):
        raise ValueError("the settle time of a revocation must be a finite number of seconds, zero or more")
    mark = journal.written()  # before the first bump: a mint journalled after it may hold a token of the new generation
    generations = {"data_path": issuer.revoke_own(slot.data_path), "tool_server": issuer.revoke_own(slot.tool_server)}
    held = [t for t in (tokens.data_path, tokens.tool_server) if t is not None] if tokens is not None else []
    for token in held:
        issuer.require_revoked(token.reveal())
    now = clock()
    done = Revoked(now, now + settle_s, generations, len(held))
    name = secrets.token_hex(8)
    journal.record_revoked(
        slot.name,
        revocation=name,
        revoked_at=done.revoked_at,
        settle_until=done.settle_until,
        generations=generations,
        verified=done.verified,
    )
    if journal.slot_lines_since(slot.name, mark, revocation=name):
        journal.record_indeterminate(slot.name)
        raise RevocationUnconfirmed(
            "a token of the slot was journalled during its revocation, so it may outlive it: the slot is held until an "
            "operator clears it"
        )
    return done


__all__ = ["Revoked", "revoke_slot"]
