"""The four service verbs of an HPC federation (stop, start, status, exec), as logic over M1's step launcher and state
store (DESIGN.md, section 7).

Written, not run: nothing here has run against a cluster. `hpcfed.py` supplies the two objects (`hpclaunch.Launcher`
and `hpclaunch.StateStore`) and calls this module; the tests run it against those very objects over the driver tests'
fake Slurm, and against fakes of the two. Python 3.8 syntax, the standard library and the driver's own modules only.

The hook, in `hpcfed.py`'s command dispatch:

    if verb in hpcfed_control.VERBS:
        return hpcfed_control.run(verb, service, argv, launcher=launcher, state=state, stop_seconds=stop_seconds,
                                  mounts=mounts)

`run(verb, service, argv, *, launcher, state, stop_seconds=600, mounts=None, out=None, err=None)` returns the exit
status. `argv` is what followed `--` for exec, and empty for the other verbs; `out` and `err` are this process's
standard output and error when None.

**The launcher** (`hpclaunch.Launcher`, DESIGN.md section 3):

- `control(node, argv, *, name, timeout_s)`: a control step on `node`, bounded; returns an object with `returncode`,
  `stdout` and `stderr`; raises `TimeoutError` (`hpcslurm.StepTimeout`) at its bound, after cancelling the step by its
  id. `returncode` is None when the step's end is not known (its client was ended after the step vanished, and the
  accounting holds no status), never a success; a step a signal ended reads `128 + signal`.
- `service(node, argv, *, kind, name, log)`: a detached service step; it records its srun client in the state
  (`clients`, by the step's name) and returns nothing.
- `listed(name)`: the id of the step of that name in the held job's step list, or None.
- `cancel(step_id)`: cancel that one step.

**The state** (`hpclaunch.StateStore`, DESIGN.md section 4): `read()` returns the state file's dict; `update(fn)` is a
locked, atomic read-modify-write (`fn` takes the dict and returns it; an exception from `fn` writes nothing) that
returns the new dict. This module reads `allocation.name`, `allocation.ends` (Slurm's `EndTime` as `up` recorded it:
an ISO 8601 time with no zone is the login node's local time, as `scontrol` printed it; one with a `Z` or an offset,
or epoch seconds, is read as such), `site` (`node_tmp`, `logs`, `node_python`), `clients` and, for the service,
`kind`, `node`, `workdir`, `launch`, `step` (`name`, `id`), `state` and `mounts_proven`; it writes `launch`, `step`,
`log`, `state`, `stops` and `mounts_proven`.

**Which services.** The service must be a key of `services` with kind `op` or `oz`, and its work directory one
`hpcstate.checked_workdir` accepts for that kind (directly under the site's node tmpfs, `hpcop-<op|oz>.XXXXXX`).
Anything else is exit 2 with a message that names neither a node nor a path: the agent's node among them (`sandbox`,
kind `sandbox`), which no verb ever acts on.

**One stop or start at a time.** A stop or a start holds `VERBS_LOCK` in the federation's root (the state store's
`root`) from the state read that decides it to its last record, never waiting for it: a second one meanwhile is exit 1
and runs nothing, so two processes never launch two service steps on one work directory. Status and exec take no lock.

**status**: one JSON line `{"service", "state", "running", "identity", "launch", "node", "step", "ready"}`. `running`
is true only when the service step is listed **and** a control step (`bundle/status.sh`, bounded to 60 s) finds the
recorded process alive. `identity` is `"<launch>:<pid>:<start>"` while running, else null. `ready` is running and the
service's own ready marker (written once its checks passed) and, for an owner of shared imported trees, the proof of
this launch that it holds them read-only (below). A ready launch's status also carries `"domain_wait_s"` when the node
reported one (O58): the seconds its start waited for op_worker's domain, which op_worker says null until its Onezone
connection is up; 0 when no wait was needed, left out when none was reported or it is not a finite number of at
least 0, so a status never holds a figure nobody measured. A control step that fails or times out is exit 1 with
nothing on stdout.

**The read-only proof of a restarted owner (O59, M3).** `mounts` is None in a core without the shared import; else it
has `spaces(service)` (the shared trees the service owns, by space name; empty for a service that owns none),
`argv(workdir, python)` (the check's control step, `hpcimport.mount_check_argv`) and `problems(report, spaces)` (what
its report leaves unproven, `hpcimport.mount_check_problems`). `up` records `services.<n>.mounts_proven` once an owner
passed the check. A start clears it (false) in the same state change that claims the launch, and a status that finds
the restarted owner otherwise ready runs the check on its node (a control step, bounded to `MOUNT_CHECK_TIMEOUT_S`)
and records the proof only for the launch it read; until then `ready` is false, so a restarted owner never reads as
ready, and never serves the restore, before its trees are proven read-only again. The check runs there and not in the
start because a start returns once its step is listed, before the restarted service holds its mounts. A service whose
record carries `mounts_proven` with no `mounts` to check it is never ready.

**stop** records `stopping` in the state before anything runs, then runs `bundle/teardown.sh <kind> --workdir W --keep
--keep-until <allocation end> --timeout <stop_seconds>` as a control step (bounded to `stop_seconds` + 60), waits (about
60 s) for the service step to leave `listed`, and records `stopped` only when teardown exited 0 and the step is gone
(and, for a step never seen listed, its srun client has ended too, so no pending step can still be made), and only for
the launch it stopped. Otherwise it exits 1 and leaves `stopping`. A step still listed at the bound is
cancelled (by its id, never more) only when teardown exited 0: a step left behind by a stop that did run. After a
teardown that failed it cancels nothing, because the keep marks may not have been written, and a cancelled batch
script without them removes the work directory; the stop is simply tried again (it is idempotent). It prints one JSON
line, the evidence of the stop, and appends the same object to `stops` in the state: `{"service", "stopped", "ended",
"seconds", "service_seconds", "requested_utc", "confirmed_utc"}`. `ended` is read from teardown's `hpcop-stop` line
(`exit`, `kill` or `none`; null when unreadable) and `service_seconds` from the same line: how long the service took to
end after its SIGTERM, as teardown measured it on the node (null when unreadable). `seconds` runs from just before the
control step to the verb's verdict: the step confirmed gone, or, for a stop not confirmed, the verb giving up; so every
stop's duration is kept, and `stopped` says which it was. A service already `stopped` whose step is not listed answers
`ended: "none"` and `seconds: 0` at once and changes nothing.

**start** of a service whose step is listed and whose process is alive launches nothing and exits 0 (recording the
step's id and `running` for a start whose step was listed after its wait); a listed step whose process is not alive is
exit 1 (stop it first). Otherwise the service may be started from `stopped`, and from
`stopping`, `starting` or `running` once a status reading finds its process not alive (a stop whose verdict was never
recorded, a start whose step ended or never came, a service that ended), but never while an earlier start's step was
never listed and its srun client still runs (`clients`: a launch in flight), and never from any other state. Then the
state gets `launch` + 1, the step `{"name": "<allocation>-<service>-run-<launch>", "id": null}`, its log (in the site's
log directory), `starting` and, for an owner, `mounts_proven` false, before the detached service step
`bundle/restart.sh <kind> --workdir W` is launched; the step's id and `running` are recorded once it is listed (about 60
s). Not listed in time is exit 1, and the state stays `starting`. A provider of a federation whose storage is on scratch
(O57) is given `--storage=PATH` last, its own directory read from the state's record checked whole
(`hpcstorage.provider_args`), so its node binds exactly what the state names; a state that names a scratch root and no
record is refused (exit 1) before anything is recorded or launched.

**exec** runs `bundle/exec.sh <kind> --workdir W -- <argv>` as a control step (bounded to 300 s) and passes its stdout
and stderr through unchanged; the exit status is the command's (`128 + n` for a signal), 124 when the launcher timed out
or the step's end is not known, and 125 when the step could not run.
"""

