"""The loop end to end against the 3b stub backend and fake tool providers: endings, rounds, arguments, capping."""

import asyncio
import json

import pytest
from loop_support import FakeProvider, ScriptedBackend, call, malformed, turn

from expcore.loop.agent_loop import _Run, run_agent
from expcore.loop.records import TOOL_STATUSES
from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import StubAdapter
from expcore.plugin import ToolOutput

ECHO = {"echo": lambda args: f"echoed {args.get('x', '')}"}


def tool_texts(result):
    return [r.text for r in result.tool_calls]


def test_a_tool_free_reply_finishes_with_its_text_as_the_final_reply(run):
    result = run(StubAdapter(["the answer"]), [FakeProvider(ECHO)])
    assert result.outcome.name == "finished"
    assert result.final_text == "the answer"
    assert (result.rounds, result.tool_calls) == (0, [])
    assert result.outcome.set_aside is False


def test_a_tool_round_runs_the_calls_in_order_and_hands_the_results_back(run):
    provider = FakeProvider(ECHO)
    backend = StubAdapter([[("echo", {"x": "a"}), ("echo", {"x": "b"})], "done"])
    result = run(backend, [provider])
    assert [c[1] for c in provider.calls] == [{"x": "a"}, {"x": "b"}]
    assert tool_texts(result) == ["echoed a", "echoed b"]
    roles = [m["role"] for m in result.transcript]
    assert roles == ["system", "user", "assistant", "tool", "tool", "assistant"]
    assert [t.status for t in result.tool_calls] == ["ok", "ok"]
    assert all(status in TOOL_STATUSES for status in (t.status for t in result.tool_calls))


def test_the_record_holds_each_turn_with_its_usage_and_the_hashes_of_what_was_run(run):
    result = run(StubAdapter([[("echo", {"x": "a"})], "done"]), [FakeProvider(ECHO)])
    assert [t.index for t in result.turns] == [0, 1]
    assert result.turns[0].tool_call_ids == [result.tool_calls[0].call_id]
    assert result.tokens_used == sum(t.input_tokens + t.output_tokens for t in result.turns)
    assert len(result.tool_manifest_sha256) == 64
    assert len(result.config_sha256) == 64
    assert result.backend_info["backend"] == "stub"
    assert result.config["limits"]["output_cap_chars"] == 8000


def test_a_stub_backend_names_the_model_tools_it_was_given(run):
    seen = {}

    class Spy(StubAdapter):
        def begin(self, system, user, tools):
            seen["tools"] = tools
            super().begin(system, user, tools)

    run(Spy(["ok"]), [FakeProvider(ECHO)])
    (tool,) = seen["tools"]
    assert tool["name"] == "echo"
    assert "Output limit:" in tool["description"]


def test_l01_malformed_arguments_are_reported_and_never_run(run):
    """L01: arguments '{"a":' are not executed, the tool result says so, the history is valid JSON holding the
    raw text, the round counts toward the turn cap, and the run's malformed-call count is 1."""
    provider = FakeProvider(ECHO)
    backend = ScriptedBackend(
        [turn(malformed("call_one", "echo")), turn(call("call_two", "echo", {"x": "ok"})), "done"]
    )
    result = run(backend, [provider], max_tool_rounds=2)
    assert provider.calls[0][1] == {"x": "ok"}  # only the valid call ran; nothing ran with {}
    assert len(provider.calls) == 1
    first = result.tool_calls[0]
    assert first.status == "malformed"
    assert "not valid JSON" in first.text
    assert "was not executed" in first.text
    assert first.arguments == {"_raw": '{"a":'}
    assert first.raw_arguments == '{"a":'
    history = json.loads(result.transcript[2]["tool_calls"][0]["function"]["arguments"])
    assert history == {"_raw": '{"a":'}
    assert result.malformed_calls == 1
    assert result.malformed_rounds == 1
    assert result.rounds == 2  # the malformed round counted
    assert result.outcome.name == "finished"


def test_l01_the_malformed_round_counts_toward_the_turn_cap(run):
    """L01: a malformed round is a round, so the cap fires on the next one."""
    backend = ScriptedBackend([turn(malformed("call_one", "echo")), turn(call("call_two", "echo")), "never reached"])
    result = run(backend, [FakeProvider(ECHO)], max_tool_rounds=1)
    assert result.outcome.name == "turn_cap"
    assert result.malformed_calls == 1


