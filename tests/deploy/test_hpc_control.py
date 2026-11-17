"""The four service verbs of an HPC federation (deploy/hpc-federation/hpcfed_control.py), against fakes (O58).

The module is logic over two injected objects, the step launcher and the state store that `hpcfed.py` supplies. Here
both are fakes, so nothing reaches a cluster, and the clock is a fake too, so the bounds cost no time; the verbs over
M1's own launcher and state store are test_hpc_control_wiring.py's.
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
with pytest.MonkeyPatch.context() as _patch:  # it imports the driver's own modules (hpcstate, hpclaunch, hpcslurm,
    _patch.syspath_prepend(str(HPC.parent / "compose-federation"))  # hpcstorage, whose hpcremove needs fedlayout), on
    _patch.syspath_prepend(str(HPC))  # the paths hpcfed.py, the one that imports it, puts first
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
    site = {"node_tmp": "/nodetmp", "logs": "/logs", "node_python": "python3.11"}
    return {"allocation": {"name": PREFIX, "ends": ENDS}, "site": site, "services": {"prov-a": provider, "zone": zone}}


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeState:
    """A state file: read() copies, update(fn) is atomic (fn runs on a copy; an exception from it writes nothing)."""

    def __init__(self, doc, root=None):
        self.doc = doc
        self.root = root  # where a stop or a start holds the verbs' lock, as M1's StateStore has it
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
def world(tmp_path):
    """A federation whose provider runs: its state, its launcher, and the output the verbs write."""
    state = FakeState(document(), root=tmp_path)
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
    assert call["timeout_s"] == 180 + 60  # the step's launch wait, then its own bound
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


def waited(figure, **extra):
    """What status.sh printed for a ready service whose start waited `figure` seconds for the provider's domain."""
    record = {"alive": True, "pid": 4242, "start": "777", "ready": True, "domain_wait_s": figure, **extra}
    return Done(0, json.dumps(record) + "\n")


@pytest.mark.parametrize("figure", [0.0, 2.013, 17], ids=["no wait", "a wait", "an integer"])
def test_o58_status_passes_on_the_seconds_the_start_waited_for_the_domain(figure, world):
    """O58: a ready service's status carries `domain_wait_s` as status.sh printed it, for the injector's snapshot."""
    world.launcher.on_control = lambda argv: waited(figure)
    status, out, err = verb(world, "status")
    assert status == 0, err
    assert json.loads(out)["domain_wait_s"] == figure


def test_o58_status_without_the_figure_prints_none(world):
    """O58: status.sh printed none (a service of an earlier bundle, or not ready): the status has no such key."""
    world.launcher.on_control = lambda argv: alive(ready=True)
    assert "domain_wait_s" not in json.loads(verb(world, "status")[1])
    world.launcher.on_control = lambda argv: waited(1.0, ready=False)
    assert "domain_wait_s" not in json.loads(verb(world, "status")[1])


@pytest.mark.parametrize("figure", [-1, "2", True, None, float("nan")], ids=repr)
def test_o58_a_figure_that_is_not_a_duration_is_not_passed_on(figure, world):
    """O58: anything but a finite number of at least 0 is dropped, and the status is still a status."""
    world.launcher.on_control = lambda argv: waited(figure)
    status, out, _ = verb(world, "status")
    assert status == 0
    record = json.loads(out)
    assert record["ready"] is True
    assert "domain_wait_s" not in record


def test_o58_a_service_not_running_reports_no_figure_whatever_status_sh_printed(world):
    """O58: the figure belongs to a running, ready launch: a step that is not listed has none."""
    world.launcher.on_control = lambda argv: waited(3.0)
    world.launcher.steps.clear()
    assert "domain_wait_s" not in json.loads(verb(world, "status")[1])


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
    assert call["timeout_s"] == 300 + 180 + 60  # stop_seconds, the step's launch wait, teardown's margin
    assert call["name"].startswith(f"{PREFIX}-prov-a-stop-")


