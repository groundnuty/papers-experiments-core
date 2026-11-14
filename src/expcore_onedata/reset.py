# Provenance: new
"""A complete reset of one space between runs, each step verified and timed (O01, O07, O08, O09, O11).

A run's agent can leave more behind than files on a provider: a QoS requirement goes on replicating into the local
provider after the next run's start state was verified, a transfer still running changes a replica later, and the
listing of ended transfers shows the next agent what earlier runs replicated. `SpaceReset.run` takes them in order,
on the space its provisioner is bound to, and records the time of each stage, a resource-projection figure. Every
read and every check is made on each provider that supports the space, since each lists what it has seen:

1. **qos**: every QoS requirement in the space is found (there is no listing of a space's requirements; how they are
   found, a summary per leaf or one recursive listing, is `[placement] qos_scan`, `resetqos.py`), and each one not in
   the caller's baseline (`qos_baseline`, the run's start's, from `qos_requirements()`) is removed (`resetremove.py`):
   first its record is read on every provider until its status is `fulfilled` or `impossible` there, within
   `[placement] qos_fulfil_timeout_s` (120 s), or the reset fails and deletes nothing, since a deletion cancels the
   replication only on the provider that processes it, best-effort, and the replicating provider goes on until dbsync
   brings it the deletion; it is then deleted through the mutation gate (bound to this space, its record's `fileId`
   resolved by its parent links); then the deletion barrier: on every provider the summary of its entry must no longer
   list it and its record must answer that it is not there (`client.answered_absent`: a 404, or 25.0's 400 posix
   enoent; any other 400 fails closed) on consecutive reads, within `resetremove.BARRIER_BOUND_S` (60 s), so no file is
   settled or evicted before every provider has processed the deletion; and a second scan must find none but the
   baseline's (O33). `pending` does not prove that bytes are moving, and no REST answer shows bytes in flight.
   **qos_settle**: a QoS replication creates no transfer to wait for, and a cancel can still place the blocks it had in
   flight, so the block maps of the files a removed requirement applied to must stay the same on every provider for
   `resetqos.SETTLE_STABLE_S`, within `resetqos.SETTLE_BOUND_S`.
2. **transfers**: every waiting and ongoing transfer, on every provider that lists it, is cancelled there through the
   gate (bound to this space, its record's `spaceId` resolved); a cancel answered 400 is a job that has ended, never
   one the gate refused unsent. Each must then report an ended state on that provider, and the listings must show
   nothing waiting or ongoing on every provider in `transfer_settle_polls` observations in a row, `transfer_settle_s`
   apart (the listing's lag, O07), within twice that many observations. `cancelled` counts the distinct jobs the stage
   sent a cancel to, from its own actions, so a job it cancelled while still scheduled, which the history cannot see,
   is never invisible.
3. **eviction**: the files the caller names leave each provider named (`eviction.py`): a directory in one transfer, a
   set of files in parallel under `[placement] concurrency`, each scheduled again while its block map still shows bytes
   after the transfers ended (an eviction on 25.0 can complete having evicted nothing), up to `EVICTION_ATTEMPTS`.
   Once the eviction's transfers have ended, the listings must show no transfer running on any provider before the
   block maps are read, so a map is never read while a job still changes it (O01); and once every eviction is verified,
   the listings must show none running again. The block maps judge; the transfers' counters (`filesProcessed`,
   `filesEvicted`, `filesFailed` of the last attempt) are recorded per provider, and when files still hold bytes at
   the bound the failure names the provider, those counts and the likely cause: an eviction cancelled per file, e.g. a
   QoS requirement still covering the file there (O01). A gap with every map empty is no failure: a file the provider
   holds nothing of is processed, never evicted.
4. **occupancy**: the stage first waits `resetqos.occupancy_wait_s` from the eviction stage's end, the occupancy's
   longest lag for the deepest regular file of the space plus the margin (O45): an eviction's change reaches the
   space root's physical size as late as a placement's (the 14 Nov live slot saw a drop 20.03 s after an eviction at
   depth 3 was scheduled), and every eviction and every transfer, the reset's and an earlier run's, had ended by that
   instant, with none running. Then, with no transfer running on any provider (the listings are read again), the
   space root's distribution gives each provider's physical size, the bytes its storage holds of the space; never the
   logical figures, which lag. The reading is kept only when no transfer ran or ended since the wait began: the first
   page of each provider's ended listing, newest first by finish time, read at the eviction stage's end and again
   after the reading, holds no new job, and no transfer is waiting or ongoing. Otherwise a job no stage sent changed
   the space after the instant the wait counts from, and its change may not show: the stage lets it end and waits
   again from then, up to `OCCUPANCY_ROUNDS` (3) waits, and then fails. A listing shows a job up to
   `transfer_settle_s` late (O07), so the look that vouches for a reading is made that long after it: the stage's own
   when the quiet stage is skipped, else the quiet stage's, after its own reading and against the same first pages,
   which fails the reset on a job either look missed. The reset's own jobs, the evictions it sent and the jobs it
   cancelled, all seen to end before the wait, never count as new, however late they are listed. A provider other
   than the data provider that holds more than the caller allows (nothing by default) fails the reset, so a reset
   without an eviction plan still shows that only the owner holds data. Read before the evictions could show, the
   figure held evicted bytes: a reset that allows nothing failed after a good eviction, and the quiet stage, held to
   that figure, passed a growth up to their size. The record gives `waited_s` (every wait together), `depth`, `rounds`
   and `listings_after_s`.
   **quiet**: the block maps of the eviction plan's own files (the files the reset evicts from a provider other than
   the data provider, each on the providers it evicted it from) and of the files a removed requirement applied to are
   read at one instant T, and none may hold more than the eviction (nothing, where it verified them empty) or the
   settle (what it saw) left. The stage then waits `resetqos.quiet_wait_s` from T, the occupancy's longest lag for the
   deepest file a late replication could reach, from the source, plus a margin, and reads the occupancy, which no
   provider other than the data provider may have grown over the occupancy stage's. **What it proves, exactly: no
   replica of the plan's files, and no occupancy growth, had landed by T.** The occupancy read at T + the lag + the
   margin shows every placement that landed by T, whichever file it is on, so it alone catches a replication that lands
   before T on a file that is not in the plan, since no block map of the rest of the space is read. **It does not prove
   that nothing lands after T:** a replication still running at T can place its bytes later, may not show in the
   occupancy read, and the stage then passes. **A transfer that ran or ended since the occupancy stage's wait began
   fails the stage** (the same first pages and listings, looked at `transfer_settle_s` after its occupancy read,
   O45): an eviction in that window can hide a growth in the second reading, which then certifies nothing, and a job
   listed too late for the occupancy stage's own look made its figure wrong. The block maps decide for the plan's
   files; a zero occupancy is never taken alone as nothing having landed, since it lags. The stage runs unless the
   caller knows that no run since the last verified reset could have set a QoS requirement (`run(qos_rights=...)`,
   below), whether or not a requirement is left at the reset. **The residual:** a fulfilment whose cancel missed may
   still be replicating one file, with no requirement left and no transfer document to show it: of a requirement the
   agent added and removed during the run, or of one the reset removed after a reconciliation restarted between its
   fulfilled status and its deletion reaching the replicating provider. It is at most one file's size per missed
   cancel, and rare; it may land after T, and is then caught by the next start's reset. The record gives `ran`, `why`
   (`requirements`, `rights` or `default`, `resetheld.py`), `held_files` and `held_source` (`"eviction plan"`),
   `files`, `block_maps`, `listings_after_s` and the stage's seconds. It costs the wait, 20 s for files one directory
   below the root and 5 s more per further level (the depth of the deepest file of the space, as the QoS scan saw
   it), `transfer_settle_s` for the late look, and one block-map read per evicted (file, provider) pair of the plan
   and per (file, provider) of a removed requirement's files, once: bounded by the plan, never by the size of the
   space. The occupancy stage's wait comes on top, on every reset: the same lag and margin, for the deepest regular
   file of the space.
5. **history**: ended transfers cannot be removed. The 25.0 REST description offers, for a transfer, only `GET` and
   `DELETE /transfers/{tid}`, a cancel that answers 400 for a job that has ended, and `POST .../rerun`; the space's
   listing (`GET /spaces/{sid}/transfers`) only reads. So the next agent sees every ended transfer, the reset's own
   included, and the stage records what the listing shows it, per provider and together, since the window's start
   (`since`: the run's start when the caller gives it, else the reset's own start, on the reset's epoch clock `now`):
   the count of the jobs that ended with a finish time since then (`ended`), the count of those that may have ended on
   either side of `since` (`uncertain`), the newest finish time, read from the records, whether older ended jobs are
   listed too (`earlier`), and `removable` false, its limit stated. A job cancelled before it started carries no finish
   time (its `finishTime` is 0), so the listing puts it after every job that has one and the walk stops at it: it and
   what follows are neither read as part of the window nor counted against `LISTED_MAX`, however many such
   cancellations the history holds, and neither count holds it, nor `earlier`; it moved no bytes, and the reset's own
   such cancels are in the transfers stage's `cancelled`. A job with a finish time listed after one with none is an
   order the listing cannot have, and fails the stage. The history grows with every run, so the listing is read newest
   first down to the window less the clock-skew allowance and `transferorder.STOP_MARGIN_S`, its order checked as it
   goes, never whole (O32); `LISTED_MAX` counts the jobs read down to there. A finish time is stamped on the
   platform's global clock, which a provider's or Onezone's `Date` header does not give (`transferorder.py` has the
   source), so the band at the window's start is placed on that clock, read from Onezone's public current-time
   endpoint before and after the listings: each reading's offset from this host is the global time less the midpoint
   of this host's clock around the read, and half the round trip is its uncertainty. The band runs from `since` moved
   by the least offset the readings allow less the allowance to `since` moved by the greatest plus it
   (`transferorder.start_band`, recorded under `clock`, with the `Date` headers of Onezone's answer and of each
   provider's beside it, a diagnostic that places nothing), the band in which the probes flag a job near the window's
   edge. A job counts in `ended` when every provider's record puts its finish after the band; one in the band, or put
   on both sides, counts in `uncertain` alone; one before it is an older job, as is one read in the margin. `ended` is
   exact when `uncertain` is 0, and a lower bound otherwise. A reading of the global clock that fails or cannot be
   read, or one that puts the global clock more than the margin behind this host's, fails the stage.

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

from expcore_onedata import consistency, resetheld, resetqos, resetremove
from expcore_onedata.client import ZONE_TIME_PATH, NotSent, PlatformError
from expcore_onedata.eviction import evict_ids, evict_tree, sum_counts
from expcore_onedata.occupancy import physical_sizes
from expcore_onedata.provision import Provisioner, ProvisionError
from expcore_onedata.transferlog import PAGE_LIMIT
from expcore_onedata.transferorder import (
    EARLIER,
    INSIDE,
    STOP_MARGIN_S,
    UNCERTAIN,
    Walk,
    newest_first,
    start_band,
)
from expcore_onedata.transferrecord import Unreadable, page_of
from expcore_onedata.transferwindow import ZoneClock, offset_of, span, zone_clock_of

STAGES = ("qos", "qos_settle", "transfers", "eviction", "occupancy", "quiet", "history")
ACTIVE = ("waiting", "ongoing")  # the listing's states of a job that has not ended
# Transfer ids one provider's waiting or ongoing listing, or its ended one within the history's window, may hold before
# it counts as unreadable, not cut. Older ended transfers are never read, so they never count (O32).
LISTED_MAX = 100_000
# Waits the occupancy stage takes in all, beginning again when a transfer ended or ran during the last one, before it
# fails: a reading after such a wait may not show that transfer's change (O45).
OCCUPANCY_ROUNDS = 3
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
        qos_rights: bool | None = None,
    ) -> dict[str, Any]:
        """Check the plan, verify the space, run the stages in order and return the record; raise ResetFailed at the
        first stage that fails.

        `evict` maps a provider to the space-relative paths of what must leave it: a directory leaves in one
        transfer, files together. `allowed` maps a provider to the physical bytes of the space it may still hold at the
        end (none by default). Every provider in either must support the space and must not be the data provider; a
        plan that breaks this is refused, and so is a space whose live name is not the configured one, before any
        stage runs. `since` is where the history's window starts, epoch seconds on `now`: the run's start, to count
        what the run and the reset added; by default the reset's own start. `qos_baseline` holds the ids of the QoS
        requirements the run started with (`qos_requirements()` at its start), which are kept; by default none is.
        `qos_rights` says whether **any run since the last verified reset** could have set a QoS requirement. A study's
        reset runs at the start of each run and clears what earlier runs left, so this is not the rights of the run
        about to start: a read-only run that follows a writing one would skip the quiet check exactly when it is
        needed. `True` or `None` (the fail-safe default) runs the check, whether or not a requirement is left at the
        reset, since a fulfilment whose cancel missed may still be replicating one file; pass
        `False` only when you know that no run since the last verified reset had the right to manage QoS, and then
        only a reset that removed no requirement skips it. `Condition.manages_qos` describes one condition: combine it
        over every run since the last verified reset (O33)."""
        if since is not None and (isinstance(since, bool) or not isinstance(since, int | float) or not isfinite(since)):
            raise ValueError("the history's since is a finite number of epoch seconds")
        if isinstance(qos_baseline, str) or not all(isinstance(q, str) for q in qos_baseline):
            raise ValueError("the QoS baseline is a collection of requirement ids")
        if qos_rights is not None and not isinstance(qos_rights, bool):
            raise ValueError("qos_rights is True, False, or None for a caller that does not say")
        history_since = float(self._now() if since is None else since)
        # what one stage hands a later one: the files a removed requirement applied to, why the quiet check runs (None:
        # it is skipped), the depth of the deepest file of the space, the (file, provider) pairs the eviction emptied
        # (collected as it lists them), the instant the eviction stage ended and the ended listings' first pages then
        # (later, at the occupancy stage's accepted reading), the maps the settle ended on, the occupancy
        run: dict[str, Any] = {
            "affected": {},
            "why": resetheld.quiet_reason(0, qos_rights),
            "deepest": 0,
            "evicted": set(),
            "evicted_at": None,
            "ended_head": {},
            "own": set(),  # the ids of the jobs the reset sent or cancelled, all ended before the occupancy's wait
            "settled": {},
            "occupancy": {},
            "qos_rights": qos_rights,
        }
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
            "transfers": lambda: self._transfers(run),
            "eviction": lambda: self._evict(plan, run),
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
                seconds = round(self._p.clock() - started, 3)
                record["stages"][stage] = {"ok": False, "seconds": seconds, **getattr(error, "facts", {})}
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
        """Remove every requirement not in the baseline, each fulfilled first and its deletion processed everywhere
        after (`resetremove.py`), and show them gone; keep the files they applied to."""
        facts, run["affected"], run["deepest"] = resetremove.remove(self._p, self._providers, baseline)
        run["why"] = resetheld.quiet_reason(facts["removed"], run["qos_rights"])
        return facts

    def _qos_settle(self, run: dict[str, Any]) -> dict[str, Any]:
        """Wait for the affected files' block maps to stay the same, within the bound; keep what they ended on."""
        facts, run["settled"] = resetqos.settle(self._p, run["affected"], self._providers)
        return facts

    def _quiet(self, run: dict[str, Any]) -> dict[str, Any]:
        """Read, at one instant, the block maps of the eviction plan's files and of the files a removed requirement
        applied to, against what the eviction and the settle left; wait; read the occupancy and set it against the
        occupancy stage's. It proves that no replica of the plan's files, and no occupancy growth, had landed by that
        instant, and not that nothing lands after it. A transfer that ran or ended since the occupancy stage's wait
        began fails it (O45): an eviction there can hide a growth in the second reading, and a job the occupancy
        stage's own look at the listings missed, listed late, made its figure wrong. The listings are looked at
        `transfer_settle_s` after the reading, by when a job that ended before it is listed (O07). Skipped, and
        recorded so, for a caller that knows no run could have set a requirement, when none was removed."""
        why, evicted = run["why"], run["evicted"]
        if why is None:
            return {"ran": False, "why": None, "qos_rights": run["qos_rights"], "files": 0, "waited_s": 0.0}
        sizes = partial(physical_sizes, self._p, self._providers, retry=self._p.retry)  # read after the wait
        depth = resetheld.quiet_depth(run["affected"], run["deepest"], run["qos_rights"])
        expected = resetheld.expected_maps(run["settled"], evicted)
        facts = resetqos.quiet(self._p, expected, sizes, run["occupancy"], depth=depth)
        settle = self._p.settings.transfer_settle_s
        self._p.sleep(settle)  # a job that ended by the reading is listed by now (O07)
        moved = self._moved(run["ended_head"], self._ended_heads(), run["own"])
        if moved:
            raise ProvisionError(
                f"a transfer ran or ended on {', '.join(moved)} after the occupancy stage's wait began: an eviction "
                "then can hide a growth in the quiet stage's reading, so it certifies nothing"
            )
        return {
            "ran": True,
            "why": why,
            "qos_rights": run["qos_rights"],
            "held_files": len({file_id for file_id, _ in evicted}),
            "held_source": resetheld.SOURCE,
            **facts,
            "listings_after_s": settle,
        }

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

    def _ended_heads(self) -> dict[str, frozenset[str]]:
        """The ids on the first page of each provider's ended listing. The listing is newest first by finish time, so
        a job that ends later is listed on that page once the listing shows it, and an id there that an earlier reading
        lacked is a job that ended since (O45). A job that ended just before the earlier reading but was not listed yet
        counts so too, a wait more. A job is seen only once listed: a reading that must hold every job ended by an
        instant is made at least `transfer_settle_s`, the listing's lag (O07), after it."""
        return {p: frozenset(self._page(p, "ended", None)[0]) for p in self._providers}

    def _moved(
        self, before: Mapping[str, frozenset[str]], now: Mapping[str, frozenset[str]], own: Collection[str]
    ) -> list[str]:
        """The labels of the providers whose ended listing's first page holds a job `before` did not (it ended
        between the two readings), or that list a transfer waiting or ongoing now; none when no transfer has run or
        ended since `before`. A job of the reset's own (`own`, which it sent or cancelled and saw end before the
        occupancy's wait began) is never new, though its listing may come late (O07)."""
        moved = [p for p in self._providers if now[p] - before.get(p, frozenset()) - frozenset(own)]
        moved += [p for p, _ in self._active()]
        return [self._label(p) for p in dict.fromkeys(moved)]

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

    def _transfers(self, run: dict[str, Any]) -> dict[str, Any]:
        """Cancel every job that has not ended, on every provider that lists it, until `transfer_settle_polls`
        observations in a row show none, within twice that many. `cancelled` counts the distinct jobs the reset sent a
        cancel to (one answered 400, a job that had just ended, included): the history's `ended` cannot hold one it
        cancelled while still scheduled, which is given no finish time. Their ids join the reset's own
        (`run["own"]`, O45)."""
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
                run["own"].update(tid for _, tid in cancelled)
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
    def _evict(self, plan: Mapping[str, list[str]], run: dict[str, Any]) -> dict[str, Any]:
        """Evict each provider's paths: a directory in one transfer, the files together, each verified on the block
        maps only once no transfer runs on any provider; then require that no transfer runs again. The files it
        evicts, the ones the plan names and those it lists under a directory, each with the provider it evicted it
        from, are kept for the quiet check (`run["evicted"]`), which holds their block maps there to the empty map the
        eviction verified, and never lists the tree again. The instant it ends, with no transfer running, is kept too
        (`run["evicted_at"]`, an empty plan included), with the first page of each provider's ended listing then
        (`run["ended_head"]`): every eviction and every transfer had ended by that instant, the reset's own and an
        earlier run's, and the occupancy stage waits from it and checks against those pages that none ended since
        (O45). The ids of the jobs it sends join the reset's own (`run["own"]`), which that check never takes for new
        when a listing shows them late."""
        try:
            resolved = self._p.resolve_many(path for paths in plan.values() for path in paths)
        except KeyError:
            raise ProvisionError(NOT_IN_SPACE) from None
        out: dict[str, Any] = {}
        evicted: set[tuple[str, str]] = run["evicted"]  # mutated on purpose: what this stage hands the quiet stage
        own: set[str] = run["own"]  # mutated on purpose: the jobs it sends, which the occupancy stage knows as its own
        for provider, paths in plan.items():
            entries = [resolved[path] for path in paths]
            results = [
                evict_tree(
                    self._p,
                    entry,
                    provider,
                    quiet=self._require_quiet,
                    listed=lambda ids, q=provider: evicted.update((file_id, q) for file_id in ids),
                    scheduled=own.update,
                )
                for entry in entries
                if entry.kind == "DIR"
            ]
            files = [entry.file_id for entry in entries if entry.kind != "DIR"]
            evicted.update((file_id, provider) for file_id in files)
            if files:
                results.append(evict_ids(self._p, files, provider, quiet=self._require_quiet, scheduled=own.update))
            out[self._label(provider)] = {
                "trees": sum(1 for entry in entries if entry.kind == "DIR"),
                "files": sum(r["files"] for r in results),
                "transfers": sum(r["transfers"] for r in results),
                "polls": max((r["polls"] for r in results), default=0),
                "attempts": max((r["attempts"] for r in results), default=0),
                **sum_counts(results),
            }
        self._require_quiet()
        run["ended_head"] = self._ended_heads()
        run["evicted_at"] = self._p.clock()
        return {"providers": out}

    # -- 4. occupancy -----------------------------------------------------------------
    def _occupancy(self, allowed: Mapping[str, int], run: dict[str, Any]) -> dict[str, Any]:
        """Wait until every eviction's and every ended transfer's change can show in the space root's physical size:
        `resetqos.occupancy_wait_s` for the deepest regular file of the space, from the eviction stage's end (O45).
        Then, with no transfer running, read each provider's physical size of the space, and keep the reading only when
        no transfer ran or ended since the wait began (the ended listings' first pages, and the waiting and ongoing
        listings, read after it): a job no stage sent may have, after the instant the wait counts from. Otherwise wait
        again from when none runs, up to `OCCUPANCY_ROUNDS` waits in all. A listing shows a job up to
        `transfer_settle_s` late (O07): when the quiet stage is skipped, the listings are looked at that long after the
        reading, so the reading is kept only when it holds every job's change; when it runs, they are looked at at once,
        and the quiet stage's own look, that long after its reading, against the same first pages, covers the rest and
        fails the reset. Refuse a provider other than the data provider that holds more than it is allowed; keep the
        sizes for the quiet re-check, which would otherwise hold the occupancy to a figure still showing evicted
        bytes."""
        depth = max(run["deepest"], 1)
        settle = self._p.settings.transfer_settle_s if run["why"] is None else 0.0
        anchor, before, waited, rounds = run["evicted_at"], run["ended_head"], 0.0, 0
        while True:
            rounds += 1
            wait = max(0.0, anchor + resetqos.occupancy_wait_s(depth) - self._p.clock())
            if wait > 0:
                self._p.sleep(wait)
                waited += wait
            self._require_quiet()
            sizes = physical_sizes(self._p, self._providers, retry=self._p.retry)
            if settle > 0:
                self._p.sleep(settle)  # a job that ended by the reading is listed by now (O07)
            if not self._moved(before, self._ended_heads(), run["own"]):
                break
            if rounds == OCCUPANCY_ROUNDS:
                raise ProvisionError(
                    f"transfers kept ending or running during the occupancy stage's wait, {OCCUPANCY_ROUNDS} waits in "
                    "all: no reading of the occupancy is shown to hold their changes"
                )
            self._require_quiet()  # a job ran or ended since the wait began: let it end, then wait from then
            before, anchor = self._ended_heads(), self._p.clock()
        run["occupancy"], run["ended_head"] = sizes, before  # the quiet stage looks for jobs since this wait began
        over = [p for p, held in sizes.items() if p != self._p.settings.holder and held > allowed.get(p, 0)]
        if over:
            where = ", ".join(self._label(p) for p in over)
            raise ProvisionError(f"{where} hold(s) bytes of the space beyond what the reset allows")
        return {
            "physical_bytes": {self._label(p): held for p, held in sizes.items()},
            "waited_s": round(waited, 3),
            "depth": depth,
            "rounds": rounds,
            "listings_after_s": settle,
        }

    # -- 5. ended-transfer history ------------------------------------------------------
    def _finish(self, provider: str, tid: str) -> float:
        """An ended job's finish time, from its record; 0 for a job cancelled before it started, which was given none
        (`transferorder.py`). One without a number there is unreadable."""
        record = self._p.client.get(f"/transfers/{tid}", provider=provider, retry=self._p.retry)
        value = record.get("finishTime") if isinstance(record, dict) else None
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise Unreadable("an ended transfer's record has no finishTime that is a number")
        return float(value)

    def _zone_clock(self) -> ZoneClock | None:
        """Onezone's reading of the global clock, the one finish times are stamped on, from its public current-time
        endpoint, sent with no credential: its offset from this host and half the round trip, which spans any retry;
        None when the answer cannot be read. A read that fails raises, and fails the stage."""
        sent = self._now()
        answer, date = self._p.client.get_dated(ZONE_TIME_PATH, retry=self._p.retry, anonymous=True)
        return zone_clock_of(answer, date, sent, self._now())

    def _dated_page(self, provider: str, token: str | None) -> tuple[list[str], str | None, float | None]:
        """One page of a provider's ended listing (its ids and the next page's token, None on the last) with the
        provider's system-clock offset from this host that the answer's Date header gives, None when it cannot be read:
        a diagnostic, since a finish time is on the global clock."""
        params = {"state": "ended", "limit": PAGE_LIMIT, **({"page_token": token} if token else {})}
        path = f"/spaces/{self._p.space_id}/transfers"
        answer, date = self._p.client.get_dated(path, provider=provider, params=params, retry=self._p.retry)
        ids, following = page_of(answer)
        return ids, following, offset_of(date, self._now())

    def _walk_ended(self, provider: str, cutoff: float) -> tuple[Walk, list[float | None]]:
        """Walk one provider's ended listing newest first down to `cutoff` (`transferorder.newest_first`), and return
        the walk with the clock offset each of its listing answers gave."""
        settings = self._p.settings
        offsets: list[float | None] = []

        def page(token: str | None) -> tuple[list[str], str | None]:
            ids, following, offset = self._dated_page(provider, token)
            offsets.append(offset)  # the walk takes ids and a token alone: the offsets are kept beside it
            return ids, following

        walk = newest_first(
            page,
            lambda ids: consistency.bounded_map(lambda t: self._finish(provider, t), list(ids), settings.concurrency),
            lambda moment: moment,
            cutoff=cutoff,
            bound=self._listed_max,
            max_pages=self._listed_max // PAGE_LIMIT + 1,
            where=f"{self._label(provider)}: ",
            chunk=settings.concurrency,
        )
        return walk, offsets

    def _history(self, since: float) -> dict[str, Any]:
        """What the ended listing shows the next agent since `since`, per provider and together: the count of the jobs
        that surely ended with a finish time since then, the count of those that may have ended on either side of it,
        the newest finish, and whether older jobs are listed too. A job cancelled before it started has no finish time:
        the walk stops at it, and it is in neither count (`_transfers` counts the reset's own cancels). Each provider's
        listing is read newest first down to `since` less the allowance and the margin, never the whole history;
        Onezone's readings of the global clock, made before and after the listings, place the band at the window's
        start (`transferorder.start_band`), and a clock that cannot be read, or that puts the band below where the
        reading stops, fails the stage."""
        allowance = self._p.settings.clock_skew_allowance_s
        cutoff = since - allowance - STOP_MARGIN_S
        walks, offsets = {}, {}
        zone = [self._zone_clock()]
        for provider in self._providers:
            walks[self._label(provider)], offsets[self._label(provider)] = self._walk_ended(provider, cutoff)
        zone.append(self._zone_clock())
        band, clock = start_band(since, zone, allowance)
        clock["provider_offsets_s"] = {label: span(samples) for label, samples in offsets.items()}
        per: dict[str, Any] = {}
        places: dict[str, set[str]] = {}  # job id -> where each provider's record puts it
        for label, walk in walks.items():
            # The margin decides where the walk stops, never what counts: a job it kept that finished before the band
            # ended before `since` and shows that older jobs are listed; one in the band is neither counted nor older.
            # A job with no finish time, cancelled before it started, stops the walk and is never kept: `ended` counts
            # finish times.
            mine = {tid: band.place(moment) for tid, moment in walk.kept.items()}
            for tid, place in mine.items():
                places.setdefault(tid, set()).add(place)
            per[label] = {
                "ended": sum(place == INSIDE for place in mine.values()),
                "uncertain": sum(place == UNCERTAIN for place in mine.values()),
                "newest_finish": walk.newest,
                "earlier": walk.earlier or EARLIER in mine.values(),
            }
        return {
            "removable": False,  # no call of the API removes an ended transfer (the module docstring has the evidence)
            "since": since,
            # A job counts when every provider that read it puts it after the band; one any puts in the band, or that
            # providers put on both sides, is uncertain, never folded into `ended`.
            "ended": sum(found == {INSIDE} for found in places.values()),
            "uncertain": sum(found not in ({INSIDE}, {EARLIER}) for found in places.values()),
            "newest_finish": max((w.newest for w in walks.values() if w.newest is not None), default=None),
            "earlier": any(row["earlier"] for row in per.values()),
            "clock": clock,
            "providers": per,
        }


__all__ = ["OCCUPANCY_ROUNDS", "STAGES", "ResetFailed", "SpaceReset"]
