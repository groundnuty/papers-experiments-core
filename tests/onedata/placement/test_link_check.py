"""The pre-campaign link check (O35, #221): every ordered (reading provider, source) pair fetches a probe from the probe
space within its bound before any run, or the campaign is refused naming the pair. The pairs of the local provider are
read here; `test_link_check_pairs.py` holds the other readers'."""

import json

import pytest
from link_support import (
    ALPHA_ID,
    BETA_ID,
    BOUND_S,
    CONFIGURED,
    DELTA_ID,
    GAMMA_ID,
    OMEGA_ID,
    add_providers,
    held,
    markers_of,
    pair_of,
    probe_of,
    probes_of,
    reads_through,
    run,
    three_providers,
)

from expcore.config.loading import ConfigError
from expcore_onedata import linkcheck
from expcore_onedata.client import OnezoneClient, OutOfScope
from expcore_onedata.config import load_platform
from expcore_onedata.health import PlatformHealth
from expcore_onedata.linkcheck import PROBE_PREFIX, probe_content, probe_name
from expcore_onedata.testing import SPACE_NAME, with_placement, write_platform
from expcore_onedata.testing.world import PROBE_SPACE_ID, PROBE_SPACE_NAME
from expcore_onedata.tokens import TokenIssuer


@pytest.fixture(autouse=True)
def one_pair_at_a_time(monkeypatch):
    """The fake federation's clock is the test's `Ticker`, which every thread shares: a wait one pair makes (a hang's, a
    poll's) would run out another pair's deadline. The pairs run one at a time here; test_link_check_pairs.py shows that
    the check hands its pairs to `workers`."""
    monkeypatch.setattr(linkcheck, "WORKERS", 1)


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, three_providers)


@pytest.fixture
def fake(fake):
    return add_providers(fake)


def alpha_reads(fake, start=0):
    """The ids of the files whose content the local provider was asked for, in order, from the `start`th call on."""
    return reads_through(fake, ALPHA_ID, start)


def test_o35_every_pair_is_checked_and_recorded_with_its_seconds_and_bytes(fake, config, client, ticker):
    """O35: Every pair is checked and recorded with its seconds and bytes; those read through the local provider are
    the first two, and carry the check's original probe files, named for their source alone."""
    report = run(config, client, ticker)
    assert report.ok, report.detail
    assert len(report.pairs) == 6
    assert [(p.reader, p.source) for p in report.pairs[:2]] == [
        ("providers[0]", "providers[1]"),
        ("providers[0]", "providers[2]"),
    ]
    size = config.link_check.probe_bytes
    for pair, source in zip(report.pairs[:2], (BETA_ID, GAMMA_ID), strict=True):
        assert pair.ok
        assert pair.bytes == size
        assert pair.seconds is not None
        assert pair.seconds <= BOUND_S
        assert pair.provisioned  # written by this first check, through the source
        probe = probe_of(fake, source)
        assert bytes(fake.nodes[probe].content) == probe_content(source, size)
        assert held(fake, probe, source) == size
        reads = [c for c in fake.provider_calls if c[1] == "GET" and c[2] == f"/data/{probe}/content"]
        assert (ALPHA_ID, "GET", f"/data/{probe}/content") in reads  # the fetch went through the local provider
    record = json.loads(json.dumps(report.to_json()))
    assert record["version"] == 1
    assert record["ok"] is True
    assert [(p["reader"], p["source"], p["bytes"]) for p in record["pairs"][:2]] == [
        ("providers[0]", "providers[1]", size),
        ("providers[0]", "providers[2]", size),
    ]
    assert len(record["pairs"]) == 6
    assert record["window"]["start_epoch_s"] <= record["window"]["end_epoch_s"]
    assert not any(value in json.dumps(record) for value in CONFIGURED)


