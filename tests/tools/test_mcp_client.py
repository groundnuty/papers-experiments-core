"""The MCP stdio client against a tiny real server written with the SDK's server API: the start, the frozen manifest,
the allow-list, and the split of failures by side (L12, L13, L14, C02). No network: the server is a local child."""

import asyncio
import os
import sys
import time

import anyio
import pytest
from mcp import MCPError, StdioServerParameters
from mcp.types import CONNECTION_CLOSED, INTERNAL_ERROR, INVALID_PARAMS

from expcore.config.models import Limits
from expcore.errors import InfraError
from expcore.loop.agent_loop import run_agent
from expcore.loop.config import AgentConfig
from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import StubAdapter
from expcore.tools.manifest import FrozenManifest, ManifestError, live_manifest
from expcore.tools.mcp_client import McpToolProvider

SERVER = ["-m", "expcore_onedata.testing.fake_mcp_server"]  # the stand-in MCP server: its module, run with -m


def launch(tools="echo,add", *, suffix="", hang=False):
    """The command and arguments of the fake server, with its server-side allow-list."""
    args = [*SERVER, "--tools", tools]
    if suffix:
        args += ["--suffix", suffix]
    if hang:
        args.append("--hang")
    return sys.executable, args


def provider(frozen, tools="echo,add", **kwargs):
    command, args = launch(tools, **{k: kwargs.pop(k) for k in ("suffix", "hang") if k in kwargs})
    kwargs.setdefault("allowed_tools", [t for t in tools.split(",") if t])
    return McpToolProvider(command, args, frozen=frozen, **kwargs)


def freeze(tools):
    command, args = launch(tools)
    return FrozenManifest.from_live(asyncio.run(live_manifest(StdioServerParameters(command=command, args=args))))


@pytest.fixture(scope="module")
def frozen() -> FrozenManifest:
    """The frozen manifest of the two-tool condition, read once from a real server."""
    return freeze("echo,add")


@pytest.fixture(scope="module")
def frozen_with_crash() -> FrozenManifest:
    return freeze("echo,crash")


def start_fails(p) -> BaseException:
    async def go():
        async with p:
            raise AssertionError("the start was expected to fail")

    with pytest.raises((ManifestError, InfraError)) as caught:
        asyncio.run(go())
    return caught.value


def test_a_server_that_equals_the_frozen_manifest_starts_and_serves_its_tools(frozen):
    async def go():
        async with provider(frozen) as p:
            return (
                [s.name for s in p.tools()],
                await p.execute("echo", {"text": "hi"}, timeout_s=10),
                await p.execute("add", {"a": 2, "b": 3}, timeout_s=10),
                p.manifest_sha256,
            )

    names, echoed, added, sha = asyncio.run(go())
    assert names == ["add", "echo"]
    assert (echoed.text, echoed.is_error) == ('{"result": "hi"}', False)
    assert added.text == '{"result": 5}'
    assert sha == frozen.sha256


def test_l13_an_extra_tool_on_the_server_fails_the_start(frozen):
    """L13: a server exposing a tool the frozen manifest does not hold fails to start; nothing is filtered away."""
    error = start_fails(provider(frozen, "echo,add,extra"))
    assert isinstance(error, ManifestError)
    assert "extra on the server: extra" in error.problems


def test_l13_a_missing_tool_fails_the_start(frozen):
    """L13: a tool of the frozen manifest that the server lacks fails the start."""
    error = start_fails(provider(frozen, "echo"))
    assert isinstance(error, ManifestError)
    assert "missing from the server: add" in error.problems


def test_l13_a_changed_description_fails_the_start(frozen):
    """L13: names, descriptions and schemas must all equal the frozen manifest."""
    error = start_fails(provider(frozen, suffix=" Now with more."))
    assert isinstance(error, ManifestError)
    assert "echo.description differs" in error.problems


def test_l13_an_allow_list_naming_a_tool_the_server_lacks_fails_the_start(frozen):
    """L13: the allow-list handed to the launcher must name tools the server has."""
    error = start_fails(provider(frozen, "echo,add", allowed_tools=["echo", "add", "ghost"]))
    assert isinstance(error, ManifestError)
    assert any("allow-list names tools the server lacks: ['ghost']" in p for p in error.problems)


def test_l13_the_client_does_not_filter_a_server_that_shows_more_than_the_allow_list(frozen):
    """L13: there is no client-side filter, so a server showing a tool outside the allow-list fails the start."""
    error = start_fails(provider(frozen, "echo,add", allowed_tools=["echo"]))
    assert isinstance(error, ManifestError)
    assert any("outside the allow-list: ['add']" in p for p in error.problems)


