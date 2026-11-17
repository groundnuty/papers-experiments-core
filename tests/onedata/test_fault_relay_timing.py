"""O58: where a relayed verb's time went. After each verb the relay reads the accounting once and puts, in the answer's
`timing`, the time from the verb's issue to each of its steps' accounted Start and from its answer to each step's
accounted End; a controller stall during a scored batch is then the site's in the record. A figure that cannot be given
is unknown, with the reason, and a timing never fails a verb or changes its answer.

The accounting here is a stand-in `sacct` that prints what a test says, over the real relay in this process.
"""

import json
import subprocess
from datetime import datetime

import pytest
from fault_support import SERVICES, FakeClock
from hpc_support import FEDERATION

from expcore_onedata.compose import CommandResult
from expcore_onedata.fault_hpc import Request, timing_evidence
from expcore_onedata.fault_relay import Relay, RelayClient
from expcore_onedata.fault_relay_late import step_name
from expcore_onedata.fault_relay_timing import read_timing, step_names

SERVICE = SERVICES["beta"]
JOB = "4242"
TAG = "ab12cd34ef56"
PREFIX = ["/opt/venv/bin/python", "-I", "-B", "hpcfed.py", "--layout", "LAYOUT", "--root", "ROOT"]
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "HOME": "/home/someone"}
HEAD = "2026-11-17T09:00:"  # sacct prints the login node's local time, to the second


def moment(second: int) -> float:
    """The epoch second of `HEAD` + second, read as local time as sacct prints it."""
    return datetime.strptime(f"{HEAD}{second:02d}", "%Y-%m-%dT%H:%M:%S").timestamp()


def row(name, step, start, end, state="COMPLETED"):
    return f"{JOB}.{step}|{name}|{start}|{end}|{state}|0:0"


def accounting(*rows, returncode=0):
    """A stand-in sacct, recording its calls: it prints the rows (whatever it was asked for)."""
    calls = []

    def sacct(argv, bound):
        calls.append((list(argv), bound))
        return CommandResult(returncode, "".join(f"{line}\n" for line in rows), "")

    sacct.calls = calls
    return sacct


def stop_name(tag=TAG):
    return step_name(FEDERATION, SERVICE, "stop", tag)


def timing_of(sacct, verb="stop", issued=None, answered=None, **options):
    names = step_names(FEDERATION, SERVICE, verb, TAG, options.pop("service_step", None))
    return read_timing(
        sacct,
        job=options.pop("job", JOB),
        names=names,
        issued=moment(0) if issued is None else issued,
        answered=moment(20) if answered is None else answered,
        **options,
    )


def test_o58_the_time_from_issue_to_the_steps_start_and_from_the_answer_to_its_end_is_read_from_one_accounting_call():
    """O58: the stop's step started 5 s after the verb was issued (0.4 s into its second) and ended 1 s before the
    answer: both figures, from one bounded sacct call for the whole job, the step found by its exact name."""
    sacct = accounting(
        row("batch", "batch", f"{HEAD}00", f"{HEAD}59"),
        row(stop_name(), 7, f"{HEAD}05", f"{HEAD}19"),
        row(stop_name("0" * 12), 6, f"{HEAD}01", f"{HEAD}02"),  # another request's: never read
    )
    timing = timing_of(sacct, issued=moment(0) + 0.4, answered=moment(20))
    (step,) = timing["steps"]
    assert (step["word"], step["id"], step["state"]) == ("stop", f"{JOB}.7", "COMPLETED")
    assert step["start_after_issue_s"] == pytest.approx(4.6)
    assert step["end_after_answer_s"] == pytest.approx(-1.0)
    assert "unknown" not in step
    assert (timing["accounting"], timing["reason"], timing["resolution_s"]) == ("read", None, 1)
    ((argv, bound),) = sacct.calls  # one call, for the held job, bounded
    assert argv[:3] == ["sacct", "-j", JOB]
    assert 0 < bound <= 10


