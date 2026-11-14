"""The reset's QoS stages: the run's own requirements found (by a summary per leaf, or by one recursive listing) and
removed while the baseline's are kept, then the files they touched settled (O33). The quiet stage that follows the
eviction is in `test_reset_quiet.py`.
"""

import math

import pytest
from qos_world import FILES, federation, provisioner_with, two_providers

from expcore_onedata import resetqos
from expcore_onedata.config import load_platform
from expcore_onedata.provision import Provisioner
from expcore_onedata.rangemap import DEFAULT_QOS_SCAN, QOS_SCANS, load_placement
from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.testing import BETA_ID, DATA, with_placement, write_platform
from expcore_onedata.testing.fake_data import ROOT_ID


@pytest.fixture
def fake(monkeypatch, ticker):
    return federation(monkeypatch, ticker)


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture(params=["summaries", "listing"])
def scanned(request, client, config, platform_file, ticker, tmp_path, fake) -> Provisioner:
    return provisioner_with(client, config, platform_file, ticker, tmp_path, scan=request.param)


def test_o33_the_runs_requirements_are_removed_and_the_baselines_kept(scanned, fake, ticker):
    """O33: a requirement present at the run's start is in the baseline and stays; the two the agent added, on a file
    and on a directory, are found by either scan, removed, and the second scan finds none but the baseline's."""
    kept = fake.add_qos(FILES[0])
    reset = SpaceReset(scanned, now=ticker.clock)
    baseline = reset.qos_requirements()
    assert baseline == {kept}
    added = {fake.add_qos(FILES[1]), fake.add_qos("study")}
    record = reset.run({"beta": ["study"]}, qos_baseline=baseline)
    assert set(fake.qos) == {kept}
    stage = record["stages"]["qos"]
    assert (stage["scan"], stage["found"], stage["kept"], stage["removed"]) == (scanned.settings.qos_scan, 3, 1, 2)
    assert stage["affected_files"] == len(FILES)  # the directory's requirement applies to every file under it
    assert not added & set(fake.qos)


def test_o33_the_listing_costs_a_page_per_thousand_entries_on_each_provider_and_a_summary_per_direct_entry(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: 2500 files more: the listing scan reads three pages of the recursive listing on each of the two providers,
    the summary of each of the two entries a requirement is on, on each provider that marks it, and the two
    requirements' records once, where the summaries scan reads a summary per leaf on each of the two providers."""
    for i in range(2500):
        fake.add_file(f"bulk/f-{i:04d}.bin", b"x")
    fake.add_qos(FILES[2])
    fake.add_qos("bulk")
    listing = provisioner_with(client, config, platform_file, ticker, tmp_path, scan="listing")
    before = len(fake.provider_calls)
    assert len(SpaceReset(listing).qos_requirements()) == 2
    made = fake.provider_calls[before:]
    entries = 1 + 2 + len(FILES) + 2500  # the root, two directories, the files
    pages = math.ceil(entries / resetqos.LISTING_PAGE)
    assert len([c for c in made if c[2].endswith("/files")]) == 2 * pages == 6
    assert len([c for c in made if c[2].endswith("/qos/summary")]) == 2 * 2
    assert len([c for c in made if c[2].startswith("/qos_requirements/")]) == 2
    found = resetqos.by_listing(listing, ["alpha", "beta"])
    assert found.reads == {"listings": 2, "listing_pages": 2 * pages, "summaries": 4, "records": 2}
    summaries = provisioner_with(client, config, platform_file, ticker, tmp_path)
    before = len(fake.provider_calls)
    SpaceReset(summaries).qos_requirements()
    read = [c for c in fake.provider_calls[before:] if c[2].endswith("/qos/summary")]
    assert len(read) == 2 * (1 + len(FILES) + 2500)


def test_o33_a_requirement_only_the_provider_it_was_added_through_has_is_found_and_removed(scanned, fake, ticker):
    """O33: an agent adds a requirement through beta just before the reset; alpha, the data provider, receives it
    through dbsync 8 s later. The scan of alpha finds nothing, so the reset reads every supporting provider, finds it
    on beta, waits until alpha has it too and it is fulfilled on both, removes it through beta, and shows it gone on
    both."""
    fake.qos_sync_s = 8.0
    qid = fake.add_qos("study", through=BETA_ID)
    record = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]})
    stage = record["stages"]["qos"]
    assert (stage["found"], stage["removed"], stage["affected_files"]) == (1, 1, len(FILES))
    assert stage["fulfil_s"] >= 8.0
    assert qid not in fake.qos


