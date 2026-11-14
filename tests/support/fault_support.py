"""Fakes for the fault injector: Compose services in memory or in a file, and a platform.toml with `[faults]`."""

import json
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from expcore_onedata.compose import CommandResult, DriverError, Observation
from expcore_onedata.config import load_platform
from expcore_onedata.faults import FaultInjector, load_faults
from expcore_onedata.testing import write_platform

SERVICES = {"alpha": "store-one", "beta": "store-two"}  # by provider name; providers[0] is alpha, providers[1] beta
SERVICE_NAMES = tuple(SERVICES.values())
OUTAGE_TARGET, LOCAL_TARGET = "providers[1]", "providers[0]"


class FakeServices:
    """Compose services that run or not, each with a container whose start count is part of its identity.

    `start` of a stopped service starts it anew (a new identity); `start` of a running one does nothing. With a `path`
    the state lives in that file, so a child process and its parent see the same services. Knobs: `stop_noop` (stop does
    nothing), `start_keeps_identity` (a start runs the service with the identity it had), `start_fails`, `unreadable`.

    A service that starts anew does not serve at once: `serves_after` readings of `serves` (counted in this object,
    not in the file) answer no first, and `never_serves` makes every one answer no. `on_serves` is called with the
    service before each such reading.
    """

    def __init__(self, services=SERVICE_NAMES, *, path: Path | None = None, **knobs: Any) -> None:
        self.path = path
        self.knobs = {
            "stop_noop": False,
            "start_keeps_identity": False,
            "start_fails": False,
            "unreadable": False,
            "serves_after": 0,
            "never_serves": False,
        }
        self.knobs.update(knobs)
        self.calls: list[tuple[str, str]] = []
        self.on_stop = None  # called with the service before a stop takes effect
        self.on_serves = None  # called with the service before each reading of whether it serves
        self._starting: dict[str, int] = {}  # service -> readings of `serves` that still answer no
        state = {
            name: {"running": True, "container": f"cid-{index}", "starts": 1} for index, name in enumerate(services)
        }
        if path is not None and path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
        self._state = state
        self._save()

    def _load(self) -> dict[str, Any]:
        if self.path is not None:
            self._state = json.loads(self.path.read_text(encoding="utf-8"))
        return self._state

    def _save(self) -> None:
        if self.path is not None:
            self.path.write_text(json.dumps(self._state), encoding="utf-8")

    def running(self, service: str) -> bool:
        return self._load()[service]["running"]

    def identity(self, service: str) -> str:
        entry = self._load()[service]
        return f"{entry['container']}@start-{entry['starts']}"

    def observe(self, service: str) -> Observation:
        self.calls.append(("observe", service))
        if self.knobs["unreadable"]:
            raise DriverError("the container's inspection could not be read")
        entry = self._load()[service]
        return Observation(entry["running"], self.identity(service))

    def serves(self, service: str) -> bool:
        self.calls.append(("serves", service))
        if self.on_serves is not None:
            self.on_serves(service)
        if self.knobs["never_serves"] or not self._load()[service]["running"]:
            return False
        if self._starting.get(service, 0) > 0:
            self._starting[service] -= 1
            return False
        return True

    def stop(self, service: str) -> None:
        self.calls.append(("stop", service))
        if self.on_stop is not None:
            self.on_stop(service)
        if not self.knobs["stop_noop"]:
            self._load()[service]["running"] = False
            self._save()

    def start(self, service: str) -> None:
        self.calls.append(("start", service))
        if self.knobs["start_fails"]:
            raise DriverError("the command to start the service failed with exit code 1")
        entry = self._load()[service]
        if not entry["running"]:
            entry["running"] = True
            if not self.knobs["start_keeps_identity"]:
                entry["starts"] += 1
            self._starting[service] = self.knobs["serves_after"]
            self._save()


def faults_table(**changes: Any) -> dict[str, Any]:
    """The `[faults]` table of the fixture platform.toml: sanctioned, short waits (no session drain, which a test of the
    drain declares), both providers' services."""
    table = {
        "sanctioned": True,
        "journal": "fault-journal",
        "compose_file": "compose.yaml",
        "project": "stack-a",
        "poll_s": 1,
        "settle_timeout_s": 5,
        "ready_timeout_s": 5,
        "clear_attempts": 2,
        "admission_timeout_s": 1,
        "session_drain_s": 0,
        "services": dict(SERVICES),
    }
    table.update(changes)
    return table


