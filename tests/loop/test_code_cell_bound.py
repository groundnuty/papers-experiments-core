"""L18: the optional wall limit on one code cell. A study that declares `[agent] code_cell_bound_s` has each call to a
tool that runs code cells handed the shorter of the limit and the agent's time left, and told, through the call
context, that it runs under the limit. Every other tool is handed what it was without the key, and a study that declares
none keeps every configuration, record and fingerprint it had.

The limit is the sandbox's to keep: it kills the cell, the call returns the cell's timeout, and the run goes on (the
code tool's side of this is in `tests/onedata/sandbox/test_codetool_cell_bound.py`). The loop's part is what it hands
the call, and that a cell which reaches the time left first is its own cut, as it always was.
"""

import dataclasses

import pytest
from loop_support import FakeProvider, slow, stalls

from expcore.config.models import Limits
from expcore.loop.config import AgentConfig, config_sha256_of
from expcore.models.adapters import StubAdapter
from expcore.plugin import ToolOutput

LIMITS = Limits(max_tool_rounds=5, time_cap_s=300, token_budget=100_000, output_cap_chars=8000, max_output_tokens=4096)


def test_l18_a_code_cell_is_handed_the_shorter_of_the_declared_limit_and_the_time_left(run, clock):
    """L18: the minimum is taken at every call. With a 250 s limit under a 300 s cap, the first cell is handed the
    limit; after it has taken 90 s the 210 s left are shorter, and so are the 120 s left after the second."""
    provider = FakeProvider({"python": slow(clock, 90)}, code_cell=True)
    result = run(
        StubAdapter([[("python", {})], [("python", {})], [("python", {})], "done"]),
        [provider],
        time_cap_s=300,
        code_cell_bound_s=250,
    )
    assert [c[2] for c in provider.calls] == [250, 210, 120]
    assert result.outcome.name == "finished"


def test_l18_the_call_context_names_the_limit_only_for_a_call_that_runs_under_it(run, clock):
    """L18: the code tool tells a cell killed at the limit from one that reached the time left by `ctx.cell_bound_s`,
    which is set only while the limit is strictly shorter than the time left: the first call here, not the next two."""
    seen = []

    def cell(args, ctx):
        seen.append(getattr(ctx, "cell_bound_s", None))
        clock.advance(100)
        return "ok"

    provider = FakeProvider({"python": cell}, code_cell=True)
    run(
        StubAdapter([[("python", {})], [("python", {})], [("python", {})], "done"]),
        [provider],
        time_cap_s=300,
        code_cell_bound_s=250,
    )
    assert seen == [250, None, None]


def test_l18_a_limit_past_the_time_left_hands_the_time_left_and_names_no_limit(run, clock):
    """L18: a limit longer than the agent's time is no limit at all: the cell is handed the time left, as without the
    key, and told of none."""
    seen = []

    def cell(args, ctx):
        seen.append(getattr(ctx, "cell_bound_s", None))
        return "ok"

    provider = FakeProvider({"python": cell}, code_cell=True)
    run(StubAdapter([[("python", {})], "done"]), [provider], time_cap_s=300, code_cell_bound_s=900)
    assert [c[2] for c in provider.calls] == [300]
    assert seen == [None]


def test_l18_a_cell_killed_at_the_limit_is_the_agents_own_result_and_the_next_call_runs(run, clock):
    """L18: the cell's timeout is a tool result like any other: an error the agent reads, not an ending. The run is
    not infrastructure, not a time cap, and the call after it is made and answered."""

    def killed(args):
        clock.advance(60)
        return ToolOutput("partial\nthe code ran past its 60 s limit and was killed", True, "timeout")

    code, other = FakeProvider({"python": killed}, code_cell=True), FakeProvider({"echo": lambda args: "echoed"})
    result = run(
        StubAdapter([[("python", {})], [("echo", {})], "done"]), [code, other], time_cap_s=300, code_cell_bound_s=60
    )
    assert [c[2] for c in code.calls] == [60]
    assert [c[2] for c in other.calls] == [240]  # the next tool is handed the time left, whatever is declared
    assert [(c.status, c.error_class) for c in result.tool_calls] == [("error", "timeout"), ("ok", None)]
    assert "ran past its 60 s limit" in result.tool_calls[0].text
    assert (result.outcome.name, result.outcome.infra_kind) == ("finished", None)