def test_o35_the_probe_is_evicted_from_the_local_provider_afterwards(fake, config, client, ticker):
    """O35: The probe is evicted from the local provider afterwards."""
    report = run(config, client, ticker)
    assert report.ok
    for pair, source in zip(report.pairs[:2], (BETA_ID, GAMMA_ID), strict=True):
        probe = probe_of(fake, source)
        assert pair.evicted is True
        assert pair.evictions
        assert held(fake, probe, ALPHA_ID) == 0  # cold for the next check
        assert held(fake, probe, source) == config.link_check.probe_bytes  # the source keeps it
        evictions = [fake.transfers[tid] for tid in pair.evictions]
        assert all(t.kind == "eviction" and t.provider_id == ALPHA_ID and t.file_id == probe for t in evictions)
    assert sorted(report.to_json()["transfers"]) == sorted(t for p in report.pairs for t in p.evictions)


def test_o35_a_hanging_pair_fails_within_the_bound_and_is_named(fake, config, client, ticker):
    """O35: A hanging pair fails within the bound and is named."""
    fake.links[(ALPHA_ID, BETA_ID)] = "hang"
    fake.hang_hook = ticker.sleep  # the read's wait runs out on the test's clock
    report = run(config, client, ticker)
    assert not report.ok
    stuck, other = pair_of(report, "alpha", "beta"), pair_of(report, "alpha", "gamma")
    assert not stuck.ok
    assert stuck.seconds <= BOUND_S + 0.001
    assert stuck.reason.startswith(f"no transfer from providers[1] to providers[0] within {BOUND_S:g} s")
    assert "providers[1] to providers[0]" in report.detail
    assert "providers[2]" not in report.detail
    assert other.ok  # every other pair still transferred
    assert [p.ok for p in report.pairs].count(False) == 1
    assert fake.hang_hook is not None
    assert BOUND_S in ticker.slept
    assert stuck.evicted is None
    assert other.evicted is None
    assert not any(value in report.detail for value in CONFIGURED)


def test_o35_a_link_that_fails_fails_the_check_naming_the_pair(fake, config, client, ticker):
    """O35: A link that fails fails the check naming the pair."""
    fake.links[(ALPHA_ID, GAMMA_ID)] = "fail"
    report = run(config, client, ticker)
    assert not report.ok
    assert [p.ok for p in report.pairs] == [True, False, True, True, True, True]
    assert report.pairs[1].reason.startswith("no transfer from providers[2] to providers[0]")


def test_o35_a_probe_left_on_the_local_provider_never_hides_a_stuck_link(fake, config, client, ticker):
    """O35: A probe still warm on the local provider (an eviction that took nothing) would let the read pass without the
    link: the next check evicts it first, and the stuck link is found."""
    fake.evictions_remove_blocks = False
    first = run(config, client, ticker)
    assert first.ok
    assert [p.evicted for p in first.pairs] == [False] * 6
    probe = probe_of(fake, BETA_ID)
    assert held(fake, probe, ALPHA_ID) > 0
    fake.evictions_remove_blocks = True
    fake.links[(ALPHA_ID, BETA_ID)] = "hang"
    fake.hang_hook = ticker.sleep
    second = run(config, client, ticker)
    assert not second.ok
    stuck = second.pairs[0]
    assert stuck.cooled
    assert not stuck.ok
    assert "no transfer from providers[1] to providers[0]" in stuck.reason
    assert not stuck.provisioned  # the probe was kept from the first check


def test_o35_a_probe_is_written_once_and_written_again_when_it_is_gone(fake, config, client, ticker):
    """O35: A probe is written once and written again when it is gone."""
    assert all(p.provisioned for p in run(config, client, ticker).pairs)
    again = run(config, client, ticker)
    assert again.ok
    assert not any(p.provisioned for p in again.pairs)
    fake._delete(probe_of(fake, GAMMA_ID))  # the probe space emptied, as reset --clean does
    third = run(config, client, ticker)
    assert third.ok
    assert [p.provisioned for p in third.pairs] == [False, True, False, False, False, False]