def test_o58_a_stop_prints_its_evidence_and_records_the_same_object_under_the_service(world, clock):
    """O58: one JSON line: stopped, how it ended and the service's own seconds (teardown's line), the seconds from the
    request to the step gone, and both times; the same object is appended to `stops` in the state."""

    def on_control(argv):
        clock.now += 4.25  # teardown takes a while
        world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1")
        return Done(0, 'noise\nhpcop-stop {"ended": "kill", "seconds": 3.9}\n')

    world.launcher.on_control = on_control
    status, out, _ = verb(world, "stop")
    assert status == 0
    assert out.count("\n") == 1
    evidence = json.loads(out)
    assert list(evidence) == [
        "service",
        "stopped",
        "ended",
        "seconds",
        "service_seconds",
        "requested_utc",
        "confirmed_utc",
    ]
    assert evidence["service"] == "prov-a"
    assert evidence["stopped"] is True
    assert evidence["ended"] == "kill"
    assert evidence["seconds"] == pytest.approx(4.25)
    assert evidence["service_seconds"] == 3.9  # teardown's own measure: SIGTERM to the process gone, on the node
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
    assert json.loads(out)["service_seconds"] is None
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
        assert evidence["service_seconds"] is None
    assert world.launcher.controls == []
    assert world.state.history == []
    assert "stops" not in world.state.service()


def test_o58_a_stopped_service_whose_step_is_still_listed_is_stopped_for_real(world, clock):
    """O58: `stopped` in the state is not enough while the step is still in the list: the stop runs."""
    world.state.doc["services"]["prov-a"]["state"] = "stopped"
    stopping_launcher(world, stdout='hpcop-stop {"ended": "none", "seconds": 0}\n')
    assert verb(world, "stop")[0] == 0
    assert len(world.launcher.controls) == 1


@pytest.mark.parametrize(
    "answer",
    [Done(1, ""), Done(2, TEARDOWN), Done(None, TEARDOWN), TimeoutError(), RuntimeError("boom")],
    ids=["failed", "failed with a line", "end not known", "timeout", "launcher error"],
)
def test_o58_a_stop_whose_teardown_fails_leaves_the_state_stopping(answer, world, clock):
    """O58: teardown that did not exit 0 (a step whose end is not known among them), timed out or could not run: exit
    1, `stopping` stays, the evidence says stopped false with the seconds the attempt took, and no step is cancelled
    (the keep marks may not have been written)."""

    def on_control(argv):
        clock.now += 7.0
        return answer

    world.launcher.on_control = on_control
    status, out, err = verb(world, "stop")
    assert status == 1
    evidence = json.loads(out)
    assert evidence["stopped"] is False
    assert evidence["seconds"] == pytest.approx(7.0)  # every stop's duration is kept, confirmed or not
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
    "ends", [ENDS, "2026-11-16T14:00:00+00:00", "2026-11-16T16:00:00+02:00", str(ENDS_EPOCH), ENDS_EPOCH]
)
def test_o58_the_allocations_end_is_read_in_each_zoned_form(ends, world, clock):
    """O58: a Z, an offset, epoch seconds as text or a number: all the same epoch."""
    world.state.doc["allocation"]["ends"] = ends
    stopping_launcher(world)
    verb(world, "stop")
    argv = world.launcher.controls[0]["argv"]
    assert argv[argv.index("--keep-until") + 1] == str(ENDS_EPOCH)


@pytest.fixture
def two_hours_east(monkeypatch):
    """This host's local time two hours ahead of UTC, as on a cluster in central Europe in summer."""
    import time

    monkeypatch.setenv("TZ", "XXX-2")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("ends", ["2026-11-16T16:00:00", "2026-11-16 16:00:00"])
