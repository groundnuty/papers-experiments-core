"""L18: what a record keeps of the optional wall limit on one code cell, and that a replay judges the run alike.

The limit is a value of the agent's configuration, `code_cell_bound_s`, and beside it, written only with it,
`code_cell_tools` names the tools that run code cells. Both are optional in version 3: absent when the study declared
no limit (never a null), so a record written before them loads, writes back and replays to its own bytes, and unknown to
a record of version 2. The tool server's bound and its list (L17) are separate keys and stay as they were.
"""

import pytest
from loop_support import FakeClock, FakeProvider
from record_support import record_run

from expcore.plugin import ToolOutput
from expcore.records.record import RunRecord, decode
from expcore.records.schema import RecordError, validate
from expcore.replay.divergence import ReplayDivergence
from expcore.replay.replay import replay_record

V2, V3 = "expcore-record/2", "expcore-record/3"


def killed_then_answered(**options):
    """A record of a run whose code cell ran past a 60 s limit and was killed there, returning the code tool's timeout,
    and whose agent then made a second call, which was answered, and finished."""
    clock = FakeClock()

    def cell(args):
        clock.advance(60)
        return ToolOutput("partial\nthe code ran past its 60 s limit and was killed", True, "timeout")

    code = FakeProvider({"python": cell}, code_cell=True)
    other = FakeProvider({"echo": lambda args: "echoed"})
    script = [[("python", {"code": "x"})], [("echo", {"x": "b"})], "done"]
    return record_run(script, {}, providers=[code, other], clock=clock, code_cell_bound_s=60.0, **options)


def test_l18_an_undeclared_limit_leaves_agent_config_as_it_was_and_a_declared_one_is_a_value_of_it():
    """L18: absent when the study declared none, never a null, and no list either; present, as the number, with the
    names of the tools that run code cells beside it when it declared one. The digest covers it, the record loads and
    writes back to its bytes, and a replay serves the same configuration back."""
    code = FakeProvider({"python": lambda args: "42"}, code_cell=True)
    plain = record_run(["hello"], {}, providers=[code])
    assert "code_cell_bound_s" not in plain.agent_config
    assert "code_cell_tools" not in plain.agent_config
    declared = record_run(["hello"], {}, providers=[code], code_cell_bound_s=3600.0)
    assert declared.agent_config["code_cell_bound_s"] == 3600.0
    assert declared.agent_config["code_cell_tools"] == ["python"]
    assert list(declared.agent_config)[-3:] == ["code_cell_bound_s", "terminal_tools", "code_cell_tools"]
    assert declared.agent_config_sha256 != plain.agent_config_sha256
    text = declared.dumps()
    assert validate(decode(text)) == V3
    assert RunRecord.from_json(decode(text)).dumps() == text
    assert replay_record(declared).dumps() == text


def test_l18_a_run_that_killed_a_cell_at_the_limit_and_went_on_round_trips_and_replays_alike():
    """L18 and R03: the cell's timeout is an error result of the call, the run's next call is made and answered, and the
    outcome is the agent's own. The record validates and the replay, which serves every recorded call by one provider
    and learns which tools run code cells from the record, gives back the same bytes."""
    record = killed_then_answered()
    first, second = record.tool_calls
    assert (first["status"], first["error_class"], first["elapsed_s"]) == ("error", "timeout", 60.0)
    assert "ran past its 60 s limit and was killed" in first["text"]
    assert second["status"] == "ok"
    assert (record.outcome["name"], record.outcome["infra_kind"]) == ("finished", None)
    assert record.agent_config["code_cell_tools"] == ["python"]
    text = record.dumps()
    assert validate(decode(text)) == V3
    assert replay_record(RunRecord.from_json(decode(text))).dumps() == text


def test_l18_a_replay_whose_list_of_code_cell_tools_was_edited_diverges():
    """L18: the list is part of the configuration the record keeps and hashes, so a list edited after the run is not the
    configuration the replay's loop writes, and the replay diverges at the configuration."""
    data = decode(killed_then_answered().dumps())
    data["agent_config"]["code_cell_tools"] = ["echo", "python"]
    with pytest.raises(ReplayDivergence, match="agent_config"):
        replay_record(RunRecord.from_json(data))


@pytest.mark.parametrize(
    "bad", [None, 0, -1.0, "60", True, float("inf")], ids=["null", "zero", "negative", "text", "bool", "inf"]
)
def test_l18_a_declared_limit_in_a_record_is_a_positive_finite_number(bad):
    """L18: the rule the configuration holds, checked when a record is loaded. A null is refused: the writer leaves the
    key out when nothing was declared."""
    data = decode(killed_then_answered().dumps())
    data["agent_config"]["code_cell_bound_s"] = bad
    with pytest.raises(RecordError, match=r"agent_config\.code_cell_bound_s"):
        validate(data)


def test_l18_a_record_of_version_two_that_holds_the_limit_or_its_list_is_refused():
    """L18: both are version 3's, so a record that names version 2 and holds either is unknown to its own version."""
    base = decode(record_run(["hello"], {}, record_options={"schema_version": V2}).dumps())
    assert validate(base) == V2
    for key, value in (("code_cell_bound_s", 60.0), ("code_cell_tools", [])):
        data = decode(record_run(["hello"], {}, record_options={"schema_version": V2}).dumps())
        data["agent_config"][key] = value
        with pytest.raises(RecordError, match=rf"agent_config: unknown field\(s\) \['{key}'\]"):
            validate(data)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c.pop("code_cell_tools"), r"agent_config\.code_cell_tools: held when"),
        (lambda c: c.pop("code_cell_bound_s"), r"agent_config\.code_cell_tools: held when"),
        (lambda c: c.update(code_cell_tools="python"), r"agent_config\.code_cell_tools: a list of tool names"),
        (lambda c: c.update(code_cell_tools=[None]), r"agent_config\.code_cell_tools: a list of tool names"),
    ],
    ids=["missing", "without-a-limit", "text", "not-names"],
)
def test_l18_the_list_of_code_cell_tools_is_a_list_of_names_written_with_the_limit_and_only_with_it(change, message):
    """L18: a record whose list is missing under a limit, held without one, or not a list of names is refused when it is
    loaded."""
    data = decode(killed_then_answered().dumps())
    change(data["agent_config"])
    with pytest.raises(RecordError, match=message):
        validate(data)


def test_l18_the_two_bounds_are_independent_in_one_record():
    """L18: a study may declare both. Each is written with its own list, neither list holds the other's tools, and the
    record validates and replays."""
    code = FakeProvider({"python": lambda args: "42"}, code_cell=True)
    server = FakeProvider({"search": lambda args: "found"}, tool_server=True)
    record = record_run(
        ["hello"], {}, providers=[code, server], code_cell_bound_s=3600.0, tool_server_call_bound_s=300.0
    )
    assert record.agent_config["code_cell_tools"] == ["python"]
    assert record.agent_config["tool_server_tools"] == ["search"]
    text = record.dumps()
    assert validate(decode(text)) == V3
    assert replay_record(RunRecord.from_json(decode(text))).dumps() == text
