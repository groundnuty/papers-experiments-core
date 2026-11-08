"""Redacting secret-named keys inside JSON text: a scan with its own stack that changes only what it replaces (R12)."""

import pytest

from expcore.records.redact import redact_structure


def deep(levels, leaf):
    """JSON text nested `levels` objects deep, with `leaf` at the bottom."""
    return '{"a":' * levels + leaf + "}" * levels


def test_R12_a_deep_json_string_is_scanned_in_full_and_the_hidden_value_is_redacted():
    """R12: Codex's 65-level input; the key replacement found at the bottom is not dropped for the depth."""
    out, paths = redact_structure({"s": deep(65, '{"token": "hidden"}')}, {})
    assert "hidden" not in out["s"]
    assert out["s"] == deep(65, '{"token": "[redacted]"}')
    assert paths == ["s"]
    kept, _ = redact_structure({"s": deep(65, '{"token": "hidden", "n": 1e400}')}, {})
    assert kept["s"] == deep(65, '{"token": "[redacted]", "n": 1e400}')


def test_R12_a_json_string_nested_far_beyond_the_interpreters_recursion_limit_is_still_scanned():
    """R12: the scan keeps its own stack, so thousands of levels are as good as two."""
    levels = 5000
    out, _ = redact_structure({"s": deep(levels, '{"password": "p", "keep": [1, 2.50]}')}, {})
    assert out["s"] == deep(levels, '{"password": "[redacted]", "keep": [1, 2.50]}')
    arrays = "[" * levels + '{"secret": 1}' + "]" * levels
    assert redact_structure({"s": arrays}, {})[0]["s"] == "[" * levels + '{"secret": "[redacted]"}' + "]" * levels


def test_R12_a_secret_keyed_object_deep_in_a_string_is_replaced_whole_with_what_it_held():
    text = deep(70, '{"authorization": {"scheme": "x", "deeper": {"token": "t"}}, "ok": 1}')
    out, _ = redact_structure({"s": text}, {})
    assert out["s"] == deep(70, '{"authorization": "[redacted]", "ok": 1}')


@pytest.mark.parametrize(
    "text", ["{" * 70, '{"a": [1, 2}', '{"a": 1} {"b": 2}', "[1,]", '{"a" 1}', '{"a": tru}', '{"a": 01}', "[1 2]"]
)
def test_R12_json_text_that_is_not_strict_is_left_to_redaction_by_value(text):
    """R12: nothing is guessed from text that is not strict JSON, however deep."""
    assert redact_structure({"s": text}, {})[0] == {"s": text}
