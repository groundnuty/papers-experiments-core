"""Which attempt a context runs, and whether the attempt or the process a token names is still live (C33)."""

import subprocess
import sys
from pathlib import Path

import pytest
from scheduler_support import FakeExecutor, build

from expcore.campaign import liveness
from expcore.campaign.healthgate import Health

SMALL = {"tasks": ("task-a",), "seeds": (1,)}


def reaped() -> str:
    """The token of a process that has exited and been reaped, in this boot, as `lease.process_owner` names one."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    return f"{boot}:{child.pid}:0"


@pytest.fixture
def other():
    """Another process that still runs, as a token names it by its pid alone."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield f"pid:{child.pid}"
    child.kill()
    child.wait()


def test_c33_an_attempt_is_live_while_its_store_says_running_and_ended_once_it_holds_a_result():
    """C33: inside `running` the context names its attempt; the token reads live while the campaign's store says the
    row is running, and ended the moment it says anything else (finished, set aside, interrupted) or has no row.
    Outside it the context names no attempt, and a hold names this process alone."""
    rows = {7: "running"}
    campaign = liveness.register(rows.get)
    try:
        assert liveness.current() is None
        assert liveness.owner() == {"process": liveness.this_process()}
        with liveness.running(campaign, 7) as token:
            assert liveness.current() == token == liveness.owner()
            assert token == {"process": liveness.this_process(), "campaign": campaign, "attempt_id": 7}
            assert liveness.state(token) == liveness.LIVE
        assert liveness.current() is None
        for ended in ("done", "infra_fail", "outage_discarded", "interrupted", "harness_error"):
            rows[7] = ended
            assert liveness.state(token) == liveness.ENDED
        del rows[7]
        assert liveness.state(token) == liveness.ENDED
    finally:
        liveness.unregister(campaign)


def test_c33_an_attempt_of_a_campaign_this_process_runs_no_more_reads_ended():
    """C33: once the campaign's store is taken back, which the scheduler does as it closes, its attempts are over."""
    campaign = liveness.register(lambda attempt_id: "running")
    with liveness.running(campaign, 1) as token:
        assert liveness.state(token) == liveness.LIVE
    liveness.unregister(campaign)
    assert liveness.state(token) == liveness.ENDED


def test_c33_a_token_whose_process_is_gone_reads_ended_and_one_of_another_live_process_reads_live(other):
    """C33: a process that is gone ends whatever it held, an attempt or not; another process that still runs reads
    live, since this process cannot read its store; a token that cannot be read, or a store that fails, is unknown."""
    gone = reaped()
    assert liveness.state({"process": gone}) == liveness.ENDED
    assert liveness.state({"process": gone, "campaign": "c", "attempt_id": 3}) == liveness.ENDED
    assert liveness.state({"process": other}) == liveness.LIVE
    assert liveness.state({"process": other, "campaign": "c", "attempt_id": 3}) == liveness.LIVE
    assert liveness.state({"process": liveness.this_process()}) == liveness.LIVE
    for unreadable in (None, "pid:1", {}, {"process": ""}, {"process": liveness.this_process(), "attempt_id": True}):
        assert liveness.state(unreadable) is None

    def broken(attempt_id):
        raise OSError("the store cannot be read")

    campaign = liveness.register(broken)
    try:
        with liveness.running(campaign, 1) as token:
            assert liveness.state(token) is None
    finally:
        liveness.unregister(campaign)


def test_c33_the_executor_and_the_post_run_check_run_inside_the_attempt_and_the_pre_run_check_does_not(tmp_path):
    """C33: the attempt is live from its 'running' row to the store's record of its result: its executor and its
    post-run check see its token, naming its row, and the pre-run check sees none; once the result is stored the token
    reads ended, while the campaign is still open."""
    seen: dict[str, list] = {"checks": [], "executor": []}

    def check():
        seen["checks"].append(liveness.current())
        return Health(True, "fine")

    executor = FakeExecutor()

    async def look(request):
        token = liveness.current()
        seen["executor"].append((token, liveness.state(token)))
        return await executor._run(request, "finish")

    executor.default = look
    with build(tmp_path, executor=executor, plan=SMALL, health=check) as scheduler:
        scheduler.run()
        ids = [row["id"] for row in scheduler.store.q("SELECT id FROM attempts ORDER BY id")]
        assert [token["attempt_id"] for token, _ in seen["executor"]] == ids
        assert {state for _, state in seen["executor"]} == {liveness.LIVE}
        assert seen["checks"][0::2] == [None] * len(ids)  # each pre-run check: outside any attempt
        assert seen["checks"][1::2] == [token for token, _ in seen["executor"]]  # each post-run check: inside its own
        assert {liveness.state(token) for token, _ in seen["executor"]} == {liveness.ENDED}
