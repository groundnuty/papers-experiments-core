"""The four service verbs of an HPC federation (stop, start, status, exec), as pure logic over two injected objects.

Written, not run: nothing here has run against a cluster. M1's `hpcfed.py` supplies the two objects (its step launcher
and its state file) and calls this module; the tests give it fakes. Python 3.8 syntax, standard library only.

The hook, in `hpcfed.py`'s command dispatch:

    if verb in hpcfed_control.VERBS:
        return hpcfed_control.run(verb, service, argv, launcher=launcher, state=state, stop_seconds=stop_seconds)

`run(verb, service, argv, *, launcher, state, stop_seconds=600, out=sys.stdout, err=sys.stderr)` returns the exit
status. `argv` is what followed `--` for exec, and empty for the other verbs.

**The launcher** (the federation's step launcher, DESIGN.md section 3):

- `control(node, argv, *, name, timeout_s)`: a control step on `node`, bounded; returns an object with `returncode`,
  `stdout` and `stderr`; raises `TimeoutError` at its bound.
- `service(node, argv, *, kind, name, log)`: a detached service step; returns nothing.
- `listed(name)`: the id of the step of that name in the allocation's step list, or None.
- `cancel(step_id)`: cancel that one step.

**The state** (DESIGN.md section 4): `read()` returns the state file's dict; `update(fn)` is a locked, atomic
read-modify-write (`fn` takes the dict and returns it; an exception from `fn` writes nothing) that returns the new dict.
This module reads `allocation.name`, `allocation.ends` (UTC: an ISO 8601 time, with a `Z` or a numeric offset, naive
taken as UTC; or epoch seconds) and, for the service, `kind`, `node`, `workdir`, `launch`, `step` (`name`, `id`), `log`
and `state`; it writes `launch`, `step`, `log`, `state` and `stops`.

**Which services.** The service must be a key of `services` with kind `op` or `oz`, and its work directory must match
`/<tmpfs>/hpcop-<op|oz>.XXXXXX` for that kind (six alphanumerics). Anything else is exit 2 with a message that names
neither a node nor a path.

**status**: one JSON line `{"service", "state", "running", "identity", "launch", "node", "step", "ready"}`. `running`
is true only when the service step is listed **and** a control step (`bundle/status.sh`, bounded to 60 s) finds the
recorded process alive. `identity` is `"<launch>:<pid>:<start>"` while running, else null. `ready` is running and the
service's own ready marker (written once its checks passed). A control step that fails or times out is exit 1 with
nothing on stdout.

**stop** records `stopping` in the state before anything runs, then runs `bundle/teardown.sh <kind> --workdir W --keep
--keep-until <allocation end> --timeout <stop_seconds>` as a control step (bounded to `stop_seconds` + 60), waits (about
60 s) for the service step to leave `listed`, and records `stopped` only when teardown exited 0 and the step is gone.
Otherwise it exits 1 and leaves `stopping`. Where the contract said to cancel a step still listed at the bound, this
cancels it (by its id, never more) only when teardown exited 0: a step left behind by a stop that did run. After a
teardown that failed it cancels nothing, because the keep marks may not have been written, and a cancelled batch
script without them removes the work directory; the stop is simply tried again (it is idempotent). It prints one JSON
line, the evidence of the stop, and appends the same object to `stops` in the state:
`{"service", "stopped", "ended", "seconds", "requested_utc", "confirmed_utc"}`. `ended` is read from teardown's
`hpcop-stop` line (`exit`, `kill` or `none`; null when unreadable); `seconds` runs from just before the control step to
the step confirmed gone (null when it was not). A service already `stopped` whose step is not listed answers
`ended: "none"` at once and changes nothing.

**start** of a service whose step is listed and whose process is alive does nothing and exits 0. Otherwise the step
must not be listed and the state must be `stopped`; then the state gets `launch` + 1, the step
`{"name": "<allocation>-<service>-run-<launch>", "id": null}`, its log (in the service's log directory) and `starting`
before the detached service step `bundle/restart.sh <kind> --workdir W` is launched; the step's id and `running` are
recorded once it is listed (about 60 s). Not listed in time is exit 1, and the state stays `starting`: a stop (which is
idempotent) brings it back to `stopped`.

**exec** runs `bundle/exec.sh <kind> --workdir W -- <argv>` as a control step (bounded to 300 s) and passes its stdout
and stderr through unchanged; the exit status is the command's, 124 when the launcher timed out and 125 when the step
could not run.
"""

import json
import posixpath
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

