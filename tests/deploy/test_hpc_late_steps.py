"""O58: a verb the fault relay answered `timed_out` can still start a step: its `srun` waits on the Slurm controller,
which may stall for its message timeout, and the step is made and launched afterwards. The relay stamps the verb's
control steps with a tag of its own (`hpcfed.py --step-tag`), and `hpcfed.py late-steps` watches for any step of one of
that verb's exact names, in the federation's own job alone, cancels it by its exact `<job>.<step>` id, and writes what
it saw to the relay's journal.

Nothing here reaches a cluster: the watch runs over a stand-in Slurm that shows each poll what a script says, and the
command-line wiring over the fake Slurm of `hpc_fed_support`.
"""

import copy
import json
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeProcess, FakeSlurm, agent_site, hpc_on_path

from expcore_onedata.fault_relay_late import JOURNAL, LATE_POLL_S, LATE_WINDOW_S, step_tag, watch_argv

PREFIX = "hpcop-demo-ab12cd34"
JOB = "4242"
SERVICE = "prov-a"
TAG = step_tag("1" * 32, "a" * 32)
REQUEST = "a" * 32
WORDS = {"status": ("status", "mounts"), "stop": ("stop",), "start": ("status",), "exec": ("exec",)}


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcfed_control
        import hpclate

        modules.control, modules.late = hpcfed_control, hpclate
        yield modules


class Store:
    """The federation's state file as the watch reads it: the allocation's name and job, and its providers."""

    def __init__(self, sandbox=None):
        self.doc = {
            "allocation": {"name": PREFIX, "job": JOB},
            "services": {SERVICE: {"kind": "op"}, "zone": {"kind": "oz"}},
        }
        if sandbox is not None:
            self.doc["sandbox"] = sandbox

    def read(self):
        return copy.deepcopy(self.doc)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Cluster:
    """Slurm as the held job's controller shows it, poll by poll. `timeline[n]` runs at the n-th listing of the job's
    steps and changes what is listed or accounted; `unreadable` makes every listing fail, as a stalled controller's."""

    def __init__(self, slurm_module):
        self.error = slurm_module.SlurmError
        self.polls, self.listed, self.accounting, self.timeline = 0, {}, {}, {}
        self.other_jobs: dict[str, dict[str, str]] = {}  # a job of nobody's here: asked for never
        self.asked: list[str] = []
        self.cancelled: list[str] = []
        self.unreadable = self.accounting_unreadable = False
        self.cancel_error: Exception | None = None
        self.gone_at: int | None = None
        self.gone_fails_at: int | None = None  # the check that the job exists fails at this look

    def steps(self, job):
        self.asked.append(job)
        self.polls += 1
        if self.polls in self.timeline:
            self.timeline[self.polls](self)
        if self.unreadable:
            raise self.error("squeue -s did not answer")
        return dict(self.listed if job == JOB else self.other_jobs.get(job, {}))

    def gone(self, job):
        if self.gone_fails_at is not None and self.gone_fails_at <= self.polls <= self.gone_fails_at + 1:
            raise self.error("squeue failed")
        return self.gone_at is not None and self.polls >= self.gone_at

    def accounted(self, job):
        if self.accounting_unreadable:
            raise self.error("sacct failed")
        return dict(self.accounting)

    def cancel_step(self, step_id):
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(step_id)
        self.listed = {name: step for name, step in self.listed.items() if step != step_id}


def run_watch(hpc, tmp_path, cluster, verb="stop", window_s=60.0, poll_s=5.0, store=None, since=None):
    """The watch, to its end on a fake clock; the journal's lines."""
    launcher = hpc.launch.Launcher(cluster, store or Store(), None)
    journal, clock = tmp_path / JOURNAL, Clock()
    status = hpc.late.watch(
        launcher,
        store or launcher.store,
        SERVICE,
        verb,
        TAG,
        request=REQUEST,
        journal=journal,
        window_s=window_s,
        poll_s=poll_s,
        clock=clock.monotonic,
        sleep=clock.sleep,
        since=since,
    )
    lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()] if journal.exists() else []
    return status, lines, clock


def name(word, tag=TAG, service=SERVICE, prefix=PREFIX):
    return f"{prefix}-{service}-{word}-{tag}"


def appears(step_name, step_id, poll):
    def run(cluster):
        cluster.listed[step_name] = step_id

    return {poll: run}


def events(lines, kind):
    return [line for line in lines if line["event"] == kind]


