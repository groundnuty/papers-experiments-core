"""The served-model check (B06): ids, echoed settings, silent server defaults, fingerprints, dated snapshots."""

from types import SimpleNamespace

import pytest

from expcore.models.served import (
    MIXED_FINGERPRINTS,
    NOT_REPORTED,
    SETTINGS,
    Baseline,
    Served,
    is_mixed,
    judge,
    requested_settings,
    same_model,
)

REQUESTED = {"temperature": 0.2}


def reply(model="test-model", fingerprint=None, **extra):
    return SimpleNamespace(model=model, system_fingerprint=fingerprint, model_extra=extra)


def served_after(*replies):
    served = Served()
    for item in replies:
        served.note(item)
    return served


def judged(served, expected="test-model", requested=REQUESTED, **kwargs):
    info = served.info()
    return judge(
        info["served_model_ids"],
        info["served_settings"],
        expected,
        requested,
        served_fingerprints=info["system_fingerprints"],
        **kwargs,
    )


def test_b06_the_expected_id_with_nothing_else_reported_is_a_match():
    """B06: The expected id with nothing else reported is a match."""
    result = judged(served_after(reply()))
    assert result["state"] == "match"
    assert result["reasons"] == []
    assert result["not_reported"] == list(SETTINGS)


def test_b06_another_model_id_is_a_mismatch():
    """B06: Another model id is a mismatch."""
    result = judged(served_after(reply(model="other-model")))
    assert result["state"] == "mismatch"
    assert "'other-model'" in result["reasons"][0]
    assert "'test-model'" in result["reasons"][0]


def test_b06_one_reply_of_another_id_among_good_ones_is_a_mismatch():
    """B06: One reply of another id among good ones is a mismatch."""
    assert judged(served_after(reply(), reply(model="other-model"), reply()))["state"] == "mismatch"


def test_b06_a_setting_that_differs_from_the_request_is_a_mismatch():
    """B06: A setting that differs from the request is a mismatch."""
    result = judged(served_after(reply(temperature=0.7)))
    assert result["state"] == "mismatch"
    assert result["reasons"] == ["the server reports temperature=0.7, requested 0.2"]


def test_b06_a_setting_equal_to_the_request_is_a_match_even_as_a_string_or_integer():
    """B06: A setting equal to the request is a match even as a string or integer."""
    assert judged(served_after(reply(temperature=0.2)))["state"] == "match"
    assert judged(served_after(reply(temperature="0.2")))["state"] == "match"
    assert judged(served_after(reply(top_p=1)), requested={"top_p": 1.0})["state"] == "match"


def test_b06_a_setting_that_was_never_requested_is_a_silent_server_default_and_a_mismatch():
    """B06: A setting that was never requested is a silent server default and a mismatch."""
    result = judged(served_after(reply(reasoning_effort="high")))
    assert result["state"] == "mismatch"
    assert result["reasons"] == ["the server reports reasoning_effort='high' but none was requested (a server default)"]


def test_b06_replies_that_disagree_about_a_setting_are_a_mismatch():
    """B06: Replies that disagree about a setting are a mismatch."""
    result = judged(served_after(reply(temperature=0.2), reply(temperature=0.9)))
    assert result["state"] == "mismatch"
    assert any("different temperature in different replies" in r for r in result["reasons"])


def test_b06_a_reply_with_no_id_is_unreported_and_stops_nothing():
    """B06: A reply with no id is unreported and stops nothing."""
    result = judged(served_after(reply(model=None)))
    assert result["state"] == "unreported"
    assert result["reasons"] == ["the server reported no model id"]


def test_b06_a_wrong_setting_stops_the_model_even_when_no_id_came_back():
    """B06: A wrong setting stops the model even when no id came back."""
    assert judged(served_after(reply(model=None, temperature=0.9)))["state"] == "mismatch"


def test_b06_a_missing_setting_is_recorded_as_not_reported_never_as_the_requested_value():
    """B06: A missing setting is recorded as not reported never as the requested value."""
    info = served_after(reply()).info()
    assert info["served_settings"]["temperature"] == NOT_REPORTED
    assert all(value == NOT_REPORTED for value in info["served_settings"].values())
    assert info["served_model_ids"] == ["test-model"]


def test_b06_a_reported_setting_is_recorded_and_a_varying_one_lists_every_value():
    """B06: A reported setting is recorded and a varying one lists every value."""
    assert served_after(reply(temperature=0.2)).info()["served_settings"]["temperature"] == 0.2
    varied = served_after(reply(temperature=0.2), reply(temperature=0.9)).info()["served_settings"]["temperature"]
    assert varied == {"varied": [0.2, 0.9]}


def test_b06_thinking_is_counted_per_reply_and_its_tokens_stay_none_unless_reported():
    """B06: Thinking is counted per reply and its tokens stay none unless reported."""
    served = Served()
    served.note_thinking(True)
    served.note_thinking(False)
    thinking = served.info()["thinking"]
    assert (thinking["calls"], thinking["calls_with_thinking"], thinking["thinking_tokens"]) == (2, 1, None)
    served.note_thinking(True, 40)
    served.note_thinking(True, 2)
    assert served.info()["thinking"]["thinking_tokens"] == 42
    assert served.info()["reasoning_content_seen"] is True


def test_b06_a_dated_snapshot_of_the_expected_id_is_the_same_model():
    """B06: A dated snapshot of the expected id is the same model."""
    assert judged(served_after(reply(model="test-model-20261001")))["state"] == "match"
    assert same_model("test-model", "test-model-20261001")
    assert same_model("test-model", "test-model")


