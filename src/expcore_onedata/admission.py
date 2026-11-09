# Provenance: new
"""Admission to a federation with a fault injector: outage runs exclusive with every other run (O11).

A provider an outage takes down is down for every run on the federation, not only for the run that took it down. So
when platform.toml has `[faults]`, every run of the platform holds the federation's reader-writer lock, a `flock` on
`federation.lock` beside the fault journal, from before its sandbox and tool server start until after their teardown:
shared for a run of a baseline condition, exclusive for a run whose condition uses an injector (its injection and its
clear included). An outage run never also takes it shared, so there is no upgrade and no deadlock: the injector
refuses to start under a shared admission, and takes the exclusive one itself when its run holds none. The tool
server and the sandbox (`SandboxPlatform.launch`) refuse to start without an admission (`require_admission`).

Waiting for the lock is admission, before the agent's loop starts, so it is outside the agent's time and charges it
nothing, as a lease taken before the run is. Taken inside a tool call instead, it is a `lease_queue` wait (`waits=`).
The wait is a poll of a non-blocking lock with a bound (`admission_timeout_s`), which a cancellation stops. Under a
shared admission the journal must show no unsettled injection, checked while the lock is held, or the run is refused
as `injector_failure`. A held admission is a `HeldLock`: closed by its finalizer when its owner is dropped, and closed
in a forked child. An admission names the federation it holds (`Federation`: the lock file's resolved path and the
digest of platform.toml), and `require_admission` and the injector refuse one held for another federation, so a run
of one platform is never admitted by a lock of another.

`flock` is not fair, so a steady stream of shared holders could keep an outage run out for ever. A turnstile prevents
that: every admission, shared or exclusive, first takes `federation.intent` exclusively, then the main lock, and only
then lets the turnstile go. An outage run waiting for the main lock holds the turnstile, so runs that arrive after it
queue behind it, and it gets in once the runs that were on the federation at its arrival have finished.

`FaultGate` is the fault half of the platform's standard health check, and runs around the rest of it. Whenever the
journal holds an open injection and the federation can be taken exclusively (no run is on it), it recovers every
injection whose holder is gone, verifying each restore, before it considers the journal: so the first check of a
campaign settles what a crashed run left, and a later one settles an injection whose injector was dropped. Recovery
restores and so needs only the configuration's half of the sanction. It then takes the federation shared, as a run's
admission does, waiting at most `health_wait_s`, and holds it for the whole check, so no injection starts while the
check runs: under the shared lock it fails for an injection that is still open (recovery failed, or its lock is stuck)
and for a journal it cannot read, naming targets by their labels only, and otherwise answers as the rest of the check
(`inner`: the pre-campaign checks and the space). An outage run in progress holds the federation exclusively and is no
fault of the federation: a check whose wait runs out answers deferred (`Health(..., deferred=True)`), which the core's
gate treats as a wait that charges no run and leaves a finished run's outcome standing. Keep `health_wait_s` below the
campaign's `health_timeout_s`, or the check counts as down before it can be deferred.
"""

import asyncio
import contextlib
import fcntl
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.healthgate import Health, HealthCheck, read_health
from expcore.config.loading import ConfigError, load_toml
from expcore.errors import InfraError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_journal import HeldLock, InjectionJournal, make_directories
from expcore_onedata.fault_recovery import INJECTOR_FAILURE, FaultDriver, Recovery, failure
from expcore_onedata.fault_settings import FaultSettings, load_faults

FEDERATION_LOCK = "federation.lock"
FEDERATION_INTENT = "federation.intent"  # the turnstile every admission passes before the main lock
_ADMISSION: ContextVar["Admission | None"] = ContextVar("expcore_onedata_admission", default=None)


class AdmissionRefused(ConfigError):
    """A run of a platform with a fault injector would start, or inject, without the admission its condition needs."""


class FederationBusy(InfraError):
    """The bounded wait for the federation ran out: another holds it (kind `injector_failure`, as any failed wait)."""


