"""The bounded waits of provisioning (O07), on their own and without a federation."""

import asyncio

import httpx
import pytest
from placement_support import Ticker

from expcore_onedata import consistency
from expcore_onedata.client import OnezoneClient, PlatformError


def scripted(values):
    """A reader returning the values in order (an exception is raised), then repeating the last."""
    items = list(values)

    def read():
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    return read


def run(values, ticker, **kw):
    return consistency.converge(
        scripted(values),
        bool,
        timeout_s=kw.pop("timeout_s", 20.0),
        poll_s=1.0,
        clock=ticker.clock,
        sleep=ticker.sleep,
        **kw,
    )


def test_o07_convergence_needs_two_consecutive_matching_polls():
    """O07: a match, then a miss, then two matches converges at the fourth poll, not the first."""
    result = run([True, False, True, True], Ticker())
    assert result.converged
    assert [c["matches"] for c in result.checks] == [True, False, True, True]


def test_o07_a_match_that_keeps_reverting_never_converges_and_stops_at_the_bound():
    """O07: alternating answers never give two in a row; the wait ends at its timeout and says it did not converge."""
    ticker = Ticker()
    result = run([True, False] * 50, ticker, timeout_s=10.0)
    assert not result.converged
    assert ticker.now - 1000.0 <= 11.0


def test_o07_a_failed_poll_counts_as_a_miss_and_never_as_a_match():
    """O07: a read that fails is recorded as not ok and resets the run, so a failure cannot complete a convergence."""
    result = run([True, PlatformError("x"), True, True], Ticker())
    assert result.converged
    assert [c["probe_ok"] for c in result.checks] == [True, False, True, True]
    assert len(result.checks) == 4


def test_o07_all_polls_failing_gives_no_value_and_no_convergence():
    """O07: with every read failing the wait ends at the bound with nothing guessed."""
    result = run([PlatformError("x")], Ticker(), timeout_s=5.0)
    assert not result.converged
    assert result.value is None


def test_o07_a_defect_in_the_reader_is_not_swallowed():
    """O07: a reader that raises something other than a platform error is a defect and propagates."""
    with pytest.raises(KeyError):
        run([KeyError("x")], Ticker())


def test_o07_a_wait_inside_a_running_event_loop_is_refused():
    """O07: the waits are not a declared wait kind, so they must not run in the agent's time (on the loop's thread)."""

    async def inside():
        return consistency.converge(lambda: True, bool, timeout_s=1.0, poll_s=1.0)

    with pytest.raises(RuntimeError, match="must not run inside a run's event loop"):
        asyncio.run(inside())


def test_o07_no_provisioning_wait_is_a_declared_wait_kind():
    """O07: the data waits belong to provisioning, so none is among the harness's own wait kinds."""
    from expcore.plugin import WAIT_KINDS

    assert not [kind for kind in WAIT_KINDS if "transfer" in kind or "placement" in kind or "consisten" in kind]


def test_a_transfer_record_without_a_known_state_field_has_the_state_unknown():
    """An answer that names none of the state fields is "unknown", which is not an ended state."""
    assert consistency.transfer_state({"bytesReplicated": 5}) == "unknown"
    assert "unknown" not in consistency.ENDED_STATES
    assert consistency.transfer_state({"replicationStatus": "completed"}) == "completed"


def test_o12_a_space_root_answer_without_a_file_id_names_the_provider_by_position_not_by_name(config):
    """O12: the message says `providers[1]`, the position of the provider in platform.toml, and none of its name or
    address; the autouse scan of this directory also fails the test if any configured value is in it."""
    client = OnezoneClient(
        config, http=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    )
    with pytest.raises(PlatformError, match="holds no root file id") as raised:
        consistency.space_root(client, "beta", config.space.id)
    assert str(raised.value).startswith("providers[1]: ")
    assert "beta" not in str(raised.value)
