# Provenance: new
"""An HPC federation as the fault injector's driver: a provider's Apptainer service stopped, started, read and exec'd
into (O11, O12, O35).

`HpcDriver` implements `expcore_onedata.fault_recovery.FaultDriver`, and the heal's commands, for a federation whose
providers run as setuid Apptainer services on batch nodes (deploy/hpc-federation). It has the Compose driver's surface
(`expcore_onedata.compose.ComposeDriver`), so the injector, its journal, the restore and its settle, the recovery, the
heal and the fault gate run on it unchanged: the same lines journalled in the same order, the same waits, the same
words, the same fail-closed rules. Only how a verb reaches a provider differs.

**Four verbs.** The federation's process model (`ServiceCommands`) builds one argv per verb for a provider's service:
`status` (read-only: whether it runs and its identity), `stop` (the service stopped, its work directory, data and
registration kept), `start` (the same service started again from them) and `exec` (an argv run inside the running
service). The driver runs each through an injectable `CommandRunner` (`compose.run_command`, a plain subprocess, by
default), bounded by `command_timeout_s`, so a test hands it a fake and nothing here reaches a cluster. Which argv
each verb is (a step of the federation's allocation on the service's node, say) is the process model's, never this
module's.

**The identity** is the service's own, as `status` prints it, one JSON line `{"running": <bool>, "identity": <str or
null>}`: stable while the service is left alone, and changed by every start of a stopped service, so a restore that
happened always changes it and one that did not keeps it (O11). A status that fails, times out, cannot run or answers
anything else is a reading that failed (DriverError), never a guess. A service has no container health check:
`serves` is the service running and the probe (`fault_serves.provider_serves`) finding its provider answering an
authenticated REST read; a stopped service is not asked about.

**A stop of unknown outcome.** A stop command killed at its timeout may leave the service stopping all the same, as a
Docker daemon does; the injector journals it `stop_unknown` and the restore waits, for at most `settle_timeout_s`, for
it to show down first (`faults.py`, "A stop of unknown outcome"). So `settle_timeout_s` must exceed the longest a
service's stop may take.

**The heal (O35).** `ping` runs op-worker's runner (`/usr/sbin/op_worker ping`) inside the provider's service, and
`restart_link` runs the Compose driver's very command, `compose.RESTART_LINK_SCRIPT` (rtransfer_config's restart_link
through nodetool's rpcterms), inside it with the same bound, so env.sh finds the service's own epmd port and cookie
and none is on a command line. Its result is the same word (`compose.link_result`); a failure the exec does not
describe, a step that could not be made among them, may have reached the node, so it is never `not_run` and never
retried. **The container fallback** is the heal's restart of a reader through the injector's own stop and start
(`fault_restore.LinkSettling.restart_container`), which here is the provider's service stopped and started again,
journalled, verified and recovered exactly as on Compose.

**The REST side** (the session gate, the link check, the transfer listings, the gate's probe file) is the probe's, read
as the Compose driver reads it. No message holds a service name, a path or a command's output: a failure says which
step failed and how its command ended.
"""

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol

from expcore_onedata.compose import (
    OP_WORKER,
    RESTART_LINK_MARGIN_S,
    RESTART_LINK_SCRIPT,
    RPC_TIMEOUT_S,
    CommandResult,
    CommandRunner,
    DriverError,
    Observation,
    link_result,
    run_command,
)

if TYPE_CHECKING:  # the heal's module imports the Compose driver's, which this one shares
    from expcore_onedata.fault_matrix import LinkMatrix

IDENTITY = re.compile(r"[A-Za-z0-9@:._+-]{1,200}")  # an identity as a status prints it: one word, never a path
STATUS = "read the service's status"


class ServiceCommands(Protocol):
    """The federation's process model: the argv of each verb on one provider's service. Each raises DriverError, or
    any other error (an unreadable state), when the service cannot be addressed, before anything runs."""

    def status(self, service: str) -> Sequence[str]:
        """Print one JSON line: whether the service runs and its identity (read-only)."""
        ...

    def stop(self, service: str) -> Sequence[str]:
        """Stop the service, keeping its work directory, data and registration."""
        ...

    def start(self, service: str) -> Sequence[str]:
        """Start the stopped service again from its work directory; a running one is left as it is."""
        ...

    def exec(self, service: str, argv: Sequence[str]) -> Sequence[str]:
        """Run `argv` inside the running service, its output captured and its exit code returned."""
        ...