@dataclass(frozen=True)
class Federation:
    """Which federation an admission is for: its lock file's resolved path and the digest of its platform.toml. Two
    configurations that share a lock file but differ are two federations, and so are two with one file but two locks."""

    lock_file: str
    config_sha256: str


def federation_of(settings: FaultSettings) -> Federation:
    """The identity of the federation `[faults]` describes."""
    return Federation(str((settings.journal / FEDERATION_LOCK).resolve()), settings.config_sha256)


@dataclass(frozen=True)
class Admission:
    """The federation's lock as one run holds it, shared or exclusive, and which federation it is."""

    exclusive: bool
    lock: HeldLock
    federation: Federation

    @property
    def held(self) -> bool:
        return self.lock.held

    def close(self) -> None:
        self.lock.close()


def current_admission() -> Admission | None:
    """The admission the running context holds, if it holds one that is still held."""
    admission = _ADMISSION.get()
    return admission if admission is not None and admission.held else None


class FederationLock:
    """The reader-writer lock of one federation, on `federation.lock` in the fault journal's directory of `[faults]`;
    each admission it grants names that federation (`Federation`)."""

    def __init__(self, settings: FaultSettings, *, poll_s: float = 0.05) -> None:
        self.directory, self._poll_s = Path(settings.journal), poll_s
        self.federation = federation_of(settings)

    def _try(self, name: str, mode: int) -> HeldLock | None:
        """Take one of the two lock files in `mode` without waiting; None when another holds it in a conflicting one."""
        make_directories(self.directory)
        descriptor = os.open(self.directory / name, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return HeldLock(descriptor)

    def _wait(self, name: str, mode: int, deadline: float | None, cancel: threading.Event | None) -> HeldLock:
        """Poll for one of the two lock files until the deadline or the cancellation."""
        while True:
            held = self._try(name, mode)
            if held is not None:
                return held
            if cancel is not None and cancel.is_set():
                raise failure("the wait for the federation was cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise FederationBusy(INJECTOR_FAILURE, "the federation stayed held past the bound of the wait")
            time.sleep(self._poll_s)

    def try_acquire(self, *, exclusive: bool) -> Admission | None:
        """Pass the turnstile and take the lock without waiting; None when either is held against it."""
        intent = self._try(FEDERATION_INTENT, fcntl.LOCK_EX)
        if intent is None:
            return None
        try:
            main = self._try(FEDERATION_LOCK, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        finally:
            intent.close()
        return None if main is None else Admission(exclusive, main, self.federation)

    def acquire(
        self, *, exclusive: bool, timeout_s: float | None = None, cancel: threading.Event | None = None
    ) -> Admission:
        """Pass the turnstile, then wait for the lock while holding it, polling, at most `timeout_s` in all and until
        `cancel` is set; raise InfraError of kind `injector_failure` when it could not be had."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        intent = self._wait(FEDERATION_INTENT, fcntl.LOCK_EX, deadline, cancel)
        try:
            main = self._wait(FEDERATION_LOCK, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH, deadline, cancel)
        finally:
            intent.close()  # whoever waits behind it may now queue for the main lock
        return Admission(exclusive, main, self.federation)

    @contextlib.contextmanager
    def held(self, *, exclusive: bool, timeout_s: float | None = None) -> Iterator[Admission]:
        """Hold the lock for the body of a `with`, as the running context's admission (the synchronous form)."""
        if current_admission() is not None:
            raise AdmissionRefused("this run already holds the federation's admission")
        admission = self.acquire(exclusive=exclusive, timeout_s=timeout_s)
        token = _ADMISSION.set(admission)
        try:
            if not exclusive:
                self._require_settled()
            yield admission
        finally:
            _ADMISSION.reset(token)
            admission.close()

    @contextlib.asynccontextmanager
    async def admitted(
        self, *, exclusive: bool, waits: Any = None, timeout_s: float | None = None
    ) -> AsyncIterator[Admission]:
        """Hold the lock for the body of an `async with`, as the running task's admission.

        The wait runs off the event loop and stops on cancellation; with `waits` (a timer or a call context) it is
        inside `waits.infrastructure_wait("lease_queue")`. Shared, the journal must hold no unsettled injection.
        """
        if current_admission() is not None:
            raise AdmissionRefused("this run already holds the federation's admission")
        cancel = threading.Event()
        waiting = asyncio.ensure_future(
            asyncio.to_thread(self.acquire, exclusive=exclusive, timeout_s=timeout_s, cancel=cancel)
        )
        try:
            if waits is None:
                admission = await asyncio.shield(waiting)
            else:
                async with waits.infrastructure_wait("lease_queue"):
                    admission = await asyncio.shield(waiting)
        except asyncio.CancelledError:
            cancel.set()  # the thread stops polling; an admission it took all the same is let go at once
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({waiting}, timeout=10 * self._poll_s + 1)
            if waiting.done() and not waiting.cancelled() and waiting.exception() is None:
                waiting.result().close()
            raise
        token = _ADMISSION.set(admission)
        try:
            if not exclusive:
                self._require_settled()
            yield admission
        finally:
            _ADMISSION.reset(token)
            admission.close()

    def _require_settled(self) -> None:
        """Under the lock: refuse the run while the journal holds an unsettled injection, or cannot be read."""
        try:
            entries = InjectionJournal(self.directory).open_entries()
        except (OSError, ValueError) as error:
            raise failure(f"the injection journal cannot be read ({type(error).__name__})") from None
        if entries:
            targets = ", ".join(sorted({entry["target"] for entry in entries}))
            raise failure(f"an injection of {targets} is unsettled, so no run may start on the federation")


def fault_settings_of(config: PlatformConfig) -> FaultSettings | None:
    """`[faults]` of the configuration's platform.toml, validated; None when it has no such table."""
    if "faults" not in load_toml(config.file):
        return None
    return load_faults(config.file, config)


def require_same_federation(admission: Admission, federation: Federation) -> None:
    """Refuse an admission held for another federation than `federation`; the message names neither (O12)."""
    if admission.federation != federation:
        raise AdmissionRefused(
            "the admission held is for another federation (another lock file or another platform.toml) than the one "
            "this run acts on"
        )


def require_admission(config: PlatformConfig) -> None:
    """Refuse to start a run's infrastructure on a platform with `[faults]` unless the run holds that federation."""
    settings = fault_settings_of(config)
    if settings is None:
        return
    admission = current_admission()
    if admission is None:
        raise AdmissionRefused(
            f"{config.file}: [faults]: a run's infrastructure starts only under the federation's admission "
            "(FederationLock.admitted, shared for a baseline run, exclusive for an outage run)"
        )
    require_same_federation(admission, federation_of(settings))


class FaultGate:
    """The fault half of the platform's standard health check, around the rest of it (see the module docstring)."""

    def __init__(self, settings: FaultSettings, driver: FaultDriver, inner: HealthCheck | None = None) -> None:
        self._lock = FederationLock(settings)
        self._recovery = Recovery(settings, driver)
        self._may_recover = settings.sanctioned is True
        self._wait_s = settings.health_wait_s
        self._inner = inner

    def __call__(self) -> Health:
        try:
            if self._recovery.entries() and self._may_recover:
                exclusive = self._lock.try_acquire(exclusive=True)
                if exclusive is not None:  # no run is on the federation: settle what is left
                    try:
                        self._recovery.recover()
                    finally:
                        exclusive.close()
            try:
                shared = self._lock.acquire(exclusive=False, timeout_s=self._wait_s)
            except FederationBusy:  # an outage run holds it: a wait, which charges no run, never a failure
                return Health(False, "an outage run holds the federation: the check waits for it", deferred=True)
            try:  # held shared for the whole check: no injection starts until it has answered
                entries = self._recovery.entries()
                if entries:
                    targets = ", ".join(sorted({entry["target"] for entry in entries}))
                    return Health(
                        False, f"an injection of {targets} is unsettled: the federation is not in its baseline state"
                    )
                return (
                    read_health(self._inner) if self._inner is not None else Health(True, "no injection is unsettled")
                )
            finally:
                shared.close()
        except InfraError as error:
            return Health(False, error.detail)
