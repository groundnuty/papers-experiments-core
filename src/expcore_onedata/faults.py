# Provenance:
#   source: icsoc-2026-code-agent @ c1e76c2e1b7cea56a88ad70efca2d5048aa9a4ea, harness/provider_restart.py, blob 9c1013c507a9bfae7e318ba6c69bc80f68c6097b
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsoc-2026-code-agent @ c1e76c2, harness/sweep_driver.py:130-131 (the fault is cleared in finally)
#   merged: icsoc-2026-code-agent @ c1e76c2, harness/netem.py:85-86,337-348,362-392,401-416 (verified inject, bounded verified clear, fail-closed)
#   changes: see git history of this file from its import commit
"""Taking one provider offline for a run and bringing it back, as an instrument (O10, O11, O12).

A condition whose manipulation is a provider outage declares the kind `provider_outage` with the provider's position
label as its target, in study.toml: `infrastructure_is_treatment = [{ kind = "provider_outage", target = "providers[1]"
}]`. The run's `FaultInjector`, built for that label, takes the provider down in `start`, before the agent's loop starts,
and brings it back in `finish`, after the loop ends; neither happens in the agent's timed window, so neither is a wait
kind. Its snapshot (namespace `fault_injector`, `expcore.records.injections`) records the injection, and the core scores
the attempt as treatment only if the failure that ended it names the same label (`provider_outage` builds such a
failure) and the snapshot shows that label injected in this attempt. Another provider's failure, the agent's own
included, is set aside.

**Sanction (O10).** Taking a provider down is destructive, so nothing here acts on two halves of a sanction short of
both: `sanctioned = true` in `[faults]` of platform.toml, and `sanctioned=True` passed by the code that builds the
injector. Anything else raises `Unsanctioned` when the injector is built, and again before it recovers or injects.

**Inject (O11).** The provider's identity is read first, the injection is journalled durably before the stop command
runs (`expcore_onedata.fault_journal`), and the provider must then be seen down within a bounded wait and the injection
journalled as done. Whatever breaks after the stop command (the wait, the journal, the driver, anything else) brings
the provider back at once and fails `start` as `injector_failure`, a kind no condition may declare, so the attempt is
set aside and run again; the reading shows the failed injection and the clear that followed.

**Clear (O11).** `clear` brings the provider back and counts a restore only when the provider runs with an identity
other than the one read before the injection (`ComposeDriver`: the container's id and start time). Readiness alone is
not a restore, and a provider back with the same identity is a failed restore. The start is tried a bounded number of
times; a service that cannot be read is started all the same, and its restore is `restore_unverified`. `clear`
never raises: a failed restore is in the snapshot (`verified: false`), stays open in the journal and stays pending,
with the target's lock held, so a later `clear`, `finish` or `start` tries again. `finish` calls it, `session` calls it
in a `finally` around both `start` and the agent's run, and `start` calls it before failing.

**Recovery (O11).** Before it injects, `start` (like `recover`) clears and verifies every injection the journal holds
open whose target no live injector holds: each injector holds an exclusive lock on its target's lock file for as long
as it may have the target down, the kernel drops the lock when its holder dies, and a forked child never keeps it.
A crash between inject and clear therefore leaves no provider down after the next start, and an open injection that
cannot be cleared and verified fails that start as `injector_failure`, so a campaign never runs on with a provider it
left down. The journal names each injection's holder (pid and boot id): a lock held while that holder is gone is
stuck, and fails the start the same way instead of being skipped as a live run's.

**Baseline (O11).** While an injection proceeds or is unsettled the federation is not in its baseline state, so no
other run may be on it. An outage run holds the federation's lock exclusively for its whole run, inject and clear
included (`expcore_onedata.admission`): the injector uses its run's exclusive admission, takes one itself when the run
holds none, refuses a shared one, and keeps it until the restore is verified. Every other run holds it shared, so the
two never overlap; a turnstile in front of the lock keeps a stream of shared holders from starving an outage run. An
injector also refuses to start, as `injector_failure`, while the journal holds an injection recovery could not
settle, and the platform's standard health check (`PlatformHealth`), whenever platform.toml has a `[faults]` table,
recovers what a crashed run left, holds the federation shared for the whole check, is deferred (charging no run)
while an outage run holds it, and fails only for an injection that stays open.

**Readings.** Every step (`sanctioned`, `recovered`, `injected`, `cleared`, `verified`) is a reading in the snapshot,
labelled with the seconds on the injector's clock since `start`, so replay serves it and no scorer asks the platform.

**Use.** The study's plugin passes the platform's run admission to the campaign (`admission=run_admission(config)`),
which admits each run to the federation before anything starts (exclusive when its condition declares
`provider_outage`) and keeps it until after the post-run check. The executor, already admitted, starts the sandbox and
the tool server, builds the injector for the target its condition declares and runs the agent inside `running`:

    injector = FaultInjector(config, settings, request.treatment_targets[PROVIDER_OUTAGE], sanctioned=True,
                             driver=compose_driver(settings))
    async with injector.running(dataclasses.asdict(request.identity)):
        record = await record_run(request, ...)
    record = await asyncio.to_thread(finish_instruments, record, [injector])

`running` starts the injector in a worker thread and keeps that thread's future: a cancellation cannot stop the thread,
so on cancellation it waits for it (shielded, bounded), clears in `finally` and raises the cancellation again. Never
await `asyncio.to_thread(injector.start, ...)` bare: cancelled, it leaves the thread stopping the provider while the
code meant to clear it never runs. `start` and `clear` hold the injector's own lock, so a clear waits for a start still
running in its thread. `finish_instruments` puts the snapshot into the record and derives its treatment flag again from it.

**Names (O12).** Outside the driver and the journal the provider is known only by its label: in the snapshot, in every
message and in every failure. Identities are recorded as SHA-256 digests. The settings are `[faults]` of
platform.toml (`expcore_onedata.fault_settings`).
"""

