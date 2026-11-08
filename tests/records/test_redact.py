"""Secret redaction (R12): what is a secret, every form it can take, and that the written record holds none."""

import json
import warnings
from urllib.parse import quote

import pytest
from record_support import hand_made

from expcore.records.record import RunRecord, decode
from expcore.records.redact import (
    REDACTED,
    Redactor,
    forget_secrets,
    is_secret_key,
    redact_structure,
    redact_text,
    register_secret,
    secret_names,
    secret_values,
)
from expcore.records.schema import RecordError

KEY = "sk-live-0123456789abcdefghij"
ENV = {"MODEL_API_KEY": KEY, "SERVICE_TOKEN": "tok_ABCDEFGH12345678", "HOME": "/home/someone/long-path-here"}


def test_R12_a_variable_is_a_secret_by_its_name_whatever_its_value_looks_like():
    """R12: KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL and AUTH in the name; no filter on the value."""
    env = {
        "MODEL_API_KEY": KEY,
        "db_password": "correct horse battery",
        "SOME_SECRET": "short",
        "COUNT_TOKEN": "1234567890123456789",
        "DB_PASSWD": "pw",
        "CLOUD_CREDENTIAL": "[abc]",
        "SSH_AUTH_SOCK": "/run/agent.1",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/very/long/path",
        "EMPTY_KEY": "",
        "BLANK_KEY": "   ",
    }
    assert secret_names(env) == [
        "CLOUD_CREDENTIAL",
        "COUNT_TOKEN",
        "DB_PASSWD",
        "MODEL_API_KEY",
        "SOME_SECRET",
        "SSH_AUTH_SOCK",
        "db_password",
    ]
    assert secret_values(env)[0] == KEY  # longest first


def test_R12_a_long_value_is_replaced_inside_strings_and_keys_and_a_short_one_only_as_a_whole_string():
    """R12: 8 or more characters are replaced as substrings; fewer only where the whole string equals the value."""
    env = {"A_PASSWORD": "hunter2", "B_KEY": "ab[cd]efghij", "C_TOKEN": "98765432101"}
    data = {
        "note": "the password is hunter2 and the key is ab[cd]efghij",
        "whole": "hunter2",
        "digits": "id 98765432101 end",
        "ab[cd]efghij": ["hunter2", "x hunter2"],
    }
    out, paths = redact_structure(data, env)
    assert out == {
        "note": "the password is hunter2 and the key is [redacted]",
        "whole": REDACTED,
        "digits": f"id {REDACTED} end",
        REDACTED: [REDACTED, "x hunter2"],
    }
    assert paths == ["digits", "note", "whole", "{key 3}", "{key 3}[0]"]


def test_R12_the_forms_codex_listed_a_short_password_all_digits_brackets_a_number_and_a_minted_token():
    """R12: each form that used to survive is now gone from the written structure."""
    forget_secrets()
    register_secret("minted-9f8e7d6c5b4a")
    env = {"P_PASSWORD": "pw1", "N_KEY": "123456789012", "B_SECRET": "a]b[c]d[e", "I_TOKEN": "4096"}
    data = {
        "short": "pw1",
        "digits": "pin 123456789012",
        "brackets": "x a]b[c]d[e y",
        "number": 4096,
        "big": 123456789012,
        "token": "minted-9f8e7d6c5b4a",
        "arguments": {"token": 12, "headers": {"Authorization": "Bearer minted-9f8e7d6c5b4a"}},
        "ordinary": [1, 2.5, True, None, "text"],
    }
    out, _ = redact_structure(data, env)
    forget_secrets()
    text = json.dumps(out)
    for secret in ("pw1", "123456789012", "a]b[c]d[e", "minted-9f8e7d6c5b4a"):
        assert secret not in text
    assert out["number"] == REDACTED
    assert out["big"] == REDACTED
    assert out["ordinary"] == [1, 2.5, True, None, "text"]


def test_R12_a_minted_token_is_redacted_everywhere_once_registered_and_not_before():
    """R12: a token minted at run time is in no environment variable; registering it is what makes it a secret."""
    forget_secrets()
    token = "minted-0a1b2c3d4e5f"
    data = {"url": f"https://host/path?access={token}", "note": f"bearer {token}"}
    assert redact_structure(data, {})[0] == data
    register_secret(token)
    try:
        out, paths = redact_structure(data, {})
    finally:
        forget_secrets()
    assert token not in json.dumps(out)
    assert paths == ["note", "url"]
    with pytest.raises(ValueError, match="non-empty string"):
        register_secret("")


def test_R12_the_value_of_a_key_that_names_a_secret_is_replaced_whatever_it_is():
    """R12: a credential the process was never told about is caught by the name of the key that holds it."""
    data = {
        "Token": "anything",
        "api_key": 7,
        "x-api-key": None,
        "Authorization": {"type": "bearer", "value": "abc"},
        "client_secret": ["a", "b"],
        "headers": {"Bearer": "xyz", "apikey": "q", "password": ""},
    }
    out, paths = redact_structure(data, {})
    assert all(v == REDACTED for v in (out["Token"], out["api_key"], out["x-api-key"], out["Authorization"]))
    assert out["client_secret"] == REDACTED
    assert out["headers"] == {"Bearer": REDACTED, "apikey": REDACTED, "password": REDACTED}
    assert "headers.password" in paths


def test_R12_keys_that_only_contain_the_word_are_not_secrets():
    """R12: counts and budgets are named with the word and are not credentials."""
    data = {
        "tokens_used": 5,
        "token_budget": 100,
        "input_tokens": 3,
        "max_tokens": 9,
        "secretary": "x",
        "keyboard": "y",
    }
    assert redact_structure(data, {})[0] == data
    assert not is_secret_key("tokens_used")
    assert is_secret_key("X-API-KEY")
    assert is_secret_key("auth_token")
    assert not is_secret_key("author")


