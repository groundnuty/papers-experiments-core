# Provenance: new
"""The PID namespaces the mount watcher has seen only while their cells were being built (`mountproof`, #179, O60).

A nested process whose mount table is still the host's is not judged (`CellPoll`): it is pending, under its PID
namespace (its own `(pid, start time)` while that cannot be read). A namespace is settled when one of its processes is
judged. One left with no live process is lost, but the loss is counted a poll after it is seen: the last helper of a
namespace may have forked a successor that the poll's listing, made before the fork, did not hold.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

Seen = tuple[int, int]  # a process by its pid and start time


class Pending:
    """The pending namespaces with their processes, and those lost at the last poll."""

    def __init__(self, started: Callable[[int], int | None]) -> None:
        self._started = started  # a process's start time now, or None when it is gone
        self._sets: dict[Any, set[Seen]] = {}
        self._vanished: set[Any] = set()

    def add(self, key: Any, seen: Seen, track: bool) -> None:
        """A process seen being built. It leaves any other namespace it was pending under (its link could not be read,
        or could, since the last look), and joins `key` when `track`, which is not so for a namespace already proved."""
        self.drop(seen)
        if track:
            self._sets.setdefault(key, set()).add(seen)

    def drop(self, seen: Seen) -> None:
        """A process is no longer pending anywhere; a namespace left with none goes, uncounted."""
        for key, members in list(self._sets.items()):
            members.discard(seen)
            if not members:
                del self._sets[key]

    def settled(self, key: Any, seen: Seen) -> None:
        """A process is judged: its namespace and the process are no longer pending."""
        self._sets.pop(key, None)
        self.drop(seen)

    def lost(self, proved: Collection[Any]) -> bool:
        """Whether a namespace seen vanishing at the last poll is still unproved and without a process (so was never
        proved); then this poll's own vanished ones are noted for the next."""
        gone = self._gone(proved)
        self._vanished = set()
        for key, members in list(self._sets.items()):
            alive = {m for m in members if self._started(m[0]) == m[1]}
            if alive:
                self._sets[key] = alive
            else:
                del self._sets[key]
                self._vanished.add(key)
        return gone

    def unsettled(self, proved: Collection[Any]) -> bool:
        """Whether a namespace is still pending, or was lost at the last poll and is unproved (read at the close)."""
        return bool(self._sets) or self._gone(proved)

    def _gone(self, proved: Collection[Any]) -> bool:
        return any(key not in proved and key not in self._sets for key in self._vanished)