def test_o33_the_listing_of_the_data_provider_alone_misses_what_the_other_providers_listings_find(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: the defect the scan of every provider closes: the requirement beta has and alpha has not received is found
    by beta's listing and not by alpha's, and the union of the two finds it, with the files it applies to."""
    qid = fake.add_qos("study")
    fake.qos_hidden_on[qid] = {fake.PROVIDER_ID}
    listing = provisioner_with(client, config, platform_file, ticker, tmp_path, scan="listing")
    assert resetqos.by_listing(listing, ["alpha"]).where == {}
    both = resetqos.by_listing(listing, ["alpha", "beta"])
    assert both.where == {qid: "beta"}
    assert set(both.files[qid]) == {fake.id_of(path) for path in FILES}


def test_o33_one_entry_two_providers_mark_holds_the_requirements_each_of_them_shows(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: two requirements on the one directory, the second not yet received by alpha: both providers mark the
    directory as having a requirement on itself, so its summary is read on each, and the second is found, where one
    summary read on the first provider to mark it would have shown the first alone."""
    first, second = fake.add_qos("study"), fake.add_qos("study", expression="other")
    fake.qos_hidden_on[second] = {fake.PROVIDER_ID}
    listing = provisioner_with(client, config, platform_file, ticker, tmp_path, scan="listing")
    found = resetqos.by_listing(listing, ["alpha", "beta"])
    assert found.where == {first: "alpha", second: "beta"}
    assert found.reads["summaries"] == 2


def test_o33_a_listing_page_without_its_end_marker_fails_the_qos_stage(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33 with O26: a page of the recursive listing that does not say whether it is the last is refused, never taken
    for the end, and nothing is removed."""
    fake.add_qos(FILES[1])
    listing = provisioner_with(client, config, platform_file, ticker, tmp_path, scan="listing")
    original = fake._list_recursive

    def unmarked(*args):
        answer = original(*args)
        body = answer.json()
        del body["nextPageToken"]
        return type(answer)(200, json=body)

    fake._list_recursive = unmarked
    with pytest.raises(ResetFailed, match=r"qos stage.*nextPageToken"):
        SpaceReset(listing, now=ticker.clock).run({"beta": ["study"]})
    assert len(fake.qos) == 1


def test_o33_block_maps_that_change_once_settle_and_ones_that_keep_changing_fail_within_the_bound(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: a replication that lands at the settle's second poll restarts its three quiet seconds and passes; one that
    keeps placing bytes every poll fails the qos_settle stage at the bound, and the bound holds on the clock."""
    first, polls = fake.id_of(FILES[4]), [0]
    fake.blocks.pop((first, BETA_ID))

    def lands_once(seconds):
        ticker.sleep(seconds)
        polls[0] += seconds == resetqos.SETTLE_POLL_S
        if polls[0] == 2:  # the replication lands while the settle reads
            fake.blocks[(first, BETA_ID)] = [[0, len(DATA)]]

    fake.add_qos(FILES[4])
    lands = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=lands_once)
    record = SpaceReset(lands, now=ticker.clock).run({"beta": ["study"]})
    settled = record["stages"]["qos_settle"]
    assert settled["seconds"] >= 2 * resetqos.SETTLE_POLL_S + resetqos.SETTLE_STABLE_S

    second, grown = fake.id_of(FILES[5]), [0]

    def keeps_landing(seconds):
        ticker.sleep(seconds)
        if seconds == resetqos.SETTLE_POLL_S:  # another byte lands on beta every poll
            grown[0] += 1
            fake.blocks[(second, BETA_ID)] = [[0, grown[0]]]

    fake.add_qos(FILES[5])
    keeps = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=keeps_landing)
    with pytest.raises(ResetFailed, match=r"qos_settle stage.*still changing") as caught:
        SpaceReset(keeps, now=ticker.clock).run({"beta": ["study"]})
    seconds = caught.value.record["stages"]["qos_settle"]["seconds"]
    assert resetqos.SETTLE_BOUND_S <= seconds <= resetqos.SETTLE_BOUND_S + resetqos.SETTLE_POLL_S