def test_o58_a_step_named_for_the_timed_out_verb_that_appears_late_is_cancelled_by_its_exact_id_and_recorded(
    hpc, tmp_path
):
    """O58: the controller catches up and the stop's step is made after the relay answered: the third look finds it
    listed under the verb's exact name, and it is cancelled as `<job>.<step>`, by that id alone, and journalled."""
    cluster = Cluster(hpc.slurm)
    cluster.timeline = appears(name("stop"), f"{JOB}.7", 3)
    status, lines, _ = run_watch(hpc, tmp_path, cluster)
    assert status == 0
    assert cluster.cancelled == [f"{JOB}.7"]
    (late,) = events(lines, "late_step")
    assert (late["request"], late["step"], late["id"], late["cancelled"]) == (REQUEST, name("stop"), f"{JOB}.7", True)
    assert events(lines, "watch_closed")[-1]["outcome"] == "late_steps"


def test_o58_a_step_of_another_name_or_service_or_tag_or_word_is_never_touched(hpc, tmp_path):
    """O58: only the verb's own names: not another request's tag (a hand-run verb's millisecond stamp, a later request
    of the same service), another service's step, another verb's word, the service's run step, nor the job's own."""
    cluster = Cluster(hpc.slurm)
    cluster.listed = {
        name("stop", tag="0" * 12): f"{JOB}.1",  # another request's
        f"{PREFIX}-{SERVICE}-stop-1763000000000": f"{JOB}.2",  # a verb run by hand: the clock's stamp
        name("stop", service="prov-b"): f"{JOB}.3",  # another service's
        name("status"): f"{JOB}.4",  # this tag, but a word a stop never makes
        f"{PREFIX}-{SERVICE}-run-3": f"{JOB}.5",  # the service's own step
        name("stop", prefix="hpcop-other-99999999"): f"{JOB}.6",  # another federation's name
        name("stop"): f"{JOB}.9",  # the one that is the verb's
    }
    _, lines, _ = run_watch(hpc, tmp_path, cluster)
    assert cluster.cancelled == [f"{JOB}.9"]
    assert [line["step"] for line in events(lines, "late_step")] == [name("stop")]


@pytest.mark.parametrize("verb", ["stop", "start", "status", "exec"])
def test_o96_a_kept_stops_holder_lingering_beside_the_service_is_never_taken_for_a_late_step(hpc, tmp_path, verb):
    """O96: the holder of a kept stop lingers beside its service after the restore (`hpcfed_control.holder_name`: no
    control word and no tag in it): the watch of any verb of that service never cancels it, and cancels the verb's own
    late step."""
    import hpcfed_control

    cluster = Cluster(hpc.slurm)
    word = WORDS[verb][0]
    cluster.listed = {hpcfed_control.holder_name(PREFIX, SERVICE): f"{JOB}.8", name(word): f"{JOB}.9"}
    _, lines, _ = run_watch(hpc, tmp_path, cluster, verb=verb)
    assert cluster.cancelled == [f"{JOB}.9"]
    assert [line["step"] for line in events(lines, "late_step")] == [name(word)]


def test_o58_a_step_listed_under_another_job_is_never_cancelled_even_when_it_has_the_name(hpc, tmp_path):
    """O58: the watch lists the federation's own job alone and cancels through the launcher, which refuses a step of
    any other job: a listing that shows a foreign id under the right name is journalled as refused, never cancelled."""
    cluster = Cluster(hpc.slurm)
    cluster.other_jobs = {"9999": {name("stop"): "9999.1"}}
    cluster.listed = {name("stop"): "5555.2"}  # an id of a job that is not the held job's
    _, lines, _ = run_watch(hpc, tmp_path, cluster)
    assert "9999" not in cluster.asked
    assert cluster.cancelled == []
    (late,) = events(lines, "late_step")  # said once, not at every look
    assert late["cancelled"] is False
    assert late["error"] == "StateError"


@pytest.mark.parametrize("verb", sorted(WORDS))
def test_o58_each_verb_is_watched_for_the_names_it_makes_and_no_other_word(hpc, tmp_path, verb):
    """O58: a status makes a status step and a read-only check (`mounts`), a stop its `stop`, a start only the status
    it reads first, an exec its `exec`; a step of this tag with any other word is not that verb's."""
    cluster = Cluster(hpc.slurm)
    cluster.listed = {
        name(word): f"{JOB}.{index}" for index, word in enumerate(["status", "mounts", "stop", "exec"], 1)
    }
    run_watch(hpc, tmp_path, cluster, verb=verb)
    ours = {
        f"{JOB}.{index}" for index, word in enumerate(["status", "mounts", "stop", "exec"], 1) if word in WORDS[verb]
    }
    assert set(cluster.cancelled) == ours