def test_o58_a_step_that_ended_after_the_answer_has_a_positive_end_figure():
    """O58: a step that ran past the relay's answer (a late one) reads as time after the answer."""
    timing = timing_of(accounting(row(stop_name(), 7, f"{HEAD}05", f"{HEAD}45")), answered=moment(20))
    assert timing["steps"][0]["end_after_answer_s"] == pytest.approx(25.0)


@pytest.mark.parametrize(
    ("start", "end", "unknown"),
    [
        ("Unknown", f"{HEAD}19", {"start_after_issue_s": "not_recorded"}),
        (f"{HEAD}05", "Unknown", {"end_after_answer_s": "not_recorded"}),  # still running, or not yet accounted
        ("garbage", f"{HEAD}19", {"start_after_issue_s": "unparsable"}),
    ],
)
def test_o58_a_figure_the_accounting_does_not_give_is_unknown_with_its_reason_and_the_other_is_kept(
    start, end, unknown
):
    """O58: a Start or End the accounting does not hold (or holds unreadably) is None and says why; the other figure of
    the step is still given."""
    step = timing_of(accounting(row(stop_name(), 7, start, end)))["steps"][0]
    assert step["unknown"] == unknown
    for figure in unknown:
        assert step[figure] is None
    assert [step[name] is None for name in ("start_after_issue_s", "end_after_answer_s")].count(False) == 1


def test_o58_a_step_the_accounting_does_not_list_is_unknown_as_no_such_step():
    """O58: the verb made no such step (or the accounting does not show it): unknown, never a figure nobody read."""
    timing = timing_of(accounting(row("something-else", 3, f"{HEAD}01", f"{HEAD}02")))
    (step,) = timing["steps"]
    assert (step["start_after_issue_s"], step["end_after_answer_s"], step["id"]) == (None, None, None)
    assert step["unknown"] == {"start_after_issue_s": "no_such_step", "end_after_answer_s": "no_such_step"}
    assert timing["accounting"] == "read"


@pytest.mark.parametrize(
    ("sacct", "accounting_word", "reason"),
    [
        (accounting(returncode=1), "unreadable", "sacct_failed"),
        (
            lambda argv, bound: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, bound)),
            "unreadable",
            "sacct_timeout",
        ),
        (lambda argv, bound: (_ for _ in ()).throw(FileNotFoundError("sacct")), "unreadable", "sacct_not_run"),
        (lambda argv, bound: (_ for _ in ()).throw(ValueError("odd")), "unreadable", "sacct_ValueError"),
    ],
    ids=["failed", "timeout", "not_run", "other"],
)
def test_o58_an_unreadable_sacct_gives_unknown_with_the_reason_and_never_raises(sacct, accounting_word, reason):
    """O58: the accounting cannot be read: every figure is unknown, the whole answer says why, and nothing raises."""
    timing = timing_of(sacct)
    assert (timing["accounting"], timing["reason"]) == (accounting_word, reason)
    (step,) = timing["steps"]
    assert step["start_after_issue_s"] is None
    assert step["end_after_answer_s"] is None
    assert set(step["unknown"].values()) == {reason}


def test_o58_a_reading_with_no_time_left_or_no_job_is_skipped_and_calls_nothing():
    """O58: a verb that used the client's whole wait leaves no time for the accounting, and a state that names no job
    has nothing to ask: skipped, with the reason, and sacct is not called."""
    sacct = accounting()
    assert timing_of(sacct, budget_s=0.5)["reason"] == "no_time_left"
    assert timing_of(sacct, job=None)["reason"] == "no_job"
    assert sacct.calls == []


