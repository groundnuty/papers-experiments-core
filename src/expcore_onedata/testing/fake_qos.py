# Provenance: new
"""How a fake QoS requirement reaches each provider, the status it reports, and how its replication ends (#209).

`FakeData` takes this in (`FakeQos`); its own docstring lists the QoS calls. The build is op-worker `f118ed9`, read
for #209 (`expcore_onedata.resetremove` cites the source).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

STATUSES = ("fulfilled", "pending", "impossible")  # a requirement's statuses, as the record and the summary give them


@dataclass
class Landing:
    """One file of a QoS replication: it lands whole on `provider` at `when`, unless the requirement's removal reached
    that provider before then; `survives` is a file whose cancel missed, which lands whatever the removal."""

    when: float
    qid: str
    file_id: str
    provider: str
    survives: bool = False


class FakeQos:
    """The dbsync, status and replication model of a fake's QoS requirements, for `FakeData`.

    Models:

    - **dbsync.** A requirement added through a provider (`POST /qos_requirements` there, or `add_qos(...,
      through=)`) is visible there at once and on every other provider `qos_sync_s` seconds of `clock` later
      (`qos_sync_by_provider` gives a provider its own delay). A removal (`DELETE /qos_requirements/{qid}`) reaches the
      provider it was sent through at once and every other provider after its delay; until it reaches a provider,
      that provider still answers the requirement: its record, the summaries and the listing's marks. The default 0
      makes both instant everywhere, as before. `add_qos` with no `through` is a test's setup, visible everywhere at
      once. `qos_removed` maps a requirement to the clock time its removal reaches each provider, and the requirement
      leaves `qos` once its removal has reached every provider.
    - **Status.** A requirement is `pending` while its replication has a file still to land, and `fulfilled` after;
      `qos_status_of` gives one a status of its own (`impossible`, or a `pending` that never ends). The record's
      `status` and the summary's entry give it.
    - **Replication.** With `qos_replicates_to`, a new requirement's regular files land whole on that provider one
      after another, the first `qos_land_after_s` seconds of `clock` after it was added and each next one
      `qos_land_every_s` later (0: together). `qos_reconcile(qid)` starts the replication again, as a change of a
      file's location does in the build (`replica_dbsync_hook.erl:95,370`). A file due before the removal reaches the
      replicating provider lands, so the replication goes on there until the removal reaches it; what is still to
      land then is cancelled (`qos_traverse.erl:88-93`), unless `qos_missed_cancel` is set: then the file next due
      still lands, whole, and the rest are dropped, since a cancel can miss and leave at most the one file in flight
      (`replica_synchronizer.erl:224-231`). `qos_landed` lists every landing as `(when, qid, file id, provider id)`.

    Does not model: the build's aggregation of a requirement's status over every provider that supports the space,
    where a provider that does not know the requirement answers `pending` (`qos_status.erl:107-137,122-127`): here
    that provider answers that the requirement is not there. Nor the bytes of a file that land after a cancel that did
    not miss (none land), a partial file, an expression's evaluation, or an eviction a requirement still protects.
    """

    # What it reads of `FakeData`: the requirements, where a new one replicates, the providers, the clock and the tree.
    qos: dict[str, dict]
    qos_replicates_to: str | None
    qos_land_after_s: float
    providers: dict[str, dict]
    clock: Callable[[], float]
    _regular_under: Callable[[str], list[str]]

    def _init_qos_model(self) -> None:
        self.qos_sync_s = 0.0  # seconds of `clock` a requirement's addition or removal takes to reach another provider
        self.qos_sync_by_provider: dict[str, float] = {}  # provider id -> its own delay, over the one above
        self.qos_land_every_s = 0.0  # seconds between two files of one replication landing (0: together)
        self.qos_missed_cancel = False  # a removal's cancel misses the file next due, which still lands whole
        self.qos_status_of: dict[str, str] = {}  # requirement id -> the status it reports, whatever its replication
        self.qos_removed: dict[str, dict[str, float]] = {}  # requirement id -> provider id -> when its removal is there
        self.qos_landed: list[tuple[float, str, str, str]] = []  # (when, requirement id, file id, provider id)
        self._qos_added: dict[str, tuple[str | None, float]] = {}  # requirement id -> (provider added through, when)
        self._qos_landings: list[Landing] = []

    # -- dbsync ------------------------------------------------------------------
    def _qos_delay(self, provider: str) -> float:
        return self.qos_sync_by_provider.get(provider, self.qos_sync_s)

    def _qos_added_at(self, qid: str, through: str | None) -> None:
        """Note that a requirement was added through a provider (None: a test's setup, everywhere at once)."""
        self._qos_added[qid] = (through, self.clock())

    def _qos_reaches(self, qid: str, provider: str) -> bool:
        """Whether a provider has a requirement now: its addition has reached it and its removal has not."""
        through, added = self._qos_added.get(qid, (None, -math.inf))
        if through not in (None, provider) and self.clock() < added + self._qos_delay(provider):
            return False
        return self.clock() < self.qos_removed.get(qid, {}).get(provider, math.inf)

    def _qos_remove(self, qid: str, through: str) -> None:
        """Remove a requirement through a provider: there at once, on every other provider after its delay; with
        `qos_missed_cancel`, the file of its replication next due where the removal arrives still lands."""
        now = self.clock()
        reaches = {p: now if p == through else now + self._qos_delay(p) for p in self.providers}
        self.qos_removed[qid] = reaches
        if self.qos_missed_cancel:
            for provider, when in reaches.items():
                due = [x for x in self._qos_landings if x.qid == qid and x.provider == provider and x.when >= when]
                if due:
                    min(due, key=lambda x: x.when).survives = True
        self._advance_qos()

    # -- replication -------------------------------------------------------------
    def _qos_schedule(self, qid: str, file_id: str, provider: str) -> None:
        """Schedule a replication of the regular files at or under an entry into a provider, one after another."""
        start = self.clock() + self.qos_land_after_s
        for i, regular in enumerate(self._regular_under(file_id)):
            self._qos_landings.append(Landing(start + i * self.qos_land_every_s, qid, regular, provider))

    def qos_reconcile(self, qid: str) -> None:
        """Start a requirement's replication into `qos_replicates_to` again, as a file's location change does."""
        if self.qos_replicates_to is None:
            raise ValueError("qos_reconcile replicates into qos_replicates_to, which is not set")
        self._qos_schedule(qid, self.qos[qid]["fileId"], self.qos_replicates_to)

    def _qos_doomed(self, landing: Landing) -> bool:
        """Whether the requirement's removal reaches the landing's provider before the file is due, and cancels it."""
        reached = self.qos_removed.get(landing.qid, {}).get(landing.provider, math.inf)
        return reached <= landing.when and not landing.survives

    def _qos_status(self, qid: str) -> str:
        """A requirement's status: its own, if a test gave it one, else pending while a file is still to land."""
        if qid in self.qos_status_of:
            return self.qos_status_of[qid]
        waiting = any(x.qid == qid and not self._qos_doomed(x) for x in self._qos_landings)
        return "pending" if waiting else "fulfilled"

    def _advance_qos(self) -> None:
        """Land every file whose time has come, drop the ones a removal cancelled, and forget a requirement whose
        removal has reached every provider. Each call of a provider runs this first."""
        now = self.clock()
        for landing in sorted((x for x in self._qos_landings if x.when <= now), key=lambda x: x.when):
            self._qos_landings.remove(landing)
            if not self._qos_doomed(landing):
                self._qos_place(landing)
        for qid, reaches in list(self.qos_removed.items()):
            if qid in self.qos and max(reaches.values(), default=now) <= now:
                del self.qos[qid]

    def _qos_place(self, landing: Landing) -> None:
        """Place the whole file on the landing's provider and log it."""
        node = self.nodes.get(landing.file_id)  # type: ignore[attr-defined]
        if node is not None and node.content:
            self.blocks[(landing.file_id, landing.provider)] = [[0, len(node.content)]]  # type: ignore[attr-defined]
            self.qos_landed.append((landing.when, landing.qid, landing.file_id, landing.provider))


__all__ = ["STATUSES", "FakeQos", "Landing"]