def test_o58_a_step_that_ran_and_ended_between_two_looks_is_found_in_the_accounting_and_recorded(hpc, tmp_path):
    """O58: a status or a link restart is over in seconds, between two polls: it is never listed, so the window's end
    reads the accounting for the verb's exact names and records a step that was made, with how it ended. Nothing is
    cancelled: it is over."""
    cluster = Cluster(hpc.slurm)
    cluster.accounting = {name("exec"): {"id": f"{JOB}.8", "exit": 0, "state": "COMPLETED", "start": 1_500.0}}
    _, lines, _ = run_watch(hpc, tmp_path, cluster, verb="exec", since=1_000.0)  # it began after the answer
    assert cluster.cancelled == []
    (late,) = events(lines, "accounted_late")
    assert (late["step"], late["id"], late["exit"], late["state"]) == (name("exec"), f"{JOB}.8", 0, "COMPLETED")
    assert late["start_known"] is True
    assert events(lines, "watch_closed")[-1]["outcome"] == "late_steps"


def test_o58_a_step_the_accounting_shows_that_began_before_the_answer_ran_inside_the_verb_and_is_no_late_step(
    hpc, tmp_path
):
    """O58: a verb that ran 25 s or more is watched though its steps all ran in time (a stop that took a minute, say):
    its own steps are in the accounting under the exact names, and are no late steps. A step that began before the
    moment the relay answered is recorded as `accounted_in_time` and does not make the window a `late_steps` one."""
    cluster = Cluster(hpc.slurm)
    cluster.accounting = {name("stop"): {"id": f"{JOB}.7", "exit": 0, "state": "COMPLETED", "start": 900.0}}
    _, lines, _ = run_watch(hpc, tmp_path, cluster, since=1_000.0)
    assert events(lines, "accounted_late") == []
    (inside,) = events(lines, "accounted_in_time")
    assert (inside["step"], inside["id"], inside["state"]) == (name("stop"), f"{JOB}.7", "COMPLETED")
    closed = events(lines, "watch_closed")[-1]
    assert (closed["outcome"], closed["late_steps"]) == ("none_seen", 0)


@pytest.mark.parametrize(("start", "known"), [(None, False), (1_000.4, True)])
def test_o58_a_step_with_no_accounted_start_or_one_in_the_answers_own_second_is_counted_as_late(
    hpc, tmp_path, start, known
):
    """O58: sacct's Start is whole seconds: a step that began in the second of the answer, or that the accounting gives
    no Start at all, may have run late, so it is counted (fail closed), with `start_known` saying which."""
    cluster = Cluster(hpc.slurm)
    cluster.accounting = {name("stop"): {"id": f"{JOB}.7", "exit": 0, "state": "COMPLETED", "start": start}}
    _, lines, _ = run_watch(hpc, tmp_path, cluster, since=1_000.7)
    (late,) = events(lines, "accounted_late")
    assert late["start_known"] is known
    assert events(lines, "watch_closed")[-1]["outcome"] == "late_steps"


def test_o58_the_accounting_read_gives_each_steps_start_as_epoch_seconds_and_none_for_unknown(hpc):
    """O58: `Slurm.accounted` reads sacct once for the job's steps: id, exit status, state, and Start as epoch seconds
    (the login node's local time, as sacct prints it) or None."""
    from datetime import datetime

    rows = (
        f"{JOB}.batch|batch|0:0|COMPLETED|2026-11-17T09:00:00\n"
        f"{JOB}.7|a-step|0:0|COMPLETED|2026-11-17T09:00:05\n"
        f"{JOB}.8|b-step|0:9|CANCELLED by 1000|Unknown\n"
    )
    seen = []

    class Runner:
        def run(self, argv, timeout):
            seen.append(argv)
            return hpc.slurm.Completed(0, rows, "")

    found = hpc.slurm.Slurm(Runner()).accounted(JOB)
    assert set(found) == {"a-step", "b-step"}
    assert found["a-step"]["start"] == datetime(2026, 11, 17, 9, 0, 5).timestamp()
    assert (found["b-step"]["start"], found["b-step"]["exit"], found["b-step"]["state"]) == (None, 137, "CANCELLED")
    assert len(seen) == 1
    assert "Start" in seen[0][-1]