def test_o58_only_the_verbs_own_names_are_looked_for_and_a_start_that_launched_adds_its_service_step():
    """O58: a status looks for its status and its read-only check, a stop for its stop, an exec for its exec, a start
    for its status read and, once it launched, the service step the state recorded."""
    words = {verb: [word for word, _ in step_names(FEDERATION, SERVICE, verb, TAG)] for verb in SERVICES_VERBS}
    assert words == {"status": ["status", "mounts"], "stop": ["stop"], "start": ["status"], "exec": ["exec"]}
    run = f"{FEDERATION}-{SERVICE}-run-3"
    assert step_names(FEDERATION, SERVICE, "start", TAG, run)[-1] == ("run", run)
    sacct = accounting(row(run, 9, f"{HEAD}03", f"{HEAD}08"))
    timing = timing_of(sacct, verb="start", service_step=run)
    assert [step["word"] for step in timing["steps"]] == ["status", "run"]
    assert timing["steps"][1]["start_after_issue_s"] == pytest.approx(3.0)


SERVICES_VERBS = ("status", "stop", "start", "exec")


# -- the relay puts it in the answer ----------------------------------------------------------------------------------


class Verbs:
    """The federation's verbs behind the relay: each command recorded, each answered by `behaviour`."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or (lambda argv: CommandResult(0, '{"running": true}\n'))
        self.calls = []

    def __call__(self, argv, *, timeout_s):
        self.calls.append((list(argv), timeout_s))
        return self.behaviour(argv)

    @property
    def tag(self):
        """The step tag of the latest verb the relay ran."""
        return self.calls[-1][0][len(PREFIX) + 1]


def no_watch(argv, env):
    return type("Handle", (), {"poll": staticmethod(lambda: 0)})()


def state_of(launch=None, step=None, job=JOB):
    entry = {"kind": "op", "launch": 1, "state": "running"}
    if launch is not None:
        entry.update(launch=launch, step=step)
    return {
        "format": "expcore-hpc-federation/1",
        "allocation": {"name": FEDERATION, **({} if job is None else {"job": job})},
        "services": {"zone": {"kind": "oz"}, SERVICE: entry},
    }


def make(tmp_path, behaviour=None, sacct=None, state=None, **options):
    tmp_path.mkdir(exist_ok=True)
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "hpc-federation.json").write_text(json.dumps(state or state_of()), encoding="utf-8")
    verbs = Verbs(behaviour)
    relay = Relay(root, PREFIX, runner=verbs, environ=ENVIRONMENT, watcher=no_watch, sacct=sacct, **options)
    return root, relay, verbs


def ask(root, relay, request, timeout_s=10):
    clock = FakeClock()

    def pause(seconds):
        clock.sleep(seconds)
        relay.serve_once()

    try:
        return RelayClient(root, wait_s=5, clock=clock, sleep=pause)(request, timeout_s=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as error:
        return error


def test_o58_the_answer_carries_the_timing_of_the_verbs_steps_and_the_command_is_unchanged(tmp_path):
    """O58: an answered verb's end is what it was; its timing is beside it, found by the tag the relay gave the verb."""
    seen = []
    holder = {}

    def sacct(argv, bound):
        seen.append(argv)
        name = step_name(FEDERATION, SERVICE, "status", holder["verbs"].tag)
        return CommandResult(0, row(name, 5, f"{HEAD}01", f"{HEAD}02") + "\n")

    root, relay, verbs = make(tmp_path, sacct=sacct)
    holder["verbs"] = verbs
    with relay.holding():
        done = ask(root, relay, Request("status", SERVICE))
    assert done == CommandResult(0, '{"running": true}\n')  # the timing is no part of its equality
    assert [step["word"] for step in done.timing["steps"]] == ["status", "mounts"]
    assert done.timing["steps"][0]["id"] == f"{JOB}.5"
    assert done.timing["steps"][1]["unknown"]["start_after_issue_s"] == "no_such_step"  # no read-only check was made
    assert len(seen) == 1


