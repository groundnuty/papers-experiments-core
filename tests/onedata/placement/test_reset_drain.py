"""The reset's removal of the run's own QoS requirements, in order: each waited on until it is fulfilled or impossible
on every provider, deleted, and its deletion waited on until every provider has processed it, before the files are
settled and evicted; and the residual it leaves, an add-then-remove whose cancel missed (O33)."""

import pytest
from qos_world import federation, provisioner_with, two_providers

from expcore.config.loading import ConfigError
from expcore_onedata import resetremove
from expcore_onedata.rangemap import load_placement
from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA, with_placement, write_platform
from expcore_onedata.testing.fake_data import ROOT_ID

OTHER = [f"other/f-{i}.bin" for i in range(6)]  # alpha holds them, beta does not
PLAN = {"beta": ["study", "other"]}


@pytest.fixture
def fake(monkeypatch, ticker):
    world = federation(monkeypatch, ticker)
    for path in OTHER:
        world.add_file(path, DATA)
    return world


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture
def provisioner(client, config, platform_file, ticker, tmp_path, fake):
    return provisioner_with(client, config, platform_file, ticker, tmp_path)


def test_o33_a_requirement_still_pending_is_waited_on_then_deleted(provisioner, fake, ticker):
    """O33: the agent's requirement on `other` is still replicating into beta at the reset, its files due 30 s on:
    it is `pending`, so the reset reads it until it is `fulfilled` on both providers, and only then deletes it. Its
    replication has landed whole, the eviction removes it, and the record has the wait."""
    fake.qos_replicates_to, fake.qos_land_after_s = BETA_ID, 30.0
    start, qid = ticker.now, fake.add_qos("other")
    record = SpaceReset(provisioner, now=ticker.clock).run(PLAN)
    assert fake.qos_removed[qid][ALPHA_ID] >= start + 30.0  # deleted once fulfilled, never before
    assert sorted(fake.nodes[f].name for _, _, f, _ in fake.qos_landed) == sorted(p.split("/")[1] for p in OTHER)
    stage = record["stages"]["qos"]
    assert stage["removed"] == 1
    assert stage["fulfil_s"] >= 30.0
    assert stage["impossible"] == 0
    assert all(fake.held(path, BETA_ID) == [] for path in OTHER)


def test_o33_a_requirement_that_never_settles_fails_the_reset_at_the_bound_and_is_not_deleted(
    provisioner, fake, ticker
):
    """O33: a requirement that stays `pending` is never deleted while its replication may run: the reset fails at its
    qos stage once `qos_fulfil_timeout_s` has passed, and the requirement is still there for the next attempt."""
    qid = fake.add_qos("other")
    fake.qos_status_of[qid] = "pending"
    with pytest.raises(ResetFailed, match=r"qos stage.*still pending") as caught:
        SpaceReset(provisioner, now=ticker.clock).run(PLAN)
    bound = provisioner.settings.qos_fulfil_timeout_s
    assert bound == 120.0
    assert bound <= caught.value.record["stages"]["qos"]["seconds"] <= bound + provisioner.settings.poll_s
    assert qid in fake.qos
    assert fake.qos_removed == {}


def test_o33_an_impossible_requirement_is_settled_and_deleted(provisioner, fake, ticker):
    """O33: `impossible` is no storage able to hold the copy, so nothing replicates: it does not hold the deletion."""
    qid = fake.add_qos("other")
    fake.qos_status_of[qid] = "impossible"
    stage = SpaceReset(provisioner, now=ticker.clock).run(PLAN)["stages"]["qos"]
    assert (stage["removed"], stage["impossible"]) == (1, 1)
    assert qid not in fake.qos


def test_o33_a_requirement_a_provider_has_not_received_yet_is_waited_for_there(provisioner, fake, ticker):
    """O33: added through beta just before the reset, the requirement reaches alpha 8 s later: alpha answers that it is
    not there until then, which is not settled, so the reset waits for it before deleting it through beta."""
    fake.qos_sync_s = 8.0
    start, qid = ticker.now, fake.add_qos("other", through=BETA_ID)
    record = SpaceReset(provisioner, now=ticker.clock).run(PLAN)
    assert record["stages"]["qos"]["fulfil_s"] >= 8.0
    assert fake.qos_removed[qid][BETA_ID] >= start + 8.0


def test_o33_the_barrier_waits_for_the_slowest_provider(provisioner, fake, ticker):
    """O33: deleted through alpha, the requirement is gone there at once, and beta processes the deletion 25 s later.
    The barrier reads the summary of its entry and its record on both until beta's no longer show it, past the 20 s
    `settle_timeout_s` of these settings, since the barrier has its own bound; the record has its wait."""
    fake.qos_sync_by_provider = {BETA_ID: 25.0}
    qid = fake.add_qos("other")
    record = SpaceReset(provisioner, now=ticker.clock).run(PLAN)
    stage = record["stages"]["qos"]
    assert stage["barrier_s"] >= 25.0
    reached = fake.qos_removed[qid]
    assert reached[BETA_ID] - reached[ALPHA_ID] == 25.0
    summaries = {c[0] for c in fake.provider_calls if c[2] == f"/data/{fake.id_of('other')}/qos/summary"}
    assert summaries == {ALPHA_ID, BETA_ID}
    assert qid not in fake.qos