import contextlib
import fcntl
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import hpcslurm
import hpcstate
import hpcstorage
from hpclaunch import STEP_NAME

VERBS = ("stop", "start", "status", "exec")
# The words of the control steps a verb launches (`Target.control_name`): the relay's late-step watch (`hpclate`) looks
# for exactly these, so a verb that gains a control step names its word here. A start's service step (`...-run-<n>`) is
# no control step: it is launched detached, recorded in the state, and never cancelled by name.
CONTROL_WORDS = {"status": ("status", "mounts"), "stop": ("stop",), "start": ("status",), "exec": ("exec",)}
STEP_TAG = re.compile(r"[0-9a-f]{12}")  # the fault relay's tag for one request's control steps (`--step-tag`)
KINDS = ("op", "oz")
BUNDLE = Path(__file__).resolve().parent / "bundle"
ENDED = ("exit", "kill", "none")
STARTABLE = ("stopped", "stopping", "starting", "running")  # what a start may follow, once nothing runs (see above)
MUTATING = ("stop", "start")  # one at a time per federation, under VERBS_LOCK
VERBS_LOCK = "hpc-federation.verbs.lock"  # in the root, beside M1's state lock and lifecycle lock

STATUS_TIMEOUT_S = 60  # the status control step's bound
EXEC_TIMEOUT_S = 300  # the exec control step's bound
MOUNT_CHECK_TIMEOUT_S = 30  # the read-only proof's control step (it reads the service's mounts, nothing more)
STOP_MARGIN_S = 60  # the stop's control step lives this long past teardown's own timeout
STEP_GONE_WAIT_S = 60  # how long a stop waits for the service step to leave the step list
CANCEL_WAIT_S = 10  # and for a step it cancelled to leave it
START_LISTED_WAIT_S = 60  # how long a start waits for the new service step to be listed
POLL_S = 2.0