def test_o44_a_probe_with_other_bytes_is_written_again_at_offset_0_and_a_longer_one_gives_way(
    fake, config, client, ticker
):
    """O35, O44: a probe of the right size whose bytes differ is written again over them at offset 0; a longer probe
    could only be cut by a write without an offset, which is never sent, so a fresh file under the next generation
    replaces it and the passing check deletes the old one. Every write of content carries an offset."""
    assert run(config, client, ticker).ok
    size = config.link_check.probe_bytes
    first = probe_of(fake, GAMMA_ID)
    fake.nodes[first].content[0] ^= 0xFF
    rewritten = run(config, client, ticker)
    assert rewritten.ok
    assert [p.provisioned for p in rewritten.pairs] == [False, True, False, False, False, False]
    assert probe_of(fake, GAMMA_ID) == first
    assert bytes(fake.nodes[first].content) == probe_content(GAMMA_ID, size)
    fake.nodes[first].content.extend(b"tail")
    replaced = run(config, client, ticker)
    assert replaced.ok
    assert [p.provisioned for p in replaced.pairs] == [False, True, False, False, False, False]
    assert list(probes_of(fake, GAMMA_ID)) == [1]
    assert bytes(fake.nodes[probes_of(fake, GAMMA_ID)[1]].content) == probe_content(GAMMA_ID, size)
    assert all("offset" in r.query for r in fake.requests if r.method == "PUT" and r.path.endswith("/content"))


def test_o35_pairs_and_no_probe_space_refuse_and_no_pairs_pass_without_one(tmp_path, fake, client, ticker):
    """O35: Pairs and no probe space refuse and no pairs pass without one."""
    unprobed = load_platform(write_platform(tmp_path, lambda d: three_providers(d, link_check=False)))
    report = run(unprobed, client, ticker)
    assert not report.ok
    assert "6 provider pair(s)" in report.detail
    assert "[link_check]" in report.detail
    alone = load_platform(write_platform(tmp_path, with_placement))  # alpha alone supports the study space
    assert run(alone, client, ticker).ok


def test_o35_a_link_stuck_at_the_first_call_holds_every_run_until_a_recheck_after_the_remedy_passes(
    fake, config, client, ticker
):
    """O35: the first call fails naming the pair, and the verdict is not kept: every later call (the closed gate's
    re-checks) checks the links again, so the remedied link reopens the gate without restarting the campaign."""
    fake.links[(ALPHA_ID, BETA_ID)] = "fail"
    issuer = TokenIssuer(client, config, clock=lambda: 1_800_000_000.0)
    health = PlatformHealth(config, client, issuer, monotonic=ticker.clock, sleep=ticker.sleep)
    verdict = health()
    assert not verdict.ok
    assert verdict.detail.startswith("no run is admitted until every provider link transfers: ")
    assert "no transfer from providers[1] to providers[0]" in verdict.detail
    assert health.links is not None
    assert not health.links.ok
    calls = len(fake.provider_calls)
    assert not health().ok  # still stuck: checked again
    assert len(fake.provider_calls) > calls
    del fake.links[(ALPHA_ID, BETA_ID)]  # the remedy: the link restarted
    passed = health()
    assert passed.ok, passed.detail
    assert health.links.ok


def test_o35_platform_health_passes_and_answers_as_the_space_check_once_every_link_works(fake, config, client, ticker):
    """O35: Platform health passes and answers as the space check once every link works."""
    issuer = TokenIssuer(client, config, clock=lambda: 1_800_000_000.0)
    health = PlatformHealth(config, client, issuer, monotonic=ticker.clock, sleep=ticker.sleep)
    verdict = health()
    assert verdict.ok, verdict.detail
    assert health.links.ok
    assert len(health.links.pairs) == 6


def test_o35_only_a_request_bound_to_the_probe_space_reaches_it(fake, config, client, ticker):
    """O35: Only a request bound to the probe space reaches it."""
    assert run(config, client, ticker).ok
    probe = probe_of(fake, BETA_ID)
    sent = len(fake.requests)
    with pytest.raises(OutOfScope):
        client.mutate("DELETE", f"/data/{probe}", provider="beta")  # a study's mutation, bound to no space
    with pytest.raises(OutOfScope):
        client.mutate("DELETE", f"/data/{probe}", provider="beta", space=config.space)
    assert not any(r.method == "DELETE" for r in fake.requests[sent:])
    assert probe.startswith("fid-")
    assert probe_name(BETA_ID).startswith(PROBE_PREFIX)


