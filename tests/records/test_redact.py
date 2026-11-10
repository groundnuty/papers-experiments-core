"""Secret redaction (R12): what is a secret, every form it can take, and that the written record holds none."""

import json
import warnings
from urllib.parse import quote, unquote

import pytest
from record_support import clear_registry, hand_made

from expcore.config.loading import ConfigError
from expcore.records.record import RunRecord, decode
from expcore.records.redact import (
    REDACTED,
    SECRET_NAME,
    Redactor,
    forms_of,
    is_secret_key,
    redact_structure,
    redact_text,
    register_secret,
    secret_env_names,
    secret_values,
)
from expcore.records.schema import RecordError

KEY = "sk-live-0123456789abcdefghij"
ENV = {"MODEL_API_KEY": KEY, "SERVICE_TOKEN": "tok_ABCDEFGH12345678", "HOME": "/home/someone/long-path-here"}


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "X_APIKEY",
        "CLIENT_SECRET",
        "SIGNING_SECRET_KEY",
        "DB_PASSWORD",
        "db_passwd",
        "CLOUD_ACCESS_TOKEN",
        "REFRESH_AUTH_TOKEN",
        "GITHUB_TOKEN",
        "service_token",
        "TOKEN",
        "PASSWORD",
        "API_KEY",
    ],
)
def test_R12_a_name_that_ends_as_a_secret_name_does_is_a_secret(name):
    """R12: the suffix pattern, in any case, with or without something before it."""
    assert SECRET_NAME.search(name)


@pytest.mark.parametrize(
    "name", ["MAX_TOKENS", "SSH_AUTH_SOCK", "KEYBOARD_LAYOUT", "TOKEN_BUDGET", "AUTHOR", "PASSWORDS"]
)
def test_R12_a_name_that_only_contains_the_word_is_not_a_secret(name):
    """R12: the suffix must end the name, so counts, sockets and layouts are left alone."""
    assert not SECRET_NAME.search(name)
    assert secret_env_names({name: "some value here"}) == []


def test_R12_the_caller_can_declare_the_variables_that_hold_secrets():
    """R12: a name outside the pattern is a secret when declared, and a variable without a value is nothing."""
    env = {"ODD_NAME": "declared-value-1", "OTHER": "not declared", "EMPTY_API_KEY": "", "BLANK_TOKEN": "  "}
    assert secret_env_names(env) == []
    assert secret_env_names(env, declared=["ODD_NAME", "UNSET"]) == ["ODD_NAME"]
    assert secret_values(env, declared=["ODD_NAME"]) == ["declared-value-1"]


def test_R12_every_value_of_four_or_more_characters_is_redacted_inside_text_whatever_its_length():
    """R12: password=hunter2 loses hunter2, and a four-character value is redacted as a substring too."""
    env = {"A_PASSWORD": "hunter2", "B_SECRET": "ab12"}
    out, paths = redact_structure(
        {"url": "login?user=x&password=hunter2&ok=1", "note": "code ab12cd", "other": "ab1"}, env
    )
    assert out == {"url": f"login?user=x&password={REDACTED}&ok=1", "note": f"code {REDACTED}cd", "other": "ab1"}
    assert paths == ["note", "url"]


@pytest.mark.parametrize("value", ["zq9", "q7", "z"])
def test_R12_a_value_under_four_characters_is_refused_naming_the_variable_and_never_the_value(value):
    """R12: it would redact ordinary text, so it is a configuration error, not a quiet gap."""
    with pytest.raises(ConfigError, match="environment variable A_PASSWORD") as caught:
        secret_values({"A_PASSWORD": value})
    assert f"'{value}'" not in str(caught.value)
    assert f"={value}" not in str(caught.value)
    with pytest.raises(ConfigError, match="a registered secret"):
        register_secret(value)
    with pytest.raises(ConfigError, match="given for this record"):
        secret_values({}, extra=[value])
    with pytest.raises(ConfigError, match="fewer than 4 characters"):
        Redactor([value])


def test_R12_a_variable_with_no_value_is_nothing_to_redact_not_a_short_secret():
    """R12: an unset or empty variable is skipped."""
    assert secret_values({"A_PASSWORD": "", "B_TOKEN": "   "}) == []


