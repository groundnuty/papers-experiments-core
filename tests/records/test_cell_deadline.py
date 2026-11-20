"""O97: a call whose cell's deadline fired with a process that outlived its kill keeps what the code tool said of it, as
an optional field of the call (`tool_calls[i].cell_deadline`), the last one. A call whose cell was killed cleanly, or
whose tool says nothing, has none, never a null. The field is optional in version 3 and unknown to the versions before,
a record without it loads, writes back and replays to its own bytes, a replay serves it as recorded, and the nested
shape (steps, survivors, the verdict) is checked, bounded and plain."""

import copy

import pytest
from loop_support import FakeProvider
from record_support import record_run

from expcore.plugin import ToolOutput
from expcore.records.cell_deadline import SIZE_CAP, STEPS_CAP, SURVIVORS_CAP, TASKS_CAP, TEXT_CAP, problem
from expcore.records.record import RunRecord, decode
from expcore.records.schema import SCHEMAS, RecordError, fields_of, optional_in, validate
from expcore.replay.replay import replay_record

V2, V3 = "expcore-record/2", "expcore-record/3"
SCRIPT = [[("python", {"x": "read()"}), ("echo", {"x": "b"})], "done"]
HELD = {
    "outcome": "mount_restored",
    "utc": "2026-11-20T19:00:00.000+00:00",
    "grace_s": 5.0,
    "margin_s": 30.0,
    "restore": "restored",
    "steps": [
        {"step": "kill", "t_s": 0.11, "signalled": 3},
        {"step": "survived", "t_s": 5.02, "left": 1, "states": ["D"]},
        {"step": "abort", "t_s": 5.03, "ok": True, "method": "fusectl", "waiting_before": 1, "waiting_after": 0},
        {"step": "ended", "t_s": 5.31},
        {"step": "restored", "s": 11.8, "generation": 2},
    ],
    "survivors": [
        {
            "comm": "python3",
            "state": "D",
            "tasks": [{"state": "D", "wchan": "request_wait_answer", "syscall": {"nr": 0, "fd": 3}}],
            "waits_on": [{"fd": 3, "mount": "data", "fstype": "fuse", "space": "space-a", "path": "x/y", "pos": 0}],
            "cwd_mount": "work",
        }
    ],
    "survivors_cut": 0,
    "connection": {"fusectl": "writable"},
    "attribution": {
        "verdict": "agent",
        "why": "blocked_on_held_down_holder",
        "target": "providers[1]",
        "space_holder": "providers[1]",
    },
}


def recorded(held=HELD, **options) -> RunRecord:
    """A record of a run whose code-like call says its cell's deadline fired, and a call of another tool, none."""
    handlers = {
        "python": lambda args: ToolOutput("partial\n", True, "timeout", cell_deadline=held),
        "echo": lambda args: "echoed",
    }
    return record_run(SCRIPT, handlers, **options)


def invalid(**changes) -> dict:
    data = copy.deepcopy(HELD)
    for key, value in changes.items():
        if value is KeyError:
            data.pop(key)
        else:
            data[key] = value
    return data


def test_o97_a_calls_deadline_is_its_last_field_and_a_call_without_one_has_none():
    record = recorded()
    cell, other = record.tool_calls
    assert cell["cell_deadline"] == HELD
    assert list(cell)[-1] == "cell_deadline"  # after every field the earlier merge points wrote
    assert "cell_deadline" not in other  # absent, never a null
    text = record.dumps()
    assert validate(decode(text)) == V3
    assert RunRecord.from_json(decode(text)).dumps() == text


def test_o97_the_record_holds_a_copy_of_the_deadline_the_provider_gave():
    """O97: what the provider does to its own object after the call never changes the record."""
    held = copy.deepcopy(HELD)
    record = recorded(held)
    held["steps"].append({"step": "stopped"})
    held["outcome"] = "survived_kill"
    assert record.tool_calls[0]["cell_deadline"] == HELD