_SLEEP = time.sleep
_MONOTONIC = time.monotonic
_STAMP = time.time


def _client_alive(client: Any) -> bool:
    """Whether a recorded srun client of this login node still runs: its pid holds its recorded start tick (M1's own
    test, `hpcslurm.Runner.client_alive`, over a client the state recorded)."""
    if not isinstance(client, dict):
        return False
    pid, start = client.get("pid"), client.get("start")
    if isinstance(pid, bool) or isinstance(start, bool) or not isinstance(pid, int) or not isinstance(start, int):
        return False
    return pid > 0 and start >= 0 and hpcslurm.proc_start(pid) == start


_CLIENT_ALIVE = _client_alive


def _in_flight(target: "Target") -> bool:
    """A launch in flight: the service's recorded step was never seen listed and its srun client still runs, so the
    step may yet be made (a pending step is not in the list)."""
    return target.step_id is None and _CLIENT_ALIVE(target.client)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def control_step_name(prefix: str, service: str, word: str, tag: Optional[str] = None) -> str:
    """`<allocation>-<service>-<word>-<tag>`, or with no tag the clock's millisecond in its place."""
    return "%s-%s-%s-%s" % (prefix, service, word, tag if tag is not None else int(_STAMP() * 1000))


class Refusal(Exception):
    """The request cannot be carried out as given: exit 2 (or 1 for a state that is not the one the verb needs)."""

    def __init__(self, message: str, status: int = 2) -> None:
        super().__init__(message)
        self.status = status


def _site_value(site: Dict[str, Any], key: str) -> str:
    value = site.get(key)
    if not isinstance(value, str) or not value:
        raise Refusal("the federation's state records no usable site value %s" % key)
    return value


class Target:
    """What the state file says about one service, validated."""

    def __init__(self, doc: Any, service: Any, mounts: Any = None, step_tag: Optional[str] = None) -> None:
        services = doc.get("services") if isinstance(doc, dict) else None
        allocation = doc.get("allocation") if isinstance(doc, dict) else None
        site = doc.get("site") if isinstance(doc, dict) else None
        record = services.get(service) if isinstance(services, dict) and isinstance(service, str) else None
        if not isinstance(record, dict) or not isinstance(allocation, dict):
            raise Refusal("no such service in the federation's state")
        if not isinstance(site, dict):
            raise Refusal("the federation's state records no site")
        kind, workdir, node = record.get("kind"), record.get("workdir"), record.get("node")
        if kind not in KINDS:
            raise Refusal("the service is neither an Oneprovider nor an Onezone")
        try:
            hpcstate.checked_workdir(workdir, _site_value(site, "node_tmp"), kind)
        except hpcstate.StateError:
            raise Refusal("the service's recorded work directory is not an hpcop work directory of its kind") from None
        if not isinstance(node, str) or not node or node != node.strip():
            raise Refusal("the service has no recorded node")
        prefix = allocation.get("name")
        if not isinstance(prefix, str) or not STEP_NAME.fullmatch("%s-%s-mounts-%d" % (prefix, service, 10**12)):
            raise Refusal("the allocation or the service has no name a step can carry")  # the longest name made here
        if step_tag is not None and not STEP_TAG.fullmatch(step_tag):
            raise Refusal("the step tag is not 12 hex digits")
        step = record.get("step")
        self.step_tag = step_tag
        self.service, self.kind, self.node, self.workdir = service, kind, node, workdir
        self.prefix, self.site, self.doc = prefix, site, doc
        self.ends = allocation.get("ends")
        self.state = record.get("state")
        self.launch = record.get("launch")
        self.step_name = step.get("name") if isinstance(step, dict) else None
        self.step_id = step.get("id") if isinstance(step, dict) else None
        clients = doc.get("clients")
        self.client = clients.get(self.step_name) if isinstance(clients, dict) and self.step_name else None
        self.proven = record.get("mounts_proven")
        self.marks_proof = "mounts_proven" in record  # only `up` of a core with the shared import writes it
        self.owned = [str(space) for space in mounts.spaces(service)] if mounts is not None else []

    def script(self, name: str, *args: str) -> List[str]:
        """The argv of one of the bundle's scripts for this service."""
        return ["bash", str(BUNDLE / name), self.kind, "--workdir", self.workdir, *args]

    def control_name(self, verb: str) -> str:
        """A control step's name, `<allocation>-<service>-<word>-<n>`, unique per call: `n` is the clock's millisecond,
        or, for a verb the fault relay runs, the relay's tag for the request (`--step-tag`), which no other request and
        no verb run by hand shares, so the relay's watch names this verb's steps exactly (`hpclate`)."""
        return control_step_name(self.prefix, self.service, verb, self.step_tag)

    @property
    def needs_proof(self) -> bool:
        """Whether the service is ready only with the read-only proof of its shared trees."""
        return bool(self.owned) or self.marks_proof