def test_R12_a_number_whose_string_form_is_a_secret_is_replaced():
    """R12: numbers are compared by their string form."""
    out, paths = redact_structure({"pin": 482913, "n": 3, "f": 2.5, "ok": True}, {"A_PASSWORD": "482913"})
    assert out == {"pin": REDACTED, "n": 3, "f": 2.5, "ok": True}
    assert paths == ["pin"]


def test_R12_the_forms_codex_listed_a_short_password_all_digits_brackets_and_a_minted_token():
    """R12: each form that used to survive is gone from the written structure."""
    clear_registry()
    register_secret("minted-9f8e7d6c5b4a")
    env = {"P_PASSWORD": "pw12", "N_SECRET": "123456789012", "B_SECRET_KEY": "a]b[c]d[e"}
    data = {
        "short": "the password pw12 here",
        "digits": "pin 123456789012",
        "brackets": "x a]b[c]d[e y",
        "big": 123456789012,
        "token": "minted-9f8e7d6c5b4a",
        "arguments": {"token": 12, "headers": {"Authorization": "Bearer minted-9f8e7d6c5b4a"}},
        "ordinary": [1, 2.5, True, None, "text"],
    }
    out, _ = redact_structure(data, env)
    clear_registry()
    text = json.dumps(out)
    for secret in ("pw12", "123456789012", "a]b[c]d[e", "minted-9f8e7d6c5b4a"):
        assert secret not in text
    assert out["big"] == REDACTED
    assert out["ordinary"] == [1, 2.5, True, None, "text"]


def test_R12_a_minted_token_is_redacted_everywhere_once_registered_and_not_before():
    """R12: a token minted at run time is in no environment variable; registering it is what makes it a secret."""
    clear_registry()
    token = "minted-0a1b2c3d4e5f"
    data = {"url": f"https://host/path?access={token}", "note": f"bearer {token}"}
    assert redact_structure(data, {})[0] == data
    register_secret(token)
    try:
        out, paths = redact_structure(data, {})
    finally:
        clear_registry()
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


def test_R12_a_json_schema_definition_is_not_redacted_by_the_name_of_a_property():
    """R12: the tools of a record and any mapping under properties name things; a token property keeps its schema."""
    schema = {"type": "object", "properties": {"token": {"type": "string"}, "password": {"type": "string"}}}
    record_like = {"tools": [{"name": "login", "input_schema": schema}], "other": {"properties": {"secret": {"a": 1}}}}
    out, paths = redact_structure(record_like, {})
    assert out == record_like
    assert paths == []
    arguments = {"arguments": {"token": "live-value"}}
    assert redact_structure(arguments, {})[0] == {"arguments": {"token": REDACTED}}


def test_R12_a_secret_inside_a_json_string_is_redacted_in_its_escaped_form():
    """R12: a native message holds text inside JSON strings, where a quote in the value is a backslash and a quote."""
    secret = 'sk"live\\0123456789'
    message = {"role": "tool", "content": json.dumps({"echo": secret, "n": 1})}
    assert json.dumps(secret)[1:-1] in message["content"]
    out, _ = redact_structure(message, {"A_API_KEY": secret})
    assert secret not in out["content"]
    assert json.dumps(secret)[1:-1] not in out["content"]
    assert json.loads(out["content"]) == {"echo": REDACTED, "n": 1}


def test_R12_a_secret_in_json_inside_json_inside_json_is_redacted_at_every_level():
    """R12: up to three levels of escaping; decoding the result at any level does not recover the value."""
    secret = 'sk"live\\0123456789'
    level1 = json.dumps({"k": secret})
    level2 = json.dumps({"inner": level1})
    level3 = json.dumps({"outer": level2})
    out, _ = redact_structure({"one": level1, "two": level2, "three": level3}, {"A_API_KEY": secret})
    flat = json.dumps(out)
    assert secret not in flat
    for form in forms_of(secret):
        assert form not in flat
    decoded = json.loads(json.loads(json.loads(out["three"])["outer"])["inner"])
    assert decoded == {"k": REDACTED}
    assert json.loads(json.loads(out["two"])["inner"]) == {"k": REDACTED}


def test_R12_a_secret_in_a_url_is_redacted_in_its_percent_encoded_form():
    """R12: decoding the redacted text must not recover the value."""
    secret = "sk/live+key 0123456789"
    url = f"https://host/p?k={quote(secret, safe='')}&z=1"
    out, _ = redact_structure({"url": url}, {"A_API_KEY": secret})
    assert quote(secret, safe="") not in out["url"]
    assert out["url"] == f"https://host/p?k={REDACTED}&z=1"
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
        redact_structure({KEY: 1, other: 2}, {"A_API_KEY": KEY, "B_API_KEY": other})