import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore.records.injections import FAULT_NAMESPACE
from expcore_onedata.admission import (
    INJECTOR,
    Admission,
    AdmissionRefused,
    FederationLock,
    current_admission,
    require_same_federation,
)
from expcore_onedata.compose import DriverError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_journal import HeldLock, SettledInjections, holder
from expcore_onedata.fault_recovery import INJECTOR_FAILURE, PROVIDER_OUTAGE, FaultDriver, Recovery, digest
from expcore_onedata.fault_settings import FaultSettings, compose_driver, load_faults

__all__ = [
    "INJECTOR_FAILURE",
    "PROVIDER_OUTAGE",
    "FaultDriver",
    "FaultInjector",
    "FaultRefused",
    "FaultSettings",
    "SettledInjections",
    "Unsanctioned",
    "compose_driver",
    "load_faults",
    "provider_outage",
]


class Unsanctioned(ConfigError):
    """The injector was built or run without both halves of the sanction (O10)."""


class FaultRefused(ConfigError):
    """The injector was asked to act on a target platform.toml does not let it take down, or for no run."""


class _NotSeenDown(Exception):
    """The stop command returned, but the service was never seen down within the bounded wait."""


def provider_outage(config: PlatformConfig, name: str, detail: str) -> InfraError:
    """The failure of a provider the platform attributes to it, naming it by its label (`providers[i]`), never by name."""
    return InfraError(PROVIDER_OUTAGE, detail, target=config.provider_label(name))


