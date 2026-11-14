# Provenance: new
"""Several spaces reset as one: each space's stages in order, the waits they share taken once, the spaces' own work
at once (O07, O11, O46).

`SpaceReset` (`reset.py`) has the stages of one space's reset and what each proves. A study with several spaces resets
each of them between runs, and reset one after another they wait their waits one after another: the transfer
settle, the occupancy's lag, the quiet wait and the listings' late look, each once per space. `SpacesReset` resets them
together, stage by stage, every space's stages in `STAGES` order (no space begins a stage before its previous one has
ended, and none begins one while another space is still in an earlier one):

- **qos, qos_settle, eviction, history:** each space's own work, at once.
- **transfers:** every space observes its listings, cancels what they show and waits for it to end; one
  `transfer_settle_s` then covers every space's next observation. **The fast settle:** a space whose first observation
  shows nothing waiting or ongoing, and that cancelled nothing, settles on its second observation when that shows
  nothing either, one `transfer_settle_s` after the first: the listing shows a job at most `transfer_settle_s` after
  it is scheduled (the setting's meaning, O07), and the reset runs between runs, under the run's exclusive admission,
  so nothing but the reset schedules a job meanwhile; a job scheduled before the first observation and still waiting or
  ongoing at the second is therefore listed at the second. The transfer log settles on the same two observations
  (`transferlog.py`). A space where anything was seen waiting or ongoing, or cancelled, at any observation needs
  `transfer_settle_polls` clean observations in a row, within twice that many, as before. The record says which
  (`fast`), with the observations and the cancels.
- **occupancy:** one wait, to the latest instant any space may read its occupancy at (each space's eviction end plus
  the lag for its deepest file, O46), then every space's reading; one more `transfer_settle_s` when any space's quiet
  stage is skipped, then every space's look at its listings; a space whose look shows a job that ran or ended waits
  again, from then, with the others still waiting.
- **quiet:** every space reads its block maps at its own T; one wait, to the latest T plus that space's wait; every
  space's occupancy; one `transfer_settle_s`; every space's late look.

**A shared wait never weakens a check.** Each of the waits is a lower bound: the occupancy must be read no earlier
than the lag after the eviction stage's end, the quiet stage's occupancy no earlier than its wait after T, the listings
no earlier than `transfer_settle_s` after a reading, and a transfer observation no earlier than that after the one
before. One wait to the latest of the spaces' instants keeps every bound, since every reading it covers is taken after
it ends, and every look after a reading made before it began. A later reading still shows every change an earlier one
would have (a change shows once its lag has passed), and a job that ran or ended in the longer window is in the
listings the stage looks at, and makes it wait again or fail. So a longer wait can make a reset fail where it would
have passed, never pass where it would have failed. Holding a space between its stages, while another finishes, only
runs its next check later: every check reads the federation's state when it runs.

**At once, within bounds.** At most `[placement] reset_concurrency` spaces (4 by default) do their own work at once,
each under its own `[placement] concurrency` (16), so at most their product of requests is in flight: 64 with the
defaults, under the 100 connections of the HTTP client's pool (httpx's default, which the client keeps), which makes a
request beyond them wait for a connection rather than fail. Each space's work is its own provisioner's, on its own
space; the client and the fake federation serve several threads. Every wait the spaces share is taken on the calling
thread, never in a space's work, so a reset with `reset_concurrency` 1 runs wholly on it.

**A failure in one space fails the reset.** A stage that fails in one space is recorded there, as `SpaceReset.run`
records it; the other spaces end the stage they are in, and no space begins a later one. `ResetFailed` then holds every
space's record (`spaces`, in the order given): the failed space's stages up to the one that failed, and each other
space's up to where it stopped, with `halted_before`, the stage it did not begin. Its `stage` is the first failed
space's. The caller must not start the next run.

The record (`run`): `ok`, `spaces` (each space's record, as `SpaceReset.run` returns it), `seconds`, `stages` (each
stage's seconds across the spaces), `shared_waits` (per stage, the `count` and `seconds` of the waits taken once for
every space) and `concurrency`; a failed one adds `failed` (the positions of the spaces that failed) and `stage`.
"""

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any