@pytest.mark.parametrize(
    ("change", "key", "words"),
    [
        ({"id": "spmock0001"}, "id", "a declared study space"),
        ({"name": SPACE_NAME, "prefixes": ["study-a-"]}, "name", "a declared study space"),
        ({"name": "study-a-probes", "prefixes": ["study-a-"]}, "name", "a study's own mutations could reach"),
        ({"providers": ["beta", "gamma"]}, "providers", "leaves out the local provider providers[0]"),
        ({"providers": ["alpha", "beta"]}, "providers", "providers[0] <- providers[2] cannot be tested"),
        ({"probe_bytes": 1 << 20}, "probe_bytes", "under 1 MiB"),
        ({"timeout_s": 0}, "timeout_s", "at least"),
        ({"fabricated": 1}, "fabricated", "not a known key"),
    ],
    ids=["study id", "study name", "study prefix", "no local", "a source left out", "large", "no bound", "unknown key"],
)
def test_o35_a_probe_space_that_is_agent_visible_or_cannot_test_a_pair_is_refused_at_load(
    tmp_path, fake, change, key, words
):
    """O35: A probe space that is agent visible or cannot test a pair is refused at load."""
    path = write_platform(tmp_path, lambda d: (three_providers(d), d["link_check"].update(change)))
    with pytest.raises(ConfigError) as refused:
        load_platform(path)
    message = str(refused.value)
    assert "[link_check]" in message
    assert key in message
    assert words in message
    assert not any(value in message for value in (PROBE_SPACE_NAME, "alpha", "beta", "gamma", SPACE_NAME))


def test_o35_a_failed_pair_names_the_remedy_and_records_that_it_needs_one(fake, config, client, ticker):
    """O35: A failed pair names the remedy and records that it needs one."""
    fake.links[(ALPHA_ID, BETA_ID)] = "fail"
    report = run(config, client, ticker)
    stuck, other = pair_of(report, "alpha", "beta"), pair_of(report, "alpha", "gamma")
    assert "rtransfer_config:restart_link() in its op-worker" in stuck.reason
    assert "never a compose down" in stuck.reason
    assert stuck.remedy is not None
    assert "providers[0]" in stuck.remedy
    assert other.remedy is None
    assert report.to_json()["pairs"][0]["remedy"] == stuck.remedy


def test_o35_every_probe_read_and_every_eviction_is_of_a_file_in_the_probe_space(fake, config, client, ticker):
    """O35: The probe is never a file another reader may have open: everything the check reads, writes or evicts lies in
    the probe space, and no study space's file is touched."""
    assert run(config, client, ticker).ok
    probe_root = fake.space_roots[PROBE_SPACE_ID]

    def root_of(file_id):
        node = fake.nodes[file_id]
        while node.parent is not None:
            node = fake.nodes[node.parent]
        return node.file_id

    touched = {c[2].split("/")[2] for c in fake.provider_calls if c[2].endswith(("/content", "/children"))}
    assert touched
    assert {root_of(i) for i in touched} == {probe_root}
    assert {root_of(t.file_id) for t in fake.transfers.values()} == {probe_root}


def test_o35_the_health_gate_checks_the_links_again_every_every_s_and_a_stuck_link_closes_it(
    fake, config, client, ticker
):
    """O35: A link that sticks mid-campaign fails the first call once `every_s` (600 s) has passed since the last check:
    the gate closes, naming the pair and the remedy, and its re-checks pass again once the link transfers."""
    issuer = TokenIssuer(client, config, clock=lambda: 1_800_000_000.0)
    health = PlatformHealth(config, client, issuer, monotonic=ticker.clock, sleep=ticker.sleep)
    assert health().ok
    fake.links[(ALPHA_ID, GAMMA_ID)] = "fail"
    assert health().ok  # too soon: the last check passed less than every_s ago
    ticker.sleep(config.link_check.every_s)
    stuck = health()
    assert not stuck.ok
    assert "stopped transferring during the campaign" in stuck.detail
    assert "no transfer from providers[2] to providers[0]" in stuck.detail
    assert "remedy" in stuck.detail
    del fake.links[(ALPHA_ID, GAMMA_ID)]
    assert health().ok  # a failed check is run again at the next call, however soon


