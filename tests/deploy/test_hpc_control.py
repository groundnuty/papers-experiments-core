"""The four service verbs of an HPC federation (deploy/hpc-federation/hpcfed_control.py), against fakes (O58).

The module is pure logic over two injected objects, the step launcher and the state file that `hpcfed.py` supplies.
Here both are fakes, so nothing reaches a cluster, and the clock is a fake too, so the bounds cost no time.
"""

import copy
import importlib.util
import io
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
spec = importlib.util.spec_from_file_location("hpcfed_control", HPC / "hpcfed_control.py")
control = sys.modules.get("hpcfed_control") or importlib.util.module_from_spec(spec)
sys.modules["hpcfed_control"] = control
spec.loader.exec_module(control)

PREFIX = "hpcop-demo-ab12cd34"
NODE = "node-x1"
WORKDIR = "/nodetmp/hpcop-op.Ab12Cd"
ZONE_WORKDIR = "/nodetmp/hpcop-oz.Zz34Yy"
ENDS = "2026-11-16T14:00:00Z"
ENDS_EPOCH = int(datetime(2026, 11, 16, 14, 0, 0, tzinfo=UTC).timestamp())
BUNDLE = Path(control.BUNDLE)
UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def document(**service):
    """The state file's relevant part: an allocation and a provider and a zone, both running."""
    provider = {
        "kind": "op",
        "node": NODE,
        "workdir": WORKDIR,
        "launch": 1,
        "step": {"name": f"{PREFIX}-prov-a-run-1", "id": "77.2"},
        "log": f"/logs/{PREFIX}-prov-a-run-1.log",
        "state": "running",
    }
    provider.update(service)
    zone = {
        "kind": "oz",
        "node": "node-x0",
        "workdir": ZONE_WORKDIR,
        "launch": 1,
        "step": {"name": f"{PREFIX}-zone-run-1", "id": "77.1"},
        "log": f"/logs/{PREFIX}-zone-run-1.log",
        "state": "running",
    }
    return {"allocation": {"name": PREFIX, "ends": ENDS}, "services": {"prov-a": provider, "zone": zone}}


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeState:
    """A state file: read() copies, update(fn) is atomic (fn runs on a copy; an exception from it writes nothing)."""

    def __init__(self, doc):
        self.doc = doc
        self.history = []

    def read(self):
        return copy.deepcopy(self.doc)

    def update(self, fn):
        self.doc = fn(copy.deepcopy(self.doc))
        self.history.append(copy.deepcopy(self.doc))
        return copy.deepcopy(self.doc)

    def service(self, name="prov-a"):
        return self.doc["services"][name]


class FakeLauncher:
    """The step launcher. `steps` is the step list (name to id); `on_control(argv)` answers a control step and may
    change the list; `on_service(name)` is what a launched service step does to the list (lists it by default)."""

    def __init__(self, state, steps=None):
        self.state, self.steps = state, dict(steps or {})
        self.controls, self.services, self.cancels, self.listings = [], [], [], []
        self.on_control = lambda argv: Done()
        self.on_service = lambda name: self.steps.__setitem__(name, "88.1")

    def control(self, node, argv, *, name, timeout_s):
        self.controls.append(
            {"node": node, "argv": argv, "name": name, "timeout_s": timeout_s, "state": self.state.read()}
        )
        answer = self.on_control(argv)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def service(self, node, argv, *, kind, name, log):
        self.services.append(
            {"node": node, "argv": argv, "kind": kind, "name": name, "log": log, "state": self.state.read()}
        )
        self.on_service(name)

    def listed(self, name):
        self.listings.append(name)
        return self.steps.get(name)

    def cancel(self, step_id):
        self.cancels.append(step_id)
        for name, listed in list(self.steps.items()):
            if listed == step_id:
                del self.steps[name]


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(control, "_SLEEP", fake.sleep)
    monkeypatch.setattr(control, "_MONOTONIC", fake.monotonic)
    return fake


@pytest.fixture
def world():
    """A federation whose provider runs: its state, its launcher, and the output the verbs write."""
    state = FakeState(document())
    launcher = FakeLauncher(state, {f"{PREFIX}-prov-a-run-1": "77.2", f"{PREFIX}-zone-run-1": "77.1"})
    return type("World", (), {"state": state, "launcher": launcher})


