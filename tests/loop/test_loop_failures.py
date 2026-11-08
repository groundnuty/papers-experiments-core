"""How failures end an attempt: infrastructure (B02, C02), slow requests (B03), defects (L08), own errors."""

import pytest
from loop_support import FakeProvider, ScriptedBackend, call, turn

from expcore import errors
from expcore.errors import CORE_KINDS, ContextOverflow, InfraError, RequestAbandoned, register_infra_kind
from expcore.loop.outcomes import harness_defect_of
from expcore.models.adapters import StubAdapter
from expcore.plugin import ToolOutput

ECHO = {"echo": lambda args: "echoed"}


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Every test starts with only the core's infrastructure kinds and leaves no platform kind behind."""
    monkeypatch.setattr(errors, "_platform_kinds", {})


def test_b02_an_endpoint_failure_after_the_retries_is_infrastructure_with_the_partial_record(run):
    """B02: InfraError("endpoint") from the backend is outcome infrastructure, unscored, with what happened so far."""
    backend = StubAdapter([[("echo", {})], InfraError("endpoint", "5 retries, HTTP 503")])
    result = run(backend, [FakeProvider(ECHO)])
    assert result.outcome.name == "infrastructure"
    assert result.outcome.infra_kind == "endpoint"
    assert result.outcome.detail == "5 retries, HTTP 503"
    assert result.outcome.set_aside is True
    assert len(result.turns) == 1  # the partial record is kept (R09)
    assert len(result.tool_calls) == 1
    assert result.final_text is None


def test_b03_a_request_that_went_out_and_was_not_answered_is_a_time_cap_with_usage_marked_incomplete(run):
    """B03: RequestAbandoned ends the run at its time cap, never as infrastructure, usage unknown."""
    result = run(StubAdapter([RequestAbandoned("read timeout")]), [FakeProvider(ECHO)])
    assert result.outcome.name == "time_cap"
    assert result.outcome.set_aside is False
    assert result.usage_incomplete is True


def test_a_context_overflow_is_the_attempts_own_ending_not_infrastructure(run):
    """A conversation that outgrew the window ends as an error of that class, and is scored as the attempt's."""
    result = run(StubAdapter([ContextOverflow(400, "maximum_context_length")]), [FakeProvider(ECHO)])
    assert result.outcome.name == "error"
    assert result.outcome.error_type == "ContextOverflow"
    assert result.outcome.set_aside is False


def test_any_other_backend_exception_is_an_error_named_by_its_class(run):
    result = run(StubAdapter([LookupError("odd")]), [FakeProvider(ECHO)])
    assert (result.outcome.name, result.outcome.error_type) == ("error", "LookupError")
    assert "odd" in result.outcome.detail


@pytest.mark.parametrize("kind", CORE_KINDS)
def test_c02_every_core_kind_from_the_backend_survives_into_the_result_under_its_own_name(run, kind):
    """C02: a kind the backend raises is not folded into another one on the way to the result."""
    result = run(StubAdapter([InfraError(kind, "scripted")]), [FakeProvider(ECHO)])
    assert result.outcome.name == "infrastructure"
    assert result.outcome.infra_kind == kind


@pytest.mark.parametrize("kind", CORE_KINDS)
def test_c02_every_core_kind_from_a_tool_provider_survives_into_the_result_under_its_own_name(run, kind):
    """C02: the same for a kind a tool provider raises (a failed tool server, a failed sandbox, an unknown kill)."""

    def fail(args):
        raise InfraError(kind, "scripted")

    result = run(StubAdapter([[("fail", {})], "never"]), [FakeProvider({"fail": fail})])
    assert result.outcome.infra_kind == kind
    assert result.tool_calls[0].status == "failed"
    assert result.tool_calls[0].detail == f"infrastructure: {kind}"
    assert len([m for m in result.transcript if m["role"] == "tool"]) == 1


def test_c02_a_kind_a_platform_registered_survives_too(run):
    """C02: the vocabulary is closed but extensible; a registered kind comes out unchanged."""
    register_infra_kind("provider_restart", "a storage provider restarted under the run")
    result = run(StubAdapter([InfraError("provider_restart", "scripted")]), [FakeProvider(ECHO)])
    assert result.outcome.infra_kind == "provider_restart"


def test_l08_a_tool_raising_on_valid_input_is_a_harness_defect_never_a_scored_failure(run):
    """L08: a crash in tool code ends as harness_defect with a line-free stack hash; the attempt is set aside."""

    def buggy(args):
        raise ZeroDivisionError("inside the provider")

    result = run(StubAdapter([[("buggy", {})], "never"]), [FakeProvider({"buggy": buggy})])
    assert result.outcome.name == "harness_defect"
    assert result.outcome.set_aside is True
    assert result.outcome.defect.exception == "ZeroDivisionError"
    assert len(result.outcome.defect.stack_sha256) == 64
    assert "inside the provider" in result.outcome.detail
    assert result.tool_calls[0].status == "failed"


def test_l08_the_same_defect_gives_the_same_hash_and_another_defect_another(run):
    """L08: the hash identifies the defect, so a re-run after the fix can be matched to it."""

    def fail_with(error):
        def handler(args):
            raise error

        return handler

    def hash_of(error):
        result = run(StubAdapter([[("t", {})], "never"]), [FakeProvider({"t": fail_with(error)})])
        return result.outcome.defect.stack_sha256

    assert hash_of(ValueError("one")) == hash_of(ValueError("two"))
    assert hash_of(ValueError("one")) != hash_of(KeyError("one"))


def test_l08_the_stack_hash_ignores_line_numbers():
    """L08: two raise sites on different lines of one function are one defect."""

    def boom(first):
        if first:
            raise ValueError("a")
        raise ValueError("b")

    hashes = set()
    for flag in (True, False):
        try:
            boom(flag)
        except ValueError as exc:
            hashes.add(harness_defect_of(exc).stack_sha256)
    assert len(hashes) == 1


def test_l08_a_crash_in_the_loops_own_code_is_a_defect_too(run):
    """L08: a fault outside the tool, here the backend failing to take the results, is not the agent's failure."""

    class Broken(StubAdapter):
        def add_tool_results(self, results):
            raise RuntimeError("history lost")

    result = run(Broken([[("echo", {})], "never"]), [FakeProvider(ECHO)])
    assert result.outcome.name == "harness_defect"
    assert "the loop" in result.outcome.detail


def test_a_provider_error_result_is_not_a_defect(run):
    """An error the provider returns is for the model; only a raise is a defect."""
    failing = {"t": lambda a: ToolOutput("denied", is_error=True, error_class="PermissionDenied")}
    result = run(StubAdapter([[("t", {})], "done"]), [FakeProvider(failing)])
    assert result.outcome.name == "finished"
    assert result.tool_calls[0].error_class == "PermissionDenied"


def test_an_infrastructure_ending_after_a_tool_error_keeps_both_the_calls_and_the_turns(run):
    backend = ScriptedBackend([turn(call("call_one", "echo")), InfraError("endpoint", "down")])
    result = run(backend, [FakeProvider(ECHO)])
    assert result.outcome.infra_kind == "endpoint"
    assert [t.status for t in result.tool_calls] == ["ok"]
