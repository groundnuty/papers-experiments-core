"""A preallocated ingest's start held back by its baseline's mark costs no attempt, and the campaign holds instead of
spinning; a mark no live attempt owns is unhealthy at once, until a recovery (O45). Through the real scheduler, its
store and the ingest's health check, over the fake federation."""

import itertools
import json
import subprocess
import sys
import threading
import time

import pytest
from campaign_support import infra, make_schema
from scheduler_support import FakeExecutor, attempts_of, build, key_of, settings_for

from expcore.campaign import liveness
from expcore.campaign.attempts import classify
from expcore.campaign.factors import FactorSchema
from expcore.campaign.healthgate import META as HEALTH_META
from expcore.campaign.healthgate import parse
from expcore.errors import InfraError, uncharged_kinds
from expcore_onedata import ingesthealth
from expcore_onedata.ingest import ProgressiveIngest
from expcore_onedata.ingesthealth import baseline_health, last_marks
from expcore_onedata.ingestplan import load_ingest_plan
from expcore_onedata.sandbox.kinds import INGEST_FAILURE, INGEST_HELD
from expcore_onedata.testing import ALPHA_ID, BETA_ID, with_placement, write_platform

DAY = bytes((i * 31 + i // 97) % 256 for i in range(10240))
DAY_PATH = "records/day-13.csv"
PREFIX = 1024
ROW = {
    "label": "day-13",
    "path": DAY_PATH,
    "source": "day-13.csv",
    "at_s": 30,
    "baseline": "complete",
    "prefix": PREFIX,
    "parts": 3,
    "over_s": 60,
}
CHANGES = ("POST", "PUT", "DELETE")
SCHEMA = make_schema()
ONE = FactorSchema(SCHEMA.declared, SCHEMA.order, {"api-none": SCHEMA.conditions["api-none"]})  # a plan of one run
KEY = key_of(ONE, "api-none")
PLAN = {"tasks": ("task-a",), "seeds": (1,)}


def two_providers(data):
    with_placement(data)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture
def fake(fake):
    fake.add_provider(BETA_ID, "beta.test")
    fake.add_file(DAY_PATH, DAY)
    return fake


@pytest.fixture
def journal(tmp_path):
    return tmp_path / "journal.jsonl"


@pytest.fixture
def make(provisioner, config, tmp_path, journal):
    """Build an instrument over a preallocated plan of the one day; every one shares the journal and the change log."""
    staging = tmp_path / "staged"
    staging.mkdir()
    (staging / "day-13.csv").write_bytes(DAY)
    body = "\n[[ingest.files]]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in ROW.items())
    path = tmp_path / "ingest.toml"
    path.write_text('[ingest]\nstaging = "staged"\nmode = "preallocated"\n' + body, encoding="utf-8")
    plan = load_ingest_plan(path, config)
    built = []

    def build_one():
        ingest = ProgressiveIngest(provisioner, plan, journal=journal, max_wait_s=0.001)
        built.append(ingest)
        return ingest

    yield build_one
    for ingest in built:
        ingest.stop()


@pytest.fixture
def other():
    """Another process that still runs: the owner of a run in progress elsewhere."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield child
    child.kill()
    child.wait()


def held_by(make, monkeypatch, process):
    """An instrument whose start holds the baseline open, as the process named `process` (a run in progress)."""
    holder = make()
    with monkeypatch.context() as patch:
        patch.setattr(ingesthealth, "owner", lambda: {"process": process})
        holder.start({})
    return holder


def ingest_run(make, executor, *, drop=None):
    """The study's executor: start the ingest, take the first turn and finish it, then run the loop; `drop(n)` says
    whether the n-th attempt drops its instrument instead, with no finish and no reset."""
    calls = []

    async def run(request):
        calls.append(request)
        ingest = make()
        ingest.start({"run_key": request.run_key, "attempt": request.identity.attempt})
        ingest.at_first_turn({"clock_s": 0.0})
        if drop is not None and drop(len(calls)):
            ingest.stop()
        else:
            ingest.finish()
            ingest.check()
        return await executor._run(request, "finish")

    return run


def until(predicate, timeout_s=20.0):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "the campaign did not reach the expected state"
        time.sleep(0.005)


def test_o45_a_start_on_a_held_baseline_is_held_back_as_a_hold_that_costs_no_attempt(make, fake, monkeypatch, other):
    """O45: another live process holds the baseline open, a run in progress: a start is held back with nothing sent,
    as `ingest_held`, a kind that costs no attempt, where it was an `ingest_failure`, charged, before; its post-run
    check refuses with the same hold, so an executor that swallowed the start's error is not charged either."""
    held_by(make, monkeypatch, f"pid:{other.pid}")
    since = len(fake.provider_calls)
    refused = make()
    with pytest.raises(InfraError, match="held open by a live attempt") as held:
        refused.start({})
    assert held.value.kind == INGEST_HELD != INGEST_FAILURE
    assert INGEST_HELD in uncharged_kinds()
    assert [c for c in fake.provider_calls[since:] if c[1] in CHANGES] == []
    assert classify(infra(held.value.kind, held.value.detail)) == ("infra_fail", False, False)
    assert classify(infra(INGEST_FAILURE)).charged is True
    with pytest.raises(InfraError) as checked:
        refused.check()
    assert checked.value.kind == INGEST_HELD


def test_o45_a_held_start_costs_the_run_no_attempt_however_often_and_runs_once_the_baseline_is_reset(
    tmp_path, make, monkeypatch, other
):
    """O45: with no health check to hold the run before its attempt, each attempt's start is held back while another
    live process holds the baseline: set aside uncharged, under the same attempt number, three times, more than
    `max_attempts`; once that run's reset marks the baseline whole, the start goes through and the run is done."""
    holder = held_by(make, monkeypatch, f"pid:{other.pid}")
    executor = FakeExecutor()
    started = ingest_run(make, executor)
    attempts = []

    async def run(request):
        attempts.append(request)
        if len(attempts) == 4:  # the other run ends: its reset marks the baseline whole
            holder.at_first_turn({"clock_s": 0.0})
            holder.finish()
        return await started(request)

    executor.default = run
    with build(tmp_path, schema=ONE, executor=executor, plan=PLAN, settings=settings_for(max_attempts=2)) as scheduler:
        summary = scheduler.run()
        assert attempts_of(scheduler.store, KEY) == [("infra_fail", 0, 1)] * 3 + [("done", 0, 1)]
        kinds = [r["infra_kind"] for r in scheduler.store.q("SELECT infra_kind FROM attempts ORDER BY id")]
        assert kinds == [INGEST_HELD] * 3 + [None]
        assert scheduler.store.charged_attempts(KEY) == 0
        assert summary.statuses == {"done": 1}


def test_o45_the_campaign_holds_while_a_live_attempt_holds_the_baseline_and_asks_again_no_sooner_than_recheck_s(
    tmp_path, make, monkeypatch, other, journal
):
    """O45: given the ingest's journal, the pre-run check is deferred while another live process holds the baseline:
    no attempt begins, and the run is asked again only after `recheck_s`, never in a loop; once that run's reset marks
    the baseline whole the check passes and the run starts at its first attempt."""
    holder = held_by(make, monkeypatch, f"pid:{other.pid}")
    calls = []

    def check():
        verdict = baseline_health([journal])
        calls.append((time.monotonic(), verdict.deferred))
        return verdict

    executor = FakeExecutor()
    executor.default = ingest_run(make, executor)
    settings = settings_for(recheck_s=0.05)
    with build(tmp_path, schema=ONE, executor=executor, plan=PLAN, health=check, settings=settings) as scheduler:
        running = threading.Thread(target=scheduler.run)
        running.start()
        until(lambda: sum(deferred for _, deferred in calls) >= 3)
        assert scheduler.store.q("SELECT COUNT(*) AS n FROM attempts")[0]["n"] == 0  # held: no attempt began
        holder.at_first_turn({"clock_s": 0.0})
        holder.finish()
        running.join(20)
        assert not running.is_alive()
        assert attempts_of(scheduler.store, KEY) == [("done", 0, 1)]
    deferred = [moment for moment, was in calls if was]
    waits = [later - earlier for earlier, later in itertools.pairwise(deferred)]
    assert waits
    assert min(waits) >= 0.05  # each deferred check was followed by a wait of recheck_s, not by another at once


def test_o45_an_attempt_that_ends_without_its_reset_leaves_the_baseline_unhealthy_at_once_until_a_recovery(
    tmp_path, make, fake, journal
):
    """O45: the first attempt drops its instrument, with no finish and no reset, and its process runs on: its own
    post-run check fails, its ingest having ended without its reset, and the moment the store holds its result the
    open mark it left is orphaned, with no wait for the run's ceiling. The next pre-run check fails, closing the gate
    and charging no run, so nothing runs until a recovery marks the baseline whole; then the run goes again and is
    done."""
    executor = FakeExecutor()
    executor.default = ingest_run(make, executor, drop=lambda n: n == 1)
    settings = settings_for(recheck_s=0.05)
    health = lambda: baseline_health([journal])  # noqa: E731
    with build(tmp_path, schema=ONE, executor=executor, plan=PLAN, health=health, settings=settings) as scheduler:
        running = threading.Thread(target=scheduler.run)
        running.start()
        until(lambda: scheduler.gate.closed)
        store = scheduler.store
        assert attempts_of(store, KEY) == [("infra_fail", 1, 1)]  # the attempt that dropped its ingest, charged
        mark = last_marks(journal)["spaces[0]"]
        assert (mark["state"], mark["owner"]["attempt_id"]) == ("open", store.q("SELECT id FROM attempts")[0]["id"])
        assert liveness.state(mark["owner"]) == liveness.ENDED
        verdict = baseline_health([journal])
        assert (verdict.ok, verdict.deferred, verdict.charge) == (False, False, False)
        assert "its attempt has ended" in verdict.detail
        time.sleep(0.2)  # the dispatcher's own checks keep failing: nothing runs
        assert len(attempts_of(store, KEY)) == 1
        assert make().recover()["ok"]
        running.join(20)
        assert not running.is_alive()
        assert attempts_of(store, KEY) == [("infra_fail", 1, 1), ("done", 0, 2)]
        outage = parse(store.get_meta(HEALTH_META))[0]
        assert (outage["charged"], outage["how"]) == (False, "check_passed")
    assert last_marks(journal)["spaces[0]"]["state"] == "whole"
    assert fake.held(DAY_PATH, ALPHA_ID) == [[0, len(DAY)]]  # the second run's reset left the day whole
