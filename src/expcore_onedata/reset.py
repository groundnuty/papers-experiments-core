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
   apart (the listing's lag, O07), within twice that many observations; **a space whose every observation showed
   nothing, so that nothing was cancelled, settles on two**, one `transfer_settle_s` apart, by when a job scheduled
   before the first is listed (O07); a job scheduled after the first is the caller's to rule out, as for every stage,
   and the later stages' looks at the listings still catch one scheduled after the settle (`resetspaces.py` has the
   argument, and the record says `fast`). `cancelled` counts the
   distinct jobs the stage sent a cancel to, from its own actions, so a job it cancelled while still scheduled, which
   the history cannot see, is never invisible.
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
   longest lag for the deepest regular file of the space plus the margin (O46): an eviction's change reaches the
   space root's physical size as late as a placement's (the 14 Nov live slot saw a drop 20.03 s after an eviction at
   depth 3 was scheduled), and every eviction and every transfer, the reset's and an earlier run's, had ended by that
   instant, with none running. Then, with no transfer running on any provider (the listings are read again), the
   occupancy is read (`occupancy.py`): every regular file's block map at each provider that can hold the space's bytes,
   read there and summed, which is the verdict, and the space root's distribution beside it, its per-provider
   physical size, a directory-statistics counter that is wrong in both directions and never the sole verdict (it
   stayed 1 MiB high for good after an eviction, and read 0 for 9.534 s while a placement was held); never the
   logical figures, which lag. The reading is kept only when no transfer ran or ended since the wait began: the first
   page of each provider's ended listing, newest first by finish time, read at the eviction stage's end and again
   after the reading, holds no new job, and no transfer is waiting or ongoing. Otherwise a job no stage sent changed
   the space after the instant the wait counts from, and its change may not show: the stage lets it end and waits
   again from then, up to `OCCUPANCY_ROUNDS` (3) waits, and then fails. A listing shows a job up to
   `transfer_settle_s` late (O07), so the look that vouches for a reading is made that long after it: the stage's own
   when the quiet stage is skipped, else the quiet stage's, after its own reading and against the same first pages,
   which fails the reset on a job either look missed. The reset's own jobs, the evictions it sent and the jobs it
   cancelled, all seen to end before the wait, never count as new, however late they are listed. A provider other
   than the data provider whose files hold more than the caller allows (nothing by default) fails the reset, so a
   reset without an eviction plan still shows that only the owner holds data; the message states the bytes held, the
   figure allowed and the counter, per provider, and the files by position. The counter skips a provider's files only
   for one the caller declares never holds the space's bytes (`never_holds`), while it reads 0, and each disagreement
   with the files is an event. Read before the evictions could show, the figure held evicted bytes: a reset that
   allows nothing failed after a good eviction, and the quiet stage, held to that figure, passed a growth up to their
   size. The record gives `physical_bytes` (the counter), `files_bytes`, `files_read`, `events`, `waited_s` (every
   wait together), `depth`, `rounds` and `listings_after_s`.
   **quiet**: the block maps of the eviction plan's own files (the files the reset evicts from a provider other than
   the data provider, each on the providers it evicted it from) and of the files a removed requirement applied to are
   read at one instant T, and none may hold more than the eviction (nothing, where it verified them empty) or the
   settle (what it saw) left. The stage then waits `resetqos.quiet_wait_s` from T, the occupancy's longest lag for the
   deepest file a late replication could reach, from the source, plus a margin, and reads the occupancy, which no
   provider other than the data provider may have grown over the occupancy stage's, by its files' block maps summed,
   never by the counter (O46). **What it proves, exactly: no
   replica of the plan's files, and no occupancy growth, had landed by T.** The occupancy read at T + the lag + the
   margin shows every placement that landed by T, whichever file it is on, so it alone catches a replication that lands
   before T on a file that is not in the plan, since no block map of the rest of the space is read. **It does not prove
   that nothing lands after T:** a replication still running at T can place its bytes later, may not show in the
   occupancy read, and the stage then passes. **A transfer that ran or ended since the occupancy stage's wait began
   fails the stage** (the same first pages and listings, looked at `transfer_settle_s` after its occupancy read,
   O46): an eviction in that window can hide a growth in the second reading, which then certifies nothing, and a job
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
   it), `transfer_settle_s` for the late look, one block-map read at T per evicted (file, provider) pair of the plan
   and per (file, provider) of a removed requirement's files, once, bounded by the plan, and the occupancy's reading,
   one block-map read per regular file at each provider that can hold the space's bytes, which follows the size of
   the space (O46). The occupancy stage's wait and reading come on top, on every reset: the same lag and margin, for
   the deepest regular file of the space.
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

