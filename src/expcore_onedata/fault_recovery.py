# Provenance: new
"""Settling injections: bring a target back with a changed identity, and recover what a run left open (O11).

`Recovery` holds what the fault injector and the platform's health check share and neither needs a target for: the
infrastructure kinds of a fault, the driver protocol, and the steps that leave one journalled injection settled. A
restore counts only when the service runs with an identity other than the one journalled before the injection;
readiness alone is not one, and a service back with the same identity is a failed restore. A service that cannot be
read is started all the same (starting a running service changes nothing), and its restore is then
`restore_unverified`. `recover` settles every open injection whose target's lock it can take (its holder is gone),
reports a lock held while its journalled holder is gone as stuck, and returns the targets live injectors hold.
"""

import contextlib
import hashlib
import math
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from expcore.errors import InfraError, register_infra_kind
from expcore_onedata.compose import DriverError, Observation
from expcore_onedata.fault_journal import InjectionJournal, holder_alive
from expcore_onedata.fault_settings import FaultSettings

PROVIDER_OUTAGE = "provider_outage"
INJECTOR_FAILURE = "injector_failure"
register_infra_kind(PROVIDER_OUTAGE, "a provider of the federation was unavailable")
register_infra_kind(INJECTOR_FAILURE, "the fault injector could not inject, clear or verify its fault", treatable=False)


class FaultDriver(Protocol):
    """What takes a provider's service down and brings it back (`expcore_onedata.compose.ComposeDriver`)."""

    def observe(self, service: str) -> Observation:
        """Read whether the service runs and its identity; raise DriverError when it cannot be read."""
        ...

    def stop(self, service: str) -> None:
        """Stop the service; raise DriverError when the command fails."""
        ...

    def start(self, service: str) -> None:
        """Start the service again; raise DriverError when the command fails."""
        ...


def digest(identity: str) -> str:
    """The SHA-256 of an identity, the only form in which one is journalled or recorded."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def failure(detail: str, target: str | None = None) -> InfraError:
    """The injector's own failure, which no condition may declare as its treatment."""
    return InfraError(INJECTOR_FAILURE, detail, target=target)


class Recovery:
    """Settle journalled injections through a driver, with the bounds of `[faults]`."""

    def __init__(
        self,
        settings: FaultSettings,
        driver: FaultDriver,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings, self._driver, self._clock, self._sleep = settings, driver, clock, sleep
        self.journal = InjectionJournal(settings.journal)
        self.last: Observation | None = None  # the latest reading `wait` took

    def entries(self, target: str | None = None) -> list[dict[str, Any]]:
        """The journal's open injections; raise InfraError of kind `injector_failure` when it cannot be read."""
        try:
            return self.journal.open_entries()
        except ValueError as error:  # the journal's own complaint: a line number, never its content
            raise failure(f"the injection journal cannot be read: {error}", target) from None
        except OSError as error:  # an OS error's text holds the journal's path: only its type is told (O12)
            raise failure(f"the injection journal cannot be read ({type(error).__name__})", target) from None

    def recover(self, on_step: Callable[..., None] | None = None) -> list[str]:
        """Settle every open injection whose target's lock is free; raise InfraError of kind `injector_failure` when one
        cannot be settled or its lock is stuck. Return the targets of those live injectors hold, sorted.

        `on_step("recovered", ok, target=..., how=..., ...)` is told of every injection it settled or failed to.
        """
        step = on_step or (lambda *args, **kwargs: None)
        held: list[str] = []
        for entry in self.entries():
            target = entry["target"]
            lock = self.journal.try_lock(target)
            if lock is None:
                if holder_alive(entry.get("holder")) is False:  # held, yet by no injector the journal knows
                    step("recovered", False, target=target, how="lock_stuck")
                    raise failure(f"the lock of {target} is stuck: its journalled holder is gone", target)
                held.append(target)  # a live injector holds the target down on purpose
                continue
            try:
                done, how, after = self.settle(entry)
            finally:
                lock.close()
            step("recovered", done, target=target, how=how, identity_after_sha256=after)
            if not done:
                raise failure(f"an injection of {target} that an earlier run left open was not cleared", target)
        return sorted(set(held))

    def settle(self, entry: Mapping[str, Any]) -> tuple[bool, str, str | None]:
        """Leave one injection's target up: find it back, find it never went down, or bring it back with a changed
        identity; journal it cleared when it is, and its clear failed when it is not, or when settling raised. Return
        whether it is, how, and the new identity's digest if any."""
        try:
            done, how, after = self._settle(entry)
        except Exception as error:
            self._clear_failed(entry, type(error).__name__)
            raise
        if not done:
            self._clear_failed(entry, how)
        return done, how, after

    def _clear_failed(self, entry: Mapping[str, Any], how: str) -> None:
        with contextlib.suppress(OSError):  # the journal may be what failed: the injection stays open all the same
            self.journal.append({"event": "clear_failed", "id": entry["id"], "how": how})

    def _settle(self, entry: Mapping[str, Any]) -> tuple[bool, str, str | None]:
        before = entry["identity_sha256"]
        try:
            seen: Observation | None = self._driver.observe(entry["service"])
        except DriverError:
            seen = None
        same = seen is not None and seen.identity is not None and digest(seen.identity) == before
        after = None
        if seen is not None and seen.running and seen.identity is not None and not same:
            how, after = "found_restored", digest(seen.identity)
        elif seen is not None and seen.running and same:
            if entry["injected"]:  # it was seen down, so it must have started anew: its identity cannot be trusted
                return False, "identity_unchanged", None
            how = "never_down"  # the stop never took effect
        else:
            restored, after, reason = self._restore(entry["service"], before)
            if restored:
                how = "restored"
            elif reason == "identity_unchanged" and not entry["injected"]:
                how = "never_down"  # unreadable at first, then running as it was: it never went down
            else:
                return False, reason, None
        self.journal.append({"event": "cleared", "id": entry["id"], "how": how})
        return True, how, after

    def _restore(self, service: str, before: str) -> tuple[bool, str | None, str]:
        """Start the service and wait for it to run with an identity whose digest is not `before`, a bounded number of
        times; return whether it did, the new identity's digest, and why not."""
        reason = "not_running"
        for attempt in range(self._settings.clear_attempts):
            if attempt:
                self._sleep(self._settings.poll_s)
            try:
                self._driver.start(service)
            except DriverError:
                reason = "start_failed"
                continue
            try:
                seen = self.wait(
                    lambda s: s.running and s.identity is not None and digest(s.identity) != before, service
                )
            except DriverError:
                reason = "restore_unverified"  # started, but no reading shows the new identity
                continue
            if seen is not None:
                return True, digest(seen.identity or ""), "restored"
            last = self.last
            if last is not None and last.running and last.identity is not None and digest(last.identity) == before:
                return False, None, "identity_unchanged"  # back with the identity it had: not a restore (O11)
        return False, None, reason

    def wait(self, done: Callable[[Observation], bool], service: str) -> Observation | None:
        """Read the service until `done` holds, bounded by the settle timeout and by a count; None when it never did."""
        polls = max(1, math.ceil(self._settings.settle_timeout_s / self._settings.poll_s)) + 1
        started = self._clock()
        self.last = None
        for poll in range(polls):
            if poll:
                self._sleep(self._settings.poll_s)
            self.last = self._driver.observe(service)
            if done(self.last):
                return self.last
            if self._clock() - started >= self._settings.settle_timeout_s:
                break
        return None
