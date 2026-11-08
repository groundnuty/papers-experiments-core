"""Helpers for the record and replay tests: a hand-made record, and stub runs recorded through the loop. No network."""

import asyncio
import hashlib
import os
from typing import Any

import pytest
from loop_support import FakeClock, FakeProvider, ScriptedBackend, never

from expcore.config.models import Limits
from expcore.loop.config import AgentConfig
from expcore.records.build import RunIdentity, record_attempt
from expcore.records.record import RunRecord
from expcore.records.redact import SECRET_NAME, forget_secrets
from expcore.records.schema import CURRENT
from expcore.records.seal import seal

CODE = {"commit": "c" * 40, "dirty": False, "source": "git", "stale": False, "why": None, "tree_sha256": "d" * 64}
IDENTITY = RunIdentity(
    run_key="task=task-a|model=model-a|seed=0", factors={"task": "task-a", "model": "model-a", "seed": 0}, attempt=1
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hand_made(**overrides: Any) -> RunRecord:
    """A small valid record built by hand: one answer, no tools. Meant for tests of the format, not of replay."""
    system, user = "be brief", "say hi"
    turn = {
        "index": 0,
        "neutral": {"text": "hi", "reasoning": None, "tool_calls": []},
        "native": {"role": "assistant", "content": "hi"},
        "tool_messages": [],
        "request_sha256": sha("request"),
        "settings_sha256": sha("settings"),
        "stop_reason": "stop",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "uncached_input_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        },
        "latency_s": 0.5,
        "served_model": "m1",
        "system_fingerprint": None,
        "retries": [],
        "waits_left_open": 0,
        "infra_wait_by_kind": {},
    }
    fields: dict[str, Any] = {
        "run_key": IDENTITY.run_key,
        "factors": dict(IDENTITY.factors),
        "attempt": 1,
        "route": {"backend": "stub", "settings": {sha("settings"): {}}, "settings_sent_sha256": sha("settings")},
        "prompt": {
            "system": {"text": system, "sha256": sha(system), "template_sha256": None, "slots": None},
            "user": {"text": user, "sha256": sha(user), "template_sha256": None, "slots": None},
            "tools_sha256": sha("tools"),
        },
        "tools": [],
        "agent_config": {**make_config().as_dict(), "terminal_tools": []},
        "agent_config_sha256": sha("config"),
        "code": dict(CODE),
        "core_version": "0.1.0",
        "transcript": [turn],
        "failed_request": None,
        "tool_calls": [],
        "clock": [["start", 1000.0], ["before_model", 1000.0], ["end", 1000.5]],
        "outcome": {"name": "finished", "detail": "done", "infra_kind": None, "error_type": None, "defect": None},
        "totals": {
            "rounds": 0,
            "malformed_calls": 0,
            "malformed_events": [],
            "tokens_used": 12,
            "usage_incomplete": False,
            "agent_s": 0.5,
            "elapsed_s": 0.5,
            "infra_wait_s": 0.0,
            "infra_wait_by_kind": {},
            "cleanup_s": 0.0,
            "cleanup_timed_out": False,
            "abandoned_threads": 0,
            "final_text": "hi",
        },
        "schema_version": CURRENT,
    }
    seal(fields)  # a hand-made record is consistent; a test that breaks it does so with an override
    fields.update(overrides)
    return RunRecord(**fields)


def make_config(**overrides: Any) -> AgentConfig:
    """A loop configuration with room for a few rounds; overrides go to the limits or to the configuration."""
    limit_names = set(Limits.__dataclass_fields__)
    limits = {
        "max_tool_rounds": 10,
        "time_cap_s": 300.0,
        "token_budget": 100_000,
        "output_cap_chars": 200,
        "max_output_tokens": 4096,
    }
    limits.update({k: v for k, v in overrides.items() if k in limit_names})
    return AgentConfig(Limits(**limits), **{k: v for k, v in overrides.items() if k not in limit_names})


def record_run(
    script: list,
    handlers: dict,
    *,
    terminal: tuple[str, ...] = (),
    backend: Any = None,
    providers: list | None = None,
    sleep: Any = never,
    clock: Any = None,
    system: str = "be brief",
    user: str = "do the task",
    identity: RunIdentity = IDENTITY,
    shared_timer: bool = False,
    cleanup_waiter: Any = None,
    record_options: dict | None = None,
    **config: Any,
) -> RunRecord:
    """Run a scripted attempt through the real loop on a fake clock and return its record.

    With `shared_timer` (or a `cleanup_waiter`, which the timer takes) the loop's timer is made here and handed to the
    backend as its `time_left` and `timer`, as a backend built with a retry policy would have it, so the backend's own
    clock readings and waits are in the record.
    """
    backend = backend or ScriptedBackend(script)
    providers = providers if providers is not None else [FakeProvider(handlers, terminal=terminal)]
    clock = clock or FakeClock()
    agent_config = make_config(**config)
    timer = agent_config.timer(clock, cleanup_waiter=cleanup_waiter) if shared_timer or cleanup_waiter else None
    if timer is not None:
        backend.time_left, backend.timer = timer.time_left, timer
    return asyncio.run(
        record_attempt(
            backend,
            providers,
            system,
            user,
            agent_config,
            identity,
            code=dict(CODE),
            core_version="0.1.0",
            timer=timer,
            clock=clock,
            sleep=sleep,
            **(record_options or {}),
        )
    )


@pytest.fixture
def no_secrets_in_the_environment(monkeypatch):
    """Remove every variable named like a secret (a CI runner sets some) and forget registered secrets.

    Their short values would otherwise redact ordinary numbers of a record, as the redaction rules intend. A test
    module gets this as an autouse fixture by importing it in its directory's `conftest.py`.
    """
    for name in [n for n in os.environ if SECRET_NAME.search(n) and not n.startswith("GIT_")]:  # git needs its own
        monkeypatch.delenv(name)
    forget_secrets()
    yield
    forget_secrets()
