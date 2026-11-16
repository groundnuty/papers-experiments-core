"""Fakes for the fault injector's HPC backend (`expcore_onedata.fault_hpc`): providers' Apptainer services behind the
command runner, on the injector's fake clock, reached through the driver's `VerbCommand` transport.

The verbs are run as `hpc-service <verb> <service>`, an exec followed by `--` and the allowed command to run inside the
service, as the relay runs the federation's `hpcfed.py`. `FakeHpc` is the runner that answers them: each
service runs or not under a launch counter, which its identity carries, so a start changes it; a stop is asked of the
service, which exits `exit_after_s` seconds later (never, when None), and with `stop_hangs` the command does not return
and is killed at the runner's timeout while the service goes on stopping. Inside a running service, `op_worker ping`
answers `pong` and the nodetool link restart answers `ok` and mends the reader's stuck pairs. `FakeHpc.probe` is the
REST side: whether a provider serves, the link check of every pair, the session gate and the transfer listings, each
read noted with the clock's time, so a test can tell a fetch from a listing and when each happened.
"""

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fault_support import SERVICES, FakeClock

from expcore_onedata.compose import OP_WORKER, RESTART_LINK_SCRIPT, CommandResult
from expcore_onedata.fault_hpc import HpcDriver, VerbCommand
from expcore_onedata.fault_matrix import LinkMatrix, PairState
from expcore_onedata.testing import write_platform

TOOL = "hpc-service"
PING = [OP_WORKER, "ping"]
RESTART_LINK = ["bash", "-c", RESTART_LINK_SCRIPT]
GATE_PROBE = "gate-probe-1"


class FakeProbe:
    """The REST side of the fake federation: `probe(service)` whether a provider serves, `matrix` every pair, `arm`
    and `sessions` the session gate, `transfers` the listings; each read noted in `FakeHpc.reads`."""

    def __init__(self, federation: "FakeHpc") -> None:
        self._federation = federation

    def __call__(self, service: str) -> bool:
        return self._federation.answer_serves(service)

    def matrix(self, service: str) -> LinkMatrix:
        return self._federation.answer_matrix()

    def links(self, service: str) -> str | None:
        matrix = self.matrix(service)
        return None if matrix.ok else matrix.detail

    def arm(self, service: str) -> str:
        self._federation.note("arm_gate", service)
        return GATE_PROBE

    def sessions(self, service: str, gate_probe: str | None) -> str | None:
        self._federation.read("sessions", service)
        return None if gate_probe == GATE_PROBE else "the gate's probe is not the one armed"

    def transfers(self) -> int:
        self._federation.listings.append(self._federation.clock.now)
        return 0