def test_o58_an_end_with_no_zone_is_the_login_nodes_local_time_as_scontrol_printed_it(
    ends, world, clock, two_hours_east
):
    """O58: `up` records Slurm's EndTime as scontrol printed it, in the login node's local time and with no zone: read
    as UTC on a cluster east of it, the keep-until would fall before the allocation's end, and the sweep could remove a
    stopped service's work directory while the job still runs."""
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
    assert clock.slept >= control.DEFAULT_LAUNCH_WAIT_S + control.START_LISTED_WAIT_S  # launch wait, then its own wait
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


@pytest.mark.parametrize("state", ["failed", "planned", "removed", None])
def test_o58_a_service_in_a_state_no_start_follows_is_not_started(state, world, clock):
    """O58: a step that is not listed and a state a start never follows (a failed or removed service, one never
    placed): exit 1, nothing run, launched or changed."""
    world.state.doc["services"]["prov-a"]["state"] = state
    world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1")
    status, _out, err = verb(world, "start")
    assert status == 1
    assert world.launcher.controls == []
    assert world.launcher.services == []
    assert world.state.history == []
    assert "stop" in err


@pytest.mark.parametrize("state", ["stopping", "starting", "running"])
def test_o58_a_service_whose_stop_or_start_was_never_recorded_is_started_once_nothing_runs(state, world, clock):
    """O58: a stop whose verdict never reached the state (its verb killed at the relay's bound, say), a start whose step
    ended or never came, a service that ended by itself: once its step is not listed and status.sh finds its process
    not alive, it is started, so the restore is never refused for a provider that is down."""
    world.state.doc["services"]["prov-a"]["state"] = state
    world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1")
    world.launcher.on_control = lambda argv: DEAD
    status, _out, err = verb(world, "start")
    assert status == 0, err
    [reading] = world.launcher.controls
    assert reading["argv"][1].endswith("status.sh")
    [call] = world.launcher.services
    assert call["state"]["services"]["prov-a"]["state"] == "starting"
    assert world.state.service()["launch"] == 2


@pytest.mark.parametrize("state", ["stopping", "starting", "running"])
def test_o58_a_service_whose_process_runs_without_a_listed_step_is_not_started_again(state, world, clock):
    """O58: no recorded stop and the process alive: a second service is never launched beside it."""
    world.state.doc["services"]["prov-a"]["state"] = state
    world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1")
    world.launcher.on_control = lambda argv: alive()
    status, _out, err = verb(world, "start")
    assert status == 1
    assert world.launcher.services == []
    assert "stop it first" in err


@pytest.mark.parametrize("state", ["stopping", "starting"])
def test_o58_a_start_with_no_recorded_stop_whose_status_cannot_be_read_launches_nothing(state, world, clock):
    """O58: unknown whether the process runs: nothing launched, exit 1."""
    world.state.doc["services"]["prov-a"]["state"] = state
    world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1")
    world.launcher.on_control = lambda argv: TimeoutError()
    assert verb(world, "start")[0] == 1
    assert world.launcher.services == []


def in_flight(world, monkeypatch, state="starting"):
    """A start whose step was never listed and whose srun client still runs on the login node."""
    record = world.state.doc["services"]["prov-a"]
    record.update(state=state, launch=2, step={"name": f"{PREFIX}-prov-a-run-2", "id": None})
    world.state.doc["clients"] = {f"{PREFIX}-prov-a-run-2": {"pid": 31337, "start": 99}}
    world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1")
    monkeypatch.setattr(control, "_CLIENT_ALIVE", lambda client: client == {"pid": 31337, "start": 99})


@pytest.mark.parametrize("state", ["starting", "stopped", "stopping"])
def test_o58_a_start_while_an_earlier_starts_step_may_still_be_made_launches_nothing(state, world, clock, monkeypatch):
    """O58: a pending step is not in the list, so a start whose step was never listed and whose srun client still runs
    may yet start the service: a second start would put two services on one work directory. Exit 1, nothing run."""
    in_flight(world, monkeypatch, state)
    status, _out, err = verb(world, "start")
    assert status == 1
    assert world.launcher.services == []
    assert world.launcher.controls == []
    assert "in flight" in err


