# Provenance: new
"""The link half of settling a restore (O35): the session gate, every ordered pair, and the heal when allowed.

`LinkSettling` is a part of `fault_recovery.Recovery`, kept in a module of its own. Once a restored provider serves,
`_links_settled` polls the session gate (`fault_sessions.await_sessions`) within `gate_timeout_s`, then checks every
ordered pair through the driver (`ComposeDriver.link_matrix`, or the detail alone, `restored_links`), and, only when the
caller holds the federation exclusively and `[faults] heal` allows, runs the heal (`fault_heal.LinkHeal`). At the
heal's start it asks `heal_skip` (the injector's: a step of the run that uses its tokens still runs and the long token
margin is not declared): a skipped heal is recorded in `restore_record` (`heal_skipped`) and the restore fails
`links_stuck`, the injection not marked healed, so a later settling with no such step may heal it.

**Once per injection, durably.** Before its first action the heal journals that it heals the injection: a `clear_failed`
line (`links_stuck`, which the restore is at that moment) marked `healing`, which `InjectionJournal.open_entries` reads
back as `healed`. Neither a later clear by the same injector nor another process's recovery of the same injection heals
it again, so no provider gets a second `restart_link` or container restart in its restore. A journal that cannot take
the line heals nothing. Before the line the heal raises the federation's link epoch (`fault_epoch`), so a run whose
pre-run check passed before it is refused, uncharged, at its admission; an epoch that cannot be raised heals nothing.
An injection that is itself a heal's container restart (`heal` on its line) is never healed.

**A reader's container restart** (`restart_container`) is an injection of its own: journalled before its stop with the
gate probe of the injection it heals, under the reader's lock, restored and waited for as any injection is, and
**cleared only when the heal ends with every pair passing**; otherwise it is journalled `clear_failed` with the heal's
word and stays open, and recovery (which settles such entries first, `Recovery.recover`) brings it back and checks it.
The gate's and the heal's readings go into `restore_record`, for the injector's snapshot.

**A link check whose record was not written says so there too (O35).** Every check this settling makes, the restore's
own and each read of the pairs the heal makes, answers with a matrix that holds why its record was not written, if it
was not (`LinkMatrix.not_written`, from `fault_serves.recorded`), and the settling puts `link_record: "not_written"`
and `link_record_reason` (`linkrecord.fact`) into `restore_record`, the latest such check's, so the injector's snapshot
and its `recovered` step carry it. Nothing is put there when every record was written.

**The settle of a recovered container restart.** A recovery that finds a heal's container restart open (its heal was cut
short, or ended with a pair failing) first waits out every hold its heal left (one reading then decides: a reader the
heal never stopped is `never_down` and cleared), brings the reader back, verified (a new identity, serving), and reads
its session gate only once a settle journalled on that entry has passed: when it started the container itself, or when
none is journalled (who started it, and when, is not known), it holds one of `heal_settle_s` from the moment the reader
serves, in the federation's drain record first when it has one (`hold_for`), then on the entry (`settle_until`), a
`clear_failed` line `settling` marked `healing`, which no reader takes for a failed restore. A recovery that may not
wait (the fault gate's, whose check the core bounds by `health_timeout_s`: `recover(defer=True)`) never sleeps out a
held settle: it raises `RestoreSettling`, which the gate answers as a wait that charges no run; a later check, once the
settle has passed, reads the gate and clears the entry when it passes, and the gate's own link check, next in that
check, judges every pair (an entry left open would keep every later heal out). Any other recovery (the injector's start)
waits the settle out and clears the entry only when the gate and every pair pass.

**The fault gate's heal** (`heal_stuck`, `fault_gate_heal`) runs the same heal on stuck pairs the gate's own check
found, with no injection to journal it on: its holds are the caller's, and its container restarts are cleared or left
open as a restore's are.
"""

import contextlib
import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from expcore.campaign import liveness
from expcore.errors import InfraError
from expcore_onedata.compose import DriverError, Observation
from expcore_onedata.fault_epoch import HEAL, LinkEpoch
from expcore_onedata.fault_heal import HealResult, LinkHeal
from expcore_onedata.fault_journal import InjectionJournal, holder
from expcore_onedata.fault_matrix import LinkMatrix
from expcore_onedata.fault_sessions import await_sessions
from expcore_onedata.fault_settings import (
    FaultSettings,
    command_bound_s,
    heal_all_links_reserve,
    heal_container_reserve,
    heal_link_reserve,
)
from expcore_onedata.linkrecord import fact
from expcore_onedata.sandbox.kinds import INJECTOR_FAILURE