def test_redact_text_replaces_the_secret_in_every_form():
    secret = 'tok"en-with-quote-0123456789'
    text, count = redact_text(f"saw {json.dumps(secret)[1:-1]} and {secret}", {"X_TOKEN": secret})
    assert count == 1
    assert secret not in text
    assert json.dumps(secret)[1:-1] not in text


QUOTED = "quartz'pebble\"7781-amber"  # both quote kinds: Python's repr escapes one of them


def test_R12_a_secret_in_python_repr_form_is_redacted_from_a_record(tmp_path):
    """R12: an exception's text shows a secret as Python's repr does (`KeyError(secret)`), which escapes a quote when
    the secret holds both kinds, so neither the raw form nor a JSON form matches it."""
    shown = str(KeyError(QUOTED))
    assert QUOTED not in shown  # the repr escaped a quote
    base = hand_made()
    record = hand_made(totals={**base.totals, "final_text": f"failed: {shown}"})
    with pytest.warns(RuntimeWarning, match="redacted secrets"):
        written = record.write(tmp_path, environ={"TOYLAB_TOKEN": QUOTED}).read_text(encoding="utf-8")
    final = RunRecord.from_json(decode(written)).totals["final_text"]
    assert final == "failed: '[redacted]'"
    assert "pebble" not in written


def test_R12_a_secret_in_the_repr_of_its_bytes_is_redacted_too():
    """R12: `str(ValueError(secret.encode()))` shows the bytes' repr, which escapes the non-ASCII characters."""
    secret = "café-quartz-7781"
    text, count = redact_text(f"bad header {secret.encode()!r} and {secret!r}", {"TOYLAB_TOKEN": secret})
    assert count == 1
    assert "quartz" not in text


@pytest.mark.parametrize("secret", ["café-quartz-7781", "quartz'pebble\"7781"])
def test_R12_a_repr_of_a_secret_inside_json_or_a_url_is_redacted_in_that_form(secret):
    """R12: every escaped form is derived from every base form, so the repr of a secret's bytes inside a JSON string
    (an exception's text a record or a log keeps as JSON) and inside a URL is caught too."""
    env = {"TOYLAB_TOKEN": secret}
    for shown in (str(ValueError(secret.encode())), repr(secret)):
        for text in (json.dumps({"e": shown}), json.dumps(json.dumps({"e": shown})), quote(shown, safe="")):
            redacted, _ = redact_text(text, env)
            assert "quartz" not in redacted, text


def test_R12_a_multi_line_secret_leaves_a_records_keys_intact_and_is_redacted_whole(tmp_path):
    """R12: a record holds a multi-line secret whole or JSON-escaped, which the whole-secret forms match, so its lines
    are not secrets there: a line that is a common word (`prompt`) must not rename a key, or sealing fails."""
    secret = "prompt\nquartz-7781"
    base = hand_made()
    record = hand_made(totals={**base.totals, "final_text": f"saw {secret} and {json.dumps(secret)}"})
    with pytest.warns(RuntimeWarning, match="redacted secrets"):
        written = record.write(tmp_path, environ={"TOYLAB_TOKEN": secret}).read_text(encoding="utf-8")
    loaded = RunRecord.from_json(decode(written))
    assert loaded.prompt == base.prompt  # the key and its content are as they were
    assert loaded.totals["final_text"] == 'saw [redacted] and "[redacted]"'
    assert "quartz-7781" not in written


def test_R12_a_multi_line_secret_that_comes_back_line_by_line_is_redacted_in_a_records_values(tmp_path):
    """R12: a multi-line secret (a private key, say) can come back re-encoded: CRLF line ends, or its lines as separate
    strings. Each line is replaced in a record's string values, while its keys are searched for whole secrets only."""
    secret = "amber-line-one\ncobalt-line-two"
    base = hand_made()
    record = hand_made(
        totals={**base.totals, "final_text": "amber-line-one\r\ncobalt-line-two"},
        instruments={"probe": {"lines": ["amber-line-one", "cobalt-line-two"]}},
    )
    with pytest.warns(RuntimeWarning, match="redacted secrets"):
        written = record.write(tmp_path, environ={"TOYLAB_TOKEN": secret}).read_text(encoding="utf-8")
    assert "amber-line-one" not in written
    assert "cobalt-line-two" not in written
    loaded = RunRecord.from_json(decode(written))
    assert loaded.instruments["probe"]["lines"] == ["[redacted]", "[redacted]"]


