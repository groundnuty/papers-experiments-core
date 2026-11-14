# Provenance: new
"""The fallback collection of a client that moves large bodies (#259, O47).

The fix for large bodies kept alive in reference cycles is at the source: the client closes every answer and cuts the
cycle httpx builds around it (`client._release`), so a body is freed the moment its exchange is used. This is the net
under that fix and not the fix: a cycle that arises some other way (a caller's own, a library's) still holds its bytes
until a full collection, so after a fixed number of bytes sent and received the client asks for one.

The count is of the bytes of bodies sent and received since the client last collected, those of an exchange that
failed included. When it has reached the budget, the collection runs at the start of the NEXT exchange, never in the
one that spent the budget: that one's own request and answer are still alive when it ends, and a collection run then
would leave them alive. A client's exchanges are counted only once their answer is released, so a collection that
another thread starts as one ends finds no cycle of it. At most one full collection runs per budget of bytes moved, so
its cost is bounded by the transfer it follows, and a client with no budget never collects.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Callable

COLLECT_AFTER_BYTES = 128 * 1024 * 1024  # a full collection at most once per this many bytes of bodies moved


def check_budget(collect_after_bytes: int | None) -> int | None:
    """Return a budget that is a positive whole number of bytes, or None for no fallback; refuse anything else."""
    if collect_after_bytes is None:
        return None
    if isinstance(collect_after_bytes, bool) or not isinstance(collect_after_bytes, int) or collect_after_bytes < 1:
        raise ValueError("collect_after_bytes is a positive whole number of bytes, or None for no fallback collection")
    return collect_after_bytes


class CollectBudget:
    """Counts the bytes of bodies an HTTP client has moved since its last collection and runs a full one when due.

    One client may serve several threads, so the count is guarded; the collection itself runs outside the guard, since
    it may run any finalizer. `collect` is `gc.collect`, looked up as the budget is built, unless a caller counts the
    collections instead."""

    def __init__(
        self, collect_after_bytes: int | None = COLLECT_AFTER_BYTES, *, collect: Callable[[], object] | None = None
    ) -> None:
        self._after = check_budget(collect_after_bytes)
        self._collect = gc.collect if collect is None else collect
        self._pending = 0
        self._lock = threading.Lock()

    def spent(self, size: int) -> None:
        """Count `size` bytes of bodies sent or received."""
        with self._lock:
            self._pending += size

    def before_exchange(self) -> None:
        """Collect now if the bodies moved since the last collection have reached the budget; call it as an exchange
        begins, when the last one's request and answer are no longer in use."""
        with self._lock:
            due = self._after is not None and self._pending >= self._after
            if due:
                self._pending = 0
        if due:
            self._collect()