from expcore_onedata import consistency, resetheld
from expcore_onedata.provision import ProvisionError
from expcore_onedata.reset import FAILURES, NOT_IN_SPACE, STAGES, ResetFailed, SpaceReset


@dataclass(frozen=True)
class ResetPlan:
    """What one space's reset is given, as `SpaceReset.run` takes it: the eviction plan (provider to paths), the
    physical bytes each provider may keep, the history's window start, the QoS baseline and `qos_rights`."""

    evict: Mapping[str, Iterable[str]] | None = None
    allowed: Mapping[str, int] | None = None
    since: float | None = None
    qos_baseline: Collection[str] = ()
    qos_rights: bool | None = None


class _Space:
    """One space's part of the reset: its checked plan, what its stages hand on (`run`) and its record. A space is
    worked on by one thread at a time, so its state needs no lock."""

    def __init__(self, reset: SpaceReset, plan: ResetPlan) -> None:
        since, baseline, rights = plan.since, plan.qos_baseline, plan.qos_rights
        if since is not None and (isinstance(since, bool) or not isinstance(since, int | float) or not isfinite(since)):
            raise ValueError("the history's since is a finite number of epoch seconds")
        if isinstance(baseline, str) or not all(isinstance(q, str) for q in baseline):
            raise ValueError("the QoS baseline is a collection of requirement ids")
        if rights is not None and not isinstance(rights, bool):
            raise ValueError("qos_rights is True, False, or None for a caller that does not say")
        self.reset, self.p = reset, reset.provisioner
        self.since = float(reset.now() if since is None else since)
        self.baseline = baseline
        # What one stage hands a later one: the files a removed requirement applied to, why the quiet check runs (None:
        # it is skipped), the depth of the deepest file of the space, the (file, provider) pairs the eviction emptied
        # (collected as it lists them), the instant the eviction stage ended and the ended listings' first pages then
        # (later, at the occupancy stage's accepted reading), the maps the settle ended on, the occupancy.
        self.run: dict[str, Any] = {
            "affected": {},
            "why": resetheld.quiet_reason(0, rights),
            "deepest": 0,
            "evicted": set(),
            "evicted_at": None,
            "ended_head": {},
            "own": set(),  # the ids of the jobs the reset sent or cancelled, all ended before the occupancy's wait
            "ended": set(),  # those of them whose records have reported them ended: they hold no read up
            "settled": {},
            "occupancy": {},
            "qos_rights": rights,
        }
        self.plan = {provider: list(dict.fromkeys(paths)) for provider, paths in (plan.evict or {}).items()}
        self.allowed = dict(plan.allowed or {})
        for provider in [*self.plan, *self.allowed]:
            self.p.check_eviction(provider)
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in self.allowed.values()):
            raise ProvisionError(f"{self.p.where}an allowed holding is a whole number of bytes, zero or more")
        self.record: dict[str, Any] = {
            "space": self.p.space.position,
            "ok": False,
            "stages": {},
            "seconds": None,
            "reads_retried": 0,
        }
        self.failure: ResetFailed | None = None
        self.settle = _Settle(self.p.settings.transfer_settle_polls, set())  # the transfers stage's
        self._began, self._retried = self.p.clock(), self.p.reads_retried
        self._started: dict[str, float] = {}

    @property
    def live(self) -> bool:
        return self.failure is None

    def begin(self, stage: str) -> None:
        self._started[stage] = self.p.clock()

    def done(self, stage: str, facts: Mapping[str, Any]) -> None:
        seconds = round(self.p.clock() - self._started[stage], 3)
        self.record["stages"][stage] = {"ok": True, "seconds": seconds, **facts}

    def step[T](self, stage: str, call: Callable[[], T]) -> T | None:
        """One step of a stage: its result, or None once a failure the stage records has failed the space."""
        try:
            return call()
        except FAILURES as error:
            self.fail(stage, error)
            return None

    def attempt(self, stage: str, call: Callable[[], Mapping[str, Any]]) -> None:
        """A whole stage of the space's own: begun, run, and recorded ok or failed."""
        self.begin(stage)
        facts = self.step(stage, call)
        if facts is not None:
            self.done(stage, facts)

    def fail(self, stage: str, error: Exception) -> None:
        """Fail the space at a stage, as `SpaceReset.run` records a failed stage: its seconds, the facts the failure
        carries, its message (never a path's part, O12), and the record so far."""
        detail = NOT_IN_SPACE if isinstance(error, KeyError) else str(error)
        seconds = round(self.p.clock() - self._started[stage], 3)
        self.record["stages"][stage] = {"ok": False, "seconds": seconds, **getattr(error, "facts", {})}
        self.record["stages"][stage]["error"] = detail
        self.close(ok=False)
        message = (
            f"{self.p.where}the reset failed at its {stage} stage ({type(error).__name__}): {detail}; "
            "the next run must not start"
        )
        self.failure = ResetFailed(message, stage, self.record)

    def close(self, *, ok: bool, halted_before: str | None = None) -> None:
        self.record.update(
            ok=ok, seconds=round(self.p.clock() - self._began, 3), reads_retried=self.p.reads_retried - self._retried
        )
        if halted_before is not None:
            self.record["halted_before"] = halted_before


