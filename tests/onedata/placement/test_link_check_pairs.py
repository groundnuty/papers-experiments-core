"""O35: the link check tests every ordered (reading provider, source) pair among the providers that share a space, one
probe file per pair, so that a link between two providers that are not the agent's local one cannot stay stuck unseen
(#221, and the slot of 14 Nov 2026, where the pair cedar <- pine stayed stuck after a provider outage)."""

import pytest
from fault_support import SERVICES, faults_table
from link_support import (
    ALPHA_ID,
    BETA_ID,
    BOUND_S,
    GAMMA_ID,
    PROVIDERS,
    add_providers,
    held,
    key_of,
    markers_of,
    pair_of,
    probe_of,
    reads_through,
    run,
    three_providers,
)

from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.linkcheck import check_links, probe_content
from expcore_onedata.linksettings import link_pairs
from expcore_onedata.testing import write_platform
from expcore_onedata.testing.world import with_spaces

ORDERED = [
    ("alpha", "beta"),
    ("alpha", "gamma"),
    ("beta", "alpha"),
    ("beta", "gamma"),
    ("gamma", "alpha"),
    ("gamma", "beta"),
]  # (the reading provider, the source), in the order platform.toml declares the providers
STORES = {**SERVICES, "gamma": "store-three"}  # the Compose service of each provider a restore can concern


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, three_providers)


@pytest.fixture
def fake(fake):
    return add_providers(fake)


def stick(fake, ticker, reader, source, how="hang"):
    """The link of `reader` to `source` is stuck: a read through `reader` of bytes only `source` holds never answers."""
    fake.links[(PROVIDERS[reader], PROVIDERS[source])] = how
    fake.hang_hook = ticker.sleep


def test_o35_every_ordered_pair_is_checked_through_its_reader_with_a_probe_of_its_own(fake, config, client, ticker):
    """O35: three providers sharing a space are six ordered pairs, not two: each is read through its own reader, from a
    probe file that no other pair reads, written through its source, which holds it whole and alone."""
    report = run(config, client, ticker)
    assert report.ok, report.detail
    labels = {name: f"providers[{index}]" for index, name in enumerate(PROVIDERS)}
    assert [(p.local, p.source) for p in report.pairs] == [(labels[r], labels[s]) for r, s in ORDERED]
    size = config.link_check.probe_bytes
    probes = []
    for reader, source in ORDERED:
        pair = pair_of(report, reader, source)
        probe = probe_of(fake, key_of(reader, source))
        probes.append(probe)
        assert pair.ok
        assert pair.provisioned
        assert pair.bytes == size
        assert bytes(fake.nodes[probe].content) == probe_content(key_of(reader, source), size)
        assert held(fake, probe, PROVIDERS[source]) == size
        assert probe in reads_through(fake, PROVIDERS[reader])  # the fetch went through the pair's own reader
        for other, other_id in PROVIDERS.items():
            if other not in (reader, source):  # the source reads its probe back; no third provider reads it
                assert probe not in reads_through(fake, other_id)
    assert len(set(probes)) == len(ORDERED)  # no two pairs share a probe, so no reader's copy serves another's fetch
    assert len(report.to_json()["pairs"]) == len(ORDERED)


def test_o35_every_probe_is_evicted_from_the_reader_that_fetched_it(fake, config, client, ticker):
    """O35: the eviction afterwards is of each reader's own copy, scheduled through that reader, whichever providers
    read: a check that passed leaves every probe held by its source alone."""
    report = run(config, client, ticker)
    assert report.ok
    for reader, source in ORDERED:
        pair = pair_of(report, reader, source)
        probe = probe_of(fake, key_of(reader, source))
        assert pair.evicted is True
        assert pair.evictions
        assert held(fake, probe, PROVIDERS[reader]) == 0
        assert held(fake, probe, PROVIDERS[source]) == config.link_check.probe_bytes
        evictions = [fake.transfers[tid] for tid in pair.evictions]
        assert all(
            t.kind == "eviction" and t.provider_id == PROVIDERS[reader] and t.file_id == probe for t in evictions
        )


@pytest.mark.parametrize(("reader", "source"), [pair for pair in ORDERED if pair[0] != "alpha"])
def test_o35_a_stuck_link_between_providers_other_than_the_local_one_fails_the_check(
    fake, config, client, ticker, reader, source
):
    """O35: the pair cedar <- pine of the live slot, whose reader is not the local provider, was stuck while the local
    provider's own pairs transferred. Every pair of the other readers is checked, so the stuck one is found, named
    by position, with the remedy for its reader; every other pair still transferred, and nothing is evicted."""
    assert run(config, client, ticker).ok  # the first check: every probe is written, fetched and evicted again
    stick(fake, ticker, reader, source)
    report = run(config, client, ticker)
    assert not report.ok
    failed = [p for p in report.pairs if not p.ok]
    assert [(p.local, p.source) for p in failed] == [
        (pair_of(report, reader, source).local, pair_of(report, reader, source).source)
    ]
    labels = list(PROVIDERS)
    stuck = failed[0]
    assert stuck.reason.startswith(
        f"no transfer from providers[{labels.index(source)}] to providers[{labels.index(reader)}] within {BOUND_S:g} s"
    )
    assert stuck.remedy is not None
    assert f"restart the rtransfer link on providers[{labels.index(reader)}]" in stuck.remedy
    assert all(p.ok for p in report.pairs if p is not stuck)
    assert all(p.evicted is None for p in report.pairs)


