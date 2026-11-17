# Provenance: new
"""An HPC federation as the fault injector's driver: a provider's Apptainer service stopped, started, read and exec'd
into (O11, O12, O35).

`HpcDriver` implements `expcore_onedata.fault_recovery.FaultDriver`, and the heal's commands, for a federation whose
providers run as setuid Apptainer services on batch nodes (deploy/hpc-federation, DESIGN.md section 7). It has the
Compose driver's surface (`expcore_onedata.compose.ComposeDriver`), so the injector, its journal, the restore and its
settle, the recovery, the heal and the fault gate run on it unchanged: the same lines journalled in the same order,
the same waits, the same words, the same fail-closed rules. Only how a verb reaches a provider differs.

**Four verbs, as requests.** The driver asks for one of four verbs on a provider's service, by its layout name: `status`
(read-only: whether it runs, its identity, whether its own checks passed), `stop` (the service stopped, its work
directory, data and registration kept), `start` (the same service started again from them) and `exec`, which runs one
command of a fixed set (`EXEC_COMMANDS`: the heal's `ping` and `restart_link`) inside the running service; never a
free-form command. Each `Request` goes through a `Transport`: `VerbCommand` runs the federation's verbs
(`hpcfed.py <verb> <service>`) as a subprocess, which only the cluster's login node can do, and
`expcore_onedata.fault_relay.RelayClient` hands the request to the relay that runs them there, from a campaign that
runs on a node. A transport raises `subprocess.TimeoutExpired` for a request with no answer in its bound (its outcome
unknown, never taken as done), OSError for one it could not send, and DriverError for one it refused.

**The identity** is the service's own, as `status` prints it, one JSON line with `running` (a bool), `identity` (a word,
or null) and `ready` (a bool, optional): stable while the service is left alone, and changed by every start of a stopped
service (`<launch>:<pid>:<start tick>`), so a restore that happened always changes it and one that did not keeps it
(O11). A status that fails, times out, cannot be sent or answers anything else is a reading that failed (DriverError),
never a guess. `ready` is the service script's own checks for this launch (the Couchbase guard, the rotation, the live
default and port checks), which run after its REST API first answers: a service running and not yet ready is read as a
container whose health check is still `starting`, so `serves` answers no, and a restore is never verified before them.

**The start's evidence (O58).** A restarted service waits, inside its readiness budget, for op_worker's domain, which
op_worker says null until its Onezone connection is up, before its identity is checked; the wait is part of the
provider's down time, which a live slot measures. Its status carries `domain_wait_s`, the seconds that wait took as the
node measured it (0 when none was needed), while the service is ready. The driver keeps the figure of the latest
status of a ready service as `last_domain_wait_s`, checked (a finite number of at least 0, else None; None too for a
status that is not ready, one that carries none, a status that cannot be read, and after a start), and the injector
records it beside `ready_after_s` in the restore. A status without it is no figure, never 0: only the node's own
measurement is.

**Where the time went (O58).** The relay reads the accounting after each verb and puts, in its answer, the time from
the verb's issue to each of its steps' accounted Start and from the answer to each End (`fault_relay_timing`), so a
controller stall during a scored batch is the site's in the record. The driver keeps the checked reading of each kind
of request (`last_timing`: `stop`, `start`, `status`, `ping`, `restart_link`; the latest, and none for a request that
was refused or never sent), from an answer and from a request that timed out too, and the injector journals it where
the verb's own evidence goes: a stop's with the stop's evidence (`stop.timing`), a start's in the restore
(`start_timing`), a link restart's in the heal's action (`timing`). A figure the relay could not read is None with its
reason, never a 0, and no timing fails or changes a verb.

**The stop's own bound.** A service's stop waits for its graceful end, then the kill, then the step's end: up to
`stop_seconds` plus about 90 s, 140 s with the layout's default of 50 s (DESIGN.md section 7 (e)), and the stop's
control step may wait to start before that (`step_launch_wait_s`, 240 s). The driver bounds `stop` by `stop_timeout_s`
(`[faults] settle_timeout_s` for this backend, refused below the two together at load), not by the command timeout: a
stop that runs past the command timeout would otherwise always be one of unknown outcome. A stop that does time out is
the Compose case: the injector journals it `stop_unknown` and the restore waits for it to show down (`faults.py`, "A
stop of unknown outcome"). The launch wait widens this timeout only: it never lengthens a stop or an outage.

**The stop's evidence.** The `stop` verb prints whether the stop was confirmed (`stopped`), how it ended (`ended`: the
service's own exit, the kill at the bound, or none, nothing to stop), how long the verb took from the request to its
verdict (`seconds`) and how long the service took to end after its SIGTERM, as the node measured it
(`service_seconds`). The driver keeps them, checked, as `last_stop`, from every stop that answered, confirmed or not,
and the injector journals them with the stop's outcome (`injected`, or `stop_unknown` for a stop that answered
unconfirmed) and records them in its snapshot, so a live slot's journal holds every stop's real duration, which a
measured `[hpc] stop_seconds` comes from. A stop with no answer has none.

**The heal (O35).** `ping` runs op-worker's runner (`/usr/sbin/op_worker ping`) inside the provider's service, and
`restart_link` runs the Compose driver's very command, `compose.RESTART_LINK_SCRIPT` (rtransfer_config's restart_link
through nodetool's rpcterms), inside it, so env.sh finds the service's own epmd port and cookie and none is on a
command line. Its bound is its own inner worst case, the exec step's launch wait and bound, and
`HPC_VERB_MARGIN_S` (`verb_bounds`), which the heal's reserves count (`fault_settings.link_command_s`); a ping's is
the same. Its result is the same word (`compose.link_result`); a failure the
exec does not describe, a step that could not be made among them, may have reached the node, so it is never `not_run`
and never retried. **The container fallback** is the heal's restart of a reader through the injector's own stop and
start (`fault_restore.LinkSettling.restart_container`), which here is the provider's service stopped and started again,
journalled, verified and recovered exactly as on Compose.

**The REST side** (the session gate, the link check, the transfer listings, the gate's probe file) is the probe's, read
as the Compose driver reads it. No message holds a service name, a path or a command's output: a failure says which
step failed and how it ended.
"""

