"""The reset's `[placement] wait_mode` and `evict_batch` (O01, O07, O46, O51).

`fixed`, the default, takes every wait as before, pinned to a golden capture of the code before the setting; `event`
reads the occupancy at once after the eviction stage's end, since the files' block maps show an eviction and a placement
at once, and keeps the lag for a space with a provider declared never to hold its bytes; the quiet wait is the same in
both. A set of per-file evictions goes in batches, each ended before the next is sent, since one batch of about 15,300
OOM-killed a provider (`test_probe_walk_deadline.py` has the probes' deadline)."""

import dataclasses
import json

import httpx
import pytest
from qos_world import FILES, federation, two_providers
from reset_golden_scenarios import GOLDEN, SCENARIOS, one_space, several_spaces, without_new

from expcore_onedata import resetqos
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.resetspaces import ResetPlan
from expcore_onedata.testing import BETA_ID, DATA, write_platform

HELD = len(FILES) * len(DATA)  # what beta holds of the space at the start
LAG_S = resetqos.occupancy_wait_s(2)  # the occupancy wait for files one directory below the root
SETTLE_S = 5.0  # `two_providers`' transfer_settle_s


def make_client(config, fake) -> OnezoneClient:
    return OnezoneClient(config, http=fake.client_http())


@pytest.fixture
def fake(monkeypatch, ticker):
    return federation(monkeypatch, ticker)


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