def verb(world, name, service="prov-a", argv=(), **kwargs):
    out, err = io.StringIO(), io.StringIO()
    status = control.run(
        name, service, list(argv), launcher=world.launcher, state=world.state, out=out, err=err, **kwargs
    )
    return status, out.getvalue(), err.getvalue()


def alive(pid=4242, start="777", ready=False):
    return Done(0, json.dumps({"alive": True, "pid": pid, "start": start, "ready": ready}) + "\n")


DEAD = Done(0, json.dumps({"alive": False, "pid": None, "start": None, "ready": False}) + "\n")
TEARDOWN = 'hpcop-stop {"ended": "exit", "seconds": 4.2}\n'


# --- what the module is -------------------------------------------------------------------------------------------


def test_o58_the_four_verbs_are_stop_start_status_exec():
    """O58: the names M1's dispatch tests membership of."""
    assert control.VERBS == ("stop", "start", "status", "exec")


def test_o58_the_docstring_states_the_contract_the_hook_and_that_nothing_has_run_live():
    """O58: a reader of the module finds the contract, how hpcfed.py calls it, and that no cluster has seen it."""
    doc = control.__doc__
    assert "if verb in hpcfed_control.VERBS" in doc
    assert "hpcfed_control.run(" in doc
    assert "Written, not run" in doc
    for word in ("control(node, argv", "service(node, argv", "listed(name)", "cancel(step_id)", "update(fn)"):
        assert word in doc


def test_o58_the_bundle_is_the_one_beside_the_module():
    """O58: the scripts every step runs are this checkout's, never found anywhere else."""
    assert BUNDLE == HPC / "bundle"
    for script in ("status.sh", "teardown.sh", "restart.sh", "exec.sh"):
        assert (BUNDLE / script).is_file()


# --- the services it accepts --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["status", "stop", "start", "exec"])
@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["services"].pop("prov-a"),
        lambda doc: doc["services"]["prov-a"].update(kind="agent"),
        lambda doc: doc["services"]["prov-a"].pop("kind"),
        lambda doc: doc["services"]["prov-a"].update(workdir="/nodetmp/elsewhere"),
        lambda doc: doc["services"]["prov-a"].update(workdir="/nodetmp/hpcop-op.Ab12C"),
        lambda doc: doc["services"]["prov-a"].update(workdir="/nodetmp/hpcop-oz.Ab12Cd"),
        lambda doc: doc["services"]["prov-a"].update(workdir="/../hpcop-op.Ab12Cd"),
        lambda doc: doc["services"]["prov-a"].update(workdir="/nodetmp/../x/hpcop-op.Ab12Cd"),
        lambda doc: doc["services"]["prov-a"].update(workdir=None),
        lambda doc: doc["services"]["prov-a"].update(node=""),
        lambda doc: doc["allocation"].pop("name"),
        lambda doc: doc.pop("services"),
    ],
    ids=[
        "unknown service",
        "kind not op or oz",
        "no kind",
        "work directory not hpcop",
        "work directory with a short suffix",
        "work directory of the other kind",
        "work directory with ..",
        "work directory with .. inside",
        "no work directory",
        "no node",
        "no allocation name",
        "no services",
    ],
)
def test_o58_a_service_the_state_does_not_describe_is_refused_without_naming_a_node_or_a_path(name, mutate, world):
    """O58: exit 2, nothing launched or run, nothing on stdout, and a message that names neither a node nor a path."""
    doc = document()
    mutate(doc)
    world.state.doc = doc
    status, out, err = verb(world, name, argv=["true"])
    assert status == 2
    assert out == ""
    assert err.startswith("hpcfed: ")
    assert NODE not in err
    assert "/" not in err
    assert world.launcher.controls == []
    assert world.launcher.services == []
    assert world.state.history == []


def test_o58_an_unknown_verb_is_refused(world):
    """O58: exit 2 for a verb that is not one of the four."""
    assert verb(world, "restart")[0] == 2
    assert world.launcher.controls == []


def test_o58_a_state_that_cannot_be_read_is_refused_naming_only_the_error_type(world):
    """O58: exit 2, and the message holds the error's type, never its text (which names a path)."""

    def unreadable():
        raise OSError("/secret/path/state.json")

    world.state.read = unreadable
    status, out, err = verb(world, "status")
    assert status == 2
    assert "OSError" in err
    assert "secret" not in err
    assert out == ""