def test_o58_a_start_whose_step_was_never_listed_and_whose_client_ended_is_started_again(world, clock, monkeypatch):
    """O58: the srun client gone, no step can be made from it any more: the service may be started."""
    in_flight(world, monkeypatch)
    monkeypatch.setattr(control, "_CLIENT_ALIVE", lambda client: False)
    world.launcher.on_control = lambda argv: DEAD
    assert verb(world, "start")[0] == 0
    assert [call["name"] for call in world.launcher.services] == [f"{PREFIX}-prov-a-run-3"]


def test_o58_a_stop_is_not_confirmed_while_a_start_that_was_never_listed_may_still_make_its_step(
    world, clock, monkeypatch
):
    """O58: teardown found nothing to stop and no step is listed, but the srun client of a start still runs: its step
    may be made after the stop recorded `stopped`. Not confirmed: exit 1, `stopping` stays."""
    in_flight(world, monkeypatch)
    world.launcher.on_control = lambda argv: Done(0, 'hpcop-stop {"ended": "none", "seconds": 0}\n')
    status, out, err = verb(world, "stop")
    assert status == 1
    assert json.loads(out)["stopped"] is False
    assert world.state.service()["state"] == "stopping"
    assert "srun client" in err


def test_o58_a_start_whose_step_was_listed_after_its_wait_is_recorded_running_and_launches_nothing(world, clock):
    """O58: the start before this one gave up waiting, then its step came: listed and alive is running, its id and
    state are recorded, and nothing is launched."""
    record = world.state.doc["services"]["prov-a"]
    record.update(state="starting", launch=2, step={"name": f"{PREFIX}-prov-a-run-2", "id": None})
    world.launcher.steps = {f"{PREFIX}-prov-a-run-2": "77.9"}
    world.launcher.on_control = lambda argv: alive()
    assert verb(world, "start")[0] == 0
    assert world.launcher.services == []
    assert world.state.service()["state"] == "running"
    assert world.state.service()["step"] == {"name": f"{PREFIX}-prov-a-run-2", "id": "77.9"}


def test_o58_a_stops_record_never_undoes_a_start_made_meanwhile(world, clock):
    """O58: a stop confirmed after a start claimed a new launch (a stop's verb that outlived its caller, say) appends
    its evidence but leaves the new launch's state alone."""

    def on_control(argv):
        world.launcher.steps.pop(f"{PREFIX}-prov-a-run-1", None)
        world.state.doc["services"]["prov-a"].update(launch=2, state="starting")  # a start, meanwhile
        return Done(0, TEARDOWN)

    world.launcher.on_control = on_control
    status, out, _ = verb(world, "stop")
    assert status == 0
    assert json.loads(out)["stopped"] is True
    assert world.state.service()["state"] == "starting"
    assert len(world.state.service()["stops"]) == 1


def test_o58_a_stop_whose_step_list_cannot_be_read_is_unconfirmed_and_still_records_its_evidence(world, clock):
    """O58: teardown exited 0 but squeue failed: not confirmed, exit 1, and the evidence is printed and recorded."""

    def listed(name):
        raise RuntimeError("squeue -s failed on a node")

    world.launcher.listed = listed
    world.launcher.on_control = lambda argv: Done(0, TEARDOWN)
    world.state.doc["services"]["prov-a"]["state"] = "running"
    status, out, err = verb(world, "stop")
    assert status == 1
    evidence = json.loads(out)
    assert evidence["stopped"] is False
    assert evidence["ended"] == "exit"
    assert world.state.service()["stops"] == [evidence]
    assert "RuntimeError" in err
    assert "node" not in err.replace("hpcfed:", "")


def test_o58_a_stop_whose_record_cannot_be_written_prints_its_evidence_and_fails(world, clock):
    """O58: the state store failing after the stop: the evidence is still printed, and the exit status is 1."""
    stopping_launcher(world)
    update = world.state.update

    def failing(fn):
        if len(world.state.history) >= 1:  # `stopping` is written; the record after the stop is not
            raise OSError("the root's filesystem is full")
        return update(fn)

    world.state.update = failing
    status, out, err = verb(world, "stop")
    assert status == 1
    assert json.loads(out)["stopped"] is True
    assert "OSError" in err


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


