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
included, is set aside. Both kinds of this module are registered by `expcore_onedata.sandbox.kinds`, which the study
lists in `[study] platforms` (`platforms = ["expcore_onedata.sandbox.kinds"]`), so `provider_outage` is known when its
conditions are validated and `check` knows both kinds when a record names one.

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
times; a service that cannot be read is started all the same, and its restore is `restore_unverified`.

**A restore is verified only when the provider serves (O11).** A changed identity is not yet a provider that serves: in
the live run of 12 Nov 2026 (deploy/compose-federation/day0) the restarted provider's REST endpoint still refused
connections about a second after the restart, and its container's health check passed 48 to 52 s after the container
started. Releasing the federation then lets the next run, a control perhaps, start on a provider that is still starting,
whose refusals and delays its agent can absorb without any recorded infrastructure error. So once the identity is
verified, `clear` waits for the driver to say the provider serves (`FaultDriver.serves`: `ComposeDriver` asks that its
container, if it has a health check, is healthy and that the platform's authenticated REST read through that provider
succeeds, `expcore_onedata.fault_serves`), every `poll_s`, for at most `ready_timeout_s` (180 s by default), and only
then journals the injection cleared and lets the federation go. A provider that does not serve within the bound is a
restore `not_serving`: unverified, the injection open and journalled `clear_failed`, the target's lock and the
federation's exclusive admission held, the fault gate failing for it, exactly as for a failed restore; the start command
is not issued again, and a later `clear`, `finish` or `start` finds the provider restored and waits again. The snapshot
records the wait as `ready_after_s`, in the `verified` step and in the injection once it served: the seconds from the
verified identity change to the first reading that showed it serving (0 for a provider that already served). A provider
the stop never took down is not waited for. The recovery of a crashed run's injection, by `start`, `recover` or the
fault gate, waits the same way, and its `recovered` reading carries `ready_after_s` too.