import json
import math
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from expcore_onedata.compose import (
    OP_WORKER,
    RESTART_LINK_SCRIPT,
    CommandResult,
    CommandRunner,
    DriverError,
    Observation,
    link_result,
    run_command,
)
from expcore_onedata.fault_relay_late import STEP_TAG_OPTION

if TYPE_CHECKING:  # the heal's module imports the Compose driver's, which this one shares
    from expcore_onedata.fault_matrix import LinkMatrix

VERBS = ("status", "stop", "start", "exec")
# The only commands an exec runs inside a service: the heal's two, as the Compose driver runs them.
EXEC_COMMANDS: Mapping[str, tuple[str, ...]] = {
    "ping": (OP_WORKER, "ping"),
    "restart_link": ("bash", "-c", RESTART_LINK_SCRIPT),
}
# The layout's stop_seconds (50: three provider outages on one HPC federation, 17 Nov, ended a service 26.1 to 33.1 s
# after its SIGTERM, the largest plus 50 %), supervise.py's kill tail (30 s) and the step's end (60 s).
HPC_STOP_BOUND_S = 140.0
# How long a control step may wait to start before its own work begins (`[faults] step_launch_wait_s`, the layout's
# `[hpc] step_launch_wait_s`). A step's creation can wait on the scheduler's controller, up to its message timeout
# (200 s on a site that sets it so, with no error); the three outages (17 Nov) showed relay verb steps starting 47 to
# 103 s after they were issued during a heal. 240 s is that 200 s plus 20 %. Every bound that waits on a step's client
# from its start adds it, and it widens timeouts only (how long the harness waits before failing closed), never an
# outage or a hold.
HPC_STEP_LAUNCH_WAIT_S = 240.0
# What a relay verb does besides waiting to start: its control step's own bound (60 s, `STATUS_TIMEOUT_S` of
# deploy/hpc-federation/hpcfed_control.py; a start waits 60 s for its service step to be listed).
HPC_VERB_WORK_S = 60.0
# What one relay verb (a status, a start, a ping) may take: [faults] command_timeout_s on this backend, a step's
# launch wait and the verb's work, 300 s, where a command had 120 s (Compose's, which keeps it) until one round trip
# took 104.6 s with launch waits of up to 103 s. `up` declares it in the platform file, derived from the launch wait.
HPC_COMMAND_TIMEOUT_S = HPC_STEP_LAUNCH_WAIT_S + HPC_VERB_WORK_S
HPC_POLL_S = 10.0  # every status is a Slurm step on the node's one control core: read every 10 s, not every 2