@pytest.mark.parametrize("bad", [0, -1, 2.5, "600", True])
def test_o58_stop_seconds_must_be_a_positive_whole_number(bad, world):
    """O58: refused before anything runs."""
    assert verb(world, "stop", stop_seconds=bad)[0] == 2
    assert world.launcher.controls == []


# --- status -------------------------------------------------------------------------------------------------------


def test_o58_status_of_a_running_service_prints_one_json_line_with_its_identity(world):
    """O58: running is listed and alive; identity is launch:pid:start; node, step and state come from the state."""
    world.launcher.on_control = lambda argv: alive(ready=True)
    status, out, err = verb(world, "status")
    assert status == 0, err
    assert out.count("\n") == 1
    assert json.loads(out) == {
        "service": "prov-a",
        "state": "running",
        "running": True,
        "identity": "1:4242:777",
        "launch": 1,
        "node": NODE,
        "step": f"{PREFIX}-prov-a-run-1",
        "ready": True,
    }
    [call] = world.launcher.controls
    assert call["node"] == NODE
    assert call["argv"] == ["bash", str(BUNDLE / "status.sh"), "op", "--workdir", WORKDIR]
    assert call["timeout_s"] == 60
    assert call["name"].startswith(f"{PREFIX}-prov-a-status-")


@pytest.mark.parametrize(
    ("listed", "reading", "running"),
    [(True, "alive", True), (False, "alive", False), (True, "dead", False), (False, "dead", False)],
)
def test_o58_a_service_runs_only_when_its_step_is_listed_and_its_process_is_alive(listed, reading, running, world):
    """O58: both halves are needed, and an identity exists only for a running service."""
    if not listed:
        world.launcher.steps.clear()
    world.launcher.on_control = lambda argv: alive() if reading == "alive" else DEAD
    status, out, _ = verb(world, "status")
    record = json.loads(out)
    assert status == 0
    assert record["running"] is running
    assert (record["identity"] is not None) is running
    assert record["ready"] is False


def test_o58_a_service_is_ready_only_when_it_runs_and_its_marker_is_current(world):
    """O58: ready comes from status.sh, and only while the service is running."""
    world.launcher.on_control = lambda argv: alive(ready=True)
    world.launcher.steps.clear()
    assert json.loads(verb(world, "status")[1])["ready"] is False


@pytest.mark.parametrize(
    "answer",
    [
        Done(1, ""),
        Done(0, ""),
        Done(0, "not json\n"),
        Done(0, "{}\n"),
        Done(0, '{"alive": "yes"}\n'),
        Done(0, '{"alive": true, "pid": 0, "start": "5"}\n'),
        Done(0, '{"alive": true, "pid": 5, "start": null}\n'),
        Done(0, '{"alive": false}\n{"alive": false}\n'),
        TimeoutError(),
    ],
    ids=["failed", "silent", "not json", "no alive", "alive not a bool", "no pid", "no start", "two lines", "timeout"],
)
def test_o58_a_status_that_cannot_be_read_is_exit_1_with_nothing_on_stdout(answer, world):
    """O58: a control step that fails, times out or answers something unreadable is a reading that failed."""
    world.launcher.on_control = lambda argv: answer
    status, out, err = verb(world, "status")
    assert status == 1
    assert out == ""
    assert NODE not in err
    assert "/" not in err


def test_o58_status_changes_nothing_in_the_state(world):
    """O58: it reads."""
    world.launcher.on_control = lambda argv: alive()
    verb(world, "status")
    assert world.state.history == []


# --- stop ---------------------------------------------------------------------------------------------------------


def stopping_launcher(world, stdout=TEARDOWN, returncode=0, steps_end=True):
    """A control step that runs teardown: it prints its line, and the service step ends with it."""

    def on_control(argv):
        if steps_end:
            world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1", None)
        return Done(returncode, stdout)

    world.launcher.on_control = on_control


def test_o58_a_stop_records_stopping_before_its_control_step_then_stopped_when_the_step_is_gone(world, clock):
    """O58: the state says stopping before teardown runs, and stopped once teardown exited 0 and the step is gone."""
    stopping_launcher(world)
    status, _out, err = verb(world, "stop")
    assert status == 0, err
    [call] = world.launcher.controls
    assert call["state"]["services"]["prov-a"]["state"] == "stopping"
    assert world.state.service()["state"] == "stopped"