def _epoch(value: Any) -> int:
    """The allocation's end as epoch seconds; ValueError for anything else. A time with no zone is the login node's
    local time, as `scontrol` printed it there (`up` records Slurm's `EndTime` as it came)."""
    if isinstance(value, bool):
        raise ValueError("not a time")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"[0-9]{1,15}", text):
        return int(text)
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.astimezone()  # this host's local time, which is the zone scontrol printed in
    return int(moment.timestamp())


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _say(err: Any, message: str) -> None:
    err.write("hpcfed: %s\n" % message)
    err.flush()


def _emit(out: Any, record: Dict[str, Any]) -> None:
    out.write(json.dumps(record) + "\n")
    out.flush()


def _set(store: Any, service: str, **fields: Any) -> Dict[str, Any]:
    """Change fields of one service in the state file, atomically."""

    def change(doc: Dict[str, Any]) -> Dict[str, Any]:
        doc["services"][service].update(fields)
        return doc

    return store.update(change)


def _wait_gone(launcher: Any, name: Any, seconds: float) -> Optional[str]:
    """None once the step is not listed (at once if it never was); else the id it still has at the bound."""
    deadline = _MONOTONIC() + seconds
    while True:
        step_id = launcher.listed(name) if name else None
        if step_id is None:
            return None
        if _MONOTONIC() >= deadline:
            return str(step_id)
        _SLEEP(POLL_S)


def _wait_listed(launcher: Any, name: str, seconds: float) -> Optional[str]:
    deadline = _MONOTONIC() + seconds
    while True:
        step_id = launcher.listed(name)
        if step_id is not None:
            return str(step_id)
        if _MONOTONIC() >= deadline:
            return None
        _SLEEP(POLL_S)