# The bounds of the deploy driver's control steps (`deploy/hpc-federation/hpcfed_control.py`), mirrored here because the
# core cannot import the deploy directory; a test pins each to its original. A verb's relay bound is derived from them.
HPC_STATUS_STEP_S = 60.0  # `STATUS_TIMEOUT_S`: the status step's own bound, beyond its launch wait
HPC_MOUNT_STEP_S = 30.0  # `MOUNT_CHECK_TIMEOUT_S`: a restarted shared-import owner's read-only proof
HPC_START_LISTED_S = 60.0  # `START_LISTED_WAIT_S`: a start's wait for its new service step to be listed
HPC_EXEC_STEP_S = 300.0  # `EXEC_TIMEOUT_S`: the exec step's own bound, whatever the command
HPC_STOP_STEP_MARGIN_S = 60.0  # `STOP_MARGIN_S`: the stop step's bound past `stop_seconds`
HPC_STEP_GONE_S = 60.0  # `STEP_GONE_WAIT_S`: a stop's wait for the service step to leave the list
HPC_CANCEL_WAIT_S = 10.0  # `CANCEL_WAIT_S`: and for a step it cancelled to leave it
HPC_STOP_EXTRA_S = (
    90.0  # `stop_bound_s` beyond `stop_seconds`: supervise.py's kill tail (30 s) and the step's end (60 s)
)
HPC_VERB_MARGIN_S = 30.0  # what a verb's relay bound keeps beyond its inner worst case


def verb_inner_worst_s(step_launch_wait_s: float, stop_bound_s: float) -> dict[str, float]:
    """Each verb's own longest run when every step it makes takes its whole bound and every wait its whole wait, the
    launch wait counted in each step: a status then a restarted owner's mount check; a start after an unrecorded stop (a
    status step, then the wait to see its service step listed); a stop (its control step, the wait for the step to
    leave the list, the wait after a cancel); an exec, which `hpcfed_control` bounds alike for every command, a ping and
    a link restart too. 570 s, 600 s, 420 s, 540 s and 540 s with the defaults."""
    launch, status_step = step_launch_wait_s, step_launch_wait_s + HPC_STATUS_STEP_S
    stop_seconds = max(stop_bound_s - HPC_STOP_EXTRA_S, 0.0)
    return {
        "status": status_step + launch + HPC_MOUNT_STEP_S,
        "start": status_step + launch + HPC_START_LISTED_S,
        "stop": stop_seconds + launch + HPC_STOP_STEP_MARGIN_S + HPC_STEP_GONE_S + HPC_CANCEL_WAIT_S,
        "ping": launch + HPC_EXEC_STEP_S,
        "restart_link": launch + HPC_EXEC_STEP_S,
    }


def verb_bounds(
    *, step_launch_wait_s: float, stop_bound_s: float, command_timeout_s: float, settle_timeout_s: float
) -> dict[str, float]:
    """The bound the relay gives each verb: its own inner worst case (`verb_inner_worst_s`) and `HPC_VERB_MARGIN_S`, and
    never less than the declared floor, `command_timeout_s` for a status, a start and an exec, `settle_timeout_s` for a
    stop. 600 s, 630 s, 450 s, 570 s and 570 s with the defaults. A relay that cut a verb short would time out one
    that was about to complete, and a restore after a stop of unknown outcome would fail exactly when it matters."""
    inner = verb_inner_worst_s(step_launch_wait_s, stop_bound_s)
    bounds = {verb: max(command_timeout_s, worst + HPC_VERB_MARGIN_S) for verb, worst in inner.items()}
    bounds["stop"] = max(settle_timeout_s, inner["stop"] + HPC_VERB_MARGIN_S)
    return bounds


IDENTITY = re.compile(r"[A-Za-z0-9@:._+-]{1,200}")  # an identity as a status prints it: one word, never a path
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")  # a layout's service name, as `[faults.services]` holds it
STOP_ENDS = ("exit", "kill", "none")
STATUS = "read the service's status"


@dataclass(frozen=True)
class Request:
    """One verb on one provider's service; `command` names one of `EXEC_COMMANDS` for an exec, and is None otherwise."""

    verb: str
    service: str
    command: str | None = None

    def __post_init__(self) -> None:
        if self.verb not in VERBS or not SERVICE.fullmatch(self.service):
            raise DriverError("the request names no verb of the federation, or no service name")
        if (self.verb == "exec") != (self.command in EXEC_COMMANDS):
            raise DriverError("an exec names one command of the allowed set, and no other verb names one")

    def to_json(self) -> dict[str, str]:
        return {
            "verb": self.verb,
            "service": self.service,
            **({} if self.command is None else {"command": self.command}),
        }