def test_R12_a_secret_inside_a_json_string_is_redacted_in_its_escaped_form():
    """R12: a native message holds text inside JSON strings, where a quote in the value is a backslash and a quote."""
    secret = 'sk"live\\0123456789'
    message = {"role": "tool", "content": json.dumps({"echo": secret, "n": 1})}
    assert json.dumps(secret)[1:-1] in message["content"]
    out, _ = redact_structure(message, {"A_KEY": secret})
    assert secret not in out["content"]
    assert json.dumps(secret)[1:-1] not in out["content"]
    assert json.loads(out["content"]) == {"echo": REDACTED, "n": 1}


def test_R12_a_secret_in_a_url_is_redacted_in_its_percent_encoded_form():
    """R12: decoding the redacted text must not recover the value."""
    secret = "sk/live+key 0123456789"
    url = f"https://host/p?k={quote(secret, safe='')}&z=1"
    out, _ = redact_structure({"url": url}, {"A_KEY": secret})
    assert quote(secret, safe="") not in out["url"]
    assert out["url"] == f"https://host/p?k={REDACTED}&z=1"
    from urllib.parse import unquote

    assert secret not in unquote(out["url"])


def test_R12_redacting_twice_changes_nothing():
    """R12: redaction is a fixed point, which keeps write-back byte for byte."""
    data = {"a": f"x {KEY} y", "token": "t", "n": 4096, "b": "a]b[c]d[e"}
    env = {**ENV, "I_TOKEN": "4096", "B_SECRET": "a]b[c]d[e"}
    once, paths = redact_structure(data, env)
    twice, again = redact_structure(once, env)
    assert twice == once
    assert paths
    assert again == []


def test_R12_the_paths_name_the_fields_that_changed_and_never_a_value():
    """R12: a redacted key is named by its place, since its text is the secret."""
    out, paths = redact_structure({KEY: {"k": KEY}, "plain": 1, "list": [KEY]}, ENV)
    assert KEY not in json.dumps(out)
    assert all(KEY not in p for p in paths)
    assert paths == ["list[0]", "{key 0}", "{key 0}.k"]


def test_two_keys_that_collide_once_redacted_are_refused_not_merged():
    other = "sk-live-9999999999999999999"
    with pytest.raises(ValueError, match="two keys are the same"):
        redact_structure({KEY: 1, other: 2}, {"A_KEY": KEY, "B_KEY": other})


def test_redact_text_replaces_the_secret_in_every_form():
    secret = 'tok"en-with-quote-0123456789'
    text, count = redact_text(f"saw {json.dumps(secret)[1:-1]} and {secret}", {"X_TOKEN": secret})
    assert count == 1
    assert secret not in text
    assert json.dumps(secret)[1:-1] not in text


def test_a_redactor_of_nothing_changes_nothing_even_a_key_that_names_a_secret():
    data = {"token": "kept", "text": KEY}
    assert Redactor.none().structure(data) == (data, [])


def test_R12_a_planted_key_is_absent_from_the_written_record_and_a_warning_is_raised(tmp_path):
    """R12: a planted key value is absent from the written record; the writer warns without showing the value."""
    base = hand_made()
    record = hand_made(
        prompt={**base.prompt, "user": {**base.prompt["user"], "text": f"the key is {KEY}"}},
        totals={**base.totals, "final_text": f"echo {KEY}"},
    )
    with pytest.warns(RuntimeWarning, match=r"redacted secrets at 2 place") as caught:
        path = record.write(tmp_path, environ=ENV)
    written = path.read_text(encoding="utf-8")
    assert KEY not in written
    assert all(KEY not in str(w.message) for w in caught)
    assert json.loads(written)["redacted_paths"] == ["prompt.user.text", "totals.final_text"]


def test_R12_a_record_that_lists_where_it_was_redacted_writes_back_to_the_same_bytes(tmp_path):
    """R12 and R01: the listed paths are part of the record, so a second write changes nothing and warns of nothing."""
    base = hand_made()
    record = hand_made(totals={**base.totals, "final_text": f"echo {KEY}"})
    with pytest.warns(RuntimeWarning):
        stored = record.write(tmp_path, environ=ENV).read_bytes()
    loaded = RunRecord.from_json(decode(stored))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert loaded.dumps(environ=ENV).encode("utf-8") == stored


def test_R12_a_secret_that_equals_a_number_the_record_must_hold_refuses_the_write_naming_the_place(tmp_path):
    """R12: replacing a count with text would write a record that cannot be read back; it is refused, not written."""
    base = hand_made()
    record = hand_made(totals={**base.totals, "tokens_used": 4096})
    with pytest.raises(RecordError, match=r"redacting secrets at \[.*'totals\.tokens_used'.*\] leaves a record"):
        record.write(tmp_path, environ={"MAX_TOKEN": "4096"})
    assert not list(tmp_path.iterdir())


def test_a_record_without_a_secret_writes_without_a_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        text = hand_made().dumps(environ=ENV)
    assert RunRecord.from_json(decode(text)).dumps(environ=ENV) == text


def test_R12_the_default_environment_is_the_processs_own(monkeypatch):
    """R12: with no environment given, the process's variables are read at write time."""
    monkeypatch.setenv("FRESH_API_KEY", "fresh-value-1234567890-abc")
    base = hand_made()
    record = hand_made(totals={**base.totals, "final_text": "fresh-value-1234567890-abc"})
    with pytest.warns(RuntimeWarning, match="redacted secrets at 1 place"):
        text = record.dumps()
    assert "fresh-value-1234567890-abc" not in text