def _seconds(value: Any) -> Optional[float]:
    """A duration as teardown printed it: a finite number of at least 0, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 3)


def _stop_line(stdout: str) -> Tuple[Optional[str], Optional[float]]:
    """`ended` and the service's own `seconds` from the last valid `hpcop-stop {...}` line of teardown's stdout; None
    for what no such line gives."""
    ended, seconds = None, None
    for line in stdout.splitlines():
        if not line.startswith("hpcop-stop "):
            continue
        try:
            record = json.loads(line[len("hpcop-stop ") :])
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("ended") in ENDED:
            ended, seconds = record["ended"], _seconds(record.get("seconds"))
    return ended, seconds


def _status_reading(target: Target, launcher: Any) -> Optional[Dict[str, Any]]:
    """The status control step's answer (alive, ready, pid, start); None when it failed, timed out or was unreadable.
    A step whose end is not known (`returncode` None) is one that failed."""
    try:
        done = launcher.control(
            target.node,
            target.script("status.sh"),
            name=target.control_name("status"),
            timeout_s=STATUS_TIMEOUT_S,
        )
    except TimeoutError:
        return None
    lines = [line for line in _text(done.stdout).splitlines() if line.strip()]
    if done.returncode != 0 or len(lines) != 1:
        return None
    try:
        reading = json.loads(lines[0])
    except ValueError:
        return None
    if not isinstance(reading, dict) or not isinstance(reading.get("alive"), bool):
        return None
    if reading["alive"]:  # a running service has the record an identity is made of
        pid, start = reading.get("pid"), reading.get("start")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            return None
        if not isinstance(start, str) or not re.fullmatch(r"[0-9]+", start):
            return None
    return reading


def _proven(target: Target, launcher: Any, state: Any, mounts: Any, err: Any) -> bool:
    """Whether a service that needs it holds the read-only proof of its shared trees for this launch: the recorded
    one, or a check run now on its node and recorded for the launch it read (see the module docstring)."""
    if target.proven is True:
        return True
    if mounts is None or not target.owned:
        _say(err, "the service's shared imported trees have no read-only proof, and this core cannot check them")
        return False
    argv = list(mounts.argv(target.workdir, _site_value(target.site, "node_python")))
    try:
        done = launcher.control(target.node, argv, name=target.control_name("mounts"), timeout_s=MOUNT_CHECK_TIMEOUT_S)
    except TimeoutError:
        _say(err, "the read-only check of the service's shared imported trees did not finish in time")
        return False
    except Exception as error:  # a check that could not run proves nothing (type only: a message may name a node)
        _say(
            err, "the read-only check of the service's shared imported trees could not run (%s)" % type(error).__name__
        )
        return False
    if done.returncode != 0:
        failed = [line for line in _text(done.stderr).splitlines() if line.startswith("FAIL: shared import ")]
        _say(err, "the service's shared imported trees are not proven read-only (status %s)" % (done.returncode,))
        for line in failed:  # the check's own lines name a space, never a path
            _say(err, line)
        return False
    problems = list(mounts.problems(_text(done.stdout), target.owned))
    if problems:
        _say(err, "the service's shared imported trees are not proven read-only: " + "; ".join(problems))
        return False

    def prove(doc: Dict[str, Any]) -> Dict[str, Any]:
        entry = doc["services"][target.service]
        if entry.get("launch") == target.launch and entry.get("state") == target.state:
            entry["mounts_proven"] = True  # for the launch this status read, never a later one
        return doc

    state.update(prove)
    return True


def _status(target: Target, launcher: Any, state: Any, mounts: Any, out: Any, err: Any) -> int:
    listed = launcher.listed(target.step_name) if target.step_name else None
    reading = _status_reading(target, launcher)
    if reading is None:
        _say(err, "the status of the service could not be read")
        return 1
    running = listed is not None and reading["alive"]
    identity = "%s:%s:%s" % (target.launch, reading.get("pid"), reading.get("start")) if running else None
    ready = bool(running and reading.get("ready") is True)
    if ready and target.needs_proof:
        ready = _proven(target, launcher, state, mounts, err)
    record = {
        "service": target.service,
        "state": target.state,
        "running": running,
        "identity": identity,
        "launch": target.launch,
        "node": target.node,
        "step": target.step_name,
        "ready": ready,
    }
    waited = _seconds(reading.get("domain_wait_s")) if ready else None
    if waited is not None:  # only a ready launch's, and only a figure the node measured (see the docstring)
        record["domain_wait_s"] = waited
    _emit(out, record)
    return 0


def _stop(target: Target, launcher: Any, state: Any, stop_seconds: int, out: Any, err: Any) -> int:
    if target.state == "stopped" and (not target.step_name or launcher.listed(target.step_name) is None):
        now = _stamp(_utcnow())
        _emit(
            out,
            {
                "service": target.service,
                "stopped": True,
                "ended": "none",
                "seconds": 0.0,
                "service_seconds": None,
                "requested_utc": now,
                "confirmed_utc": now,
            },
        )
        return 0
    try:
        until = _epoch(target.ends)
    except (ValueError, TypeError, IndexError, OverflowError, OSError):
        raise Refusal("the allocation has no usable end time to keep the work directory until") from None
    _set(state, target.service, state="stopping")  # recorded before the stop is attempted
    requested = _utcnow()
    started = _MONOTONIC()
    argv = target.script("teardown.sh", "--keep", "--keep-until", str(until), "--timeout", str(stop_seconds))
    ended, service_seconds, teardown_ok = None, None, False
    try:
        done = launcher.control(
            target.node, argv, name=target.control_name("stop"), timeout_s=stop_seconds + STOP_MARGIN_S
        )
        teardown_ok = done.returncode == 0  # None (an end not known) or any other status confirms nothing
        ended, service_seconds = _stop_line(_text(done.stdout))
    except TimeoutError:
        _say(err, "the stop's control step did not finish in time")
    except Exception as error:  # the launcher failed: the stop is unconfirmed, whatever the step did (type only)
        _say(err, "the stop's control step failed (%s)" % type(error).__name__)
    step_gone = False
    if teardown_ok:
        try:
            left = _wait_gone(launcher, target.step_name, STEP_GONE_WAIT_S)
            if left is not None:
                launcher.cancel(left)
                left = _wait_gone(launcher, target.step_name, CANCEL_WAIT_S)
            step_gone = left is None
        except Exception as error:  # the step list could not be read: the step is not confirmed gone
            _say(err, "the stop's service step could not be confirmed gone (%s)" % type(error).__name__)
        if step_gone and _in_flight(target):
            step_gone = False
            _say(err, "a start's step that was never listed may still be made: its srun client runs")
    stopped = teardown_ok and step_gone
    confirmed = _utcnow()
    evidence = {
        "service": target.service,
        "stopped": stopped,
        "ended": ended,
        "seconds": round(_MONOTONIC() - started, 3),
        "service_seconds": service_seconds,
        "requested_utc": _stamp(requested),
        "confirmed_utc": _stamp(confirmed) if stopped else None,
    }

    def record(doc: Dict[str, Any]) -> Dict[str, Any]:
        entry = doc["services"][target.service]
        entry.setdefault("stops", []).append(dict(evidence))
        if stopped and entry.get("launch") == target.launch:  # a start since then is not undone by this record
            entry["state"] = "stopped"
        return doc

    try:
        state.update(record)
    except Exception as error:  # the evidence is still printed; the state is not known to say stopped
        _emit(out, evidence)
        _say(err, "the stop could not be recorded in the federation's state (%s)" % type(error).__name__)
        return 1
    _emit(out, evidence)
    if not stopped:
        _say(err, "the service is not confirmed stopped: its state stays 'stopping'; stop it again")
    return 0 if stopped else 1


def _start(target: Target, launcher: Any, state: Any, err: Any) -> int:
    listed = launcher.listed(target.step_name) if target.step_name else None
    if listed is not None:
        reading = _status_reading(target, launcher)
        if reading is None:
            _say(err, "the status of the service could not be read")
            return 1
        if not reading["alive"]:
            raise Refusal("the service step is still listed though its process is not running: stop it first", 1)
        if target.state == "starting" and target.step_id is None:  # a start whose step was listed late

            def running_now(doc: Dict[str, Any]) -> Dict[str, Any]:
                entry = doc["services"][target.service]
                if entry.get("launch") == target.launch and entry.get("state") == "starting":
                    entry["step"] = {"name": target.step_name, "id": str(listed)}
                    entry["state"] = "running"
                return doc

            state.update(running_now)
        return 0  # running: nothing to start
    if target.state not in STARTABLE:
        raise Refusal("the service is '%s', which a start does not follow: stop it first" % (target.state,), 1)
    if _in_flight(target):
        raise Refusal("an earlier start of the service is still in flight (its step was never listed): wait", 1)
    if target.state != "stopped":  # no recorded stop: nothing may run before a second service is launched
        reading = _status_reading(target, launcher)
        if reading is None:
            _say(err, "the status of the service could not be read")
            return 1
        if reading["alive"]:
            raise Refusal("the service's process runs though its step is not listed: stop it first", 1)
    try:  # a provider's own storage on scratch (O57), read before anything is recorded; none on tmpfs or for the zone
        storage = hpcstorage.provider_args(target.doc, target.service) if target.kind == "op" else []
    except hpcstorage.StorageRefused as refusal:
        raise Refusal(str(refusal), 1) from None
    launch = int(target.launch or 0) + 1
    name = "%s-%s-run-%d" % (target.prefix, target.service, launch)
    if not STEP_NAME.fullmatch(name):
        raise Refusal("the service's next step would have no name a step can carry")
    log = str(Path(_site_value(target.site, "logs")) / (name + ".log"))

    def claim(doc: Dict[str, Any]) -> Dict[str, Any]:
        entry = doc["services"][target.service]
        if entry.get("state") != target.state or entry.get("launch") != target.launch:
            raise Refusal("the service changed while the start was read: try again", 1)
        entry.update(launch=launch, step={"name": name, "id": None}, log=log, state="starting")
        if target.needs_proof:
            entry["mounts_proven"] = False  # proven again for this launch before it is ready (see the docstring)
        return doc

    state.update(claim)  # recorded before the step is launched, so a crash leaves the attempt in the state
    launcher.service(
        target.node,
        ["bash", str(BUNDLE / "restart.sh"), target.kind, "--workdir", target.workdir, *storage],
        kind=target.kind,
        name=name,
        log=log,
    )
    step_id = _wait_listed(launcher, name, START_LISTED_WAIT_S)
    if step_id is None:
        _say(err, "the service step was not listed in time: its state stays 'starting'")
        return 1

    def listed_now(doc: Dict[str, Any]) -> Dict[str, Any]:
        entry = doc["services"][target.service]
        if entry.get("launch") == launch:
            entry["step"] = {"name": name, "id": step_id}
            entry["state"] = "running"
        return doc

    state.update(listed_now)
    return 0


def _exec(target: Target, launcher: Any, argv: Sequence[str], out: Any, err: Any) -> int:
    if not argv or not all(isinstance(word, str) for word in argv):
        raise Refusal("exec takes a command after --")
    try:
        done = launcher.control(
            target.node,
            target.script("exec.sh", "--", *argv),
            name=target.control_name("exec"),
            timeout_s=EXEC_TIMEOUT_S,
        )
    except TimeoutError:
        _say(err, "the command did not finish in time")
        return 124
    out.write(_text(done.stdout))
    out.flush()
    err.write(_text(done.stderr))
    err.flush()
    code = done.returncode
    if isinstance(code, bool) or not isinstance(code, int):
        _say(err, "the command's end is not known (its step's status could not be read)")
        return 124
    return 128 - code if code < 0 else code  # a client a signal ended reads as the shell reads it


@contextlib.contextmanager
def _exclusive(state: Any) -> Iterator[None]:
    """Hold the federation's verbs lock for a whole stop or start, never waiting for it: from the state read that
    decides it to its last record, so two of them (two processes of `hpcfed.py`) never both launch a service step on
    one work directory, nor a stop undo a start between its claim and its launch. M1's state lock is held only for one
    change of the state; the kernel lets this one go with a process that dies."""
    root = getattr(state, "root", None)
    if root is None:
        raise Refusal("the state store names no root to hold the verbs' lock in")
    descriptor = os.open(os.path.join(str(root), VERBS_LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("another stop or start of this federation is running: try again", 1) from None
        yield
    finally:
        os.close(descriptor)


def run(
    verb: str,
    service: str,
    argv: Sequence[str],
    *,
    launcher: Any,
    state: Any,
    stop_seconds: int = 600,
    mounts: Any = None,
    out: Any = None,
    err: Any = None,
    step_tag: Optional[str] = None,
) -> int:
    """Carry out one verb on one service; return the exit status (see the module docstring). `step_tag`: the fault
    relay's tag for this request, which names every control step the verb launches."""
    out = sys.stdout if out is None else out  # read at the call, so a caller that redirects them is followed
    err = sys.stderr if err is None else err
    try:
        if verb not in VERBS:
            raise Refusal("unknown verb '%s'" % (verb,))
        if isinstance(stop_seconds, bool) or not isinstance(stop_seconds, int) or stop_seconds < 1:
            raise Refusal("stop_seconds must be a positive whole number")
        with _exclusive(state) if verb in MUTATING else contextlib.nullcontext():
            try:
                doc = state.read()
            except Exception as error:  # an unreadable state names its path: only the error's type is told
                raise Refusal("the federation's state could not be read (%s)" % type(error).__name__) from None
            target = Target(doc, service, mounts, step_tag)
            if verb == "status":
                return _status(target, launcher, state, mounts, out, err)
            if verb == "stop":
                return _stop(target, launcher, state, stop_seconds, out, err)
            if verb == "start":
                return _start(target, launcher, state, err)
            return _exec(target, launcher, argv, out, err)
    except Refusal as refusal:
        _say(err, str(refusal))
        return refusal.status
    except Exception as error:  # a launcher or a state that failed: the type, never its message (it may name a node)
        _say(err, "%s failed (%s)" % (verb, type(error).__name__))
        return 125 if verb == "exec" else 1