class Transport(Protocol):
    """How a request reaches the federation's verbs: answer it as a command's end, or raise `subprocess.TimeoutExpired`
    (no answer within `timeout_s`: its outcome unknown), OSError (it could not be sent) or DriverError (refused)."""

    def __call__(self, request: Request, *, timeout_s: float) -> CommandResult: ...


class VerbCommand:
    """The federation's verbs as commands, `[*prefix, <verb>, <service>]` and, for an exec, `[*prefix, "exec",
    <service>, "--", *EXEC_COMMANDS[command]]`, run through a command runner (no shell). The relay's transport, on the
    login node; `prefix` is how `up` runs `hpcfed.py` for its own federation."""

    def __init__(self, prefix: Sequence[str], runner: CommandRunner = run_command) -> None:
        if not prefix or not all(isinstance(word, str) and word for word in prefix):
            raise ValueError("the verbs' command is a non-empty argv")
        self._prefix, self._runner = list(prefix), runner

    def argv(self, request: Request, step_tag: str | None = None) -> list[str]:
        """The verb's argv; with `step_tag` (the relay's), `--step-tag <tag>` follows the prefix, so the verb names the
        control steps it launches by it (`fault_relay_late`)."""
        head = self._prefix if step_tag is None else [*self._prefix, STEP_TAG_OPTION, step_tag]
        if request.verb == "exec":
            assert request.command is not None  # `Request` holds it for an exec
            return [*head, "exec", request.service, "--", *EXEC_COMMANDS[request.command]]
        return [*head, request.verb, request.service]

    def __call__(self, request: Request, *, timeout_s: float, step_tag: str | None = None) -> CommandResult:
        return self._runner(self.argv(request, step_tag), timeout_s=timeout_s)


