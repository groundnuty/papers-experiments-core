"""Secret redaction (R12): environment-named secrets never reach a record; numbers stay; a warning is raised."""

import json
import warnings

import pytest
from record_support import hand_made

from expcore.records.record import RunRecord, decode
from expcore.records.redact import REDACTED, redact_structure, redact_text, secret_names, secret_values

KEY = "sk-live-0123456789abcdefghij"
ENV = {"MODEL_API_KEY": KEY, "SERVICE_TOKEN": "tok_ABCDEFGH12345678", "HOME": "/home/someone/long-path-here"}


def test_R12_only_variables_named_like_secrets_with_long_values_count():
    """R12: KEY, TOKEN, SECRET and PASSWORD in the name, at least 16 characters, not only digits."""
    env = {
        "MODEL_API_KEY": KEY,
        "db_password": "correct horse battery",
        "SOME_SECRET": "short",
        "COUNT_TOKEN": "1234567890123456789",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/very/long/path",
        "EMPTY_KEY": "",
    }
    assert secret_names(env) == ["MODEL_API_KEY", "db_password"]
    assert secret_values(env) == [KEY, "correct horse battery"]  # longest first


def test_R12_secrets_are_replaced_in_strings_and_in_keys_and_nowhere_else():
    """R12: values and keys are redacted; numbers, booleans and null are never touched."""
    data = {f"header {KEY}": [f"Bearer {KEY}", 42, 3.5, True, None, {"nested": f"{KEY}{KEY}"}]}
    out, count = redact_structure(data, ENV)
    assert out == {
        f"header {REDACTED}": [f"Bearer {REDACTED}", 42, 3.5, True, None, {"nested": f"{REDACTED}{REDACTED}"}]
    }
    assert count == 4
    assert KEY not in json.dumps(out)


def test_R12_redacting_twice_changes_nothing():
    """R12: redaction is a fixed point, which keeps write-back byte for byte."""
    once, _ = redact_structure({"a": f"x {KEY} y"}, ENV)
    twice, count = redact_structure(once, ENV)
    assert twice == once
    assert count == 0


def test_two_keys_that_collide_once_redacted_are_refused_not_merged():
    other = "sk-live-9999999999999999999"
    with pytest.raises(ValueError, match="two keys are the same"):
        redact_structure({KEY: 1, other: 2}, {"A_KEY": KEY, "B_KEY": other})


def test_redact_text_handles_the_json_escaped_form():
    escaped = json.dumps('tok"en-with-quote-0123456789')[1:-1]
    env = {"X_TOKEN": 'tok"en-with-quote-0123456789'}
    text, count = redact_text(f"saw {escaped} and {env['X_TOKEN']}", env)
    assert count == 2
    assert escaped not in text


def test_R12_a_planted_key_is_absent_from_the_written_record_and_a_warning_is_raised(tmp_path):
    """R12: a planted key value is absent from the written record, and the writer warns without showing the value."""
    record = hand_made(
        prompt={**hand_made().prompt, "user": {**hand_made().prompt["user"], "text": f"the key is {KEY}"}},
        totals={**hand_made().totals, "final_text": f"echo {KEY}"},
    )
    with pytest.warns(RuntimeWarning, match=r"redacted 2 secret value") as caught:
        path = record.write(tmp_path, environ=ENV)
    assert KEY not in path.read_text(encoding="utf-8")
    assert all(KEY not in str(w.message) for w in caught)
    assert REDACTED in path.read_text(encoding="utf-8")


def test_a_record_without_a_secret_writes_without_a_warning(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        text = hand_made().dumps(environ=ENV)
    assert RunRecord.from_json(decode(text)).dumps(environ=ENV) == text


def test_R12_the_default_environment_is_the_processs_own(monkeypatch):
    """R12: with no environment given, the process's variables are read at write time."""
    monkeypatch.setenv("FRESH_API_KEY", "fresh-value-1234567890-abc")
    record = hand_made(totals={**hand_made().totals, "final_text": "fresh-value-1234567890-abc"})
    with pytest.warns(RuntimeWarning, match="redacted 1"):
        text = record.dumps()
    assert "fresh-value-1234567890-abc" not in text