class FakeHpc:
    """Providers' Apptainer services behind the command runner, on a fake clock (see the module docstring).

    `labels` maps each service to its provider's label. `stuck` holds the failing pairs as (reader, source) labels; a
    link restart of a reader mends its own stuck pairs unless `link_fixes[reader]` says what it mends, and a start of a
    reader in `container_fixes` mends its pairs. `start_fails` holds services whose start command fails, `never_serving`
    those that, started anew, never serve, and `serves_after` how many readings a service started anew answers no.
    `actions` notes every status, stop, start, ping and link restart with its label and time; `reads` every REST read
    that fetches through a provider (serves, the link check, the session gate); `listings` every transfer listing.
    """

    def __init__(self, clock: FakeClock, labels: Mapping[str, str]) -> None:
        self.clock = clock
        self.label = dict(labels)
        self.state = {service: {"launch": 1, "running": True, "exit_at": None} for service in labels}
        self.exit_after_s: float | None = 0.0
        self.stop_hangs = False
        self.stop_noop = False
        self.start_fails: set[str] = set()
        self.never_serving: set[str] = set()
        self.serves_after = 0
        self.unreadable: set[str] = set()
        self.not_ready: set[str] = set()  # services that run but whose own checks have not passed
        self.stuck: set[tuple[str, str]] = set()
        self.link_fixes: dict[str, tuple[tuple[str, str], ...]] = {}
        self.container_fixes: set[str] = set()
        self.pong: dict[str, bool] = {}
        self.calls: list[tuple[list[str], float]] = []
        self.actions: list[tuple[str, str, float]] = []
        self.reads: list[tuple[str, str, float]] = []
        self.listings: list[float] = []
        self.log: list[tuple[str, str, float]] = []  # every action and read, in the order they happened
        self.slow: dict[str, int] = {}  # service -> readings of whether it serves that still answer no
        self.probe = FakeProbe(self)

    # --- what the tests read --------------------------------------------------------------------------------------

    def running(self, service: str) -> bool:
        return self._service(service)["running"]

    def launches(self, service: str) -> int:
        return self._service(service)["launch"]

    def acted(self, *verbs: str) -> list[tuple[str, str]]:
        return [(verb, label) for verb, label, _ in self.actions if verb in verbs]

    def note(self, verb: str, service: str) -> None:
        entry = (verb, self.label.get(service, "all"), self.clock.now)
        self.actions.append(entry)
        self.log.append(entry)

    def read(self, what: str, service: str | None) -> None:
        entry = (what, self.label.get(service, "all") if service else "all", self.clock.now)
        self.reads.append(entry)
        self.log.append(entry)

    # --- the runner -----------------------------------------------------------------------------------------------

    def _service(self, service: str) -> dict[str, Any]:
        entry = self.state[service]
        if entry["exit_at"] is not None and self.clock.now >= entry["exit_at"]:
            entry["running"], entry["exit_at"] = False, None
        return entry

    def __call__(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        self.calls.append((list(argv), timeout_s))
        tool, verb, service, *rest = argv
        assert tool == TOOL
        if verb == "status":
            return self._status(service)
        if verb == "stop":
            return self._stop(service, argv, timeout_s)
        if verb == "start":
            return self._start(service)
        assert verb == "exec"
        assert rest[0] == "--"
        return self._exec(service, rest[1:])

    def _status(self, service: str) -> CommandResult:
        self.note("status", service)
        if service in self.unreadable:
            return CommandResult(1, "", "srun: error: the step could not be created\n")
        entry = self._service(service)
        identity = f"{entry['launch']}:{4000 + entry['launch']}:{900 + entry['launch']}"
        ready = entry["running"] and service not in self.not_ready
        record = {"service": service, "running": entry["running"], "identity": identity, "ready": ready}
        return CommandResult(0, json.dumps(record) + "\n")

    def _stop(self, service: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
        self.note("stop", service)
        entry = self._service(service)
        if self.exit_after_s is not None and entry["running"] and not self.stop_noop:
            entry["exit_at"] = self.clock.now + self.exit_after_s
        if self.stop_hangs:
            self.clock.sleep(timeout_s)  # the command is killed here; the service goes on stopping
            raise subprocess.TimeoutExpired(list(argv), timeout_s)
        ended = "exit" if entry["exit_at"] is not None else "none"
        record = {"service": service, "stopped": True, "ended": ended, "seconds": self.exit_after_s or 0.0}
        return CommandResult(0, json.dumps(record) + "\n")

    def _start(self, service: str) -> CommandResult:
        self.note("start", service)
        if service in self.start_fails:
            return CommandResult(1, "", "hpcop: the service did not start\n")
        entry = self._service(service)
        if not entry["running"]:
            entry["running"], entry["launch"] = True, entry["launch"] + 1
            self.slow[service] = self.serves_after
            if self.label[service] in self.container_fixes:
                self.stuck = {pair for pair in self.stuck if pair[0] != self.label[service]}
        return CommandResult(0, "")

    def _exec(self, service: str, inner: list[str]) -> CommandResult:
        running = self._service(service)["running"]
        label = self.label[service]
        not_there = CommandResult(1, f"Node 'op_worker@{service}' not responding to pings.\n")
        if inner == PING:
            self.note("ping", service)
            return CommandResult(0, "pong\n") if running and self.pong.get(label, True) else not_there
        if inner == RESTART_LINK:
            self.note("restart_link", service)
            if not running:
                return not_there
            fixes = self.link_fixes.get(label)
            mended = {pair for pair in self.stuck if pair[0] == label} if fixes is None else set(fixes)
            self.stuck -= mended
            return CommandResult(0, "ok\n")
        return CommandResult(127, "", "unknown command\n")

    # --- the REST side --------------------------------------------------------------------------------------------

    def answer_serves(self, service: str) -> bool:
        self.read("serves", service)
        if service in self.never_serving:
            return False
        if self.slow.get(service, 0) > 0:
            self.slow[service] -= 1
            return False
        return True

    def answer_matrix(self) -> LinkMatrix:
        self.read("matrix", None)
        labels = sorted(self.label.values())
        pairs = tuple(
            PairState(reader, source, (reader, source) not in self.stuck, 0.1, (reader, source) in self.stuck)
            for reader in labels
            for source in labels
            if reader != source
        )
        shown = [f"{source}->{reader}" for reader, source in sorted(self.stuck)]
        return LinkMatrix(not self.stuck, "stuck: " + ", ".join(shown) if shown else "every pair passed", pairs)


def hpc_driver(federation: FakeHpc, *, timeout_s: float = 10.0, stop_timeout_s: float | None = None) -> HpcDriver:
    """The real HPC driver over the fake federation: the verbs run as commands through it, and its probe."""
    transport = VerbCommand([TOOL], federation)
    return HpcDriver(transport, timeout_s=timeout_s, stop_timeout_s=stop_timeout_s, probe=federation.probe)


FEDERATION = "hpcop-study-0a1b2c3d"  # an allocation's name, as `up` makes it (DESIGN.md section 4)


def write_state(root: Path, services: Sequence[str]) -> Path:
    """The federation's state file in `root`, as `up` writes it: the allocation's name and each service."""
    state = {
        "format": "expcore-hpc-federation/1",
        "allocation": {"name": FEDERATION},
        "services": {name: {"kind": "op", "launch": 1, "state": "running"} for name in services},
    }
    path = root / "hpc-federation.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def hpc_table(**changes: Any) -> dict[str, Any]:
    """`[faults]` of an HPC federation's platform.toml: the relay's backend, short waits, both providers' services."""
    table = {
        "sanctioned": True,
        "journal": "fault-journal",
        "backend": "hpc",
        "stop_bound_s": 4,
        "settle_timeout_s": 5,
        "relay_wait_s": 1,
        "poll_s": 1,
        "ready_timeout_s": 5,
        "clear_attempts": 2,
        "admission_timeout_s": 1,
        "session_drain_s": 0,
        "services": dict(SERVICES),
    }
    table.update(changes)
    return {key: value for key, value in table.items() if value is not None}


def write_hpc_platform(root: Path, *, margin_s: int | None = 100_000, **changes: Any) -> Path:
    """platform.toml of an HPC federation in `root`, with its state file; `margin_s` sets `[tokens] margin_s`."""

    def edit(data: dict[str, Any]) -> None:
        data["faults"] = hpc_table(**changes)
        if margin_s is not None:
            data["tokens"] = {**data.get("tokens", {}), "margin_s": margin_s}

    write_state(root, tuple(SERVICES.values()))
    return write_platform(root, edit)