class HpcDriver:
    """Stop, start, read and exec into providers' Apptainer services through a command runner (see the module
    docstring)."""

    def __init__(
        self,
        commands: ServiceCommands,
        *,
        runner: CommandRunner = run_command,
        timeout_s: float = 120.0,
        probe: Callable[[str], bool],
    ) -> None:
        """`probe(service)` says whether the provider behind a service answers its REST API: True when it does, False
        when it does not (yet). It is required: the driver's `serves` is not defined without it."""
        self._commands, self._runner, self._timeout_s, self._probe = commands, runner, timeout_s, probe

    def _argv(self, build: Callable[[], Sequence[str]], step: str) -> list[str]:
        """The argv the process model builds for a step; raise DriverError, naming only the step (or the model's own
        DriverError), when the service cannot be addressed."""
        try:
            return list(build())
        except DriverError:
            raise
        except Exception as error:  # an unreadable state names its path: only the error's type is told (O12)
            raise DriverError(f"the command to {step} could not be addressed ({type(error).__name__})") from None

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
        """Stop the provider's service, keeping its work directory, data and registration."""
        step = "stop the service"
        self._run(self._argv(lambda: self._commands.stop(service), step), step)

    def start(self, service: str) -> None:
        """Start the provider's stopped service again."""
        step = "start the service"
        self._run(self._argv(lambda: self._commands.start(service), step), step)

    def observe(self, service: str) -> Observation:
        """Read whether the service runs and its identity; raise DriverError when the status cannot be read."""
        done = self._run(self._argv(lambda: self._commands.status(service), STATUS), STATUS)
        return parse_status(done.stdout)

    def serves(self, service: str) -> bool:
        """Whether the service serves now: it runs and the probe finds its provider answering its REST API. The probe
        is not asked of a service that does not run. Raises DriverError when the status cannot be read."""
        if not self.observe(service).running:
            return False
        return self._probe(service) is True

    def _exec(self, service: str, argv: Sequence[str]) -> list[str]:
        """The argv that runs `argv` inside the service; raise DriverError when the service cannot be addressed, before
        anything was sent to it."""
        return self._argv(lambda: self._commands.exec(service, argv), "exec into the service")

    def ping(self, service: str) -> bool:
        """Whether the op-worker node in the service answers `pong` to its runner's `ping`. A command that fails,
        times out or prints anything else is no pong; raise DriverError only when the service cannot be addressed."""
        argv = self._exec(service, [OP_WORKER, "ping"])
        try:
            done = self._runner(argv, timeout_s=self._timeout_s)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return done.returncode == 0 and done.stdout.strip() == "pong"

    def restart_link(self, service: str) -> str:
        """Restart the rtransfer link of the op-worker node in the service, once, and return how it ended as a word
        of `compose.LINK_RESULTS`; never the command's output, which names the node. Raise DriverError only when the
        service cannot be addressed, before anything was sent to it."""
        argv = self._exec(service, ["bash", "-c", RESTART_LINK_SCRIPT])
        try:
            done = self._runner(argv, timeout_s=RPC_TIMEOUT_S + RESTART_LINK_MARGIN_S)
        except subprocess.TimeoutExpired:
            return "command_timeout"  # the command was killed; the RPC may still be running on the node
        except OSError:
            return "not_run"
        return link_result(done)

    # --- the REST side, the probe's as on Compose (`ComposeDriver`) ------------------------------------------------

    def restored_links(self, service: str) -> str | None:
        """The link check of every pair, once a restored service serves (O35): None when each passed, or when the
        probe checks no link, else why one did not."""
        links = getattr(self._probe, "links", None)
        return None if links is None else links(service)

    def link_matrix(self, service: str) -> "LinkMatrix | None":
        """The link check of every ordered pair as a matrix, which the heal reads; None when the probe checks none."""
        matrix = getattr(self._probe, "matrix", None)
        return None if matrix is None else matrix(service)

    def arm_gate(self, service: str) -> str | None:
        """Before an injection: the id of the session gate's probe file; None when the probe has no session gate."""
        arm = getattr(self._probe, "arm", None)
        return None if arm is None else arm(service)

    def active_transfers(self) -> int | None:
        """How many transfer listings show a job not yet ended; None when the probe cannot tell."""
        transfers = getattr(self._probe, "transfers", None)
        return None if transfers is None else transfers()

    @property
    def has_sessions(self) -> bool:
        """Whether the probe has a session gate: without one the restore records no gate reading."""
        return getattr(self._probe, "sessions", None) is not None

    def sessions_ready(self, service: str, gate_probe: str | None) -> str | None:
        """One reading of the session gate for the provider behind a service: None when it passes, else why not."""
        sessions = getattr(self._probe, "sessions", None)
        return None if sessions is None else sessions(service, gate_probe)


def parse_status(stdout: str) -> Observation:
    """A status command's answer as an observation: exactly one JSON line `{"running": <bool>, "identity": <str or
    null>}` (other keys are left alone), the identity one word of `IDENTITY`; raise DriverError, naming only the step,
    for anything else, never echoing what was printed."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    unreadable = DriverError(f"the command to {STATUS} answered something unreadable")
    if len(lines) != 1:
        raise unreadable
    try:
        record = json.loads(lines[0])
    except ValueError:
        raise unreadable from None
    if not isinstance(record, dict) or not isinstance(record.get("running"), bool) or "identity" not in record:
        raise unreadable
    identity = record["identity"]
    if identity is not None and not (isinstance(identity, str) and IDENTITY.fullmatch(identity)):
        raise unreadable
    return Observation(record["running"], identity)
