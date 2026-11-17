"""The outage check's call of a knowledge tool when the client's own bound expires (L16, O28): no server and no
network, only a provider that raises what `McpToolProvider.execute` raises."""

import asyncio

from expcore.errors import InfraError
from expcore.tools.mcp_client import CallTimedOut
from expcore_onedata.day0.outage_tools import Tools


class RaisingProvider:
    """A provider whose every call raises `error`."""

    def __init__(self, error: BaseException):
        self.error = error

    def tools(self):
        return []

    async def execute(self, name, arguments, *, timeout_s):
        raise self.error


def once(error: BaseException):
    """Make one call through `Tools` against a provider that raises `error`, on a clock that ticks 1 s a read."""
    ticks = iter([0.0, 1.0])
    tools = Tools(
        RaisingProvider(error),
        roles=None,
        space_id="space",
        scrub=lambda text: {"chars": len(text)},
        bound_s=5.0,
        poll_s=0.0,
        clock=lambda: next(ticks),
        sleep=lambda seconds: None,
    )
    return asyncio.run(tools._once("get_transfer", {}))


def test_l16_the_clients_own_expiry_at_the_bound_is_blocked_and_timed_out_not_an_answer_that_never_came():
    """L16: the client now ends a call at its `timeout_s`; the check passes the same bound, so the client's expiry is
    the check's own: `blocked` and timed out, as when the check's wait ran out first (O28), and never `no answer`."""
    facts, raw = once(CallTimedOut("get_transfer", 5.0))
    assert facts == {"outcome": "blocked", "timed_out": True, "seconds": 1.0}
    assert raw is None


def test_l16_a_server_that_closed_is_still_no_answer():
    """L16: only the bound's expiry is blocked; a server that closed or failed is still `no answer`."""
    facts, raw = once(InfraError("knowledge", "the connection closed"))
    assert (facts["outcome"], facts["timed_out"]) == ("no answer", False)
    assert raw is None
