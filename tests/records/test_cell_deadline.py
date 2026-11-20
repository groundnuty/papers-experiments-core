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
