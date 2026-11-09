"""Fakes for the fault injector: Compose services in memory or in a file, and a platform.toml with `[faults]`."""

import json
import os
import time
from pathlib import Path
from typing import Any

from platform_support import write_platform

from expcore_onedata.compose import DriverError, Observation
from expcore_onedata.config import load_platform
from expcore_onedata.faults import FaultInjector, load_faults

SERVICES = {"alpha": "store-one", "beta": "store-two"}  # by provider name; providers[0] is alpha, providers[1] beta
SERVICE_NAMES = tuple(SERVICES.values())
OUTAGE_TARGET, LOCAL_TARGET = "providers[1]", "providers[0]"


class FakeServices:
    """Compose services that run or not, each with a container whose start count is part of its identity.

    `start` of a stopped service starts it anew (a new identity); `start` of a running one does nothing. With a `path`
    the state lives in that file, so a child process and its parent see the same services. Knobs: `stop_noop` (stop does
    nothing), `start_keeps_identity` (a start runs the service with the identity it had), `start_fails`, `unreadable`.
    """

    def __init__(self, services=SERVICE_NAMES, *, path: Path | None = None, **knobs: Any) -> None:
        self.path = path
        self.knobs = {"stop_noop": False, "start_keeps_identity": False, "start_fails": False, "unreadable": False}
        self.knobs.update(knobs)
        self.calls: list[tuple[str, str]] = []
        self.on_stop = None  # called with the service before a stop takes effect
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
            self._save()


def faults_table(**changes: Any) -> dict[str, Any]:
    """The `[faults]` table of the fixture platform.toml: sanctioned, short waits, both providers' services."""
    table = {
        "sanctioned": True,
        "journal": "fault-journal",
        "compose_file": "compose.yaml",
        "poll_s": 1,
        "settle_timeout_s": 5,
        "clear_attempts": 2,
        "admission_timeout_s": 1,
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
