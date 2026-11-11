"""Usage is counted on arrival and never invented: unknown is None, not 0 (B10)."""

from types import SimpleNamespace

import pytest

from expcore.models import usage as usage_rules
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


# --- the provider's other numeric usage fields, recorded only (B10) ---------------------------------------------


def test_b10_the_extra_fields_of_a_mapping_a_model_and_an_object_read_the_same():
    """B10: a usage given as a mapping, as an SDK model (its dump) or as a plain object yields the same fields."""
    fields = {"prompt_tokens": 5, "used_credits": 1.5, "details": {"reasoning_tokens": 3}}
    expected = ({"details.reasoning_tokens": 3, "used_credits": 1.5}, 0)

    class Dumpable:
        def model_dump(self):
            return fields

    plain_object = SimpleNamespace(prompt_tokens=5, used_credits=1.5, details={"reasoning_tokens": 3})
    for shape in (fields, Dumpable(), plain_object):
        assert usage_rules.extra_usage(shape, skip=("prompt_tokens",)) == expected


def test_b10_no_usage_or_one_that_holds_nothing_else_gives_no_fields_and_none_cut():
    """B10: a reply with no usage object, and one with only the counts the turn holds, have an empty mapping."""
    assert usage_rules.extra_usage(None) == ({}, 0)
    assert usage_rules.extra_usage(SimpleNamespace(prompt_tokens=5), skip=("prompt_tokens",)) == ({}, 0)
    assert usage_rules.extra_usage(42) == ({}, 0)


@pytest.mark.parametrize(
    "name",
    ["", "has space", "1leading", "x" * 200, "new\nline", "a..b", "dotted.", "café"],
    ids=["empty", "space", "digit_first", "too_long", "newline", "double_dot", "trailing_dot", "non_ascii"],
)
def test_b10_a_field_whose_name_is_not_a_plain_identifier_is_left_out_not_counted_as_cut(name):
    """B10: a hostile or odd name cannot reach the record, and is not a field the cap cut."""
    assert usage_rules.extra_usage({name: 3, "used_credits": 1}) == ({"used_credits": 1}, 0)


def test_b10_an_integer_too_large_to_be_a_count_is_left_out_and_a_zero_is_kept():
    """B10: a number past 64 bits is not a usage field; a zero the provider reported is a field, not an absence."""
    assert usage_rules.extra_usage({"huge": 10**30, "none_used": 0, "small": -2}) == ({"none_used": 0, "small": -2}, 0)


def test_b10_the_cap_keeps_the_first_names_in_order_and_counts_the_rest():
    """B10: the kept names are the same whatever the order the reply listed them in."""
    cap = usage_rules.USAGE_EXTRA_CAP
    names = [f"f{i:04d}" for i in range(cap + 3)]
    forward, cut = usage_rules.extra_usage(dict.fromkeys(names, 1))
    backward, cut_back = usage_rules.extra_usage(dict.fromkeys(reversed(names), 1))
    assert list(forward) == names[:cap] == list(backward)
    assert (cut, cut_back) == (3, 3)


def test_b10_nested_fields_count_toward_the_cap_too():
    """B10: a nested object cannot be a way round the cap."""
    cap = usage_rules.USAGE_EXTRA_CAP
    nested = {"group": {f"n{i:03d}": i for i in range(cap + 10)}}
    kept, cut = usage_rules.extra_usage(nested)
    assert (len(kept), cut) == (cap, 10)