def test_o58_a_step_the_watch_cancelled_is_not_reported_again_from_the_accounting(hpc, tmp_path):
    """O58: the cancelled step is in the accounting too, as cancelled: it is recorded once, as the late step it was."""
    cluster = Cluster(hpc.slurm)
    cluster.timeline = appears(name("stop"), f"{JOB}.7", 2)
    cluster.accounting = {name("stop"): {"id": f"{JOB}.7", "exit": 0, "state": "CANCELLED"}}
    _, lines, _ = run_watch(hpc, tmp_path, cluster)
    assert [line["id"] for line in events(lines, "late_step")] == [f"{JOB}.7"]
    assert events(lines, "accounted_late") == []


def test_o58_a_window_with_no_step_closes_as_none_seen_and_only_after_the_whole_window(hpc, tmp_path):
    """O58: nothing listed and nothing accounted for the verb's names through the window: `none_seen`, with how many
    looks were read; the watch lasts the window and polls inside it."""
    cluster = Cluster(hpc.slurm)
    _, lines, clock = run_watch(hpc, tmp_path, cluster, window_s=30.0, poll_s=5.0)
    (closed,) = events(lines, "watch_closed")
    assert closed["outcome"] == "none_seen"
    assert closed["accounting"] == "read"
    assert closed["polls_read"] >= 5
    assert closed["polls_unread"] == 0
    assert 30.0 <= clock.now - 1000.0 <= 30.0 + 5.0
    assert [line["event"] for line in lines] == ["watch_closed"]


def test_o58_a_window_that_could_never_be_read_is_never_recorded_as_none_seen(hpc, tmp_path):
    """O58: a controller that answered no listing in the whole window is no evidence that nothing came: the watch
    closes `unreadable`, with the count of looks that failed."""
    cluster = Cluster(hpc.slurm)
    cluster.unreadable = True
    _, lines, _ = run_watch(hpc, tmp_path, cluster)
    (closed,) = events(lines, "watch_closed")
    assert closed["outcome"] == "unreadable"
    assert closed["polls_read"] == 0
    assert closed["polls_unread"] > 0


def test_o58_an_accounting_that_cannot_be_read_is_never_recorded_as_none_seen(hpc, tmp_path):
    """O58: listings read, nothing listed, but the accounting (which would show a step that came and went) cannot be
    read: `unreadable`, never `none_seen`."""
    cluster = Cluster(hpc.slurm)
    cluster.accounting_unreadable = True
    _, lines, _ = run_watch(hpc, tmp_path, cluster)
    closed = events(lines, "watch_closed")[-1]
    assert closed["outcome"] == "unreadable"
    assert closed["accounting"] == "unreadable"


def test_o58_a_failed_cancel_is_journalled_and_tried_again_at_the_next_look(hpc, tmp_path):
    """O58: a scancel that fails is recorded as not cancelled, with the error's type; the step is still listed, so the
    next look tries again, and the one that succeeds is recorded."""
    cluster = Cluster(hpc.slurm)
    cluster.timeline = {
        **appears(name("stop"), f"{JOB}.7", 2),
        4: lambda cluster: setattr(cluster, "cancel_error", None),
    }
    cluster.cancel_error = cluster.error("scancel failed")
    _, lines, _ = run_watch(hpc, tmp_path, cluster)
    tried = events(lines, "late_step")
    assert next(line["cancelled"] for line in tried) is False
    assert tried[0]["error"] == "SlurmError"
    assert tried[-1]["cancelled"] is True
    assert cluster.cancelled == [f"{JOB}.7"]


def test_o58_a_look_whose_job_check_fails_still_cancels_the_step_it_found(hpc, tmp_path):
    """O58: the listing showed the late step and the check that the job still exists failed (an intermittent
    controller): the step found is cancelled at that look, not at a later one that may find it ended."""
    cluster = Cluster(hpc.slurm)
    cluster.timeline = appears(name("stop"), f"{JOB}.7", 2)
    cluster.gone_fails_at = 2
    _, lines, _ = run_watch(hpc, tmp_path, cluster)
    assert cluster.cancelled == [f"{JOB}.7"]
    (late,) = events(lines, "late_step")
    assert late["cancelled"] is True


