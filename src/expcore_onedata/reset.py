# Provenance: new
"""A complete reset of one space between runs, each step verified and timed (O01, O07, O08, O09, O11).

A run's agent can leave more behind than files on a provider: a QoS requirement goes on replicating into the local
provider after the next run's start state was verified, a transfer still running changes a replica later, and the
listing of ended transfers shows the next agent what earlier runs replicated. `SpaceReset.run` takes them in order,
on the space its provisioner is bound to, and records the time of each stage, a resource-projection figure. Every
read and every check is made on each provider that supports the space, since each lists what it has seen:

1. **qos**: every QoS requirement in the space is found (there is no listing of a space's requirements; how they are
   found, a summary per leaf or one recursive listing, is `[placement] qos_scan`, `resetqos.py`), and each one not in
   the caller's baseline (`qos_baseline`, the run's start's, from `qos_requirements()`) is removed through the
   mutation gate (bound to this space, its record's `fileId` resolved by its parent links), must then answer that it is
   not there (`client.answered_absent`: a 404, or 25.0's 400 posix enoent; any other 400 fails closed) on consecutive
   reads on every provider, and a second scan must find none but the baseline's (O33).
   **qos_settle**: a QoS replication creates no transfer to wait for, so the block maps of the files a removed
   requirement applied to must stay the same on every provider for `resetqos.SETTLE_STABLE_S`, within
   `resetqos.SETTLE_BOUND_S`.
2. **transfers**: every waiting and ongoing transfer, on every provider that lists it, is cancelled there through the
   gate (bound to this space, its record's `spaceId` resolved); a cancel answered 400 is a job that has ended, never
   one the gate refused unsent. Each must then report an ended state on that provider, and the listings must show
   nothing waiting or ongoing on every provider in `transfer_settle_polls` observations in a row, `transfer_settle_s`
   apart (the listing's lag, O07), within twice that many observations.
3. **eviction**: the files the caller names leave each provider named (`eviction.py`): a directory in one transfer, a
   set of files in parallel under `[placement] concurrency`, each scheduled again while its block map still shows bytes
   after the transfers ended (an eviction on 25.0 can complete having evicted nothing), up to `EVICTION_ATTEMPTS`.
   Once the eviction's transfers have ended, the listings must show no transfer running on any provider before the
   block maps are read, so a map is never read while a job still changes it (O01); and once every eviction is verified,
   the listings must show none running again.
4. **occupancy**: with no transfer running on any provider (the listings are read first, as above), the space root's
   distribution gives each provider's physical size, the bytes its storage holds of the space; never the logical
   figures, which lag. A provider other than the data provider that holds more than the caller allows (nothing by
   default) fails the reset, so a reset without an eviction plan still shows that only the owner holds data.
   **quiet**: when a requirement was removed, its files' block maps are read, again after `resetqos.quiet_wait_s`
   (twice the occupancy's longest lag for the deepest of them, from the source), and the occupancy with them: a
   provider holding more of one of those files, or a provider other than the data provider
   holding more of the space than at the occupancy stage, fails the reset (a replication that landed after the
   eviction). The block maps decide; the occupancy, which grows late, is a second sign and never alone the proof.
5. **history**: ended transfers cannot be removed. The 25.0 REST description offers, for a transfer, only `GET` and
   `DELETE /transfers/{tid}`, a cancel that answers 400 for a job that has ended, and `POST .../rerun`; the space's
   listing (`GET /spaces/{sid}/transfers`) only reads. So the next agent sees every ended transfer, the reset's own
   included, and the stage records what the listing shows it, per provider and together, since the window's start
   (`since`: the run's start when the caller gives it, else the reset's own start, on the reset's epoch clock `now`):
   the count of the jobs that ended since then, the newest finish time, read from the records, whether older ended
   jobs are listed too (`earlier`), and `removable` false, its limit stated. The history grows with every run, so the
   listing is read newest first down to the window less the clock-skew allowance and `transferorder.STOP_MARGIN_S`,
   its order checked as it goes, never whole (O32); `LISTED_MAX` counts the jobs in that window.

**Before any stage** the plan is checked (no provider to evict from or allowed to hold bytes is the data provider, by
name or id, or one that does not support the space) and the space's live name is verified (`verify_space`).

**Reads that time out are tried again, boundedly and in the log** (O29, `readretry.py`): the reset's own reads (the
walks' listings, the summaries, the transfer listings, records and waits, the eviction stage's waits for its transfers,
the occupancy, a space's root and the gate's reads before each cancel and removal) go under the provisioner's policy,
three attempts with pauses of 2 s and 5 s, and the record says how many were retried (`reads_retried`). A read inside
a poll that counts a failed read as one that did not match (the requirements' removal check) is one attempt: the poll is
the retry. Nothing but a read that timed out is tried again: an error answer, a connection error and every mutation
stand as before.

**Fail closed.** A stage that cannot verify what it did, a read that fails, an answer in another shape, a bound
reached, leaves the reset failed: `run` raises `ResetFailed` naming the stage, with the record of the stages so far
(each with its seconds), and runs no later stage. The caller must not start the next run then. Nothing is assumed:
a provider that does not answer is a failure, not an empty listing. Every message and every record key names the space
and each provider by its position, never a label, a name or a path.

The waits happen between runs, never in an agent's time (`consistency.guard`).
"""

