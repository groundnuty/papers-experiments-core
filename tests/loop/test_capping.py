"""The character cap with its notice inside, the error cap with the class first, and the sentence that pins the cap."""

import pytest

from expcore.loop.capping import TRUNCATION_NOTICE, cap_error, cap_text, describe_cap, render_error


def test_l06_text_within_the_cap_is_delivered_whole():
    """L06: a result that fits is not touched and not flagged."""
    assert cap_text("short", 5) == ("short", False)
    assert cap_text("", 0) == ("", False)


@pytest.mark.parametrize(
    ("total", "cap"),
    [(101, 100), (1000, 100), (10_000, 8000), (99_999, 8000), (1_000_000, 8000), (8001, 8000), (200, 90), (200, 85)],
)
def test_l06_a_long_output_ends_with_the_notice_and_stays_within_the_cap(total, cap):
    """L06: the notice counts inside the cap, so the delivered text is never longer than the cap."""
    text = "x" * total
    delivered, truncated = cap_text(text, cap)
    assert truncated is True
    assert len(delivered) <= cap
    kept = delivered.index("\n[output truncated")
    assert delivered.endswith(TRUNCATION_NOTICE.format(dropped=total - kept, total=total))
    assert delivered[:kept] == text[:kept]


def test_l06_the_cut_uses_all_the_room_the_notice_leaves():
    """L06: no character is dropped that would have fitted."""
    text = "y" * 5000
    delivered, _ = cap_text(text, 1000)
    assert len(delivered) in range(990, 1001)


def test_l06_a_cap_smaller_than_the_notice_is_a_plain_cut():
    """L06: when the notice cannot fit, the text is cut at the cap and still flagged."""
    delivered, truncated = cap_text("z" * 500, 20)
    assert (delivered, truncated) == ("z" * 20, True)


def test_l06_an_error_is_capped_with_its_class_first():
    """L06: a long error keeps its class at the start of what the model sees, within the cap, and is flagged."""
    message = "disk full " * 500
    delivered, truncated = cap_error("ToolError", message, 300)
    assert truncated is True
    assert len(delivered) <= 300
    assert delivered.startswith("ToolError: disk full")
    assert "characters dropped by the harness cap" in delivered


def test_l06_a_short_error_is_not_cut():
    """L06: an error that fits is delivered as class and message."""
    assert cap_error("InvalidArguments", "needs a string", 300) == ("InvalidArguments: needs a string", False)
    assert render_error("Empty", "") == "Empty"


def test_l06_the_description_sentence_states_the_cap_and_changes_with_it():
    """L06: two caps give two sentences, so two tool manifests, and the notice wording is pinned too."""
    first, second = describe_cap(8000), describe_cap(4000)
    assert "8000 characters" in first
    assert first != second
    assert "characters dropped by the harness cap" in first
