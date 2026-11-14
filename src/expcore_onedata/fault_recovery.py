# Provenance: new
"""Settling injections: bring a target back with a changed identity, and recover what a run left open (O11).

`Recovery` holds what the fault injector and the platform's health check share and neither needs a target for: the
injector's own failure (its kind, like `provider_outage`, is registered by `expcore_onedata.sandbox.kinds`, the module
a study lists in `[study] platforms`), the driver protocol, and the steps that leave one journalled injection settled. A
restore counts only when the service runs with an identity other than the one journalled before the injection;
readiness alone is not one, and a service back with the same identity is a failed restore. A service that cannot be
read is started all the same (starting a running service changes nothing), and its restore is then
`restore_unverified`.

A stop whose outcome is unknown (its command timed out, so the CLI was killed while the Docker daemon goes on stopping
the container for up to the provider's `stop_grace_period`) is not judged by a reading taken at once: a `start` of a
container that is still running does nothing, and the old identity read then is the container on its way down, not
one that never went down. The entry says so (`stop_unknown`); `settle` first waits, bounded by the settle timeout, for
the service to show not running, and only then restores it and judges the restore by the changed identity. A service
that never shows down within the bound is `stop_unsettled`, not cleared, never verified. The settle timeout must
therefore exceed the providers' `stop_grace_period` with a margin (180 s against the federation's 120 s, by default).

A process killed while its stop command blocks journals nothing about it, so an open injection that was never seen down
is of unknown outcome whatever its journal says (`InjectionJournal.open_entries`): `stop_unknown` when a `stopping` line
or a `stop_unknown` line says the stop was asked for, `stop_unrecorded` when neither does (the process died before the
command, or the journal predates the `stopping` line). Both wait as above. They differ only when the service never shows
down: after `stopping` that is `stop_unsettled`, as the daemon was asked for a stop that outlasts the bound; after
`injecting` alone, a service that every reading of the bound showed running with the identity it had is `never_down`,
and one that was unreadable, or ran with no identity, is `stop_unsettled`. A restore after either then requires that the
provider serves, as for any restore.

A restore whose identity changed is not yet a provider that serves: the live run of 12 Nov 2026 found the restarted
provider's REST endpoint refusing connections right after the restart and its health check passing 48 to 52 s later.
`settle` therefore waits, once the identity is verified, for the driver to say the service serves
(`FaultDriver.serves`), polling every `poll_s` for at most `ready_timeout_s`, and journals the injection cleared only
then. A service that does not serve within the bound is `not_serving`: unverified, the injection open, its identity
not asked for again (it is already verified), and the next `settle` finds it restored and waits again. The seconds
waited, counted from the verified identity change, are returned (`ready_after_s`) for the injector's snapshot and the
`recovered` reading. A service the stop never took down (`never_down`) kept running and is not waited for. A probe
that refuses the service (`ConfigError`: no provider is declared for it, which no wait changes) is not serving, at once:
the settling ends `serves_unconfigured`, unverified and open, and the failure says why.

`recover` settles every open injection whose target's lock it can take (its holder is gone),
reports a lock held while its journalled holder is gone as stuck, and returns the targets live injectors hold.
"""

import contextlib
import hashlib
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple, Protocol

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.compose import DriverError, Observation
from expcore_onedata.fault_journal import InjectionJournal, holder_alive
from expcore_onedata.fault_settings import FaultSettings
from expcore_onedata.sandbox.kinds import INJECTOR_FAILURE  # the platform layer's one module of kinds registers it


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

    def serves(self, service: str) -> bool:
        """Whether the running service serves now: its API answers an authenticated read and its container, when it has
        a health check, is healthy. False when it does not (yet); raise DriverError only when it cannot be read."""
        ...


log = logging.getLogger(__name__)

_WORD = re.compile(r"[A-Za-z_]{1,40}")  # how a settling ended, as a word: nothing else goes into a message

_WHY = {  # how a settling failed, in words, where the bare word would not say it
    "serves_unconfigured": "its service has no provider declared in [faults.services], so no read can show it serving",
    "links_stuck": (  # the link check tests every ordered pair, so the stuck link need not be the restored provider's
        "it serves, but the link check (O35) found a pair of providers that transferred no probe within its bound: "
        "restart the rtransfer link on the reading provider each failed pair names (rtransfer_config:restart_link() in "
        "its op-worker) or that provider's container alone (docker restart, never a compose down), one at a time, "
        "re-checking every pair after each"
    ),
}


class Settled(NamedTuple):
    """How one injection's settling ended: whether it is cleared, how (or why not), the new identity's digest when one
    was read, and the seconds the restored service took to serve (None when it was not waited for)."""

    done: bool
    how: str
    after: str | None = None
    ready_after_s: float | None = None