class FaultInjector:
    """The instrument that takes one provider down for a run and brings it back (see the module docstring)."""

    namespace = FAULT_NAMESPACE

    def __init__(
        self,
        config: PlatformConfig,
        settings: FaultSettings,
        target: str,
        *,
        sanctioned: bool = False,
        driver: FaultDriver,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Check the sanction and the target; act on nothing yet. `target` is a provider's label (`providers[1]`)."""
        self._settings, self._sanctioned = settings, sanctioned
        self._require_sanction()
        names = {config.provider_label(name): name for name in config.providers}
        if target not in names:  # what it is may be a configured name, so it is never echoed (O12)
            raise FaultRefused(f"{settings.file}: [faults]: the target is not the label of a provider in [providers]")
        service = settings.services.get(names[target])
        if service is None:
            raise FaultRefused(f"{settings.file}: [faults.services]: no service is declared for {target}")
        self.target, self._service, self._driver = target, service, driver
        self._clock, self._sleep = clock, sleep
        self._recovery = Recovery(settings, driver, clock=clock, sleep=sleep)
        self._journal = self._recovery.journal
        self._lock: HeldLock | None = None
        self._federation = FederationLock(settings)
        self._admission: Admission | None = None  # the exclusive admission it took itself, when its run held none
        self._cancel = threading.Event()  # set when `running` is cancelled: an admission still awaited is given up
        self._mutex = threading.RLock()  # a start and a clear never run at once, in whichever threads
        rounds = settings.clear_attempts + 2
        self.cancel_wait_s = (
            settings.settle_timeout_s + settings.command_timeout_s
        ) * rounds  # about a start's longest
        self._pending: tuple[str, str] | None = None  # the open injection's journal id and identity digest
        self._run: dict[str, Any] | None = None
        self._steps: list[dict[str, Any]] = []
        self._injection: dict[str, Any] | None = None
        self._went_down = False  # the target was seen down during the open injection
        self._origin = clock()

    # --- the sanction and the readings ------------------------------------------------------------------------

    def _require_sanction(self) -> None:
        """Refuse unless platform.toml and the caller both sanction the injector, each with exactly True (O10)."""
        if self._settings.sanctioned is not True or self._sanctioned is not True:
            raise Unsanctioned(
                f"{self._settings.file}: [faults]: a fault injector takes a provider down and needs both "
                "'sanctioned = true' in platform.toml and sanctioned=True from its caller"
            )

    def _step(self, step: str, ok: bool, target: str | None = None, **extra: Any) -> None:
        self._steps.append(
            {"step": step, "at_s": self._clock() - self._origin, "target": target or self.target, "ok": ok, **extra}
        )

    def _fail(self, detail: str) -> InfraError:
        return InfraError(INJECTOR_FAILURE, detail, target=self.target)

    # --- the instrument -----------------------------------------------------------------------------------------

    def start(self, identity: Mapping[str, Any]) -> None:
        """Recover what an earlier run left open, then take the target down and see it down; raise InfraError of kind
        `injector_failure` when either fails, or while an injection stays unsettled. Every path after the stop command
        brings the target back first (O11).

        `identity` is the run's identity as a plain mapping with at least its `run_key` and `attempt`, which the snapshot
        names so the treatment rule counts the injection for this attempt only.
        """
        with self._mutex:
            try:
                self._start(identity)
            except BaseException:
                if self._pending is None:  # nothing is down: the federation goes back to the other runs
                    self._release_admission()
                raise

    def _start(self, identity: Mapping[str, Any]) -> None:
        self._require_sanction()
        if self._pending is not None and not self._clear():  # an earlier start of this injector was never cleared
            raise self._fail(f"an earlier injection of {self.target} by this injector is still not cleared")
        run_key, attempt = identity.get("run_key"), identity.get("attempt")
        if not isinstance(run_key, str) or isinstance(attempt, bool) or not isinstance(attempt, int):
            raise FaultRefused("a fault injector is started with the run's identity: its run_key and attempt")
        self._origin, self._steps, self._injection = self._clock(), [], None
        self._run = {"run_key": run_key, "attempt": attempt}
        self._admit()
        self._step("sanctioned", True)
        unsettled = self._recover()
        if unsettled:  # held down by a live injector: the federation is not in its baseline state
            raise self._fail(f"an injection of {', '.join(unsettled)} is unsettled, so no other run may start")
        self._lock = self._journal.try_lock(self.target)
        if self._lock is None:
            raise self._fail(f"{self.target} is held by another run's fault injector")
        digest, entry = self._journal_injection()
        self._pending, self._went_down = (entry, digest), False
        self._injection = {"target": self.target, "injected": False, "cleared": False, "verified": False}
        try:
            self._driver.stop(self._service)
            self._went_down = self._recovery.wait(lambda seen: not seen.running, self._service) is not None
            if not self._went_down:
                raise _NotSeenDown()
            self._journal.append({"event": "injected", "id": entry})
        except BaseException as error:  # whatever broke after the stop, the target is brought back before it is raised
            why = type(error).__name__
            self._step("injected", False, identity_before_sha256=digest, seen_down=self._went_down, error=why)
            self._clear()
            if not isinstance(error, Exception):
                raise
            raise self._fail(f"{self.target} could not be taken down, seen down and journalled ({why})") from None
        self._injection["injected"] = True
        self._step("injected", True, identity_before_sha256=digest)

    def _admit(self) -> None:
        """Hold the federation exclusively for the whole injection: the run's own admission, which must then be
        exclusive, or one taken here and kept until the restore is verified."""
        held = current_admission()
        if held is not None:
            require_same_federation(held, self._federation.federation)  # another federation's admission is none
            if not held.exclusive:
                raise AdmissionRefused(
                    "an outage run holds the federation exclusively for its whole run and never shared: no upgrade"
                )
            held.mark(self.target)  # the run's holder record names the target it takes down
        elif self._admission is None or not self._admission.held:
            timeout_s, cancel = self._settings.admission_timeout_s, self._cancel
            try:
                self._admission = self._federation.acquire(
                    exclusive=True, timeout_s=timeout_s, cancel=cancel, purpose=INJECTOR, target=self.target
                )
            except InfraError as error:
                raise self._fail(error.detail) from None

    def _release_admission(self) -> None:
        if self._admission is not None:
            self._admission.close()
            self._admission = None

    def _journal_injection(self) -> tuple[str, str]:
        """Read the target's identity and journal the injection durably, before anything stops it; on any failure
        release the lock and raise InfraError of kind `injector_failure`. Return the identity's digest and the line's id."""
        try:
            before = self._driver.observe(self._service)
            if not before.running or before.identity is None:
                raise self._fail(f"{self.target} is not running before the injection, so no restore could be confirmed")
            before_sha256, entry = digest(before.identity), uuid.uuid4().hex
            line = {
                "target": self.target,
                "service": self._service,
                "identity_sha256": before_sha256,
                "holder": holder(),
            }
            self._journal.append({"event": "injecting", "id": entry, **line})
        except InfraError:
            self._unlock()
            raise
        except DriverError as error:  # its message names the step and the exit code, never the service
            self._unlock()
            raise self._fail(f"{self.target} could not be read before the injection: {error}") from None
        except Exception as error:  # nothing was stopped: the lock goes, and only the error's type is told (O12)
            self._unlock()
            raise self._fail(f"{self.target} could not be read and journalled ({type(error).__name__})") from None
        return before_sha256, entry

    @contextlib.asynccontextmanager
    async def running(
        self, identity: Mapping[str, Any], *, cancel_wait_s: float | None = None
    ) -> AsyncIterator["FaultInjector"]:
        """Start in a worker thread for the body of an `async with`, and clear in `finally` (O11).

        The start's future is kept: when the body's task is cancelled while the start runs, the thread runs on, so the
        cancellation waits for it, shielded and at most `cancel_wait_s` (by default `cancel_wait_s` of the injector,
        about the longest a start takes), then the clear runs and the cancellation is raised again. The clear, in its
        own thread, waits for a start that is still running, through the injector's lock.
        """
        bound = self.cancel_wait_s if cancel_wait_s is None else cancel_wait_s
        self._cancel = threading.Event()
        starting = asyncio.ensure_future(asyncio.to_thread(self.start, identity))
        try:
            try:
                await asyncio.shield(starting)
            except asyncio.CancelledError:
                self._cancel.set()  # a start still waiting for the federation gives up instead of injecting
                await _outlast(starting, bound)
                raise
            yield self
        finally:
            clearing = asyncio.ensure_future(asyncio.to_thread(self.clear))
            try:
                await asyncio.shield(clearing)
            except asyncio.CancelledError:
                await _outlast(clearing, bound)
                raise

    def finish(self) -> dict[str, Any]:
        """Bring the target back and verify it (`clear`), then return the snapshot of every step."""
        self.clear()
        return self.snapshot()

    @contextlib.contextmanager
    def session(self, identity: Mapping[str, Any]) -> Iterator["FaultInjector"]:
        """Inject for the body of a `with` and clear in `finally`, whatever the start or the body raises (O11)."""
        try:
            self.start(identity)
            yield self
        finally:
            self.clear()

    def snapshot(self) -> dict[str, Any]:
        """The reading the record keeps: the run, the target, the injection and every step; `ok` only when the target
        was injected and its restore verified."""
        injection = None if self._injection is None else dict(self._injection)
        return {
            "ok": bool(injection and injection["injected"] and injection["verified"]),
            **(self._run or {"run_key": None, "attempt": None}),
            "kind": PROVIDER_OUTAGE,
            "target": self.target,
            "injections": [] if injection is None else [injection],
            "steps": [dict(step) for step in self._steps],
        }

    # --- clearing and recovery --------------------------------------------------------------------------------

    def clear(self) -> bool:
        """Bring the target back and verify its identity changed; record the outcome and never raise for it.

        Returns True when this injector leaves nothing down. Until a restore is verified the injection stays pending,
        with the target's lock held, so a later `clear`, `finish` or `start` tries again; a verified one makes calling
        it again harmless. A target the injection never took down is found running with its old identity. An injector
        discarded with its restore still unverified lets the lock go when it is collected, so recovery can settle it.
        """
        with self._mutex:
            return self._clear()

    def _clear(self) -> bool:
        if self._pending is None:
            return True
        entry, digest = self._pending
        injection = self._injection if self._injection is not None else {"target": self.target, "injected": False}
        try:
            done, how, after = self._recovery.settle(
                {"id": entry, "service": self._service, "identity_sha256": digest, "injected": self._went_down}
            )
        except Exception as error:  # never lose the reading: a clear that failed stays pending and open
            done, how, after = False, type(error).__name__, None
        verified = done and how in ("restored", "found_restored")
        self._injection = {**injection, "cleared": done, "verified": verified}
        self._step("cleared", done, how=how)
        self._step("verified", verified, identity_changed=after is not None, identity_after_sha256=after)
        if done:
            self._pending = None
            self._unlock()
            self._release_admission()
        return done

    def recover(self) -> list[str]:
        """Clear and verify every open injection of the journal that no live injector holds; raise InfraError of kind
        `injector_failure` when one cannot be. Return the targets of the injections left open, which live injectors
        hold."""
        with self._mutex:
            return self._recover()

    def _recover(self) -> list[str]:
        self._require_sanction()
        return self._recovery.recover(self._step)

    def _unlock(self) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None


async def _outlast(future: "asyncio.Future[Any]", bound: float) -> None:
    """Wait, shielded and at most `bound` seconds, for a worker thread's future that a cancellation left running."""
    with contextlib.suppress(asyncio.CancelledError):  # cancelled again: the thread still runs, the bound still holds
        await asyncio.wait({future}, timeout=bound)
    if future.done() and not future.cancelled():
        future.exception()  # read here: what the thread raised is the clear's to answer, never an unread error