def test_o58_a_listing_that_fails_after_the_step_was_found_keeps_what_was_found(hpc, tmp_path):
    """O58: the held job and the agent's own job are listed one after the other; the second listing fails, and the step
    the first showed is still cancelled at that look."""
    sandbox = {"placement": "separate", "job": "5151"}
    cluster = Cluster(hpc.slurm)
    cluster.timeline = appears(name("stop"), f"{JOB}.7", 1)
    steps = cluster.steps

    def listing(job):
        found = steps(job)
        if job == "5151":
            raise cluster.error("squeue -s did not answer")
        return found

    cluster.steps = listing
    _, lines, _ = run_watch(hpc, tmp_path, cluster, store=Store(sandbox), window_s=5.0)
    assert cluster.cancelled == [f"{JOB}.7"]
    assert events(lines, "late_step")[0]["cancelled"] is True


def test_o58_a_job_check_that_raises_something_unforeseen_does_not_kill_the_watch(hpc, tmp_path):
    """O58: the check that the job exists fails with a type the watch did not name (a state with no job gives None): the
    window still runs and closes with its record."""
    cluster = Cluster(hpc.slurm)

    def broken(job):
        raise TypeError("no job")

    cluster.gone = broken
    status, lines, _ = run_watch(hpc, tmp_path, cluster, window_s=10.0)
    assert status == 0
    assert events(lines, "watch_closed")[-1]["outcome"] == "none_seen"


def test_o58_the_relays_tables_and_threshold_hold_to_hpcfeds_own(hpc):
    """O58: the words the relay looks for are the words hpcfed's verbs name their steps by, and the elapsed time at
    which the relay watches a verb is below every bound hpcfed ends a control step at (it kills an srun only after its
    step's own bound, so a verb quicker than that cannot have left one pending that the group kill missed). Every
    bound is the step's own and the layout's launch wait (`step_launch_wait_s`, 0 or more), so the shortest is the
    read-only check's 30 s with a launch wait of 0, which is what the threshold is held under."""
    import inspect
    import re

    from expcore_onedata.fault_relay_late import CONTROL_WORDS, LATE_ELAPSED_S

    assert hpc.control.CONTROL_WORDS == CONTROL_WORDS
    source = inspect.getsource(hpc.control)
    bounds = re.findall(r"timeout_s=([^,\n)]+)", source) + re.findall(r"^\s+bound = (.+)$", source, re.MULTILINE)
    launch = 0  # the least a layout declares: `run` refuses a negative launch wait
    known = {
        "target.launch_wait_s + STATUS_TIMEOUT_S": launch + hpc.control.STATUS_TIMEOUT_S,
        "target.launch_wait_s + MOUNT_CHECK_TIMEOUT_S": launch + hpc.control.MOUNT_CHECK_TIMEOUT_S,
        "target.launch_wait_s + EXEC_TIMEOUT_S": launch + hpc.control.EXEC_TIMEOUT_S,
        # a stop's bound, `stop_seconds` at least 1
        "stop_seconds + target.launch_wait_s + STOP_MARGIN_S": 1 + launch + hpc.control.STOP_MARGIN_S,
    }
    control_bounds = [bound.strip() for bound in bounds if bound.strip() not in ("timeout_s", "timeout", "bound")]
    assert set(control_bounds) <= set(known), "a control step has a bound the relay's threshold does not know"
    assert min(known.values()) >= LATE_ELAPSED_S


def test_o58_the_watch_and_the_threshold_hold_under_the_per_verb_bounds_and_the_launch_wait():
    """O58, with the per-verb relay bounds (`fault_hpc.verb_bounds`) and the launch wait they rest on: a verb the relay
    ends at its bound ran far past the threshold, so it is watched either way; the watch's window outlasts the longest a
    step may wait on the controller (the launch wait, which was derived from the site's message timeout) and a look;
    and a verb ended at its bound is still answered inside the client's wait, which the group's kill and the one
    accounting read share (30 s beyond the bound)."""
    from expcore_onedata.fault_hpc import HPC_STEP_LAUNCH_WAIT_S, verb_bounds
    from expcore_onedata.fault_relay_group import GRACE_S
    from expcore_onedata.fault_relay_late import LATE_ELAPSED_S
    from expcore_onedata.fault_relay_timing import SACCT_TIMEOUT_S
    from expcore_onedata.fault_settings import DEFAULT_RELAY_WAIT_S

    stop_bound_s = 50 + 90  # the layouts' `stop_seconds` and what the service's own stop adds (`HPC_STOP_EXTRA_S`)
    bounds = verb_bounds(
        step_launch_wait_s=HPC_STEP_LAUNCH_WAIT_S, stop_bound_s=stop_bound_s, command_timeout_s=300.0,
        settle_timeout_s=stop_bound_s + HPC_STEP_LAUNCH_WAIT_S + 70,
    )  # fmt: skip
    assert set(bounds) == {"status", "start", "stop", "ping", "restart_link"}
    assert all(bound > LATE_ELAPSED_S for bound in bounds.values())
    assert LATE_WINDOW_S >= HPC_STEP_LAUNCH_WAIT_S + LATE_POLL_S
    assert GRACE_S + SACCT_TIMEOUT_S < DEFAULT_RELAY_WAIT_S


