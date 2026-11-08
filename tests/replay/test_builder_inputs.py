"""What the record builder is given: an iterable is read once, whatever it is (R12)."""

import asyncio

import pytest
from loop_support import FakeClock, ScriptedBackend, never
from record_support import CODE, IDENTITY, make_config, record_run

from expcore.loop.agent_loop import run_agent
from expcore.loop.toolbox import ToolBox
from expcore.records.build import build_record
from expcore.records.recording import RecordingBackend
from expcore.records.redact import REDACTED
from expcore.replay.replay import replay_record


def run_with(options, *, user="do the task", environ=None):
    """Record a run with the given record options and a clean environment for the builder."""
    return record_run(
        ["done"], {}, user=user, record_options={"environ": {} if environ is None else environ, **options}
    )


def test_R12_an_iterator_given_as_secrets_or_as_secret_env_is_read_once_and_still_redacts():
    """R12: the early check of the builder must not use up what the record is redacted with."""
    secret = "iter-secret-value-5544"
    with pytest.warns(RuntimeWarning):
        by_value = run_with({"secrets": iter([secret])}, user=f"use {secret}")
    assert by_value.prompt["user"]["text"] == f"use {REDACTED}"
    declared = "declared-iter-value-33"
    with pytest.warns(RuntimeWarning):
        by_name = run_with({"secret_env": iter(["ODD_NAME"])}, user=f"use {declared}", environ={"ODD_NAME": declared})
    assert by_name.prompt["user"]["text"] == f"use {REDACTED}"
    assert replay_record(by_value).outcome["name"] == "finished"


def test_R12_the_record_builder_too_reads_an_iterator_once():
    """R12: build_record takes the same arguments and the same iterators."""
    token = "run-only-token-iterated-1"
    config = make_config()
    recording = RecordingBackend(ScriptedBackend(["done"]))
    box = ToolBox([], cap_chars=config.limits.output_cap_chars)
    result = asyncio.run(run_agent(recording, box, "s", f"use {token}", config, clock=FakeClock(), sleep=never))
    with pytest.warns(RuntimeWarning):
        record = build_record(
            result,
            recording,
            identity=IDENTITY,
            system="s",
            user=f"use {token}",
            specs=[],
            code=CODE,
            secrets=iter([token]),
            secret_env=iter([]),
            environ={},
        )
    assert record.prompt["user"]["text"] == f"use {REDACTED}"