**Reads that fail are tried again, boundedly and in the log** (O29, O46, O51, `readretry.py`). A read inside a poll that
counts a failed read as one that did not match (the requirements' fulfilment and removal checks, the eviction's block
maps) is one attempt: the poll is the retry. Every other read the reset makes goes under a policy that also tries a
connection failure again, four attempts 2, 5 and 15 s apart, never an answer (`Provisioner.reading_retry`): the reads of
each reading (the scans, `summaries` mode's directory listings included, the settle, the occupancy with its counter, the
quiet stage's maps) under one made for that reading, at most `[placement] provider_concurrency` in flight at one
provider, tallied as the stage's `read_retry`; each stage's own transfer reads (the listings, the records, the waits for
its jobs, the eviction stage's included, and Onezone's clock) under one per stage (`stage_reads`), tallied as the
stage's `transfer_read_retry`; and the mutation gate's reads before a cancel, an eviction or a requirement's deletion,
the space's name (`verify_space`; the driver's tallied as `verify_read_retry`), the eviction stage's path listings and
visibility wait (`Provisioner.await_visible`) and the space root's first read, each under the policy of the stage it is
made in. The record says how many reads were retried (`reads_retried`). No read is left on timeouts only. Nothing else
is tried again: an error answer stands as before, and **a mutation is sent once**: a cancel, an eviction or a deletion
whose answer was lost after it went out (`client.sent_unanswered`) may have been applied, so it is never sent again; the
job's record, the block maps or the deletion barrier decide, failing closed at their bounds, and the stage records
`unanswered` (O09).

**Fail closed.** A stage that cannot verify what it did, a read that fails, an answer in another shape, a bound
reached, leaves the reset failed: `run` raises `ResetFailed` naming the stage, with the record of the stages so far
(each with its seconds), and runs no later stage. The caller must not start the next run then. Nothing is assumed:
a provider that does not answer is a failure, not an empty listing. Every message and every record key names the space
and each provider by its position, never a label, a name or a path.