def test_o33_a_deletion_that_never_reaches_a_provider_fails_the_reset_at_the_barriers_bound(provisioner, fake, ticker):
    """O33: beta never processes the deletion: the barrier fails the reset at its qos stage once `BARRIER_BOUND_S` has
    passed, before any file is settled or evicted."""
    fake.qos_sync_by_provider = {BETA_ID: 1e9}
    fake.add_qos("other")
    with pytest.raises(ResetFailed, match=r"qos stage.*still listed") as caught:
        SpaceReset(provisioner, now=ticker.clock).run(PLAN)
    assert list(caught.value.record["stages"]) == ["qos"]
    assert resetremove.BARRIER_BOUND_S == 60.0
    seconds = caught.value.record["stages"]["qos"]["seconds"]
    assert resetremove.BARRIER_BOUND_S <= seconds <= resetremove.BARRIER_BOUND_S + 2 * provisioner.settings.poll_s


def test_o33_a_replication_still_running_where_the_deletion_has_not_arrived_is_stopped_before_the_eviction(
    provisioner, fake, ticker
):
    """O33: the requirement was read `fulfilled` (alpha's copy satisfied it); as it is deleted through alpha, a change
    of a file's location starts a reconciliation into beta (`replica_dbsync_hook.erl:95,370`), a file every 5 s. Beta
    processes the deletion 22 s later and goes on replicating until then. The barrier waits for beta, so the last file
    lands before the settle and the eviction; nothing lands after, beta ends holding none of it, and the reset
    passes."""
    qid = fake.add_qos("other")
    original, evictions = fake._qos_remove, []

    def removed(target, through):
        original(target, through)
        fake.qos_replicates_to, fake.qos_land_after_s, fake.qos_land_every_s = BETA_ID, 5.0, 5.0
        fake.qos_reconcile(target)

    scheduled = fake._schedule

    def schedule(request, via):
        evictions.append(ticker.now)
        return scheduled(request, via)

    fake._qos_remove, fake._schedule = removed, schedule
    fake.qos_sync_by_provider = {BETA_ID: 22.0}
    record = SpaceReset(provisioner, now=ticker.clock).run(PLAN)
    reached = fake.qos_removed[qid][BETA_ID]
    landed = [when for when, *_ in fake.qos_landed]
    assert len(landed) == 4  # due at 5, 10, 15 and 20 s; the files due at 25 and 30 s are cancelled
    assert max(landed) < reached <= min(evictions)
    assert record["stages"]["qos"]["barrier_s"] >= 22.0
    assert all(fake.held(path, BETA_ID) == [] for path in OTHER)


def test_o33_the_residual_an_add_then_remove_whose_cancel_missed_lands_after_t_and_the_next_reset_catches_it(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33, the limit pinned: during the run the agent adds a requirement on `late` through beta and removes it there;
    the cancel misses the file in flight, which still replicates whole. The reset finds no requirement, and the file
    lands after T, so the reset passes with beta holding nothing: it is not caught as of T. It lands, and the next
    start's reset fails at its occupancy stage."""
    late = fake.add_file("late/big.bin", DATA)
    fake.qos_replicates_to, fake.qos_land_after_s, fake.qos_missed_cancel = BETA_ID, 1000.0, True
    qid = fake.add_qos("late", through=BETA_ID)
    client.mutate("DELETE", f"/qos_requirements/{qid}", provider="beta")
    first = provisioner_with(client, config, platform_file, ticker, tmp_path)
    record = SpaceReset(first, now=ticker.clock).run(PLAN, qos_rights=True)
    assert record["stages"]["qos"]["found"] == 0
    assert record["stages"]["quiet"]["physical_bytes"]["providers[1]"] == 0
    ticker.sleep(1000.0)
    client.get(f"/data/{ROOT_ID}/distribution", provider="alpha")  # it lands at this call
    assert fake.blocks[(late, BETA_ID)] == [[0, len(DATA)]]
    ticker.sleep(60.0)  # past the occupancy's lag
    second = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage .*providers\[1\] hold"):
        SpaceReset(second, now=ticker.clock).run(PLAN, qos_rights=True)


def test_o33_the_fulfilment_bound_is_a_placement_setting_of_120_s_by_default_and_never_negative(
    config, platform_file, tmp_path
):
    """O33: `[placement] qos_fulfil_timeout_s` bounds the wait for a requirement to be fulfilled: 120 s unless set."""
    assert load_placement(platform_file, config).settings.qos_fulfil_timeout_s == 120.0
    (tmp_path / "set").mkdir()
    longer = write_platform(tmp_path / "set", lambda d: with_placement(d, qos_fulfil_timeout_s=300))
    assert load_placement(longer, config).settings.qos_fulfil_timeout_s == 300.0
    (tmp_path / "bad").mkdir()
    negative = write_platform(tmp_path / "bad", lambda d: with_placement(d, qos_fulfil_timeout_s=-1))
    with pytest.raises(ConfigError, match=r"key 'qos_fulfil_timeout_s'"):
        load_placement(negative, config)