def test_l01_the_declared_alternative_ends_the_attempt(run):
    """L01: with the policy end, malformed arguments end the attempt as malformed_end and nothing runs."""
    provider = FakeProvider(ECHO)
    backend = ScriptedBackend([turn(call("call_one", "echo"), malformed("call_two", "echo")), "never reached"])
    result = run(backend, [provider], malformed_policy="end")
    assert result.outcome.name == "malformed_end"
    assert provider.calls == []
    assert [t.status for t in result.tool_calls] == ["skipped", "malformed"]
    assert result.malformed_calls == 1


def test_l01_running_the_tool_with_empty_arguments_is_not_a_policy(make_config):
    """L01: there is no setting that executes a malformed call with {}."""
    for policy in ("run_empty", "execute", "ignore", ""):
        with pytest.raises(ValueError, match="malformed_policy"):
            make_config(malformed_policy=policy)


def test_l01_a_malformed_call_is_counted_even_when_a_cap_ends_the_round(run):
    """L01: the count includes the round a cap ended, because the model did request it."""
    backend = ScriptedBackend([turn(malformed("call_one", "echo"), input_tokens=900, output_tokens=200)])
    result = run(backend, [FakeProvider(ECHO)], token_budget=1000)
    assert result.outcome.name == "token_cap"
    assert result.malformed_calls == 1


def test_l02_an_unknown_tool_is_an_error_result_listing_the_tools(run):
    """L02: an unknown name returns an error that lists the tools; the run goes on."""
    backend = StubAdapter([[("nope", {})], "done"])
    result = run(backend, [FakeProvider({"echo": lambda a: "x", "other": lambda a: "y"})])
    (record,) = result.tool_calls
    assert record.status == "error"
    assert record.error_class == "UnknownTool"
    assert record.text.startswith("UnknownTool: there is no tool named 'nope'")
    assert "echo, other" in record.text
    assert result.outcome.name == "finished"


def test_l02_an_argument_of_the_wrong_type_is_an_error_result_not_a_crash(run):
    """L02: a number where the schema says string is a tool error and the provider never sees the call."""
    provider = FakeProvider(ECHO)
    result = run(StubAdapter([[("echo", {"x": 5})], "done"]), [provider])
    (record,) = result.tool_calls
    assert record.error_class == "InvalidArguments"
    assert "argument 'x': expected string, got integer" in record.text
    assert provider.calls == []
    assert result.outcome.name == "finished"


def test_l02_a_missing_required_argument_is_an_error_result(run):
    """L02: the schema's required list is enforced before the provider runs."""
    schemas = {"echo": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}
    result = run(StubAdapter([[("echo", {})], "done"]), [FakeProvider(ECHO, schemas=schemas)])
    assert "missing required argument 'x'" in result.tool_calls[0].text


def test_l03_calls_in_one_turn_count_as_one_round(run):
    """L03: three calls in one reply are one round."""
    backend = StubAdapter([[("echo", {}), ("echo", {}), ("echo", {})], "done"])
    result = run(backend, [FakeProvider(ECHO)], max_tool_rounds=1)
    assert result.rounds == 1
    assert result.outcome.name == "finished"
    assert len(result.tool_calls) == 3


def test_l03_one_round_more_than_the_cap_ends_as_turn_cap(run):
    """L03: cap + 1 rounds end as turn_cap, and the unrun calls are logged as skipped."""
    provider = FakeProvider(ECHO)
    result = run(StubAdapter([[("echo", {})], [("echo", {})], [("echo", {})]]), [provider], max_tool_rounds=2)
    assert result.outcome.name == "turn_cap"
    assert result.rounds == 2
    assert len(provider.calls) == 2
    assert result.tool_calls[-1].status == "skipped"
    assert result.final_text is None


def test_l03_a_tool_free_final_reply_after_the_last_round_is_allowed(run):
    """L03: the cap counts rounds of tool calls, so a closing reply after the last one is not a cap."""
    result = run(StubAdapter([[("echo", {})], "closing words"]), [FakeProvider(ECHO)], max_tool_rounds=1)
    assert result.outcome.name == "finished"
    assert result.final_text == "closing words"


def test_a_token_budget_ends_the_attempt_as_token_cap(run):
    """The budget counts input and output tokens of every reply."""
    backend = ScriptedBackend(
        [
            turn(call("call_one", "echo"), input_tokens=600, output_tokens=100),
            turn(call("call_two", "echo"), input_tokens=400, output_tokens=50),
        ]
    )
    result = run(backend, [FakeProvider(ECHO)], token_budget=1000)
    assert result.outcome.name == "token_cap"
    assert result.tokens_used == 1150


