# Provenance: new
"""A complete reset of one space between runs, each step verified and timed (O01, O07, O08, O09, O11).

A run's agent can leave more behind than files on a provider: a QoS requirement goes on replicating into the local
provider after the next run's start state was verified, a transfer still running changes a replica later, and the
listing of ended transfers shows the next agent what earlier runs replicated. `SpaceReset.run` takes them in order,
on the space its provisioner is bound to, and records the time of each stage, a resource-projection figure:

1. **qos**: every QoS requirement in the space is found and removed. There is no listing of a space's requirements
   (Oneprovider 25.0's REST description has `/qos_requirements` and `/qos_requirements/{qid}` and, per entry,
   `/data/{id}/qos/summary`, nothing under `/spaces/{sid}`), so the stage walks the space, listing each directory once,
   and reads the summary of the root and of every leaf, a file or an empty directory: a leaf's summary holds the
   requirements on it and on every ancestor, so together they hold every requirement. Each is removed through the
   mutation gate (its space read from its record's `fileId`), each must then answer 404 on consecutive reads, and a
   second walk must find none.
2. **transfers**: every waiting and ongoing transfer of the space, as each supporting provider lists it, is cancelled
   through the gate (its space read from its record's `spaceId`); a cancel answered 400 is a job that ended on its
   own. Each cancelled job must then report an ended state, and the listings must show nothing waiting or ongoing on
   every provider in two observations in a row, `[placement] transfer_settle_s` apart (the listing's lag, O07),
   within twice `transfer_settle_polls` observations.
3. **eviction**: the files the caller names leave each provider named (`eviction.py`): a directory in one transfer, a
   set of files in parallel under `[placement] concurrency`, verified together on their block maps (O01). The paths
   are resolved with one listing per directory. The data provider's own copy is refused before any stage runs (O09).
4. **history**: ended transfers cannot be removed. The 25.0 REST description offers, for a transfer, only `GET` and
   `DELETE /transfers/{tid}`, a cancel that answers 400 for a job that has ended, and `POST .../rerun`; the space's
   listing (`GET /spaces/{sid}/transfers`) only reads. So the next agent sees every ended transfer, the reset's own
   included, and the stage records what the listing shows it, per provider and together: the count and the newest
   finish time, read from each record, so an analysis can use it, and `removable` false, its limit stated.

**Fail closed.** A stage that cannot verify what it did, a read that fails, an answer in another shape, a bound
reached, leaves the reset failed: `run` raises `ResetFailed` naming the stage, with the record of the stages so far
(each with its seconds), and runs no later stage. The caller must not start the next run then. Nothing is assumed:
a provider that does not answer is a failure, not an empty listing. Every message names the space by its position when
several are declared, and a provider by its position, never a name.

The waits happen between runs, never in an agent's time (`consistency.guard`).
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import NotSent, PlatformError, answered_absent
from expcore_onedata.eviction import evict_ids, evict_tree
from expcore_onedata.provision import Provisioner, ProvisionError
from expcore_onedata.transferlog import PAGE_LIMIT
from expcore_onedata.transferrecord import Unreadable, page_of

STAGES = ("qos", "transfers", "eviction", "history")
ACTIVE = ("waiting", "ongoing")  # the listing's states of a job that has not ended
LISTED_MAX = 100_000  # transfer ids one provider's listing may hold before it counts as unreadable, not cut
_FAILURES = (PlatformError, ProvisionError, consistency.WaitTimeout, Unreadable, KeyError)


class ResetFailed(ProvisionError):
    """A stage of the reset could not show that it did what it must; the next run must not start.

    `stage` is the stage that failed and `record` what the reset recorded up to it, each stage with its seconds."""

    def __init__(self, message: str, stage: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.stage = stage
        self.record = record


class SpaceReset:
    """The complete reset of the space a provisioner is bound to, over the providers that support it."""

    def __init__(
        self, provisioner: Provisioner, *, providers: Sequence[str] | None = None, listed_max: int = LISTED_MAX
    ) -> None:
        """`providers` are the ones whose transfer listings are read; by default every provider of the space."""
        support = provisioner.space.support.providers
        self._p, self._providers = provisioner, list(providers if providers is not None else support)
        if not self._providers or any(name not in support for name in self._providers):
            raise ValueError("a reset reads the listings of providers that support its space, at least one")
        if listed_max < 1:
            raise ValueError("a listing's bound is at least one id")
        self._listed_max = listed_max

    # -- the run -----------------------------------------------------------------
    def run(self, evict: Mapping[str, Iterable[str]] | None = None) -> dict[str, Any]:
        """Run the four stages in order and return the record; raise ResetFailed at the first that fails.

        `evict` maps a provider to the space-relative paths of what must leave it: a directory leaves in one
        transfer, files together. Every provider is checked before anything runs: none may be the data provider."""
        plan = {provider: list(dict.fromkeys(paths)) for provider, paths in (evict or {}).items()}
        for provider in plan:
            self._p.refuse_holder(provider)
            self._p.client.provider_label(provider)  # an undeclared provider is refused here, before any stage
        steps: dict[str, Callable[[], dict[str, Any]]] = {
            "qos": self._qos,
            "transfers": self._transfers,
            "eviction": lambda: self._evict(plan),
            "history": self._history,
        }
        record: dict[str, Any] = {"space": self._p.space.position, "ok": False, "stages": {}, "seconds": None}
        began = self._p.clock()
        for stage in STAGES:
            started = self._p.clock()
            try:
                facts = steps[stage]()
            except _FAILURES as error:
                record["stages"][stage] = {"ok": False, "seconds": round(self._p.clock() - started, 3)}
                record["stages"][stage]["error"] = str(error)
                record["seconds"] = round(self._p.clock() - began, 3)
                raise ResetFailed(
                    f"{self._p.where}the reset failed at its {stage} stage ({type(error).__name__}): {error}; "
                    "the next run must not start",
                    stage,
                    record,
                ) from error
            record["stages"][stage] = {"ok": True, "seconds": round(self._p.clock() - started, 3), **facts}
        record.update(ok=True, seconds=round(self._p.clock() - began, 3))
        return record

    # -- 1. QoS requirements --------------------------------------------------------
    def _summary(self, file_id: str) -> set[str]:
        """The ids of the QoS requirements on an entry and its ancestors, from its summary on the data provider."""
        answer = self._p.client.get(f"/data/{file_id}/qos/summary", provider=self._p.settings.holder)
        requirements = answer.get("requirements") if isinstance(answer, dict) else None
        if not isinstance(requirements, Mapping) or not all(isinstance(q, str) and q for q in requirements):
            raise Unreadable("a QoS summary holds no table of requirements keyed by id")
        return set(requirements)

    def _requirements(self) -> tuple[list[str], int]:
        """Every QoS requirement in the space, and how many summaries were read: the root's and every leaf's."""
        root = self._p.root()
        entries = [root, *(entry.file_id for entry, leaf in self._p.walk(root) if leaf)]
        found = consistency.bounded_map(self._summary, entries, self._p.settings.concurrency)
        return sorted(set().union(*found)), len(entries)

    def _qos_gone(self, qid: str) -> bool:
        """Whether a removed requirement answers that it is not there (`answered_absent`: a 404, or 25.0's 400 posix
        enoent); any other failure, another 400 included, propagates and does not count."""
        try:
            self._p.client.get(f"/qos_requirements/{qid}", provider=self._p.settings.holder)
        except PlatformError as error:
            if answered_absent(error):
                return True
            raise
        return False

    def _qos(self) -> dict[str, Any]:
        """Find every requirement, remove each, and show them gone: each answers 404, and a second walk finds none."""
        found, read = self._requirements()
        if not found:
            return {"found": 0, "removed": 0, "summaries_read": read}
        for qid in found:
            self._p.client.mutate("DELETE", f"/qos_requirements/{qid}", provider=self._p.settings.holder)
        settings = self._p.settings
        gone = consistency.converge_each(
            found,
            self._qos_gone,
            bool,
            timeout_s=settings.settle_timeout_s,
            poll_s=settings.poll_s,
            consecutive=settings.consecutive_polls,
            workers=settings.concurrency,
            clock=self._p.clock,
            sleep=self._p.sleep,
        )
        if not gone.converged:
            raise ProvisionError(f"{len(gone.pending)} QoS requirement(s) still answer after their removal")
        again, read_again = self._requirements()
        if again:
            raise ProvisionError(f"the walk after the removals still finds {len(again)} QoS requirement(s)")
        return {"found": len(found), "removed": len(found), "summaries_read": read + read_again}

    # -- 2. running transfers ---------------------------------------------------------
    def _listed(self, provider: str, state: str) -> list[str]:
        """Every id one provider lists in one state, page by page to the end; raise Unreadable past the bound."""
        path, ids, token = f"/spaces/{self._p.space_id}/transfers", [], None
        for _ in range(self._listed_max // PAGE_LIMIT + 1):
            params = {"state": state, "limit": PAGE_LIMIT, **({"page_token": token} if token else {})}
            page, token = page_of(self._p.client.get(path, provider=provider, params=params))
            ids += page
            if len(ids) > self._listed_max:
                break
            if token is None:
                return list(dict.fromkeys(ids))
        label = self._p.client.provider_label(provider)
        raise Unreadable(f"{label}: the {state} listing is longer than {self._listed_max} ids: unknown, not cut")

    def _active(self) -> dict[str, str]:
        """Every waiting or ongoing job any provider lists, with the first provider that lists it."""
        active: dict[str, str] = {}
        for provider in self._providers:
            for state in ACTIVE:
                for tid in self._listed(provider, state):
                    active.setdefault(tid, provider)
        return active

    def _cancel(self, tid: str, provider: str) -> None:
        """Cancel one job through the gate; a 400 answered to the cancel is a job that ended before it, which the wait
        then shows. A cancel the gate refused unsent (`NotSent`, which keeps the status of the gate's own read, a 400
        among them) is never taken for that: it propagates, as does any other failure."""
        try:
            self._p.client.mutate("DELETE", f"/transfers/{tid}", provider=provider)
        except PlatformError as error:
            if isinstance(error, NotSent) or error.status != 400:
                raise

    def _transfers(self) -> dict[str, Any]:
        """Cancel every job that has not ended until two observations in a row show none, within the bound."""
        settings = self._p.settings
        cancelled: dict[str, str] = {}
        clean = observations = 0
        bound = 2 * settings.transfer_settle_polls
        while True:
            observations += 1
            active = self._active()
            fresh = {tid: provider for tid, provider in active.items() if tid not in cancelled}
            for tid, provider in fresh.items():
                self._cancel(tid, provider)
                cancelled[tid] = provider
            for provider in dict.fromkeys(fresh.values()):
                consistency.require_ended(
                    consistency.wait_transfers(
                        self._p.client,
                        provider,
                        [tid for tid, where in fresh.items() if where == provider],
                        timeout_s=settings.settle_timeout_s,
                        poll_s=settings.poll_s,
                        workers=settings.concurrency,
                        clock=self._p.clock,
                        sleep=self._p.sleep,
                    ),
                    "a cancelled transfer",
                )
            clean = 0 if active else clean + 1
            if clean >= 2:
                return {"cancelled": len(cancelled), "observations": observations}
            if observations >= bound:
                raise ProvisionError(
                    f"the listings still showed {len(active)} transfer(s) waiting or ongoing, or had not shown none "
                    f"twice in a row, after {observations} observations"
                )
            self._p.sleep(settings.transfer_settle_s)

    # -- 3. eviction ------------------------------------------------------------------
    def _evict(self, plan: Mapping[str, list[str]]) -> dict[str, Any]:
        """Evict each provider's paths: a directory in one transfer, the files together, all verified on block maps."""
        resolved = self._p.resolve_many(path for paths in plan.values() for path in paths)
        out: dict[str, Any] = {}
        for provider, paths in plan.items():
            entries = [resolved[path] for path in paths]
            results = [evict_tree(self._p, entry, provider) for entry in entries if entry.kind == "DIR"]
            files = [entry.file_id for entry in entries if entry.kind != "DIR"]
            if files:
                results.append(evict_ids(self._p, files, provider))
            out[self._p.client.provider_label(provider)] = {
                "trees": sum(1 for entry in entries if entry.kind == "DIR"),
                "files": sum(r["files"] for r in results),
                "transfers": sum(r["transfers"] for r in results),
                "polls": max((r["polls"] for r in results), default=0),
            }
        return {"providers": out}

    # -- 4. ended-transfer history ------------------------------------------------------
    def _finish(self, provider: str, tid: str) -> float:
        """An ended job's finish time, from its record; one without a number there is unreadable."""
        record = self._p.client.get(f"/transfers/{tid}", provider=provider)
        value = record.get("finishTime") if isinstance(record, dict) else None
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise Unreadable("an ended transfer's record has no finishTime that is a number")
        return float(value)

    def _history(self) -> dict[str, Any]:
        """What the ended listing shows the next agent, per provider and together: the count and the newest finish."""
        per: dict[str, Any] = {}
        newest: dict[str, float] = {}
        for provider in self._providers:
            ids = self._listed(provider, "ended")
            times = consistency.bounded_map(lambda t, p=provider: self._finish(p, t), ids, self._p.settings.concurrency)
            per[self._p.client.provider_label(provider)] = {
                "ended": len(ids),
                "newest_finish": max(times, default=None),
            }
            for tid, moment in zip(ids, times, strict=True):
                newest[tid] = max(newest.get(tid, moment), moment)
        return {
            "removable": False,  # no call of the API removes an ended transfer (the module docstring has the evidence)
            "ended": len(newest),
            "newest_finish": max(newest.values(), default=None),
            "providers": per,
        }


__all__ = ["STAGES", "ResetFailed", "SpaceReset"]
