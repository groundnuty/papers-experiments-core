"""L17: the declared per-call bound is for tool servers only. A call to any other tool, the code tool above all, is
handed the agent's time left exactly as it is without the key, and is never judged by the bound.

The bound exists so that a hung tool server (an MCP call) is charged as `knowledge` infrastructure and not as the
agent's time. A code cell the agent wrote is the agent's own time, so a study that declares the bound to classify
tool-server hangs must not also cap every cell at it, or change what the model is told when a cell runs long.
"""

from loop_support import FakeProvider, hangs_until_its_bound, slow, stalls

from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import StubAdapter


def test_l17_a_call_to_a_tool_that_is_no_tool_server_is_handed_the_time_left_whatever_bound_is_declared(run, clock):
    """L17: with the bound declared, an in-process tool (the code tool stands for one) runs to the agent's time left:
    the bound of 60 s is never handed to it, at the first call or after time has passed."""
    provider = FakeProvider({"slow": slow(clock, 100)})
    result = run(
        StubAdapter([[("slow", {})], [("slow", {})], "done"]), [provider], time_cap_s=300, tool_server_call_bound_s=60
    )
    assert [c[2] for c in provider.calls] == [300, 200]
    assert result.outcome.name == "finished"
    assert [c.status for c in result.tool_calls] == ["ok", "ok"]


def test_l17_a_call_to_a_tool_server_still_gets_the_shorter_of_the_bound_and_the_time_left(run, clock):
    """L17: the tool server's call is handed the bound while it is the shorter, and the time left after that."""
    provider = FakeProvider({"slow": slow(clock, 250), "hang": None}, tool_server=True)
    provider.handlers["hang"] = hangs_until_its_bound(clock, provider, "hang")
    result = run(
        StubAdapter([[("slow", {})], [("hang", {})], "never asked"]),
        [provider],
        time_cap_s=300,
        tool_server_call_bound_s=100,
    )
    assert [c[2] for c in provider.calls] == [100, 50]
    assert (result.outcome.name, result.outcome.infra_kind) == ("time_cap", None)


def test_l17_in_one_run_the_bound_reaches_the_tool_server_and_not_the_code_tool(run, clock):
    """L17: the two kinds of tool in one run, as a study with a code tool and a tool server has them: the same bound,
    declared once, is handed to the server's tool and not to the code tool."""
    code = FakeProvider({"python": slow(clock, 10)})
    server = FakeProvider({"lookup": slow(clock, 10)}, tool_server=True)
    result = run(
        StubAdapter([[("python", {}), ("lookup", {}), ("python", {})], "done"]),
        [code, server],
        time_cap_s=300,
        tool_server_call_bound_s=60,
    )
    assert [c[2] for c in code.calls] == [300, 280]
    assert [c[2] for c in server.calls] == [60]
    assert result.outcome.name == "finished"


def test_l17_a_code_cell_cut_at_the_time_left_after_the_bound_passed_is_the_agents_time_cap(run, clock, sleeper):
    """L17: a code cell is never judged by the bound. A cell held 301 s under a declared bound of 100 s is cut at the
    agent's 300 s: the agent's time cap, as without the key, and not `knowledge` infrastructure, which the same call to
    a tool server is (`test_loop_time`)."""
    provider = FakeProvider({"python": stalls(clock, 301)})
    result = run(
        StubAdapter([[("python", {})], "never asked"]),
        [provider],
        sleep=sleeper,
        time_cap_s=300,
        tool_server_call_bound_s=100,
    )
    assert [c[2] for c in provider.calls] == [300]
    assert (result.outcome.name, result.outcome.infra_kind) == ("time_cap", None)
    assert result.tool_calls[0].status == "cut"
    assert "outlasted the bound" not in result.tool_calls[0].text


def test_l17_a_code_cell_is_cut_at_the_same_reading_with_the_bound_declared_and_without(run, clock, sleeper):
    """L17: the same run, with and without the key, gives the same ending, the same call record and the same text."""
    plain, bounded = [], []
    for results, options in ((plain, {}), (bounded, {"tool_server_call_bound_s": 100})):
        clock.now = 1000.0
        provider = FakeProvider({"python": stalls(clock, 301)})
        results.append(
            run(
                StubAdapter([[("python", {})], "never asked"]),
                [provider],
                sleep=sleeper,
                time_cap_s=300,
                **options,
            )
        )
    (first,), (second,) = plain, bounded
    assert (first.outcome, first.tool_calls[0].text, first.tool_calls[0].status) == (
        second.outcome,
        second.tool_calls[0].text,
        second.tool_calls[0].status,
    )
    assert first.tool_calls[0].elapsed_s == second.tool_calls[0].elapsed_s == 301.0


def test_l17_the_record_names_the_tools_the_bound_applies_to_only_when_a_bound_is_declared(run, clock):
    """L17: `agent_config.tool_server_tools` says which tools a declared bound reached, so a reader can see that the
    code tool was not among them. A study that declares no bound has no such key, and its configuration is what it
    was."""
    code = FakeProvider({"python": slow(clock, 1)})
    server = FakeProvider({"lookup": slow(clock, 1), "fetch": slow(clock, 1)}, tool_server=True)
    plain = run(StubAdapter(["done"]), [code, server], time_cap_s=300)
    assert "tool_server_tools" not in plain.config
    declared = run(StubAdapter(["done"]), [code, server], time_cap_s=300, tool_server_call_bound_s=60)
    assert declared.config["tool_server_tools"] == ["fetch", "lookup"]
    assert declared.config_sha256 != plain.config_sha256
    nobody = run(StubAdapter(["done"]), [code], time_cap_s=300, tool_server_call_bound_s=60)
    assert nobody.config["tool_server_tools"] == []


def test_l17_only_a_true_marker_makes_a_provider_a_tool_server(clock):
    """L17: the marker is read as exactly `True`, so a provider whose `tool_server` is a truthy string, a number or None
    is no tool server: the bound is never handed to a tool on a loose reading."""
    for marker in ("yes", 1, None, [True]):
        provider = FakeProvider({"lookup": slow(clock, 1)})
        provider.tool_server = marker
        box = ToolBox([provider], cap_chars=100)
        assert (box.is_tool_server("lookup"), box.tool_server_names) == (False, [])
    server = FakeProvider({"lookup": slow(clock, 1)}, tool_server=True)
    assert ToolBox([server], cap_chars=100).tool_server_names == ["lookup"]


def test_l17_a_box_given_the_tool_servers_takes_them_over_its_providers_and_ignores_names_it_lacks():
    """L17: a replay's box is told by the record which tools were a tool server's, whatever its one provider says, and a
    name that is no tool of the box is not made one (the record's list then differs from the replay's and diverges)."""
    server = FakeProvider({"lookup": lambda a: "x"}, tool_server=True)
    code = FakeProvider({"python": lambda a: "x"})
    told = ToolBox([server, code], cap_chars=100, tool_servers=["python", "ghost"])
    assert told.tool_server_names == ["python"]
    assert told.is_tool_server("python") is True
    assert told.is_tool_server("lookup") is False
    assert told.is_tool_server("ghost") is False
    assert ToolBox([server, code], cap_chars=100, tool_servers=()).tool_server_names == []
    assert ToolBox([server, code], cap_chars=100).tool_server_names == ["lookup"]