def test_o58_a_timed_out_verb_is_still_answered_timed_out_and_its_timing_rides_on_the_exception(tmp_path):
    """O58: a verb at its bound has its steps' Start figures (where the controller's stall shows) and unknown Ends; the
    answer is `timed_out` as ever, which the client raises as the command with no answer it always was."""

    def hangs(argv):
        raise subprocess.TimeoutExpired(argv, 1)

    root, relay, _ = make(tmp_path, hangs, accounting())
    with relay.holding():
        error = ask(root, relay, Request("stop", SERVICE), timeout_s=3)
    assert isinstance(error, subprocess.TimeoutExpired)
    assert error.timing["steps"][0]["unknown"]["start_after_issue_s"] == "no_such_step"


def test_o58_an_unreadable_accounting_never_fails_the_verbs_answer(tmp_path):
    """O58: sacct fails: the answer is the verb's own, its timing says unreadable with the reason."""
    root, relay, _ = make(tmp_path, sacct=accounting(returncode=1))
    with relay.holding():
        done = ask(root, relay, Request("status", SERVICE))
    assert done == CommandResult(0, '{"running": true}\n')
    assert (done.timing["accounting"], done.timing["reason"]) == ("unreadable", "sacct_failed")


def test_o58_a_state_with_no_job_skips_the_reading_and_the_answer_is_still_the_verbs(tmp_path):
    """O58: a state file that names no job leaves nothing to ask: skipped, with the reason, and no accounting call."""
    sacct = accounting()
    root, relay, _ = make(tmp_path, sacct=sacct, state=state_of(job=None))
    with relay.holding():
        done = ask(root, relay, Request("status", SERVICE))
    assert (done.timing["accounting"], done.timing["reason"]) == ("skipped", "no_job")
    assert sacct.calls == []


def test_o58_a_start_that_launched_has_its_service_step_timed_and_one_that_did_not_does_not(tmp_path):
    """O58: the state's launch count says whether the start made a service step: only then is the step it recorded (the
    name `<allocation>-<service>-run-<n>`) one of this verb's. A start that changes nothing leaves the earlier launch's
    step, which is never this verb's."""
    first = {"name": f"{FEDERATION}-{SERVICE}-run-1"}
    run = f"{FEDERATION}-{SERVICE}-run-2"
    started = state_of(1, first)

    root, relay, verbs = make(tmp_path / "a", sacct=accounting(row(run, 12, f"{HEAD}03", f"{HEAD}08")), state=started)

    def launches(argv):
        state = json.loads((root / "hpc-federation.json").read_text(encoding="utf-8"))
        state["services"][SERVICE].update(launch=2, step={"name": run, "id": f"{JOB}.12"})
        (root / "hpc-federation.json").write_text(json.dumps(state), encoding="utf-8")
        return CommandResult(0, "")

    verbs.behaviour = launches
    with relay.holding():
        done = ask(root, relay, Request("start", SERVICE))
    assert [step["word"] for step in done.timing["steps"]] == ["status", "run"]
    assert done.timing["steps"][1]["id"] == f"{JOB}.12"

    root, relay, _ = make(tmp_path / "b", sacct=accounting(), state=started)
    with relay.holding():
        done = ask(root, relay, Request("start", SERVICE))  # the verb changes nothing: no new launch
    assert [step["word"] for step in done.timing["steps"]] == ["status"]


def pending_state(launch, name, listed=None):
    """The state file of a start whose service step an earlier attempt launched: `starting`, its step recorded by name,
    its id not yet (hpcfed claims the launch before the step exists); `listed` is the id once it was listed."""
    state = state_of()
    state["services"][SERVICE].update(
        launch=launch, state="starting" if listed is None else "running", step={"name": name, "id": listed}
    )
    return state