class HpcDriver:
    """Stop, start, read and exec into providers' Apptainer services through a transport (see the module docstring)."""

    def __init__(
        self,
        transport: Transport,
        *,
        timeout_s: float = HPC_COMMAND_TIMEOUT_S,
        stop_timeout_s: float | None = None,
        link_timeout_s: float | None = None,
        status_timeout_s: float | None = None,
        start_timeout_s: float | None = None,
        ping_timeout_s: float | None = None,
        probe: Callable[[str], bool],
    ) -> None:
        """`probe(service)` says whether the provider behind a service answers its REST API: True when it does, False
        when it does not (yet). It is required: the driver's `serves` is not defined without it. `timeout_s` bounds
        every request, and each verb has a bound of its own where given (`verb_bounds`): `stop_timeout_s`,
        `link_timeout_s`, `status_timeout_s`, `start_timeout_s` and `ping_timeout_s`, each `timeout_s` when None."""
        self._transport, self._timeout_s, self._probe = transport, timeout_s, probe
        self._stop_timeout_s = timeout_s if stop_timeout_s is None else stop_timeout_s
        self._link_timeout_s = timeout_s if link_timeout_s is None else link_timeout_s
        self._status_timeout_s = timeout_s if status_timeout_s is None else status_timeout_s
        self._start_timeout_s = timeout_s if start_timeout_s is None else start_timeout_s
        self._ping_timeout_s = timeout_s if ping_timeout_s is None else ping_timeout_s
        self.last_stop: dict[str, Any] | None = None  # the latest stop's evidence, once it answered (see the docstring)
        self.last_domain_wait_s: float | None = (
            None  # the latest ready status's `domain_wait_s`, if any (the docstring)
        )
        self.last_timing: dict[str, dict[str, Any]] = {}  # the latest checked timing of each kind of request

    def _via_transport(self, request: Request, timeout_s: float) -> CommandResult:
        """Send a request through the transport and keep the relay's timing of it (`last_timing`), from its answer or
        from the timeout that was its answer; every exception goes on as it came."""
        kind = request.command if request.verb == "exec" and request.command else request.verb
        self.last_timing.pop(kind, None)  # a request that never ran leaves no earlier request's figures
        try:
            done = self._transport(request, timeout_s=timeout_s)
        except subprocess.TimeoutExpired as error:
            self._keep_timing(kind, getattr(error, "timing", None))
            raise
        self._keep_timing(kind, getattr(done, "timing", None))
        return done

    def _keep_timing(self, kind: str, timing: Any) -> None:
        kept = timing_evidence(timing)
        if kept is not None:
            self.last_timing[kind] = kept

    def _ask(self, request: Request, step: str, timeout_s: float) -> CommandResult:
        """Send a request; raise DriverError, naming only the step, when it is refused, cannot be sent, has no answer in
        its bound, or its command failed."""
        try:
            done = self._via_transport(request, timeout_s)
        except subprocess.TimeoutExpired:
            raise DriverError(f"the command to {step} did not finish within {timeout_s:g} s") from None
        except OSError as error:
            raise DriverError(f"the command to {step} could not run ({type(error).__name__})") from None
        if done.returncode != 0:
            raise DriverError(f"the command to {step} failed with exit code {done.returncode}")
        return done

    def stop(self, service: str) -> None:
        """Stop the provider's service, keeping its work directory, data and registration; keep the stop's evidence
        (`last_stop`) from its answer whether or not it was confirmed, None when there is no answer or it does not
        say. An answer that is not a success raises DriverError as before: the evidence is kept first."""
        self.last_stop = None
        step, request = "stop the service", Request("stop", service)
        try:
            done = self._via_transport(request, self._stop_timeout_s)
        except subprocess.TimeoutExpired:
            raise DriverError(f"the command to {step} did not finish within {self._stop_timeout_s:g} s") from None
        except OSError as error:
            raise DriverError(f"the command to {step} could not run ({type(error).__name__})") from None
        self.last_stop = stop_evidence(done.stdout)
        if done.returncode != 0:
            raise DriverError(f"the command to {step} failed with exit code {done.returncode}")

    def start(self, service: str) -> None:
        """Start the provider's stopped service again. The figure of an earlier launch is gone: this one has not
        reported."""
        self.last_domain_wait_s = None
        self._ask(Request("start", service), "start the service", self._start_timeout_s)

    def observe(self, service: str) -> Observation:
        """Read whether the service runs, its identity and whether it is ready; raise DriverError when the status
        cannot be read. `last_domain_wait_s` is the figure of this status when the service is ready, else None."""
        self.last_domain_wait_s = None
        stdout = self._ask(Request("status", service), STATUS, self._status_timeout_s).stdout
        seen = parse_status(stdout)
        self.last_domain_wait_s = status_domain_wait(stdout) if seen.health == "healthy" else None
        return seen

    def serves(self, service: str) -> bool:
        """Whether the service serves now: it runs, its own checks passed for this launch (when its status says), and
        the probe finds its provider answering its REST API. The probe is not asked of a service that does not run or
        is not ready. Raises DriverError when the status cannot be read."""
        seen = self.observe(service)
        if not seen.running or seen.health not in (None, "healthy"):
            return False
        return self._probe(service) is True

    def _exec(self, service: str, command: str, timeout_s: float) -> CommandResult:
        """Run one allowed command inside the service; raise `subprocess.TimeoutExpired` or OSError as the transport
        does, and DriverError when the request is refused before anything was sent."""
        return self._via_transport(Request("exec", service, command), timeout_s)

    def ping(self, service: str) -> bool:
        """Whether the op-worker node in the service answers `pong` to its runner's `ping`. A command that fails,
        times out or prints anything else is no pong; raise DriverError only when the request is refused."""
        try:
            done = self._exec(service, "ping", self._ping_timeout_s)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return done.returncode == 0 and done.stdout.strip() == "pong"

    def restart_link(self, service: str) -> str:
        """Restart the rtransfer link of the op-worker node in the service, once, and return how it ended as a word
        of `compose.LINK_RESULTS`; never the command's output, which names the node. Raise DriverError only when the
        request is refused, before anything was sent."""
        try:
            done = self._exec(service, "restart_link", self._link_timeout_s)
        except subprocess.TimeoutExpired:
            return "command_timeout"  # no answer: the RPC may still be running on the node
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