def test_a_reply_that_reaches_the_token_budget_exactly_executes_none_of_its_calls(run):
    """The budget is exhausted at the budget, not past it: 15 of 15 tokens ends the attempt before the tool runs."""
    provider = FakeProvider(ECHO)
    backend = ScriptedBackend([turn(call("call_one", "echo"), input_tokens=10, output_tokens=5), "never reached"])
    result = run(backend, [provider], token_budget=15)
    assert result.outcome.name == "token_cap"
    assert result.tokens_used == 15
    assert provider.calls == []
    assert [t.status for t in result.tool_calls] == ["skipped"]


def test_a_reply_one_token_under_the_budget_is_still_acted_on(run):
    provider = FakeProvider(ECHO)
    backend = ScriptedBackend(
        [
            turn(call("call_one", "echo"), input_tokens=10, output_tokens=5),
            turn(input_tokens=1, output_tokens=0, text="x"),
        ]
    )
    result = run(backend, [provider], token_budget=16)
    assert len(provider.calls) == 1
    assert result.outcome.name == "token_cap"  # the second reply reaches 16 exactly
    assert result.tokens_used == 16


def test_no_model_request_is_made_when_the_budget_is_already_used_up(make_config, clock):
    """Checked before each request, not only after each reply: with the count at the budget the backend is not asked."""
    config = make_config(token_budget=100)
    requests = []

    class Counts(StubAdapter):
        async def step(self):
            requests.append(1)
            return await super().step()

    box = ToolBox([FakeProvider(ECHO)], cap_chars=config.limits.output_cap_chars)
    state = _Run(Counts(["never"]), box, "s", "u", config, config.timer(clock), asyncio.sleep)
    state.tokens = 100  # as if earlier replies had used the whole budget
    result = asyncio.run(state.execute())
    assert result.outcome.name == "token_cap"
    assert requests == []
    assert result.turns == []


def test_b12_a_reply_cut_at_the_output_limit_is_an_output_cap_even_over_budget(run):
    """B12: a tool-free reply with stop length ends as output cap even when the budget is also exceeded."""
    backend = StubAdapter([{"text": "a cut off ans", "stop": "length"}])
    result = run(backend, [FakeProvider(ECHO)], token_budget=1)
    assert result.outcome.name == "output_cap"
    assert result.tokens_used > 1
    assert result.final_text == "a cut off ans"


def test_b10_a_reply_without_usage_adds_nothing_and_marks_the_totals_incomplete(run):
    """B10: a missing count is not zero in the totals' meaning: the run says its token total is a lower bound."""
    backend = ScriptedBackend([turn(call("call_one", "echo"), input_tokens=None, output_tokens=None), "done"])
    result = run(backend, [FakeProvider(ECHO)])
    assert result.usage_incomplete is True
    assert result.turns[0].input_tokens is None


def test_l06_the_model_sees_the_capped_text_and_the_record_keeps_the_full_result(run):
    """L06: an output over the cap ends with the notice and stays within it; the record keeps the raw result."""
    long = "r" * 5000
    result = run(StubAdapter([[("big", {})], "done"]), [FakeProvider({"big": lambda a: long})], output_cap_chars=1000)
    (record,) = result.tool_calls
    assert len(record.text) <= 1000
    assert "characters dropped by the harness cap" in record.text
    assert record.raw_text == long
    assert record.truncated is True
    assert json.loads(json.dumps(result.transcript))[3]["content"] == record.text  # the model was given the cut text


def test_l06_a_long_error_keeps_its_class_the_raw_error_and_a_truncation_flag(run):
    """L06: a long error keeps its class in the capped text, its raw form in the record, and a truncation flag."""
    failing = {"boom": lambda a: ToolOutput("trace " * 400, is_error=True, error_class="ServerFault")}
    result = run(StubAdapter([[("boom", {})], "done"]), [FakeProvider(failing)], output_cap_chars=300)
    (record,) = result.tool_calls
    assert record.text.startswith("ServerFault: trace trace")
    assert len(record.text) <= 300
    assert record.raw_text == "ServerFault: " + "trace " * 400
    assert record.truncated is True
    assert record.status == "error"


def test_l06_an_error_without_a_class_is_named_tool_error(run):
    """L06: the class always leads, even when the provider gave none."""
    failing = {"boom": lambda a: ToolOutput("it broke", is_error=True)}
    result = run(StubAdapter([[("boom", {})], "done"]), [FakeProvider(failing)])
    assert result.tool_calls[0].text == "ToolError: it broke"
    assert result.tool_calls[0].truncated is False


