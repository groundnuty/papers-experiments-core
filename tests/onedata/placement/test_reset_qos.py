"""The reset's QoS stages: the run's own requirements found (by a summary per leaf, or by one recursive listing) and
removed while the baseline's are kept, then the files they touched settled and, after the eviction, kept quiet (O33).
"""

import dataclasses
import math

import pytest
from fake_reset import FakeReset

from expcore_onedata import resetqos
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.reset import STAGES, ResetFailed, SpaceReset
from expcore_onedata.testing import (
    ADMIN_ENV,
    BASELINE,
    BETA_ID,
    DATA,
    SPACE_NAME,
    USERS,
    with_placement,
    write_platform,
)
from expcore_onedata.testing.fake_data import ROOT_ID

FILES = [f"study/part-{i:02d}.bin" for i in range(6)]
WAIT_S = resetqos.quiet_wait_s(2)  # the quiet wait for files one directory below the root: twice 10 s + 5 s


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    """The placement fixture's federation, with a reset's calls: beta supports the space and holds every file."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)
    for path in FILES:
        file_id = federation.add_file(path, DATA)
        federation.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
    return federation


@pytest.fixture
def platform_file(tmp_path):
    def document(data):
        with_placement(data, transfer_settle_s=5.0, transfer_settle_polls=3, concurrency=4)
        data["support"]["providers"] = ["alpha", "beta"]

    return write_platform(tmp_path, document)


def provisioner_with(client, config, platform_file, ticker, tmp_path, *, scan="summaries", sleep=None):
    """A provisioner on the test's clock, finding QoS requirements by `scan`, sleeping through `sleep`."""
    settings = dataclasses.replace(load_placement(platform_file, config).settings, qos_scan=scan)
    return Provisioner(
        client,
        config,
        settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=sleep or ticker.sleep,
    )


@pytest.fixture(params=["summaries", "listing"])
def scanned(request, client, config, platform_file, ticker, tmp_path, fake) -> Provisioner:
    return provisioner_with(client, config, platform_file, ticker, tmp_path, scan=request.param)


def calls(fake, part: str) -> list:
    return [c for c in fake.provider_calls if c[1] == "GET" and part in c[2]]


def test_o33_the_runs_requirements_are_removed_and_the_baselines_kept(scanned, fake):
    """O33: a requirement present at the run's start is in the baseline and stays; the two the agent added, on a file
    and on a directory, are found by either scan, removed, and the second scan finds none but the baseline's."""
    kept = fake.add_qos(FILES[0])
    reset = SpaceReset(scanned)
    baseline = reset.qos_requirements()
    assert baseline == {kept}
    added = {fake.add_qos(FILES[1]), fake.add_qos("study")}
    record = reset.run({"beta": ["study"]}, qos_baseline=baseline)
    assert set(fake.qos) == {kept}
    stage = record["stages"]["qos"]
    assert (stage["scan"], stage["found"], stage["kept"], stage["removed"]) == (scanned.settings.qos_scan, 3, 1, 2)
    assert stage["affected_files"] == len(FILES)  # the directory's requirement applies to every file under it
    assert not added & set(fake.qos)


def test_o33_the_listing_costs_a_page_per_thousand_entries_and_a_summary_per_entry_a_requirement_is_on(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: 2500 files more: the listing scan reads three pages of the recursive listing on one provider, the summaries
    of the two entries a requirement is on and the two requirements' records, where the summaries scan reads a summary
    per leaf on each of the two providers."""
    for i in range(2500):
        fake.add_file(f"bulk/f-{i:04d}.bin", b"x")
    fake.add_qos(FILES[2])
    fake.add_qos("bulk")
    listing = provisioner_with(client, config, platform_file, ticker, tmp_path, scan="listing")
    before = len(fake.provider_calls)
    assert len(SpaceReset(listing).qos_requirements()) == 2
    made = fake.provider_calls[before:]
    entries = 1 + 2 + len(FILES) + 2500  # the root, two directories, the files
    assert len([c for c in made if c[2].endswith("/files")]) == math.ceil(entries / resetqos.LISTING_PAGE) == 3
    assert len([c for c in made if c[2].endswith("/qos/summary")]) == 2
    assert len([c for c in made if c[2].startswith("/qos_requirements/")]) == 2
    summaries = provisioner_with(client, config, platform_file, ticker, tmp_path)
    before = len(fake.provider_calls)
    SpaceReset(summaries).qos_requirements()
    read = [c for c in fake.provider_calls[before:] if c[2].endswith("/qos/summary")]
    assert len(read) == 2 * (1 + len(FILES) + 2500)


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
        SpaceReset(listing).run({"beta": ["study"]})
    assert len(fake.qos) == 1


def test_o33_a_quiet_reset_records_both_stages_and_their_seconds(scanned, fake, ticker):
    """O33: an agent's requirement whose replication has landed: its files settle (their maps stay the same for three
    seconds), are evicted, and stay quiet through the wait; both stages are in the record with their seconds."""
    fake.qos_replicates_to = BETA_ID
    fake.add_qos("study")
    record = SpaceReset(scanned).run({"beta": ["study"]})
    assert list(record["stages"]) == list(STAGES)
    settled, quiet = record["stages"]["qos_settle"], record["stages"]["quiet"]
    assert settled["files"] == quiet["files"] == len(FILES)
    assert resetqos.SETTLE_STABLE_S <= settled["seconds"] < resetqos.SETTLE_BOUND_S
    assert quiet["seconds"] >= WAIT_S == quiet["waited_s"]
    assert quiet["physical_bytes"]["providers[1]"] == 0


def test_o33_with_no_requirement_removed_the_settle_and_the_quiet_wait_are_skipped(scanned, fake, ticker):
    """O33: nothing of the run's to settle: neither stage reads a block map or waits."""
    fake.add_qos(FILES[0])
    reset = SpaceReset(scanned)
    record = reset.run({"beta": ["study"]}, qos_baseline=reset.qos_requirements())
    assert record["stages"]["qos_settle"]["polls"] == 0
    assert record["stages"]["quiet"]["waited_s"] == 0.0
    assert WAIT_S not in ticker.slept