def test_o58_a_start_without_the_sites_log_directory_is_refused(world, clock):
    """O58: the new step's log goes in the site's log directory, as up's do; with none recorded there is nowhere to
    put it, and nothing is changed."""
    stopped_provider(world)
    del world.state.doc["site"]["logs"]
    assert verb(world, "start")[0] == 2
    assert world.launcher.services == []
    assert world.state.history == []


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
    assert call["timeout_s"] == 180 + 300  # the step's launch wait, then its own bound
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


def test_o58_exec_whose_steps_end_is_not_known_is_124_never_a_success(world, clock):
    """O58: M1's launcher gives returncode None when the srun client was ended after its step vanished and the
    accounting holds no status: the command's end is unknown, like a timeout, never 0."""
    world.launcher.on_control = lambda argv: Done(None, "pong\n", "")
    status, out, err = verb(world, "exec", argv=["op_worker", "ping"])
    assert status == 124
    assert out == "pong\n"
    assert "not known" in err


def test_o58_exec_whose_client_a_signal_ended_is_128_plus_the_signal(world, clock):
    """O58: a negative returncode (the client killed by signal n) is 128 + n, as a shell reads it, never negative."""
    world.launcher.on_control = lambda argv: Done(-9, "", "")
    assert verb(world, "exec", argv=["cmd"])[0] == 137


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


# --- the read-only proof of a restarted owner of shared imported trees (O59) -----------------------------------------


class Mounts:
    """The shared import's check as `hpcfed.py` hands it over (`hpcimport`): prov-a owns one shared tree; a report
    reading `proven` leaves nothing unproven."""

    def __init__(self, owned=("cohort",)):
        self.owned = list(owned)
        self.read = []

    def spaces(self, service):
        return list(self.owned) if service == "prov-a" else []

    def argv(self, workdir, python):
        return [python, "-I", "-B", "/bundle/importstorage.py", "mounts", "--workdir", workdir]

    def problems(self, report, spaces):
        self.read.append((report, list(spaces)))
        return [] if report.strip() == "proven" else ["cohort is held by no process of the service"]


def owner_answers(proof):
    """status.sh says alive and ready; the mount check answers `proof` (a Done, or an exception)."""

    def on_control(argv):
        if argv[1].endswith("status.sh"):
            return alive(ready=True)
        return proof

    return on_control


def test_o59_a_start_of_an_owner_clears_its_proof_in_the_change_that_claims_the_launch(world, clock):
    """O59: before the restarted owner's step is launched, the state no longer says its trees are proven read-only."""
    stopped_provider(world)
    world.state.doc["services"]["prov-a"]["mounts_proven"] = True
    assert verb(world, "start", mounts=Mounts())[0] == 0
    [call] = world.launcher.services
    assert call["state"]["services"]["prov-a"]["mounts_proven"] is False
    assert call["state"]["services"]["prov-a"]["state"] == "starting"
    assert world.state.service()["mounts_proven"] is False


def test_o59_a_start_of_a_service_that_owns_no_shared_tree_writes_no_proof(world, clock):
    """O59: a mirror, or a core without the shared import: the record gains no key."""
    stopped_provider(world)
    assert verb(world, "start", mounts=Mounts(owned=()))[0] == 0
    assert "mounts_proven" not in world.state.service()


