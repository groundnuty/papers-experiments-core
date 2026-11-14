# Provenance: new
"""The link half of settling a restore (O35): the session gate, every ordered pair, and the heal when allowed.

`LinkSettling` is a part of `fault_recovery.Recovery`, kept in a module of its own. Once a restored provider serves,
`_links_settled` polls the session gate (`fault_sessions.await_sessions`) within `gate_timeout_s`, then checks every
ordered pair through the driver (`ComposeDriver.link_matrix`, or the detail alone, `restored_links`), and, only when the
caller holds the federation exclusively and `[faults] heal` allows, runs the heal (`fault_heal.LinkHeal`).

**Once per injection, durably.** Before its first action the heal journals that it heals the injection: a `clear_failed`
line (`links_stuck`, which the restore is at that moment) marked `healing`, which `InjectionJournal.open_entries` reads
back as `healed`. Neither a later clear by the same injector nor another process's recovery of the same injection heals
it again, so no provider gets a second `restart_link` or container restart in its restore. A journal that cannot take
the line heals nothing. An injection that is itself a heal's container restart (`heal` on its line) is never healed.

**A reader's container restart** (`restart_container`) is an injection of its own: journalled before its stop with the
gate probe of the injection it heals, under the reader's lock, restored and waited for as any injection is, and
**cleared only when the heal ends with every pair passing**; otherwise it is journalled `clear_failed` with the heal's
word and stays open, and recovery (which settles such entries first, `Recovery.recover`) brings it back and checks it.
The gate's and the heal's readings go into `restore_record`, for the injector's snapshot.
"""

import contextlib
import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from expcore_onedata.compose import DriverError, Observation
from expcore_onedata.fault_heal import LinkHeal
from expcore_onedata.fault_journal import InjectionJournal, holder
from expcore_onedata.fault_matrix import LinkMatrix
from expcore_onedata.fault_sessions import await_sessions
from expcore_onedata.fault_settings import (
    FaultSettings,
    heal_all_links_reserve,
    heal_container_reserve,
    heal_link_reserve,
)

log = logging.getLogger("expcore_onedata.fault_recovery")


def digest(identity: str) -> str:
    """The SHA-256 of an identity, the only form in which one is journalled or recorded."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class LinkSettling:
    """The link half of `Recovery` (see the module docstring); `Recovery` provides every attribute it reads."""

    _settings: FaultSettings
    _driver: Any
    _clock: Callable[[], float]
    _sleep: Callable[[float], None]
    _labels: dict[str, str]
    _healed: set[str]
    _deadline: float | None
    journal: InjectionJournal
    links_detail: str | None
    restore_record: dict[str, Any]
    settle: Callable[..., Any]
    wait: Callable[..., Observation | None]

    def _gate(self, service: str, gate_probe: str | None, timeout_s: float | None = None) -> dict[str, Any]:
        """The session gate for a provider's service, polled within `gate_timeout_s`, or `timeout_s` when shorter."""
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
        self._healed.add(entry["id"])  # one heal per injection: a later settling of it only checks
        try:  # durable before any action, so no process heals this injection again
            self.journal.append({"event": "clear_failed", "id": entry["id"], "how": "links_stuck", "healing": True})
        except OSError:
            return "links_stuck"
        how = self._heal(entry, service, gate_probe, matrix)
        return None if how == "healed" else how

    def _heal(self, entry: Mapping[str, Any], service: str, gate_probe: str | None, matrix: LinkMatrix) -> str:
        """Run the heal, record it, and clear or fail the container restarts it made; return how it ended."""
        settings, children = self._settings, []

        def restart(label: str, reader: str, budget_s: float) -> dict[str, Any]:
            done = self.restart_container(label, reader, gate_probe, budget_s)
            if done.get("entry") is not None:
                children.append(done["entry"])
            return done

        result = LinkHeal(
            mode=settings.heal,
            driver=self._driver,
            services=self._labels,
            restored=str(entry["target"]),
            bound_s=settings.heal_timeout_s,
            gate_s=settings.gate_timeout_s,
            poll_s=settings.poll_s,
            command_s=settings.command_timeout_s,
            settle_s=settings.heal_settle_s,
            link_s=heal_link_reserve(settings),
            all_links_s=heal_all_links_reserve(settings),
            container_s=heal_container_reserve(settings),
            matrix=lambda: self._matrix(service),
            gate=lambda reader, timeout_s: self._gate(reader, gate_probe, timeout_s),
            transfers=self._active_transfers,
            restart_container=restart,
            clock=self._clock,
            sleep=self._sleep,
        ).run()
        self.restore_record["heal"] = {**result.record, "first": matrix.to_json()}
        for child in children:  # a restarted reader is restored only with every pair passing
            line = {"event": "cleared", "how": "restored"} if result.healed else {"event": "clear_failed"}
            self.journal.append({**line, "id": child, **({} if result.healed else {"how": result.how})})
        self.links_detail = None if result.healed else str(result.record.get("why") or self.links_detail)
        return result.how

    def _active_transfers(self) -> int | None:
        """The driver's count of transfer listings with a job not yet ended; None for a driver that cannot tell."""
        read = getattr(self._driver, "active_transfers", None)
        return None if read is None else read()

    def _matrix(self, service: str) -> LinkMatrix:
        """A fresh link check of every pair, as a matrix; one that cannot be read raises."""
        read = getattr(self._driver, "link_matrix", None)
        matrix = read(service) if read is not None else None
        if not isinstance(matrix, LinkMatrix):
            raise TypeError("the driver's link check answered no matrix")
        return matrix

    def _stuck_links(self, service: str) -> tuple[str | None, LinkMatrix | None]:
        """The link check of every ordered pair after a restore, when the driver has one: as a matrix
        (`ComposeDriver.link_matrix`), or as a detail alone (`restored_links`); None when each passed, else why not,
        with the matrix when there is one. A check that raises fails closed, with no matrix to heal from."""
        try:
            read = getattr(self._driver, "link_matrix", None)
            matrix = read(service) if read is not None else None
            if isinstance(matrix, LinkMatrix):
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
            line = {"target": label, "service": service, "identity_sha256": before, "holder": holder(), "heal": True}
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
                self.journal.append({"event": "injected" if down else "stop_unknown", "id": entry})
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
