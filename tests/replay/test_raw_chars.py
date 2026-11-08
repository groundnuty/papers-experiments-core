"""The length a tool call records for its raw result lets replay check a cut it cannot recompute (R03, R12)."""

import copy

import pytest
from record_support import record_run

from expcore.loop.capping import cap_text
from expcore.records.record import RunRecord
from expcore.records.redact import REDACTED, register_secret
from expcore.replay.divergence import ReplayDivergence
from expcore.replay.rendering import shown_follows_raw
from expcore.replay.replay import replay_record

SECRET = "abcd-secret-value-0001"
CAP = 200
ECHO = [[("echo", {"x": "1"})], "done"]
NOTICE_OF_531 = "[output truncated: 400 of 531 characters dropped by the harness cap]"


def cut_inside_secret():
    """The record of a result that the cap cut right after four characters of a registered secret: x, abcd, then Y."""
    register_secret(SECRET)
    result = "x" * 127 + "abcd" + "Y" * 400
    assert cap_text(result, CAP)[0].split("\n[output truncated")[0] == "x" * 127 + "abcd"
    with pytest.warns(RuntimeWarning):
        return record_run(ECHO, {"echo": lambda args: result}, user="say abcd now")


def capped_without_secret():
    """The record of a long result with no secret anywhere: the call is verified, and the cap is recomputed."""
    return record_run(ECHO, {"echo": lambda args: "q" * 531})


def key_in_the_result(monkeypatch):
    """The record of a short result that held a secret: not truncated, and its raw text is redacted."""
    monkeypatch.setenv("RUN_API_KEY", "sk-live-0123456789abcdefghij")
    with pytest.warns(RuntimeWarning):
        return record_run(ECHO, {"echo": lambda args: "the key is sk-live-0123456789abcdefghij"})


def edited(record, **changes):
    """The JSON of a record with fields of its first tool call replaced; `redacted` adds paths to redacted_paths."""
    data = copy.deepcopy(record.to_json())
    data["redacted_paths"] = sorted({*data["redacted_paths"], *changes.pop("redacted", ())})
    data["tool_calls"][0].update(changes)
    return data


def diverges(data, path, match=""):
    """Replay altered JSON; the divergence must be at `path`, name the call and mention `match`."""
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert caught.value.path == path
    assert "call_1_0" in str(caught.value)
    assert match in str(caught.value)


def test_R03_every_kind_of_call_records_the_length_of_its_raw_result():
    """R03: the field is set where the cap runs: results, errors, a skipped call, a cut result and an empty one."""
    handlers = {"echo": lambda args: "q" * 531, "fail": lambda args: "", "submit": lambda args: "sent"}
    script = [[("echo", {"x": "1"}), ("fail", {}), ("missing", {}), ("submit", {}), ("echo", {"x": "2"})], "done"]
    record = record_run(script, handlers, terminal=("submit",))
    assert {c["status"] for c in record.tool_calls} == {"ok", "error", "skipped"}
    assert [c["truncated"] for c in record.tool_calls].count(True) == 1
    assert all(c["raw_chars"] == len(c["raw_text"]) for c in record.tool_calls)
    assert [c["raw_chars"] for c in record.tool_calls][:2] == [531, 0]


def test_R03_a_secret_cut_off_by_the_cap_keeps_the_length_of_the_whole_result():
    """R03: the length is that of the result before redaction, though the shown text holds a marker in its place."""
    call = cut_inside_secret().tool_calls[0]
    assert call["raw_chars"] == 531
    assert call["text"] == "x" * 127 + REDACTED + "\n" + NOTICE_OF_531
    assert call["rendering_verified"] is False


def test_R03_a_genuine_cut_inside_a_secret_still_replays():
    """R03: the marker at the cut point, the notice for 531 characters and the full raw text all agree."""
    record = cut_inside_secret()
    assert replay_record(record).tool_calls == record.tool_calls


def test_R03_a_faithful_capped_call_without_a_secret_still_replays():
    """R03: a verified call is re-rendered and its recorded length is compared like any other field."""
    record = capped_without_secret()
    call = record.tool_calls[0]
    assert (call["truncated"], call["rendering_verified"], call["raw_chars"]) == (True, True, 531)
    assert replay_record(record).tool_calls == record.tool_calls


def test_R03_a_cut_whose_raw_text_was_shortened_to_the_kept_part_diverges():
    """R03: the case of the review; a raw text cut to the x repeated shows no cut inside a secret."""
    record = cut_inside_secret()
    diverges(edited(record, raw_text="x" * 127), "tool_calls[0].raw_chars", "531")