def test_R12_extra_secrets_given_as_an_iterator_are_read_once_for_both_passes():
    """R12: the long and the short secrets are picked from one reading of `extra`, so an iterator is not used up by
    the first pass and a short value it holds is still replaced as a token."""
    redactor = Redactor.from_environment({}, extra=iter(["xyz", "long-secret-4471"]), short_tokens=True)
    assert redactor.text("xyz and long-secret-4471, not xyzzy") == "[redacted] and [redacted], not xyzzy"


def test_R12_each_line_of_a_multi_line_secret_is_redacted_on_its_own():
    """R12: in plain text, which is never sealed, a formatter that puts something between the lines of a secret (an
    exception group's `| `) breaks the whole; each line of four or more characters is still caught."""
    secret = "amber-line-one\ncobalt-line-two\nxy"
    text, _ = redact_text("  | ValueError: amber-line-one\n    | cobalt-line-two\n    | xy", {"TOYLAB_TOKEN": secret})
    assert "amber-line-one" not in text
    assert "cobalt-line-two" not in text
    assert text.endswith("| xy")  # a line under four characters would match ordinary text: it is left


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


def test_R12_the_writer_refuses_a_short_secret_in_the_environment_naming_the_variable(tmp_path):
    """R12: a secret too short to redact is a configuration error at the write as it is when the builder starts."""
    with pytest.raises(ConfigError, match="environment variable DB_PASSWORD"):
        hand_made().write(tmp_path, environ={"DB_PASSWORD": "abc"})


def test_R12_a_key_that_names_a_secret_is_applied_inside_a_string_that_holds_a_json_object():
    """R12: a tool call's arguments are a JSON string in a native message, and the key's value is still a secret."""
    arguments = '{"token": "live-value-123", "user": "ann"}'
    data = {"function": {"arguments": arguments}, "note": "[1, 2]", "plain": "{not json", "text": '{"user": "ann"}'}
    out, paths = redact_structure(data, {})
    assert json.loads(out["function"]["arguments"]) == {"token": REDACTED, "user": "ann"}
    assert (out["note"], out["plain"], out["text"]) == ("[1, 2]", "{not json", '{"user": "ann"}')
    assert "function.arguments" in paths
    again, more = redact_structure(out, {})
    assert again == out
    assert more == []


SECRET_FOR_CUTS = "zqx7-live-key-4455667788"


NOTICE = "\n[output truncated: 312 of 600 characters dropped by the harness cap]"


def test_R12_the_fragment_a_cap_left_at_its_cut_point_is_replaced_by_one_marker_and_the_notice_stays():
    """R12: four or more characters that begin a secret, just before the cap's notice, are the cut secret."""
    env = {"A_API_KEY": SECRET_FOR_CUTS}
    for length in (4, 7, len(SECRET_FOR_CUTS) - 1):
        out = redact_text(f"head {SECRET_FOR_CUTS[:length]}{NOTICE}", env)[0]
        assert out == f"head {REDACTED}{NOTICE}"
    assert redact_text(f"head {SECRET_FOR_CUTS[:3]}{NOTICE}", env)[0] == f"head {SECRET_FOR_CUTS[:3]}{NOTICE}"
    once = redact_text(f"head {SECRET_FOR_CUTS[:9]}{NOTICE}", env)[0]
    assert redact_text(once, env)[0] == once


def test_R12_a_fragment_anywhere_but_the_cut_point_is_ordinary_text_and_is_left_alone():
    """R12: the end of a text with no notice, the start of a text, and the middle of one are not cut points."""
    env = {"A_API_KEY": SECRET_FOR_CUTS}
    start, mid = SECRET_FOR_CUTS[:6], SECRET_FOR_CUTS[:5]
    for text in (
        f"result: {start}",  # ends with it, but nothing was cut
        f"{start} and more",
        f"{SECRET_FOR_CUTS[-6:]} rest",  # begins with the end of the secret
        f"a {mid} in the middle",
        f"x {mid} y{NOTICE}",  # a notice, but the fragment is not at the cut
    ):
        assert redact_text(text, env)[0] == text