@dataclass
class _Settle:
    """One space's transfer settle: the cancels sent, the observations, the clean ones in a row, and whether every
    observation so far showed nothing (`fast`), so that two clean ones settle it."""

    polls: int
    cancelled: set[tuple[str, str]]
    observations: int = 0
    clean: int = 0
    fast: bool = True

    def observed(self, active: bool) -> None:
        self.observations += 1
        self.fast = self.fast and not active
        self.clean = 0 if active else self.clean + 1

    @property
    def settled(self) -> bool:
        return self.clean >= (2 if self.fast else self.polls)

    @property
    def exhausted(self) -> bool:
        return self.observations >= 2 * self.polls


class SpacesReset:
    """The reset of several spaces, each through its own `SpaceReset`, as one: stage by stage, the shared waits once."""

    def __init__(self, resets: Sequence[SpaceReset], *, concurrency: int | None = None) -> None:
        """`resets` are the spaces' resets, each bound to its own space, which they must all share one clock with (one
        process's monotonic clock, or a test's). `concurrency` is how many spaces work at once; by default the first
        reset's `[placement] reset_concurrency`."""
        self._resets = list(resets)
        if not self._resets:
            raise ValueError("a reset of several spaces resets at least one")
        positions = [reset.provisioner.space.position for reset in self._resets]
        if len(set(positions)) != len(positions):
            raise ValueError("a reset of several spaces names each space once")
        first = self._resets[0].provisioner
        workers = first.settings.reset_concurrency if concurrency is None else concurrency
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("a reset's concurrency is a whole number of spaces, one or more")
        self._concurrency, self._clock, self._sleep = workers, first.clock, first.sleep
        self._waits: dict[str, dict[str, Any]] = {}

    # -- the run -----------------------------------------------------------------
    def run(self, plans: Sequence[ResetPlan] | None = None) -> dict[str, Any]:
        """Check every plan, verify every space, run the stages and return the record; raise ResetFailed, with every
        space's record, once a stage has failed in any space. `plans` holds one plan per reset, in the same order
        (by default an empty plan for each)."""
        began = self._clock()
        spaces = self._run(plans)
        record = self._record(spaces, began)
        failures = [space.failure for space in spaces if space.failure is not None]
        if not failures:
            return record
        where = ", ".join(f"{failure.record['space']} at its {failure.stage} stage" for failure in failures)
        record.update(failed=[failure.record["space"] for failure in failures], stage=failures[0].stage)
        message = f"the reset of {len(spaces)} space(s) failed: {where}. The first: {failures[0]}"
        raise ResetFailed(message, failures[0].stage, record)

    def run_one(self, plan: ResetPlan) -> dict[str, Any]:
        """The reset of the one space this driver holds, as `SpaceReset.run` returns it and raises."""
        if len(self._resets) != 1:
            raise ValueError("run_one resets one space")
        (space,) = self._run([plan])
        if space.failure is not None:
            raise space.failure
        return space.record

    def _run(self, plans: Sequence[ResetPlan] | None) -> list["_Space"]:
        given = list(plans) if plans is not None else [ResetPlan() for _ in self._resets]
        if len(given) != len(self._resets):
            raise ValueError("a reset of several spaces takes one plan per space, in the order of its resets")
        spaces = [_Space(reset, plan) for reset, plan in zip(self._resets, given, strict=True)]
        for space in spaces:
            space.p.verify_space()
        self._waits, self._walls = {}, {}
        stages: dict[str, Callable[[list[_Space]], None]] = {
            "qos": lambda live: self._each(live, "qos", lambda s: s.reset._qos(s.baseline, s.run)),
            "qos_settle": lambda live: self._each(live, "qos_settle", lambda s: s.reset._qos_settle(s.run)),
            "transfers": self._transfers,
            "eviction": lambda live: self._each(live, "eviction", lambda s: s.reset._evict(s.plan, s.run)),
            "occupancy": self._occupancy,
            "quiet": self._quiet,
            "history": lambda live: self._each(live, "history", lambda s: s.reset._history(s.since)),
        }
        for index, stage in enumerate(STAGES):
            started = self._clock()
            stages[stage](spaces)
            self._walls[stage] = round(self._clock() - started, 3)
            if any(not space.live for space in spaces):
                following = STAGES[index + 1] if index + 1 < len(STAGES) else None
                for space in spaces:
                    if space.live:
                        space.close(ok=False, halted_before=following)
                return spaces
        for space in spaces:
            space.close(ok=True)
        return spaces

    def _record(self, spaces: list[_Space], began: float) -> dict[str, Any]:
        return {
            "ok": all(space.live for space in spaces),
            "spaces": [space.record for space in spaces],
            "seconds": round(self._clock() - began, 3),
            "stages": dict(self._walls),
            "shared_waits": {stage: dict(wait) for stage, wait in self._waits.items()},
            "concurrency": self._concurrency,
        }

    # -- how the spaces work, and wait -------------------------------------------
    def _fan(self, spaces: Sequence[_Space], call: Callable[[_Space], Any]) -> list[Any]:
        """Call `call` on every live space, at most `concurrency` at once, and return the results in order (None for a
        space that failed); with one worker, on this thread."""
        live = [space for space in spaces if space.live]
        results = consistency.bounded_map(call, live, self._concurrency)
        found = dict(zip(map(id, live), results, strict=True))
        return [found.get(id(space)) for space in spaces]

    def _each(self, spaces: Sequence[_Space], stage: str, call: Callable[[_Space], Mapping[str, Any]]) -> None:
        """A stage of each space's own, at once."""
        self._fan(spaces, lambda space: space.attempt(stage, lambda: call(space)))

    def _wait(self, stage: str, seconds: float) -> float:
        """One wait for every space, on this thread; return the seconds waited."""
        wait = max(0.0, seconds)
        entry = self._waits.setdefault(stage, {"count": 0, "seconds": 0.0})
        if wait > 0:
            self._sleep(wait)
            entry["count"] += 1
            entry["seconds"] = round(entry["seconds"] + wait, 3)
        return wait

    # -- 2. transfers ------------------------------------------------------------------
    def _transfers(self, spaces: list[_Space]) -> None:
        """Observe every space, cancel what it lists and wait for it to end, until each has settled: on two clean
        observations when every observation showed nothing (`_Settle.fast`), else on `transfer_settle_polls` in a row,
        within twice that many. One `transfer_settle_s`, the longest any space's settings give, covers every space's
        next observation. A space's cancelled jobs join the reset's own (`run["own"]`, O46)."""
        for space in spaces:
            space.begin("transfers")
        pending = list(spaces)
        while pending:
            self._fan(pending, self._observe)
            pending = [space for space in pending if space.live and not self._settled(space)]
            if pending:
                self._wait("transfers", max(space.p.settings.transfer_settle_s for space in pending))

    @staticmethod
    def _observe(space: _Space) -> None:
        settle = space.settle
        active = space.step("transfers", lambda: space.reset._observe(settle.cancelled))
        if active is not None:
            settle.observed(active)

    @staticmethod
    def _settled(space: _Space) -> bool:
        """Whether a space's transfers stage has ended: settled and recorded, or failed at its bound."""
        settle = space.settle
        if settle.settled:
            space.run["own"].update(tid for _, tid in settle.cancelled)
            space.run["ended"].update(tid for _, tid in settle.cancelled)  # each was waited on until it ended
            cancelled = len({tid for _, tid in settle.cancelled})
            space.done("transfers", {"cancelled": cancelled, "observations": settle.observations, "fast": settle.fast})
            return True
        if settle.exhausted:
            message = (
                f"the listings had not shown no transfer waiting or ongoing {settle.polls} times in a row after "
                f"{settle.observations} observations"
            )
            space.fail("transfers", ProvisionError(message))
            return True
        return False

    # -- 4. occupancy ------------------------------------------------------------------
    def _occupancy(self, spaces: list[_Space]) -> None:
        """One wait to the latest instant any space may read its occupancy at; every space's reading; one late look's
        wait when any space needs one; every space's look. A space whose look shows a job that ran or ended waits
        again, from then, with the others that do (`SpaceReset._occupancy_*`)."""
        for space in spaces:
            space.begin("occupancy")
        pending = list(spaces)
        while pending:
            pending = self._occupancy_round(pending)

    def _occupancy_round(self, pending: list[_Space]) -> list[_Space]:
        """One round of the occupancy stage for the spaces still waiting; return those that must wait again."""
        due = self._fan(pending, lambda s: s.step("occupancy", lambda: s.reset._occupancy_due(s.run)))
        waited = self._wait("occupancy", max((d for d in due if d is not None), default=0.0) - self._clock())
        late = self._fan(pending, lambda s: s.step("occupancy", lambda: s.reset._occupancy_read(s.run, waited)))
        after = self._wait("occupancy", max((d for d in late if d is not None), default=0.0))
        kept = self._fan(pending, lambda s: s.step("occupancy", lambda: s.reset._occupancy_look(s.run)))
        for space, keep in zip(pending, kept, strict=True):
            if space.live and keep:
                facts = space.step("occupancy", lambda s=space: s.reset._occupancy_kept(s.allowed, s.run, after))
                if facts is not None:
                    space.done("occupancy", facts)
        return [space for space, keep in zip(pending, kept, strict=True) if space.live and keep is False]

    # -- 5. quiet ----------------------------------------------------------------------
    def _quiet(self, spaces: list[_Space]) -> None:
        """Every space that runs the quiet stage reads its block maps at its own T; one wait to the latest T plus that
        space's wait; every space's occupancy; one `transfer_settle_s`; every space's late look at the listings. A
        space whose stage is skipped records so."""
        running = []
        for space in spaces:
            space.begin("quiet")
            skipped = space.reset._quiet_skipped(space.run)
            if skipped is None:
                running.append(space)
            else:
                space.done("quiet", skipped)
        due = self._fan(running, lambda s: s.step("quiet", lambda: s.reset._quiet_maps(s.run)))
        running = [space for space in running if space.live]
        if not running:
            return
        self._wait("quiet", max(d for d in due if d is not None) - self._clock())
        self._fan(running, lambda s: s.step("quiet", lambda: s.reset._quiet_sizes(s.run)))
        running = [space for space in running if space.live]
        if not running:
            return
        after = self._wait("quiet", max(space.p.settings.transfer_settle_s for space in running))
        looked = self._fan(running, lambda s: s.step("quiet", lambda: s.reset._quiet_look(s.run, after)))
        for space, facts in zip(running, looked, strict=True):
            if space.live and facts is not None:
                space.done("quiet", facts)


__all__ = ["ResetPlan", "SpacesReset"]
