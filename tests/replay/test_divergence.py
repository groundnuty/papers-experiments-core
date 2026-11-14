"""Every divergence point of the replay (R03, R06), and a replay of a record of an older schema version."""

import copy
import json

import pytest
from replay_scenarios import SCENARIOS

from expcore.records.digest import digest
from expcore.records.record import RunRecord, decode
from expcore.records.schema import RecordError, SchemaVersion
from expcore.replay.replay import ReplayDivergence, replay_record


def record_of(name: str = "tools") -> dict:
    """The JSON form of a recorded scenario, to be altered."""
    return copy.deepcopy(SCENARIOS[name][0]().to_json())


def diverges(data: dict, path: str, match: str = "") -> ReplayDivergence:
    """Replay altered JSON and return the divergence, which must be at `path` and mention `match`."""
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert caught.value.path == path
    assert match in str(caught.value)
    return caught.value


def call_index(data: dict, name: str, status: str = "ok") -> int:
    return next(i for i, c in enumerate(data["tool_calls"]) if c["name"] == name and c["status"] == status)


def test_an_untouched_record_does_not_diverge():
    assert replay_record(record_of()).outcome["name"] == "finished"


def test_R03_the_prompt_text_must_have_its_recorded_hash():
    """R03: a prompt that differs from its hash is a divergence before anything runs."""
    data = record_of()
    data["prompt"]["system"]["text"] += "!"
    diverges(data, "prompt.system.sha256")
    data = record_of()
    data["prompt"]["user"]["text"] = "another task"
    diverges(data, "prompt.user.sha256")


def test_R03_the_tools_rebuilt_from_the_record_must_have_the_recorded_manifest_hash():
    """R03: a tool whose description differs from the one the model was shown is a divergence."""
    data = record_of()
    data["tools"][0]["description"] += " and more"
    diverges(data, "prompt.tools_sha256")


def test_R03_the_stored_configuration_must_have_its_hash():
    """R03: a cap changed in the record without its hash is a divergence."""
    data = record_of()
    data["agent_config"]["limits"]["max_tool_rounds"] = 3
    diverges(data, "agent_config_sha256")


def test_R03_a_configuration_the_loop_cannot_be_built_from_is_a_divergence():
    """R03: a value the loop refuses (a policy it does not have) is named, not ignored."""
    data = record_of()
    data["agent_config"]["malformed_policy"] = "run_with_empty_arguments"
    diverges(data, "agent_config", "cannot be rebuilt")


def test_R03_a_result_message_that_does_not_hold_what_the_loop_gave_the_model_diverges_before_turn_k():
    """R03: the transcript before turn 1 differs: the tool message of turn 0 is not the result the loop rendered."""
    data = record_of()
    data["transcript"][0]["tool_messages"][0]["content"] = "tampered"
    diverges(data, "transcript[0].tool_messages")


def test_R03_a_changed_reply_makes_the_conversation_of_the_next_request_differ():
    """R03: the transcript before turn 1 differs: a recorded reply was edited, so the request digest no longer holds."""
    data = record_of()
    data["transcript"][0]["native"]["tool_calls"][0]["type"] = "other"  # nothing the neutral turn is derived from
    diverges(data, "transcript[1].request_sha256")


def test_R03_a_native_message_that_the_neutral_turn_does_not_follow_from_diverges_naming_the_turn():
    """R03: change only the native content; the loop would be served a reply the conversation does not hold."""
    data = record_of("malformed")
    assert data["transcript"][2]["neutral"]["text"] == "done"
    data["transcript"][2]["native"]["content"] = "edited"
    error = diverges(data, "transcript[2].neutral.text")
    assert "the recorded reply text does not match the model's raw reply in that turn" in str(error)


def test_R03_a_tool_call_changed_only_in_the_native_message_diverges():
    """R03: the arguments a call was made with must be those of the native message that carries it."""
    data = record_of()
    data["transcript"][0]["native"]["tool_calls"][0]["function"]["arguments"] = '{"x": "other"}'
    diverges(data, "transcript[0].neutral.tool_calls[0].arguments.x")


