# Provenance: new
"""A running lease's `out/`, snapshotted to its place on the shared file system (#462 step 6, C46).

A lease store lives on the node's memory filesystem, which a lost federation takes with it, and a SQLite store is never
live on the shared one. So while a lease runs, its worker copies the store's `out/` there with
`expcore.campaign.out_snapshot.take` (the store by the online backup, then `runs/`, verified and swapped in by rename),
as a shard step does (`deploy/hpc-federation/shards.py`): every `SNAPSHOT_S` seconds and within `POLL_S` of an attempt's
beginning or end (`runs/` appears, then its markers change: a lease is one run, and a federation lost in its first
attempt must leave a store to read), and once more after the run exited, before its archive. A controller that must
settle a lost lease reads the newest snapshot that verifies, with the lease's spend journal beside it
(`elastic.channel`, `places/lease-<id>/`).

**A snapshot that fails is reported and never ends the run**: the last good one stands (a worker that stopped beating
for a slow shared file system would lose the lease it was protecting). The worker removes no snapshot and no journal
(`take` itself keeps the newest `out_snapshot.KEEP`): if the archive cannot be fetched, the lease is lost and they are
its evidence.
"""

import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expcore.campaign import out_snapshot

SNAPSHOT_S = 900.0  # a shard step's `snapshot_every_s`
POLL_S = 30.0  # how often a run's end is looked for in `runs/`
SNAPSHOTS = "snapshots"  # under a lease's place

Take = Callable[..., Path]


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LeaseSnapshots:
    """One lease's snapshots: `poll()` takes one when it is due, `start()` polls from a thread of its own, `finish()`
    stops it and takes the last."""

    def __init__(
        self,
        out: Path,
        into: Path,
        scratch: Path,
        facts: Mapping[str, Any],
        *,
        every_s: float = SNAPSHOT_S,
        poll_s: float = POLL_S,
        take: Take = out_snapshot.take,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.out, self.into, self.scratch, self.facts = out, into, scratch, facts
        self.every_s, self.poll_s, self._take, self._clock, self._wall = every_s, poll_s, take, clock, wall
        self._last = clock()
        self._seen = out_snapshot.ended_attempts(out)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _once(self, live: bool) -> None:
        """One snapshot, when the store exists; a failure is reported and left behind."""
        if not (self.out / out_snapshot.STORE).is_file():
            return
        try:
            self._take(self.out, self.into, scratch=self.scratch, facts=self.facts, live=live, now=stamp(self._wall()))
        except Exception as error:  # the last good snapshot stands; the run goes on
            print(f"worker: a snapshot of the lease's out/ failed ({type(error).__name__}: {error})", file=sys.stderr)

    def poll(self) -> None:
        """Take a snapshot if the count of ended attempts' markers changed since the last one (`runs/` appearing is
        the first change: an attempt began, and a run shorter than `every_s` lost with its federation would otherwise
        leave a journal and no store) or `every_s` passed."""
        now = out_snapshot.ended_attempts(self.out)
        changed = now is not None and now != self._seen
        if changed or self._clock() - self._last >= self.every_s:
            self._once(live=True)
            self._last = self._clock()
            self._seen = now if now is not None else self._seen

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.poll_s):
                try:
                    self.poll()
                except Exception as error:  # a reading that fails says nothing ended; the clock takes the next one
                    print(f"worker: a snapshot poll failed ({type(error).__name__}: {error})", file=sys.stderr)

        self._thread = threading.Thread(target=loop, daemon=True, name="lease-snapshots")
        self._thread.start()

    def stop(self) -> None:
        """Stop polling and wait for a snapshot in progress."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def finish(self) -> None:
        """Stop, and take the last snapshot, of a store its dispatcher has left (read from a private copy)."""
        self.stop()
        self._once(live=False)