def test_o33_the_scan_setting_is_one_of_the_two_and_listing_by_default(config, platform_file, tmp_path):
    """O33: `[placement] qos_scan` is `listing` when the file does not say, `summaries` and `listing` are both
    accepted when it does, and nothing else."""
    assert load_placement(platform_file, config).settings.qos_scan == "listing"
    assert DEFAULT_QOS_SCAN == "listing"
    assert set(QOS_SCANS) == {"summaries", "listing"}
    for scan in QOS_SCANS:
        (tmp_path / scan).mkdir()
        declared = write_platform(tmp_path / scan, lambda d, scan=scan: with_placement(d, qos_scan=scan))
        assert load_placement(declared, config).settings.qos_scan == scan
    (tmp_path / "bad").mkdir()
    bad = write_platform(tmp_path / "bad", lambda d: with_placement(d, qos_scan="walk"))
    with pytest.raises(Exception, match="qos_scan"):
        load_placement(bad, config)


def test_o33_a_study_that_declares_no_scan_has_the_fingerprint_of_one_that_declares_either(tmp_path, fake):
    """O33: `qos_scan` is a way to find requirements and changes none of what the reset removes, so it is in no
    fingerprint: the platform's half, which a resume compares, is the same without the key, with `summaries` and with
    `listing`."""
    fingerprints = []
    for name, extra in (("none", {}), ("summaries", {"qos_scan": "summaries"}), ("listing", {"qos_scan": "listing"})):
        (tmp_path / name).mkdir()
        file = write_platform(tmp_path / name, lambda d, extra=extra: with_placement(d, **extra))
        fingerprints.append(load_platform(file).fingerprint())
    assert fingerprints[0] == fingerprints[1] == fingerprints[2] == {"tls": {"mode": "verify"}}


def test_o33_a_reset_on_the_default_settings_finds_the_requirements_by_the_listing(provisioner, fake, ticker):
    """O33: a provisioner built from a file that does not declare `qos_scan` finds the requirements by the recursive
    listing of each provider's space root, with fewer summary reads than one per leaf (the summaries scan's);
    the reset's stage records the scan."""
    assert provisioner.settings.qos_scan == "listing"
    kept = fake.add_qos(FILES[0])
    reset = SpaceReset(provisioner, now=ticker.clock)
    before = len(fake.provider_calls)
    baseline = reset.qos_requirements()
    assert baseline == {kept}
    made = fake.provider_calls[before:]
    assert len([c for c in made if c[2].endswith("/files")]) == 2  # the space root's listing on each provider
    assert len([c for c in made if c[2].endswith("/qos/summary")]) < 2 * (1 + len(FILES))  # not one per leaf
    added = fake.add_qos(FILES[1])
    record = reset.run({"beta": ["study"]}, qos_baseline=baseline)
    stage = record["stages"]["qos"]
    assert (stage["scan"], stage["found"], stage["kept"], stage["removed"]) == ("listing", 2, 1, 1)
    assert added not in fake.qos


def test_o33_the_fakes_recursive_listing_marks_requirements_on_an_entry_and_its_ancestors(client, fake):
    """O33: the fake's listing answers each entry's `effQosInheritancePath` as the build computes it."""
    fake.add_qos("study")
    fake.add_qos(FILES[0])
    answer = client.get(
        f"/data/{ROOT_ID}/files",
        provider="alpha",
        params={"attributes": ["path", "effQosInheritancePath"], "include_directories": "true", "limit": 1000},
    )
    paths = {e["path"]: e["effQosInheritancePath"] for e in answer["files"]}
    assert paths["."] == "none"
    assert paths["study"] == "direct"
    assert paths[FILES[0]] == "directAndAncestor"
    assert paths[FILES[1]] == "ancestor"
    assert (answer["nextPageToken"], answer["inaccessiblePaths"]) == (None, [])


def beta_physical(client) -> int:
    """Beta's physical size of the space, as the space root's distribution answers it on alpha."""
    answer = client.get(f"/data/{ROOT_ID}/distribution", provider="alpha")
    return answer["distributionPerProvider"][BETA_ID]["distributionPerStorageBackend"]["storage-1"]["physicalSize"]


@pytest.mark.parametrize(("path", "lag"), [("top.bin", 10.0), ("study/one.bin", 15.0), ("a/b/c/deep.bin", 25.0)])
def test_o33_the_fakes_root_size_shows_a_placement_after_the_sources_lag_and_an_eviction_at_once(
    client, fake, ticker, path, lag
):
    """O33: the fake's directory size follows the source's bound: bytes that land on a file L path parts below the
    space root show in the root's physical size 10 + 5 x (L - 1) s later, and bytes that leave show at once; what the
    test put in place before the first call is old and shows at once."""
    held = len(FILES) * len(DATA)
    assert beta_physical(client) == held  # the setup's: old (this first call also ends the setup)
    landed = fake.add_file(path, DATA, holder=BETA_ID)
    assert beta_physical(client) == held
    ticker.sleep(lag - 0.5)
    assert beta_physical(client) == held  # not yet
    ticker.sleep(0.5)
    assert beta_physical(client) == held + len(DATA)
    del fake.blocks[(landed, BETA_ID)]
    assert beta_physical(client) == held  # an eviction shows at once
    fake.blocks[(landed, BETA_ID)] = [[0, len(DATA)]]
    ticker.sleep(lag - 0.5)
    assert beta_physical(client) == held  # a placement again: the lag counts from this one
    ticker.sleep(0.5)
    assert beta_physical(client) == held + len(DATA)