def test_R03_an_answer_edited_in_a_record_is_named_in_plain_words_with_what_to_conclude():
    """R03: the message says what was compared and what it means, in words the guide uses, not the code's own terms."""
    data = record_of()
    data["transcript"][0]["neutral"]["tool_calls"][0]["arguments"]["x"] = "edited"
    error = diverges(data, "transcript[0].neutral.tool_calls[0].arguments.x")
    assert str(error).endswith(
        "the recorded tool call does not match the model's raw reply in that turn (transcript[0].native): the record "
        "was changed after it was written, or the backend now reads that reply differently"
    )
    assert "neutral turn" not in str(error)
    assert "native message" not in str(error)


def test_R03_a_malformed_call_follows_from_the_raw_arguments_in_its_native_message():
    """R03: the history keeps the raw text of arguments that were not an object, and the parse error follows from it."""
    data = record_of("malformed")
    data["transcript"][0]["native"]["tool_calls"][0]["function"]["arguments"] = json.dumps({"_raw": '{"b":'})
    diverges(data, "transcript[0].neutral.tool_calls[0].raw_arguments")


def test_R03_a_record_of_a_backend_with_no_registered_parser_cannot_be_replayed():
    """R03: the native messages of an unknown shape cannot be checked against the neutral turn, so the record fails."""
    data = record_of()
    data["route"]["backend"] = "some_new_backend"
    diverges(data, "transcript[0].native")


def test_R03_a_result_whose_call_id_and_content_have_traded_places_diverges():
    """R03: the fields are compared by name, so the same two strings in the other fields are not the result."""
    data = record_of()
    message = data["transcript"][0]["tool_messages"][0]
    message["tool_call_id"], message["content"] = message["content"], message["tool_call_id"]
    diverges(data, "transcript[0].tool_messages")


def test_R03_swapping_two_results_of_one_turn_diverges():
    """R03: results are validated per call id, not by set membership: each message must hold its own call's result."""
    data = record_of()
    first, second = data["transcript"][0]["tool_messages"][:2]
    assert first["tool_call_id"] != second["tool_call_id"]
    first["content"], second["content"] = second["content"], first["content"]
    error = diverges(data, "transcript[0].tool_messages")
    assert first["tool_call_id"] in str(error) or second["tool_call_id"] in str(error)


def test_R06_tampering_with_recorded_reasoning_diverges_on_replay():
    """R06: the reasoning is echoed in the next request, so editing it changes the digest that pins that request."""
    data = record_of("reasoning")
    assert data["transcript"][0]["native"]["reasoning_content"] == "private notes"
    data["transcript"][0]["native"]["reasoning_content"] = "edited notes"
    diverges(data, "transcript[1].request_sha256", "pinned")


def test_R06_reasoning_edited_where_the_neutral_turn_is_derived_from_diverges_at_that_turn():
    """R06: the reasoning the loop was served is the native message's, so editing it there is found at its own turn."""
    data = record_of("reasoning")
    data["transcript"][0]["native"]["_reasoning"] = "edited notes"
    diverges(data, "transcript[0].neutral.reasoning")


def test_R06_a_settings_set_that_does_not_have_its_digest_diverges_on_replay():
    """R06: the settings a request sent are pinned per turn, so a changed set is found at that turn."""
    data = record_of()
    sha = data["transcript"][0]["settings_sha256"]
    data["route"]["settings"][sha] = {"temperature": 1.0}
    diverges(data, "transcript[0].settings_sha256")


def test_R06_a_turn_that_names_no_stored_settings_diverges():
    """R06: a digest with no settings behind it is a divergence."""
    data = record_of()
    data["transcript"][1]["settings_sha256"] = digest({"temperature": 0.9})
    diverges(data, "transcript[1].settings_sha256")


