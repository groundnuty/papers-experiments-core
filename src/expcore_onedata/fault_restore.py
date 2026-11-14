# Provenance: new
"""The link half of settling a restore (O35): the session gate, every ordered pair, and the heal when allowed.

`LinkSettling` is a part of `fault_recovery.Recovery`, kept in a module of its own. Once a restored provider serves,
`_links_settled` polls the session gate (`fault_sessions.await_sessions`) within `gate_timeout_s`, then checks every
ordered pair through the driver (`ComposeDriver.link_matrix`, or the detail alone, `restored_links`), and, only when the
caller holds the federation exclusively and `[faults] heal` allows, runs the heal (`fault_heal.LinkHeal`) once per
injection. `restart_container` is the heal's container restart of a reader, journalled and settled as an injection of
its own. Their readings go into `restore_record`, for the injector's snapshot.
"""

import contextlib
import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from expcore_onedata.compose import DriverError, Observation
from expcore_onedata.fault_heal import LinkHeal, LinkMatrix
from expcore_onedata.fault_journal import InjectionJournal, holder
from expcore_onedata.fault_sessions import await_sessions
from expcore_onedata.fault_settings import FaultSettings

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
    journal: InjectionJournal
    links_detail: str | None
    restore_record: dict[str, Any]
    settle: Callable[..., Any]
    wait: Callable[..., Observation | None]

    def _gate(self, service: str, gate_probe: str | None) -> dict[str, Any]:
        """The session gate for a provider's service, polled within `gate_timeout_s` (`fault_sessions`)."""
        timeout_s, poll_s = self._settings.gate_timeout_s, self._settings.poll_s
        return await_sessions(self._driver, service, gate_probe, timeout_s, poll_s, self._clock, self._sleep)

    def _links_settled(self, entry: Mapping[str, Any], heal: bool) -> str | None:
        """After a restored provider serves: the session gate, then every ordered pair, then the heal when allowed;
        None when the restore may be cleared, else the word it fails with."""
        service, gate_probe, target = entry["service"], entry.get("gate_probe"), entry.get("target")
        gate = self._gate(service, gate_probe)
        self.restore_record["gate"] = gate
        if gate["ok"] is not True:
            return "sessions_not_ready"
        self.links_detail, matrix = self._stuck_links(service)
        if self.links_detail is None:
            return None
        log.warning("a restored provider's link check failed: %s", self.links_detail)
        settings = self._settings
        if not heal or settings.heal == "off" or matrix is None or entry["id"] in self._healed:
            return "links_stuck"
        if not isinstance(target, str) or target not in self._labels:
            return "links_stuck"
        self._healed.add(entry["id"])  # one heal per restore: a later settling of it only checks
        result = LinkHeal(
            mode=settings.heal,
            driver=self._driver,
            services=self._labels,
            restored=target,
            bound_s=settings.heal_timeout_s,
            gate_s=settings.gate_timeout_s,
            ping_s=settings.command_timeout_s,
            container_s=settings.settle_timeout_s + settings.ready_timeout_s,
            matrix=lambda: self._matrix(service),
            gate=lambda reader: self._gate(reader, gate_probe),
            restart_container=self.restart_container,
            clock=self._clock,
            sleep=self._sleep,
        ).run()
        self.restore_record["heal"] = {**result.record, "first": matrix.to_json()}
        if result.healed:
            self.links_detail = None
            return None
        self.links_detail = str(result.record.get("why") or self.links_detail)
        return result.how

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

    def restart_container(self, label: str, service: str) -> dict[str, Any]:
        """Restart a reader's container for the heal, as an injection of its own: journalled before the stop, under the
        reader's lock, and settled (restored with a changed identity and serving) as any injection is, so a process
        that dies midway leaves it open for recovery to bring back. Return `ok`, `how` and `serves_after_s`."""
        lock = self.journal.try_lock(label)
        if lock is None:
            return {"ok": False, "how": "lock_held", "serves_after_s": None}
        try:
            try:
                seen = self._driver.observe(service)
            except DriverError:
                return {"ok": False, "how": "unreadable", "serves_after_s": None}
            if not seen.running or seen.identity is None:
                return {"ok": False, "how": "not_running", "serves_after_s": None}
            before, entry = digest(seen.identity), uuid.uuid4().hex
            line = {"target": label, "service": service, "identity_sha256": before, "holder": holder(), "heal": True}
            self.journal.append({"event": "injecting", "id": entry, **line})
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
            done, how, _, ready_after_s = self.settle({**plain, "stop_unknown": unknown}, links=False)
        finally:
            lock.close()
        return {"ok": done and how in ("restored", "found_restored"), "how": how, "serves_after_s": ready_after_s}