def digest(identity: str) -> str:
    """The SHA-256 of an identity, the only form in which one is journalled or recorded."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def failure(detail: str, target: str | None = None) -> InfraError:
    """The injector's own failure, which no condition may declare as its treatment."""
    return InfraError(INJECTOR_FAILURE, detail, target=target)


class RestoreNotVerified(InfraError):
    """A provider the injector took down was not shown restored: its identity did not change to a running one, or it
    did not serve within the bound, so the injection stays open in the journal and the federation is not in its
    baseline state (O11). An `InfraError` of the injector's own, untreatable kind `injector_failure`, naming the
    provider by its label (O12).

    `how` is the word the settling ended with (`not_serving`, `identity_unchanged`, `start_failed`, ...), and
    `identity_changed` and `serving` are what was checked: whether the provider runs with an identity other than the
    one read before the injection, and whether it then served. `still_open` is whether the injection is still open in
    the journal, which makes the health gate and the next start refuse the provider until a later clear verifies it.
    The detail holds these and the label, never a service name, a path or a token.
    """

    def __init__(
        self, target: str, how: str, *, identity_changed: bool, serving: bool, still_open: bool = True
    ) -> None:
        self.how = how if _WORD.fullmatch(how) else "failed"
        self.identity_changed, self.serving, self.still_open = identity_changed, serving, still_open
        checked = (
            f"its identity {'changed' if identity_changed else 'did not change to a running one'} and it "
            f"{'served' if serving else 'did not serve'}"
        )
        left = (
            "the injection stays open in the journal, so the health gate and the next start refuse it until a later "
            "clear, finish or start verifies it"
            if still_open
            else "the injection is closed in the journal"
        )
        detail = f"the restore of {target} is not verified ({self.how}): {checked}; {left}"
        super().__init__(INJECTOR_FAILURE, detail, target=target)


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
        self.unreadable = 0  # the readings of the latest `wait` that failed and counted as no answer
        self.links_detail: str | None = None  # why the latest restore's link check failed (O35), names labels only

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

        `on_step("recovered", ok, target=..., how=..., ...)` is told of every injection it settled or failed to, with
        the seconds the restored service took to serve (`ready_after_s`).
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
                done, how, after, ready_after_s = self.settle(entry)
            finally:
                lock.close()
            step("recovered", done, target=target, how=how, identity_after_sha256=after, ready_after_s=ready_after_s)
            if not done:
                why = self._why(how)
                raise failure(f"an injection of {target} that an earlier run left open was not cleared ({why})", target)
        return sorted(set(held))

    def _why(self, how: str) -> str:
        """How a settling failed, in words. A stuck link adds what the link check found, which names each failed pair's
        reading provider by position (`links_detail` is set by the settling that just ended with `links_stuck`, so it is
        used for that word alone, never for another failure's message)."""
        why = _WHY.get(how, how)
        if how == "links_stuck" and self.links_detail:
            why = f"{why}; the link check found: {self.links_detail}"
        return why

    def settle(self, entry: Mapping[str, Any]) -> Settled:
        """Leave one injection's target up and serving: find it back, find it never went down, or bring it back with a
        changed identity, and, once it was restored, wait for it to serve; journal it cleared when it is, and its clear
        failed when it is not, or when settling raised. Return whether it is, how, the new identity's digest if any and
        the seconds the wait for it to serve took."""
        try:
            settled = self._settle(entry)
        except Exception as error:
            self._clear_failed(entry, type(error).__name__)
            raise
        if not settled.done:
            self._clear_failed(entry, settled.how)
        return settled

    def _clear_failed(self, entry: Mapping[str, Any], how: str) -> None:
        with contextlib.suppress(OSError):  # the journal may be what failed: the injection stays open all the same
            self.journal.append({"event": "clear_failed", "id": entry["id"], "how": how})

    def _settle(self, entry: Mapping[str, Any]) -> Settled:
        before, injected = entry["identity_sha256"], entry["injected"]
        asked, unrecorded = bool(entry.get("stop_unknown")), bool(entry.get("stop_unrecorded"))
        seen: Observation | None
        if asked or unrecorded:  # the daemon may still be stopping it: a reading now proves nothing
            seen, steady = self._await_down(entry["service"], before)
            if seen is not None:
                injected = True  # it was seen not running (or running anew): it did go down
            elif asked or not steady:
                return Settled(False, "stop_unsettled")  # never seen down within the bound: fail closed
            else:  # the stop may never have been asked for, and every reading of the bound showed it up as it was
                seen = self.last
        else:
            try:
                seen = self._driver.observe(entry["service"])
            except DriverError:
                seen = None
        same = seen is not None and seen.identity is not None and digest(seen.identity) == before
        after = None
        if seen is not None and seen.running and seen.identity is not None and not same:
            how, after = "found_restored", digest(seen.identity)
        elif seen is not None and seen.running and same:
            if injected:  # it was seen down, so it must have started anew: its identity cannot be trusted
                return Settled(False, "identity_unchanged")
            how = "never_down"  # the stop never took effect
        else:
            restored, after, reason = self._restore(entry["service"], before)
            if restored:
                how = "restored"
            elif reason == "identity_unchanged" and not injected:
                how = "never_down"  # unreadable at first, then running as it was: it never went down
            else:
                return Settled(False, reason)
        ready_after_s = None
        if how != "never_down":  # it was restarted: its identity changed, which is not yet that it serves
            try:
                ready_after_s = self._await_serving(entry["service"])
            except ConfigError:  # no provider is declared for the service: waiting cannot change that
                return Settled(False, "serves_unconfigured", after)
            if ready_after_s is None:
                return Settled(False, "not_serving", after)  # fail closed: the injection stays open, the identity known
            self.links_detail = self._stuck_links(entry["service"])
            if self.links_detail is not None:  # it serves, and a link of it does not transfer: not restored yet (O35)
                log.warning("a restored provider's link check failed: %s", self.links_detail)
                return Settled(False, "links_stuck", after, ready_after_s)
        self.journal.append({"event": "cleared", "id": entry["id"], "how": how})
        return Settled(True, how, after, ready_after_s)

    def _stuck_links(self, service: str) -> str | None:
        """The link check of the pairs a restored service's provider is in, when the driver has one
        (`ComposeDriver.restored_links`): None when each passed, else why not. A check that raises fails closed."""
        check = getattr(self._driver, "restored_links", None)
        if check is None:
            return None
        try:
            return check(service)
        except Exception as error:  # a check that cannot answer is a failed one, never a pass
            return f"the link check raised {type(error).__name__}"

    def _await_serving(self, service: str) -> float | None:
        """Ask the driver whether the service serves, now and then every `poll_s`, until it does or `ready_timeout_s`
        is over; return the seconds waited (0 when it served at the first reading), None when it never did. A service
        that cannot be read is no answer yet, so it is asked again; the bound is checked between two readings. A
        ConfigError of the probe (no provider is declared for the service) is not caught: no wait changes it."""
        timeout_s, poll_s = self._settings.ready_timeout_s, self._settings.poll_s
        polls = max(1, math.ceil(timeout_s / poll_s)) + 1
        started = self._clock()
        for poll in range(polls):
            if poll:
                self._sleep(poll_s)
            try:
                served = self._driver.serves(service) is True
            except DriverError:
                served = False
            if served:
                return self._clock() - started
            if self._clock() - started >= timeout_s:
                break
        return None

    def _await_down(self, service: str, before: str) -> tuple[Observation | None, bool]:
        """After a stop of unknown outcome: read the service until it shows not running, or running with another
        identity (which only a restart gives), bounded by the settle timeout. A reading that fails is no answer, so it
        is read again. Return the reading that showed it, or None when none did, and whether every reading of the wait
        showed it running with the identity it had: only such a wait says the stop took no effect."""
        steady = True

        def down(seen: Observation) -> bool:
            nonlocal steady
            if not seen.running or (seen.identity is not None and digest(seen.identity) != before):
                return True
            steady = steady and seen.identity is not None
            return False

        seen = self.wait(down, service, unreadable_ok=True)
        return seen, steady and self.unreadable == 0

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

    def wait(
        self, done: Callable[[Observation], bool], service: str, *, unreadable_ok: bool = False
    ) -> Observation | None:
        """Read the service until `done` holds, bounded by the settle timeout and by a count; None when it never did.

        A reading that fails raises DriverError, unless `unreadable_ok`: then it counts as a reading that shows nothing.
        """
        polls = max(1, math.ceil(self._settings.settle_timeout_s / self._settings.poll_s)) + 1
        started = self._clock()
        self.last, self.unreadable = None, 0
        for poll in range(polls):
            if poll:
                self._sleep(self._settings.poll_s)
            try:
                self.last = self._driver.observe(service)
            except DriverError:
                if not unreadable_ok:
                    raise
                self.last, self.unreadable = None, self.unreadable + 1
            else:
                if done(self.last):
                    return self.last
            if self._clock() - started >= self._settings.settle_timeout_s:
                break
        return None
