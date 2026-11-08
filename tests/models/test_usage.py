"""Usage is counted on arrival and never invented: unknown is None, not 0 (B10)."""

from types import SimpleNamespace

import pytest

from expcore.models.usage import UsageTally, token_count


def test_b10_a_tally_starts_unknown_not_zero():
    """B10: before any reply reports a count, the totals are None."""
    tally = UsageTally()
    assert tally.as_dict() == {"responses": 0, "input": None, "output": None, "missing": 0}


def test_b10_counts_add_up_and_a_reply_without_usage_is_counted_missing_not_zero():
    """B10: Counts add up and a reply without usage is counted missing not zero."""
    tally = UsageTally()
    tally.note(100, 20)
    tally.note(None, None)
    tally.note(50, None)
    assert tally.as_dict() == {"responses": 3, "input": 150, "output": 20, "missing": 1}


def test_b10_a_tally_that_only_saw_missing_replies_stays_none():
    """B10: A tally that only saw missing replies stays none."""
    tally = UsageTally()
    tally.note(None, None)
    assert tally.as_dict() == {"responses": 1, "input": None, "output": None, "missing": 1}


@pytest.mark.parametrize("value", [None, "12", True, -1, 1.5])
def test_b10_a_count_that_is_not_a_non_negative_integer_reads_as_unknown(value):
    """B10: A count that is not a non negative integer reads as unknown."""
    assert token_count(SimpleNamespace(prompt_tokens=value), "prompt_tokens") is None


def test_b10_a_zero_the_server_reported_is_kept_as_zero():
    """B10: A zero the server reported is kept as zero."""
    assert token_count(SimpleNamespace(completion_tokens=0), "completion_tokens") == 0


def test_b10_a_missing_attribute_reads_as_unknown():
    """B10: A missing attribute reads as unknown."""
    assert token_count(SimpleNamespace(), "prompt_tokens") is None
    assert token_count(None, "prompt_tokens") is None