def test_o33_the_fakes_root_size_does_not_show_a_placement_that_was_evicted_within_the_lag(client, fake, ticker):
    """O33: bytes that land and leave inside the lag never show in the directory's size, whatever time passes."""
    assert beta_physical(client) == len(FILES) * len(DATA)
    landed = fake.add_file("study/one.bin", DATA, holder=BETA_ID)
    ticker.sleep(5.0)
    del fake.blocks[(landed, BETA_ID)]
    ticker.sleep(60.0)
    assert beta_physical(client) == len(FILES) * len(DATA)


def test_o33_the_fakes_lag_is_two_attributes_and_zero_gives_an_instant_size(client, fake, ticker):
    """O33: a test that wants the old behaviour sets the flush delays to 0, and one that wants another lag sets them."""
    fake.blocks_flush_delay_s = fake.dir_stats_flush_s = 0.0
    assert beta_physical(client) == len(FILES) * len(DATA)
    fake.add_file("a/b/c/deep.bin", DATA, holder=BETA_ID)
    assert beta_physical(client) == (len(FILES) + 1) * len(DATA)
    fake.blocks_flush_delay_s, fake.dir_stats_flush_s = 2.0, 1.0  # at four path parts: 2 + 3 x 1 s
    ticker.sleep(5.0)
    fake.add_file("a/b/c/deeper.bin", DATA, holder=BETA_ID)
    ticker.sleep(4.5)
    assert beta_physical(client) == (len(FILES) + 1) * len(DATA)
    ticker.sleep(0.5)
    assert beta_physical(client) == (len(FILES) + 2) * len(DATA)


def test_o33_a_listing_that_could_not_enter_a_path_fails_the_qos_stage(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: an answer with `inaccessiblePaths` is a listing that skipped part of the tree, where a requirement could
    lie: the scan is incomplete, so the reset fails and removes nothing."""
    fake.add_qos(FILES[1])
    listing = provisioner_with(client, config, platform_file, ticker, tmp_path, scan="listing")
    original = fake._list_recursive

    def partial(*args):
        body = original(*args).json()
        return type(original(*args))(200, json={**body, "inaccessiblePaths": ["study/locked"]})

    fake._list_recursive = partial
    with pytest.raises(ResetFailed, match=r"qos stage.*inaccessiblePaths"):
        SpaceReset(listing, now=ticker.clock).run({"beta": ["study"]})
    assert len(fake.qos) == 1


def test_o33_the_listing_sends_no_token_on_its_first_page_and_follows_the_ones_it_gets(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: the build reads a null token as the text "null", so the first page has none; with a page of two entries
    the scan follows each token to the null that ends the listing."""
    fake.add_qos(FILES[0])
    listing = provisioner_with(client, config, platform_file, ticker, tmp_path, scan="listing")
    resetqos.LISTING_PAGE, page = 2, resetqos.LISTING_PAGE
    try:
        found = resetqos.by_listing(listing, ["alpha"])
    finally:
        resetqos.LISTING_PAGE = page
    assert set(found.where) == set(fake.qos)
    assert found.reads["listing_pages"] == math.ceil((1 + 1 + len(FILES)) / 2)


def test_o33_the_settles_bound_counts_its_waits_not_its_reads(scanned, fake, ticker):
    """O33: a requirement on a large directory makes one reading of every file's map long; here the settle's first
    reading takes 5 s a map, a minute for six files on two providers, past the bound, and nothing changes. The settle
    passes, since the bound counts the waits between readings, never the readings."""
    original, reads = fake._distribution, [0]

    def slow(node):
        reads[0] += 1
        if reads[0] <= len(FILES) * 2:  # the first reading: every file on both providers
            ticker.sleep(5.0)
        return original(node)

    fake.add_qos("study")
    fake._distribution = slow
    record = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]})
    assert record["stages"]["qos_settle"]["seconds"] > resetqos.SETTLE_BOUND_S