def hold(fake, file_id, provider_id):
    """Another provider of the probe space fetched the whole probe at some point and still holds it."""
    fake.blocks[(file_id, provider_id)] = [[0, len(fake.nodes[file_id].content)]]


def stuck_beta(fake, ticker):
    """The link local <- beta hangs, as a provider's rtransfer link did on the first real federation."""
    fake.links[(ALPHA_ID, BETA_ID)] = "hang"
    fake.hang_hook = ticker.sleep


@pytest.mark.parametrize(
    ("holder", "label", "words"),
    [
        (DELTA_ID, "providers[3]", "not a provider of [link_check]"),
        (OMEGA_ID, "a provider platform.toml does not declare", "list it in [link_check] providers"),
    ],
    ids=["declared, not listed", "not declared"],
)
def test_o35_another_holder_the_check_cannot_evict_fails_the_pair_instead_of_serving_the_fetch(
    fake, config, client, ticker, holder, label, words
):
    """O35: another supporter of the probe space that also holds the probe, one `[link_check].providers` leaves out,
    would serve the local provider's read while the tested link is stuck, and the cold check once passed with no
    eviction of it. The whole block map is read now: such a holder fails the pair, named, and nothing is fetched."""
    assert run(config, client, ticker).ok
    probe = probe_of(fake, BETA_ID)
    hold(fake, probe, holder)
    stuck_beta(fake, ticker)
    calls = len(fake.provider_calls)
    report = run(config, client, ticker)
    assert not report.ok
    failed, other = pair_of(report, "alpha", "beta"), pair_of(report, "alpha", "gamma")
    assert not failed.ok
    assert failed.seconds is None  # not tested: nothing was fetched, so the delta's copy never served the read
    assert label in failed.reason
    assert words in failed.reason
    assert other.ok
    assert probe not in alpha_reads(fake, calls)
    assert not any(value in report.detail for value in (*CONFIGURED, holder))


def test_o35_a_second_holder_the_check_can_evict_is_evicted_and_the_stuck_link_is_still_found(tmp_path, fake, ticker):
    """O35: a holder that `[link_check].providers` names is evicted before the fetch, so the stuck link is not
    bypassed through it."""
    listed = write_platform(
        tmp_path,
        lambda d: (three_providers(d), d["link_check"].update(providers=[*d["link_check"]["providers"], "delta"])),
    )
    config = load_platform(listed)
    client = OnezoneClient(config, http=fake.client_http())
    assert run(config, client, ticker).ok
    probe = probe_of(fake, BETA_ID)
    hold(fake, probe, DELTA_ID)
    stuck_beta(fake, ticker)
    report = run(config, client, ticker)
    stuck = pair_of(report, "alpha", "beta")
    assert held(fake, probe, DELTA_ID) == 0
    assert {fake.transfers[t].provider_id for t in stuck.cooled} == {DELTA_ID}
    assert not stuck.ok
    assert stuck.reason.startswith(f"no transfer from providers[1] to providers[0] within {BOUND_S:g} s")


@pytest.mark.parametrize(
    ("holder", "label"), [(DELTA_ID, "providers[3]"), (GAMMA_ID, "providers[2]"), (OMEGA_ID, "does not declare")]
)
def test_o35_a_holding_that_cannot_be_read_fails_the_pair_and_is_never_dropped(
    fake, config, client, ticker, holder, label
):
    """O35: a provider the block map lists with a holding that cannot be parsed is unknown, never empty: the pair is
    not tested, naming it, whichever provider it is."""
    assert run(config, client, ticker).ok
    probe = probe_of(fake, BETA_ID)
    fake.distribution_entries[(probe, holder)] = {"virtualSize": 7}  # no table of storage backends
    calls = len(fake.provider_calls)
    report = run(config, client, ticker)
    assert not report.ok
    failed, other = pair_of(report, "alpha", "beta"), pair_of(report, "alpha", "gamma")
    assert failed.seconds is None
    assert label in failed.reason
    assert "readable" in failed.reason
    assert other.ok
    assert probe not in alpha_reads(fake, calls)
    assert not any(value in report.detail for value in (*CONFIGURED, holder))