VERBS = ("stop", "start", "status", "exec")
KINDS = ("op", "oz")
BUNDLE = Path(__file__).resolve().parent / "bundle"
WORKDIR = re.compile(r"/.+/hpcop-(o[pz])\.[A-Za-z0-9]{6}")
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}")
ENDED = ("exit", "kill", "none")

STATUS_TIMEOUT_S = 60  # the status control step's bound
EXEC_TIMEOUT_S = 300  # the exec control step's bound
STOP_MARGIN_S = 60  # the stop's control step lives this long past teardown's own timeout
STEP_GONE_WAIT_S = 60  # how long a stop waits for the service step to leave the step list
CANCEL_WAIT_S = 10  # and for a step it cancelled to leave it
START_LISTED_WAIT_S = 60  # how long a start waits for the new service step to be listed
POLL_S = 2.0

_SLEEP = time.sleep
_MONOTONIC = time.monotonic
_STAMP = time.time


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Refusal(Exception):
    """The request cannot be carried out as given: exit 2 (or 1 for a state that is not the one the verb needs)."""

    def __init__(self, message: str, status: int = 2) -> None:
        super().__init__(message)
        self.status = status


class Target:
    """What the state file says about one service, validated."""

    def __init__(self, doc: Any, service: str) -> None:
        services = doc.get("services") if isinstance(doc, dict) else None
        allocation = doc.get("allocation") if isinstance(doc, dict) else None
        record = services.get(service) if isinstance(services, dict) else None
        if not isinstance(record, dict) or not isinstance(allocation, dict):
            raise Refusal("no such service in the federation's state")
        kind, workdir, node = record.get("kind"), record.get("workdir"), record.get("node")
        if kind not in KINDS:
            raise Refusal("the service is neither an Oneprovider nor an Onezone")
        match = WORKDIR.fullmatch(workdir) if isinstance(workdir, str) else None
        if match is None or match.group(1) != kind or ".." in workdir.split("/"):
            raise Refusal("the service's recorded work directory is not an hpcop work directory of its kind")
        if not isinstance(node, str) or not node or node != node.strip():
            raise Refusal("the service has no recorded node")
        prefix = allocation.get("name")
        if not isinstance(prefix, str) or not SAFE_NAME.fullmatch(prefix) or not SAFE_NAME.fullmatch(service):
            raise Refusal("the allocation or the service has no usable name")
        step = record.get("step")
        self.service, self.kind, self.node, self.workdir = service, kind, node, workdir
        self.prefix = prefix
        self.ends = allocation.get("ends")
        self.state = record.get("state")
        self.launch = record.get("launch")
        self.step_name = step.get("name") if isinstance(step, dict) else None
        self.log = record.get("log")

    def script(self, name: str, *args: str) -> List[str]:
        """The argv of one of the bundle's scripts for this service."""
        return ["bash", str(BUNDLE / name), self.kind, "--workdir", self.workdir, *args]

    def control_name(self, verb: str) -> str:
        """A control step's name, `<allocation>-<service>-<verb>-<n>`, unique per call."""
        return "%s-%s-%s-%d" % (self.prefix, self.service, verb, int(_STAMP() * 1000))


def _epoch(value: Any) -> int:
    """The allocation's end as epoch seconds; ValueError for anything else."""
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
        moment = moment.replace(tzinfo=timezone.utc)
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


def _stop_line(stdout: str) -> Optional[str]:
    """`ended` from the last valid `hpcop-stop {...}` line of teardown's stdout; None when there is none."""
    ended = None
    for line in stdout.splitlines():
        if not line.startswith("hpcop-stop "):
            continue
        try:
            record = json.loads(line[len("hpcop-stop ") :])
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("ended") in ENDED:
            ended = record["ended"]
    return ended


def _status_reading(target: Target, launcher: Any) -> Optional[Dict[str, Any]]:
    """The status control step's answer (alive, ready, pid, start); None when it failed, timed out or was unreadable."""
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