def test_R03_a_model_step_beyond_the_record_is_a_divergence():
    """R03: the record holds fewer turns than the loop asks for."""
    data = record_of()
    data["transcript"].pop()
    diverges(data, "transcript", "model step 6")


def test_R03_a_tool_call_beyond_the_record_is_a_divergence():
    """R03: the loop runs a call the record never made."""
    data = record_of()
    del data["tool_calls"][call_index(data, "submit")]
    diverges(data, "tool_calls", "the replay ran tool call 5")


def test_R03_a_tool_called_with_other_arguments_is_a_divergence():
    """R03: calls are served by order and exact arguments."""
    data = record_of()
    data["tool_calls"][0]["arguments"] = {"x": "other"}
    diverges(data, "tool_calls[0].arguments")


def test_R03_a_different_tool_name_is_a_divergence():
    data = record_of()
    data["tool_calls"][0]["name"] = "fail"
    diverges(data, "tool_calls[0].name")


def test_R03_a_result_the_loop_renders_differently_from_what_the_model_saw_is_a_divergence():
    """R03: the stored text and the re-rendered one disagree."""
    data = record_of()
    data["tool_calls"][0]["text"] = "something the model never saw"
    diverges(data, "tool_calls[0].text", "re-rendered")


def test_R03_a_raw_result_that_no_longer_caps_to_the_stored_text_is_a_divergence():
    """R03: the cap is applied again on replay, so a changed raw result of the recorded length shows."""
    data = record_of()
    index = next(i for i, c in enumerate(data["tool_calls"]) if c["truncated"])
    data["tool_calls"][index]["raw_text"] = "Z" * data["tool_calls"][index]["raw_chars"]
    diverges(data, f"tool_calls[{index}].text")


def test_R03_a_raw_result_of_another_length_than_recorded_is_a_divergence_naming_the_call():
    """R03: the raw text redaction did not touch has exactly raw_chars characters, so a shortened one shows at once."""
    data = record_of()
    index = next(i for i, c in enumerate(data["tool_calls"]) if c["truncated"])
    data["tool_calls"][index]["raw_text"] = "short now"
    found = diverges(data, f"tool_calls[{index}].raw_chars")
    assert data["tool_calls"][index]["call_id"] in str(found)


def test_R03_recorded_model_steps_left_unused_are_a_divergence():
    """R03: the record holds a turn the replay never asked for."""
    data = record_of()
    data["transcript"].append(copy.deepcopy(data["transcript"][-1]))
    diverges(data, "transcript", "6 of 7")


def test_R03_recorded_tool_calls_left_unused_are_a_divergence():
    """R03: the record holds a call that reached a provider and the replay never made it."""
    data = record_of()
    extra = copy.deepcopy(data["tool_calls"][0])
    extra["call_id"] = "extra"
    data["tool_calls"].append(extra)
    diverges(data, "tool_calls", "5 of 6")


def test_R03_a_clock_read_beyond_the_record_is_a_divergence():
    data = record_of()
    data["clock"].pop()
    diverges(data, "clock", "read the clock")


def test_R03_recorded_clock_readings_left_unused_are_a_divergence():
    data = record_of()
    data["clock"].append(["after_tool", 1.0])
    diverges(data, "clock", "were not read")


def test_R03_an_outcome_that_differs_from_the_replayed_one_is_a_divergence():
    """R03: the outcome with its caps is re-derived, not copied."""
    data = record_of()
    data["outcome"]["name"] = "turn_cap"
    diverges(data, "outcome.name")


def test_R03_totals_are_re_derived():
    data = record_of()
    data["totals"]["tokens_used"] += 1
    diverges(data, "totals.tokens_used")