def test_o35_a_provider_that_lists_no_blocks_is_not_a_holder(fake, config, client, ticker):
    """O35: a supporter of the probe space the block map shows with an explicit empty holding does not hold it, even
    when `[link_check].providers` leaves it out: it fails nothing."""
    assert run(config, client, ticker).ok
    assert fake.blocks.get((probe_of(fake, BETA_ID), DELTA_ID)) is None
    assert run(config, client, ticker).ok


def test_o35_after_a_timeout_the_probe_is_retired_and_the_next_check_reads_a_fresh_file(fake, config, client, ticker):
    """O35: a read that timed out leaves a fetch job on the local provider, and a later read of the same file joins it
    until its restart. The next check reads another file, a new name and id, so a recovered link passes at once."""
    fake.joins_stuck_jobs = True
    stuck_beta(fake, ticker)
    first = run(config, client, ticker)
    failed = first.pairs[0]
    assert not failed.ok
    assert failed.retired is True
    ((_, old),) = fake.stuck_jobs  # the file whose fetch the local provider is still waiting on
    assert old not in fake.nodes  # retired: deleted
    fresh = probe_of(fake, BETA_ID)
    assert fresh != old
    assert fake.nodes[fresh].name == probe_name(BETA_ID, 1)
    assert bytes(fake.nodes[fresh].content) == probe_content(BETA_ID, config.link_check.probe_bytes)
    del fake.links[(ALPHA_ID, BETA_ID)]  # the remedy: the link restarted, the outstanding job not yet
    calls = len(fake.provider_calls)
    second = run(config, client, ticker)
    assert second.ok, second.detail
    assert second.pairs[0].seconds is not None
    assert old not in alpha_reads(fake, calls)
    assert fresh in alpha_reads(fake, calls)
    assert second.pairs[0].purged == 0


def test_o35_a_probe_that_failed_is_replaced_whatever_the_failure_was(fake, config, client, ticker):
    """O35: an answered error is retired the same as a timeout, and the passing check keeps the fresh probe."""
    fake.links[(ALPHA_ID, GAMMA_ID)] = "fail"
    first = run(config, client, ticker)
    assert [p.retired for p in first.pairs] == [None, True, None, None, None, None]
    old = probes_of(fake, GAMMA_ID)
    assert set(old) == {1}
    del fake.links[(ALPHA_ID, GAMMA_ID)]
    second = run(config, client, ticker)
    assert second.ok
    assert set(probes_of(fake, GAMMA_ID)) == {1}  # kept, not written again
    assert [p.provisioned for p in second.pairs] == [False] * 6
    assert second.pairs[0].retired is None


def test_o35_a_retired_file_that_could_not_be_deleted_is_deleted_by_a_later_passing_check(fake, config, client, ticker):
    """O35: the cleanup of retired files is bounded: a deletion that failed is counted, the fresh probe is used, the
    next check that passes deletes the retired file, and the record of it is dropped once the file is gone."""
    fake.joins_stuck_jobs = True
    stuck_beta(fake, ticker)
    fake.fail_deletes = True
    first = run(config, client, ticker)
    failed = first.pairs[0]
    assert failed.retired is True  # the successor was written, so the next check reads it
    assert failed.unpurged == 1
    assert set(probes_of(fake, BETA_ID)) == {0, 1}
    assert len(markers_of(fake)) == 1  # the failed file is recorded as retired, though it is still there
    fake.fail_deletes = False
    del fake.links[(ALPHA_ID, BETA_ID)]
    second = run(config, client, ticker)
    assert second.ok, second.detail
    assert second.pairs[0].purged == 1
    assert set(probes_of(fake, BETA_ID)) == {1}
    assert len(markers_of(fake)) == 1  # the file went after the check looked, so its record goes at the next one
    assert json.loads(json.dumps(second.to_json()))["pairs"][0]["purged"] == 1
    assert run(config, client, ticker).ok
    assert markers_of(fake) == []  # no file of it is left, so nothing of it is kept