def _record(stdout: str) -> dict[str, Any] | None:
    """The one JSON object a verb printed, or None for anything else."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    try:
        record = json.loads(lines[0])
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def parse_status(stdout: str) -> Observation:
    """A status's answer as an observation: exactly one JSON line with `running` (a bool), `identity` (one word of
    `IDENTITY`, or null) and, optionally, `ready` (a bool: `healthy` when true, `starting` when false); other keys are
    left alone. Raise DriverError, naming only the step, for anything else, never echoing what was printed."""
    record = _record(stdout)
    unreadable = DriverError(f"the command to {STATUS} answered something unreadable")
    if record is None or not isinstance(record.get("running"), bool) or "identity" not in record:
        raise unreadable
    identity, ready = record["identity"], record.get("ready")
    if identity is not None and not (isinstance(identity, str) and IDENTITY.fullmatch(identity)):
        raise unreadable
    if ready is not None and not isinstance(ready, bool):
        raise unreadable
    health = None if ready is None else ("healthy" if ready else "starting")
    return Observation(record["running"], identity, health)


_TIMING_WORDS = ("status", "mounts", "stop", "exec", "run")
_TIMING_ID = re.compile(r"[0-9]{1,12}(?:\+[0-9]{1,6})?\.[0-9]{1,9}")
_TIMING_REASON = re.compile(r"[A-Za-z0-9_]{1,60}")
_TIMING_FIGURES = ("start_after_issue_s", "end_after_answer_s")


def _seconds_figure(value: Any) -> float | None:
    """A finite number (it may be negative: a step ended before the answer), rounded to the millisecond, else None."""
    ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    return round(float(value), 3) if ok else None


def timing_evidence(timing: Any) -> dict[str, Any] | None:
    """The relay's `timing` of a request (`fault_relay_timing`), checked, as the injector journals it: how the
    accounting was read (`accounting`, and `reason` when it was not), and for each of up to eight steps its word, id
    and state and the two figures (`start_after_issue_s`, `end_after_answer_s`: finite numbers or None) with, for each
    that is None, why. Names are left out (they carry the allocation's). None when there is nothing of the sort."""
    if not isinstance(timing, Mapping) or timing.get("accounting") not in ("read", "unreadable", "skipped"):
        return None
    if not isinstance(timing.get("steps"), list):
        return None
    reason = timing.get("reason")
    steps = []
    for raw in (timing.get("steps") if isinstance(timing.get("steps"), list) else [])[:8]:
        if not isinstance(raw, Mapping) or raw.get("word") not in _TIMING_WORDS:
            continue
        unknown = raw.get("unknown") if isinstance(raw.get("unknown"), Mapping) else {}
        step: dict[str, Any] = {
            "word": raw["word"],
            "id": raw["id"] if isinstance(raw.get("id"), str) and _TIMING_ID.fullmatch(raw["id"]) else None,
            "state": raw["state"]
            if isinstance(raw.get("state"), str) and re.fullmatch(r"[A-Z_]{1,32}", raw["state"])
            else None,
        }
        for figure in _TIMING_FIGURES:
            step[figure] = _seconds_figure(raw.get(figure))
            why = unknown.get(figure)
            if step[figure] is None:
                step.setdefault("unknown", {})[figure] = (
                    why if isinstance(why, str) and _TIMING_REASON.fullmatch(why) else "unknown"
                )
        steps.append(step)
    return {
        "accounting": timing["accounting"],
        "reason": reason if isinstance(reason, str) and _TIMING_REASON.fullmatch(reason) else None,
        "resolution_s": 1,
        # the verb's issue and its answer (epoch seconds): a later step's first figure counts from the issue, so it
        # includes what the verb did before launching it, which these two let a reader take out
        "issued_wall": _seconds_figure(timing.get("issued_wall")),
        "answered_wall": _seconds_figure(timing.get("answered_wall")),
        "steps": steps,
    }


def _duration(value: Any) -> float | None:
    """A finite number of at least 0, rounded to the millisecond; None for anything else."""
    numeric = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    return round(float(value), 3) if numeric and value >= 0 else None


def status_domain_wait(stdout: str) -> float | None:
    """The seconds a start waited for the provider's domain (O58), from a status's answer: `domain_wait_s`, a finite
    number of at least 0, else None. A status that does not carry it, or carries anything else, has no figure."""
    record = _record(stdout)
    return None if record is None else _duration(record.get("domain_wait_s"))


def stop_evidence(stdout: str) -> dict[str, Any] | None:
    """A stop's evidence, from the stop verb's answer: `stopped` (a bool, or None), `ended` (a word of `STOP_ENDS`, or
    None), `seconds` (from the request to the verb's verdict) and `service_seconds` (the service's own end after its
    SIGTERM, as the node measured it), each a finite number of at least 0 or None; None when the answer holds none of
    them. Nothing else the answer holds is kept (it can name a node)."""
    record = _record(stdout)
    if record is None:
        return None
    stopped, ended = record.get("stopped"), record.get("ended")
    evidence = {
        "stopped": stopped if isinstance(stopped, bool) else None,
        "ended": ended if ended in STOP_ENDS else None,
        "seconds": _duration(record.get("seconds")),
        "service_seconds": _duration(record.get("service_seconds")),
    }
    return None if all(value is None for value in evidence.values()) else evidence