def test_o33_a_replication_landing_after_the_eviction_fails_the_reset_at_its_quiet_stage(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: a QoS replication has no transfer to cancel or wait for, so one still running lands its bytes after the
    eviction: here, during the quiet wait. The files' block maps after the wait show beta holding them again, and the
    reset fails at its quiet stage, the history never read."""
    file_id = fake.id_of(FILES[3])

    def sleep(seconds):
        ticker.sleep(seconds)
        if seconds == WAIT_S:  # the replication lands while the reset waits
            fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]

    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=sleep)
    fake.add_qos(FILES[3])
    with pytest.raises(ResetFailed, match=r"quiet stage.*grew after the eviction") as caught:
        SpaceReset(provisioner).run({"beta": ["study"]})
    assert list(caught.value.record["stages"]) == list(STAGES[:-1])
    assert caught.value.record["stages"]["quiet"]["seconds"] >= WAIT_S


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
    record = SpaceReset(provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=lands_once)).run(
        {"beta": ["study"]}
    )
    settled = record["stages"]["qos_settle"]
    assert settled["seconds"] >= 2 * resetqos.SETTLE_POLL_S + resetqos.SETTLE_STABLE_S

    second, grown = fake.id_of(FILES[5]), [0]

    def keeps_landing(seconds):
        ticker.sleep(seconds)
        if seconds == resetqos.SETTLE_POLL_S:  # another byte lands on beta every poll
            grown[0] += 1
            fake.blocks[(second, BETA_ID)] = [[0, grown[0]]]

    fake.add_qos(FILES[5])
    with pytest.raises(ResetFailed, match=r"qos_settle stage.*still changing") as caught:
        SpaceReset(provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=keeps_landing)).run(
            {"beta": ["study"]}
        )
    seconds = caught.value.record["stages"]["qos_settle"]["seconds"]
    assert resetqos.SETTLE_BOUND_S <= seconds <= resetqos.SETTLE_BOUND_S + resetqos.SETTLE_POLL_S


def test_o33_the_scan_setting_is_one_of_the_two_and_summaries_by_default(config, platform_file, tmp_path):
    """O33: `[placement] qos_scan` is `summaries` until the listing's live facts are verified, and nothing else."""
    assert load_placement(platform_file, config).settings.qos_scan == "summaries"
    (tmp_path / "bad").mkdir()
    bad = write_platform(tmp_path / "bad", lambda d: with_placement(d, qos_scan="walk"))
    with pytest.raises(Exception, match="qos_scan"):
        load_placement(bad, config)


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
        SpaceReset(listing).run({"beta": ["study"]})
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
        found = resetqos.by_listing(listing, "alpha")
    finally:
        resetqos.LISTING_PAGE = page
    assert set(found.where) == set(fake.qos)
    assert found.reads["listing_pages"] == math.ceil((1 + 1 + len(FILES)) / 2)


@pytest.mark.parametrize(("depth", "lag"), [(1, 10.0), (2, 15.0), (3, 20.0)])
def test_o33_the_quiet_wait_is_twice_the_occupancys_longest_lag_for_the_deepest_file(depth, lag):
    """O33: from the source, a placement reaches the space root's physical size after the file location's flush
    (10 s) and 5 s more for each directory level above the file's parent; the quiet wait doubles that."""
    assert resetqos.occupancy_lag_s(depth) == lag
    assert resetqos.quiet_wait_s(depth) == 2 * lag


def test_o33_a_deeper_affected_file_makes_the_quiet_wait_longer(scanned, fake, ticker):
    """O33: a requirement on a file three directories down waits for that depth's lag, not the shallow one's."""
    fake.add_file("deep/er/still/data.bin", DATA)
    fake.add_qos("deep")
    record = SpaceReset(scanned).run({"beta": ["study"]})
    assert record["stages"]["quiet"]["waited_s"] == resetqos.quiet_wait_s(4)