def test_o35_when_the_successor_and_the_deletion_both_fail_the_failed_file_is_still_never_read_again(
    fake, config, client, ticker
):
    """O35: the retirement is recorded in the probe space through the local provider, apart from the source's writes:
    a failed file whose successor could not be written and that could not be deleted is not read by the next check,
    which fails closed naming the pair until a fresh probe can be written, and then reads the fresh file."""
    fake.joins_stuck_jobs = True
    fake.links[(ALPHA_ID, BETA_ID)] = "hang"

    def hang(seconds):
        ticker.sleep(seconds)
        fake.fail_creates.add(BETA_ID)  # the source writes nothing now: no successor
        fake.fail_deletes = True

    fake.hang_hook = hang
    failed = run(config, client, ticker).pairs[0]
    assert failed.retired is True  # recorded, though neither the successor nor the deletion took
    assert failed.unpurged == 1
    old = probe_of(fake, BETA_ID)  # still there
    (marker,) = markers_of(fake)
    assert old not in marker  # a digest of its id, never the id

    fake.hang_hook = ticker.sleep
    calls = len(fake.provider_calls)
    second = run(config, client, ticker)
    stuck, other = pair_of(second, "alpha", "beta"), pair_of(second, "alpha", "gamma")
    assert not second.ok
    assert stuck.reason.startswith("no fresh probe for providers[1] to providers[0]: creation failed")
    assert stuck.seconds is None  # nothing was fetched
    assert other.ok
    assert old not in alpha_reads(fake, calls)
    assert not any(value in second.detail for value in (*CONFIGURED, old))

    fake.fail_creates.clear()
    fake.fail_deletes = False
    del fake.links[(ALPHA_ID, BETA_ID)]  # the link works again; the job on the old file is still outstanding
    calls = len(fake.provider_calls)
    third = run(config, client, ticker)
    assert third.ok, third.detail
    fresh = probe_of(fake, BETA_ID)  # the retired file went with the passing check
    assert fresh != old
    assert fresh in alpha_reads(fake, calls)
    assert old not in alpha_reads(fake, calls)
    assert third.pairs[0].purged == 1


def test_o35_retiring_a_later_probe_never_brings_back_an_older_failed_one(fake, config, client, ticker):
    """O35: g0 fails and cannot be deleted, so g1 is the probe; g1 fails, g2 cannot be written, g1 is deleted and g0
    cannot be again. The next check must not read g0: the failed file ids are never read again, whatever remains."""
    fake.joins_stuck_jobs = True
    fake.links[(ALPHA_ID, BETA_ID)] = "hang"

    def first_hang(seconds):
        ticker.sleep(seconds)
        fake.fail_deletes = True

    fake.hang_hook = first_hang
    assert run(config, client, ticker).pairs[0].retired is True
    both = probes_of(fake, BETA_ID)
    assert set(both) == {0, 1}
    g0 = both[0]
    fake.fail_deletes = False

    def second_hang(seconds):
        ticker.sleep(seconds)
        fake.fail_creates.add(BETA_ID)  # no g2
        fake.statuses.append([f"/data/{g0}", 1, 500])  # g1 is deleted, g0 is not, again

    fake.hang_hook = second_hang
    second = run(config, client, ticker)
    assert second.pairs[0].retired is True
    assert probes_of(fake, BETA_ID) == {0: g0}

    fake.fail_creates.clear()
    fake.hang_hook = ticker.sleep
    del fake.links[(ALPHA_ID, BETA_ID)]
    calls = len(fake.provider_calls)
    third = run(config, client, ticker)
    assert third.ok, third.detail
    assert g0 not in alpha_reads(fake, calls)
    fresh = probe_of(fake, BETA_ID)
    assert fresh != g0
    assert third.pairs[0].purged == 1  # g0 went at last


