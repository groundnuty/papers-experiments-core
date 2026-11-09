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

**Inject (O11).** The provider's identity is read first, the injection is journalled durably (the line written, flushed
and fsynced, and the directory fsynced when the journal is new) before the stop command runs, and the provider must
then be seen down within a bounded wait. Otherwise it is brought back and `start` fails as `injector_failure`, a kind
no condition may declare, so the attempt is set aside and run again.

**Clear (O11).** `clear` brings the provider back and counts a restore only when the provider runs with an identity
other than the one read before the injection (`ComposeDriver`: the container's id and start time). Readiness alone is
not a restore, and a provider back with the same identity is a failed restore. The start is tried a bounded number of
times. `clear` never raises: a failed restore is in the snapshot (`verified: false`) and stays open in the journal.
`finish` calls it, `session` calls it in a `finally` around the agent's run, and `start` calls it before failing.

**Recovery (O11).** Before it injects, `start` (like `recover`) clears and verifies every injection the journal holds
open whose target no live injector holds: each injector holds an exclusive lock on its target's lock file for as long
as it may have the target down, and the kernel drops the lock when its holder dies. A crash between inject and clear
therefore leaves no provider down after the next start, and an open injection that cannot be cleared and verified
fails that start as `injector_failure`, so a campaign never runs on with a provider it left down.

**Readings.** Every step (`sanctioned`, `recovered`, `injected`, `cleared`, `verified`) is a reading in the snapshot,
labelled with the seconds on the injector's clock since `start`, so replay serves it and no scorer asks the platform.

**Use.** An executor builds the injector for the target its condition declares and runs it around the loop:

    injector = FaultInjector(config, settings, request.treatment_targets[PROVIDER_OUTAGE], sanctioned=True,
                             driver=compose_driver(settings))
    await asyncio.to_thread(start_instruments, [injector], dataclasses.asdict(request.identity))
    try:
        record = await record_run(request, ...)
    finally:
        await asyncio.to_thread(injector.clear)
    record = await asyncio.to_thread(finish_instruments, record, [injector])

`finish_instruments` puts the snapshot into the record and derives its treatment flag again from it.

**Names (O12).** Outside the driver and the journal the provider is known only by its label: in the snapshot, in every
message and in every failure. Identities are recorded as SHA-256 digests. The settings, read by `load_faults`:

    [faults]
    sanctioned = true                # the configuration's half of the sanction; false or absent refuses
    journal = "fault-journal"        # directory of the injection journal and the targets' lock files
    compose_file = "compose.yaml"    # the Compose file that declares the providers' services
    project = "..."                  # optional: the Compose project name
    command = ["docker", "compose"]  # optional: the Compose command
    docker = ["docker"]              # optional: the command that inspects a container
    poll_s = 2                       # optional: between two readings of a service
    settle_timeout_s = 180           # optional: the longest a service may take to go down or to come back
    command_timeout_s = 120          # optional: the longest one command may take
    clear_attempts = 3               # optional: how many times a restore is tried

    [faults.services]
    <provider name> = "<service>"    # each provider that may be taken down, by its name in [providers]
"""

import contextlib
import hashlib
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from expcore.config.loading import ConfigError, Section, load_toml
from expcore.errors import InfraError, register_infra_kind
from expcore.records.injections import FAULT_NAMESPACE
from expcore_onedata.compose import CommandRunner, ComposeDriver, DriverError, Observation, run_command
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_journal import InjectionJournal

PROVIDER_OUTAGE = "provider_outage"
INJECTOR_FAILURE = "injector_failure"
register_infra_kind(PROVIDER_OUTAGE, "a provider of the federation was unavailable")
register_infra_kind(INJECTOR_FAILURE, "the fault injector could not inject, clear or verify its fault", treatable=False)
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_KEYS = (
    "sanctioned",
    "journal",
    "compose_file",
    "project",
    "command",
    "docker",
    "poll_s",
    "settle_timeout_s",
    "command_timeout_s",
    "clear_attempts",
    "services",
)


class Unsanctioned(ConfigError):
    """The injector was built or run without both halves of the sanction (O10)."""


class FaultRefused(ConfigError):
    """The injector was asked to act on a target platform.toml does not let it take down, or for no run."""


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


def provider_outage(config: PlatformConfig, name: str, detail: str) -> InfraError:
    """The failure of a provider the platform attributes to it, naming it by its label (`providers[i]`), never by name."""
    return InfraError(PROVIDER_OUTAGE, detail, target=config.provider_label(name))


@dataclass(frozen=True)
class FaultSettings:
    """`[faults]` of platform.toml, validated: the sanction, the journal, the Compose settings and the services."""

    file: Path
    sanctioned: bool
    journal: Path
    compose_file: Path
    services: Mapping[str, str]
    project: str | None = None
    command: tuple[str, ...] = ("docker", "compose")
    docker: tuple[str, ...] = ("docker",)
    poll_s: float = 2.0
    settle_timeout_s: float = 180.0
    command_timeout_s: float = 120.0
    clear_attempts: int = 3


def _number(table: Section, key: str, default: float, minimum: float, *, integer: bool = False) -> float:
    """Read an optional finite number at or above a minimum."""
    value = table.data.get(key, default)
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value < minimum:
        raise table.fail(key, f"must be {'an integer' if integer else 'a number'} of at least {minimum:g}")
    return value


def load_faults(path: str | os.PathLike[str], config: PlatformConfig) -> FaultSettings:
    """Read `[faults]` of a platform.toml against its loaded configuration; raise ConfigError naming the file and key.

    A message names a service entry by its position, never by the provider's or the service's name.
    """
    file = Path(path).resolve()
    table = Section(load_toml(file), file, "platform.toml").section("faults")
    table.reject_unknown(_KEYS)
    sanctioned = table.data.get("sanctioned", False)
    if not isinstance(sanctioned, bool):
        raise table.fail("sanctioned", "must be true or false")
    services = Section(table.table("services"), file, "[faults.services]")
    if not services.data:
        raise services.fail(None, "must name the service of at least one provider")
    for index, (name, service) in enumerate(services.data.items()):
        if name not in config.providers:
            raise services.fail(None, f"entry {index} is not a provider declared in [providers]")
        if not isinstance(service, str) or not SERVICE.fullmatch(service):
            raise services.fail(None, f"entry {index} must be a service name: letters, digits, '_', '.' and '-'")
    words = {}
    for key, default in (("command", ("docker", "compose")), ("docker", ("docker",))):
        words[key] = tuple(table.strings(key)) or default
    return FaultSettings(
        file=file,
        sanctioned=sanctioned,
        journal=(file.parent / table.string("journal")).resolve(),
        compose_file=(file.parent / table.string("compose_file")).resolve(),
        services=dict(services.data),
        project=table.optional_string("project"),
        command=words["command"],
        docker=words["docker"],
        poll_s=_number(table, "poll_s", 2.0, 0.001),
        settle_timeout_s=_number(table, "settle_timeout_s", 180.0, 0),
        command_timeout_s=_number(table, "command_timeout_s", 120.0, 1),
        clear_attempts=_number(table, "clear_attempts", 3, 1, integer=True),
    )


def compose_driver(settings: FaultSettings, runner: CommandRunner = run_command) -> ComposeDriver:
    """The Compose driver `[faults]` describes, running its commands through `runner`."""
    return ComposeDriver(
        settings.compose_file,
        project=settings.project,
        command=settings.command,
        docker=settings.docker,
        runner=runner,
        timeout_s=settings.command_timeout_s,
    )


def _digest(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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
        if target not in names:
            raise FaultRefused(f"{settings.file}: [faults]: the target {target!r} is not a provider label")
        service = settings.services.get(names[target])
        if service is None:
            raise FaultRefused(f"{settings.file}: [faults.services]: no service is declared for {target}")
        self.target, self._service, self._driver = target, service, driver
        self._clock, self._sleep = clock, sleep
        self._journal = InjectionJournal(settings.journal)
        self._lock: int | None = None
        self._pending: tuple[str, str] | None = None  # the open injection's journal id and identity digest
        self._run: dict[str, Any] | None = None
        self._steps: list[dict[str, Any]] = []
        self._injection: dict[str, Any] | None = None
        self._last: Observation | None = None
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
        `injector_failure` when either fails, with the target brought back first.

        `identity` is the run's identity as a plain mapping with at least its `run_key` and `attempt`, which the snapshot
        names so the treatment rule counts the injection for this attempt only.
        """
        self._require_sanction()
        if self._pending is not None:  # an earlier start of this injector was never cleared
            self.clear()
        run_key, attempt = identity.get("run_key"), identity.get("attempt")
        if not isinstance(run_key, str) or isinstance(attempt, bool) or not isinstance(attempt, int):
            raise FaultRefused("a fault injector is started with the run's identity: its run_key and attempt")
        self._origin, self._steps, self._injection = self._clock(), [], None
        self._run = {"run_key": run_key, "attempt": attempt}
        self._step("sanctioned", True)
        self.recover()
        self._lock = self._journal.try_lock(self.target)
        if self._lock is None:
            raise self._fail(f"{self.target} is held by another run's fault injector")
        try:
            before = self._driver.observe(self._service)
        except DriverError as error:
            self._unlock()
            raise self._fail(f"{self.target} could not be read before the injection: {error}") from None
        if not before.running or before.identity is None:
            self._unlock()
            raise self._fail(f"{self.target} is not running before the injection, so no restore could be confirmed")
        entry, digest = uuid.uuid4().hex, _digest(before.identity)
        line = {"target": self.target, "service": self._service, "identity_sha256": digest}
        self._journal.append({"event": "injecting", "id": entry, **line})
        self._pending = (entry, digest)
        self._injection = {"target": self.target, "injected": False, "cleared": False, "verified": False}
        try:
            self._driver.stop(self._service)
            down = self._wait(lambda seen: not seen.running)
        except DriverError:
            down = None
        if down is None:
            self._step("injected", False, identity_before_sha256=digest)
            self.clear()
            raise self._fail(f"{self.target} could not be taken down and seen down; it was brought back")
        self._journal.append({"event": "injected", "id": entry})
        self._injection["injected"] = True
        self._step("injected", True, identity_before_sha256=digest)

    def finish(self) -> dict[str, Any]:
        """Bring the target back and verify it (`clear`), then return the snapshot of every step."""
        self.clear()
        return self.snapshot()

    @contextlib.contextmanager
    def session(self, identity: Mapping[str, Any]) -> Iterator["FaultInjector"]:
        """Inject for the body of a `with` and clear in `finally`, whatever the body raises (O11)."""
        self.start(identity)
        try:
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

        Returns True when this injector leaves nothing down. Calling it again after it ran is harmless. A target the
        injection never took down (the stop failed) is found running with its old identity and needs no restore.
        """
        if self._pending is None:
            return True
        entry, digest = self._pending
        self._pending = None
        injection = self._injection if self._injection is not None else {"target": self.target, "injected": False}
        try:
            done, how, after = self._settle(
                {"id": entry, "service": self._service, "identity_sha256": digest, "injected": injection["injected"]}
            )
        except Exception as error:  # never lose the reading: a clear that failed stays open for recovery
            done, how, after = False, type(error).__name__, None
        finally:
            self._unlock()
        verified = done and how in ("restored", "found_restored")
        self._injection = {**injection, "cleared": done, "verified": verified}
        self._step("cleared", done, how=how)
        self._step("verified", verified, identity_changed=after is not None, identity_after_sha256=after)
        return done

    def recover(self) -> None:
        """Clear and verify every open injection of the journal that no live injector holds; raise InfraError of kind
        `injector_failure` when one cannot be."""
        self._require_sanction()
        try:
            entries = self._journal.open_entries()
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise self._fail(f"the injection journal cannot be read: {error}") from None
        for entry in entries:
            lock = self._journal.try_lock(entry["target"])
            if lock is None:
                continue  # a live injector holds the target down on purpose
            try:
                done, how, after = self._settle(entry)
            finally:
                self._journal.release(lock)
            self._step("recovered", done, target=entry["target"], how=how, identity_after_sha256=after)
            if not done:
                raise self._fail(f"an injection of {entry['target']} that an earlier run left open was not cleared")

    def _settle(self, entry: Mapping[str, Any]) -> tuple[bool, str, str | None]:
        """Leave one injection's target up: find it back, find it never went down, or bring it back with a changed
        identity; journal it cleared when it is. Return whether it is, how, and the new identity's digest if any."""
        before = entry["identity_sha256"]
        try:
            seen = self._driver.observe(entry["service"])
        except DriverError:
            return False, "unreadable", None
        same = seen.identity is not None and _digest(seen.identity) == before
        after = None
        if seen.running and seen.identity is not None and not same:
            how, after = "found_restored", _digest(seen.identity)
        elif seen.running and same:
            if entry["injected"]:  # it was seen down, so it must have started anew: its identity cannot be trusted
                return False, "identity_unchanged", None
            how = "never_down"  # the stop never took effect
        else:
            restored, after, reason = self._restore(entry["service"], before)
            if not restored:
                return False, reason, None
            how = "restored"
        self._journal.append({"event": "cleared", "id": entry["id"], "how": how})
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
                seen = self._wait(
                    lambda s: s.running and s.identity is not None and _digest(s.identity) != before, service
                )
            except DriverError:
                reason = "unreadable"
                continue
            if seen is not None:
                return True, _digest(seen.identity or ""), "restored"
            last = self._last
            if last is not None and last.running and last.identity is not None and _digest(last.identity) == before:
                return False, None, "identity_unchanged"  # back with the identity it had: not a restore (O11)
        return False, None, reason

    def _wait(self, done: Callable[[Observation], bool], service: str | None = None) -> Observation | None:
        """Read the service until `done` holds, bounded by the settle timeout and by a count; None when it never did."""
        polls = max(1, math.ceil(self._settings.settle_timeout_s / self._settings.poll_s)) + 1
        started = self._clock()
        self._last = None
        for poll in range(polls):
            if poll:
                self._sleep(self._settings.poll_s)
            self._last = self._driver.observe(service or self._service)
            if done(self._last):
                return self._last
            if self._clock() - started >= self._settings.settle_timeout_s:
                break
        return None

    def _unlock(self) -> None:
        if self._lock is not None:
            self._journal.release(self._lock)
            self._lock = None
