# Provenance: new
"""Revoke a leased slot's tokens, confirm it, and journal when it has settled (C19).

Without it a slot is free only once its tokens' journalled expiry has passed, and a run's tokens live for its whole-time
ceiling plus their margins, about a day for a long run, whatever the run took. Onezone checks a temporary token by its
user's generation, so the slot's two users each revoke all their temporary tokens with their own credential
(`TokenIssuer.revoke_own`): the run's tokens, those an earlier run on the slot dropped, and any a provider derived from
them, and nothing of another slot or of the fixed principals, since `users.load_slots` keeps every user to one slot and
plane. The revocation's **start** line is journalled first, durably, and every check must pass before its **revoked**
line is:

1. for each user, its generation read, its revocation sent and its generation read again, which must have risen;
2. every token value the run still holds verifies as 400 `tokenRevoked` (`TokenIssuer.require_revoked`), never merely
   as an error: the verification refuses a caveated token for its caveats anyway;
3. then one **revoked** line, durably: `settle_until` is the clock then plus the settle time, by which the change has
   reached every provider (pushed over Graph Sync at once; at worst the 300 s purge after a provider's lost connection
   to Onezone, plus the 15 s an open session waits before checking its token again). A settle time shorter than that
   bound, 315 s (`config.REVOCATION_SETTLE_MIN_S`), is refused before anything is sent, whether it comes from
   `[tokens] revocation_settle_s` or is passed directly.

Any failure raises before the revoked line, so the journal keeps the expiry rule; a start line alone caps nothing.

**The cap is safe by construction, with no second write.** The revoked line caps only the slot's lines before its
start line (`users.TokenJournal.held_until`), and the start line is written before the first bump. A line after it may
be a mint that finished after the bump, at the new generation, so it holds a valid token: it keeps its own end, and
that needs nothing written after the cap, so a process that dies right after the revoked line leaves no valid token
capped. A line before it is a mint that ended before it, because the caller keeps any mint off the slot while this
runs (its run ended and no mint or start thread of it runs, or the command holds the slot's lease lock): each of its
tokens was minted before the bump, at the old generation, which the bump revoked. Capping lines by their position in
the file would not do: an operator who removes an indeterminate line moves every later line up.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from expcore_onedata.config import REVOCATION_SETTLE_BOUND, REVOCATION_SETTLE_MIN_S
from expcore_onedata.tokens import RunTokens, TokenIssuer

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

    The caller makes sure no mint of the slot is under way when it starts (its run ended and no mint or start thread
    of it runs, or the slot's lease lock is held). A line of the slot journalled after the start line all the same is
    never capped by this revocation, so it keeps its own end.
    """
    if (
        isinstance(settle_s, bool)
        or not isinstance(settle_s, int | float)
        or not REVOCATION_SETTLE_MIN_S <= settle_s < float("inf")
    ):
        raise ValueError(f"a revocation's settle time must be a finite number of seconds {REVOCATION_SETTLE_BOUND}")
    name = secrets.token_hex(8)
    # durably before the first bump: the revoked line caps only what precedes this, never a mint journalled after it
    journal.record_revoking(slot.name, revocation=name, started_at=clock())
    generations = {"data_path": issuer.revoke_own(slot.data_path), "tool_server": issuer.revoke_own(slot.tool_server)}
    held = [t for t in (tokens.data_path, tokens.tool_server) if t is not None] if tokens is not None else []
    for token in held:
        issuer.require_revoked(token.reveal())
    now = clock()
    done = Revoked(now, now + settle_s, generations, len(held))
    journal.record_revoked(
        slot.name,
        revocation=name,
        revoked_at=done.revoked_at,
        settle_until=done.settle_until,
        generations=generations,
        verified=done.verified,
    )
    return done


__all__ = ["Revoked", "revoke_slot"]