log = logging.getLogger("expcore_onedata.fault_recovery")


class RestoreSettling(InfraError):
    """A recovery that may not wait (`recover(defer=True)`, the fault gate's) met a held settle before a gate read or
    a link check: nothing was read, the entry stays open, and a later recovery reads once `left_s` has passed. The fault
    gate answers it as a wait that charges no run. The detail names the target by its label only (O12)."""

    def __init__(self, left_s: float, target: str | None = None) -> None:
        self.left_s = max(0.0, left_s)
        whose = f"the restore of {target}" if target is not None else "a restore"
        detail = (
            f"{whose} waits {self.left_s:.0f} s more for a heal's settle before its session gate and every pair are "
            "read: the check waits"
        )
        super().__init__(INJECTOR_FAILURE, detail, target=target)


def digest(identity: str) -> str:
    """The SHA-256 of an identity, the only form in which one is journalled or recorded."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def stop_record(driver: Any) -> dict[str, Any]:
    """What a driver kept of the stop it just made (`fault_hpc.HpcDriver.last_stop`: whether it was confirmed, how it
    ended and its seconds), as the `stop` key of the injection's `injected` or `stop_unknown` line and step; nothing
    for a driver that keeps none (Compose), or for a stop that did not answer."""
    found = getattr(driver, "last_stop", None)
    timing = (getattr(driver, "last_timing", None) or {}).get("stop")  # where the stop's time went (O58), else nothing
    if not isinstance(found, Mapping) and not isinstance(timing, Mapping):
        return {}
    evidence = dict(found) if isinstance(found, Mapping) else {}
    return {"stop": {**evidence, **({"timing": dict(timing)} if isinstance(timing, Mapping) else {})}}


def unhealed_detail(record: Mapping[str, Any], latest: LinkMatrix, before: str | None) -> str:
    """Why a heal left the links stuck, for the restore's failure: the heal's own why (else the check's detail before
    it), then each pair failing at the latest link check that answered, named by its reading provider, the one whose
    link the remedy restarts, and its source, by label (O35, O12). `latest` is that check: the heal's last one, or,
    when a later one could not run, the last that answered (the restore's own check when none of the heal's did), so
    the failure always names the readers the last answer found failing."""
    why = str(record.get("why") or before)
    failing = [f"{pair.reader} reading from {pair.source}" for pair in latest.failing()]
    if failing:
        return f"{why}; failing at the latest link check that answered, by reading provider: {', '.join(failing)}"
    return why


class LinkSettling:
    """The link half of `Recovery` (see the module docstring); `Recovery` provides every attribute it reads."""

    _settings: FaultSettings
    _driver: Any
    _clock: Callable[[], float]
    _sleep: Callable[[float], None]
    _labels: dict[str, str]
    _healed: set[str]
    _deadline: float | None
    _quiet_until: float | None
    _wall: Callable[[], float]
    _waited_wall: float
    _heal_skip: Callable[[], Mapping[str, Any] | None] | None  # asked at a heal's start: why to skip it, or None
    _hold_for: Callable[[float], None] | None
    _defer: bool
    journal: InjectionJournal
    links_detail: str | None
    restore_record: dict[str, Any]
    settle: Callable[..., Any]
    wait: Callable[..., Observation | None]

    def hold(self, seconds: float, entry: str | None = None) -> None:
        """Mark that nothing may read through the links for `seconds` from now: a heal sends it before and after every
        restart, so a later gate read or link check, even one after an interrupted heal, waits until the settle could
        have ended (`_await_quiet`). With `entry`, the healed injection's journal id, the hold is also journalled
        durably (`settle_until`, on the wall clock, on a `clear_failed` line), so a recovery in another process, after
        this one died or exited mid-settle, waits for it too. A journal that cannot take it raises: no restart is then
        sent."""
        self._quiet_until = self._clock() + seconds
        if entry is not None:
            until = self._wall() + seconds
            self.journal.append({"event": "clear_failed", "id": entry, "how": "links_stuck", "settle_until": until})
            self._waited_wall = max(self._waited_wall, until)  # its own: the clock's wait above covers it

    def _await_quiet(self) -> None:
        """Wait out what is left of the latest held settle, this recovery's own and every one the journal holds for
        an open injection, before any gate read or link check; a recovery that may not wait (`_defer`) raises
        `RestoreSettling` instead, having read nothing. A journal that cannot be read raises (fail closed)."""
        until = self._quiet_until
        if until is not None and self._clock() < until:
            self._wait_out(until - self._clock())
        journalled = [entry.get("settle_until") for entry in self.journal.open_entries()]
        latest = max((value for value in journalled if isinstance(value, int | float)), default=None)
        if latest is not None and latest > self._waited_wall:  # another's hold, or one this recovery has not waited
            if self._wall() < latest:
                self._wait_out(latest - self._wall())
            self._waited_wall = latest

    def _wait_out(self, seconds: float) -> None:
        """Sleep out what is left of a held settle, or, in a recovery that may not wait, raise `RestoreSettling`."""
        if self._defer:
            raise RestoreSettling(seconds)
        self._sleep(seconds)

    def _settle_child(self, entry: Mapping[str, Any], how: str) -> None:
        """A heal's container restart this recovery has just verified serving: when this recovery started the container
        (`restored`), or no settle is journalled on its entry (who started it, and when, is not known: a recovery killed
        after its start and before its hold leaves exactly that), hold a settle of `heal_settle_s` from now, in the
        federation's drain record first (`hold_for`, when this recovery has one), then on the entry, so that its gate
        and pairs are read, in any process, only once it has passed (`_await_quiet`). A record that cannot be written
        raises, and nothing is read."""
        until = entry.get("settle_until")
        if how != "restored" and isinstance(until, int | float) and not isinstance(until, bool):
            return  # held already, by the recovery that started it: `_await_quiet` waits for it, or defers
        seconds = self._settings.heal_settle_s
        until = self._wall() + seconds  # read before the drain record's own: the record never ends before the line
        if self._hold_for is not None:
            self._hold_for(seconds)
        line = {"event": "clear_failed", "id": entry["id"], "how": "settling", "healing": True, "settle_until": until}
        self.journal.append(line)  # `healing`: a recovery under way, never taken for a failed restore

    def _child_settled(self, entry: Mapping[str, Any], heal: bool) -> str | None:
        """A heal's container restart once its settle has passed: None when it may be cleared, else the word it fails
        with. In a recovery that may not wait (`_defer`, the fault gate's) its session gate alone decides: the gate's
        own link check of every pair comes next in the same check, keeps every run out until each passes, and heals a
        stuck pair again when the latch allows, which an entry left open would forbid for ever. In any other (the
        injector's start, which injects next) the gate and every pair decide (`_links_settled`)."""
        if not self._defer:
            return self._links_settled(entry, heal)
        gate = self._gate(entry["service"], entry.get("gate_probe"))
        self.restore_record["gate"] = gate
        return None if gate["ok"] is True else "sessions_not_ready"

    def _gate(self, service: str, gate_probe: str | None, timeout_s: float | None = None) -> dict[str, Any]:
        """The session gate for a provider's service, polled within `gate_timeout_s`, or `timeout_s` when shorter;
        never before a held settle has ended."""
        self._await_quiet()
        bound = self._settings.gate_timeout_s if timeout_s is None else timeout_s
        return await_sessions(self._driver, service, gate_probe, bound, self._settings.poll_s, self._clock, self._sleep)

    def _may_heal(self, entry: Mapping[str, Any], heal: bool, matrix: LinkMatrix | None) -> bool:
        """Whether this settling may heal: asked to (the exclusive hold), declared, with a matrix, the injection not a
        heal's own container restart, never healed before (in memory or in the journal), and its target known."""
        target = entry.get("target")
        return (
            heal
            and self._settings.heal != "off"
            and matrix is not None
            and entry.get("heal") is not True
            and entry.get("healed") is not True
            and entry["id"] not in self._healed
            and isinstance(target, str)
            and target in self._labels
        )

    def _links_settled(self, entry: Mapping[str, Any], heal: bool) -> str | None:
        """After a restored provider serves: the session gate, then every ordered pair, then the heal when allowed;
        None when the restore may be cleared, else the word it fails with."""
        service, gate_probe = entry["service"], entry.get("gate_probe")
        gate = self._gate(service, gate_probe)
        self.restore_record["gate"] = gate
        if gate["ok"] is not True:
            return "sessions_not_ready"
        self.links_detail, matrix = self._stuck_links(service)
        if self.links_detail is None:
            return None
        log.warning("a restored provider's link check failed: %s", self.links_detail)
        if matrix is None or not self._may_heal(entry, heal, matrix):
            return "links_stuck"
        skipped = self._heal_skip() if self._heal_skip is not None else None
        if skipped is not None:  # a step of the run that uses its tokens still runs: no heal may outlast its token
            self.restore_record["heal_skipped"] = dict(skipped)
            log.warning("a restored provider's heal was skipped: a step of the run that uses its tokens still runs")
            return "links_stuck"  # not marked healed: a later settling with no such step may heal it
        self._healed.add(entry["id"])  # one heal per injection: a later settling of it only checks
        try:  # durable before any action, so no process heals this injection again, and a run checked before the heal
            # is refused at its admission (`fault_epoch`)
            LinkEpoch(self.journal.directory).raise_for(HEAL)
            self.journal.append({"event": "clear_failed", "id": entry["id"], "how": "links_stuck", "healing": True})
        except (OSError, ValueError):
            return "links_stuck"
        how = self._heal(entry, service, gate_probe, matrix)
        return None if how == "healed" else how

    def _heal(self, entry: Mapping[str, Any], service: str, gate_probe: str | None, matrix: LinkMatrix) -> str:
        """Run the heal, record it, and clear or fail the container restarts it made; return how it ended."""

        def hold(seconds: float) -> None:
            self.hold(seconds, str(entry["id"]))  # journalled on the healed injection, for every process to wait out

        result, latest, children = self._run_heal(service, str(entry["target"]), gate_probe, hold, matrix)
        self.restore_record["heal"] = {**result.record, "first": matrix.to_json()}
        self._close_children(result, children)
        self.links_detail = None if result.healed else unhealed_detail(result.record, latest, self.links_detail)
        return result.how

    def heal_stuck(
        self,
        service: str,
        first: LinkMatrix,
        *,
        hold: Callable[[float], None],
        gate_probe: str | None,
        stopping: Callable[[], bool] | None = None,
    ) -> tuple[HealResult, LinkMatrix]:
        """The fault gate's heal of the stuck pairs its own link check found (`first`), with no injection open and no
        restored provider (`expcore_onedata.fault_gate_heal`): the same heal, its bounds, settle and fallback, by the
        caller's `hold`, never restarting the container of a provider that was ever the source of a failing pair, and
        starting no step once `stopping()` (a clean exit) says so. `service` is any declared service, which the
        driver's link check takes. The container restarts it made are cleared when every pair passes, else left open,
        journalled `clear_failed`. Return how it ended and the latest link check that answered."""
        sources = first.sources()
        result, latest, children = self._run_heal(service, None, gate_probe, hold, first, sources, stopping)
        self._close_children(result, children)
        return result, latest

    def _run_heal(
        self,
        service: str,
        restored: str | None,
        gate_probe: str | None,
        hold: Callable[[float], None],
        first: LinkMatrix,
        sources: set[str] | None = None,
        stopping: Callable[[], bool] | None = None,
    ) -> tuple[HealResult, LinkMatrix, list[str]]:
        """Run one heal (`fault_heal.LinkHeal`); return how it ended, the latest link check that answered (`first`, or
        the heal's last that did) and the journal ids of the container restarts it made."""
        settings, children = self._settings, []

        def restart(label: str, reader: str, budget_s: float) -> dict[str, Any]:
            done = self.restart_container(label, reader, gate_probe, budget_s)
            if done.get("entry") is not None:
                children.append(done["entry"])
            return done

        answered = [first]  # the latest link check that answered: the caller's own, then each of the heal's

        def read() -> LinkMatrix:
            latest = self._matrix(service)  # one that cannot be read raises, and the heal reads it as unreadable
            answered.append(latest)
            return latest

        result = LinkHeal(
            mode=settings.heal,
            driver=self._driver,
            services=self._labels,
            restored=restored,
            bound_s=settings.heal_timeout_s,
            gate_s=settings.gate_timeout_s,
            poll_s=settings.poll_s,
            command_s=command_bound_s(settings),  # `command_timeout_s`, and the relay's wait on HPC
            settle_s=settings.heal_settle_s,
            link_s=heal_link_reserve(settings),
            all_links_s=heal_all_links_reserve(settings),
            container_s=heal_container_reserve(settings),
            matrix=read,
            gate=lambda reader, timeout_s: self._gate(reader, gate_probe, timeout_s),
            transfers=self._active_transfers,
            restart_container=restart,
            hold=hold,
            container_fallback=settings.heal_container_fallback,
            clock=self._clock,
            sleep=self._sleep,
            sources_seen=sources,
            only_stuck=sources is not None,  # the fault gate's heal: nothing is done unless a fetch failed
            stopping=stopping,
        ).run()
        return result, answered[-1], children

    def _close_children(self, result: HealResult, children: list[str]) -> None:
        """A restarted reader is restored only with every pair passing: cleared then, else journalled `clear_failed`
        with the heal's word and left open for recovery."""
        for child in children:
            line = {"event": "cleared", "how": "restored"} if result.healed else {"event": "clear_failed"}
            self.journal.append({**line, "id": child, **({} if result.healed else {"how": result.how})})

    def _active_transfers(self) -> int | None:
        """The driver's count of transfer listings with a job not yet ended; None for a driver that cannot tell."""
        read = getattr(self._driver, "active_transfers", None)
        return None if read is None else read()

    def _matrix(self, service: str) -> LinkMatrix:
        """A fresh link check of every pair, as a matrix, never before a held settle has ended; one that cannot be
        read raises."""
        self._await_quiet()
        read = getattr(self._driver, "link_matrix", None)
        matrix = read(service) if read is not None else None
        if not isinstance(matrix, LinkMatrix):
            raise TypeError("the driver's link check answered no matrix")
        self._said(matrix)
        return matrix

    def _said(self, matrix: LinkMatrix) -> None:
        """Have the settling's record take note of a check whose own record was not written (`LinkMatrix.not_written`):
        its latest such reason, as the optional fact `link_record` (the mutation of `restore_record` is the point)."""
        if matrix.not_written is not None:
            self.restore_record.update(fact(matrix.not_written))

    def _stuck_links(self, service: str) -> tuple[str | None, LinkMatrix | None]:
        """The link check of every ordered pair after a restore, when the driver has one: as a matrix
        (`ComposeDriver.link_matrix`), or as a detail alone (`restored_links`); None when each passed, else why not,
        with the matrix when there is one. A check that raises fails closed, with no matrix to heal from. Never before
        a held settle has ended."""
        self._await_quiet()
        try:
            read = getattr(self._driver, "link_matrix", None)
            matrix = read(service) if read is not None else None
            if isinstance(matrix, LinkMatrix):
                self._said(matrix)
                return (None if matrix.ok else matrix.detail), matrix
            check = getattr(self._driver, "restored_links", None)
            return (None if check is None else check(service)), None
        except Exception as error:  # a check that cannot answer is a failed one, never a pass
            return f"the link check raised {type(error).__name__}", None

    def restart_container(
        self, label: str, service: str, gate_probe: str | None = None, budget_s: float | None = None
    ) -> dict[str, Any]:
        """Restart a reader's container for the heal, as an injection of its own: journalled before the stop with the
        healed injection's gate probe, under the reader's lock, restored and waited for as any injection is, but not
        cleared: the heal clears it once every pair passes. With `budget_s`, every wait and start attempt after the
        first start ends once that many seconds have passed (the start itself is always sent, so the reader is never
        left stopped by the bound), and a restart cut short is unverified, its entry left open for recovery. Return
        `ok`, `how`, `serves_after_s` and its journal `entry` (None when nothing was journalled)."""
        lock = self.journal.try_lock(label)
        if lock is None:
            return {"ok": False, "how": "lock_held", "serves_after_s": None, "entry": None}
        self._deadline = None if budget_s is None else self._clock() + budget_s
        try:
            try:
                seen = self._driver.observe(service)
            except DriverError:
                return {"ok": False, "how": "unreadable", "serves_after_s": None, "entry": None}
            if not seen.running or seen.identity is None:
                return {"ok": False, "how": "not_running", "serves_after_s": None, "entry": None}
            before, entry = digest(seen.identity), uuid.uuid4().hex
            line = {
                "target": label,
                "service": service,
                "identity_sha256": before,
                "holder": holder(),
                "owner": liveness.owner(),  # a heal runs in the gate, outside any attempt: a process alone (O80)
                "heal": True,
            }
            probe = {} if gate_probe is None else {"gate_probe": gate_probe}
            self.journal.append({"event": "injecting", "id": entry, **line, **probe})
            self.journal.append({"event": "stopping", "id": entry})
            unknown = False
            try:
                self._driver.stop(service)
            except DriverError:
                unknown = True
            down = not unknown and self.wait(lambda s: not s.running, service, unreadable_ok=True) is not None
            with contextlib.suppress(OSError):  # the `stopping` line already makes recovery wait for it to show down
                evidence = stop_record(self._driver)  # a stop that answered, confirmed or not, keeps its duration
                self.journal.append({"event": "injected" if down else "stop_unknown", "id": entry, **evidence})
            plain = {"id": entry, "service": service, "identity_sha256": before, "injected": down}
            done, how, _, ready_after_s = self.settle({**plain, "stop_unknown": unknown}, links=False, clear=False)
            if done and how == "never_down":  # it never went down: nothing to restore, so its entry closes now
                self.journal.append({"event": "cleared", "id": entry, "how": how})
                entry = None
        finally:
            self._deadline = None
            lock.close()
        verified = done and how in ("restored", "found_restored")
        return {"ok": verified, "how": how, "serves_after_s": ready_after_s, "entry": entry}
