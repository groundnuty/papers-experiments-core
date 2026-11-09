# Provenance: new
"""Admission to a federation with a fault injector: outage runs exclusive with every other run (O11).

A provider an outage takes down is down for every run on the federation, not only for the run that took it down. So when
platform.toml has `[faults]`, every run of the platform holds the federation's reader-writer lock, a `flock` on
`federation.lock` beside the fault journal, from before its sandbox and tool server start until after its post-run
check: shared for a run of a baseline condition, exclusive for a run whose condition uses an injector (its injection and
its clear included). The campaign takes it through the platform's run admission (`run_admission`, the study plugin's
`admission`), around the whole attempt, so no outage run can take the federation between a run's teardown and the
post-run check that judges it. An outage run never also takes it shared, so there is no upgrade and no deadlock: the
injector refuses to start under a shared admission, and takes the exclusive one itself when its run holds none. The tool
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

The fault half of the platform's health check, which reads the holder record this module writes, is
`expcore_onedata.fault_gate.FaultGate`. Whoever takes the federation exclusively (`OUTAGE_RUN`, `INJECTOR` or the
gate's `RECOVERY`) first writes a holder record beside the lock, whole and fsynced (`fault_journal.HOLDER`): its pid and
boot id, its purpose, the target's label and the time it took the lock; it removes the record before it lets the lock
go, and a shared admission clears any record left behind, since no exclusive holder can then exist.
"""

import asyncio
import contextlib
import fcntl
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expcore.campaign.attempt import RunAdmission
from expcore.config.loading import ConfigError, load_toml
from expcore.errors import InfraError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_journal import HeldLock, InjectionJournal, holder, make_directories
from expcore_onedata.fault_recovery import INJECTOR_FAILURE, PROVIDER_OUTAGE, failure
from expcore_onedata.fault_settings import FaultSettings, load_faults

FEDERATION_LOCK = "federation.lock"
FEDERATION_INTENT = "federation.intent"  # the turnstile every admission passes before the main lock
OUTAGE_RUN, INJECTOR, RECOVERY = "outage_run", "injector", "recovery"  # why the federation is held exclusively
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


class _Shares:
    """Who still needs an admission: its owner, and every worker handed a share that may outlive the owner's wait."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.count, self.owner_left = 1, False


@dataclass(frozen=True)
class Admission:
    """The federation's lock as one run holds it, shared or exclusive, and which federation it is; held exclusively,
    the holder record it wrote beside the lock (`record`), which it removes before it lets the lock go.

    The owner's `close` gives up its share; the lock goes when the last share is given up, so a worker the owner
    handed a share (`retain`) keeps the federation held until it ends, even after the owner has left."""

    exclusive: bool
    lock: HeldLock
    federation: Federation
    journal: InjectionJournal | None = None
    record: Mapping[str, Any] | None = None
    shares: _Shares = field(default_factory=_Shares, compare=False, repr=False)

    @property
    def held(self) -> bool:
        return self.lock.held

    def retain(self) -> Callable[[], None]:
        """Take one more share of the admission for a worker; return the call that gives it up (only the first call
        counts). A share of an admission no longer held is nothing to give up."""
        with self.shares.lock:
            if not self.held or self.shares.count == 0:
                return lambda: None
            self.shares.count += 1
        given_up = threading.Event()

        def give_up() -> None:
            if not given_up.is_set():
                given_up.set()
                self._give_up()

        return give_up

    def _give_up(self) -> None:
        with self.shares.lock:
            self.shares.count -= 1
            last = self.shares.count == 0
        if last:
            self._release()

    def mark(self, target: str) -> None:
        """Name in the holder record the target this exclusive admission takes down."""
        if self.journal is not None and self.record is not None and self.held:
            with contextlib.suppress(OSError):  # the record only names the holder: the lock is what holds
                self.journal.write_holder({**self.record, "target": target})

    def close(self) -> None:
        """Give up the owner's share; closing again is harmless. The last share lets the lock go (`_release`)."""
        with self.shares.lock:
            if self.shares.owner_left:
                return
            self.shares.owner_left = True
        self._give_up()

    def _release(self) -> None:
        """Let the lock go, its holder record first, so no record outlives a lock let go this way."""
        if self.journal is not None and self.record is not None and self.held:
            with contextlib.suppress(OSError):  # a record left behind is stale, and the next holder clears it
                self.journal.clear_holder(self.record["token"])
        self.lock.close()


def current_admission() -> Admission | None:
    """The admission the running context holds, if it holds one that is still held."""
    admission = _ADMISSION.get()
    return admission if admission is not None and admission.held else None


