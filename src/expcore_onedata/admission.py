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
(`inner`: the pre-campaign checks and the space). Keep `health_wait_s` below the campaign's `health_timeout_s`, or
the check counts as down before its wait can run out.

**A wait that runs out is told apart.** Whoever takes the federation exclusively (`OUTAGE_RUN`, `INJECTOR` or the
gate's `RECOVERY`) first writes a holder record beside the lock, whole and fsynced (`fault_journal.HOLDER`): its pid and
boot id, its purpose, the target's label and the time it took the lock; it removes the record before it lets the lock
go, and a shared admission clears any record left behind, since no exclusive holder can then exist. When the check's
wait runs out it reads the journal and the record. An injection whose restore failed (journalled `clear_failed`)
fails the check, naming the target by its label, whoever still holds the federation. A live holder, or none recorded,
is contention, an outage run in progress, which is no fault of the federation: the check answers deferred
(`Health(..., deferred=True)`), which the core's gate treats as a wait that charges no run and leaves a finished run's
outcome standing. A recorded holder that is gone while the lock stays held is stuck: the check recovers, verified,
whatever the journal holds open (or, with nothing open, only clears the record) and fails, saying the lock is stuck.
"""

import asyncio
import contextlib
import fcntl
import os
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expcore.campaign.healthgate import Health, HealthCheck, read_health
from expcore.config.loading import ConfigError, load_toml
from expcore.errors import TARGET_LABEL, InfraError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_journal import HeldLock, InjectionJournal, holder, holder_alive, make_directories
from expcore_onedata.fault_recovery import INJECTOR_FAILURE, FaultDriver, Recovery, failure
from expcore_onedata.fault_settings import FaultSettings, load_faults

FEDERATION_LOCK = "federation.lock"
FEDERATION_INTENT = "federation.intent"  # the turnstile every admission passes before the main lock
OUTAGE_RUN, INJECTOR, RECOVERY = "outage_run", "injector", "recovery"  # why the federation is held exclusively
_PURPOSES = {OUTAGE_RUN: "an outage run", INJECTOR: "an injector", RECOVERY: "the recovery of the health check"}
_STAMP = re.compile(r"[0-9T:+.-]{10,40}")
_WORD = re.compile(r"[A-Za-z_]{1,40}")
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
    """The federation's lock as one run holds it, shared or exclusive, and which federation it is; held exclusively,
    the holder record it wrote beside the lock (`record`), which it removes before it lets the lock go."""

    exclusive: bool
    lock: HeldLock
    federation: Federation
    journal: InjectionJournal | None = None
    record: Mapping[str, Any] | None = None

    @property
    def held(self) -> bool:
        return self.lock.held

    def mark(self, target: str) -> None:
        """Name in the holder record the target this exclusive admission takes down."""
        if self.journal is not None and self.record is not None and self.held:
            with contextlib.suppress(OSError):  # the record only names the holder: the lock is what holds
                self.journal.write_holder({**self.record, "target": target})

    def close(self) -> None:
        """Let the lock go, its holder record first, so no record outlives a lock let go this way."""
        if self.journal is not None and self.record is not None and self.held:
            with contextlib.suppress(OSError):  # a record left behind is stale, and the next holder clears it
                self.journal.clear_holder(self.record["token"])
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
        self, *, exclusive: bool, timeout_s: float | None = None, target: str | None = None
    ) -> Iterator[Admission]:
        """Hold the lock for the body of a `with`, as the running context's admission (the synchronous form)."""
        if current_admission() is not None:
            raise AdmissionRefused("this run already holds the federation's admission")
        admission = self.acquire(exclusive=exclusive, timeout_s=timeout_s, target=target)
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


def _named(record: Mapping[str, Any] | None) -> str:
    """The holder a record names, in words: its purpose, the target's label and since when; never anything else."""
    if record is None:
        return "a run"
    who = _PURPOSES.get(str(record.get("purpose")), "a holder")
    target = record.get("target")
    since = record.get("since_utc")
    of = f" of {target}" if isinstance(target, str) and TARGET_LABEL.fullmatch(target) else ""
    at = f" since {since}" if isinstance(since, str) and _STAMP.fullmatch(since) else ""
    return f"{who}{of}{at}"


def _unsettled(entries: list[dict[str, Any]]) -> Health:
    """The failure of a journal with open injections: a failed restore by its target's label and how it failed."""
    failed = sorted({(entry["target"], entry["clear_failed"]) for entry in entries if "clear_failed" in entry})
    if failed:
        hows = ", ".join(f"{target} ({how if _WORD.fullmatch(how) else 'failed'})" for target, how in failed)
        return Health(False, f"the restore of {hows} failed: the federation is not in its baseline state")
    targets = ", ".join(sorted({entry["target"] for entry in entries}))
    return Health(False, f"an injection of {targets} is unsettled: the federation is not in its baseline state")


class FaultGate:
    """The fault half of the platform's standard health check, around the rest of it (see the module docstring)."""

    def __init__(self, settings: FaultSettings, driver: FaultDriver, inner: HealthCheck | None = None) -> None:
        self._lock = FederationLock(settings)
        self._recovery = Recovery(settings, driver)
        self._journal = self._recovery.journal
        self._may_recover = settings.sanctioned is True
        self._wait_s = settings.health_wait_s
        self._inner = inner

    def __call__(self) -> Health:
        try:
            if self._recovery.entries() and self._may_recover:
                exclusive = self._lock.try_acquire(exclusive=True, purpose=RECOVERY)
                if exclusive is not None:  # no run is on the federation: settle what is left
                    try:
                        self._recovery.recover()
                    finally:
                        exclusive.close()
            try:
                shared = self._lock.acquire(exclusive=False, timeout_s=self._wait_s)
            except FederationBusy:
                return self._busy()
            try:  # held shared for the whole check: no injection starts until it has answered
                entries = self._recovery.entries()
                if entries:
                    return _unsettled(entries)
                return (
                    read_health(self._inner) if self._inner is not None else Health(True, "no injection is unsettled")
                )
            finally:
                shared.close()
        except InfraError as error:
            return Health(False, error.detail)

    def _busy(self) -> Health:
        """The wait for the federation ran out: tell a failed restore, a stuck holder and an outage run on it apart."""
        entries = self._recovery.entries()
        if any("clear_failed" in entry for entry in entries):
            return _unsettled(entries)  # a restore failed: a fault of the federation, whoever still holds it
        record = self._journal.read_holder()
        if record is None or holder_alive(record) is not False:  # contention: a wait, which charges no run
            return Health(False, f"{_named(record)} holds the federation: the check waits for it", deferred=True)
        done = "its record is cleared"
        if entries:  # it left something open: bring it back, verified, before anything else
            if not self._may_recover:
                return Health(
                    False, f"the federation's lock is stuck: {_named(record)} is gone and left an injection open"
                )
            self._recovery.recover()  # raises InfraError naming the target when one cannot be settled
            done = "what it left open is recovered and its record cleared"
        self._journal.clear_holder(record.get("token"))
        return Health(
            False, f"the federation's lock is stuck: {_named(record)} is gone, yet the lock stays held; {done}"
        )
