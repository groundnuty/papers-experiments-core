# Provenance: new
"""The per-provider bound on a reset's per-file reads, across every space and reading of the process (O46, O51).

A reset of several spaces works on `[placement] reset_concurrency` spaces at once (4), each reading `[placement]
concurrency` block maps at once (16), and the spaces of one federation share their providers: 64 reads in flight at one
provider with the defaults, and a planner's readings (`resetplan.plan_scan`) on top when a study makes them at once.
`[placement] provider_concurrency` (16 by default, a single space's own level) bounds them at each provider: a read
takes one of its provider's slots before it is sent and gives it back when it ends, its retries and their pauses
included, so a provider whose reads fail is not asked faster while they do. The slots are the process's, one set per
provider address and bound, shared by every provisioner and client of the process, so spaces read at once share them.
Each reading's record says the bound (`policy.provider_concurrency`) and, per provider, the seconds its reads waited
for a slot (`slot_wait_s`, `readretry.ReadRetry.facts`).
"""

import threading

_LOCK = threading.Lock()
_SLOTS: dict[tuple[str, int], threading.BoundedSemaphore] = {}


def slots(address: str, bound: int) -> threading.BoundedSemaphore:
    """The slots of the provider at `address` under `bound`, made at first use and then the process's for good. Two
    bounds for one address are two sets: the spaces of one federation share one `[placement]` table, so they agree."""
    if isinstance(bound, bool) or not isinstance(bound, int) or bound < 1:
        raise ValueError("a provider's bound on reads in flight is a whole number, one or more")
    with _LOCK:
        return _SLOTS.setdefault((address, bound), threading.BoundedSemaphore(bound))


__all__ = ["slots"]
