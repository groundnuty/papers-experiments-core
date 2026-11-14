# Provenance: new
#   merged: icsoc-2026-code-agent @ c1e76c2, harness/provider_restart.py:148-178 (commands through an injected runner)
"""Docker Compose as the fault injector's driver: stop and start one named service and read its identity (O11, O12).

`ComposeDriver` implements `expcore_onedata.faults.FaultDriver` for a federation whose providers run as Compose
services. Every command goes through an injectable `CommandRunner` (`run_command`, a plain subprocess, by default), so a
test hands it a fake and nothing here touches Docker, and nothing runs at import. The Compose file, the project and the
service names come from `[faults]` of platform.toml; no module holds one.

A service's identity is its container's id and the time the container last started, as `docker inspect` reports them.
`docker compose stop` and then `start` keep the container and change its start time; a recreated container changes its
id. A restore that really happened therefore always changes the identity, and a service that was never down keeps it,
which is what lets the injector refuse to count readiness as a restore (O11).

A running container is not yet a service that serves. `serves` answers whether it does: its container's health check,
when it has one, reports healthy, and the `probe` the driver is built with, a read of the platform's REST API through
that provider (`expcore_onedata.fault_serves`), succeeds. The probe is required, so a driver cannot be built that
takes a restore for a ready provider without asking.

No message holds a service name, a path or a command's output, which can echo either: a failure says which step failed
and how the command ended.

**The heal's two commands (O35, `expcore_onedata.fault_heal`)** run inside the service's own container, found by its
Compose project and service as `observe` finds it, never through a shell on the host. `ping` runs the image's
`/usr/sbin/op_worker ping` and is True only when it prints `pong`. `restart_link` runs op-worker's
`rtransfer_config:restart_link()` through node_package's `nodetool rpcterms` (`RESTART_LINK_SCRIPT`; the trailing `""`
makes the call arity 0, and `env.sh` finds the node's name and cookie inside the container, so neither is on the host's
command line), with an RPC timeout of `RPC_TIMEOUT_S` and a command bound `RESTART_LINK_MARGIN_S` above it, so that
nodetool's own timeout answer is read rather than the command killed. Its result is a word (`LINK_RESULTS`): `ok` is the
call returning `ok`; `badrpc_timeout`, `badrpc`, `unexpected` and `command_timeout` mean the restart may be going on
or may have happened, never that nothing happened; `no_node` and `not_run` mean nodetool reached no node or the command
could not run. nodetool prints the node's name in its failure line, so its output is never kept, only the word.
"""

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from expcore_onedata.client import PlatformError

if TYPE_CHECKING:  # the heal's module imports this one
    from expcore_onedata.fault_matrix import LinkMatrix

# The fourth field is the container's health status, empty for a container with no health check.
INSPECT_FORMAT = "{{.Id}}|{{.State.StartedAt}}|{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"
HEALTH_STATUS = re.compile(r"[a-z]{1,20}")  # Docker's: healthy, unhealthy, starting (and none, for no health check)
OP_WORKER = "/usr/sbin/op_worker"  # node_package's runner in the image's deb layout
RPC_TIMEOUT_S = 120.0
RESTART_LINK_SCRIPT = (
    '. /usr/lib/op_worker/lib/env.sh && $NODETOOL -rpctimeout 120000 rpcterms rtransfer_config restart_link ""'
)
RESTART_LINK_MARGIN_S = 30.0  # the command's bound above the RPC's, so nodetool's timeout answer is read
LINK_RESULTS = ("ok", "badrpc_timeout", "badrpc", "unexpected", "command_timeout", "no_node", "not_run")
_BADRPC = re.compile(r"RPC to \S+ failed: (\S+)")


class DriverError(PlatformError):
    """A command of the fault driver failed, did not finish, or answered with something unreadable."""


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
    """What a service looked like at one reading: whether it runs, its identity when it has a container, and its
    container's health status (`healthy`, `unhealthy` or `starting`) when the container has a health check."""

    running: bool
    identity: str | None
    health: str | None = None


