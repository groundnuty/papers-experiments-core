# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:172-255 (pending cleanups, sampled PIDs)
#   changes: one object a driver owns, not module-level state, so tests and campaigns do not share it
"""What a driver remembers across sandboxes: starts whose cleanup was incomplete, and the PIDs it has seen.

A sandbox whose start failed and whose cleanup then failed too (the daemon was in trouble) is registered here and
retried at the next start, so it does not leak. The PIDs seen in any sandbox of this driver tell another run's
out-of-memory victim from one nobody saw.
"""

import logging
import threading
from typing import Protocol

log = logging.getLogger(__name__)


class Stoppable(Protocol):
    def stop(self) -> None: ...


class Registry:
    """Pending cleanups and sampled PIDs of one driver. Safe from several threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._drain_lock = (
            threading.Lock()
        )  # one drain at a time; a concurrent start skips instead of waiting on docker
        self._pending: list[Stoppable] = []
        self._pids: set[int] = set()

    def add_pending(self, sandbox: Stoppable) -> None:
        """Register a sandbox whose cleanup is incomplete; `drain` retries it."""
        with self._lock:
            if sandbox not in self._pending:
                self._pending.append(sandbox)

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def drain(self) -> int:
        """Retry the cleanup of every pending sandbox; return how many are still not clean."""
        if not self._drain_lock.acquire(blocking=False):  # another thread is draining: its result is as good as ours
            return self.pending()
        try:
            with self._lock:
                todo = list(self._pending)
            for sandbox in todo:
                try:
                    sandbox.stop()
                except Exception:
                    log.warning("a sandbox's cleanup is still incomplete", exc_info=True)
                    continue  # still pending; an interrupt propagates and leaves it registered
                with self._lock:
                    if sandbox in self._pending:
                        self._pending.remove(sandbox)
            return self.pending()
        finally:
            self._drain_lock.release()

    def note_pids(self, pids: set[int]) -> None:
        with self._lock:
            self._pids.update(pids)

    def has_seen(self, pid: int) -> bool:
        with self._lock:
            return pid in self._pids


DEFAULT = Registry()