def wait_for(condition, *, what: str, deadline_s: float = 60.0) -> None:
    """Poll until `condition()` is true; fail naming `what` after a bound that no loaded machine should reach."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    pytest.fail(f"{what} did not happen within {deadline_s:g} s")


def test_l14_a_server_that_answers_starts_within_a_generous_bound(frozen):
    """L14: the success path. The bound is generous (60 s) on a fast server, so a slow disk cannot fail the start."""

    async def go():
        async with provider(frozen, start_timeout_s=60.0) as p:
            return [s.name for s in p.tools()]

    assert asyncio.run(go()) == ["add", "echo"]


@pytest.fixture
def spawned(monkeypatch) -> list[int]:
    """The pid of every process the MCP client spawns, taken from the launcher's own process handle.

    The pid is known the moment the process exists, whatever the child has got round to doing, so a test that asserts
    the child's end does not wait for a loaded machine to let the child write a file first.
    """
    pids: list[int] = []
    launch_process = anyio.open_process

    async def spy(*args, **kwargs):
        process = await launch_process(*args, **kwargs)
        pids.append(process.pid)
        return process

    monkeypatch.setattr(anyio, "open_process", spy)
    return pids


def test_l14_a_server_that_never_answers_fails_the_start_at_the_bound_and_leaves_no_child(frozen, spawned):
    """L14: a server that never speaks is broken: the start fails as knowledge infrastructure at its timeout.

    The server sleeps for an hour, so a start that returns at all was cut by the bound. The bound is never honoured
    early (the lower limit), and the upper limit is loose by tens of seconds: the test does not depend on how fast
    the machine is, only on the bound being enforced rather than the hour being waited out. The child it started is
    found by the pid the launcher reports, and must be gone.
    """
    bound_s = 1.0
    started = time.monotonic()
    error = start_fails(provider(frozen, hang=True, start_timeout_s=bound_s))
    elapsed = time.monotonic() - started
    assert bound_s <= elapsed < bound_s + 30
    assert isinstance(error, InfraError)
    assert error.kind == "knowledge"
    assert "did not start within 1 s" in error.detail
    assert len(spawned) == 1, "the client was expected to launch the server through anyio.open_process"

    def gone() -> bool:
        try:
            os.kill(spawned[0], 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(gone, what="the exit of the failed start's child process")


def test_l14_a_command_that_cannot_be_launched_is_a_broken_server(frozen):
    """L14: a server that cannot even be spawned fails the start as knowledge infrastructure."""
    error = start_fails(McpToolProvider("/nonexistent/server-binary", [], frozen=frozen))
    assert isinstance(error, InfraError)
    assert error.kind == "knowledge"


def test_l12_invalid_parameters_come_back_as_a_tool_error_for_the_model(frozen):
    """L12: the server's answer to a bad call is a tool result, not infrastructure."""

    async def go():
        async with provider(frozen) as p:
            return await p.execute("add", {"a": "not a number", "b": 1}, timeout_s=10)

    result = asyncio.run(go())
    assert result.is_error is True
    assert result.error_class == "ToolError"
    assert "validation error" in result.text


def test_l12_a_closed_connection_is_knowledge_infrastructure(frozen_with_crash):
    """L12: the server dying under a call raises infrastructure of kind knowledge."""

    async def go():
        async with provider(frozen_with_crash, "echo,crash") as p:
            return await p.execute("crash", {}, timeout_s=10)

    with pytest.raises(InfraError) as caught:
        asyncio.run(go())
    assert caught.value.kind == "knowledge"


class FakeSession:
    """Stands in for the SDK session, to raise protocol errors a real server of this SDK does not send."""

    def __init__(self, error):
        self.error = error

    async def call_tool(self, name, arguments):
        raise self.error


@pytest.mark.parametrize("code", [INVALID_PARAMS, -32601, 12345])
def test_l12_any_other_mcp_error_goes_to_the_model_as_tool_output(frozen, code):
    """L12: invalid params, an unknown method or a server-defined error answer the agent's own call."""
    p = provider(frozen)
    p._session = FakeSession(MCPError(code, "bad request"))
    result = asyncio.run(p.execute("echo", {}, timeout_s=1))
    assert (result.is_error, result.error_class, result.text) == (True, "MCPError", f"{code}: bad request")


@pytest.mark.parametrize("code", [CONNECTION_CLOSED, INTERNAL_ERROR])
def test_l12_a_closed_connection_or_an_internal_error_is_infrastructure(frozen, code):
    """L12: only these two protocol errors are the server's fault."""
    p = provider(frozen)
    p._session = FakeSession(MCPError(code, "the server said so"))
    with pytest.raises(InfraError) as caught:
        asyncio.run(p.execute("echo", {}, timeout_s=1))
    assert caught.value.kind == "knowledge"


def test_a_transport_error_from_the_streams_is_infrastructure(frozen):
    class ClosedResourceError(Exception):
        """Same class name as the stream library's, which the client matches by name."""

    p = provider(frozen)
    p._session = FakeSession(ClosedResourceError())
    with pytest.raises(InfraError):
        asyncio.run(p.execute("echo", {}, timeout_s=1))


def test_a_call_before_the_start_is_a_programming_error(frozen):
    with pytest.raises(RuntimeError, match="not started"):
        asyncio.run(provider(frozen).execute("echo", {}, timeout_s=1))


def test_c02_a_knowledge_failure_in_the_middle_of_a_run_keeps_its_kind_in_the_loop_result(frozen_with_crash):
    """C02: the kind the client raised comes out of the loop unchanged, with the partial record."""
    limits = Limits(
        max_tool_rounds=5, time_cap_s=60.0, token_budget=100_000, output_cap_chars=2000, max_output_tokens=512
    )
    config = AgentConfig(limits)

    async def go():
        async with provider(frozen_with_crash, "echo,crash") as p:
            box = ToolBox([p], cap_chars=limits.output_cap_chars)
            backend = StubAdapter([[("echo", {"text": "alive"})], [("crash", {})], "never"])
            return await run_agent(backend, box, "system", "task", config)

    result = asyncio.run(go())
    assert result.outcome.name == "infrastructure"
    assert result.outcome.infra_kind == "knowledge"
    assert [t.status for t in result.tool_calls] == ["ok", "failed"]
    assert result.tool_calls[0].text == '{"result": "alive"}'


def test_the_tool_box_shows_the_model_the_frozen_tools_with_the_cap_stated(frozen):
    async def go():
        async with provider(frozen) as p:
            return ToolBox([p], cap_chars=1234).definitions()

    definitions = asyncio.run(go())
    assert [d["name"] for d in definitions] == ["add", "echo"]
    assert all("longer than 1234 characters" in d["description"] for d in definitions)
    assert definitions[1]["input_schema"]["required"] == ["text"]
