"""The tool provider interface: what a spec and an output may hold, and that a plain double satisfies the protocol."""

import asyncio

import pytest

from expcore.plugin import NO_WAITS, CallContext, ToolOutput, ToolProvider, ToolSpec


@pytest.mark.parametrize("name", ["", "has space", "dot.name", "x" * 65, "naïve"])
def test_a_tool_name_the_vendor_apis_reject_is_refused(name):
    with pytest.raises(ValueError, match="tool name"):
        ToolSpec(name, "d")


def test_a_schema_that_is_not_an_object_is_refused():
    with pytest.raises(ValueError, match="type object"):
        ToolSpec("t", "d", {"type": "string"})


def test_a_spec_defaults_to_a_non_terminal_tool_without_arguments():
    spec = ToolSpec("ping", "Reply with pong.")
    assert spec.terminal is False
    assert spec.input_schema == {"type": "object", "properties": {}}


def test_an_error_class_needs_an_error():
    with pytest.raises(ValueError, match="error_class"):
        ToolOutput("fine", error_class="Oops")
    assert ToolOutput("bad", is_error=True, error_class="Oops").error_class == "Oops"


def test_a_tool_output_takes_no_reported_wait_any_more():
    """L04: waits on infrastructure are measured live through the call context, never reported afterwards."""
    with pytest.raises(TypeError):
        ToolOutput("x", infra_wait_s=1.0)  # type: ignore[call-arg]


def test_a_call_context_can_be_used_outside_the_loop_and_measures_nothing():
    async def go():
        async with NO_WAITS.infrastructure_wait():
            return "done"

    assert isinstance(NO_WAITS, CallContext)
    assert asyncio.run(go()) == "done"


def test_l07_the_source_flag_is_unset_by_default_and_must_be_a_boolean_when_given():
    assert ToolOutput("x").source_truncated is None
    assert ToolOutput("x", source_truncated=True).source_truncated is True
    with pytest.raises(ValueError, match="source_truncated"):
        ToolOutput("x", source_truncated="yes")  # type: ignore[arg-type]


def test_a_class_with_the_two_methods_is_a_provider_without_inheriting():
    class Mine:
        def tools(self):
            return [ToolSpec("t", "d")]

        async def execute(self, name, arguments, *, timeout_s, ctx):
            return ToolOutput("ok")

    assert isinstance(Mine(), ToolProvider)
    assert not isinstance(object(), ToolProvider)
