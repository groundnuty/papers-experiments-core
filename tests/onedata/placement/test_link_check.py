"""The pre-campaign link check (O35, #221): every (local, source) pair fetches a probe from the probe space within its
bound before any run, or the campaign is refused naming the pair."""

import json

import pytest

from expcore.config.loading import ConfigError
from expcore_onedata.client import OutOfScope
from expcore_onedata.config import load_platform
from expcore_onedata.health import PlatformHealth
from expcore_onedata.linkcheck import PROBE_PREFIX, check_links, probe_content, probe_name
from expcore_onedata.testing import ALPHA_ID, BETA_ID, SPACE_NAME, with_placement, write_platform
from expcore_onedata.testing.world import PROBE_SPACE_ID, PROBE_SPACE_NAME, with_link_check
from expcore_onedata.tokens import TokenIssuer

GAMMA_ID = "pr-gamma"
BOUND_S = 5.0
CONFIGURED = (PROBE_SPACE_NAME, PROBE_SPACE_ID, "alpha", "beta", "gamma", SPACE_NAME, ".test")  # never in a message


def three_providers(data, *, link_check=True, **override):
    """The study space supported by alpha (local), beta and gamma; the probe space by all three."""
    with_placement(data)
    data["providers"]["gamma"] = {"url": "https://gamma.test", "id": GAMMA_ID}
    data["support"] = {"providers": ["alpha", "beta", "gamma"], "local": "alpha"}
    if link_check:
        with_link_check(data, ["alpha", "beta", "gamma"], timeout_s=BOUND_S, **override)


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, three_providers)


@pytest.fixture
def fake(fake):
    fake.supports[BETA_ID] = {"readonly": False}
    fake.add_provider(GAMMA_ID, "gamma.test")
    fake.add_data_space(PROBE_SPACE_ID, PROBE_SPACE_NAME, [ALPHA_ID, BETA_ID, GAMMA_ID])
    return fake


def run(config, client, ticker):
    """One check on the test's clock, its fetches one at a time so that a hang's wait moves no other pair's clock."""
    return check_links(config, client, clock=ticker.clock, sleep=ticker.sleep, now=ticker.clock, workers=1)


def probe_of(fake, provider_id):
    """The probe file of a source, found by its name in the probe space's tree."""
    root = fake.space_roots[PROBE_SPACE_ID]
    found = [n for n in fake.nodes.values() if n.parent == root and n.name == probe_name(provider_id)]
    assert len(found) == 1, "each source has exactly one probe"
    return found[0].file_id


def held(fake, file_id, provider_id):
    return sum(n for _, n in fake.blocks.get((file_id, provider_id), []))


def test_o35_every_pair_is_checked_and_recorded_with_its_seconds_and_bytes(fake, config, client, ticker):
    """O35: Every pair is checked and recorded with its seconds and bytes."""
    report = run(config, client, ticker)
    assert report.ok, report.detail
    assert [(p.local, p.source) for p in report.pairs] == [
        ("providers[0]", "providers[1]"),
        ("providers[0]", "providers[2]"),
    ]
    size = config.link_check.probe_bytes
    for pair, source in zip(report.pairs, (BETA_ID, GAMMA_ID), strict=True):
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
    assert [(p["local"], p["source"], p["bytes"]) for p in record["pairs"]] == [
        ("providers[0]", "providers[1]", size),
        ("providers[0]", "providers[2]", size),
    ]
    assert record["window"]["start_epoch_s"] <= record["window"]["end_epoch_s"]
    assert not any(value in json.dumps(record) for value in CONFIGURED)


def test_o35_the_probe_is_evicted_from_the_local_provider_afterwards(fake, config, client, ticker):
    """O35: The probe is evicted from the local provider afterwards."""
    report = run(config, client, ticker)
    assert report.ok
    for pair, source in zip(report.pairs, (BETA_ID, GAMMA_ID), strict=True):
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
    stuck, other = report.pairs
    assert not stuck.ok
    assert stuck.seconds <= BOUND_S + 0.001
    assert stuck.reason.startswith(f"no transfer from providers[1] to providers[0] within {BOUND_S:g} s")
    assert "providers[1] to providers[0]" in report.detail
    assert "providers[2]" not in report.detail
    assert other.ok  # every other pair still transferred
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
    assert [p.ok for p in report.pairs] == [True, False]
    assert report.pairs[1].reason.startswith("no transfer from providers[2] to providers[0]")


def test_o35_a_probe_left_on_the_local_provider_never_hides_a_stuck_link(fake, config, client, ticker):
    """O35: A probe still warm on the local provider (an eviction that took nothing) would let the read pass without the
    link: the next check evicts it first, and the stuck link is found."""
    fake.evictions_remove_blocks = False
    first = run(config, client, ticker)
    assert first.ok
    assert [p.evicted for p in first.pairs] == [False, False]
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
    assert [p.provisioned for p in third.pairs] == [False, True]


def test_o35_pairs_and_no_probe_space_refuse_and_no_pairs_pass_without_one(tmp_path, fake, client, ticker):
    """O35: Pairs and no probe space refuse and no pairs pass without one."""
    unprobed = load_platform(write_platform(tmp_path, lambda d: three_providers(d, link_check=False)))
    report = run(unprobed, client, ticker)
    assert not report.ok
    assert "2 provider pair(s)" in report.detail
    assert "[link_check]" in report.detail
    alone = load_platform(write_platform(tmp_path, with_placement))  # alpha alone supports the study space
    assert run(alone, client, ticker).ok


def test_o35_platform_health_runs_the_check_at_its_first_call_and_keeps_the_report(fake, config, client, ticker):
    """O35: Platform health runs the check at its first call and keeps the report."""
    fake.links[(ALPHA_ID, BETA_ID)] = "fail"
    issuer = TokenIssuer(client, config, clock=lambda: 1_800_000_000.0)
    health = PlatformHealth(config, client, issuer, monotonic=ticker.clock, sleep=ticker.sleep)
    verdict = health()
    assert not verdict.ok
    assert "refused before any run starts" in verdict.detail
    assert "no transfer from providers[1] to providers[0]" in verdict.detail
    assert health.links is not None
    assert not health.links.ok
    calls = len(fake.provider_calls)
    assert not health().ok
    assert len(fake.provider_calls) == calls


def test_o35_platform_health_passes_and_answers_as_the_space_check_once_every_link_works(fake, config, client, ticker):
    """O35: Platform health passes and answers as the space check once every link works."""
    issuer = TokenIssuer(client, config, clock=lambda: 1_800_000_000.0)
    health = PlatformHealth(config, client, issuer, monotonic=ticker.clock, sleep=ticker.sleep)
    verdict = health()
    assert verdict.ok, verdict.detail
    assert health.links.ok
    assert len(health.links.pairs) == 2


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
    stuck, other = report.pairs
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


def test_o35_after_a_restore_only_the_pairs_the_provider_is_in_are_checked(fake, config, client, ticker):
    """O35: After a restore only the pairs the provider is in are checked."""
    source = check_links(config, client, clock=ticker.clock, sleep=ticker.sleep, workers=1, involving="gamma")
    assert [(p.local, p.source) for p in source.pairs] == [("providers[0]", "providers[2]")]
    local = check_links(config, client, clock=ticker.clock, sleep=ticker.sleep, workers=1, involving="alpha")
    assert len(local.pairs) == 2


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