def test_o35_a_stuck_link_is_found_when_another_reader_fetched_the_same_source_first(fake, config, client, ticker):
    """O35: the false pass a probe shared by every reader of a source would give. Alpha fetches beta's probe before
    gamma does and then holds a copy, so a read through gamma of the same file would be served by alpha while its own
    link to beta is stuck. Each pair has a file of its own, held by its source alone when its fetch starts, so
    gamma's fetch of beta's probe cannot be served by alpha, and the stuck link is found on the first check."""
    stick(fake, ticker, "gamma", "beta")
    report = run(config, client, ticker)
    assert not report.ok
    assert [p.ok for p in report.pairs] == [r != "gamma" or s != "beta" for r, s in ORDERED]
    own = probe_of(fake, key_of("gamma", "beta"))
    assert own != probe_of(fake, key_of("alpha", "beta"))
    assert held(fake, own, ALPHA_ID) == 0
    assert own not in reads_through(fake, ALPHA_ID)


def test_o35_a_check_with_more_pairs_than_workers_prepares_and_fetches_them_at_once(fake, config, client, ticker):
    """O35: the pairs are prepared as well as fetched at most `workers` at once, so a check of every ordered pair takes
    about as long as one of the local provider's pairs did, and its results keep the declared order."""
    report = check_links(config, client, clock=ticker.clock, sleep=ticker.sleep, now=ticker.clock, workers=4)
    assert report.ok, report.detail
    assert [(p.local, p.source) for p in report.pairs] == [
        (f"providers[{list(PROVIDERS).index(r)}]", f"providers[{list(PROVIDERS).index(s)}]") for r, s in ORDERED
    ]


def test_o35_the_retirement_of_one_pairs_probe_leaves_every_other_pairs_files_and_markers_alone(
    fake, config, client, ticker
):
    """O35: a marker names a retired file of one pair, and only that pair reads it back or drops it. Gamma's read of
    beta's probe timed out and no successor could be written, so the marker, written through gamma, alone keeps the
    old file from being read again: the checks of beta's other pairs do not drop it, the next check writes a fresh file
    for gamma and never reads the old one, and no other pair's probe is touched."""
    fake.joins_stuck_jobs = True
    assert run(config, client, ticker).ok
    others = {(r, s): probe_of(fake, key_of(r, s)) for r, s in ORDERED if (r, s) != ("gamma", "beta")}
    fake.links[(GAMMA_ID, BETA_ID)] = "hang"

    def hang(seconds):
        ticker.sleep(seconds)
        fake.fail_creates.add(BETA_ID)  # no successor can be written
        fake.fail_deletes = True  # and the failed file stays

    fake.hang_hook = hang
    failed = pair_of(run(config, client, ticker), "gamma", "beta")
    assert failed.retired is True
    (marker,) = markers_of(fake)
    ((_, old),) = fake.stuck_jobs
    assert old in fake.nodes
    fake.fail_creates.clear()
    fake.fail_deletes = False
    fake.hang_hook = ticker.sleep
    del fake.links[(GAMMA_ID, BETA_ID)]
    calls = len(fake.provider_calls)
    second = run(config, client, ticker)
    assert second.ok, second.detail
    assert old not in reads_through(fake, GAMMA_ID, calls)
    assert pair_of(second, "gamma", "beta").purged == 1  # the passing check deleted the old file
    assert markers_of(fake) == [marker]  # kept until its file is gone, which the next check sees
    assert {pair: probe_of(fake, key_of(*pair)) for pair in others} == others
    assert run(config, client, ticker).ok
    assert markers_of(fake) == []


def test_o35_a_restore_checks_every_ordered_pair_including_a_stuck_one_the_restored_provider_is_not_in(
    tmp_path, fake, ticker
):
    """O35: the pair cedar <- pine of the slot was stuck after the outage of another provider, birch: pine's
    rtransfer link crashed during it and cedar's did not reconnect, a pair neither of whose providers was restored. A
    restore's check runs every ordered pair, so it finds it, and the restore that follows it is unverified."""
    platform = write_platform(
        tmp_path, lambda d: (three_providers(d), d.__setitem__("faults", faults_table(services=STORES)))
    )
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    serves = provider_serves(config, load_faults(platform, config), client)
    assert serves.links(STORES["beta"]) is None  # beta restored, every link transferring
    stick(fake, ticker, "gamma", "alpha")  # gamma <- alpha, neither of whom is beta
    failure = serves.links(STORES["beta"])
    assert failure is not None
    assert "no transfer from providers[0] to providers[2]" in failure
    assert "restart the rtransfer link on providers[2]" in failure


def test_o35_the_pairs_are_the_providers_that_share_a_declared_space(tmp_path, fake):
    """O35: two providers that support no space in common are no pair: their link carries no read of a study, so
    beta and gamma, which each share a space with alpha and none with each other, are tested with alpha only."""

    def several(data):
        three_providers(data)
        with_spaces(data, 2, providers=["alpha", "beta"])
        data["spaces"][1]["providers"] = ["alpha", "gamma"]

    config = load_platform(write_platform(tmp_path, several))
    pairs = link_pairs(config.spaces, list(config.providers))
    assert pairs == (("alpha", "beta"), ("alpha", "gamma"), ("beta", "alpha"), ("gamma", "alpha"))