def test_o58_a_start_that_adopts_the_step_an_earlier_attempt_launched_has_that_step_timed(tmp_path):
    """O58: a start that stalled past its 60 s wait for its service step to be listed leaves the state `starting`, the
    step recorded by name only; the retried start finds the step listed and takes it over (`running`) with the launch
    count unchanged. The count alone said it launched nothing, so the step's own timing, which is where the stalled
    start's time went, was lost: the answer of the start that adopts it holds it (its `start_after_issue_s` is
    negative: the step began before this verb was issued)."""
    run = f"{FEDERATION}-{SERVICE}-run-2"
    root, relay, verbs = make(
        tmp_path, sacct=accounting(row(run, 12, f"{HEAD}03", f"{HEAD}08")), state=pending_state(2, run)
    )

    def adopts(argv):
        state = json.loads((root / "hpc-federation.json").read_text(encoding="utf-8"))
        state["services"][SERVICE].update(state="running", step={"name": run, "id": f"{JOB}.12"})
        (root / "hpc-federation.json").write_text(json.dumps(state), encoding="utf-8")
        return CommandResult(0, "")

    verbs.behaviour = adopts
    with relay.holding():
        done = ask(root, relay, Request("start", SERVICE))
    assert [step["word"] for step in done.timing["steps"]] == ["status", "run"]
    assert (done.timing["steps"][1]["id"], done.timing["steps"][1]["state"]) == (f"{JOB}.12", "COMPLETED")


def test_o58_a_start_that_found_its_service_already_up_adopts_nothing(tmp_path):
    """O58: a start on a service that was `running` before it (nothing launched, nothing adopted) leaves the step of
    the launch long ago, which is never this verb's."""
    run = f"{FEDERATION}-{SERVICE}-run-2"
    running = pending_state(2, run, listed=f"{JOB}.12")
    root, relay, _ = make(tmp_path, sacct=accounting(row(run, 12, f"{HEAD}03", f"{HEAD}08")), state=running)
    with relay.holding():
        done = ask(root, relay, Request("start", SERVICE))
    assert [step["word"] for step in done.timing["steps"]] == ["status"]


def test_o58_a_retried_start_whose_step_is_still_not_listed_has_no_service_step_to_time(tmp_path):
    """O58: the retry did not see the step listed either: the state is as it was (`starting`, no id), so nothing was
    adopted and no figure is invented for a step that is not known to exist."""
    run = f"{FEDERATION}-{SERVICE}-run-2"
    root, relay, _ = make(tmp_path, sacct=accounting(), state=pending_state(2, run))
    with relay.holding():
        done = ask(root, relay, Request("start", SERVICE))
    assert [step["word"] for step in done.timing["steps"]] == ["status"]


def test_o58_an_adopted_steps_negative_start_figure_reaches_the_driver_as_it_is():
    """O58: the figure is negative when the step began before the verb was issued, and the driver's check keeps a
    finite number's sign (a step that ended before the answer has one too)."""
    timing = read_timing(
        accounting(row("any-run-2", 12, f"{HEAD}03", f"{HEAD}08")),
        job=JOB,
        names=[("run", "any-run-2")],
        issued=moment(40),
        answered=moment(50),
    )
    (step,) = timing_evidence(timing)["steps"]
    assert (step["word"], step["start_after_issue_s"], step["end_after_answer_s"]) == ("run", -37.0, -42.0)


def test_o58_a_timing_that_is_no_object_is_no_timing_to_the_client(tmp_path):
    """O58: the client reads the relay's file and checks it: an answer whose `timing` is not the object the relay writes
    carries none."""
    root, relay, _ = make(tmp_path)
    relay._timing = lambda *a, **k: {"accounting": "other", "steps": "x"}
    with relay.holding():
        done = ask(root, relay, Request("status", SERVICE))
    assert done == CommandResult(0, '{"running": true}\n')
    assert done.timing is None


def test_o58_a_timing_that_fails_to_be_made_is_no_timing_and_the_answer_is_the_verbs(tmp_path):
    """O58: anything that goes wrong in making the timing leaves the answer as it was."""

    def broken(argv, bound):
        raise RuntimeError("boom")

    root, relay, _ = make(tmp_path, sacct=broken)
    with relay.holding():
        done = ask(root, relay, Request("status", SERVICE))
    assert done.returncode == 0
    assert done.timing["reason"] == "sacct_RuntimeError"