def test_o58_a_stop_runs_teardown_with_keep_the_allocations_end_and_the_stop_bound(world, clock):
    """O58: teardown.sh --keep --keep-until <allocation end as epoch> --timeout <stop_seconds>, bounded past it."""
    stopping_launcher(world)
    verb(world, "stop", stop_seconds=300)
    [call] = world.launcher.controls
    assert call["node"] == NODE
    assert call["argv"] == [
        "bash",
        str(BUNDLE / "teardown.sh"),
        "op",
        "--workdir",
        WORKDIR,
        "--keep",
        "--keep-until",
        str(ENDS_EPOCH),
        "--timeout",
        "300",
    ]
    assert call["timeout_s"] == 360
    assert call["name"].startswith(f"{PREFIX}-prov-a-stop-")


def test_o58_a_stop_prints_its_evidence_and_records_the_same_object_under_the_service(world, clock):
    """O58: one JSON line: stopped, how it ended (teardown's line), the seconds from the request to the step gone, and
    both times; the same object is appended to `stops` in the state."""

    def on_control(argv):
        clock.now += 4.25  # teardown takes a while
        world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1")
        return Done(0, 'noise\nhpcop-stop {"ended": "kill", "seconds": 3.9}\n')

    world.launcher.on_control = on_control
    status, out, _ = verb(world, "stop")
    assert status == 0
    assert out.count("\n") == 1
    evidence = json.loads(out)
    assert list(evidence) == ["service", "stopped", "ended", "seconds", "requested_utc", "confirmed_utc"]
    assert evidence["service"] == "prov-a"
    assert evidence["stopped"] is True
    assert evidence["ended"] == "kill"
    assert evidence["seconds"] == pytest.approx(4.25)
    assert UTC.fullmatch(evidence["requested_utc"])
    assert UTC.fullmatch(evidence["confirmed_utc"])
    assert evidence["confirmed_utc"] >= evidence["requested_utc"]
    assert world.state.service()["stops"] == [evidence]


@pytest.mark.parametrize(
    "stdout",
    ["", "hpcop-stop not json\n", 'hpcop-stop {"ended": "gone"}\n', 'hpcop-stop {"seconds": 1}\n', "stopped\n"],
)
def test_o58_the_ended_word_is_null_when_teardowns_line_cannot_be_read(stdout, world, clock):
    """O58: unreadable evidence is null, not a guess, and the stop still succeeds on teardown's exit status."""
    stopping_launcher(world, stdout=stdout)
    status, out, _ = verb(world, "stop")
    assert status == 0
    assert json.loads(out)["ended"] is None
    assert json.loads(out)["stopped"] is True


@pytest.mark.parametrize("ended", ["exit", "kill", "none"])
def test_o58_the_ended_word_is_teardowns(ended, world, clock):
    """O58: exit, kill or none, as teardown printed it."""
    stopping_launcher(world, stdout=f'hpcop-stop {{"ended": "{ended}", "seconds": 0.5}}\n')
    assert json.loads(verb(world, "stop")[1])["ended"] == ended


def test_o58_a_stop_of_a_stopped_service_answers_none_at_once_and_changes_nothing(world, clock):
    """O58: idempotent: state stopped and the step not listed: ended none, exit 0, no control step, no new record."""
    world.state.doc["services"]["prov-a"]["state"] = "stopped"
    world.launcher.steps.clear()
    for _ in range(2):
        status, out, _ = verb(world, "stop")
        assert status == 0
        evidence = json.loads(out)
        assert evidence["stopped"] is True
        assert evidence["ended"] == "none"
        assert evidence["seconds"] == 0.0
    assert world.launcher.controls == []
    assert world.state.history == []
    assert "stops" not in world.state.service()


def test_o58_a_stopped_service_whose_step_is_still_listed_is_stopped_for_real(world, clock):
    """O58: `stopped` in the state is not enough while the step is still in the list: the stop runs."""
    world.state.doc["services"]["prov-a"]["state"] = "stopped"
    stopping_launcher(world, stdout='hpcop-stop {"ended": "none", "seconds": 0}\n')
    assert verb(world, "stop")[0] == 0
    assert len(world.launcher.controls) == 1