def test_o97_the_field_is_optional_in_version_three_and_in_no_earlier_version():
    (v3,) = (s for s in SCHEMAS if s.name == V3)
    assert "cell_deadline" in v3.added["tool_call"]
    assert "cell_deadline" in optional_in("tool_call", V3)
    assert set(v3.optional["cell_deadline"]) == set(v3.added["cell_deadline"])  # every key optional: the flat rule
    assert "cell_deadline" not in fields_of("tool_call", V2)


def test_o97_a_record_of_version_two_is_built_without_it_and_one_that_holds_it_is_refused():
    record = recorded(record_options={"schema_version": V2})
    assert all("cell_deadline" not in call for call in record.tool_calls)
    data = decode(record.dumps())
    assert validate(data) == V2
    data["tool_calls"][0]["cell_deadline"] = HELD
    with pytest.raises(RecordError, match=r"tool_calls\[0\]: unknown field\(s\) \['cell_deadline'\]"):
        validate(data)


def test_o97_a_record_with_a_deadline_replays_to_its_own_bytes_and_one_without_gains_none():
    """O97 with R03: the replay serves each call's deadline as recorded, so the replayed record holds it where the
    stored one does, and nowhere else."""
    held = recorded()
    assert replay_record(held).dumps() == held.dumps()
    without = record_run(SCRIPT, {"python": lambda args: "1\n", "echo": lambda args: "echoed"})
    assert all("cell_deadline" not in call for call in without.tool_calls)
    assert replay_record(without).dumps() == without.dumps()


def test_o97_a_fake_provider_hands_the_deadline_through_the_loop_unchanged():
    provider = FakeProvider({"python": lambda args: ToolOutput("", cell_deadline=HELD)})
    record = record_run([[("python", {"x": "pass"})], "done"], {}, providers=[provider])
    assert record.tool_calls[0]["cell_deadline"] == HELD


def test_o97_the_provider_refuses_a_deadline_that_is_not_an_object():
    with pytest.raises(ValueError, match="cell_deadline"):
        ToolOutput("x", cell_deadline=["not", "an", "object"])


def test_o97_the_deadline_of_an_escalated_cell_with_no_diagnosis_is_valid_as_the_four_keys_alone():
    """O97: a backend that reads nothing of its survivors still says how the deadline went."""
    bare = {k: HELD[k] for k in ("outcome", "utc", "grace_s", "margin_s", "steps")}
    assert problem(bare) is None
    assert validate(decode(recorded(bare).dumps())) == V3


