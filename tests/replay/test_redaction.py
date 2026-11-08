"""A record built from a run that held a secret pins the redacted data, and replays from it (R12, R03)."""

import asyncio
import warnings

import pytest
from loop_support import FakeClock, FakeProvider, ScriptedBackend, never
from record_support import CODE, IDENTITY, make_config, record_run

from expcore.loop.agent_loop import run_agent
from expcore.loop.toolbox import ToolBox
from expcore.prompt.templates import PromptTemplate, sha256_of
from expcore.records.build import build_record, record_attempt
from expcore.records.digest import digest
from expcore.records.record import RunRecord, decode
from expcore.records.recording import RecordingBackend
from expcore.records.redact import REDACTED, register_secret
from expcore.replay.replay import replay_record

KEY = "sk-live-0123456789abcdefghij"


@pytest.fixture
def key_in_environment(monkeypatch):
    monkeypatch.setenv("RUN_API_KEY", KEY)


def test_R12_a_secret_in_the_prompt_is_redacted_when_the_record_is_built_and_listed_by_path(key_in_environment):
    """R12: the stored prompt holds the placeholder, the record lists the field, and nothing is left to redact later."""
    with pytest.warns(RuntimeWarning, match="redacted secrets at 1 place"):
        record = record_run(["done"], {}, user=f"use the key {KEY} to log in")
    assert record.prompt["user"]["text"] == f"use the key {REDACTED} to log in"
    assert record.redacted_paths == ["prompt.user.text"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        text = record.dumps()
    assert KEY not in text
    assert RunRecord.from_json(decode(text)).dumps() == text


def test_R12_the_digests_are_those_of_the_redacted_data_so_a_single_turn_run_with_a_secret_replays_cleanly(
    key_in_environment,
):
    """R12: prompt, conversation and configuration digests are taken after redaction; replay compares those."""
    with pytest.warns(RuntimeWarning):
        record = record_run(["done"], {}, user=f"the key is {KEY}")
    user = record.prompt["user"]
    assert user["sha256"] == sha256_of(user["text"]) != sha256_of(f"the key is {KEY}")
    opening = [{"role": "system", "content": "be brief"}, {"role": "user", "content": user["text"]}]
    assert record.transcript[0]["request_sha256"] == digest(opening)
    replayed = replay_record(record)
    assert replayed.outcome["name"] == "finished"
    assert replayed.prompt == record.prompt


def test_R12_a_slot_value_that_is_a_secret_is_redacted_and_the_rendered_text_follows_it(key_in_environment):
    """R12: a prompt rendered from a template keeps the template hash, and its slots and text hold the placeholder."""
    template = PromptTemplate("Log in with {CREDENTIAL} and report.")
    rendered = template.render({"CREDENTIAL": KEY})
    with pytest.warns(RuntimeWarning):
        record = asyncio.run(
            record_attempt(
                ScriptedBackend(["done"]),
                [FakeProvider({"echo": lambda a: "x"})],
                "sys",
                rendered,
                make_config(),
                IDENTITY,
                code=CODE,
                clock=FakeClock(),
                sleep=never,
            )
        )
    user = record.prompt["user"]
    assert user["slots"] == {"CREDENTIAL": REDACTED}
    assert user["text"] == f"Log in with {REDACTED} and report."
    assert user["template_sha256"] == template.sha256
    assert record.redacted_paths == ["prompt.user.slots.CREDENTIAL", "prompt.user.text"]
    assert replay_record(record).prompt["user"] == user


def test_R12_a_tool_result_that_held_a_secret_is_redacted_in_the_log_and_the_messages_and_replays(key_in_environment):
    """R12: the call log and the native messages agree on the redacted text, and the next request pins it."""
    with pytest.warns(RuntimeWarning):
        record = record_run([[("echo", {"x": "a"})], "done"], {"echo": lambda a: f"the key is {KEY}"})
    assert record.tool_calls[0]["text"] == f"the key is {REDACTED}"
    assert KEY not in record.dumps()
    assert replay_record(record).tool_calls == record.tool_calls
    second = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "do the task"}]
    second += [record.transcript[0]["native"], *record.transcript[0]["tool_messages"]]
    assert record.transcript[1]["request_sha256"] == digest(second)


def test_R12_a_secret_registered_after_it_was_minted_is_redacted_from_a_record_built_later():
    """R12: a minted token is in no variable; once registered it is gone from the records that would hold it."""
    token = "minted-5a4b3c2d1e0f"
    register_secret(token)
    with pytest.warns(RuntimeWarning):
        record = record_run(["done"], {}, user=f"token {token}")
    assert token not in record.dumps()
    assert record.redacted_paths == ["prompt.user.text"]
    assert replay_record(record).outcome["name"] == "finished"


def test_R12_secrets_given_for_one_record_are_redacted_from_it_and_remembered_by_no_other():
    """R12: the builder takes secrets for one record, so a platform can pass what it minted for this run."""
    token = "run-only-token-8f7e6d5c"
    config = make_config()
    recording = RecordingBackend(ScriptedBackend(["done"]))
    box = ToolBox([], cap_chars=config.limits.output_cap_chars)
    result = asyncio.run(run_agent(recording, box, "s", f"use {token}", config, clock=FakeClock(), sleep=never))
    with pytest.warns(RuntimeWarning):
        record = build_record(
            result, recording, identity=IDENTITY, system="s", user=f"use {token}", specs=[], code=CODE, secrets=[token]
        )
    assert record.prompt["user"]["text"] == f"use {REDACTED}"
    plain = build_record(result, recording, identity=IDENTITY, system="s", user=f"use {token}", specs=[], code=CODE)
    assert plain.prompt["user"]["text"] == f"use {token}"
