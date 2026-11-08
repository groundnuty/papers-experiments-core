"""The record schema: versions, the table of added fields, and the refusals at load (R02)."""

import copy

import pytest
from record_support import hand_made

from expcore.records.record import RunRecord
from expcore.records.schema import (
    CURRENT,
    SCHEMAS,
    RecordError,
    SchemaVersion,
    added_after,
    config_defaults,
    fields_of,
    validate,
)


def data(**changes):
    """The JSON form of a valid record, with changes applied at the top level."""
    return {**hand_made().to_json(), **changes}


def test_a_valid_record_validates_and_reports_its_version():
    assert validate(data()) == CURRENT


def test_the_table_has_a_current_version_and_lists_each_version_once():
    assert SCHEMAS[-1].name == CURRENT
    assert len({s.name for s in SCHEMAS}) == len(SCHEMAS)


def test_R02_an_unknown_schema_version_is_refused_with_the_readable_ones_named():
    """R02: a record of a version the table does not hold is refused at load."""
    with pytest.raises(RecordError, match=r"unsupported record schema 'expcore-record/99'.*expcore-record/1"):
        RunRecord.from_json(data(schema_version="expcore-record/99"))


def test_R02_a_missing_or_non_text_version_is_refused():
    """R02: no version, or one that is not text, is refused."""
    without = data()
    del without["schema_version"]
    with pytest.raises(RecordError, match="unsupported record schema"):
        validate(without)
    with pytest.raises(RecordError, match="unsupported record schema"):
        validate(data(schema_version=1))


def test_R02_an_unknown_top_level_field_fails_load():
    """R02: an unknown top-level field fails load, and the message names it."""
    with pytest.raises(RecordError, match=r"unknown field\(s\) \['extra'\]"):
        RunRecord.from_json(data(extra=1))


def test_R02_a_missing_field_fails_load():
    """R02: a field the version requires cannot be left out."""
    without = data()
    del without["outcome"]
    with pytest.raises(RecordError, match=r"missing field\(s\) \['outcome'\]"):
        RunRecord.from_json(without)


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (("transcript", 0), "surprise"),
        (("prompt", "system"), "surprise"),
        (("outcome",), "surprise"),
        (("totals",), "surprise"),
        (("transcript", 0, "usage"), "surprise"),
    ],
)
def test_R02_an_unknown_field_in_a_nested_section_fails_load(path, field):
    """R02: unknown fields are refused in the nested sections too, not only at the top."""
    bad = copy.deepcopy(data())
    node = bad
    for step in path:
        node = node[step]
    node[field] = 1
    with pytest.raises(RecordError, match="unknown field"):
        validate(bad)


def test_a_value_of_the_wrong_type_names_its_place():
    with pytest.raises(RecordError, match=r"attempt: expected int, got str"):
        validate(data(attempt="1"))
    with pytest.raises(RecordError, match="expected int"):
        validate(data(attempt=True))


def test_R10_a_missing_count_is_none_and_a_non_finite_number_is_refused():
    """R10: an unreported count is null in the file; NaN is not a value a record can hold."""
    record = hand_made()
    assert record.transcript[0]["usage"]["uncached_input_tokens"] is None
    bad = copy.deepcopy(data())
    bad["transcript"][0]["latency_s"] = float("nan")
    with pytest.raises(RecordError, match="latency_s"):
        validate(bad)


def test_an_outcome_outside_the_vocabulary_or_with_a_stray_kind_is_refused():
    bad = copy.deepcopy(data())
    bad["outcome"]["name"] = "exploded"
    with pytest.raises(RecordError, match="unknown outcome"):
        validate(bad)
    bad = copy.deepcopy(data())
    bad["outcome"]["infra_kind"] = "endpoint"
    with pytest.raises(RecordError, match="infra_kind"):
        validate(bad)


def test_a_tool_call_status_outside_the_closed_list_is_refused():
    record = hand_made(
        tool_calls=[
            {
                "turn": 0,
                "round": 1,
                "call_id": "c",
                "name": "t",
                "arguments": {},
                "status": "weird",
                "text": "",
                "raw_text": "",
                "raw_chars": 0,
                "truncated": False,
                "error_class": None,
                "raw_arguments": None,
                "infra_wait_s": 0.0,
                "started_s": 0.0,
                "ended_s": 0.1,
                "detail": "",
                "source_truncated": None,
                "waits_left_open": 0,
                "terminal": False,
                "infra_wait_by_kind": {},
                "rendering_verified": True,
            }
        ]
    )
    with pytest.raises(RecordError, match=r"tool_calls\[0\].status"):
        validate(record.to_json())