@pytest.mark.parametrize("served", ["test-model-2026", "test-model-v2", "test-model-20261001-x", "xtest-model", "test"])
def test_b06_a_lookalike_id_is_not_a_dated_snapshot(served):
    """B06: A lookalike id is not a dated snapshot."""
    assert not same_model("test-model", served)
    assert judged(served_after(reply(model=served)))["state"] == "mismatch"


def test_b06_an_alias_the_table_lists_is_accepted_and_so_is_its_dated_snapshot():
    """B06: An alias the table lists is accepted and so is its dated snapshot."""
    assert judged(served_after(reply(model="Test Model")), accepted_ids=["Test Model"])["state"] == "match"
    assert judged(served_after(reply(model="alias-20260102")), accepted_ids=["alias"])["state"] == "match"


def test_b06_a_reply_with_several_fingerprints_is_a_change_by_itself():
    """B06: A reply with several fingerprints is a change by itself."""
    mixed = MIXED_FINGERPRINTS.join(["fp-a", "fp-b"])
    assert is_mixed(mixed)
    assert not is_mixed("fp-a")
    assert not is_mixed(None)
    result = judged(served_after(reply(fingerprint=mixed)))
    assert result["state"] == "mismatch"
    assert result["reasons"] == ["a reply carried several system fingerprints"]


def test_b06_a_reply_given_several_fingerprints_as_a_list_is_mixed_too():
    """B06: A reply given several fingerprints as a list is mixed too."""
    served = served_after(reply(fingerprint=["fp-a", "fp-b"]))
    assert is_mixed(served.fingerprints[0])
    assert judged(served)["state"] == "mismatch"
    assert served_after(reply(fingerprint=["fp-a", "fp-a"])).fingerprints == ["fp-a"]


def test_b06_fingerprints_that_change_between_replies_are_a_mismatch():
    """B06: Fingerprints that change between replies are a mismatch."""
    result = judged(served_after(reply(fingerprint="fp-a"), reply(fingerprint="fp-b")))
    assert result["state"] == "mismatch"
    assert "different system fingerprints in different replies" in result["reasons"][0]


def test_b06_a_fingerprint_other_than_the_baseline_is_a_mismatch():
    """B06: A fingerprint other than the baseline is a mismatch."""
    served = served_after(reply(fingerprint="fp-b"))
    assert judged(served, expected_fingerprint="fp-b")["state"] == "match"
    result = judged(served, expected_fingerprint="fp-a")
    assert result["state"] == "mismatch"
    assert "'fp-b' is not the baseline 'fp-a'" in result["reasons"][0]


def test_b06_the_same_fingerprint_on_every_reply_and_none_at_all_are_no_change():
    """B06: The same fingerprint on every reply and none at all are no change."""
    assert judged(served_after(reply(fingerprint="fp-a"), reply(fingerprint="fp-a")))["state"] == "match"
    assert judged(served_after(reply(), reply()), expected_fingerprint="fp-a")["state"] == "match"


def test_b06_requested_settings_come_from_sampling_extra_body_and_chat_template_kwargs():
    """B06: Requested settings come from sampling extra body and chat template kwargs."""
    assert requested_settings(
        {"temperature": 0.2, "seed": 1},
        {"thinking": {"type": "enabled"}, "chat_template_kwargs": {"enable_thinking": True}},
    ) == {"temperature": 0.2, "thinking": {"type": "enabled"}, "enable_thinking": True}
    assert requested_settings(None, None) == {}


def test_b06_baseline_the_first_reply_sets_the_id_and_the_fingerprint_and_passes():
    """B06: Baseline the first reply sets the id and the fingerprint and passes."""
    baseline = Baseline()
    assert baseline.check("test-model", "fp-a") is None
    assert baseline.to_dict() == {"served_model": "test-model", "system_fingerprint": "fp-a"}
    assert baseline.check("test-model", "fp-a") is None


def test_b06_baseline_another_id_or_fingerprint_later_is_a_change():
    """B06: Baseline another id or fingerprint later is a change."""
    baseline = Baseline("test-model", "fp-a")
    assert baseline.check("other-model", "fp-a") == {"first_served_model": "test-model", "served_model": "other-model"}
    assert baseline.check("test-model", "fp-b") == {"first_system_fingerprint": "fp-a", "system_fingerprint": "fp-b"}


def test_b06_baseline_a_dated_snapshot_of_the_first_id_is_the_same_model():
    """B06: Baseline a dated snapshot of the first id is the same model."""
    assert Baseline("test-model", None).check("test-model-20261001", None) is None


def test_b06_baseline_a_mixed_fingerprint_is_a_change_and_never_a_baseline():
    """B06: Baseline a mixed fingerprint is a change and never a baseline."""
    mixed = MIXED_FINGERPRINTS.join(["fp-a", "fp-b"])
    fresh = Baseline()
    assert fresh.check("test-model", mixed) == {"first_system_fingerprint": None, "system_fingerprint": mixed}
    assert fresh.to_dict()["system_fingerprint"] is None
    assert fresh.check("test-model", "fp-a") is None  # the first single fingerprint is the baseline
    assert fresh.check("test-model", mixed) is not None


def test_b06_baseline_a_reply_without_a_fingerprint_is_no_change_and_sets_nothing():
    """B06: Baseline a reply without a fingerprint is no change and sets nothing."""
    baseline = Baseline()
    assert baseline.check("test-model", None) is None
    assert baseline.to_dict() == {"served_model": "test-model", "system_fingerprint": None}
    assert baseline.check(None, None) is None


def test_b06_a_baseline_survives_a_round_trip_through_its_dict():
    """B06: A baseline survives a round trip through its dict."""
    restored = Baseline.from_dict(Baseline("test-model", "fp-a").to_dict())
    assert restored.check("test-model", "fp-b") is not None