def test_o58_the_watch_ends_early_once_the_held_job_is_gone(hpc, tmp_path):
    """O58: a job that is gone can start no step: the watch closes `job_gone` at once, whatever the window."""
    cluster = Cluster(hpc.slurm)
    cluster.gone_at = 2
    _, lines, clock = run_watch(hpc, tmp_path, cluster, window_s=300.0)
    assert events(lines, "watch_closed")[-1]["outcome"] == "job_gone"
    assert clock.now - 1000.0 < 30.0


def test_o58_a_step_still_listed_at_the_end_is_said_so(hpc, tmp_path):
    """O58: a step that was cancelled and is still listed when the window ends (Slurm gives a step KillWait to end) is
    named in the closing line, never silently taken as gone."""
    cluster = Cluster(hpc.slurm)
    cluster.timeline = appears(name("stop"), f"{JOB}.7", 11)  # the last look of a 60 s window at 5 s

    def stubborn(step_id):
        cluster.cancelled.append(step_id)  # accepted, but the step stays listed

    cluster.cancel_step = stubborn
    _, lines, _ = run_watch(hpc, tmp_path, cluster, window_s=60.0, poll_s=5.0)
    assert events(lines, "watch_closed")[-1]["still_listed"] == [name("stop")]


def test_o58_a_state_file_that_is_gone_ends_the_watch_and_says_so(hpc, tmp_path):
    """O58: the federation was taken down (its state file is gone): nothing to watch, nothing to cancel with; the
    journal says `unreadable` and the command fails."""

    class Gone(Store):
        def read(self):
            raise hpc.state.StateError("the root holds no state")

    cluster = Cluster(hpc.slurm)
    status, lines, _ = run_watch(hpc, tmp_path, cluster, store=Gone())
    assert status == 1
    assert events(lines, "watch_closed")[-1]["outcome"] == "unreadable"


# -- the verbs name their steps by the relay's tag ---------------------------------------------------------------------


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class VerbState:
    def __init__(self, state, root):
        self.doc, self.root = state, root

    def read(self):
        return copy.deepcopy(self.doc)

    def update(self, fn):
        self.doc = fn(copy.deepcopy(self.doc))
        return self.doc


class VerbLauncher:
    """The steps a verb launches: each control step's name, the service steps' names, and what is listed."""

    def __init__(self, listed=None):
        self.control_names, self.service_names, self.steps = [], [], dict(listed or {})

    def control(self, node, argv, *, name, timeout_s):
        self.control_names.append(name)
        script = Path(argv[1]).name if len(argv) > 1 else ""
        if script == "status.sh":
            return Done(0, json.dumps({"alive": True, "pid": 4321, "start": "88", "ready": True}) + "\n")
        if script == "teardown.sh":
            self.steps = {}
            return Done(0, 'hpcop-stop {"ended": "exit", "seconds": 1.5}\n')
        return Done(0, "pong\n")

    def service(self, node, argv, *, kind, name, log):
        self.service_names.append(name)
        self.steps[name] = "4242.30"

    def listed(self, name):
        return self.steps.get(name)

    def cancel(self, step_id):
        self.steps = {name: step for name, step in self.steps.items() if step != step_id}


def provider(**fields):
    record = {
        "kind": "op",
        "node": "node-x1",
        "workdir": "/nodetmp/hpcop-op.Ab12Cd",
        "launch": 1,
        "step": {"name": f"{PREFIX}-{SERVICE}-run-1", "id": f"{JOB}.2"},
        "log": f"/logs/{PREFIX}-{SERVICE}-run-1.log",
        "state": "running",
        **fields,
    }
    return {
        "allocation": {"name": PREFIX, "ends": "2026-11-16T14:00:00Z", "job": JOB},
        "site": {"node_tmp": "/nodetmp", "logs": "/logs", "node_python": "python3.11"},
        "services": {SERVICE: record},
    }


class Mounts:
    """A shared import's owner: its read-only proof is a control step of its own (`mounts`)."""

    spaces = staticmethod(lambda service: ["space-a"])
    argv = staticmethod(lambda workdir, python: ["bash", "/bundle/mount-check.sh", workdir])
    problems = staticmethod(lambda report, spaces: [])


