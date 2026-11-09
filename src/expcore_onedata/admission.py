# Provenance: new
"""Admission to a federation with a fault injector: outage runs exclusive with every other run (O11).

A provider an outage takes down is down for every run on the federation, not only for the run that took it down. So
when platform.toml has `[faults]`, every run of the platform holds the federation's reader-writer lock, a `flock` on
`federation.lock` beside the fault journal, from before its sandbox and tool server start until after their teardown:
shared for a run of a baseline condition, exclusive for a run whose condition uses an injector (its injection and its
clear included). An outage run never also takes it shared, so there is no upgrade and no deadlock: the injector
refuses to start under a shared admission, and takes the exclusive one itself when its run holds none. The tool
server refuses to start without an admission (`require_admission`); a sandbox start must do the same.

Waiting for the lock is admission, before the agent's loop starts, so it is outside the agent's time and charges it
nothing, as a lease taken before the run is. Taken inside a tool call instead, it is a `lease_queue` wait (`waits=`).
The wait is a poll of a non-blocking lock with a bound (`admission_timeout_s`), which a cancellation stops. Under a
shared admission the journal must show no unsettled injection, checked while the lock is held, or the run is refused
as `injector_failure`. A held admission is a `HeldLock`: closed by its finalizer when its owner is dropped, and closed
in a forked child.

`FaultGate` is the fault half of the platform's standard health check. Whenever the journal holds an open injection
and the federation can be taken exclusively (no run is on it), it recovers every injection whose holder is gone,
verifying each restore, before it considers the journal: so the first check of a campaign settles what a crashed run
left, and a later one settles an injection whose injector was dropped. It then fails while the federation is held
exclusively (an injection is in progress), while an injection is unsettled (a live injector holds it, or recovery
failed), and while the journal cannot be read, naming targets by their labels only. Recovery restores and so needs only
the configuration's half of the sanction.
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

from expcore.campaign.healthgate import Health
from expcore.config.loading import ConfigError, load_toml
from expcore.errors import InfraError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_journal import HeldLock, InjectionJournal, make_directories
from expcore_onedata.fault_recovery import FaultDriver, Recovery, failure
from expcore_onedata.fault_settings import FaultSettings, load_faults

FEDERATION_LOCK = "federation.lock"
_ADMISSION: ContextVar["Admission | None"] = ContextVar("expcore_onedata_admission", default=None)


class AdmissionRefused(ConfigError):
    """A run of a platform with a fault injector would start, or inject, without the admission its condition needs."""


@dataclass(frozen=True)
class Admission:
    """The federation's lock as one run holds it, shared or exclusive."""

    exclusive: bool
    lock: HeldLock

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
    """The reader-writer lock of one federation, on `federation.lock` in the fault journal's directory."""

    def __init__(self, directory: Path, *, poll_s: float = 0.05) -> None:
        self.directory, self._poll_s = Path(directory), poll_s

    def try_acquire(self, *, exclusive: bool) -> Admission | None:
        """Take the lock without waiting; None when the other mode holds it."""
        make_directories(self.directory)
        descriptor = os.open(self.directory / FEDERATION_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return Admission(exclusive, HeldLock(descriptor))

    def acquire(
        self, *, exclusive: bool, timeout_s: float | None = None, cancel: threading.Event | None = None
    ) -> Admission:
        """Wait for the lock, polling, at most `timeout_s` and until `cancel` is set; raise InfraError of kind
        `injector_failure` when it could not be had."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            admission = self.try_acquire(exclusive=exclusive)
            if admission is not None:
                return admission
            if cancel is not None and cancel.is_set():
                raise failure("the wait for the federation was cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise failure(f"the federation stayed held for {timeout_s:g} s: another run's injection is in progress")
            time.sleep(self._poll_s)

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


def require_admission(config: PlatformConfig) -> None:
    """Refuse to start a run's infrastructure on a platform with `[faults]` unless the run holds the federation."""
    if fault_settings_of(config) is not None and current_admission() is None:
        raise AdmissionRefused(
            f"{config.file}: [faults]: a run's infrastructure starts only under the federation's admission "
            "(FederationLock.admitted, shared for a baseline run, exclusive for an outage run)"
        )


class FaultGate:
    """The fault half of the platform's standard health check (see the module docstring)."""

    def __init__(self, settings: FaultSettings, driver: FaultDriver) -> None:
        self._lock = FederationLock(settings.journal)
        self._recovery = Recovery(settings, driver)
        self._may_recover = settings.sanctioned is True

    def __call__(self) -> Health:
        try:
            if self._recovery.entries() and self._may_recover:
                exclusive = self._lock.try_acquire(exclusive=True)
                if exclusive is not None:  # no run is on the federation: settle what is left
                    try:
                        self._recovery.recover()
                    finally:
                        exclusive.close()
            shared = self._lock.try_acquire(exclusive=False)
            try:
                entries = self._recovery.entries()
            finally:
                if shared is not None:
                    shared.close()
        except InfraError as error:
            return Health(False, error.detail)
        targets = ", ".join(sorted({entry["target"] for entry in entries}))
        if shared is None:
            named = f" of {targets}" if targets else ""
            return Health(False, f"an injection{named} is in progress: the federation is held exclusively")
        if entries:
            return Health(False, f"an injection of {targets} is unsettled: the federation is not in its baseline state")
        return Health(True, "no injection is unsettled")