**The links after a restore (O35).** A provider that serves is not yet a federation whose links transfer: an outage
can leave a pair between two other providers stuck. Before it stops anything, `start` makes the session gate's probe
file and journals it with the injection (`fault_sessions`); the restore then passes the session gate (`gate_timeout_s`,
else `sessions_not_ready`) and checks every ordered pair (`links_stuck`), and with `[faults] heal` declared heals a stuck
link, bounded and failing closed (`fault_heal`: `all_links` restarts every provider's link at once, then a stuck
reader's container). Only the injector's own clear and start heal, while an exclusive admission is held; the snapshot
gains the `gate` and `heal` steps.

**A stop of unknown outcome (O11).** When the stop command raises, typically at its timeout (`command_timeout_s`), the
CLI is killed but the Docker daemon goes on stopping the container for up to the provider's `stop_grace_period`; the
federation's is 120 s, the default command timeout. The injection is then journalled `stop_unknown`, and the clear
does not start the service or read its identity at once, because a `start` of a container still running does nothing
and the old identity then read is the container on its way down, not one that never went down. It waits, for at most
`settle_timeout_s`, for the service to show not running, and only then starts it and judges the restore by the changed
identity. A service not seen down within that bound leaves the restore `stop_unsettled`: unverified, the injection
open and pending, never cleared. A process killed while the stop command blocks cannot journal `stop_unknown`, so the
journal says it before the stop instead: a `stopping` line, written durably right before the command (a journal that
fails there stops the start before anything is taken down). Recovery treats an open injection that was never seen down
as of unknown outcome, `stop_unknown` for one that has the `stopping` line or the `stop_unknown` line, and
`stop_unrecorded` for one that has neither (killed before the command, or journalled before the `stopping` line
existed): it waits,
for at most `settle_timeout_s`, for the service to show not running or running with a changed identity, and then
restores it and requires it to serve, as for any restore. Only a service that every reading of the whole bound showed
running with the identity it had, after a journal that never says the stop was asked for, is `never_down`; after a
`stopping` line it is `stop_unsettled` instead. `settle_timeout_s` must therefore exceed the providers' `stop_grace_period` with a
margin; its default, 180 s, does for the federation's 120 s, and platform.toml does not know the grace, so a federation
that raises it raises this too (`expcore_onedata.fault_settings`). `clear` never raises: a failed restore is in the
snapshot (`verified: false`), stays open in the journal, journalled `clear_failed`, and stays pending, with the target's
lock held, so a later `clear`, `finish` or `start` tries again, and the health gate and every other injector's start
refuse the provider until then.

**A restore that is not verified is never passed silently (O11).** `clear` and `finish` answer it as data (`False`, the
snapshot's `ok` false), which a caller that handles only a raising exit never reads, so every other way out either
raises or leaves the failure in the record, which the campaign refuses to score:

- `session` clears however the start and the body end, and raises `RestoreNotVerified` at its exit when the restore is
  not verified. It is an `InfraError` of kind `injector_failure`, so a caller keyed on that kind sees what it saw for a
  failed start, and it names the provider by its label, how the restore ended (`how`, a word such as `not_serving`), and
  what was checked (whether the identity changed to a running one, and whether the provider then served), never a
  service, a path or a token. When the start or the body raised too, it is raised from that exception (`__cause__`), so
  neither is lost; an interrupt, an exit or a cancellation (anything but an `Exception`) is raised as it was, with the
  restore failure in its notes.
- `running` clears the same way and **returns** when its body returned, so the executor that runs the agent inside it
  (`record = await record_run(...)`) keeps its record. The record carries the failure itself: the snapshot, which is
  the record's instrument reading, holds `restore_verified` (`true` once a restore of a target this injector took down
  is verified, `false` with `how`, `identity_changed` and `serving` when it is not, absent when nothing was taken down).
  The core never scores a record that carries it as anything but `true` (`expcore.records.injections.restore_refusal`):
  the record is set aside, and the scheduler sets its attempt aside as `injector_failure` whatever the post-run health
  check decides. When the body raised, no record exists to carry it, and `running` raises as `session` does.
- `check`, the instrument's post-run check, raises the same failure, so an executor that starts the injector with
  `start_instruments` and clears it itself gets the refusal from `finish_instruments`: the record keeps its snapshot
  and holds the refusal (`instrument_refusal`), and the attempt is set aside as infrastructure and run again.
- `start` already raises `injector_failure` whenever the clear it makes before or after a failure does not verify.

`finish` returns the snapshot and never raises for it, as `finish_instruments` would replace a raised snapshot with a
bare failure that loses the injections; its `check` refuses. `finish` calls `clear`, `session` and `running` call it
around both `start` and the agent's run, and `start` calls it before failing.

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
The injection's own entry holds `target`, `injected`, `cleared`, `verified` and, once its provider served, `ready_after_s`.

**Use.** The study's plugin passes the platform's run admission to the campaign (`admission=run_admission(config,
schema)`, with the study's factor schema), which admits each run to the federation before anything starts (exclusive
when its condition declares `provider_outage` or `exclusive = true`) and keeps it until after the post-run check, and
the platform's half of the fingerprint (`platform=config.fingerprint()`), which holds the injector's sanction and
targets. The executor, already admitted, starts the sandbox and
the tool server, builds the injector for the target its condition declares and runs the agent inside `running`:

    injector = FaultInjector(config, settings, request.treatment_targets[PROVIDER_OUTAGE], sanctioned=True,
                             driver=compose_driver(settings, probe=provider_serves(config, settings, client)))
    async with injector.running(dataclasses.asdict(request.identity)):
        record = await record_run(request, ...)
    record = await asyncio.to_thread(finish_instruments, record, [injector])

`running` starts the injector in a worker thread and keeps that thread's future: a cancellation cannot stop the thread,
so on cancellation it waits for it (shielded, bounded), clears however the block ends and raises the cancellation again.
A restore that is not verified does not raise at the block's exit: the record, built inside the block, is kept, and
`finish_instruments` puts the injector's reading into it, with `restore_verified` false, which the campaign refuses to
score (it sets the attempt aside as `injector_failure` to run again), and `check` refuses it as well. The journal keeps
the provider unhealthy for the gate. An executor that drives the injector through `start_instruments` and clears it
itself gets the same from `finish_instruments`, with its record kept. Never
await `asyncio.to_thread(injector.start, ...)` bare: cancelled, it leaves the thread stopping the provider while the
code meant to clear it never runs. `start` and `clear` hold the injector's own lock, so a clear waits for a start still
running in its thread. `finish_instruments` puts the snapshot into the record and derives its treatment flag again from it.

**Order (C31).** `finish` is the restore, and `finish_instruments` finishes the instruments in the reverse of their
list. Probes listed after the injector therefore finish before the restore and read the state the outage left; probes
listed before it finish after the restore. `running` clears when its block exits, so a `finish_instruments` called
after the block runs after the restore the block's exit made, whatever the order (a restore that was not verified is
tried again by `finish`, and then the order decides again): call it inside the block for the probes to read first.

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
from expcore.errors import InfraError, uncharged_kinds
from expcore.records.injections import FAULT_NAMESPACE
from expcore_onedata.admission import (
    INJECTOR,
    Admission,
    AdmissionRefused,
    FederationLock,
    current_admission,
    require_same_federation,
    retain_admission,
)
from expcore_onedata.compose import DriverError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_journal import HeldLock, SettledInjections, holder
from expcore_onedata.fault_recovery import RESTORED, FaultDriver, Recovery, RestoreNotVerified, digest
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_settings import FaultSettings, compose_driver, load_faults
from expcore_onedata.sandbox.kinds import INJECTOR_FAILURE, PROVIDER_OUTAGE