# The control steps each verb makes from each state of a provider (a stop of a stopped service, a start of one that is
# not listed, make none): what the table of words the watch holds must cover.
MADE = {
    ("status", "running"): {"status", "mounts"},
    ("status", "stopped"): {"status"},
    ("stop", "running"): {"stop"},
    ("stop", "stopped"): set(),
    ("start", "running"): {"status"},
    ("start", "stopped"): set(),
    ("exec", "running"): {"exec"},
    ("exec", "stopped"): {"exec"},
}


@pytest.mark.parametrize(("verb", "state"), sorted(MADE))
def test_o58_a_verb_run_with_the_relays_tag_names_every_control_step_by_it_and_only_by_words_the_watch_knows(
    hpc, monkeypatch, tmp_path, verb, state
):
    """O58: with `--step-tag` every control step of the verb is `<allocation>-<service>-<word>-<tag>`, and every word
    it makes is one the watch looks for (the table the watch holds and the verbs make agree), so no step a timed-out
    verb queued can carry a name the watch does not know."""
    monkeypatch.setattr(hpc.control, "_SLEEP", lambda seconds: None)
    running = {f"{PREFIX}-{SERVICE}-run-1": f"{JOB}.2"} if state == "running" else {}
    launcher, store = VerbLauncher(running), VerbState(provider(state=state, mounts_proven=False), tmp_path)
    argv = ["/usr/sbin/op_worker", "ping"] if verb == "exec" else []
    hpc.control.run(verb, SERVICE, argv, launcher=launcher, state=store, stop_seconds=30, mounts=Mounts, step_tag=TAG)
    words = set()
    for step in launcher.control_names:
        word, tag = step.removeprefix(f"{PREFIX}-{SERVICE}-").rsplit("-", 1)
        assert tag == TAG, step
        words.add(word)
    assert words == MADE[(verb, state)]
    watched = hpc.late.names(PREFIX, SERVICE, verb, TAG)
    assert all(step in watched for step in launcher.control_names)
    assert watched == [name(word) for word in WORDS[verb]]


def test_o58_without_a_tag_the_control_steps_are_named_as_they_were(hpc, monkeypatch, tmp_path):
    """O58: a verb run by hand (no relay, no tag) keeps its names: the clock's millisecond stamp."""
    monkeypatch.setattr(hpc.control, "_SLEEP", lambda seconds: None)
    launcher = VerbLauncher({f"{PREFIX}-{SERVICE}-run-1": f"{JOB}.2"})
    hpc.control.run("status", SERVICE, [], launcher=launcher, state=VerbState(provider(), tmp_path), stop_seconds=30)
    (step,) = launcher.control_names
    assert step.startswith(f"{PREFIX}-{SERVICE}-status-")
    assert step.rsplit("-", 1)[1].isdigit()


def test_o58_a_start_names_its_service_step_as_it_did_and_the_watch_never_cancels_it(hpc, monkeypatch, tmp_path):
    """O58: a start's service step (`<allocation>-<service>-run-<n>`) is launched detached, in a session of its own, and
    recorded in the state file: it is the start itself arriving, never a step to cancel by name, tag or no tag."""
    monkeypatch.setattr(hpc.control, "_SLEEP", lambda seconds: None)
    launcher, store = VerbLauncher(), VerbState(provider(state="stopped"), tmp_path)
    hpc.control.run("start", SERVICE, [], launcher=launcher, state=store, stop_seconds=30, step_tag=TAG)
    assert launcher.service_names == [f"{PREFIX}-{SERVICE}-run-2"]
    assert not any("-run-" in step for step in hpc.late.names(PREFIX, SERVICE, "start", TAG))


