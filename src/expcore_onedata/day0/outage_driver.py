# Provenance: new
"""Taking one provider's service down and bringing it back, for the day-0 outage check (O27, O11).

This is the mechanism of the fault injector of the open pull request #27 (`expcore_onedata.faults`), without its
journal, its federation lock and its sanction hook, none of which is on `main`: the service is stopped and started
through Docker Compose, its identity is the container's id and the time it last started (`docker inspect`), and a
restore counts only when the service runs with an identity other than the one read before the stop. Readiness alone is
not a restore, and a service back with the same identity is a failed restore. The names that matter for a merge are the
injector's: the `[faults]` table of platform.toml (`sanctioned`, `compose_file`, `project`, `command`, `docker`,
`services`, the timeouts), the observation of a service and the injectable command runner. Once #27 is on `main` the
check can take its `FaultInjector` through `Outage`'s two calls (`take_down`, `restore`).

What this module does not do, and the injector does: it keeps no journal, so a process killed between the stop and the
start leaves the service stopped (`federation.py up` starts what is stopped), and it takes no lock on the federation, so
the one federation session at a time on this host (`.claude/rules/onedata-25.md`) is the operator's to keep.

**Sanction (O10).** A provider is taken down only when platform.toml says `sanctioned = true` in `[faults]` and the
caller allowed it (`EXPCORE_LIVE_FAULTS=1`, `day0.runner.faults_allowed`); the check refuses, as unknown, without both.

**Names (O12).** Compose services are named in platform.toml alone. A message holds the step that failed and how the
command ended, never a service, a path or a command's output; an identity is recorded as a SHA-256 digest. Nothing here
runs at import, and every command goes through an injectable runner, so a test hands it a fake and nothing touches
Docker.
"""

import contextlib
import hashlib
import math
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from expcore.config.loading import Section, load_toml
from expcore_onedata.client import PlatformError
from expcore_onedata.config import PlatformConfig

INSPECT_FORMAT = "{{.Id}}|{{.State.StartedAt}}|{{.State.Running}}"
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# The keys of `[faults]` that the injector of #27 reads. All are accepted, so a platform.toml written for it loads here.
# The journal, the admission timeout and the health wait belong to its journal, lock and health gate, which this
# mechanism has none of: they are checked as the right kind of value and not used.
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
    "admission_timeout_s",
    "health_wait_s",
    "services",
)


class DriverError(PlatformError):
    """A command of the driver failed, did not finish, or answered with something unreadable."""


class OutageError(RuntimeError):
    """The service could not be taken down and seen down. `stop_sent` says whether the stop command was sent, so a
    caller knows that a restore has something to do."""

    def __init__(self, message: str, *, stop_sent: bool) -> None:
        super().__init__(message)
        self.stop_sent = stop_sent


@dataclass(frozen=True)
class CommandResult:
    """How a command ended: its exit code and what it printed."""

    returncode: int
    stdout: str
    stderr: str = ""