__all__ = [
    "INJECTOR_FAILURE",
    "PROVIDER_OUTAGE",
    "FaultDriver",
    "FaultInjector",
    "FaultRefused",
    "FaultSettings",
    "RestoreNotVerified",
    "SettledInjections",
    "Unsanctioned",
    "compose_driver",
    "load_faults",
    "provider_outage",
    "provider_serves",
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
        labels = {config.provider_label(name): service for name, service in settings.services.items()}
        self._recovery = Recovery(settings, driver, clock=clock, sleep=sleep, labels=labels)
        self._journal = self._recovery.journal
        self._gate_probe: str | None = None  # the open injection's session gate probe file (`fault_sessions`)
        self._lock: HeldLock | None = None
        self._federation = FederationLock(settings)
        self._admission: Admission | None = None  # the exclusive admission it took itself, when its run held none
        self._cancel = threading.Event()  # set when `running` is cancelled: an admission still awaited is given up
        self._mutex = threading.RLock()  # a start and a clear never run at once, in whichever threads
        rounds = settings.clear_attempts + 2
        heal_s = settings.heal_timeout_s if settings.heal != "off" else 0.0
        self.cancel_wait_s = (
            (settings.settle_timeout_s + settings.command_timeout_s) * rounds
            + 2 * (settings.ready_timeout_s + settings.gate_timeout_s + heal_s)  # a recovered provider's, and its own
            + settings.session_drain_s  # and the drain a clear waits out before it lets its own admission go
        )  # about a start's longest
        self._pending: tuple[str, str] | None = None  # the open injection's journal id and identity digest
        self._run: dict[str, Any] | None = None
        self._steps: list[dict[str, Any]] = []
        self._injection: dict[str, Any] | None = None
        self._went_down = False  # the target was seen down during the open injection
        self._stop_unknown = False  # the open injection's stop command raised: what it did is not known
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
            give_up = retain_admission()  # the run's admission stays held until this start ends, whatever its task does
            try:
                self._start(identity)
            except BaseException:
                if self._pending is None:  # nothing is down: the federation goes back to the other runs
                    self._release_admission()
                raise
            finally:
                give_up()

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
        unsettled = self._recover(heal=self._holds_exclusive())  # under the exclusive admission just taken
        if unsettled:  # held down by a live injector: the federation is not in its baseline state
            raise self._fail(f"an injection of {', '.join(unsettled)} is unsettled, so no other run may start")
        self._lock = self._journal.try_lock(self.target)
        if self._lock is None:
            raise self._fail(f"{self.target} is held by another run's fault injector")
        digest, entry = self._journal_injection()
        self._pending, self._went_down = (entry, digest), False
        self._injection = {"target": self.target, "injected": False, "cleared": False, "verified": False}
        try:
            # durable before the command, so a process killed while it blocks leaves the line (a failing journal stops here)
            self._journal.append({"event": "stopping", "id": entry})
            self._stop_unknown = True  # until the command returns, the daemon may be stopping the target all the same
            self._driver.stop(self._service)
            self._stop_unknown = False
            self._went_down = self._recovery.wait(lambda seen: not seen.running, self._service) is not None
            if not self._went_down:
                raise _NotSeenDown()
            self._journal.append({"event": "injected", "id": entry})
        except BaseException as error:  # whatever broke after the stop, the target is brought back before it is raised
            why = type(error).__name__
            self._step("injected", False, identity_before_sha256=digest, seen_down=self._went_down, error=why)
            if self._stop_unknown:
                self._journal_stop_unknown(entry)
            self._clear()
            if not isinstance(error, Exception):
                raise
            raise self._fail(f"{self.target} could not be taken down, seen down and journalled ({why})") from None
        self._injection["injected"] = True
        self._step("injected", True, identity_before_sha256=digest)

    def _journal_stop_unknown(self, entry: str) -> None:
        """Journal that the stop command raised, so that a later process settling the injection waits for the target
        to show down too. The journal may be what failed: the injection stays open all the same, and this injector's
        own clear still knows."""
        with contextlib.suppress(OSError):
            self._journal.append({"event": "stop_unknown", "id": entry})

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

    def _holds_exclusive(self) -> bool:
        """Whether the federation is held exclusively for this injector now: the admission it took itself, or its run's
        own, still held and for this federation. Only then may a restore heal a link (O35): a run's admission that has
        ended, or none, heals nothing."""
        return self._exclusive_admission() is not None

    def _exclusive_admission(self) -> Admission | None:
        """The exclusive admission that holds the federation for this injector now: the one it took itself, else its
        run's, still held and for this federation; None when there is none."""
        if self._admission is not None and self._admission.held and self._admission.exclusive:
            return self._admission
        held = current_admission()
        if held is None or not held.exclusive:
            return None
        try:
            require_same_federation(held, self._federation.federation)
        except AdmissionRefused:
            return None
        return held

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
            self._gate_probe = self._arm_gate()
            before_sha256, entry = digest(before.identity), uuid.uuid4().hex
            line = {
                "target": self.target,
                "service": self._service,
                "identity_sha256": before_sha256,
                "holder": holder(),
                **({} if self._gate_probe is None else {"gate_probe": self._gate_probe}),
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

    def _arm_gate(self) -> str | None:
        """The session gate's probe file, made and seen by every provider of the probe space before anything stops
        (`fault_sessions`), so a restore's gate reads a file its provider knew; None when the driver has no gate. A
        probe that cannot be made fails the start with nothing stopped (its error's type only, O12)."""
        arm = getattr(self._driver, "arm_gate", None)
        if arm is None:
            return None
        try:
            return arm(self._service)
        except Exception as error:
            why = type(error).__name__
            raise self._fail(
                f"the session gate's probe could not be made before {self.target}'s outage ({why})"
            ) from None

    def _record_links(self, record: Mapping[str, Any]) -> None:
        """The session gate's reading and the heal's record of the latest restore, as steps of the snapshot (O35). A
        driver with no session gate (`how` none) records no gate step, so its readings stay as they were."""
        gate = record.get("gate")
        if isinstance(gate, Mapping) and gate.get("how") != "none":
            self._step("gate", gate.get("ok") is True, **{k: v for k, v in gate.items() if k != "ok"})
        heal = record.get("heal")
        if isinstance(heal, Mapping):
            self._step("heal", heal.get("ok") is True, **{k: v for k, v in heal.items() if k != "ok"})

    @contextlib.asynccontextmanager
    async def running(
        self, identity: Mapping[str, Any], *, cancel_wait_s: float | None = None
    ) -> AsyncIterator["FaultInjector"]:
        """Start in a worker thread for the body of an `async with`, and clear however it ends (O11).

        The start's future is kept: when the body's task is cancelled while the start runs, the thread runs on, so the
        cancellation waits for it, shielded and at most `cancel_wait_s` (by default `cancel_wait_s` of the injector,
        about the longest a start takes), then the clear runs and the cancellation is raised again. The clear, in its
        own thread, waits for a start that is still running, through the injector's lock.

        An exit that follows the body returning returns too, whether or not the restore is verified: the executor that
        runs the agent inside the block (`record = await record_run(...)`) keeps its record, which carries the failure
        itself (`snapshot`: `restore_verified` false, with `how`, `identity_changed` and `serving`) for the campaign to
        refuse to score, and `check` refuses it as well. When the body raised, no record exists to carry it, so the
        restore failure is raised from the body's exception as `session` does; a cancellation or any other
        `BaseException` is raised as it was, with the failure in its notes.
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
        except BaseException as error:
            await self._clear_for(bound)
            self._raise_unrestored(error)
            raise
        await self._clear_for(bound)

    async def _clear_for(self, bound: float) -> None:
        """Clear in a worker thread, shielded: a cancellation waits for it for at most `bound` and is raised again, with
        in its notes what the clear found (a restore not verified), or that it was still running when the wait ended."""
        clearing = asyncio.ensure_future(asyncio.to_thread(self.clear))
        try:
            await asyncio.shield(clearing)
        except asyncio.CancelledError as cancelled:
            await _outlast(clearing, bound)
            self._note_clear(cancelled, clearing)
            raise

    def _note_clear(self, error: BaseException, clearing: "asyncio.Future[Any]") -> None:
        """Add to `error`, a cancellation that interrupted the clear's wait, the restore failure the clear left, or that
        the clear had not finished. The injector's lock is taken only for a clear that did: a cancelled task (the event
        loop's shutdown cancels every one) says nothing of its worker thread, which may still hold the lock, and waiting
        for it would block the loop and defeat the bound."""
        if not clearing.done() or clearing.cancelled():
            error.add_note(f"the clear of {self.target} had not finished when the wait for it ended: not verified")
            return
        with self._mutex:
            failure = self._unrestored()
        if failure is not None:
            error.add_note(str(failure))

    def finish(self) -> dict[str, Any]:
        """Bring the target back and verify it (`clear`), then return the snapshot of every step. It never raises for
        a restore that is not verified, so the snapshot reaches the record whole: the instrument's `check` refuses it."""
        self.clear()
        return self.snapshot()

    @contextlib.contextmanager
    def session(self, identity: Mapping[str, Any]) -> Iterator["FaultInjector"]:
        """Inject for the body of a `with`, and clear however it ends, whatever the start or the body raises (O11).

        The exit raises `RestoreNotVerified` when the restore is not verified: the exit that returns normally is a
        verified restore. When the start or the body raised too, the restore failure is raised from that exception
        (`__cause__`) so neither is lost; an interrupt or an exit (anything but an `Exception`) is raised as it was,
        with the restore failure in its notes. The injection stays open in the journal, so the health gate and the
        next start refuse the provider, until a later `clear`, `finish` or `start` verifies the restore.
        """
        try:
            self.start(identity)
            yield self
        except BaseException as error:
            self.clear()
            self._raise_unrestored(error)
            raise
        self.clear()
        self.check()

    def check(self) -> None:
        """The post-run check (`finish_instruments` calls it once the snapshot is in): return when nothing this
        injector took down is left unrestored; raise `RestoreNotVerified`, an `InfraError` of kind `injector_failure`,
        when an injection it made is not verified restored or is still open. A start that took nothing down is not one.
        It does not clear: `finish` or `clear` did, and a record whose restore failed keeps its snapshot and holds this
        refusal (`instrument_refusal`), so the attempt is set aside and run again with its usage known."""
        with self._mutex:
            failure = self._unrestored()
        if failure is not None:
            raise failure

    def _unrestored(self) -> RestoreNotVerified | None:
        """The failure of a restore that is not verified, read from the injector's own steps; None when there is none."""
        injection = self._injection
        open_ = self._pending is not None
        if not open_ and not (injection is not None and injection["injected"] and not injection["verified"]):
            return None
        cleared = [step for step in self._steps if step["step"] == "cleared"]
        verified = [step for step in self._steps if step["step"] == "verified"]
        healed = [step for step in self._steps if step["step"] == "heal"]
        return RestoreNotVerified(
            self.target,
            str(cleared[-1]["how"]) if cleared else "not_cleared",
            identity_changed=bool(verified and verified[-1]["identity_changed"]),
            serving=bool(verified and verified[-1]["ready_after_s"] is not None),
            still_open=open_,
            tried=str(healed[-1]["tried"]) if healed else None,
        )

    def _raise_unrestored(self, error: BaseException) -> None:
        """Called after a clear, while `error` is being raised: raise the failure of a restore not verified from
        `error`, or, when `error` is no `Exception` or is a platform's hold (an `InfraError` of a kind that costs no
        attempt, C33: an ingest's start its baseline's mark held back), attach it as a note and return so that `error`
        is raised as it was. A held start costs its run no attempt whatever failed after it; the restore's failure is
        still in the journal (`clear_failed`), so the health gate fails for it and no run starts on it."""
        with self._mutex:
            failure = self._unrestored()
        if failure is None:
            return
        held = isinstance(error, InfraError) and error.kind in uncharged_kinds()
        if isinstance(error, Exception) and not held:
            raise failure from error
        error.add_note(str(failure))

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
            **self._restore_reading(injection),
        }

    def _restore_reading(self, injection: Mapping[str, Any] | None) -> dict[str, Any]:
        """What the snapshot says of the restore, so that the record carries a failure itself (O11): `restore_verified`
        true once a target this injector took down is verified restored; false, with `how` the restore ended, whether the
        provider's `identity_changed` and whether it was `serving`, when an injection is open or was not verified. The
        core reads it (`expcore.records.injections.restore_refusal`) and never scores the record. Nothing, when the
        injector took nothing down and has nothing open: no restore was needed, so none is claimed verified."""
        failure = self._unrestored()
        if failure is not None:
            return {
                "restore_verified": False,
                "how": failure.how,
                "identity_changed": failure.identity_changed,
                "serving": failure.serving,
            }
        return {"restore_verified": True} if injection and injection["injected"] else {}

    # --- clearing and recovery --------------------------------------------------------------------------------

    def clear(self) -> bool:
        """Bring the target back and verify its identity changed; record the outcome and never raise for it.

        Returns True when this injector leaves nothing down. Until a restore is verified the injection stays pending,
        with the target's lock held, so a later `clear`, `finish` or `start` tries again; a verified one makes calling
        it again harmless. A target the injection never took down is found running with its old identity, unless the
        stop's outcome is unknown: then the clear first waits for the target to show not running. An injector discarded
        with its restore still unverified lets the lock go when it is collected, so recovery can settle it.
        """
        with self._mutex:
            # a share of the run's admission for the whole clear: a cancelled run whose context lets its admission go
            # while this worker still restores or heals keeps the federation held until the clear has ended (O35)
            give_up = retain_admission()
            try:
                return self._clear()
            finally:
                give_up()

    def _clear(self) -> bool:
        if self._pending is None:
            return True
        entry, digest = self._pending
        injection = self._injection if self._injection is not None else {"target": self.target, "injected": False}
        began = self._clock()  # the restore's start, from which an unknown end of the run's sessions is drained in full
        try:
            done, how, after, ready_after_s = self._recovery.settle(
                {
                    "id": entry,
                    "target": self.target,
                    "service": self._service,
                    "identity_sha256": digest,
                    "injected": self._went_down,
                    "stop_unknown": self._stop_unknown,
                    "gate_probe": self._gate_probe,
                },
                heal=self._holds_exclusive(),  # a heal only under an exclusive admission held now, between runs
            )
        except Exception as error:  # never lose the reading: a clear that failed stays pending and open
            done, how, after, ready_after_s = False, type(error).__name__, None, None
        verified = done and how in ("restored", "found_restored")
        timed = {} if ready_after_s is None else {"ready_after_s": ready_after_s}
        self._injection = {**injection, "cleared": done, "verified": verified, **timed}
        self._record_links(self._recovery.restore_record)
        self._step("cleared", done, how=how)
        self._step(
            "verified",
            verified,
            identity_changed=after is not None,
            identity_after_sha256=after,
            ready_after_s=ready_after_s,
        )
        if done:
            self._drain(how, began)  # before the federation goes back to the other runs
            self._pending, self._stop_unknown = None, False
            self._unlock()
            self._release_admission()
        return done

    def recover(self) -> list[str]:
        """Clear and verify every open injection of the journal that no live injector holds; raise InfraError of kind
        `injector_failure` when one cannot be. Return the targets of the injections left open, which live injectors
        hold."""
        with self._mutex:
            return self._recover()

    def _recover(self, *, heal: bool = False) -> list[str]:
        """Recovery, healing a stuck link only when `heal`: the caller holds the federation exclusively (`_start`). A
        provider it restored went down in a run this injector knows nothing of, so the exclusive admission held arms its
        drain with no times: the full wait from the start of the recovery (`fault_drain`)."""
        self._require_sanction()
        began = self._clock()
        try:
            return self._recovery.recover(self._step, heal=heal)
        finally:
            if self._recovery.restored and self._settings.session_drain_s > 0:
                admission = self._exclusive_admission()
                if admission is not None and admission.drain is not None:
                    admission.drain.arm(began, clock=self._clock, sleep=self._sleep)

    def _drain(self, how: str, began: float) -> None:
        """After a verified restore of a provider that went down: arm the drain of the exclusive admission that holds the
        federation (`fault_drain`), and record a `drain` step. An injector that holds the federation by an admission of
        its own waits the drain out here, before it lets go: it has no sandbox of its own run. A run's admission is
        waited out when the run ends, with the sandbox's mount and gate ends known; the restore comes before them, so
        the step records the plan as it stands (`deferred`), with the mount unknown if it is still up."""
        if how not in RESTORED or self._settings.session_drain_s <= 0:
            return
        admission = self._exclusive_admission()
        if admission is None or admission.drain is None:
            self._step("drain", False, why="no exclusive admission holds the federation: nothing keeps it closed")
            return
        admission.drain.arm(began, clock=self._clock, sleep=self._sleep)
        own = admission is self._admission
        record = admission.drain.wait() if own else {**admission.drain.plan().record(), "waited_s": 0.0}

        def since_start(moment: float | None) -> float | None:
            return None if moment is None else round(moment - self._origin, 3)

        self._step(
            "drain",
            True,
            deferred=not own,
            drain_s=record["drain_s"],
            basis=record["basis"],
            why=record["why"],
            mount_end_s=since_start(record["mount_end"]),
            gate_stop_s=since_start(record["gate_stop"]),
            restore_start_s=since_start(record["restore_started"]),
            until_s=since_start(record["until"]),
            waited_s=round(record["waited_s"], 3),
        )

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