def _status(target: Target, launcher: Any, out: Any, err: Any) -> int:
    listed = launcher.listed(target.step_name) if target.step_name else None
    reading = _status_reading(target, launcher)
    if reading is None:
        _say(err, "the status of the service could not be read")
        return 1
    running = listed is not None and reading["alive"]
    identity = "%s:%s:%s" % (target.launch, reading.get("pid"), reading.get("start")) if running else None
    _emit(
        out,
        {
            "service": target.service,
            "state": target.state,
            "running": running,
            "identity": identity,
            "launch": target.launch,
            "node": target.node,
            "step": target.step_name,
            "ready": bool(running and reading.get("ready") is True),
        },
    )
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
                "requested_utc": now,
                "confirmed_utc": now,
            },
        )
        return 0
    try:
        until = _epoch(target.ends)
    except (ValueError, TypeError, IndexError):
        raise Refusal("the allocation has no usable end time to keep the work directory until") from None
    _set(state, target.service, state="stopping")  # recorded before the stop is attempted
    requested = _utcnow()
    started = _MONOTONIC()
    argv = target.script("teardown.sh", "--keep", "--keep-until", str(until), "--timeout", str(stop_seconds))
    ended, teardown_ok = None, False
    try:
        done = launcher.control(
            target.node, argv, name=target.control_name("stop"), timeout_s=stop_seconds + STOP_MARGIN_S
        )
        teardown_ok = done.returncode == 0
        ended = _stop_line(_text(done.stdout))
    except TimeoutError:
        _say(err, "the stop's control step did not finish in time")
    except Exception as error:  # the launcher failed: the stop is unconfirmed, whatever the step did (type only)
        _say(err, "the stop's control step failed (%s)" % type(error).__name__)
    step_gone = False
    if teardown_ok:
        left = _wait_gone(launcher, target.step_name, STEP_GONE_WAIT_S)
        if left is not None:
            launcher.cancel(left)
            left = _wait_gone(launcher, target.step_name, CANCEL_WAIT_S)
        step_gone = left is None
    stopped = teardown_ok and step_gone
    confirmed = _utcnow()
    evidence = {
        "service": target.service,
        "stopped": stopped,
        "ended": ended,
        "seconds": round(_MONOTONIC() - started, 3) if stopped else None,
        "requested_utc": _stamp(requested),
        "confirmed_utc": _stamp(confirmed) if stopped else None,
    }

    def record(doc: Dict[str, Any]) -> Dict[str, Any]:
        entry = doc["services"][target.service]
        entry.setdefault("stops", []).append(dict(evidence))
        if stopped:
            entry["state"] = "stopped"
        return doc

    state.update(record)
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
        if reading["alive"]:
            return 0  # running: nothing to start
        raise Refusal("the service step is still listed though its process is not running: stop it first", 1)
    if target.state != "stopped":
        raise Refusal("the service is '%s', not stopped: stop it first" % (target.state,), 1)
    if not isinstance(target.log, str) or not target.log:
        raise Refusal("the service has no recorded log")
    chosen: Dict[str, Any] = {}

    def claim(doc: Dict[str, Any]) -> Dict[str, Any]:
        entry = doc["services"][target.service]
        if entry.get("state") != "stopped":
            raise Refusal("the service is '%s', not stopped: stop it first" % (entry.get("state"),), 1)
        launch = int(entry.get("launch") or 0) + 1
        name = "%s-%s-run-%d" % (target.prefix, target.service, launch)
        chosen.update(name=name, launch=launch, log=posixpath.join(posixpath.dirname(target.log), name + ".log"))
        entry.update(launch=launch, step={"name": name, "id": None}, log=chosen["log"], state="starting")
        return doc

    state.update(claim)  # recorded before the step is launched, so a crash leaves the attempt in the state
    launcher.service(
        target.node,
        ["bash", str(BUNDLE / "restart.sh"), target.kind, "--workdir", target.workdir],
        kind=target.kind,
        name=chosen["name"],
        log=chosen["log"],
    )
    step_id = _wait_listed(launcher, chosen["name"], START_LISTED_WAIT_S)
    if step_id is None:
        _say(err, "the service step was not listed in time: its state stays 'starting'")
        return 1

    def listed_now(doc: Dict[str, Any]) -> Dict[str, Any]:
        entry = doc["services"][target.service]
        entry["step"] = {"name": chosen["name"], "id": step_id}
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
    return int(done.returncode)


def run(
    verb: str,
    service: str,
    argv: Sequence[str],
    *,
    launcher: Any,
    state: Any,
    stop_seconds: int = 600,
    out: Any = sys.stdout,
    err: Any = sys.stderr,
) -> int:
    """Carry out one verb on one service; return the exit status (see the module docstring)."""
    try:
        if verb not in VERBS:
            raise Refusal("unknown verb '%s'" % (verb,))
        if isinstance(stop_seconds, bool) or not isinstance(stop_seconds, int) or stop_seconds < 1:
            raise Refusal("stop_seconds must be a positive whole number")
        try:
            doc = state.read()
        except Exception as error:  # an unreadable state names its path: only the error's type is told
            raise Refusal("the federation's state could not be read (%s)" % type(error).__name__) from None
        target = Target(doc, service)
        if verb == "status":
            return _status(target, launcher, out, err)
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