def test_R03_a_raw_text_that_redaction_did_not_touch_must_have_the_recorded_length():
    """R03: (b) a longer or shorter raw text, and a changed length, of a verified call and of a marked one."""
    plain, marked = capped_without_secret(), cut_inside_secret()
    diverges(edited(plain, raw_chars=530), "tool_calls[0].raw_chars", "530")
    diverges(edited(plain, raw_text="q" * 530), "tool_calls[0].raw_chars", "530")
    diverges(edited(marked, raw_chars=532), "tool_calls[0].raw_chars", "532")


def test_R03_the_length_of_a_raw_text_that_redaction_changed_is_not_compared_with_it(monkeypatch):
    """R03: (b) applies only to untouched raw text; a secret and its marker differ in length."""
    record = key_in_the_result(monkeypatch)
    assert "tool_calls[0].raw_text" in record.redacted_paths
    assert record.tool_calls[0]["raw_chars"] == len("the key is sk-live-0123456789abcdefghij")
    assert len(record.tool_calls[0]["raw_text"]) != record.tool_calls[0]["raw_chars"]
    assert replay_record(record).tool_calls == record.tool_calls


@pytest.mark.parametrize("record_of", [cut_inside_secret, capped_without_secret])
def test_R03_the_notice_must_state_the_length_and_the_cut_that_the_record_gives(record_of):
    """R03: (a) a notice with another count of characters dropped, or another total, diverges at the shown text."""
    record = record_of()
    text = record.tool_calls[0]["text"]
    assert NOTICE_OF_531 in text
    diverges(edited(record, text=text.replace("400 of", "399 of")), "tool_calls[0].text", "notice")
    diverges(edited(record, text=text.replace("of 531", "of 532")), "tool_calls[0].text", "notice")
    diverges(edited(record, text=text.replace("\n" + NOTICE_OF_531, "")), "tool_calls[0].text", "notice")


def test_R03_a_length_that_gives_another_notice_than_the_shown_one_diverges_at_the_shown_text():
    """R03: (a) with the raw text listed as redacted, so only the notice can show that the length was changed."""
    record = cut_inside_secret()
    diverges(edited(record, raw_chars=540, redacted=["tool_calls[0].raw_text"]), "tool_calls[0].text", "540")


def test_R03_a_cut_call_must_have_a_raw_result_longer_than_the_cap():
    """R03: (a) a call marked as cut whose recorded length fits the cap was never cut."""
    record = cut_inside_secret()
    diverges(edited(record, raw_chars=CAP, redacted=["tool_calls[0].raw_text"]), "tool_calls[0].raw_chars", "fits")


def test_R03_a_trailing_marker_must_stand_for_at_least_four_characters_of_the_raw_text():
    """R03: (c) with the length check out of the way, a raw text that ends where the marker begins is no cut."""
    record = cut_inside_secret()
    listed = ["tool_calls[0].raw_text"]
    diverges(edited(record, raw_text="x" * 127, redacted=listed), "tool_calls[0].text", "cannot follow")
    diverges(edited(record, raw_text="x" * 127 + "abc", redacted=listed), "tool_calls[0].text", "cannot follow")
    assert replay_record(RunRecord.from_json(edited(record, raw_text="x" * 127 + "abcd", redacted=listed)))


@pytest.mark.parametrize(
    ("raw", "follows"),
    [("ab", False), ("abXYZ", False), ("abXYZW", True), ("abXYZW and more", True), ("zzzzzzzz", False)],
)
def test_R03_a_marker_at_the_cut_needs_four_characters_of_raw_text_past_what_precedes_it(raw, follows):
    """R03: (c) the marker stands for four or more characters, so the raw text runs at least that far."""
    notice = "\n[output truncated: 5 of 9 characters dropped by the harness cap]"
    assert shown_follows_raw("ab" + REDACTED + notice, raw, True) is follows


def test_R03_a_call_not_marked_as_cut_must_have_a_raw_result_that_fits_the_cap(monkeypatch):
    """R03: (d) a recorded length above the cap contradicts a shown text that is whole; the cap itself is allowed."""
    record = key_in_the_result(monkeypatch)
    assert record.tool_calls[0]["truncated"] is False
    diverges(edited(record, raw_chars=CAP + 1), "tool_calls[0].raw_chars", "exceeds")
    assert replay_record(RunRecord.from_json(edited(record, raw_chars=CAP)))


def test_R03_a_negative_length_is_not_a_record(monkeypatch):
    """R03: a length is never below zero, whatever redaction hides."""
    record = key_in_the_result(monkeypatch)
    with pytest.raises(ValueError, match=r"tool_calls\[0\].raw_chars"):
        RunRecord.from_json(edited(record, raw_chars=-1))
