"""A reset reads and checks every provider, reads block maps and physical sizes only while no transfer runs, and acts
on its own space alone (O01, O07, O08, O09, O11, O12)."""

import pytest
from fake_reset import FakeReset

from expcore_onedata.eviction import evict_ids
from expcore_onedata.provision import Refused
from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.testing import (
    ADMIN_ENV,
    ALPHA_ID,
    BASELINE,
    BETA_ID,
    DATA,
    SPACE_NAME,
    USERS,
    with_placement,
    write_platform,
)

FILES = [f"study/part-{i:02d}.bin" for i in range(4)]


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    """The federation with a reset's calls: alpha holds the data, beta supports the space too."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)
    return federation


def document(data):
    with_placement(data, transfer_settle_s=5.0, transfer_settle_polls=3, concurrency=4)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, document)


@pytest.fixture
def stocked(fake) -> FakeReset:
    """Four files held by alpha and replicated whole to beta."""
    for path in FILES:
        fake.blocks[(fake.add_file(path, DATA), BETA_ID)] = [[0, len(DATA)]]
    return fake


def test_o11_a_requirement_only_one_provider_has_seen_is_found_there_removed_and_shown_gone_everywhere(
    provisioner, stocked
):
    """O11: a requirement the data provider has not seen yet is still in the space: the walk on the other provider
    finds it, it is removed there, and every provider then shows it gone."""
    qid = stocked.add_qos(FILES[2])
    stocked.qos_hidden_on[qid] = {ALPHA_ID}
    record = SpaceReset(provisioner).run({"beta": ["study"]})
    assert stocked.qos == {}
    assert record["stages"]["qos"]["removed"] == 1
    removals = [c for c in stocked.provider_calls if c[1] == "DELETE" and c[2] == f"/qos_requirements/{qid}"]
    assert [c[0] for c in removals] == [BETA_ID]


def test_o11_a_job_both_providers_list_is_cancelled_and_waited_for_on_each(provisioner, stocked, ticker):
    """O11: a running job is cancelled on every provider that lists it, and its end is read on each."""
    stocked.add_file("other/twice.bin", DATA)
    tid = stocked.add_transfer("replication", "other/twice.bin", BETA_ID, scheduled=ticker.now, ended=False)
    SpaceReset(provisioner).run({"beta": ["study"]})
    cancels = {c[0] for c in stocked.provider_calls if c[1] == "DELETE" and c[2] == f"/transfers/{tid}"}
    assert cancels == {ALPHA_ID, BETA_ID}
    reads = {c[0] for c in stocked.provider_calls if c[1] == "GET" and c[2] == f"/transfers/{tid}"}
    assert reads == {ALPHA_ID, BETA_ID}


def test_o07_the_transfer_stage_needs_the_settle_count_of_clean_observations_in_a_row(provisioner, stocked):
    """O07: a quiet space still takes `transfer_settle_polls` observations (three here), each of both providers'
    waiting and ongoing listings, before the transfer stage passes."""
    record = SpaceReset(provisioner).run({"beta": ["study"]})
    assert record["stages"]["transfers"]["observations"] == 3


def test_o01_a_job_that_starts_during_the_eviction_fails_the_reset_before_any_block_map_is_read(provisioner, stocked):
    """O01: a replication into the evicted provider starts as the eviction ends; the listings show it running, so the
    block maps, which it may still change, are never read and the reset fails at its eviction stage. The run declares
    no QoS right, so the quiet check's read of what the provider held before the eviction (made while no job runs) is
    not in play."""
    stocked.spawn_on_evict = BETA_ID  # its record is never read, so the fake never ends it: it runs on
    with pytest.raises(ResetFailed, match="eviction stage") as caught:
        SpaceReset(provisioner).run({"beta": ["study"]}, qos_rights=False)
    assert "still waiting or ongoing" in str(caught.value)
    assert not [c for c in stocked.provider_calls if c[2].endswith("/distribution")]


def test_o11_without_an_eviction_plan_the_reset_still_shows_that_only_the_owner_holds_data(provisioner, stocked):
    """O11: with nothing to evict, a provider other than the data provider that holds bytes of the space fails the
    reset at its occupancy stage, by its physical size; bytes the caller allows pass, and are recorded."""
    with pytest.raises(ResetFailed, match=r"occupancy stage .*providers\[1\] hold"):
        SpaceReset(provisioner).run()
    held = len(FILES) * len(DATA)
    record = SpaceReset(provisioner).run(allowed={"beta": held})
    assert record["stages"]["occupancy"]["physical_bytes"] == {"providers[0]": held, "providers[1]": held}


def test_o08_a_physical_size_that_is_unknown_fails_the_reset_rather_than_pass_as_empty(provisioner, stocked):
    """O08: a space without directory statistics answers no physical size; that is unknown, never zero, so the reset
    fails at its occupancy stage even after the eviction emptied the provider."""
    stocked.dir_stats_disabled = True
    with pytest.raises(ResetFailed, match="occupancy stage"):
        SpaceReset(provisioner).run({"beta": ["study"]})


def test_o09_a_reset_first_checks_the_space_is_the_configured_one_and_changes_nothing_when_it_is_not(
    provisioner, stocked
):
    """O09: the space renamed within the prefixes is not the configured one: the reset is refused before its first
    stage, and the requirement in it is not removed."""
    stocked.add_qos(FILES[0])
    stocked.space_name = "study-a-renamed"
    with pytest.raises(Refused, match="not the configured one"):
        SpaceReset(provisioner).run({"beta": ["study"]})
    assert stocked.qos
    assert stocked.provider_calls == []


def test_o09_the_data_provider_is_refused_by_its_id_as_by_its_name_before_anything_is_sent(provisioner, stocked):
    """O09: a bulk eviction or a reset that names the data provider by its Onezone id is refused like one that names
    it by its name, before any request."""
    with pytest.raises(Refused, match="own copy"):
        evict_ids(provisioner, [stocked.id_of(FILES[0])], ALPHA_ID)
    with pytest.raises(Refused, match="own copy"):
        SpaceReset(provisioner).run({ALPHA_ID: ["study"]})
    assert stocked.requests == []


def test_o12_a_path_the_plan_names_that_is_missing_fails_the_reset_without_naming_the_path(provisioner, stocked):
    """O12: the reset's message and record say a planned path is not in the space, never which part of it."""
    with pytest.raises(ResetFailed, match="eviction stage") as caught:
        SpaceReset(provisioner).run({"beta": ["study/missing-dataset-part"]})
    assert "missing-dataset-part" not in str(caught.value)
    assert "missing-dataset-part" not in str(caught.value.record)