class ComposeDriver:
    """Stop, start and observe Compose services through a command runner."""

    def __init__(
        self,
        compose_file: Path,
        *,
        project: str | None = None,
        command: Sequence[str] = ("docker", "compose"),
        docker: Sequence[str] = ("docker",),
        runner: CommandRunner = run_command,
        timeout_s: float = 120.0,
        probe: Callable[[str], bool],
    ) -> None:
        """`probe(service)` says whether the provider behind a service answers its REST API: True when it does, False
        when it does not (yet). It is required: the driver's `serves` is not defined without it."""
        self._compose = [*command, "--file", str(compose_file), *(["--project-name", project] if project else [])]
        self._docker = list(docker)
        self._runner = runner
        self._timeout_s = timeout_s
        self._probe = probe

    def _run(self, argv: list[str], step: str) -> CommandResult:
        """Run a command; raise DriverError, naming only the step, when it cannot run, does not finish, or fails."""
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
        """Stop the service's container, keeping it."""
        self._run([*self._compose, "stop", service], "stop the service")

    def start(self, service: str) -> None:
        """Start the service's stopped container."""
        self._run([*self._compose, "start", service], "start the service")

    def observe(self, service: str) -> Observation:
        """Read whether the service runs and its identity; a service with no container runs not and has none.

        Raises DriverError when a command fails or its answer cannot be read, and when the service has more than one
        container, whose identity would be ambiguous.
        """
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
        if len(parts) == 3:  # an answer without the health field: no health check to consult
            parts.append("")
        if len(parts) != 4 or not parts[0] or not parts[1] or parts[2] not in ("true", "false"):
            raise DriverError("the container's inspection could not be read")
        health = "" if parts[3] == "none" else parts[3]
        if health and not HEALTH_STATUS.fullmatch(health):
            raise DriverError("the container's inspection could not be read")
        return Observation(parts[2] == "true", f"{parts[0]}@{parts[1]}", health or None)

    def serves(self, service: str) -> bool:
        """Whether the service serves now: its container runs, its health check (when it has one) reports healthy, and
        the probe finds the provider answering its REST API. The probe is not asked of a container that is not yet
        healthy. Raises DriverError when the container cannot be read."""
        seen = self.observe(service)
        if not seen.running or seen.health not in (None, "healthy"):
            return False
        return self._probe(service) is True

    def _container(self, service: str) -> str:
        """The id of the service's one container; raise DriverError when it has none or several."""
        listed = self._run([*self._compose, "ps", "--all", "--quiet", service], "list the service's containers")
        containers = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if len(containers) != 1:
            raise DriverError(f"the service has {len(containers)} containers, so none is exec'd into")
        return containers[0]

    def ping(self, service: str) -> bool:
        """Whether the op-worker node in the service's container answers `pong` to its runner's `ping`. A command that
        fails, times out or prints anything else is no pong; raise DriverError only when no container is found."""
        argv = [*self._docker, "exec", self._container(service), OP_WORKER, "ping"]
        try:
            done = self._runner(argv, timeout_s=self._timeout_s)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return done.returncode == 0 and done.stdout.strip() == "pong"

    def restart_link(self, service: str) -> str:
        """Restart the rtransfer link of the op-worker node in the service's container, once, and return how it ended
        as a word of `LINK_RESULTS` (see the module docstring); never the command's output, which names the node. Raise
        DriverError only when the container cannot be found, before anything was sent to it."""
        argv = [*self._docker, "exec", self._container(service), "bash", "-c", RESTART_LINK_SCRIPT]
        try:
            done = self._runner(argv, timeout_s=RPC_TIMEOUT_S + RESTART_LINK_MARGIN_S)
        except subprocess.TimeoutExpired:
            return "command_timeout"  # the CLI was killed; the RPC may still be running on the node
        except OSError:
            return "not_run"
        return link_result(done)

    def restored_links(self, service: str) -> str | None:
        """The link check of every pair the provider behind a restored service is in (O35): None when each passed, or
        when the probe checks no link, else why one did not. The recovery asks it once the service serves."""
        links = getattr(self._probe, "links", None)
        return None if links is None else links(service)

    def link_matrix(self, service: str) -> "LinkMatrix | None":
        """The link check of every ordered pair as a matrix (`fault_heal.LinkMatrix`), which the heal reads pair by
        pair; None when the probe checks no link."""
        matrix = getattr(self._probe, "matrix", None)
        return None if matrix is None else matrix(service)

    def arm_gate(self, service: str) -> str | None:
        """Before an injection: the id of the session gate's probe file, made and seen by every provider of the probe
        space (`fault_sessions`); None when the probe has no session gate."""
        arm = getattr(self._probe, "arm", None)
        return None if arm is None else arm(service)

    def active_transfers(self) -> int | None:
        """How many transfer listings show a job not yet ended (`fault_sessions.SessionGate.active_transfers`); None
        when the probe cannot tell."""
        transfers = getattr(self._probe, "transfers", None)
        return None if transfers is None else transfers()

    def sessions_ready(self, service: str, gate_probe: str | None) -> str | None:
        """One reading of the session gate for the provider behind a service: None when it passes, else why not."""
        sessions = getattr(self._probe, "sessions", None)
        return None if sessions is None else sessions(service, gate_probe)


def link_result(done: CommandResult) -> str:
    """How a `restart_link` command ended, as a word of `LINK_RESULTS`: nodetool prints the call's return value (`ok`),
    or "RPC to <node> failed: <reason>" and exits 1, or says the node does not answer its ping."""
    out = f"{done.stdout}\n{done.stderr}"
    if done.returncode == 0:
        return "ok" if done.stdout.strip() == "ok" else "unexpected"
    found = _BADRPC.search(out)
    if found:
        return "badrpc_timeout" if found.group(1) == "timeout" else "badrpc"
    if "not responding to pings" in out:
        return "no_node"
    return "unexpected"  # a failure nodetool did not describe: the call may have reached the node
