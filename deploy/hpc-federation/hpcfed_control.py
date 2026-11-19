"""The four service verbs of an HPC federation (stop, start, status, exec), as logic over M1's step launcher and state
store (DESIGN.md, section 7).

Written, not run: nothing here has run against a cluster. `hpcfed.py` supplies the two objects (`hpclaunch.Launcher`
and `hpclaunch.StateStore`) and calls this module; the tests run it against those very objects over the driver tests'
fake Slurm, and against fakes of the two. Python 3.8 syntax, the standard library and the driver's own modules only.

The hook, in `hpcfed.py`'s command dispatch:

    if verb in hpcfed_control.VERBS:
        return hpcfed_control.run(verb, service, argv, launcher=launcher, state=state, stop_seconds=stop_seconds,
                                  step_launch_wait_s=step_launch_wait_s, mounts=mounts)

`run(verb, service, argv, *, launcher, state, stop_seconds=50, step_launch_wait_s=240, mounts=None, out=None,
err=None)` returns the exit status. `argv` is what followed `--` for exec, and empty for the other verbs; `out` and
`err` are this process's standard output and error when None. **A control step's bound is its launch wait and then its
work** (`step_launch_wait_s`, the layout's `[hpc]`): a step's client is waited for from its start, so a step Slurm
starts 103 s late (seen live, 17 Nov) must not be cut off before it has begun. The status step is bounded to the launch
wait + 60 s, the exec step to it + 300 s, the mount check to it + 30 s, a start waits for the new service step to be
listed for it + 60 s, and the stop step as below. The launch wait widens timeouts only: it never lengthens a stop, an
outage or a hold.

**The launcher** (`hpclaunch.Launcher`, DESIGN.md section 3):

- `control(node, argv, *, name, timeout_s)`: a control step on `node`, bounded; returns an object with `returncode`,
  `stdout` and `stderr`; raises `TimeoutError` (`hpcslurm.StepTimeout`) at its bound, after cancelling the step by its
  id. `returncode` is None when the step's end is not known (its client was ended after the step vanished, and the
  accounting holds no status), never a success; a step a signal ended reads `128 + signal`.
- `service(node, argv, *, kind, name, log)`: a detached service step; it records its srun client in the state
  (`clients`, by the step's name) and returns nothing.
- `listed(name)`: the id of the step of that name in the held job's step list, or None.
- `cancel(step_id)`: cancel that one step.
- `holder(node, seconds, *, name, log)`: a kept stop's holder (below), a detached `sleep` that overlaps every step;
  it records its srun client in the state as `service` does. `term(step_id)`: TERM to that one step.
  `observed(step_id, name)`: that one step as Slurm shows it, by its exact id (`hpcslurm.Slurm.step_observed`).

**The state** (`hpclaunch.StateStore`, DESIGN.md section 4): `read()` returns the state file's dict; `update(fn)` is a
locked, atomic read-modify-write (`fn` takes the dict and returns it; an exception from `fn` writes nothing) that
returns the new dict. This module reads `allocation.name`, `allocation.ends` (Slurm's `EndTime` as `up` recorded it:
an ISO 8601 time with no zone is the login node's local time, as `scontrol` printed it; one with a `Z` or an offset,
or epoch seconds, is read as such), `site` (`node_tmp`, `logs`, `node_python`), `clients` and, for the service,
`kind`, `node`, `workdir`, `launch`, `step` (`name`, `id`), `state`, `mounts_proven` and `holder`; it writes `launch`,
`step`, `log`, `state`, `stops`, `mounts_proven` and `holder`.

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
--keep-until <allocation end> --timeout <stop_seconds>` as a control step (bounded to `stop_seconds` + the step's
launch wait + 60), waits (about 60 s) for the service step to leave `listed`, and records `stopped` only when teardown
exited 0 and the step is gone
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
`ended: "none"` and `seconds: 0` at once and changes nothing (with `"holder": "failed"` when the state records a
holder the step list does not show, O96).

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

**The holder of a kept stop (O96).** A kept work directory on a node's tmpfs vanished during an outage, removed by
something outside the core, on three federations where no process of ours was left on its node (live, 19 Nov
2026: after about 8, 11 and up to 34 minutes; the restore then failed at once and the member was lost), and never
beside a process of ours (a sleep step held one 40 minutes). So a stop that runs teardown also launches, just before
teardown's control step, a holder on the service's node: `sleep` until the allocation's end, a detached step named
`<allocation>-<service>-hold` (`holder_name`), `--overlap` with one core and 64 MB, so it holds back no control or
service step (`hpclaunch.Launcher.holder`). One already listed under that name (a stop tried again) is kept. The
state records `services.<n>.holder` (`name`, and `id` once listed) in the stop's own two changes. The holder, the one
launched or the one kept, is shown by the step list after teardown, waited for from its launch for the launch wait +
`HOLDER_LISTED_WAIT_S` while its srun client runs, which ends inside the time a stop already takes. A holder not
shown leaves the stop as it is (its exit status and `stopped` are teardown's and the step's) and adds `"holder":
"failed"` to its evidence, in `stops` and on stdout, where the injector's driver reads it (`expcore_onedata.fault_hpc`)
and fails the injection, so the provider is brought back while its directory is still there; a warning goes to
stderr. A start ends the holder only once its new
service step reads running by its exact id (`observed`), so a process of ours is on the node all the while: TERM by
the exact id the step list shows under the holder's name, its cancel by that id when it still runs
`HOLDER_TERM_WAIT_S` later, and its record removed once it reads ended. Every doubt keeps it (a new step that does
not read running in `HOLDER_RUNNING_WAIT_S`, a holder that does not end, a read or a write that fails, a start with
less than these waits left of its own worst case, which the relay's bound for it is made of), and none fails the
start: a sleep that overlaps every step costs nothing, and down ends it. A status of a service with a holder recorded
adds `"holder"`: the id the step list shows it under now, or null.

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

# Every bound below that waits on a step's client from its start also waits for the step to start: `step_launch_wait_s`
# (the layout's `[hpc]`, 240 by default) is added to it at the call, so a step that Slurm starts late is not cut off
# before its own work has begun. The figures here are the work's own.
DEFAULT_LAUNCH_WAIT_S = 240  # the layout's default (hpcsettings.DEFAULTS), for a caller that passes none
STATUS_TIMEOUT_S = 60  # the status control step's own bound
EXEC_TIMEOUT_S = 300  # the exec control step's own bound
MOUNT_CHECK_TIMEOUT_S = 30  # the read-only proof's control step (it reads the service's mounts, nothing more)
STOP_MARGIN_S = 60  # the stop's control step lives this long past teardown's own timeout
STEP_GONE_WAIT_S = 60  # how long a stop waits for the service step to leave the step list
CANCEL_WAIT_S = 10  # and for a step it cancelled to leave it
START_LISTED_WAIT_S = 60  # how long a start waits, past the step's launch wait, for the new service step to be listed
# A kept stop's holder (O96, below). Its listing is waited for from its launch, which comes just before teardown's
# control step, so the wait ends inside the time a stop already takes (teardown's control step alone may take longer).
HOLDER_LISTED_WAIT_S = 60  # past the step's launch wait, from the holder's launch
HOLDER_RUNNING_WAIT_S = 10  # how long a start waits for its new service step to read running before it ends the holder
HOLDER_TERM_WAIT_S = 10  # how long an ended holder (a sleep) is given to end after its TERM, before its cancel
HOLDER_FAILED = "failed"  # the stop's evidence, `holder`, when no holder was shown listed (absent when one was)
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

    def __init__(
        self, doc: Any, service: Any, mounts: Any = None, launch_wait_s: int = DEFAULT_LAUNCH_WAIT_S,
        step_tag: Optional[str] = None,
    ) -> None:  # fmt: skip
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
        self.launch_wait_s = launch_wait_s  # what every control step of this service may wait to start

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


def holder_name(prefix: str, service: str) -> str:
    """A kept stop's holder's step name, `<allocation>-<service>-hold`: its last word is no control word, no tag and
    no launch number, so it equals no other step's name (a service's run step and every control step end in digits,
    down's own in another word), and neither the relay's late-step watch nor any name down looks for matches it."""
    return "%s-%s-hold" % (prefix, service)


def _holder_record(doc: Dict[str, Any], service: str) -> Optional[Dict[str, Any]]:
    record = doc["services"][service].get("holder")
    return record if isinstance(record, dict) and isinstance(record.get("name"), str) else None


def _launch_holder(target: Target, launcher: Any, until: int, err: Any) -> None:
    """The holder of a kept stop (O96), launched unless one is already listed under the exact name (a stop tried
    again), which is kept, never doubled. A launch that fails is said, by its error's type only; whether a holder runs
    is decided by its listing after teardown (`_holder_listed`), whichever it is."""
    name = holder_name(target.prefix, target.service)
    try:
        if launcher.listed(name) is not None:
            return
        seconds = max(1, until - int(_STAMP()))  # to the allocation's end, as the kept work directory is
        log = str(Path(_site_value(target.site, "logs")) / (name + ".log"))
        launcher.holder(target.node, seconds, name=name, log=log)
    except Exception as error:  # the stop goes on: its evidence says the holder failed
        _say(err, "the holder step could not be launched (%s)" % type(error).__name__)


def _holder_listed(launcher: Any, state: Any, name: str, deadline: float) -> Optional[str]:
    """The holder's id once the step list shows it, waited for until `deadline` (monotonic) while its srun client
    still runs; None when it is not shown: its client ended without it (srun refused it) or the wait ran out."""
    while True:
        try:
            listed = launcher.listed(name)
            if listed is not None:
                return str(listed)
            if _MONOTONIC() >= deadline or not _CLIENT_ALIVE((state.read().get("clients") or {}).get(name)):
                return None
        except Exception:  # an unreadable list or state shows no holder; the stop's evidence is still recorded
            return None
        _SLEEP(POLL_S)


def _listed_or_none(launcher: Any, name: str) -> Optional[str]:
    """The id the step list shows under `name`, or None, a list that cannot be read included."""
    try:
        found = launcher.listed(name)
    except Exception:
        return None
    return str(found) if found is not None else None


def _wait_reads(launcher: Any, step_id: str, name: str, running: bool, seconds: float) -> bool:
    """Whether the step reads `running` (True or False) by its exact id within `seconds`; a read that cannot tell is
    no answer, and any error is none either."""
    deadline = _MONOTONIC() + seconds
    while True:
        try:
            if launcher.observed(step_id, name).get("running") is running:
                return True
        except Exception:
            pass  # no reading: tried again until the bound, then no answer
        if _MONOTONIC() >= deadline:
            return False
        _SLEEP(POLL_S)


def _end_holder(target: Target, launcher: Any, state: Any, step: Tuple[str, str], err: Any, began: float) -> None:
    """A start's end of the service's holder (O96), once its new service step `(id, name)` reads running by its exact
    id, so a process of ours is on the node all the while: the holder is sent TERM by its exact id, cancelled by it if
    it is still running `HOLDER_TERM_WAIT_S` later, and its record removed once it reads ended. Every doubt keeps it
    (a sleep that overlaps every step holds nothing back; down ends it), and nothing here fails the start. It runs
    only inside the start's own worst case from `began` (a status step, then the service step's listing, each with
    its launch wait), which the relay's bound for a start is made of (`fault_hpc.verb_inner_worst_s`): a start with
    less than its waits left keeps the holder."""
    try:
        _end_holder_now(target, launcher, state, step, err, began)
    except Exception as error:  # the holder's bookkeeping never fails a start whose service runs
        _say(err, "the holder could not be ended (%s): it is kept in the state" % type(error).__name__)


def _end_holder_now(
    target: Target, launcher: Any, state: Any, step: Tuple[str, str], err: Any, began: float
) -> None:  # fmt: skip
    record = _holder_record(state.read(), target.service)
    if record is None:
        return
    name = record["name"]
    worst = began + 2 * target.launch_wait_s + STATUS_TIMEOUT_S + START_LISTED_WAIT_S
    if worst - _MONOTONIC() < HOLDER_RUNNING_WAIT_S + HOLDER_TERM_WAIT_S + CANCEL_WAIT_S:
        _say(err, "the start has no time left to end the service's holder inside its bound: the holder is kept")
        return
    try:
        if not _wait_reads(launcher, step[0], step[1], True, HOLDER_RUNNING_WAIT_S):
            _say(err, "the new service step does not read running yet: its holder is kept")
            return
        listed = launcher.listed(name)
        if listed is None:  # no holder runs: none was shown, or it ended; one whose srun client runs may yet start
            if _CLIENT_ALIVE((state.read().get("clients") or {}).get(name)):
                _say(err, "the holder's launch may still be in flight: it is kept in the state")
                return
        else:
            holder = str(listed)  # the exact id the list shows under the holder's own name
            launcher.term(holder)
            if not _wait_reads(launcher, holder, name, False, HOLDER_TERM_WAIT_S):
                launcher.cancel(holder)
                if not _wait_reads(launcher, holder, name, False, CANCEL_WAIT_S):
                    _say(err, "the holder did not end after its TERM and its cancel: it is kept in the state")
                    return
    except Exception as error:
        _say(err, "the holder could not be ended (%s): it is kept in the state" % type(error).__name__)
        return

    def ended(doc: Dict[str, Any]) -> Dict[str, Any]:
        entry = doc["services"][target.service]
        if (entry.get("holder") or {}).get("name") == name:
            del entry["holder"]
        return doc

    state.update(ended)


def _status_reading(target: Target, launcher: Any) -> Optional[Dict[str, Any]]:
    """The status control step's answer (alive, ready, pid, start); None when it failed, timed out or was unreadable.
    A step whose end is not known (`returncode` None) is one that failed."""
    try:
        done = launcher.control(
            target.node,
            target.script("status.sh"),
            name=target.control_name("status"),
            timeout_s=target.launch_wait_s + STATUS_TIMEOUT_S,
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
        bound = target.launch_wait_s + MOUNT_CHECK_TIMEOUT_S
        done = launcher.control(target.node, argv, name=target.control_name("mounts"), timeout_s=bound)
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
    holder = _holder_record(target.doc, target.service)
    if holder is not None:  # O96: the id the step list shows the service's holder under now, or null
        record["holder"] = _listed_or_none(launcher, holder["name"])
    _emit(out, record)
    return 0


def _stop(target: Target, launcher: Any, state: Any, stop_seconds: int, out: Any, err: Any) -> int:
    if target.state == "stopped" and (not target.step_name or launcher.listed(target.step_name) is None):
        now = _stamp(_utcnow())
        evidence = {
            "service": target.service,
            "stopped": True,
            "ended": "none",
            "seconds": 0.0,
            "service_seconds": None,
            "requested_utc": now,
            "confirmed_utc": now,
        }
        held = _holder_record(target.doc, target.service)
        if held is not None and _listed_or_none(launcher, held["name"]) is None:  # O96: a stop asked again
            evidence["holder"] = HOLDER_FAILED
            _say(err, "WARNING: the stopped service's holder step is not shown beside its kept work directory")
        _emit(out, evidence)
        return 0
    try:
        until = _epoch(target.ends)
    except (ValueError, TypeError, IndexError, OverflowError, OSError):
        raise Refusal("the allocation has no usable end time to keep the work directory until") from None
    name = holder_name(target.prefix, target.service)
    held = _holder_record(target.doc, target.service)
    holder = held if held is not None and held["name"] == name else {"name": name, "id": None}
    _set(state, target.service, state="stopping", holder=holder)  # recorded before the stop is attempted
    requested = _utcnow()
    started = _MONOTONIC()
    _launch_holder(target, launcher, until, err)  # before teardown: never a moment with none of ours
    argv = target.script("teardown.sh", "--keep", "--keep-until", str(until), "--timeout", str(stop_seconds))
    ended, service_seconds, teardown_ok = None, None, False
    try:
        done = launcher.control(
            target.node,
            argv,
            name=target.control_name("stop"),
            timeout_s=stop_seconds + target.launch_wait_s + STOP_MARGIN_S,
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
    holder_id = _holder_listed(launcher, state, name, started + target.launch_wait_s + HOLDER_LISTED_WAIT_S)
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
    if holder_id is None:  # O96: the kept work directory has no process of ours beside it on its node
        evidence["holder"] = HOLDER_FAILED
        _say(err, "WARNING: no holder step was shown beside the kept work directory, which may not outlive a long "
                  "outage")  # fmt: skip

    def record(doc: Dict[str, Any]) -> Dict[str, Any]:
        entry = doc["services"][target.service]
        entry.setdefault("stops", []).append(dict(evidence))
        if holder_id is not None:
            entry["holder"] = {"name": name, "id": holder_id}
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
    began = _MONOTONIC()
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
        _end_holder(target, launcher, state, (str(listed), target.step_name), err, began)  # O96: the service runs
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
    step_id = _wait_listed(launcher, name, target.launch_wait_s + START_LISTED_WAIT_S)
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
    _end_holder(target, launcher, state, (step_id, name), err, began)  # O96: only once the new step reads running
    return 0


def _exec(target: Target, launcher: Any, argv: Sequence[str], out: Any, err: Any) -> int:
    if not argv or not all(isinstance(word, str) for word in argv):
        raise Refusal("exec takes a command after --")
    try:
        done = launcher.control(
            target.node,
            target.script("exec.sh", "--", *argv),
            name=target.control_name("exec"),
            timeout_s=target.launch_wait_s + EXEC_TIMEOUT_S,
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
    stop_seconds: int = 50,  # the layout's default (hpcsettings.DEFAULTS); `hpcfed.py` passes the layout's own
    step_launch_wait_s: int = DEFAULT_LAUNCH_WAIT_S,  # likewise
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
        if isinstance(step_launch_wait_s, bool) or not isinstance(step_launch_wait_s, int) or step_launch_wait_s < 0:
            raise Refusal("step_launch_wait_s must be a whole number of seconds, 0 or more")
        with _exclusive(state) if verb in MUTATING else contextlib.nullcontext():
            try:
                doc = state.read()
            except Exception as error:  # an unreadable state names its path: only the error's type is told
                raise Refusal("the federation's state could not be read (%s)" % type(error).__name__) from None
            target = Target(doc, service, mounts, step_launch_wait_s, step_tag)
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