@pytest.mark.parametrize(
    ("bad", "where"),
    [
        pytest.param(None, r"tool_calls\[0\]\.cell_deadline: expected dict", id="null"),
        pytest.param(invalid(extra=1), r"unknown field\(s\) \['extra'\]", id="an unknown key"),
        pytest.param(invalid(outcome=KeyError), r"missing field\(s\) \['outcome'\]", id="no outcome"),
        pytest.param(invalid(steps=KeyError), r"missing field\(s\) \['steps'\]", id="no steps"),
        pytest.param(invalid(outcome="Not A Name"), r"\.outcome: a plain name", id="an outcome that is no name"),
        pytest.param(invalid(utc=5), r"\.utc", id="a time that is no text"),
        pytest.param(invalid(grace_s=-1), r"\.grace_s: a number of seconds", id="a negative grace"),
        pytest.param(invalid(margin_s=True), r"\.margin_s: expected", id="a bool for seconds"),
        pytest.param(
            invalid(steps=[{"t_s": 1}]), r"steps\[0\]: an object with a plain `step` name", id="a nameless step"
        ),
        pytest.param(
            invalid(steps=[{"step": "a", "x": object}]), r"is not plain JSON|expected", id="a step with a type"
        ),
        pytest.param(
            invalid(steps=[{"step": "a", "x": float("inf")}]), r"not finite|expected", id="an infinite number"
        ),
        pytest.param(invalid(steps=[{"step": "a"}] * (STEPS_CAP + 1)), r"at most", id="too many steps"),
        pytest.param(
            invalid(steps=[{"step": "a", "why": "x" * (TEXT_CAP + 1)}]), r"more than", id="a text past the cap"
        ),
        pytest.param(invalid(survivors=[{"comm": "x"}] * (SURVIVORS_CAP + 1)), r"at most", id="too many survivors"),
        pytest.param(
            invalid(survivors=[{"comm": "x", "environ": "SECRET=1"}]),
            r"unknown field\(s\) \['environ'\]",
            id="a survivor key that could leak",
        ),
        pytest.param(invalid(survivors=[{"tasks": [{}] * (TASKS_CAP + 1)}]), r"at most", id="too many tasks"),
        pytest.param(invalid(survivors_cut=-1), r"survivors_cut: a count", id="a negative count"),
        pytest.param(invalid(connection={"mount": "/x"}), r"connection", id="a connection key that is no fusectl"),
        pytest.param(invalid(attribution={"verdict": "maybe", "why": "x"}), r"attribution\.verdict", id="no verdict"),
        pytest.param(invalid(attribution={"verdict": "agent"}), r"attribution\.why", id="no reason"),
        pytest.param(
            invalid(attribution={"verdict": "agent", "why": "x", "target": "Provider One"}),
            r"attribution\.target: a position label",
            id="a target that is a name",
        ),
        pytest.param(
            invalid(attribution={"verdict": "agent", "why": "x", "space_holder": "a-host"}),
            r"attribution\.space_holder: a position label",
            id="a holder that is a host",
        ),
    ],
)
def test_o97_a_present_deadline_is_checked_in_each_part_and_a_bad_one_is_refused(bad, where):
    data = decode(recorded().dumps())
    data["tool_calls"][0]["cell_deadline"] = bad
    with pytest.raises(RecordError, match=where):
        validate(data)


def test_o97_the_whole_deadline_is_bounded_in_size():
    """O97: every list and text is capped, and the whole is held to `SIZE_CAP` bytes as JSON, so a field built from
    /proc can never make a record that cannot be written or read back."""
    fat = invalid(steps=[{"step": "a", "text": ["x" * 200] * 4}] * STEPS_CAP)
    assert problem(fat) == f"cell_deadline: more than {SIZE_CAP} bytes as JSON"


# ------------------------------------------------------------------------- what a survivor, a task and a wait may hold
def with_survivor(**survivor) -> dict:
    return invalid(survivors=[survivor])


def with_task(**task) -> dict:
    return with_survivor(comm="python3", state="D", tasks=[task])


def with_wait(**wait) -> dict:
    return with_task(state="D", waits_on=[wait])


@pytest.mark.parametrize(
    "held",
    [
        pytest.param(with_task(), id="a task that says nothing"),
        pytest.param(with_task(state="D", wchan=None, syscall="running"), id="no call, no wait symbol"),
        pytest.param(with_task(syscall="unreadable"), id="a syscall that could not be read"),
        pytest.param(with_task(syscall={"nr": -1}), id="a task outside any syscall"),
        pytest.param(with_task(syscall={"nr": 21}), id="a call with no fd"),
        pytest.param(with_task(syscall={"nr": 0, "fd": 3}), id="a read of an fd"),
        pytest.param(
            with_wait(fd=3, mount="data", fstype="fuse", space="space-a", path="a/b", pos=0, flags="0100000"),
            id="a descriptor entry",
        ),
        pytest.param(with_wait(mount="data", fstype="fuse", space="space-a", path="a/b"), id="a path-call entry"),
        pytest.param(with_wait(mount="work", fstype=None), id="an entry off the data mount"),
        pytest.param(with_wait(), id="an entry that says nothing"),
        pytest.param(with_survivor(comm="x", waits_on=[{"mount": "data"}]), id="the design's survivor-level waits_on"),
        pytest.param(with_survivor(comm="x", cwd_mount="work", tasks_cut=2), id="a cwd mount and a cut count"),
    ],
)
def test_o97_a_survivor_a_task_and_a_wait_each_hold_the_keys_of_a_closed_list_and_these_pass(held):
    """O97: every key of the closed lists is optional, and the shapes the readers build, a descriptor entry and a
    path-call entry among them, are valid; the same check the readers' own tests make of their output."""
    assert problem(held) is None
    assert validate(decode(recorded(held).dumps())) == V3