def test_l18_a_cell_that_reaches_the_time_left_before_the_limit_is_the_loops_cut_as_without_the_key(
    run, clock, sleeper
):
    """L18: with the limit longer than the time left, a cell held 301 s is cut at the agent's 300 s, the agent's time
    cap, exactly as it is without the key: the same ending, the same call record and the same text."""
    ended = []
    for options in ({}, {"code_cell_bound_s": 400}):
        clock.now = 1000.0
        provider = FakeProvider({"python": stalls(clock, 301)}, code_cell=True)
        ended.append(
            run(StubAdapter([[("python", {})], "never asked"]), [provider], sleep=sleeper, time_cap_s=300, **options)
        )
        assert [c[2] for c in provider.calls] == [300]
    plain, declared = ended
    assert (declared.outcome.name, declared.outcome.infra_kind) == ("time_cap", None)
    assert declared.tool_calls[0].status == plain.tool_calls[0].status == "cut"
    assert declared.tool_calls[0].text == plain.tool_calls[0].text
    assert declared.tool_calls[0].elapsed_s == plain.tool_calls[0].elapsed_s == 301.0
    assert declared.outcome == plain.outcome


def test_l18_a_provider_that_does_not_keep_the_limit_is_still_cut_only_at_the_time_left(run, clock, sleeper):
    """L18: the limit is the sandbox's to keep, as the tool server's bound is the provider's. One that ignores it is cut
    at the time left, as the agent's time cap, and never judged by the limit."""
    provider = FakeProvider({"python": stalls(clock, 301)}, code_cell=True)
    result = run(
        StubAdapter([[("python", {})], "never asked"]), [provider], sleep=sleeper, time_cap_s=300, code_cell_bound_s=100
    )
    assert [c[2] for c in provider.calls] == [100]
    assert (result.outcome.name, result.outcome.infra_kind) == ("time_cap", None)
    assert result.tool_calls[0].status == "cut"


def test_l18_the_limit_reaches_a_code_cell_and_no_other_tool_in_one_run(run, clock):
    """L18: a code tool, a tool server and another in-process tool in one run. The limit is handed to the code cell
    alone; the tool server's own bound, declared beside it, to the tool server alone; the third tool gets the time
    left."""
    code = FakeProvider({"python": slow(clock, 10)}, code_cell=True)
    server = FakeProvider({"lookup": slow(clock, 10)}, tool_server=True)
    other = FakeProvider({"note": slow(clock, 10)})
    result = run(
        StubAdapter([[("python", {}), ("lookup", {}), ("note", {})], "done"]),
        [code, server, other],
        time_cap_s=300,
        code_cell_bound_s=120,
        tool_server_call_bound_s=30,
    )
    assert [c[2] for c in code.calls] == [120]
    assert [c[2] for c in server.calls] == [30]
    assert [c[2] for c in other.calls] == [280]
    assert result.outcome.name == "finished"


def test_l18_the_declared_limit_is_a_value_of_the_record_and_names_the_tools_it_reached(run, clock):
    """L18: `agent_config.code_cell_bound_s` is the number and `code_cell_tools` the names of the tools that run code
    cells, sorted, written beside it and only with it, so a replay judges alike. A limit that reached no tool says so,
    and the hash covers both."""
    code = FakeProvider({"python": slow(clock, 1), "bash": slow(clock, 1)}, code_cell=True)
    server = FakeProvider({"lookup": slow(clock, 1)}, tool_server=True)
    plain = run(StubAdapter(["done"]), [code, server], time_cap_s=300)
    assert "code_cell_bound_s" not in plain.config
    assert "code_cell_tools" not in plain.config
    declared = run(StubAdapter(["done"]), [code, server], time_cap_s=300, code_cell_bound_s=60)
    assert declared.config["code_cell_bound_s"] == 60
    assert declared.config["code_cell_tools"] == ["bash", "python"]
    assert "tool_server_tools" not in declared.config  # the tool server's bound is declared separately
    assert declared.config_sha256 != plain.config_sha256
    nobody = run(StubAdapter(["done"]), [server], time_cap_s=300, code_cell_bound_s=60)
    assert nobody.config["code_cell_tools"] == []


def test_l18_an_undeclared_limit_leaves_the_configuration_as_it_was():
    """L18: the configuration's values, and so its hash, its `agent_config` in the record and its caps in the
    fingerprint, are what they were before the key existed: it is left out, never written as a null."""
    values = AgentConfig(LIMITS).as_dict()
    assert list(values) == [
        "limits",
        "malformed_policy",
        "ceiling_s",
        "ceiling_kind",
        "min_call_s",
        "ceiling_factor",
        "cleanup_deadline_s",
    ]
    assert AgentConfig(LIMITS).config_sha256() == config_sha256_of(values)
    declared = AgentConfig(LIMITS, code_cell_bound_s=3600.0).as_dict()
    assert declared["code_cell_bound_s"] == 3600.0
    assert list(declared)[-1] == "code_cell_bound_s"
    assert set(declared) - set(values) == {"code_cell_bound_s"}
    assert "code_cell_bound_s" in {f.name for f in dataclasses.fields(AgentConfig)}


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), True, "60"], ids=str)
def test_l18_a_declared_limit_is_a_positive_finite_number_of_seconds(bad):
    """L18: refused when the configuration is built, naming the key."""
    with pytest.raises(ValueError, match="code_cell_bound_s must be a positive, finite number of seconds"):
        AgentConfig(LIMITS, code_cell_bound_s=bad)