def retain_admission() -> Callable[[], None]:
    """A share of the running context's admission for a worker that may outlive it (`Admission.retain`); a call that
    gives up nothing when the context holds none."""
    admission = current_admission()
    return admission.retain() if admission is not None else (lambda: None)


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

    def _admit(self, main: HeldLock, exclusive: bool, purpose: str, target: str | None) -> Admission:
        """The admission of a lock just taken. Exclusive, it writes its holder record (or lets the lock go and raises
        InfraError of kind `injector_failure`); shared, it clears any record, which no holder can then own."""
        journal = InjectionJournal(self.directory)
        if not exclusive:
            with contextlib.suppress(OSError):  # a stale record only misleads a diagnosis, which reads its holder
                journal.clear_holder(None)
            return Admission(False, main, self.federation)
        record = {"token": uuid.uuid4().hex, **holder(), "purpose": purpose, "target": target}
        record["since_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            journal.write_holder(record)
        except OSError as error:  # its text holds the journal's path: only its type is told (O12)
            main.close()
            raise failure(f"the federation's holder record could not be written ({type(error).__name__})") from None
        except BaseException:
            main.close()
            raise
        return Admission(True, main, self.federation, journal, record)

    def try_acquire(self, *, exclusive: bool, purpose: str = OUTAGE_RUN, target: str | None = None) -> Admission | None:
        """Pass the turnstile and take the lock without waiting; None when either is held against it. `purpose` and
        `target` (a label) go into the holder record of an exclusive admission."""
        intent = self._try(FEDERATION_INTENT, fcntl.LOCK_EX)
        if intent is None:
            return None
        try:
            main = self._try(FEDERATION_LOCK, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        finally:
            intent.close()
        return None if main is None else self._admit(main, exclusive, purpose, target)

    def acquire(
        self,
        *,
        exclusive: bool,
        timeout_s: float | None = None,
        cancel: threading.Event | None = None,
        purpose: str = OUTAGE_RUN,
        target: str | None = None,
    ) -> Admission:
        """Pass the turnstile, then wait for the lock while holding it, polling, at most `timeout_s` in all and until
        `cancel` is set; raise InfraError of kind `injector_failure` when it could not be had (`FederationBusy` when
        the wait ran out). `purpose` and `target` go into the holder record of an exclusive admission."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        intent = self._wait(FEDERATION_INTENT, fcntl.LOCK_EX, deadline, cancel)
        try:
            main = self._wait(FEDERATION_LOCK, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH, deadline, cancel)
        finally:
            intent.close()  # whoever waits behind it may now queue for the main lock
        return self._admit(main, exclusive, purpose, target)

    @contextlib.contextmanager
    def held(
        self,
        *,
        exclusive: bool,
        timeout_s: float | None = None,
        target: str | None = None,
        cancel: threading.Event | None = None,
    ) -> Iterator[Admission]:
        """Hold the lock for the body of a `with`, as the running context's admission (the synchronous form)."""
        if current_admission() is not None:
            raise AdmissionRefused("this run already holds the federation's admission")
        admission = self.acquire(exclusive=exclusive, timeout_s=timeout_s, target=target, cancel=cancel)
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
        self, *, exclusive: bool, waits: Any = None, timeout_s: float | None = None, target: str | None = None
    ) -> AsyncIterator[Admission]:
        """Hold the lock for the body of an `async with`, as the running task's admission.

        The wait runs off the event loop and stops on cancellation; with `waits` (a timer or a call context) it is
        inside `waits.infrastructure_wait("lease_queue")`. Shared, the journal must hold no unsettled injection.
        """
        if current_admission() is not None:
            raise AdmissionRefused("this run already holds the federation's admission")
        cancel = threading.Event()
        waiting = asyncio.ensure_future(
            asyncio.to_thread(self.acquire, exclusive=exclusive, timeout_s=timeout_s, cancel=cancel, target=target)
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


def run_admission(config: PlatformConfig) -> RunAdmission | None:
    """The campaign's run admission for a platform with `[faults]` (`StudyPlugin.admission`), None without one.

    The core enters it before an attempt starts and leaves it after the attempt's post-run check: a run whose condition
    declares `provider_outage` holds the federation exclusively, with its target's label in the holder record, every
    other run shares it. The wait is bounded by `admission_timeout_s`, ends with the campaign, and runs before the
    agent's clock starts; one that fails leaves the run unstarted.
    """
    settings = fault_settings_of(config)
    if settings is None:
        return None
    lock = FederationLock(settings)

    def admit(
        treatment_kinds: tuple[str, ...], treatment_targets: Mapping[str, str], cancel: threading.Event
    ) -> contextlib.AbstractContextManager[Admission]:
        exclusive = PROVIDER_OUTAGE in treatment_kinds
        target = treatment_targets.get(PROVIDER_OUTAGE) if exclusive else None
        return lock.held(exclusive=exclusive, timeout_s=settings.admission_timeout_s, target=target, cancel=cancel)

    return admit


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