@pytest.mark.parametrize(
    ("held", "where"),
    [
        pytest.param(
            with_task(cmdline="python3 -c x"), r"tasks\[0\]: unknown field\(s\) \['cmdline'\]", id="a command line"
        ),
        pytest.param(with_task(environ="A=1"), r"unknown field\(s\) \['environ'\]", id="an environment"),
        pytest.param(
            with_task(blocking={"via": "fd"}), r"unknown field\(s\) \['blocking'\]", id="what the verdict read"
        ),
        pytest.param(with_task(wchan=5), r"tasks\[0\]\.wchan: text", id="a wait symbol that is no text"),
        pytest.param(with_task(state="D" * 65), r"tasks\[0\]\.state: text of at most 64", id="a state too long"),
        pytest.param(with_task(syscall="blocked"), r"syscall: one of", id="a syscall word that is none"),
        pytest.param(with_task(syscall={"nr": 0, "fd": 3, "args": [1]}), r"syscall: .* alone", id="a syscall argument"),
        pytest.param(with_task(syscall={"fd": 3}), r"syscall: .* alone", id="a syscall with no number"),
        pytest.param(with_task(syscall={"nr": True}), r"syscall\.nr: a whole number", id="a bool number"),
        pytest.param(with_task(syscall={"nr": 0, "fd": -1}), r"syscall\.fd: a whole number from 0", id="a negative fd"),
        pytest.param(with_task(waits_on=[{}] * (TASKS_CAP + 1)), r"waits_on: a list of at most 4", id="too many waits"),
        pytest.param(with_task(waits_on={"fd": 3}), r"waits_on: a list of at most 4", id="a wait that is no list"),
        pytest.param(
            with_wait(cmdline="x"), r"waits_on\[0\]: unknown field\(s\) \['cmdline'\]", id="a wait command line"
        ),
        pytest.param(with_wait(target="/home/someone/x"), r"unknown field\(s\) \['target'\]", id="a link's target"),
        pytest.param(with_wait(host="a-node"), r"unknown field\(s\) \['host'\]", id="a node name"),
        pytest.param(with_wait(mount="/mnt/data"), r"mount: one of", id="a mount that is a configured path"),
        pytest.param(with_wait(mount=None), r"mount: one of", id="a mount that is null"),
        pytest.param(with_wait(path="x" * 257), r"path: text of at most 256", id="a path past its cap"),
        pytest.param(with_wait(space="s" * 65), r"space: text of at most 64", id="a space name past its cap"),
        pytest.param(with_wait(fd=True), r"fd: a whole number from 0", id="a bool fd"),
        pytest.param(with_wait(pos=-1), r"pos: a whole number from 0", id="a negative position"),
        pytest.param(with_wait(pos="0"), r"pos: a whole number from 0", id="a position that is text"),
        pytest.param(with_wait(flags=0o100000), r"flags: text of at most 16", id="flags that are a number"),
        pytest.param(
            with_survivor(waits_on=[{"x": 1}]), r"survivors\[0\]\.waits_on\[0\]: unknown field", id="survivor-level"
        ),
        pytest.param(with_survivor(tasks_cut=-1), r"tasks_cut: a whole number from 0", id="a negative cut count"),
        pytest.param(with_survivor(cwd_mount=3), r"cwd_mount: text", id="a cwd mount that is no text"),
    ],
)
def test_o97_a_key_outside_the_closed_lists_or_of_the_wrong_type_is_refused_at_each_level(held, where):
    """O97: the record can hold no command line, environment, configured path, link target or node name whatever a
    reader reads: an unknown key at any of the three levels, and a value of the wrong type, are refused."""
    data = decode(recorded().dumps())
    data["tool_calls"][0]["cell_deadline"] = held
    with pytest.raises(RecordError, match=where):
        validate(data)