import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from functools import partial
from math import isfinite
from typing import Any

from expcore_onedata import consistency, resetqos
from expcore_onedata.client import NotSent, PlatformError
from expcore_onedata.eviction import evict_ids, evict_tree
from expcore_onedata.occupancy import physical_sizes
from expcore_onedata.provision import Provisioner, ProvisionError
from expcore_onedata.transferlog import PAGE_LIMIT
from expcore_onedata.transferorder import STOP_MARGIN_S, newest_first
from expcore_onedata.transferrecord import Unreadable, page_of

STAGES = ("qos", "qos_settle", "transfers", "eviction", "occupancy", "quiet", "history")
ACTIVE = ("waiting", "ongoing")  # the listing's states of a job that has not ended
# Transfer ids one provider's waiting or ongoing listing, or its ended one within the history's window, may hold before
# it counts as unreadable, not cut. Older ended transfers are never read, so they never count (O32).
LISTED_MAX = 100_000
_FAILURES = (PlatformError, ProvisionError, consistency.WaitTimeout, Unreadable, KeyError)
NOT_IN_SPACE = "a path the plan names is not in the space"  # a KeyError's own text holds a path's part (O12)


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
        self,
        provisioner: Provisioner,
        *,
        providers: Sequence[str] | None = None,
        listed_max: int = LISTED_MAX,
        now: Callable[[], float] = time.time,
    ) -> None:
        """`providers` are the ones whose listings are read; by default every provider of the space. `now` is this
        host's epoch clock, the one a job's times and the history's `since` are compared on (the provisioner's clock
        is monotonic)."""
        support = provisioner.space.support.providers
        self._p, self._providers = provisioner, list(providers if providers is not None else support)
        if not self._providers or any(name not in support for name in self._providers):
            raise ValueError("a reset reads the listings of providers that support its space, at least one")
        if listed_max < 1:
            raise ValueError("a listing's bound is at least one id")
        self._listed_max, self._now = listed_max, now

    def _label(self, provider: str) -> str:
        return self._p.client.provider_label(provider)

    # -- the run -----------------------------------------------------------------
    def run(
        self,
        evict: Mapping[str, Iterable[str]] | None = None,
        allowed: Mapping[str, int] | None = None,
        *,
        since: float | None = None,
        qos_baseline: Collection[str] = (),
    ) -> dict[str, Any]:
        """Check the plan, verify the space, run the stages in order and return the record; raise ResetFailed at the
        first stage that fails.

        `evict` maps a provider to the space-relative paths of what must leave it: a directory leaves in one
        transfer, files together. `allowed` maps a provider to the physical bytes of the space it may still hold at the
        end (none by default). Every provider in either must support the space and must not be the data provider; a
        plan that breaks this is refused, and so is a space whose live name is not the configured one, before any
        stage runs. `since` is where the history's window starts, epoch seconds on `now`: the run's start, to count
        what the run and the reset added; by default the reset's own start. `qos_baseline` holds the ids of the QoS
        requirements the run started with (`qos_requirements()` at its start), which are kept; by default none is."""
        if since is not None and (isinstance(since, bool) or not isinstance(since, int | float) or not isfinite(since)):
            raise ValueError("the history's since is a finite number of epoch seconds")
        if isinstance(qos_baseline, str) or not all(isinstance(q, str) for q in qos_baseline):
            raise ValueError("the QoS baseline is a collection of requirement ids")
        history_since = float(self._now() if since is None else since)
        run: dict[str, Any] = {"affected": {}, "occupancy": {}}  # what one stage hands a later one
        plan = {provider: list(dict.fromkeys(paths)) for provider, paths in (evict or {}).items()}
        bounds = dict(allowed or {})
        for provider in [*plan, *bounds]:
            self._p.check_eviction(provider)
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in bounds.values()):
            raise ProvisionError(f"{self._p.where}an allowed holding is a whole number of bytes, zero or more")
        self._p.verify_space()
        steps: dict[str, Callable[[], dict[str, Any]]] = {
            "qos": lambda: self._qos(qos_baseline, run),
            "qos_settle": lambda: self._qos_settle(run),
            "transfers": self._transfers,
            "eviction": lambda: self._evict(plan),
            "occupancy": lambda: self._occupancy(bounds, run),
            "quiet": lambda: self._quiet(run),
            "history": lambda: self._history(history_since),
        }
        record: dict[str, Any] = {
            "space": self._p.space.position,
            "ok": False,
            "stages": {},
            "seconds": None,
            "reads_retried": 0,
        }
        began, retried = self._p.clock(), self._p.reads_retried
        for stage in STAGES:
            started = self._p.clock()
            try:
                facts = steps[stage]()
            except _FAILURES as error:
                detail = NOT_IN_SPACE if isinstance(error, KeyError) else str(error)
                record["stages"][stage] = {"ok": False, "seconds": round(self._p.clock() - started, 3)}
                record["stages"][stage]["error"] = detail
                record["seconds"] = round(self._p.clock() - began, 3)
                record["reads_retried"] = self._p.reads_retried - retried
                raise ResetFailed(
                    f"{self._p.where}the reset failed at its {stage} stage ({type(error).__name__}): {detail}; "
                    "the next run must not start",
                    stage,
                    record,
                ) from None
            record["stages"][stage] = {"ok": True, "seconds": round(self._p.clock() - started, 3), **facts}
        record.update(ok=True, seconds=round(self._p.clock() - began, 3), reads_retried=self._p.reads_retried - retried)
        return record

    # -- 1. QoS requirements, and 1a. their files settled (`resetqos.py`) -------------------
    def qos_requirements(self) -> frozenset[str]:
        """The ids of the QoS requirements on the space now, found as the reset finds them: the baseline a caller reads
        at the run's start and hands to `run`, so that the reset removes only the run's own."""
        return frozenset(resetqos.scan(self._p, self._providers).where)

    def _qos(self, baseline: Collection[str], run: dict[str, Any]) -> dict[str, Any]:
        """Remove every requirement not in the baseline and show them gone; keep the files they applied to."""
        facts, run["affected"] = resetqos.remove(self._p, self._providers, baseline)
        return facts

    def _qos_settle(self, run: dict[str, Any]) -> dict[str, Any]:
        """Wait for the affected files' block maps to stay the same, within the bound."""
        return resetqos.settle(self._p, run["affected"], self._providers)

    def _quiet(self, run: dict[str, Any]) -> dict[str, Any]:
        """Show that nothing reappeared on the affected files after the eviction, by their block maps after a wait, and
        that the occupancy did not grow."""
        sizes = partial(physical_sizes, self._p, self._providers, retry=self._p.retry)  # read after the wait
        return resetqos.quiet(self._p, run["affected"], self._providers, sizes, run["occupancy"])

    # -- 2. running transfers ---------------------------------------------------------
    def _page(self, provider: str, state: str, token: str | None) -> tuple[list[str], str | None]:
        """One page of a provider's listing in one state: its ids and the next page's token, None on the last."""
        params = {"state": state, "limit": PAGE_LIMIT, **({"page_token": token} if token else {})}
        path = f"/spaces/{self._p.space_id}/transfers"
        return page_of(self._p.client.get(path, provider=provider, params=params, retry=self._p.retry))

    def _listed(self, provider: str, state: str) -> list[str]:
        """Every id one provider lists in one state, page by page to the end; raise Unreadable past the bound. The
        waiting and ongoing listings are read so; the ended one only down to the history's window (`_history`)."""
        ids: list[str] = []
        token: str | None = None
        for _ in range(self._listed_max // PAGE_LIMIT + 1):
            page, token = self._page(provider, state, token)
            ids += page
            if len(ids) > self._listed_max:
                break
            if token is None:
                return list(dict.fromkeys(ids))
        raise Unreadable(f"{self._label(provider)}: the {state} listing is longer than {self._listed_max} ids")

    def _active(self) -> list[tuple[str, str]]:
        """Every `(provider, transfer id)` a provider lists as waiting or ongoing."""
        return [(p, tid) for p in self._providers for state in ACTIVE for tid in self._listed(p, state)]

    def _cancel(self, provider: str, tid: str) -> None:
        """Cancel one job on one provider through the gate; a 400 answered to the cancel is a job that has ended, which
        the wait then shows. A cancel the gate refused unsent (`NotSent`, which keeps the status of the gate's own read,
        a 400 among them) is never taken for that: it propagates, as does any other failure."""
        try:
            self._p.mutate("DELETE", f"/transfers/{tid}", provider=provider)
        except PlatformError as error:
            if isinstance(error, NotSent) or error.status != 400:
                raise

    def _wait_ended(self, pairs: Iterable[tuple[str, str]], what: str) -> None:
        """Wait, on each provider, for the jobs it listed to report an ended state there; raise at the bound."""
        settings, by_provider = self._p.settings, {}
        for provider, tid in pairs:
            by_provider.setdefault(provider, []).append(tid)
        for provider, tids in by_provider.items():
            consistency.require_ended(
                consistency.wait_transfers(
                    self._p.client,
                    provider,
                    tids,
                    timeout_s=settings.settle_timeout_s,
                    poll_s=settings.poll_s,
                    workers=settings.concurrency,
                    clock=self._p.clock,
                    sleep=self._p.sleep,
                    retry=self._p.retry,
                ),
                f"{self._label(provider)}: {what}",
            )

    def _transfers(self) -> dict[str, Any]:
        """Cancel every job that has not ended, on every provider that lists it, until `transfer_settle_polls`
        observations in a row show none, within twice that many."""
        settings = self._p.settings
        cancelled: set[tuple[str, str]] = set()
        clean = observations = 0
        while True:
            observations += 1
            active = self._active()
            fresh = [pair for pair in dict.fromkeys(active) if pair not in cancelled]
            for provider, tid in fresh:
                self._cancel(provider, tid)
                cancelled.add((provider, tid))
            self._wait_ended(fresh, "a cancelled transfer")
            clean = 0 if active else clean + 1
            if clean >= settings.transfer_settle_polls:
                return {"cancelled": len({tid for _, tid in cancelled}), "observations": observations}
            if observations >= 2 * settings.transfer_settle_polls:
                raise ProvisionError(
                    f"the listings had not shown no transfer waiting or ongoing {settings.transfer_settle_polls} times "
                    f"in a row after {observations} observations"
                )
            self._p.sleep(settings.transfer_settle_s)

    def _require_quiet(self) -> None:
        """Require the listings of every provider to show no transfer waiting or ongoing, observing again after the
        settle time up to `transfer_settle_polls` times (a job that has just ended may still be listed); raise when
        one is still listed at the bound. A block map or a physical size is read only after this passes."""
        settings = self._p.settings
        for observation in range(1, settings.transfer_settle_polls + 1):
            active = self._active()
            if not active:
                return
            if observation < settings.transfer_settle_polls:
                self._p.sleep(settings.transfer_settle_s)
        where = ", ".join(self._label(p) for p in dict.fromkeys(p for p, _ in active))
        raise ProvisionError(
            f"{len(active)} transfer(s) still waiting or ongoing on {where}; nothing is read until none"
        )

    # -- 3. eviction ------------------------------------------------------------------
    def _evict(self, plan: Mapping[str, list[str]]) -> dict[str, Any]:
        """Evict each provider's paths: a directory in one transfer, the files together, each verified on the block
        maps only once no transfer runs on any provider; then require that no transfer runs again."""
        try:
            resolved = self._p.resolve_many(path for paths in plan.values() for path in paths)
        except KeyError:
            raise ProvisionError(NOT_IN_SPACE) from None
        out: dict[str, Any] = {}
        for provider, paths in plan.items():
            entries = [resolved[path] for path in paths]
            results = [
                evict_tree(self._p, entry, provider, quiet=self._require_quiet)
                for entry in entries
                if entry.kind == "DIR"
            ]
            files = [entry.file_id for entry in entries if entry.kind != "DIR"]
            if files:
                results.append(evict_ids(self._p, files, provider, quiet=self._require_quiet))
            out[self._label(provider)] = {
                "trees": sum(1 for entry in entries if entry.kind == "DIR"),
                "files": sum(r["files"] for r in results),
                "transfers": sum(r["transfers"] for r in results),
                "polls": max((r["polls"] for r in results), default=0),
                "attempts": max((r["attempts"] for r in results), default=0),
            }
        self._require_quiet()
        return {"providers": out}

    # -- 4. occupancy -----------------------------------------------------------------
    def _occupancy(self, allowed: Mapping[str, int], run: dict[str, Any]) -> dict[str, Any]:
        """With no transfer running, read each provider's physical size of the space and refuse a provider other
        than the data provider that holds more than it is allowed; keep the sizes for the quiet re-check."""
        self._require_quiet()
        sizes = run["occupancy"] = physical_sizes(self._p, self._providers, retry=self._p.retry)
        over = [p for p, held in sizes.items() if p != self._p.settings.holder and held > allowed.get(p, 0)]
        if over:
            where = ", ".join(self._label(p) for p in over)
            raise ProvisionError(f"{where} hold(s) bytes of the space beyond what the reset allows")
        return {"physical_bytes": {self._label(p): held for p, held in sizes.items()}}

    # -- 5. ended-transfer history ------------------------------------------------------
    def _finish(self, provider: str, tid: str) -> float:
        """An ended job's finish time, from its record; one without a number there is unreadable."""
        record = self._p.client.get(f"/transfers/{tid}", provider=provider, retry=self._p.retry)
        value = record.get("finishTime") if isinstance(record, dict) else None
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise Unreadable("an ended transfer's record has no finishTime that is a number")
        return float(value)

    def _history(self, since: float) -> dict[str, Any]:
        """What the ended listing shows the next agent since `since`, per provider and together: the count of the jobs
        that ended since then, the newest finish, and whether older jobs are listed too. Each provider's listing is
        read newest first down to the window (`transferorder.newest_first`), never the whole history."""
        settings = self._p.settings
        cutoff = since - settings.clock_skew_allowance_s - STOP_MARGIN_S
        per: dict[str, Any] = {}
        ended: dict[str, float] = {}
        newest: list[float] = []
        earlier = False
        for provider in self._providers:
            walk = newest_first(
                lambda token, p=provider: self._page(p, "ended", token),
                lambda ids, p=provider: consistency.bounded_map(
                    lambda t: self._finish(p, t), list(ids), settings.concurrency
                ),
                lambda moment: moment,
                cutoff=cutoff,
                bound=self._listed_max,
                max_pages=self._listed_max // PAGE_LIMIT + 1,
                where=f"{self._label(provider)}: ",
                chunk=settings.concurrency,
            )
            per[self._label(provider)] = {
                "ended": len(walk.kept),
                "newest_finish": walk.newest,
                "earlier": walk.earlier,
            }
            for tid, moment in walk.kept.items():
                ended[tid] = max(ended.get(tid, moment), moment)
            newest += [] if walk.newest is None else [walk.newest]
            earlier = earlier or walk.earlier
        return {
            "removable": False,  # no call of the API removes an ended transfer (the module docstring has the evidence)
            "since": since,
            "ended": len(ended),
            "newest_finish": max(newest, default=None),
            "earlier": earlier,
            "providers": per,
        }


__all__ = ["STAGES", "ResetFailed", "SpaceReset"]