def test_l06_two_caps_give_two_tool_manifests_and_two_configuration_hashes(run):
    """L06: the cap is stated in every tool description, so the tools hash and the configuration hash both change."""
    one = run(StubAdapter(["a"]), [FakeProvider(ECHO)], output_cap_chars=8000)
    two = run(StubAdapter(["a"]), [FakeProvider(ECHO)], output_cap_chars=4000)
    assert one.tool_manifest_sha256 != two.tool_manifest_sha256
    assert one.config_sha256 != two.config_sha256
    again = run(StubAdapter(["a"]), [FakeProvider(ECHO)], output_cap_chars=8000)
    assert (again.tool_manifest_sha256, again.config_sha256) == (one.tool_manifest_sha256, one.config_sha256)


def test_l09_calls_after_a_terminal_call_are_answered_skipped(run):
    """L09: calls following a submit are logged skipped and answered; the attempt finishes."""
    provider = FakeProvider({"echo": lambda a: "e", "submit": lambda a: "accepted"}, terminal=("submit",))
    backend = StubAdapter([[("echo", {}), ("submit", {}), ("echo", {}), ("echo", {})], "never reached"])
    result = run(backend, [provider])
    assert result.outcome.name == "finished"
    assert result.final_text is None
    assert [t.status for t in result.tool_calls] == ["ok", "ok", "skipped", "skipped"]
    assert len(provider.calls) == 2
    assert result.tool_calls[2].text.startswith("skipped: not executed")
    tool_messages = [m for m in result.transcript if m["role"] == "tool"]
    assert len(tool_messages) == 4  # every call of the reply has a result


def test_l09_a_terminal_call_that_fails_does_not_end_the_attempt(run):
    """L09: only a successful terminal call ends the attempt; a rejected submission lets the model try again."""
    outputs = iter([ToolOutput("invalid", is_error=True, error_class="Rejected"), ToolOutput("accepted")])
    provider = FakeProvider({"submit": lambda a: next(outputs)}, terminal=("submit",))
    result = run(StubAdapter([[("submit", {})], [("submit", {})], "never reached"]), [provider])
    assert [t.status for t in result.tool_calls] == ["error", "ok"]
    assert result.outcome.name == "finished"
    assert result.rounds == 2


def test_l11_an_empty_final_reply_differs_from_no_reply(run):
    """L11: the record distinguishes empty content from rounds exhausted."""
    empty = run(StubAdapter([""]), [FakeProvider(ECHO)])
    exhausted = run(StubAdapter([[("echo", {})], [("echo", {})]]), [FakeProvider(ECHO)], max_tool_rounds=1)
    assert empty.outcome.name == "finished"
    assert empty.final_text == ""
    assert exhausted.outcome.name == "turn_cap"
    assert exhausted.final_text is None


def test_the_tool_box_cap_must_match_the_configuration(make_config):
    """The configuration hash describes what the model saw, so a tool box with another cap is refused."""
    box = ToolBox([FakeProvider(ECHO)], cap_chars=100)
    with pytest.raises(ValueError, match="configuration hash"):
        asyncio.run(run_agent(StubAdapter(["x"]), box, "s", "u", make_config(output_cap_chars=8000)))


def test_a_timer_passed_in_is_the_account_the_backend_reads_its_time_left_from(make_config, clock):
    """B03: the backend's retry policy and the loop read one account of the agent's time (time_left)."""
    config = make_config(time_cap_s=300)
    timer = config.timer(clock)
    seen = []

    class Reads(StubAdapter):
        async def step(self):
            seen.append(timer.time_left())
            return await super().step()

    def spend(args):
        clock.advance(50)
        return "ok"

    box = ToolBox([FakeProvider({"spend": spend})], cap_chars=config.limits.output_cap_chars)
    result = asyncio.run(run_agent(Reads([[("spend", {})], "done"]), box, "s", "u", config, timer=timer))
    assert result.outcome.name == "finished"
    assert seen == [300, 250]
    assert timer.readings == [tuple(r) for r in result.clock_readings]


def test_a_timer_that_does_not_match_the_configuration_or_has_started_is_refused(make_config, clock):
    config = make_config(time_cap_s=300)
    box = ToolBox([FakeProvider(ECHO)], cap_chars=config.limits.output_cap_chars)
    other = make_config(time_cap_s=100).timer(clock)
    with pytest.raises(ValueError, match="caps differ"):
        asyncio.run(run_agent(StubAdapter(["x"]), box, "s", "u", config, timer=other))
    started = config.timer(clock)
    started.start()
    with pytest.raises(ValueError, match="already started"):
        asyncio.run(run_agent(StubAdapter(["x"]), box, "s", "u", config, timer=started))
