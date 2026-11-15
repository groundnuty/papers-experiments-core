"""L17: what a record keeps of the per-call tool bound: the bound as a value of the agent's configuration, and each
call's elapsed time as an optional field of the call.

Both are optional in version 3, so a record written before them still loads and replays to its own bytes: the bound is
absent from `agent_config` when it was not declared (never a null), and `elapsed_s` is absent from a call that never
started (never a null), and from every call of a record that predates the field.
"""

import pytest
from loop_support import FakeClock, FakeProvider
from record_support import record_run

from expcore.records.record import RunRecord, decode
from expcore.records.schema import SCHEMAS, RecordError, fields_of, optional_in, validate
from expcore.replay.divergence import ReplayDivergence
from expcore.replay.replay import replay_record

V2, V3 = "expcore-record/2", "expcore-record/3"
TWO_CALLS = [[("submit", {"x": "a"}), ("echo", {"x": "b"})], "never asked"]


def two_calls(**options):
    """A record of a run whose first call, a terminal one, takes 12.5 s on the fake clock and ends the attempt, so its
    second call is answered as skipped and never starts."""
    clock = FakeClock()

    def submit(args):
        clock.advance(12.5)
        return "submitted"

    handlers = {"submit": submit, "echo": lambda args: "echoed"}
    return record_run(TWO_CALLS, handlers, terminal=("submit",), clock=clock, **options)


def test_l17_a_started_call_records_its_elapsed_seconds_and_a_call_that_never_started_has_none():
    """L17: `elapsed_s` is the wall time from the reading before the call to the one that ended it, the same span as
    `started_s` to `ended_s`. A call answered as skipped never started, so its entry has no such field: absent, never a
    null."""
    record = two_calls()
    started, skipped = record.tool_calls
    assert started["elapsed_s"] == started["ended_s"] - started["started_s"] == 12.5
    assert skipped["status"] == "skipped"
    assert "elapsed_s" not in skipped
    assert list(started)[-1] == "elapsed_s"  # after every field the first merge point wrote
    text = record.dumps()
    assert validate(decode(text)) == V3
    assert RunRecord.from_json(decode(text)).dumps() == text


def test_l17_the_field_is_optional_in_version_three_and_in_no_earlier_version():
    """L17: version 3 adds it to the call section as optional; a version-2 record has none."""
    (v3,) = (s for s in SCHEMAS if s.name == V3)
    assert "elapsed_s" in v3.added["tool_call"]
    assert "elapsed_s" in v3.optional["tool_call"]
    assert "elapsed_s" in optional_in("tool_call", V3)
    assert "elapsed_s" not in fields_of("tool_call", V2)


def test_l17_a_record_of_version_two_is_built_without_it_and_one_that_holds_it_is_refused():
    """L17: an older version writes none of the fields a later one added, and a record of it that holds one is unknown
    to its own version."""
    record = two_calls(record_options={"schema_version": V2})
    assert all("elapsed_s" not in call for call in record.tool_calls)
    data = decode(record.dumps())
    assert validate(data) == V2
    data["tool_calls"][0]["elapsed_s"] = 12.5
    with pytest.raises(RecordError, match=r"tool_calls\[0\]: unknown field\(s\) \['elapsed_s'\]"):
        validate(data)


@pytest.mark.parametrize("bad", [None, "12.5", True, [], float("nan")], ids=["null", "text", "bool", "list", "nan"])
def test_l17_a_present_elapsed_time_is_a_finite_number(bad):
    """L17: a null is refused, since the writer leaves the field out when the call never started."""
    data = decode(two_calls().dumps())
    data["tool_calls"][0]["elapsed_s"] = bad
    with pytest.raises(RecordError, match=r"tool_calls\[0\]\.elapsed_s"):
        validate(data)


def test_l17_an_elapsed_time_below_nothing_is_refused():
    """L17: a call cannot have taken less than no time."""
    data = decode(two_calls().dumps())
    data["tool_calls"][0]["elapsed_s"] = -0.5
    with pytest.raises(RecordError, match=r"tool_calls\[0\]\.elapsed_s: -0.5 is not a number of seconds"):
        validate(data)


def test_l17_a_record_written_before_the_field_replays_to_its_own_bytes_without_it():
    """L17 and R03: the loop derives each call's elapsed time again on replay, and a record that predates the field does
    not hold it: the replay does not fill it in."""
    data = decode(two_calls().dumps())
    for call in data["tool_calls"]:
        call.pop("elapsed_s", None)
    older = RunRecord.from_json(data)
    assert all("elapsed_s" not in call for call in older.tool_calls)
    assert replay_record(older).dumps() == older.dumps()


def test_l17_a_record_that_holds_the_field_replays_to_it_and_one_whose_value_was_changed_diverges():
    """L17 and R03: the elapsed time is derived from the recorded clock readings, so the replay writes the same value,
    and a record whose stored value is not the one its readings give is a divergence at that path."""
    record = two_calls()
    assert replay_record(record).dumps() == record.dumps()
    data = decode(record.dumps())
    data["tool_calls"][0]["elapsed_s"] = 99.0
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert "tool_calls[0].elapsed_s" in str(caught.value)