class CommandRunner(Protocol):
    """Run one command, given as an argument vector (never through a shell), bounded by `timeout_s`."""

    def __call__(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        """Return how the command ended; raise OSError or subprocess.TimeoutExpired when it could not run or finish."""
        ...


def run_command(argv: Sequence[str], *, timeout_s: float) -> CommandResult:
    """The default runner: the command as a subprocess with its output captured, no shell, bounded by `timeout_s`."""
    done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_s, check=False)
    return CommandResult(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class Observation:
    """What a service looked like at one reading: whether it runs, and its identity when it has a container."""

    running: bool
    identity: str | None


class ServiceDriver(Protocol):
    """What the outage needs of a deployment: stop and start one named service, and read it."""

    def stop(self, service: str) -> None:
        """Stop the service's container, keeping it."""
        ...

    def start(self, service: str) -> None:
        """Start the service's stopped container."""
        ...

    def observe(self, service: str) -> Observation:
        """Read whether the service runs and its identity; raise DriverError when that cannot be read."""
        ...


class ComposeServices:
    """Stop, start and observe Docker Compose services through a command runner."""

    def __init__(
        self,
        compose_file: Path,
        *,
        project: str | None = None,
        command: Sequence[str] = ("docker", "compose"),
        docker: Sequence[str] = ("docker",),
        runner: CommandRunner = run_command,
        timeout_s: float = 120.0,
    ) -> None:
        self._compose = [*command, "--file", str(compose_file), *(["--project-name", project] if project else [])]
        self._docker, self._runner, self._timeout_s = list(docker), runner, timeout_s

    def _run(self, argv: list[str], step: str) -> CommandResult:
        """Run a command; raise DriverError, naming only the step, when it cannot run, does not finish or fails."""
        try:
            done = self._runner(argv, timeout_s=self._timeout_s)
        except subprocess.TimeoutExpired:
            raise DriverError(f"the command to {step} did not finish within {self._timeout_s:g} s") from None
        except OSError as error:
            raise DriverError(f"the command to {step} could not run ({type(error).__name__})") from None
        if done.returncode != 0:
            raise DriverError(f"the command to {step} failed with exit code {done.returncode}")
        return done

    def stop(self, service: str) -> None:
        self._run([*self._compose, "stop", service], "stop the service")

    def start(self, service: str) -> None:
        self._run([*self._compose, "start", service], "start the service")

    def observe(self, service: str) -> Observation:
        """A service with no container runs not and has none; more than one container makes its identity ambiguous."""
        listed = self._run([*self._compose, "ps", "--all", "--quiet", service], "list the service's containers")
        containers = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if not containers:
            return Observation(False, None)
        if len(containers) > 1:
            raise DriverError(f"the service has {len(containers)} containers, so its identity is ambiguous")
        shown = self._run(
            [*self._docker, "inspect", "--format", INSPECT_FORMAT, containers[0]], "inspect the container"
        )
        parts = shown.stdout.strip().split("|")
        if len(parts) != 3 or not parts[0] or not parts[1] or parts[2] not in ("true", "false"):
            raise DriverError("the container's inspection could not be read")
        return Observation(parts[2] == "true", f"{parts[0]}@{parts[1]}")


@dataclass(frozen=True)
class OutageSettings:
    """`[faults]` of platform.toml, validated: the sanction, the Compose file and the service of each provider."""

    file: Path
    sanctioned: bool
    compose_file: Path
    services: Mapping[str, str]  # provider name -> Compose service
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


def load_outage_settings(path: str | os.PathLike[str], config: PlatformConfig) -> OutageSettings:
    """Read `[faults]` of a platform.toml against its loaded configuration; raise ConfigError naming the file and key.

    A message names a service entry by its position, never by the provider's or the service's name (O12).
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
    return OutageSettings(
        file=file,
        sanctioned=sanctioned,
        compose_file=(file.parent / table.string("compose_file")).resolve(),
        services=dict(services.data),
        project=table.optional_string("project"),
        command=table.strings("command") or ("docker", "compose"),
        docker=table.strings("docker") or ("docker",),
        poll_s=_number(table, "poll_s", 2.0, 0.001),
        settle_timeout_s=_number(table, "settle_timeout_s", 180.0, 0),
        command_timeout_s=_number(table, "command_timeout_s", 120.0, 1),
        clear_attempts=int(_number(table, "clear_attempts", 3, 1, integer=True)),
    )


def compose_services(settings: OutageSettings, runner: CommandRunner = run_command) -> ComposeServices:
    """The Compose driver `[faults]` describes, running its commands through `runner`."""
    return ComposeServices(
        settings.compose_file,
        project=settings.project,
        command=settings.command,
        docker=settings.docker,
        runner=runner,
        timeout_s=settings.command_timeout_s,
    )


def digest(identity: str) -> str:
    """An identity as the report keeps it: its SHA-256, never the container's id."""
    return hashlib.sha256(identity.encode()).hexdigest()


class Outage:
    """One service taken down and brought back: `take_down`, then `restore` in a `finally` (O11).

    Time goes through the clock and sleep it is given, so a test never waits. `restore` never raises: what it could not
    do is in its result (`verified` false), and calling it again after a verified restore changes nothing.
    """

    def __init__(
        self,
        driver: ServiceDriver,
        service: str,
        *,
        settle_timeout_s: float,
        poll_s: float,
        clear_attempts: int,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._driver, self._service = driver, service
        self._settle_s, self._poll_s, self._attempts = settle_timeout_s, poll_s, clear_attempts
        self._clock, self._sleep = clock, sleep
        self._before: str | None = None  # the identity read before the stop
        self._stop_sent = False
        self._went_down = False

    def _wait(self, wanted: Callable[[Observation], bool]) -> Observation | None:
        """Read the service until it is as wanted, or the settle timeout passes; None when it was not seen so."""
        began = self._clock()
        while True:
            try:
                seen = self._driver.observe(self._service)
            except DriverError:
                seen = None  # an unreadable service is not seen as anything
            if seen is not None and wanted(seen):
                return seen
            if self._clock() - began >= self._settle_s:
                return None
            self._sleep(self._poll_s)

    def take_down(self) -> dict[str, Any]:
        """Read the service's identity, stop it and see it down; raise OutageError when any of that fails.

        Nothing is stopped unless the service runs and has an identity. After the stop command is sent, a failure
        raises with `stop_sent` true: whether the service went down is then not known, and `restore` settles it."""
        try:
            before = self._driver.observe(self._service)
        except DriverError as error:
            raise OutageError(f"the service could not be read before the stop: {error}", stop_sent=False) from None
        if not before.running or before.identity is None:
            raise OutageError(
                "the service is not running before the stop, so no restore could be confirmed", stop_sent=False
            )
        self._before, began = before.identity, self._clock()
        self._stop_sent = True
        try:
            self._driver.stop(self._service)
        except DriverError as error:
            raise OutageError(f"the stop command did not succeed: {error}", stop_sent=True) from None
        if self._wait(lambda seen: not seen.running) is None:
            raise OutageError(
                f"the service was not seen down within {self._settle_s:g} s of the stop command", stop_sent=True
            )
        self._went_down = True
        seconds = round(self._clock() - began, 3)
        return {"identity_before_sha256": digest(self._before), "seen_down": True, "seconds": seconds}

    def restore(self) -> dict[str, Any]:
        """Start the service again until it runs, and verify that its identity changed; never raise.

        `verified` is true when the service runs with another identity than before the stop, or when it was never seen
        down and runs with the identity it had (nothing was injected, so nothing needs restoring). Otherwise it is
        false, with `how` saying why: the service never ran again, or it ran with the old identity after it had been
        seen down."""
        if not self._stop_sent or self._before is None:
            return {"verified": True, "how": "not_injected", "attempts": 0}
        began, seen = self._clock(), None
        attempts = 0
        while seen is None and attempts < self._attempts:
            attempts += 1
            with contextlib.suppress(DriverError):  # a start that fails is tried again, then judged by what is seen
                self._driver.start(self._service)
            seen = self._wait(lambda now: now.running)
        facts: dict[str, Any] = {"attempts": attempts, "seconds": round(self._clock() - began, 3)}
        if seen is None:
            return {**facts, "verified": False, "how": "not_running_again"}
        changed = seen.identity != self._before
        facts["identity_changed"] = changed
        if seen.identity is not None:
            facts["identity_after_sha256"] = digest(seen.identity)
        if changed:
            return {**facts, "verified": True, "how": "restored"}
        if not self._went_down:
            return {**facts, "verified": True, "how": "never_seen_down"}
        return {**facts, "verified": False, "how": "identity_unchanged"}