def test_instruments_and_scores_are_objects_under_lower_case_namespaces():
    with pytest.raises(RecordError, match="instruments"):
        validate(data(instruments={"Bad Name": {}}))
    with pytest.raises(RecordError, match="scores"):
        validate(data(scores={"fine_name": 3}))
    assert validate(data(instruments={"probe_a": {"x": 1}}, scores={"scorer_b": {"verdict": "fulfilled"}})) == CURRENT


# --- a table with two versions -----------------------------------------------------------------------------------


V1 = SchemaVersion("expcore-record/1")
V2 = SchemaVersion(
    "expcore-record/2",
    added={"record": ("instruments",), "turn": ("request_sha256",)},
    config_before={"cleanup_deadline_s": 60.0},
)
TABLE = (V1, V2)


def test_a_version_table_must_name_sections_and_fields_the_format_has():
    with pytest.raises(ValueError, match="fields the 'turn' section does not have"):
        SchemaVersion("expcore-record/3", added={"turn": ("nope",)})
    with pytest.raises(ValueError, match="must look like"):
        SchemaVersion("agent-run/3")


def test_the_fields_a_later_version_added_are_not_the_older_versions():
    assert added_after("expcore-record/1", "turn", TABLE) == {"request_sha256"}
    assert added_after("expcore-record/2", "turn", TABLE) == frozenset()
    assert "request_sha256" not in fields_of("turn", "expcore-record/1", TABLE)
    assert "request_sha256" in fields_of("turn", "expcore-record/2", TABLE)


def test_the_defaults_in_force_before_a_setting_are_those_of_the_versions_after_the_record():
    assert config_defaults("expcore-record/1", TABLE) == {"cleanup_deadline_s": 60.0}
    assert config_defaults("expcore-record/2", TABLE) == {}


def test_R02_a_field_a_later_schema_added_is_refused_in_an_older_record():
    """R02: a record that names version 1 cannot hold a field only version 2 introduced; the message says so."""
    older = hand_made(schema_version="expcore-record/1").to_json(TABLE)
    older["transcript"][0]["request_sha256"] = "a" * 64
    with pytest.raises(RecordError, match=r"request_sha256 added by a later schema than expcore-record/1"):
        validate(older, TABLE)


# --- every structure the core defines refuses a field it does not know (R02) ----------------------------------------


def scenario_json(name):
    from replay_scenarios import SCENARIOS

    return copy.deepcopy(SCENARIOS[name][0]().to_json())


def put(data, path, key, value=1):
    node = data
    for step in path:
        node = node[step]
    node[key] = value
    return data


@pytest.mark.parametrize(
    ("scenario", "path", "where"),
    [
        ("tool_crash", ("outcome", "defect"), "outcome.defect"),
        ("malformed", ("totals", "malformed_events", 0), r"totals.malformed_events\[0\]"),
        ("tools", ("agent_config",), "agent_config"),
        ("tools", ("agent_config", "limits"), r"agent_config.limits"),
        ("tools", ("code",), "code"),
        ("cached_turn", ("transcript", 0, "retries", 0), r"transcript\[0\].retries\[0\]"),
    ],
)
def test_R02_an_unknown_field_in_a_structure_the_core_defines_fails_load(scenario, path, where):
    """R02: the defect of an outcome, a malformed-call event, the configuration, the code identity and a retry."""
    data = put(scenario_json(scenario), path, "surprise")
    with pytest.raises(RecordError, match=f"{where}: unknown field.*surprise"):
        validate(data)


def test_R02_a_defect_or_a_configuration_with_a_missing_field_fails_load():
    """R02: the structures the core defines are not left half empty either."""
    data = scenario_json("tool_crash")
    del data["outcome"]["defect"]["stack_sha256"]
    with pytest.raises(RecordError, match=r"outcome.defect: missing field.*stack_sha256"):
        validate(data)
    data = scenario_json("tools")
    del data["agent_config"]["limits"]["token_budget"]
    with pytest.raises(RecordError, match=r"agent_config.limits: missing field.*token_budget"):
        validate(data)
    data = scenario_json("tools")
    data["agent_config"]["terminal_tools"] = "submit"
    with pytest.raises(RecordError, match="terminal_tools"):
        validate(data)


def test_R02_a_route_without_the_settings_the_record_adds_fails_load():
    """R02: replay reads the settings sets and their digest from the route, so a route without them is refused."""
    data = scenario_json("tools")
    del data["route"]["settings"]
    with pytest.raises(RecordError, match=r"route\.settings"):
        validate(data)
    data = scenario_json("tools")
    data["route"]["settings_sent_sha256"] = 3
    with pytest.raises(RecordError, match="settings_sent_sha256"):
        validate(data)