@pytest.mark.parametrize("answer", [Done(1, ""), Done(2, TEARDOWN), TimeoutError(), RuntimeError("boom")])
def test_o58_a_stop_whose_teardown_fails_leaves_the_state_stopping(answer, world, clock):
    """O58: teardown that did not exit 0, timed out or could not run: exit 1, `stopping` stays, the evidence says
    stopped false, and no step is cancelled (the keep marks may not have been written)."""
    world.launcher.on_control = lambda argv: answer
    status, out, err = verb(world, "stop")
    assert status == 1
    evidence = json.loads(out)
    assert evidence["stopped"] is False
    assert evidence["seconds"] is None
    assert evidence["confirmed_utc"] is None
    assert world.state.service()["state"] == "stopping"
    assert world.state.service()["stops"] == [evidence]
    assert world.launcher.cancels == []
    assert "boom" not in err
    assert NODE not in err


def test_o58_a_stop_whose_step_never_leaves_the_list_cancels_it_by_its_id_then_fails_if_it_stays(world, clock):
    """O58: teardown exited 0 but the step is listed at the bound: cancelled by its own id, once; still listed after
    that: exit 1 and `stopping` stays."""
    stopping_launcher(world, steps_end=False)
    world.launcher.cancel = lambda step_id: world.launcher.cancels.append(step_id)  # the cancel does not take
    status, out, _ = verb(world, "stop")
    assert status == 1
    assert world.launcher.cancels == ["77.2"]
    assert json.loads(out)["stopped"] is False
    assert world.state.service()["state"] == "stopping"
    assert clock.slept >= control.STEP_GONE_WAIT_S


def test_o58_a_step_that_lingers_after_a_good_teardown_is_cancelled_and_the_stop_succeeds(world, clock):
    """O58: the cancel takes: the step is gone, so the service is recorded stopped."""
    stopping_launcher(world, steps_end=False)
    status, out, _ = verb(world, "stop")
    assert status == 0
    assert world.launcher.cancels == ["77.2"]
    assert json.loads(out)["stopped"] is True
    assert world.state.service()["state"] == "stopped"


def test_o58_a_stop_waits_for_a_step_that_ends_a_moment_after_teardown(world, clock):
    """O58: the step leaves the list on the third poll: no cancel, and the wait was short of the bound."""
    polls = []

    def listed(name):
        polls.append(name)
        return "77.2" if len(polls) < 3 else None

    world.launcher.listed = listed
    world.launcher.on_control = lambda argv: Done(0, TEARDOWN)
    status, _out, _ = verb(world, "stop")
    assert status == 0
    assert world.launcher.cancels == []
    assert 0 < clock.slept < control.STEP_GONE_WAIT_S


@pytest.mark.parametrize(
    "ends",
    [ENDS, "2026-11-16T14:00:00+00:00", "2026-11-16T14:00:00", "2026-11-16 14:00:00", str(ENDS_EPOCH), ENDS_EPOCH],
)
def test_o58_the_allocations_end_is_read_as_utc_in_each_form(ends, world, clock):
    """O58: a Z, an offset, no zone (UTC), a space, epoch seconds as text or a number: all the same epoch."""
    world.state.doc["allocation"]["ends"] = ends
    stopping_launcher(world)
    verb(world, "stop")
    argv = world.launcher.controls[0]["argv"]
    assert argv[argv.index("--keep-until") + 1] == str(ENDS_EPOCH)


@pytest.mark.parametrize("ends", [None, "", "soon", True, "2026-13-45"])
def test_o58_an_allocation_without_a_usable_end_refuses_a_stop_before_it_changes_anything(ends, world, clock):
    """O58: without a time to keep the directory until, a stop could let the sweep remove it: refused, exit 2."""
    world.state.doc["allocation"]["ends"] = ends
    status, out, _ = verb(world, "stop")
    assert status == 2
    assert out == ""
    assert world.launcher.controls == []
    assert world.state.history == []


def test_o58_a_stop_of_the_zone_runs_teardown_for_an_onezone(world, clock):
    """O58: the kind and the work directory are the service's own."""

    def on_control(argv):
        world.launcher.steps.pop(f"{PREFIX}-zone-run-1")
        return Done(0, TEARDOWN)

    world.launcher.on_control = on_control
    assert verb(world, "stop", service="zone")[0] == 0
    [call] = world.launcher.controls
    assert call["node"] == "node-x0"
    assert call["argv"][2:5] == ["oz", "--workdir", ZONE_WORKDIR]