# -- the command line -------------------------------------------------------------------------------------------


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for file in ("onezone.tar", "oneprovider.tar"):
        (images / file).write_bytes(b"tar")
        (images / f"{file}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


@pytest.fixture
def federation(hpc, tmp_path, site):
    """A federation `up` brought up over the fake Slurm, and the argv the relay builds for its watch."""
    fake, root = FakeSlurm(), tmp_path / "fed" / "root"
    driver = hpc.cli.make_driver(LAYOUT, str(root), runner=fake)
    driver.sleep, driver.poll_s = (lambda seconds: None), 0.0
    driver.slurm.sleep, driver.slurm.poll_s = (lambda seconds: None), 0.0
    driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
    driver.relay_available = lambda: False
    driver.up(hpc.settings.site_from_environment(site))
    state = driver.store.read()
    service = next(name for name, entry in state["services"].items() if entry["kind"] == "op")
    prefix = ["python3", "-I", "-B", "hpcfed.py", "--layout", str(LAYOUT), "--root", str(root)]
    return type("Federation", (), {"fake": fake, "root": root, "state": state, "service": service, "prefix": prefix})


def late_argv(federation, tag=TAG, verb="stop", window_s=0.2, poll_s=0.05):
    argv = watch_argv(federation.prefix, tag, verb, federation.service, REQUEST, window_s, poll_s, 1_000.0)
    return argv[argv.index("hpcfed.py") + 1 :]  # hpcfed's own arguments: what follows the script


def test_o58_the_relays_own_watch_command_cancels_the_late_step_by_its_exact_id_through_hpcfed(
    hpc, federation, tmp_path
):
    """O58: the argv the relay builds (`watch_argv`) is what `hpcfed.py` parses: against the fake Slurm of a federation
    that is up, a stop's step named by the tag appears in the held job and is cancelled by its exact id, the journal in
    the relay's directory holds it, and a step of another tag is left running."""
    fake, root = federation.fake, federation.root
    (root / "fault-relay").mkdir(mode=0o700)
    prefix = federation.state["allocation"]["name"]
    late, other = f"{prefix}-{federation.service}-stop-{TAG}", f"{prefix}-{federation.service}-stop-{'0' * 12}"
    fake.steps[late], fake.steps[other] = f"{fake.job_id}.91", f"{fake.job_id}.92"
    status = hpc.cli.main(late_argv(federation), {}, fake)
    assert status == 0
    assert [target for target in fake.cancelled if "." in target] == [f"{fake.job_id}.91"]
    assert other in fake.steps
    assert late not in fake.steps
    lines = [json.loads(line) for line in (root / "fault-relay" / JOURNAL).read_text(encoding="utf-8").splitlines()]
    assert [(line["event"], line.get("id")) for line in lines if line["event"] == "late_step"] == [
        ("late_step", f"{fake.job_id}.91")
    ]


@pytest.mark.parametrize(
    "change",
    [
        lambda argv: [*argv[: argv.index("--step-tag") + 1], "not-a-tag", *argv[argv.index("--step-tag") + 2 :]],
        lambda argv: [*argv[: argv.index("--request") + 1], "x", *argv[argv.index("--request") + 2 :]],
        lambda argv: [*argv[: argv.index("--window-s") + 1], "-1", *argv[argv.index("--window-s") + 2 :]],
        lambda argv: [*argv[: argv.index("--for-verb") + 1], "down", *argv[argv.index("--for-verb") + 2 :]],
        lambda argv: [*argv[: argv.index("--since") + 1], "-5", *argv[argv.index("--since") + 2 :]],
    ],
    ids=["tag", "request", "window", "verb", "since"],
)
def test_o58_the_watch_command_refuses_a_tag_a_request_a_window_or_a_verb_it_was_not_built_for(hpc, federation, change):
    """O58: the command watches only the four verbs' names, for a tag and a request id of the relay's making and a
    window within bounds: anything else is refused (exit 2) before it looks at Slurm."""
    calls = len(federation.fake.calls)
    assert hpc.cli.main(change(late_argv(federation)), {}, federation.fake) == 2
    assert [call for call in federation.fake.calls[calls:] if call[0] in ("squeue", "scancel", "sacct")] == []


def test_o58_a_verb_run_by_hpcfed_with_the_tag_in_its_arguments_is_named_by_it(hpc, federation):
    """O58: `hpcfed.py --step-tag <tag> status <service>`: the tag the relay puts before the verb reaches the control
    step's name, as the srun's `-J`, over the real launcher."""
    fake, service = federation.fake, federation.service
    prefix = federation.state["allocation"]["name"]
    fake.on_control = lambda srun: FakeProcess(hpc.slurm.Completed(1, "", ""))  # the node's answer is not the point
    hpc.cli.main(
        ["--layout", str(LAYOUT), "--root", str(federation.root), "--step-tag", TAG, "status", service], {}, fake
    )
    assert f"{prefix}-{service}-status-{TAG}" in fake.control_names()


def test_o58_the_constants_the_relay_hands_the_watch_are_the_ones_the_commands_default_to(hpc):
    """O58: the window and the poll the relay passes are plain numbers the command parses back."""
    assert LATE_WINDOW_S > 0
    assert LATE_POLL_S > 0