def test_o59_a_restarted_owner_is_ready_only_once_its_trees_are_proven_read_only_for_this_launch(world, clock):
    """O59: the first status that finds the restarted owner otherwise ready runs the check on its node, reads its
    report against the trees the owner holds, records the proof, and only then says ready."""
    world.state.doc["services"]["prov-a"]["mounts_proven"] = False
    mounts = Mounts()
    world.launcher.on_control = owner_answers(Done(0, "proven\n"))
    status, out, err = verb(world, "status", mounts=mounts)
    assert status == 0, err
    assert json.loads(out)["ready"] is True
    _status_step, check = world.launcher.controls
    assert check["node"] == NODE
    assert check["argv"] == ["python3.11", "-I", "-B", "/bundle/importstorage.py", "mounts", "--workdir", WORKDIR]
    assert check["timeout_s"] == 180 + control.MOUNT_CHECK_TIMEOUT_S
    assert check["name"].startswith(f"{PREFIX}-prov-a-mounts-")
    assert mounts.read == [("proven\n", ["cohort"])]
    assert world.state.service()["mounts_proven"] is True
    world.launcher.controls.clear()
    assert json.loads(verb(world, "status", mounts=mounts)[1])["ready"] is True
    assert [call["argv"][1] for call in world.launcher.controls] == [str(BUNDLE / "status.sh")]  # proven: no check


@pytest.mark.parametrize(
    "proof",
    [
        Done(1, "", "FAIL: shared import cohort is writable in pid 77\nnoise /nodetmp/hpcop-op.Ab12Cd\n"),
        Done(0, "[]\n"),
        Done(None, "proven\n"),
        TimeoutError(),
        RuntimeError("srun on node-x1"),
    ],
    ids=["check failed", "report unproven", "end not known", "timeout", "launcher error"],
)
def test_o59_a_restarted_owner_whose_check_does_not_pass_is_not_ready_and_nothing_is_recorded(proof, world, clock):
    """O59: a check that fails, reports a tree unproven, ends unknown, times out or cannot run leaves `ready` false,
    the proof unrecorded, and a message with the check's own FAIL lines and no path or node."""
    world.state.doc["services"]["prov-a"]["mounts_proven"] = False
    world.launcher.on_control = owner_answers(proof)
    status, out, err = verb(world, "status", mounts=Mounts())
    assert status == 0
    record = json.loads(out)
    assert (record["running"], record["ready"]) == (True, False)
    assert world.state.service()["mounts_proven"] is False
    assert NODE not in err
    assert "/nodetmp" not in err
    if isinstance(proof, Done) and proof.returncode == 1:
        assert "FAIL: shared import cohort is writable in pid 77" in err


def test_o59_an_owner_that_is_not_ready_yet_is_not_checked(world, clock):
    """O59: the check runs once the service's own checks passed for this launch, not before."""
    world.state.doc["services"]["prov-a"]["mounts_proven"] = False
    world.launcher.on_control = lambda argv: alive(ready=False)
    assert json.loads(verb(world, "status", mounts=Mounts())[1])["ready"] is False
    assert len(world.launcher.controls) == 1


def test_o59_a_record_that_needs_a_proof_no_core_check_can_give_is_never_ready(world, clock):
    """O59: a state whose `up` recorded a proof, read by a core without the shared import's check: never ready."""
    world.state.doc["services"]["prov-a"]["mounts_proven"] = False
    world.launcher.on_control = lambda argv: alive(ready=True)
    status, out, err = verb(world, "status")
    assert (status, json.loads(out)["ready"]) == (0, False)
    assert "cannot check" in err
    assert len(world.launcher.controls) == 1


def test_o59_a_proof_is_recorded_only_for_the_launch_the_status_read(world, clock):
    """O59: a start that claimed a new launch while the check ran: its proof is not this check's to give."""
    world.state.doc["services"]["prov-a"]["mounts_proven"] = False

    def on_control(argv):
        if argv[1].endswith("status.sh"):
            return alive(ready=True)
        world.state.doc["services"]["prov-a"].update(launch=2, state="starting")  # a start, meanwhile
        return Done(0, "proven\n")

    world.launcher.on_control = on_control
    verb(world, "status", mounts=Mounts())
    assert world.state.service()["mounts_proven"] is False


# --- one stop or start at a time ------------------------------------------------------------------------------------