# --- start --------------------------------------------------------------------------------------------------------


def stopped_provider(world):
    world.state.doc["services"]["prov-a"]["state"] = "stopped"
    world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1")


def test_o58_a_start_of_a_running_service_launches_nothing(world, clock):
    """O58: listed and alive: exit 0, no service step, no state change."""
    world.launcher.on_control = lambda argv: alive()
    assert verb(world, "start")[0] == 0
    assert world.launcher.services == []
    assert world.state.history == []
    [call] = world.launcher.controls
    assert call["argv"][1].endswith("status.sh")


def test_o58_a_start_writes_the_state_before_it_launches_the_step(world, clock):
    """O58: when the launcher is asked, the state already says starting, launch + 1 and the new step's name."""
    stopped_provider(world)
    status, _out, err = verb(world, "start")
    assert status == 0, err
    [call] = world.launcher.services
    seen = call["state"]["services"]["prov-a"]
    assert seen["state"] == "starting"
    assert seen["launch"] == 2
    assert seen["step"] == {"name": f"{PREFIX}-prov-a-run-2", "id": None}
    assert seen["log"] == f"/logs/{PREFIX}-prov-a-run-2.log"


def test_o58_a_start_launches_restart_sh_on_the_same_node_and_records_the_listed_step(world, clock):
    """O58: restart.sh <kind> --workdir W, detached, under the new step's name and a log beside the old one; once it is
    listed its id and `running` are recorded."""
    stopped_provider(world)
    verb(world, "start")
    [call] = world.launcher.services
    assert call["node"] == NODE
    assert call["argv"] == ["bash", str(BUNDLE / "restart.sh"), "op", "--workdir", WORKDIR]
    assert call["kind"] == "op"
    assert call["name"] == f"{PREFIX}-prov-a-run-2"
    assert call["log"] == f"/logs/{PREFIX}-prov-a-run-2.log"
    record = world.state.service()
    assert record["state"] == "running"
    assert record["launch"] == 2
    assert record["step"] == {"name": f"{PREFIX}-prov-a-run-2", "id": "88.1"}
    assert record["log"] == call["log"]


def test_o58_a_second_start_is_a_no_op(world, clock):
    """O58: idempotent: after one start the service is listed, and a start while it is alive launches nothing more."""
    stopped_provider(world)
    assert verb(world, "start")[0] == 0
    world.launcher.on_control = lambda argv: alive()
    assert verb(world, "start")[0] == 0
    assert len(world.launcher.services) == 1


def test_o58_a_step_that_is_not_listed_in_time_fails_the_start_and_leaves_the_state_starting(world, clock):
    """O58: exit 1 after the bound, with the attempt recorded; a stop brings it back."""
    stopped_provider(world)
    world.launcher.on_service = lambda name: None
    status, out, err = verb(world, "start")
    assert status == 1
    assert out == ""
    assert world.state.service()["state"] == "starting"
    assert world.state.service()["step"]["id"] is None
    assert clock.slept >= control.START_LISTED_WAIT_S
    assert NODE not in err


def test_o58_a_step_that_is_listed_after_a_few_polls_is_waited_for(world, clock):
    """O58: the step appears on the fourth poll: the start succeeds, short of the bound."""
    stopped_provider(world)
    polls = []

    def listed(name):
        polls.append(name)
        return "88.1" if len(polls) > 3 else None

    world.launcher.listed = listed
    world.launcher.on_service = lambda name: None
    assert verb(world, "start")[0] == 0
    assert 0 < clock.slept < control.START_LISTED_WAIT_S


@pytest.mark.parametrize("state", ["running", "starting", "stopping", "failed", None])
def test_o58_a_service_that_is_not_stopped_is_not_started(state, world, clock):
    """O58: a step that is not listed and a state other than stopped: exit 1, nothing launched, nothing changed."""
    world.state.doc["services"]["prov-a"]["state"] = state
    world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1")
    status, _out, err = verb(world, "start")
    assert status == 1
    assert world.launcher.services == []
    assert world.state.history == []
    assert "stop" in err