def reset_in(mode, client, config, platform_file, ticker, tmp_path, **settings) -> SpaceReset:
    """A reset on the test's clock under `wait_mode` `mode`, finding QoS requirements by the listing."""
    placed = dataclasses.replace(
        load_placement(platform_file, config).settings, qos_scan="listing", wait_mode=mode, **settings
    )
    p = Provisioner(
        client,
        config,
        placed,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    return SpaceReset(p, now=ticker.clock)


# -- fixed: today's waits and records -------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", SCENARIOS)
def test_o46_fixed_mode_takes_todays_waits_records_and_calls(monkeypatch, tmp_path, kind):
    """O46, O51: under the default `fixed`, every sleep, every provider call and the whole record of one space evicting
    files (a job cancelled), one evicting a tree with the quiet check skipped, and three spaces reset as one are those
    the code made before the wait modes existed; the record only gains the waits' records and each space's mode."""
    if kind == "spaces":
        shape = several_spaces(monkeypatch, tmp_path, make_client, load_platform)
    else:
        shape = one_space(monkeypatch, tmp_path, make_client, load_platform, kind)
    assert without_new(shape) == json.loads(GOLDEN.read_text(encoding="utf-8"))[kind]
    spaces = shape["record"]["spaces"] if kind == "spaces" else [shape["record"]]
    assert {(space["wait_mode"], space["shared_waits"]) for space in spaces} == {("fixed", kind == "spaces")}


# -- event: the occupancy at once, never_holds and the quiet wait as before -----------------------------------------
def test_o46_event_mode_reads_the_occupancy_at_once_after_the_eviction(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the files' block maps show the eviction at once, so the occupancy is read with no wait after the eviction
    stage's end; the counter, read before its lag, still credits beta, and its event says so (`before_lag`)."""
    reset = reset_in("event", client, config, platform_file, ticker, tmp_path)
    record = reset.run({"beta": ["study"]}, qos_rights=True)
    occupancy = record["stages"]["occupancy"]
    assert occupancy["waited_s"] == 0.0
    assert occupancy["files_bytes"] == {client.provider_label("beta"): 0}
    [wait] = occupancy["wait_records"]
    assert (wait["why"], wait["bound_s"], wait["observed_s"], wait["ended"]) == ("block_maps", 0.0, 0.0, "event")
    assert occupancy["events"]
    assert all(event["before_lag"] for event in occupancy["events"])
    assert record["wait_mode"] == "event"


def test_o46_event_mode_keeps_the_lag_for_a_space_with_a_provider_declared_never_to_hold(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: a provider declared never to hold is skipped on its counter alone, which lags, so the occupancy still waits
    the lag from the eviction stage's end."""
    reset = reset_in("event", client, config, platform_file, ticker, tmp_path)
    record = reset.run(None, {"beta": HELD}, never_holds=["beta"], qos_rights=False)
    occupancy = record["stages"]["occupancy"]
    assert occupancy["waited_s"] == LAG_S
    wait = occupancy["wait_records"][0]
    assert (wait["why"], wait["bound_s"], wait["observed_s"], wait["ended"]) == (
        "never_holds_counter",
        LAG_S,
        LAG_S,
        "bound",
    )
    assert not any("before_lag" in event and event["before_lag"] for event in occupancy["events"])


def test_o46_event_mode_still_fails_an_earlier_runs_replication_that_ended_before_the_reset(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: read at once, the occupancy is the files' block maps, which show a replication of an earlier run four
    directories down at once: beta holds bytes beyond what the reset allows, with the quiet check skipped."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    late = fake.add_file("a/b/c/d/late.bin", DATA)
    client.get(f"/data/{fake.id_of(FILES[0])}/distribution", provider="alpha")  # what changes after this lags
    fake.blocks[(late, BETA_ID)] = [[0, len(DATA)]]
    reset = reset_in("event", client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage.*beyond what the reset allows"):
        reset.run(None, qos_rights=False)


@pytest.mark.parametrize("mode", ["fixed", "event"])
def test_o33_the_quiet_wait_and_its_late_look_are_the_same_in_both_modes(
    client, config, platform_file, ticker, tmp_path, fake, mode
):
    """O33, O46: the quiet stage waits its lag from T and looks at the listings `transfer_settle_s` after its reading,
    whatever the mode."""
    reset = reset_in(mode, client, config, platform_file, ticker, tmp_path)
    quiet = reset.run({"beta": ["study"]}, qos_rights=True)["stages"]["quiet"]
    assert (quiet["nominal_wait_s"], quiet["waited_s"], quiet["listings_after_s"]) == (LAG_S, LAG_S, SETTLE_S)
    lag, look = quiet["wait_records"]
    assert (lag["why"], lag["bound_s"], lag["observed_s"], lag["ended"]) == ("quiet_lag", LAG_S, LAG_S, "bound")
    assert (look["why"], look["bound_s"], look["observed_s"]) == ("late_look", SETTLE_S, SETTLE_S)


def test_o51_event_mode_shares_each_wait_once_across_spaces(monkeypatch, tmp_path):
    """O51: three spaces in event mode, the first with a provider declared never to hold: one settle, one occupancy
    wait (the first space's lag, which the others share) and one quiet wait and look cover all three, each space's
    record saying its waits were shared."""
    plans = [
        ResetPlan({"beta": ["study"]}, qos_rights=True, never_holds=["beta"] if index == 0 else ())
        for index in range(3)
    ]
    shape = several_spaces(monkeypatch, tmp_path, make_client, load_platform, plans, wait_mode="event")
    record = shape["record"]
    assert record["shared_waits"] == {
        "transfers": {"count": 1, "seconds": SETTLE_S},
        "occupancy": {"count": 1, "seconds": LAG_S - 2 * 2.0},  # from the last eviction's end: each took 2 s
        "quiet": {"count": 2, "seconds": LAG_S + SETTLE_S},
    }
    whys = [space["stages"]["occupancy"]["wait_records"][0]["why"] for space in record["spaces"]]
    assert whys == ["never_holds_counter", "block_maps", "block_maps"]
    assert all(space["shared_waits"] for space in record["spaces"])


# -- eviction batches ------------------------------------------------------------------------------------------------
def watch_posts(fake, fail_at: int | None = None) -> list[int]:
    """Before each eviction the fake is sent, the count of evictions still running; the one numbered `fail_at` is
    answered 500."""
    live: list[int] = []
    original = fake._schedule

    def schedule(request, via):
        live.append(sum(1 for job in fake.transfers.values() if job.kind == "eviction" and not job.ended))
        if fail_at is not None and len(live) == fail_at:
            return fake._error(500, "internalServerError")
        return original(request, via)

    fake._schedule = schedule
    return live


def test_o01_evictions_go_in_batches_each_ended_before_the_next(client, config, platform_file, ticker, tmp_path, fake):
    """O01: with `evict_batch` 2, six files leave beta in three batches; no batch is sent while an eviction of the last
    is still running, and the stage records the batches and the pauses between them."""
    live = watch_posts(fake)
    reset = reset_in("fixed", client, config, platform_file, ticker, tmp_path, evict_batch=2, concurrency=1)
    eviction = reset.run({"beta": FILES}, qos_rights=False)["stages"]["eviction"]
    assert live == [0, 1, 0, 1, 0, 1]  # at most two at once, and none of a batch left when the next begins
    provider = eviction["providers"][client.provider_label("beta")]
    assert (provider["files"], provider["transfers"], provider["batches"]) == (6, 6, 3)
    assert eviction["waits"]["batch_pause"]["count"] == 2
    assert all(fake.held(path, BETA_ID) == [] for path in FILES)


def test_o01_a_batch_that_fails_fails_the_reset_and_no_later_batch_is_sent(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O01: the second batch's first eviction is answered 500; the eviction stage fails as the whole set's eviction
    did, and the third batch is never sent."""
    live = watch_posts(fake, fail_at=3)
    reset = reset_in("fixed", client, config, platform_file, ticker, tmp_path, evict_batch=2, concurrency=1)
    with pytest.raises(ResetFailed) as failed:
        reset.run({"beta": FILES}, qos_rights=False)
    assert failed.value.stage == "eviction"
    assert len(live) == 3
    assert fake.held(FILES[5], BETA_ID) != []


def test_o01_a_batch_whose_answer_was_lost_waits_for_no_job_listed_before_the_next(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O01, O09: with `evict_batch` 1, the provider takes the first eviction and its answer is lost, so there is no id
    to wait on; the job runs on until the listing has shown it once more. The next batch is sent only once no job is
    listed running, never beside it, and the lost eviction is not sent again."""
    live, lost = [], []
    schedule, page = fake._schedule, fake._transfer_page

    def posted(request, via):
        live.append(sum(1 for job in fake.transfers.values() if job.kind == "eviction" and not job.ended))
        answer = schedule(request, via)
        if len(live) == 1:
            lost.append(answer.json()["transferId"])
            raise httpx.ReadError("simulated", request=request)
        return answer

    def listing(query, provider, include):
        shown = page(query, provider, include)
        if lost and lost[0] in shown.json()["transfers"] and not fake.transfers[lost[0]].ended:
            fake.end_transfer(lost[0])  # listed running this once: it ends before the next listing
        return shown

    fake._schedule, fake._transfer_page = posted, listing
    reset = reset_in("fixed", client, config, platform_file, ticker, tmp_path, evict_batch=1, concurrency=1)
    eviction = reset.run({"beta": FILES}, qos_rights=False)["stages"]["eviction"]
    assert live == [0] * len(FILES)
    assert eviction["providers"][client.provider_label("beta")]["unanswered"] == 1
    assert eviction["waits"]["listed_running"]["count"] >= 1