def test_o58_a_start_while_another_start_is_between_its_claim_and_its_launch_runs_nothing(world, clock):
    """O58: a second `hpcfed.py start` while the first holds the verbs' lock (here, from inside the first's launch,
    before its client is recorded) is exit 1 and launches nothing: one service step on the work directory."""
    stopped_provider(world)
    nested = []

    def second_start(name):
        nested.append(verb(world, "start"))
        world.launcher.steps[name] = "88.1"

    world.launcher.on_service = second_start
    assert verb(world, "start")[0] == 0
    [(status, _out, err)] = nested
    assert status == 1
    assert "another stop or start" in err
    assert [call["name"] for call in world.launcher.services] == [f"{PREFIX}-prov-a-run-2"]


def test_o58_a_stop_while_a_start_runs_is_refused_and_runs_nothing(world, clock):
    """O58: a stop is a mutation too: refused while a start holds the lock, so it never tears the new launch down
    between its claim and its record."""
    stopped_provider(world)
    nested = []
    world.launcher.on_service = lambda name: (
        nested.append(verb(world, "stop")),
        world.launcher.steps.update({name: "9"}),
    )
    assert verb(world, "start")[0] == 0
    assert nested[0][0] == 1
    assert [call["argv"][1] for call in world.launcher.controls] == []


def test_o58_status_and_exec_take_no_lock(world, clock):
    """O58: reading and the heal's exec go on while a stop or start holds the lock."""
    world.launcher.on_control = lambda argv: alive() if argv[1].endswith("status.sh") else Done(0, "pong\n")
    with control._exclusive(world.state):
        assert verb(world, "status")[0] == 0
        assert verb(world, "exec", argv=["op_worker", "ping"])[0] == 0


def test_o58_a_state_store_with_no_root_refuses_a_stop_or_a_start(world, clock):
    """O58: without the root to hold the lock in, no mutation runs: exit 2, nothing changed."""
    world.state.root = None
    for name in ("stop", "start"):
        assert verb(world, name)[0] == 2
    assert world.launcher.controls == []
    assert world.state.history == []


def test_o58_a_service_whose_longest_step_name_the_launcher_would_refuse_is_refused_at_once(world, clock):
    """O58: every step name this module makes must fit the launcher's (100 characters): a name that would not is exit 2
    before anything runs, never a step refused half way through a stop."""
    long_name = "p" * 60  # with the allocation, its verb and a 13-digit stamp: 101 characters
    world.state.doc["services"][long_name] = world.state.doc["services"].pop("prov-a")
    status, _out, err = verb(world, "stop", service=long_name)
    assert status == 2
    assert "no name a step can carry" in err
    assert world.launcher.controls == []


# --- a control step's bound is its launch wait, then its work (O58, 17 Nov) -----------------------------------------


def late_steps(world, launch_s, work_s):
    """Slurm starts every control step `launch_s` late and the step then runs `work_s`: its client is waited for from
    its start, so a bound below their sum times it out (TimeoutError, as the launcher's does). Returns the bounds
    asked."""
    bounds, inner = [], world.launcher.control

    def control_step(node, argv, *, name, timeout_s):
        bounds.append(timeout_s)
        if launch_s + work_s > timeout_s:
            raise TimeoutError("the step did not end within its bound")
        return inner(node, argv, name=name, timeout_s=timeout_s)

    world.launcher.control = control_step
    return bounds


def test_o58_a_stop_whose_step_starts_103_s_late_and_runs_33_s_is_confirmed_inside_its_bound(world, clock):
    """O58: relay verb steps started 47 to 103 s after they were issued, and a service ended 33.1 s after its SIGTERM
    (17 Nov). With the launch wait counted the stop's control step is bounded at 50 + 180 + 60 = 290 s and the stop is
    confirmed; the figures are the layout's defaults."""
    stopping_launcher(world)
    bounds = late_steps(world, 103, 33)
    status, _out, err = verb(world, "stop", stop_seconds=50)
    assert status == 0, err
    assert bounds == [50 + 180 + 60]
    assert world.state.service()["state"] == "stopped"