def test_R03_the_recorded_failure_of_the_last_request_is_replayed_and_checked():
    """R03: the failure is raised again, so a tampered kind shows in the outcome it produces."""
    data = record_of("infra_turn_three")
    assert replay_record(RunRecord.from_json(copy.deepcopy(data))).outcome["infra_kind"] == "endpoint"
    data["failed_request"]["infra_kind"] = "sandbox"
    diverges(data, "outcome.infra_kind")


def test_R03_a_harness_defect_replays_without_its_stack_hash_and_with_its_exception_class():
    """R03: the stack hash comes from a live traceback and is excused; the class and the call detail are compared."""
    data = record_of("tool_crash")
    data["outcome"]["defect"]["stack_sha256"] = "0" * 64
    assert replay_record(RunRecord.from_json(copy.deepcopy(data))).outcome["defect"]["exception"] == "KeyError"
    data["tool_calls"][0]["detail"] = "OtherError"
    diverges(data, "tool_calls[0].detail")


def test_R03_a_record_with_an_unknown_field_is_refused_before_replay():
    """R03: the replay of a decoded record validates it first against its schema (R02)."""
    data = record_of()
    data["surprise"] = 1
    with pytest.raises(RecordError, match="unknown field"):
        replay_record(data)


# --- a record of an older schema version ----------------------------------------------------------------------------

V1 = SchemaVersion("expcore-record/1")
V2 = SchemaVersion(
    "expcore-record/2",
    added={
        "record": ("instruments", "treatment_targets", "instrument_refusal"),
        "turn": ("request_sha256",),
        "tool_call": ("detail", "elapsed_s"),
        "usage": ("extra", "extra_cut"),  # the real table's version 3 fields, here added with this version
        "outcome": ("infra_target",),
        "totals": ("reserved_capped", "reply_cap_unheld"),
    },
    config_before={"cleanup_deadline_s": 7.0},
    optional={
        "usage": ("extra", "extra_cut"),
        "record": ("treatment_targets", "instrument_refusal"),
        "outcome": ("infra_target",),
        "totals": ("reserved_capped", "reply_cap_unheld"),
        "tool_call": ("elapsed_s",),
    },
)
TABLE = (V1, V2)


def older(data: dict) -> dict:
    """Make the JSON of a record look as one written before version 2: without the fields that version added, and
    without the cleanup-deadline setting, which the loop then read as 7 seconds."""
    data["schema_version"] = "expcore-record/1"
    del data["instruments"]
    for entry in data["transcript"]:
        del entry["request_sha256"]
    for entry in data["tool_calls"]:
        del entry["detail"]
        entry.pop("elapsed_s", None)  # a field the current writer adds, and this table's version 2 lists
    del data["agent_config"]["cleanup_deadline_s"]
    data["agent_config_sha256"] = digest(data["agent_config"])
    return data


def test_R03_a_record_of_an_older_schema_replays_with_the_defaults_then_in_force():
    """R03: the fields version 2 added are excused, and a setting version 2 added is read as it was before."""
    old = RunRecord.from_json(older(record_of("malformed")), TABLE)
    assert "cleanup_deadline_s" not in old.agent_config
    replayed = replay_record(old, schemas=TABLE)
    assert replayed.outcome["name"] == "finished"
    assert replayed.agent_config["cleanup_deadline_s"] == 7.0


def test_R03_an_older_record_still_checks_the_digests_it_has():
    """R03: a version that predates the request digest skips it; the settings digest it does have still holds."""
    data = older(record_of("malformed"))
    data["route"]["settings"][data["transcript"][0]["settings_sha256"]] = {"temperature": 1.0}
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data, TABLE), schemas=TABLE)
    assert caught.value.path == "transcript[0].settings_sha256"


def test_R01_the_older_record_is_written_back_to_its_own_bytes():
    """R01: the fields a later schema added are not written into it, and it reads back to the same bytes."""
    old = RunRecord.from_json(older(record_of("tools")), TABLE)
    text = old.dumps(TABLE)
    assert "request_sha256" not in text
    assert RunRecord.from_json(decode(text), TABLE).dumps(TABLE) == text