def write_faults(tmp_path: Path, **changes: Any) -> Path:
    """Write platform.toml with a `[faults]` table; return its path."""

    def edit(data: dict[str, Any]) -> None:
        data["faults"] = faults_table(**changes)

    return write_platform(tmp_path, edit)


class FakeClock:
    """A clock that a sleep moves forward."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeDaemon:
    """A Docker daemon behind the command runner, on the injector's fake clock: each service has one container.

    `docker compose stop` asks the daemon to stop the container, which exits `exit_after_s` seconds later (never, when
    that is None). With `stop_hangs` the CLI does not return: it is killed at the runner's timeout, as a real one is,
    and the daemon goes on stopping the container all the same. `start` of a running container does nothing, and does
    not cancel a stop in progress; of an exited one it starts it anew (another start time, so another identity).
    """

    def __init__(self, clock: FakeClock, services: Sequence[str] = SERVICE_NAMES) -> None:
        self.clock = clock
        self.exit_after_s: float | None = 0.0
        self.stop_hangs = False
        self.commands: list[tuple[str, str]] = []
        self._containers = {
            name: {"id": f"cid-{index}", "starts": 1, "running": True, "exit_at": None}
            for index, name in enumerate(services)
        }

    def _container(self, service: str) -> dict[str, Any]:
        container = self._containers[service]
        if container["exit_at"] is not None and self.clock.now >= container["exit_at"]:
            container["running"], container["exit_at"] = False, None
        return container

    def running(self, service: str) -> bool:
        return self._container(service)["running"]

    def starts(self, service: str) -> int:
        return self._container(service)["starts"]

    def __call__(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        if argv[-2] in ("stop", "start"):
            return self._act(argv[-2], argv[-1], argv, timeout_s)
        if argv[-4:-1] == ["ps", "--all", "--quiet"]:
            self.commands.append(("ps", argv[-1]))
            return CommandResult(0, self._container(argv[-1])["id"] + "\n")
        service = next(name for name, c in self._containers.items() if c["id"] == argv[-1])
        self.commands.append(("inspect", service))
        container = self._container(service)
        started = f"start-{container['starts']}"
        return CommandResult(0, f"{container['id']}|{started}|{str(container['running']).lower()}\n")

    def _act(self, verb: str, service: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
        self.commands.append((verb, service))
        container = self._container(service)
        if verb == "start":
            if not container["running"]:
                container["running"], container["starts"] = True, container["starts"] + 1
            return CommandResult(0, "")
        if self.exit_after_s is not None and container["running"]:
            container["exit_at"] = self.clock.now + self.exit_after_s
        if self.stop_hangs:
            self.clock.sleep(timeout_s)  # the CLI is killed here; the daemon is not
            raise subprocess.TimeoutExpired(list(argv), timeout_s)
        return CommandResult(0, "")


def serving(service: str) -> bool:
    """A readiness probe that finds every provider serving: for a driver whose tests are not about readiness."""
    return True


def injector_for(platform: Path, driver: Any, target: str = OUTAGE_TARGET, clock: FakeClock | None = None, **kw: Any):
    """A sanctioned injector for `target` over the fixture platform.toml at `platform`."""
    config = load_platform(platform, require_secrets=False)
    clock = clock or FakeClock()
    settings = load_faults(platform, config)
    return FaultInjector(config, settings, target, sanctioned=True, driver=driver, clock=clock, sleep=clock.sleep, **kw)


def inject_and_die(platform: str, state: str, run_key: str) -> None:
    """In a child process: take the target down and die before clearing, as a kill between inject and clear would."""
    services = FakeServices(path=Path(state))
    injector_for(Path(platform), services).start({"run_key": run_key, "attempt": 1})
    os._exit(0)  # no finally, no clear: the kernel drops the injector's lock as it would for a killed process


def inject_fork_and_die(platform: str, state: str, run_key: str, pid_file: str) -> None:
    """In a child process: take the target down, fork a grandchild that outlives this process, and die before clearing.

    The grandchild inherits every descriptor of the process, the target's lock included unless the injector closes it
    after the fork; it writes its pid to `pid_file` and sleeps until it is killed.
    """
    services = FakeServices(path=Path(state))
    injector_for(Path(platform), services).start({"run_key": run_key, "attempt": 1})
    pid = os.fork()
    if pid == 0:
        time.sleep(120)
        os._exit(0)
    Path(pid_file).write_text(str(pid), encoding="utf-8")
    os._exit(0)