def test_L04_a_wait_that_was_declared_and_lasted_no_time_is_valid_and_a_negative_one_is_not():
    """L04: 0.0 seconds waited under a kind is a figure a real run can produce; less than nothing is not."""
    data = scenario_json("tools")
    data["tool_calls"][0]["infra_wait_by_kind"] = {"lease_queue": 0.0}
    data["transcript"][0]["infra_wait_by_kind"] = {"model_retry": 0}
    data["totals"]["infra_wait_by_kind"] = {"sandbox_lifecycle": 0.0}
    assert validate(data) == CURRENT
    data["totals"]["infra_wait_by_kind"] = {"sandbox_lifecycle": -0.5}
    with pytest.raises(RecordError, match="seconds waited are given per kind"):
        validate(data)


def infra_data(kinds, flag, kind="endpoint"):
    """The JSON of a record that ended as infrastructure of a kind, with declared treatment kinds and a flag."""
    changed = copy.deepcopy(data(treatment_kinds=list(kinds)))
    changed["outcome"].update(name="infrastructure", detail="HTTP 503", infra_kind=kind, treatment=flag)
    return changed


def test_R02_the_treatment_flag_must_be_what_the_outcome_and_the_declared_kinds_make_it():
    """R02: adds that a flag set against its own record, either way, is refused at load."""
    assert validate(infra_data(["endpoint"], True)) == CURRENT
    assert validate(infra_data([], False)) == CURRENT
    assert validate(infra_data(["sandbox"], False)) == CURRENT
    with pytest.raises(
        RecordError,
        match=r"outcome\.treatment: is False but .* 1 completed turn\(s\) make it True",
    ):
        validate(infra_data(["endpoint"], False))
    with pytest.raises(
        RecordError,
        match=r"outcome\.treatment: is True but .* 1 completed turn\(s\) make it False",
    ):
        validate(infra_data([], True))
    with pytest.raises(RecordError, match=r"outcome\.treatment: is True but .* make it False"):
        validate(data(outcome={**data()["outcome"], "treatment": True}))  # a finished outcome is never treatment


def test_c01_a_failure_before_the_first_model_turn_is_never_marked_treatment():
    """C01: a record of an infrastructure failure of a declared kind with no completed turn says treatment is False,
    and a record that says True contradicts its own transcript."""
    one = infra_data(["endpoint"], True)
    assert len(one["transcript"]) == 1
    none = copy.deepcopy(one)
    none["transcript"] = []
    none["outcome"]["treatment"] = False
    assert validate(none) == CURRENT
    none["outcome"]["treatment"] = True
    with pytest.raises(RecordError, match=r"outcome\.treatment: is True but .* 0 completed turn\(s\) make it False"):
        validate(none)


def test_o05_a_record_cannot_mark_an_untreatable_kind_as_treatment():
    """O05: a record that lists tool_plane_integrity among its treatment kinds and flags the failure as treatment is
    refused, because the kind can never be one."""
    bad = infra_data(["tool_plane_integrity"], True, kind="tool_plane_integrity")
    with pytest.raises(RecordError, match=r"outcome\.treatment: is True but .* make it False"):
        validate(bad)
    assert validate(infra_data(["tool_plane_integrity"], False, kind="tool_plane_integrity")) == CURRENT


@pytest.mark.parametrize(
    ("kinds", "match"),
    [
        ("endpoint", "record.treatment_kinds: expected list, got str"),
        ([1], "a list of infrastructure kinds"),
        (["wifi"], "unknown infrastructure kind 'wifi'"),
        (["sandbox", "endpoint"], "written sorted, each once"),
        (["endpoint", "endpoint"], "written sorted, each once"),
    ],
)
def test_R02_the_declared_treatment_kinds_are_a_sorted_list_of_registered_kinds(kinds, match):
    """R02: adds the checks on the identity field that the flag is derived from."""
    with pytest.raises(RecordError, match=match):
        validate(data(treatment_kinds=kinds))


def test_R02_a_record_without_the_treatment_fields_is_refused():
    """R02: adds that both new fields of version 1 are required, so a record lacking them is not a record."""
    without = copy.deepcopy(data())
    del without["treatment_kinds"]
    with pytest.raises(RecordError, match=r"missing field.*treatment_kinds"):
        validate(without)
    without = copy.deepcopy(data())
    del without["outcome"]["treatment"]
    with pytest.raises(RecordError, match=r"missing field.*treatment"):
        validate(without)