The waits happen between runs, never in an agent's time (`consistency.guard`). **Several spaces** reset as one
through `resetspaces.SpacesReset`: the same stages for each, the waits they share taken once, and the spaces' own work
at once; `run` is that driver with this space alone.
"""

import logging
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from functools import partial
from typing import Any

from expcore_onedata import consistency, occupancy, resetgrant, resetheld, resetqos, resetremove, resetshortcut
from expcore_onedata.client import ZONE_TIME_PATH, NotSent, PlatformError, sent_unanswered
from expcore_onedata.eviction import evict_ids, evict_tree, sum_counts
from expcore_onedata.provision import Provisioner, ProvisionError
from expcore_onedata.readretry import ReadRetry
from expcore_onedata.resetgrant import ResetGrant
from expcore_onedata.resetplan import Holdings, plan_scan
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
# fails: a reading after such a wait may not show that transfer's change (O46).
OCCUPANCY_ROUNDS = 3
FAILURES = (PlatformError, ProvisionError, consistency.WaitTimeout, Unreadable, KeyError)  # a stage's, recorded
NOT_IN_SPACE = "a path the plan names is not in the space"  # a KeyError's own text holds a path's part (O12)
log = logging.getLogger(__name__)


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
        # The policy of the reset's own transfer reads in the stage under way (`stage_reads`): listings, records, the
        # waits for its jobs and Onezone's clock. One space is worked on by one thread at a time (`resetspaces.py`).
        self._reads = provisioner.reading_retry()

    def stage_reads(self) -> ReadRetry:
        """Begin a stage's own transfer reads (its listings, records, waits and Onezone's clock): a new reading policy
        (`Provisioner.reading_retry`: a connection failure tried again, an answer never, a read that stays failed
        failing the stage), whose tally the stage's record carries (`transfer_read_retry`, `resetspaces.py`)."""
        self._reads = self._p.reading_retry()
        return self._reads

    @property
    def provisioner(self) -> Provisioner:
        """The provisioner of the space this reset is bound to."""
        return self._p

    @property
    def providers(self) -> tuple[str, ...]:
        """The providers whose listings the reset reads."""
        return tuple(self._providers)

    @property
    def now(self) -> Callable[[], float]:
        """This host's epoch clock, the one the history's `since` is on."""
        return self._now

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
        never_holds: Collection[str] = (),
        planned: Holdings | None = None,
    ) -> dict[str, Any]:
        """Check the plan, verify the space, run the stages in order and return the record; raise ResetFailed at the
        first stage that fails. It is `resetspaces.SpacesReset` with this space alone, so its stages, and the
        transfer settle's fast path, are that driver's.

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
        over every run since the last verified reset (O33). `never_holds` names the providers the caller declares can
        never hold the space's bytes: the occupancy reads every regular file's block map at every other provider but
        the data provider, whatever the space root's counter says, and skips one of these only while its counter reads
        0 (`occupancy.py`, O46); none by default. `planned`, a planner's reading of this space (`plan_scan`), makes
        its scan the qos stage's first, so the space is listed once (`resetplan.py`); by default the stage scans. With
        `[placement] reset_shortcut` on and the reading made inside a live exclusive section (`exclusive`), the
        occupancy and quiet stages re-read only what may have changed since it (`resetshortcut.py`); the record's
        `occupancy_reads` says which reads were made, and why."""
        from expcore_onedata.resetspaces import ResetPlan, SpacesReset  # the driver builds on this module

        plan = ResetPlan(
            evict,
            allowed,
            since=since,
            qos_baseline=qos_baseline,
            qos_rights=qos_rights,
            never_holds=never_holds,
            planned=planned,
        )
        return SpacesReset([self], concurrency=1).run_one(plan)

    def plan_scan(self, *, never_holds: Collection[str] = ()) -> Holdings:
        """A planner's reading of the space: one recursive listing, the QoS scan the reset makes, and each regular
        file's block map at every provider that can hold the space's bytes (`resetplan.plan_scan`, `Holdings`). Made
        inside `exclusive`, the reading carries the section's grant, which the shortcut needs."""
        return plan_scan(self, never_holds=never_holds)

    def exclusive(self) -> AbstractContextManager[ResetGrant]:
        """The exclusive hold of the reset lock (`resetgrant.exclusive`), which `run` takes itself unless it runs inside
        a live one: a shortcut reset's planner's reading and the reset that uses it go inside, under the run's shared
        admission, which keeps its share of `federation.lock` throughout. With `[placement] reset_shortcut` off it
        holds the lock all the same, and gives no shortcut (`off`)."""
        return resetgrant.exclusive(self._p.config, self._p.settings)

    # -- 1. QoS requirements, and 1a. their files settled (`resetqos.py`) -------------------
    def qos_requirements(self) -> frozenset[str]:
        """The ids of the QoS requirements on the space now, found as the reset finds them: the baseline a caller reads
        at the run's start and hands to `run`, so that the reset removes only the run's own."""
        return frozenset(resetqos.scan(self._p, self._providers).where)

    def _qos(self, baseline: Collection[str], run: dict[str, Any]) -> dict[str, Any]:
        """Remove every requirement not in the baseline, each fulfilled first and its deletion processed everywhere
        after (`resetremove.py`), and show them gone; keep the files they applied to."""
        given = run["scan"]  # a planner's scan stands for the first: how old it is as the stage begins is recorded
        age = None if given is None else round(self._p.clock() - run["scan_at"], 3)
        removed = resetremove.remove(self._p, self._providers, baseline, given)
        facts, run["affected"], run["deepest"], run["files"] = removed
        run["why"] = resetheld.quiet_reason(facts["removed"], run["qos_rights"])
        return facts if age is None else {**facts, "scan_age_s": age}

    def _qos_settle(self, run: dict[str, Any]) -> dict[str, Any]:
        """Wait for the affected files' block maps to stay the same, within the bound; keep what they ended on."""
        facts, run["settled"] = resetqos.settle(self._p, run["affected"], self._providers)
        return facts

    # The quiet stage, in the steps `resetspaces` takes for every space, with one wait between each (`_quiet_*`).
    def _quiet_skipped(self, run: dict[str, Any]) -> dict[str, Any] | None:
        """The stage's record when it is skipped, for a caller that knows no run could have set a requirement, when
        none was removed; None when it runs."""
        if run["why"] is not None:
            return None
        return {"ran": False, "why": None, "qos_rights": run["qos_rights"], "files": 0, "waited_s": 0.0}

    def _quiet_maps(self, run: dict[str, Any]) -> float:
        """Read, at one instant T, the block maps of the eviction plan's files and of the files a removed requirement
        applied to, against what the eviction and the settle left; return the instant the occupancy may be read at:
        T and `resetqos.quiet_wait_s` for the deepest file a late replication could reach. It proves that no replica of
        the plan's files, and no occupancy growth, had landed by T, and not that nothing lands after it."""
        depth = resetheld.quiet_depth(run["affected"], run["deepest"], run["qos_rights"])
        facts = resetqos.quiet_maps(self._p, resetheld.expected_maps(run["settled"], run["evicted"]))
        wait = resetqos.quiet_wait_s(depth)
        run["quiet"] = {**facts, "waited_s": wait}
        return self._p.clock() + wait

    def _quiet_sizes(self, run: dict[str, Any]) -> None:
        """After the wait, read the occupancy (`_holdings`) and set it against the occupancy stage's, by the files'
        block maps summed, never by the counter (O46)."""
        run["quiet"].update(resetqos.quiet_grown(self._p, self._holdings(run), run["occupancy"]))

    def _quiet_look(self, run: dict[str, Any], after_s: float) -> dict[str, Any]:
        """Look at the listings `after_s` (at least `transfer_settle_s`) after the reading, by when a job that ended
        before it is listed (O07): a transfer that ran or ended since the occupancy stage's wait began fails the stage
        (O46), since an eviction there can hide a growth in the reading, and a job the occupancy stage's own look
        missed, listed late, made its figure wrong. Return the stage's record."""
        moved = self._moved(run["ended_head"], self._ended_heads(), run)
        if moved:
            raise ProvisionError(
                f"a transfer ran or ended on {', '.join(moved)} after the occupancy stage's wait began: an eviction "
                "then can hide a growth in the quiet stage's reading, so it certifies nothing"
            )
        resetshortcut.require_held(run)
        return {
            "ran": True,
            "why": run["why"],
            "qos_rights": run["qos_rights"],
            "held_files": len({file_id for file_id, _ in run["evicted"]}),
            "held_source": resetheld.SOURCE,
            **run["quiet"],
            "listings_after_s": after_s,
        }

    # -- 2. running transfers ---------------------------------------------------------
    def _page(self, provider: str, state: str, token: str | None) -> tuple[list[str], str | None]:
        """One page of a provider's listing in one state: its ids and the next page's token, None on the last."""
        params = {"state": state, "limit": PAGE_LIMIT, **({"page_token": token} if token else {})}
        path = f"/spaces/{self._p.space_id}/transfers"
        with self._p.reading_slot(provider, self._reads):
            answer = self._p.client.get(path, provider=provider, params=params, retry=self._reads)
        return page_of(answer)

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
        lacked is a job that ended since (O46). A job that ended just before the earlier reading but was not listed yet
        counts so too, a wait more. A job is seen only once listed: a reading that must hold every job ended by an
        instant is made at least `transfer_settle_s`, the listing's lag (O07), after it."""
        return {p: frozenset(self._page(p, "ended", None)[0]) for p in self._providers}

    def _moved(
        self, before: Mapping[str, frozenset[str]], now: Mapping[str, frozenset[str]], run: Mapping[str, Any]
    ) -> list[str]:
        """The labels of the providers whose ended listing's first page holds a job `before` did not (it ended
        between the two readings), or that list a transfer waiting or ongoing now; none when no transfer has run or
        ended since `before`. A job of the reset's own (`run["own"]`, which it sent or cancelled and saw end before the
        occupancy's wait began) is never new, though its listing may come late (O07), and one whose record reported it
        ended (`run["ended"]`) is not running, though a listing may still show it ongoing (O51)."""
        own, ended = frozenset(run["own"]), run["ended"]
        moved = [p for p in self._providers if now[p] - before.get(p, frozenset()) - own]
        moved += [p for p, tid in self._active() if tid not in ended]
        return [self._label(p) for p in dict.fromkeys(moved)]

    def _cancel(self, provider: str, tid: str) -> bool:
        """Cancel one job on one provider through the gate, whose reads go under the stage's policy (`stage_reads`);
        return whether the cancel was answered. A 400 answered to the cancel is a job that has ended, which the wait
        then shows. A cancel that went past the gate and got no answer back (`client.sent_unanswered`) may or may not
        have been applied: it is never sent again (False), and the wait for the job's record decides, failing at its
        bound on a job that never ends (O09). A cancel the gate refused unsent (`NotSent`, which keeps the status of the
        gate's own read, a 400 among them, or `ScopeUnread` once its reads are spent) is never taken for either: it
        propagates, as does any other failure."""
        try:
            self._p.mutate("DELETE", f"/transfers/{tid}", provider=provider, gate_retry=self._reads)
        except PlatformError as error:
            if sent_unanswered(error):
                log.warning(
                    "%s%s: a cancel was sent and its answer was lost (%s); it is never sent again, the job's record "
                    "decides",
                    self._p.where,
                    self._label(provider),
                    error,
                )
                return False
            if isinstance(error, NotSent) or error.status != 400:
                raise
        return True

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
                    retry=self._reads,
                ),
                f"{self._label(provider)}: {what}",
            )

    def _observe(self, cancelled: set[tuple[str, str]], unanswered: set[tuple[str, str]]) -> bool:
        """One observation of the transfers stage: cancel every job a provider lists as waiting or ongoing that the
        stage has not cancelled yet, on that provider, and wait until each reports an ended state there; return whether
        any job was listed. `cancelled` gathers the `(provider, transfer id)` pairs the stage sent a cancel to (one
        answered 400, a job that had just ended, included, and one whose answer was lost, never sent again), and
        `unanswered` the last, both mutated on purpose: the driver counts the observations of every space and decides
        when each has settled (`resetspaces.py`)."""
        active = self._active()
        fresh = [pair for pair in dict.fromkeys(active) if pair not in cancelled]
        for provider, tid in fresh:
            if not self._cancel(provider, tid):
                unanswered.add((provider, tid))
            cancelled.add((provider, tid))
        self._wait_ended(fresh, "a cancelled transfer")
        return bool(active)

    def _require_quiet(self, run: Mapping[str, Any], waits: consistency.WaitLog | None = None) -> None:
        """Require the listings of every provider to show no transfer waiting or ongoing, observing again after the
        settle time up to `transfer_settle_polls` times (a job that has just ended may still be listed); raise when
        one is still listed at the bound. A block map or a physical size is read only after this passes. A job of the
        reset's own whose record has reported an ended state on the provider it ran on (`run["ended"]`: an eviction it
        sent, a job it cancelled) holds nothing up, whatever a listing still shows: a listing lags the record (O07),
        and a job that has ended changes no map. Any other job listed holds the read until no listing shows it. Each
        wait is logged in `waits`, when given, as `listed_running`."""
        settings, ended = self._p.settings, run["ended"]
        sleep = self._p.sleep if waits is None else waits.sleeper("listed_running", self._p.sleep)
        for observation in range(1, settings.transfer_settle_polls + 1):
            active = [(provider, tid) for provider, tid in self._active() if tid not in ended]
            if not active:
                return
            if observation < settings.transfer_settle_polls:
                sleep(settings.transfer_settle_s)
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
        (O46). The ids of the jobs it sends join the reset's own (`run["own"]`), which that check never takes for new
        when a listing shows them late, and, once their records have reported them ended, the ones that hold no read
        up (`run["ended"]`, `_require_quiet`). The record gives every wait the stage took, by why (`waits`)."""
        try:
            resolved = self._p.resolve_many((path for paths in plan.values() for path in paths), retry=self._reads)
        except KeyError:
            raise ProvisionError(NOT_IN_SPACE) from None
        out: dict[str, Any] = {}
        evicted: set[tuple[str, str]] = run["evicted"]  # mutated on purpose: what this stage hands the quiet stage
        own: set[str] = run["own"]  # mutated on purpose: the jobs it sends, which the occupancy stage knows as its own
        waits = consistency.WaitLog()
        watched = {"scheduled": own.update, "ended": run["ended"].update, "waits": waits, "reads": self._reads}
        quiet = partial(self._require_quiet, run, waits)
        for provider, paths in plan.items():
            entries = [resolved[path] for path in paths]
            results = [
                evict_tree(
                    self._p,
                    entry,
                    provider,
                    quiet=quiet,
                    listed=lambda ids, q=provider: evicted.update((file_id, q) for file_id in ids),
                    **watched,
                )
                for entry in entries
                if entry.kind == "DIR"
            ]
            files = [entry.file_id for entry in entries if entry.kind != "DIR"]
            evicted.update((file_id, provider) for file_id in files)
            if files:
                results.append(evict_ids(self._p, files, provider, quiet=quiet, **watched))
            unanswered = sum(r.get("unanswered", 0) for r in results)  # evictions sent once, their answers lost
            out[self._label(provider)] = {
                "trees": sum(1 for entry in entries if entry.kind == "DIR"),
                "files": sum(r["files"] for r in results),
                "transfers": sum(r["transfers"] for r in results),
                "polls": max((r["polls"] for r in results), default=0),
                "attempts": max((r["attempts"] for r in results), default=0),
                **sum_counts(results),
                **({"unanswered": unanswered} if unanswered else {}),
            }
        quiet()
        run["ended_head"] = self._ended_heads()
        run["evicted_at"] = self._p.clock()
        return {"providers": out, "waits": waits.facts()}

    # -- 4. occupancy -----------------------------------------------------------------
    def _holdings(self, run: dict[str, Any]) -> occupancy.Reading:
        """One reading of the occupancy: every regular file's block map at each provider that can hold the space's
        bytes, and the counter beside them (`occupancy.holdings`); under the shortcut, only the files that may have
        changed since the planner's reading (`run["only"]`, `resetshortcut.py`), the rest carried as empty. Every read
        of it, the counter's too, goes under a policy made for the reading."""
        return occupancy.holdings(
            self._p,
            run["files"],
            run["never_holds"],
            providers=self._providers,
            label=self._label,
            retry=self._p.reading_retry(),
            only=run["only"],
        )

    # The occupancy stage, in the steps `resetspaces` takes for every space, with one wait between each
    # (`_occupancy_*`). Its state is `run["occupancy_wait"]`: the instant the wait counts from, the first pages of the
    # ended listings it holds the next look to, the seconds waited, the rounds and the last reading.
    def _occupancy_due(self, run: dict[str, Any]) -> float:
        """Begin a round and return the instant the occupancy may be read at: `resetqos.occupancy_wait_s` for the
        deepest regular file of the space from the instant the wait counts from, the eviction stage's end in the
        first round, by when every eviction's and every ended transfer's change can show in the space root's physical
        size (O46). Under the shortcut, no earlier than `transfer_settle_s` after the transfers stage's last observation
        either, by when every job it did not see waiting or ongoing is on the ended listing the shortcut walks
        (`resetshortcut.py`): a later bound, which never weakens a check."""
        state = run.setdefault(
            "occupancy_wait", {"anchor": run["evicted_at"], "before": run["ended_head"], "waited": 0.0, "rounds": 0}
        )
        state["rounds"] += 1
        due = state["anchor"] + resetqos.occupancy_wait_s(max(run["deepest"], 1))
        if run["reads"]["mode"] == resetshortcut.SHORTCUT and run["transfers_at"] is not None:
            due = max(due, run["transfers_at"] + self._p.settings.transfer_settle_s)
        return due

    def _occupancy_read(self, run: dict[str, Any], waited: float) -> float:
        """After a wait of `waited` seconds, with no transfer running, read the occupancy (`_holdings`: each file's
        block map at each provider that can hold the space's bytes, and the counter beside them). Return how long after
        the reading the listings must be looked at, since a listing shows a job up to `transfer_settle_s` late (O07):
        that long when the quiet stage is skipped, so a reading is kept only when it holds every job's change; 0 when it
        runs, since the quiet stage's own look, that long after its reading and against the same first pages, covers
        the rest and fails the reset."""
        state = run["occupancy_wait"]
        state["waited"] += waited
        self._require_quiet(run)
        run["only"] = resetshortcut.narrowed(self, run)  # every round: a job that ran in the last is walked too
        state["reading"] = self._holdings(run)
        return self._p.settings.transfer_settle_s if run["why"] is None else 0.0

    def _occupancy_look(self, run: dict[str, Any]) -> bool:
        """Keep the reading (True) when no transfer ran or ended since the wait began: the ended listings' first
        pages, and the waiting and ongoing listings, read now. Otherwise a job no stage sent changed the space after
        the instant the wait counts from, and its change may not show: let it end, count the next wait from then, and
        return False, up to `OCCUPANCY_ROUNDS` waits in all, then fail."""
        state = run["occupancy_wait"]
        if not self._moved(state["before"], self._ended_heads(), run):
            return True
        if state["rounds"] == OCCUPANCY_ROUNDS:
            raise ProvisionError(
                f"transfers kept ending or running during the occupancy stage's wait, {OCCUPANCY_ROUNDS} waits in "
                "all: no reading of the occupancy is shown to hold their changes"
            )
        self._require_quiet(run)  # a job ran or ended since the wait began: let it end, then wait from then
        state["before"], state["anchor"] = self._ended_heads(), self._p.clock()
        return False

    def _occupancy_kept(self, allowed: Mapping[str, int], run: dict[str, Any], after_s: float) -> dict[str, Any]:
        """The reading kept: refuse a provider other than the data provider whose files hold more than it is allowed,
        stating the figures (`occupancy.judge`, O46); keep the reading for the quiet re-check, which would otherwise
        hold the occupancy to a figure still showing evicted bytes; return the stage's record, `after_s` being how long
        after the reading the listings were looked at."""
        state = run["occupancy_wait"]
        reading = state["reading"]
        run["occupancy"], run["ended_head"] = reading, state["before"]  # the quiet stage looks for jobs since this wait
        resetshortcut.require_held(run)
        occupancy.judge(reading, allowed, self._label)
        return {
            **reading.facts(self._label),
            "waited_s": round(state["waited"], 3),
            "depth": max(run["deepest"], 1),
            "rounds": state["rounds"],
            "listings_after_s": after_s,
        }

    def _ended_since(self, epoch: float, cancelled: Collection[tuple[str, str]]) -> dict[str, str]:
        """The jobs that may have run since `epoch` (this host's clock), with the file id each record names: every job
        each provider's ended listing shows finished since `epoch` less the clock-skew allowance and the margin, walked
        newest first as the history walks it (`transferorder.newest_first`), and every job the transfers stage
        cancelled (`cancelled`, (provider, id) pairs), whose record is read on its provider whatever the listing shows.
        Onezone's readings of the global clock, before and after the walk, place that cutoff on the clock finish times
        are stamped on (`transferorder.start_band`); raise Unreadable when they cannot, or for a record without a
        readable `finishTime` and `fileId`, and PlatformError for a read that fails (`resetshortcut.py`)."""
        settings = self._p.settings
        allowance = settings.clock_skew_allowance_s
        zone = [self._zone_clock()]
        jobs: dict[str, str] = {}
        for provider in self._providers:
            walk = newest_first(
                lambda token, q=provider: self._page(q, "ended", token),
                partial(self._jobs, provider),
                lambda job: job[0],
                cutoff=epoch - allowance - STOP_MARGIN_S,
                bound=self._listed_max,
                max_pages=self._listed_max // PAGE_LIMIT + 1,
                where=f"{self._label(provider)}: ",
                chunk=settings.concurrency,
            )
            jobs.update((tid, job[1]) for tid, job in walk.kept.items())
        for provider, tid in cancelled:
            jobs[tid] = self._job(provider, tid)[1]
        zone.append(self._zone_clock())
        start_band(epoch, zone, allowance)  # raises when the global clock cannot place the cutoff
        return jobs

    def _jobs(self, provider: str, ids: Sequence[str]) -> list[tuple[float, str]]:
        """The records of a few ended jobs on one provider (`_job`), `[placement] concurrency` at a time."""
        return consistency.bounded_map(
            lambda job_id: self._job(provider, job_id), list(ids), self._p.settings.concurrency
        )

    def _job(self, provider: str, tid: str) -> tuple[float, str]:
        """An ended job's finish time (`_finish`) and the file id its record names; Unreadable without either."""
        with self._p.reading_slot(provider, self._reads):
            record = self._p.client.get(f"/transfers/{tid}", provider=provider, retry=self._reads)
        value = record.get("finishTime") if isinstance(record, dict) else None
        file_id = record.get("fileId") if isinstance(record, dict) else None
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise Unreadable("an ended transfer's record has no finishTime that is a number")
        if not isinstance(file_id, str) or not file_id:
            raise Unreadable("an ended transfer's record names no fileId")
        return float(value), file_id

    # -- 5. ended-transfer history ------------------------------------------------------
    def _finish(self, provider: str, tid: str) -> float:
        """An ended job's finish time, from its record; 0 for a job cancelled before it started, which was given none
        (`transferorder.py`). One without a number there is unreadable."""
        with self._p.reading_slot(provider, self._reads):
            record = self._p.client.get(f"/transfers/{tid}", provider=provider, retry=self._reads)
        value = record.get("finishTime") if isinstance(record, dict) else None
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise Unreadable("an ended transfer's record has no finishTime that is a number")
        return float(value)

    def _zone_clock(self) -> ZoneClock | None:
        """Onezone's reading of the global clock, the one finish times are stamped on, from its public current-time
        endpoint, sent with no credential: its offset from this host and half the round trip, which spans any retry;
        None when the answer cannot be read. A read that fails raises, and fails the stage."""
        sent = self._now()
        answer, date = self._p.client.get_dated(ZONE_TIME_PATH, retry=self._reads, anonymous=True)  # Onezone: no slot
        return zone_clock_of(answer, date, sent, self._now())

    def _dated_page(self, provider: str, token: str | None) -> tuple[list[str], str | None, float | None]:
        """One page of a provider's ended listing (its ids and the next page's token, None on the last) with the
        provider's system-clock offset from this host that the answer's Date header gives, None when it cannot be read:
        a diagnostic, since a finish time is on the global clock."""
        params = {"state": "ended", "limit": PAGE_LIMIT, **({"page_token": token} if token else {})}
        path = f"/spaces/{self._p.space_id}/transfers"
        with self._p.reading_slot(provider, self._reads):
            answer, date = self._p.client.get_dated(path, provider=provider, params=params, retry=self._reads)
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