def test_R12_ordinary_text_that_looks_like_the_start_of_a_secret_is_untouched_except_at_a_cut():
    """R12: Codex's case; abcd begins a secret, and is a word elsewhere."""
    env = {"A_API_KEY": "abcd-secret-value-0001"}
    ordinary = "abcd is just a word; abcd again"
    assert redact_text(ordinary, env)[0] == ordinary
    assert redact_text("x" * 127 + "abcd" + "Y" * 400, env)[0] == "x" * 127 + "abcd" + "Y" * 400
    assert redact_text("x" * 127 + "abcd" + NOTICE, env)[0] == "x" * 127 + REDACTED + NOTICE


def test_R12_a_marker_already_at_the_cut_point_is_not_marked_twice():
    env = {"A_API_KEY": SECRET_FOR_CUTS}
    text = f"head {REDACTED}{NOTICE}"
    assert redact_text(text, env)[0] == text


def test_R12_only_the_value_of_a_secret_named_key_changes_inside_json_text_every_other_character_stays():
    """R12: a long number and an overflowing one are byte for byte as they were, and so are the spaces."""
    text = '{"token": "abc123", "a": 1.234567890123456789,  "b": 1e400, "c": [0.10, 2E5], "d": {"password": {"x": 1}}}'
    out, paths = redact_structure({"arguments": text}, {})
    assert out["arguments"] == (
        '{"token": "[redacted]", "a": 1.234567890123456789,  "b": 1e400, "c": [0.10, 2E5], '
        '"d": {"password": "[redacted]"}}'
    )
    assert paths == ["arguments"]
    assert redact_structure(out, {})[0] == out


def test_R12_json_text_inside_json_text_is_scanned_in_turn_and_other_values_stay():
    """R12: the inner string is written back only because a secret was in it."""
    inner = '{"token": "zzz", "n": 1e400, "m": 1.234567890123456789}'
    message = '{"args": ' + json.dumps(inner) + ', "k": 2.50}'
    out, _ = redact_structure({"message": message}, {})
    decoded = json.loads(out["message"])
    assert json.loads(decoded["args"])["token"] == REDACTED
    assert "1e400" in decoded["args"]
    assert "1.234567890123456789" in decoded["args"]
    assert out["message"].endswith(', "k": 2.50}')


def test_R12_json_text_that_cannot_be_scanned_is_redacted_by_value_only():
    """R12: text that is not strict JSON (NaN, a trailing comma, a broken end) is left as the value rules made it."""
    for text in ('{"token": "abc123", "x": NaN}', '{"token": "abc123",}', '{"token": "abc123"} trailing', "[1, 2"):
        assert redact_structure({"s": text}, {})[0] == {"s": text}
    secret = "value-based-secret-99"
    out, _ = redact_structure({"s": '{"token": "' + secret + '", "x": NaN}'}, {"A_API_KEY": secret})
    assert out["s"] == '{"token": "[redacted]", "x": NaN}'


def test_R12_property_names_inside_json_text_are_not_secrets_either():
    """R12: a schema written as JSON text keeps the properties it names."""
    text = '{"properties": {"token": {"type": "string"}}, "required": ["token"]}'
    assert redact_structure({"s": text}, {})[0] == {"s": text}


def test_R12_the_registry_of_secrets_only_grows_nothing_in_the_module_removes_a_secret():
    """R12: no function of the module forgets a secret; a value registered stays redacted for the process."""
    from expcore.records import redact

    assert not hasattr(redact, "forget_secrets")
    register_secret("registered-for-good-81726354")
    assert redact_structure({"a": "registered-for-good-81726354"}, {})[0] == {"a": REDACTED}


def test_R12_for_a_diagnostic_a_short_secret_is_replaced_only_where_it_stands_as_a_token():
    """R12: a record refuses a secret under four characters; a diagnostic replaces it where no letter, digit or
    underscore touches it, so the same letters inside a longer name stay readable."""
    with pytest.raises(ConfigError, match="TEST_TOKEN"):
        Redactor.from_environment({"TEST_TOKEN": "abc"})
    redactor = Redactor.from_environment({"TEST_TOKEN": "abc"}, short_tokens=True)
    text = redactor.text("ValueError('abc') at abcd, x_abc and abc.")
    assert text == "ValueError('[redacted]') at abcd, x_abc and [redacted]."
    assert redactor.text(text) == text  # still a fixed point