def test_o58_a_listed_step_whose_process_is_gone_is_not_started_over(world, clock):
    """O58: the step is still in the list but nothing runs in it: a stop must clear it first."""
    world.launcher.on_control = lambda argv: DEAD
    status, _, err = verb(world, "start")
    assert status == 1
    assert world.launcher.services == []
    assert "stop it first" in err


def test_o58_a_start_whose_status_cannot_be_read_launches_nothing(world, clock):
    """O58: unknown whether it runs: no second service."""
    world.launcher.on_control = lambda argv: TimeoutError()
    assert verb(world, "start")[0] == 1
    assert world.launcher.services == []


def test_o58_a_start_without_a_recorded_log_directory_is_refused(world, clock):
    """O58: the new step's log goes beside the old one; with none recorded there is nowhere to put it."""
    stopped_provider(world)
    del world.state.doc["services"]["prov-a"]["log"]
    assert verb(world, "start")[0] == 2
    assert world.launcher.services == []


def test_o58_a_launcher_that_fails_to_launch_leaves_the_attempt_in_the_state(world, clock):
    """O58: exit 1, naming only the error type, and `starting` stays so a stop can clear it."""
    stopped_provider(world)

    def broken(name):
        raise RuntimeError("srun said something about node-x1")

    world.launcher.on_service = broken
    status, _, err = verb(world, "start")
    assert status == 1
    assert "RuntimeError" in err
    assert "node-x1" not in err
    assert world.state.service()["state"] == "starting"


# --- exec ---------------------------------------------------------------------------------------------------------


def test_o58_exec_runs_exec_sh_in_a_control_step_with_the_argv_after_the_separator(world, clock):
    """O58: bash exec.sh <kind> --workdir W -- <argv>, on the service's node, bounded generously."""
    verb(world, "exec", argv=["/usr/sbin/op_worker", "ping", "a b"])
    [call] = world.launcher.controls
    assert call["node"] == NODE
    assert call["argv"] == [
        "bash",
        str(BUNDLE / "exec.sh"),
        "op",
        "--workdir",
        WORKDIR,
        "--",
        "/usr/sbin/op_worker",
        "ping",
        "a b",
    ]
    assert call["timeout_s"] == 300
    assert call["name"].startswith(f"{PREFIX}-prov-a-exec-")


@pytest.mark.parametrize("rc", [0, 1, 3, 127])
def test_o58_exec_passes_the_output_through_unchanged_and_returns_the_commands_status(rc, world, clock):
    """O58: stdout and stderr exactly as the step gave them (no newline added), the exit status the command's."""
    world.launcher.on_control = lambda argv: Done(rc, "out without newline", "err\nsecond line\n")
    status, out, err = verb(world, "exec", argv=["cmd"])
    assert status == rc
    assert out == "out without newline"
    assert err == "err\nsecond line\n"


def test_o58_exec_decodes_bytes_the_launcher_gives(world, clock):
    """O58: a launcher that returns bytes is passed through as text."""
    world.launcher.on_control = lambda argv: Done(0, b"caf\xc3\xa9", b"")
    assert verb(world, "exec", argv=["cmd"])[1] == "café"


def test_o58_exec_that_times_out_is_124(world, clock):
    """O58: the launcher's TimeoutError is 124, with nothing on stdout."""
    world.launcher.on_control = lambda argv: TimeoutError()
    status, out, err = verb(world, "exec", argv=["sleep", "999"])
    assert status == 124
    assert out == ""
    assert NODE not in err


def test_o58_exec_whose_step_could_not_run_is_125_naming_only_the_error_type(world, clock):
    """O58: not the command's status (1 could be); 125, and the message holds no node."""
    world.launcher.on_control = lambda argv: RuntimeError("could not reach node-x1")
    status, _out, err = verb(world, "exec", argv=["cmd"])
    assert status == 125
    assert "RuntimeError" in err
    assert "node-x1" not in err


@pytest.mark.parametrize("argv", [[], [1, 2]])
def test_o58_exec_without_a_command_is_refused(argv, world, clock):
    """O58: nothing (or no string) after `--` is exit 2, and no step runs."""
    status, out, _ = verb(world, "exec", argv=argv)
    assert status == 2
    assert out == ""
    assert world.launcher.controls == []