def test_o35_a_retired_file_that_still_exists_is_never_selected_as_the_probe(fake, config, client, ticker):
    """O35: selection leaves out every retired file id, whatever its name or generation: with only the retired file
    listed, a new probe is written under the next generation's name and read, and the retired file is deleted."""
    fake.joins_stuck_jobs = True
    stuck_beta(fake, ticker)
    fake.fail_deletes = True
    assert run(config, client, ticker).pairs[0].retired is True
    old, successor = probes_of(fake, BETA_ID)[0], probes_of(fake, BETA_ID)[1]
    del fake.nodes[successor]  # the successor is lost, so the retired file is the only one the source lists
    fake.fail_deletes = False
    del fake.links[(ALPHA_ID, BETA_ID)]
    calls = len(fake.provider_calls)
    second = run(config, client, ticker)
    assert second.ok, second.detail
    assert second.pairs[0].provisioned
    assert old not in alpha_reads(fake, calls)
    fresh = probe_of(fake, BETA_ID)
    assert fresh not in (old, successor)
    assert fake.nodes[fresh].name == probe_name(BETA_ID, 1)
    assert old not in fake.nodes


def test_o35_a_probe_that_could_not_be_retired_at_all_is_said_so_and_retired_by_the_next_failure(
    fake, config, client, ticker
):
    """O35: when the record, the successor and the deletion all fail, nothing excludes the failed file: the failure
    says a later check may read it again, and the check after that retires it."""
    fake.joins_stuck_jobs = True
    fake.links[(ALPHA_ID, BETA_ID)] = "hang"

    def hang(seconds):
        ticker.sleep(seconds)
        fake.fail_creates.update({ALPHA_ID, BETA_ID})  # neither the record nor the successor can be written
        fake.fail_deletes = True

    fake.hang_hook = hang
    first = run(config, client, ticker)
    failed = first.pairs[0]
    assert failed.retired is False
    assert "could not be retired" in failed.reason
    assert "may read it again" in failed.reason
    assert set(probes_of(fake, BETA_ID)) == {0}
    assert markers_of(fake) == []
    fake.fail_creates.clear()
    fake.fail_deletes = False
    fake.hang_hook = ticker.sleep
    second = run(config, client, ticker)
    assert second.pairs[0].retired is True  # the same file failed again, and now it is recorded
    assert len(markers_of(fake)) == 1
    del fake.links[(ALPHA_ID, BETA_ID)]
    assert run(config, client, ticker).ok


def test_o35_a_successor_alone_does_not_make_a_failed_file_retired(fake, config, client, ticker):
    """O35: with the record unwritable and the failed file not deleted, only the generation order holds the file back
    while its successor is the highest: it is not reported retired, the failure says a later check may read it again,
    and the next check reads the successor."""
    fake.joins_stuck_jobs = True
    fake.links[(ALPHA_ID, BETA_ID)] = "hang"

    def hang(seconds):
        ticker.sleep(seconds)
        fake.fail_creates.add(ALPHA_ID)  # the record cannot be written; the source still writes
        fake.fail_deletes = True

    fake.hang_hook = hang
    failed = run(config, client, ticker).pairs[0]
    assert failed.retired is False
    assert "no record of it written" in failed.reason
    assert "a later check may read it again" in failed.reason
    both = probes_of(fake, BETA_ID)
    assert set(both) == {0, 1}  # the successor stands
    assert markers_of(fake) == []
    fake.fail_creates.clear()
    fake.fail_deletes = False
    fake.hang_hook = ticker.sleep
    del fake.links[(ALPHA_ID, BETA_ID)]
    calls = len(fake.provider_calls)
    second = run(config, client, ticker)
    assert second.ok, second.detail
    assert both[1] in alpha_reads(fake, calls)
    assert both[0] not in alpha_reads(fake, calls)
    assert second.pairs[0].purged == 1


def test_o35_the_health_gate_reopens_at_the_first_recheck_after_a_hung_link_recovers(fake, config, client, ticker):
    """O35: the closed gate's re-check, after a link hung and then recovered while its fetch job was still outstanding,
    passes at once and reads no file whose fetch may still be stuck."""
    fake.joins_stuck_jobs = True
    issuer = TokenIssuer(client, config, clock=lambda: 1_800_000_000.0)
    health = PlatformHealth(config, client, issuer, monotonic=ticker.clock, sleep=ticker.sleep)
    stuck_beta(fake, ticker)
    assert not health().ok
    del fake.links[(ALPHA_ID, BETA_ID)]
    passed = health()
    assert passed.ok, passed.detail