def test_o58_a_stop_with_no_launch_wait_counted_times_out_on_the_same_103_s_late_step_and_is_never_taken_as_done(
    world, clock
):
    """O58: what the launch wait prevents: bounded at 50 + 60 = 110 s alone, the same step (103 s late, 33 s of work)
    times out, and the stop is unconfirmed, not done: the state stays `stopping` and the verb exits 1."""
    stopping_launcher(world)
    bounds = late_steps(world, 103, 33)
    status, out, err = verb(world, "stop", stop_seconds=50, step_launch_wait_s=0)
    assert status == 1
    assert bounds == [50 + 60]
    assert world.state.service()["state"] == "stopping"
    assert json.loads(out)["stopped"] is False
    assert "control step did not finish in time" in err


@pytest.mark.parametrize(
    ("name", "answer", "work_s"),
    [("status", alive(), control.STATUS_TIMEOUT_S), ("exec", Done(0, "pong\n"), control.EXEC_TIMEOUT_S)],
)
def test_o58_a_status_or_exec_step_is_bounded_by_its_launch_wait_and_then_its_own_bound(
    world, clock, name, answer, work_s
):
    """O58: the status step's bound is the layout's launch wait and 60 s, the exec step's the launch wait and 300 s,
    each held to a step that starts 103 s late and then runs 33 s."""
    world.launcher.on_control = lambda argv: answer
    bounds = late_steps(world, 103, 33)
    argv = ["/usr/sbin/op_worker", "ping"] if name == "exec" else []
    status, _out, err = verb(world, name, argv=argv, step_launch_wait_s=103)
    assert status == 0, err
    assert bounds == [103 + work_s]
    assert bounds[0] > 103 + 33


def test_o58_a_start_waits_for_its_service_step_to_be_listed_for_the_launch_wait_and_then_60_s(world, clock):
    """O58: a start that finds its new service step not listed gives up after the launch wait and then 60 s, never
    before: a step Slurm starts 103 s late is listed within that, where a start that gave up at 60 s lost it."""
    stopped_provider(world)
    world.launcher.on_service = lambda name: None
    status, _out, _err = verb(world, "start", step_launch_wait_s=103)
    assert status == 1
    assert 103 + control.START_LISTED_WAIT_S <= clock.slept < 103 + control.START_LISTED_WAIT_S + 2 * control.POLL_S


@pytest.mark.parametrize("bad", [-1, 2.5, "180", True])
def test_o58_step_launch_wait_must_be_a_whole_number_of_seconds_of_at_least_zero(world, bad):
    """O58: a launch wait that is no whole number of seconds, or is negative, is refused with nothing run."""
    assert verb(world, "stop", step_launch_wait_s=bad)[0] == 2
    assert world.launcher.controls == []


def test_o58_the_launch_wait_and_the_verbs_work_are_one_figure_in_the_core_the_layout_and_the_verbs():
    """O58: the core's default launch wait (`fault_hpc.HPC_STEP_LAUNCH_WAIT_S`), the layout's (`hpcsettings`) and this
    module's agree, and the core's allowance for a relay verb's own work (`HPC_VERB_WORK_S`) holds the longest bound
    a verb waits on its own step for besides the launch wait (a status step, a start's wait to be listed) and 60 s."""
    from expcore_onedata.fault_hpc import HPC_STEP_LAUNCH_WAIT_S, HPC_VERB_WORK_S

    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(HPC.parent / "compose-federation"))
        patch.syspath_prepend(str(HPC))
        import hpcsettings

        layout_default = hpcsettings.DEFAULTS["step_launch_wait_s"]
    assert control.DEFAULT_LAUNCH_WAIT_S == layout_default == HPC_STEP_LAUNCH_WAIT_S == 180
    assert max(control.STATUS_TIMEOUT_S, control.START_LISTED_WAIT_S) + 60 <= HPC_VERB_WORK_S