def test_l17_an_undeclared_bound_leaves_agent_config_as_it_was_and_a_declared_one_is_a_value_of_it():
    """L17: absent when the study declared none, never a null; present, as the number, when it declared one. The digest
    of the configuration covers it, and the record loads and writes back to its bytes."""
    plain = record_run(["hello"], {})
    assert "tool_server_call_bound_s" not in plain.agent_config
    declared = record_run(["hello"], {}, tool_server_call_bound_s=60.0)
    assert declared.agent_config["tool_server_call_bound_s"] == 60.0
    assert declared.agent_config_sha256 != plain.agent_config_sha256
    text = declared.dumps()
    assert validate(decode(text)) == V3
    assert RunRecord.from_json(decode(text)).dumps() == text
    assert replay_record(declared).dumps() == text


@pytest.mark.parametrize(
    "bad", [None, 0, -1.0, "60", True, float("inf")], ids=["null", "zero", "negative", "text", "bool", "inf"]
)
def test_l17_a_declared_bound_in_a_record_is_a_positive_finite_number(bad):
    """L17: the same rule the configuration holds, checked when a record is loaded. A null is refused: the writer leaves
    the key out when nothing was declared."""
    data = decode(record_run(["hello"], {}, tool_server_call_bound_s=60.0).dumps())
    data["agent_config"]["tool_server_call_bound_s"] = bad
    with pytest.raises(RecordError, match=r"agent_config\.tool_server_call_bound_s"):
        validate(data)


def test_l17_a_record_of_version_two_that_holds_a_bound_is_refused():
    """L17: the setting is version 3's, so a record that names version 2 and holds it is unknown to its own version."""
    data = decode(record_run(["hello"], {}, record_options={"schema_version": V2}).dumps())
    assert validate(data) == V2
    data["agent_config"]["tool_server_call_bound_s"] = 60.0
    with pytest.raises(RecordError, match=r"agent_config: unknown field\(s\) \['tool_server_call_bound_s'\]"):
        validate(data)


def test_l17_a_declared_bound_is_recorded_with_the_tools_it_reached_and_an_undeclared_one_with_none():
    """L17: `agent_config.tool_server_tools` names the tools of the tool servers, the ones the bound reached, so a
    reader can see that the code tool was not among them. It is written beside the bound and only with it."""
    server = FakeProvider({"search": lambda args: "found"}, tool_server=True)
    code = FakeProvider({"python": lambda args: "42"})
    declared = record_run(["hello"], {}, providers=[code, server], tool_server_call_bound_s=60.0)
    assert declared.agent_config["tool_server_tools"] == ["search"]
    assert list(declared.agent_config)[-2:] == ["terminal_tools", "tool_server_tools"]
    alone = record_run(["hello"], {}, providers=[code], tool_server_call_bound_s=60.0)
    assert alone.agent_config["tool_server_tools"] == []  # a bound that reached no tool says so
    plain = record_run(["hello"], {}, providers=[code, server])
    assert "tool_server_tools" not in plain.agent_config  # no bound: no list, and the configuration is what it was
    for record in (declared, alone):
        text = record.dumps()
        assert validate(decode(text)) == V3
        assert replay_record(RunRecord.from_json(decode(text))).dumps() == text


def test_l17_the_old_name_of_the_bound_is_unknown_to_a_record():
    """L17: `tool_call_bound_s` is not a field of any version: a record that holds it is refused, never read as the
    new one."""
    data = decode(record_run(["hello"], {}).dumps())
    data["agent_config"]["tool_call_bound_s"] = 60.0
    with pytest.raises(RecordError, match=r"agent_config: unknown field\(s\) \['tool_call_bound_s'\]"):
        validate(data)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c.pop("tool_server_tools"), r"agent_config\.tool_server_tools: held when"),
        (lambda c: c.pop("tool_server_call_bound_s"), r"agent_config\.tool_server_tools: held when"),
        (lambda c: c.update(tool_server_tools="search"), r"agent_config\.tool_server_tools: a list of tool names"),
        (lambda c: c.update(tool_server_tools=[None]), r"agent_config\.tool_server_tools: a list of tool names"),
    ],
    ids=["missing", "without-a-bound", "text", "not-names"],
)
def test_l17_the_list_of_tool_servers_is_a_list_of_names_written_with_the_bound_and_only_with_it(change, message):
    """L17: the replay judges a cut call by the list, so a record whose list is missing under a bound, held without
    one, or not a list of names is refused when it is loaded."""
    data = decode(record_run(["hello"], {}, tool_server_call_bound_s=60.0).dumps())
    change(data["agent_config"])
    with pytest.raises(RecordError, match=message):
        validate(data)


def test_l17_a_record_of_version_two_that_holds_the_list_is_refused():
    """L17: the list is version 3's, as the bound it belongs to is."""
    data = decode(record_run(["hello"], {}, record_options={"schema_version": V2}).dumps())
    data["agent_config"]["tool_server_tools"] = []
    with pytest.raises(RecordError, match=r"agent_config: unknown field\(s\) \['tool_server_tools'\]"):
        validate(data)
