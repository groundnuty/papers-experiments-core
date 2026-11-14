"""The reset's quiet stage: after the eviction and the occupancy, the block maps of the files the eviction plan
evicted and of the files a removed requirement applied to are read at one instant T and held to what the eviction and
the settle left, the occupancy is read at T plus the occupancy's lag and a margin, and a replica or a growth that had
landed by T fails the reset (O33). A landing after T is not certified."""

import pytest
from qos_world import FILES, WAIT_S, calls, federation, provisioner_with, two_providers

from expcore_onedata import resetqos
from expcore_onedata.provision import Provisioner
from expcore_onedata.reset import STAGES, ResetFailed, SpaceReset
from expcore_onedata.testing import BETA_ID, DATA, write_platform
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


def land_before_t(fake, file_id, provider=BETA_ID):
    """Make bytes land on `provider` right after the occupancy stage reads the space root's distribution: after the
    eviction was verified, and before the quiet stage reads the block maps at T."""
    original, landed = fake._distribution, []

    def distribution(node):
        answer = original(node)
        if node.file_id == ROOT_ID and not landed:  # the first read of the root is the occupancy stage's
            landed.append(True)
            fake.blocks[(file_id, provider)] = [[0, len(DATA)]]
        return answer

    fake._distribution = distribution


def test_o33_a_quiet_reset_records_both_stages_and_their_seconds(scanned, fake, ticker):
    """O33: an agent's requirement whose replication has landed: its files settle (their maps stay the same for three
    seconds), are evicted, and stay quiet through the wait; both stages are in the record with their seconds."""
    fake.qos_replicates_to = BETA_ID
    fake.add_qos("study")
    record = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]})
    assert list(record["stages"]) == list(STAGES)
    settled, quiet = record["stages"]["qos_settle"], record["stages"]["quiet"]
    assert settled["files"] == quiet["files"] == len(FILES)
    assert (quiet["ran"], quiet["why"]) == (True, "requirements")
    assert resetqos.SETTLE_STABLE_S <= settled["seconds"] < resetqos.SETTLE_BOUND_S
    assert quiet["seconds"] >= WAIT_S == quiet["waited_s"]
    assert quiet["physical_bytes"]["providers[1]"] == 0


def test_o33_with_no_requirement_and_no_qos_rights_the_settle_and_the_quiet_check_are_skipped(scanned, fake, ticker):
    """O33: a run whose condition declares no QoS right, with no requirement of its own at the reset: nothing to settle
    and nothing that could have been added, so neither stage reads a block map or waits, and the record says so."""
    fake.add_qos(FILES[0])
    reset = SpaceReset(scanned, now=ticker.clock)
    record = reset.run({"beta": ["study"]}, qos_baseline=reset.qos_requirements(), qos_rights=False)
    assert record["stages"]["qos_settle"]["polls"] == 0
    quiet = record["stages"]["quiet"]
    assert (quiet["ran"], quiet["why"], quiet["waited_s"]) == (False, None, 0.0)
    assert ticker.slept.count(WAIT_S) == 1  # the occupancy stage's wait for the eviction (O45), and no other


def test_o33_a_run_with_qos_rights_runs_the_quiet_check_with_no_requirement_left(scanned, fake, ticker):
    """O33: an agent that may manage QoS can add a requirement and remove it before the run ends, leaving none for the
    reset to see; the quiet check runs on the rights alone, reads the maps of the files the eviction plan covered,
    waits, and records why it ran, the size and source of the files it read, and its seconds. Nothing lands, so it
    passes."""
    record = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)
    assert record["stages"]["qos"]["removed"] == 0
    assert record["stages"]["qos_settle"]["polls"] == 0  # the settle stays keyed on the affected files
    quiet = record["stages"]["quiet"]
    assert (quiet["ran"], quiet["why"], quiet["qos_rights"]) == (True, "rights", True)
    assert quiet["files"] == quiet["held_files"] == len(FILES)  # every file the plan evicts from beta
    assert quiet["held_source"] == "eviction plan"
    assert quiet["waited_s"] == WAIT_S
    assert quiet["seconds"] >= WAIT_S
    assert quiet["physical_bytes"]["providers[1]"] == 0
    assert ticker.slept.count(WAIT_S) == 2  # the occupancy stage's (O45), then the quiet stage's


def test_o33_a_caller_that_does_not_say_gets_the_quiet_check_and_the_record_says_the_default(scanned, fake, ticker):
    """O33: fail safe: a caller that passes no `qos_rights` has the quiet check run, recorded as the default."""
    quiet = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]})["stages"]["quiet"]
    assert (quiet["ran"], quiet["why"], quiet["qos_rights"]) == (True, "default", None)
    assert quiet["waited_s"] == WAIT_S


@pytest.mark.parametrize("rights", [False, True, None])
def test_o33_a_requirement_found_at_the_reset_is_why_the_quiet_check_ran_whatever_the_rights(
    scanned, fake, ticker, rights
):
    """O33: a requirement removed at the reset is the first reason, ahead of the rights and the default."""
    fake.add_qos("study")
    quiet = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=rights)["stages"]["quiet"]
    assert (quiet["ran"], quiet["why"], quiet["qos_rights"]) == (True, "requirements", rights)


@pytest.mark.parametrize("bad", ["yes", 1, 0, [True]])
def test_o33_qos_rights_is_true_false_or_not_said(scanned, ticker, bad):
    """O33: the rights are a bool, or None for a caller that does not say; any other value is refused before a stage."""
    with pytest.raises(ValueError, match="qos_rights"):
        SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=bad)


def test_o33_a_requirement_added_and_removed_by_the_agent_fails_the_reset_when_its_replication_landed_by_t(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: the gap in #209: the agent adds a requirement and removes it before the run ends. Its replication creates
    no transfer document and no requirement is left, so the reset sees nothing to settle; it lands after the eviction
    was verified. The caller's rights include the right to manage QoS, so the quiet check ran, and the evicted file's
    block map, read at T, shows beta holding it again: the reset fails there, without waiting."""
    land_before_t(fake, fake.id_of(FILES[3]))
    del fake.qos[fake.add_qos(FILES[3])]  # added, then removed: the reset finds none
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*hold more than the eviction or the settle left") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)
    assert caught.value.record["stages"]["qos"]["removed"] == 0
    assert list(caught.value.record["stages"]) == list(STAGES[:-1])
    assert ticker.slept.count(WAIT_S) == 1  # the occupancy stage's (O45): the quiet stage failed at T, unwaited


def test_o33_a_late_replication_of_a_file_beta_did_not_hold_is_caught_by_the_occupancy(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: a replication that was still landing when the run ended has placed nothing yet, so no block map shows it,
    and its file is not one the eviction plan covers (it is outside `study`, which the plan evicts); it lands after the
    eviction was verified, before T. The occupancy, which lags a placement by 15 s at this depth and is read at T + 15 s
    + the margin, shows beta holding bytes of the space again. The failure names the occupancy, so the block maps did
    not see it."""
    land_before_t(fake, fake.add_file("other/late.bin", DATA))
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*more of the space after the quiet wait") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)
    assert caught.value.record["stages"]["quiet"]["seconds"] >= WAIT_S


def test_o33_a_landing_after_t_is_not_certified_by_the_quiet_stage(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: the limit the stage states: it proves nothing about a landing after T. Bytes that land as the wait ends
    show in the occupancy 15 s later, which nothing reads, so the stage passes and beta holds them afterwards. A
    replication still running at T can do this, whichever scan found its requirement."""
    file_id = fake.add_file("other/late.bin", DATA)
    waits: list[float] = []

    def sleep(seconds):
        ticker.sleep(seconds)
        if seconds == WAIT_S:
            waits.append(seconds)
        if len(waits) == 2 and seconds == WAIT_S:  # the quiet wait, after the occupancy stage's (O45): after T
            fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]

    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=sleep)
    record = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)
    quiet = record["stages"]["quiet"]
    assert (quiet["ran"], quiet["physical_bytes"]["providers[1]"]) == (True, 0)
    ticker.sleep(resetqos.occupancy_lag_s(2))
    answer = client.get(f"/data/{ROOT_ID}/distribution", provider="alpha")
    backend = answer["distributionPerProvider"][BETA_ID]["distributionPerStorageBackend"]["storage-1"]
    assert backend["physicalSize"] == len(DATA)  # it did land, and the reset reported clean


def test_o33_the_block_maps_are_read_at_t_and_the_occupancy_at_t_plus_the_quiet_wait(
    scanned, fake, ticker, monkeypatch
):
    """O33: the occupancy is read exactly `quiet_wait_s` after the last block map of the stage, so that it certifies
    the instant the maps describe."""
    seen, original = [], fake._distribution

    def timed(node):
        seen.append((node.file_id, ticker.now))
        return original(node)

    fake._distribution = timed
    SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)
    files = {fake.id_of(path) for path in FILES}
    last_map = max(when for file_id, when in seen if file_id in files)
    last_root = max(when for file_id, when in seen if file_id == ROOT_ID)
    assert last_root - last_map == WAIT_S


def test_o33_a_file_a_removed_requirement_applied_to_is_held_to_what_the_settle_saw(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: a file the plan does not evict is held to the map the settle ended on. Beta holds half of `other/data.bin`
    when the settle ends, and the rest lands after the occupancy stage, before T: the stage's block map shows beta
    holding more than the settle saw, and the reset fails."""
    file_id = fake.add_file("other/data.bin", DATA)
    fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA) // 2]]
    fake.add_qos("other")
    land_before_t(fake, file_id)
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*hold more than the eviction or the settle left"):
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, {"beta": len(DATA)})


def test_o33_the_wait_after_a_rights_run_reaches_the_deepest_file_of_the_space(scanned, fake, ticker):
    """O33: with no requirement to name the files a late replication could reach, the wait is the occupancy's lag for
    the deepest file of the space and the margin; a run with no QoS right waits for the files it knows alone."""
    fake.add_file("deep/er/still/data.bin", DATA)
    assert SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)["stages"]["quiet"][
        "waited_s"
    ] == (resetqos.quiet_wait_s(4))


def test_o33_a_run_without_qos_rights_waits_only_for_the_files_it_knows(scanned, fake, ticker):
    """O33: a requirement found at a run that declares no QoS right names its files; beta holds the study files too, and
    the deeper file is neither held nor affected, so the wait is that of the files the check covers."""
    fake.add_file("deep/er/still/data.bin", DATA)
    fake.add_qos(FILES[0])
    quiet = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=False)["stages"]["quiet"]
    assert quiet["waited_s"] == WAIT_S


def test_o33_a_replication_landing_after_the_eviction_fails_the_reset_at_its_quiet_stage(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: a QoS replication has no transfer to cancel or wait for, so one still running lands its bytes after the
    eviction was verified. The evicted file's block map at T shows beta holding it again, and the reset fails at its
    quiet stage, the history never read."""
    land_before_t(fake, fake.id_of(FILES[3]))
    fake.add_qos(FILES[3])
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*hold more than the eviction or the settle left") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})
    assert list(caught.value.record["stages"]) == list(STAGES[:-1])


@pytest.mark.parametrize(("depth", "lag"), [(1, 10.0), (2, 15.0), (3, 20.0)])
def test_o33_the_quiet_wait_is_the_occupancys_longest_lag_for_the_deepest_file_and_a_margin(depth, lag):
    """O33: from the source, a placement reaches the space root's physical size after the file location's flush
    (10 s) and 5 s more for each directory level above the file's parent; the quiet wait from T is that lag and a
    margin, by which every placement that landed by T shows."""
    assert resetqos.occupancy_lag_s(depth) == lag
    assert resetqos.quiet_wait_s(depth) == lag + resetqos.QUIET_MARGIN_S


def test_o33_a_deeper_affected_file_makes_the_quiet_wait_longer(scanned, fake, ticker):
    """O33: a requirement on a file three directories down waits for that depth's lag, not the shallow one's."""
    fake.add_file("deep/er/still/data.bin", DATA)
    fake.add_qos("deep")
    record = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]})
    assert record["stages"]["quiet"]["waited_s"] == resetqos.quiet_wait_s(4)


def test_o33_on_a_large_space_the_quiet_stage_reads_only_the_maps_of_the_evicted_files(
    scanned, fake, ticker, monkeypatch
):
    """O33: the quiet stage is bounded by the eviction plan. In a space of 1,500 more files that beta does not hold,
    the plan evicts the six files of `study` from beta; the stage reads those six pairs' maps once, at T, and no file
    outside the plan has a map read anywhere in the reset, where a read of every file of the space on beta before the
    eviction (1,506 maps) was made until the plan bounded it."""
    for i in range(1500):
        fake.add_file(f"bulk/f-{i:04d}.bin", b"x")
    read, original = [], resetqos.read_pairs

    def counted(p, pairs):
        before = len(calls(fake, "/distribution"))
        found = original(p, pairs)
        read.append(len(calls(fake, "/distribution")) - before)
        return found

    monkeypatch.setattr(resetqos, "read_pairs", counted)
    quiet = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)["stages"]["quiet"]
    assert read == [len(FILES)]  # each evicted file on beta, at T
    assert (quiet["files"], quiet["held_files"], quiet["held_source"]) == (len(FILES), len(FILES), "eviction plan")
    assert quiet["block_maps"] == len(FILES)
    mapped = {c[2].split("/")[2] for c in calls(fake, "/distribution")}
    assert mapped == {fake.id_of(path) for path in FILES} | {ROOT_ID}  # the root's is the occupancy's, not a file's


def test_o33_a_plan_of_file_paths_bounds_the_quiet_stage_to_those_files(scanned, fake, ticker, monkeypatch):
    """O33: a plan that names files, not a directory, evicts them together; the quiet stage reads those files' maps on
    beta alone, once, and beta keeps the four it was allowed."""
    read, original = [], resetqos.read_pairs

    def counted(p, pairs):
        read.append(sorted(pairs))
        return original(p, pairs)

    monkeypatch.setattr(resetqos, "read_pairs", counted)
    plan = {"beta": FILES[:2]}
    quiet = SpaceReset(scanned, now=ticker.clock).run(plan, {"beta": 4 * len(DATA)}, qos_rights=True)["stages"]["quiet"]
    assert read[-1] == sorted((fake.id_of(path), "beta") for path in FILES[:2])
    assert (quiet["files"], quiet["held_files"], quiet["held_source"]) == (2, 2, "eviction plan")


def test_o33_with_an_empty_plan_the_occupancy_is_still_read_after_the_wait(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: nothing is evicted, so no block map is read, but the quiet stage still waits and reads the occupancy:
    a replication that landed on any file by T fails the reset on it, and with none the record has the occupancy and
    the wait."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    quiet = SpaceReset(provisioner_with(client, config, platform_file, ticker, tmp_path), now=ticker.clock).run(
        None, qos_rights=True
    )["stages"]["quiet"]
    assert (quiet["ran"], quiet["files"], quiet["held_files"], quiet["waited_s"]) == (True, 0, 0, WAIT_S)
    assert quiet["block_maps"] == 0
    assert quiet["physical_bytes"]["providers[1]"] == 0
    land_before_t(fake, fake.id_of(FILES[0]))
    landing = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*more of the space after the quiet wait"):
        SpaceReset(landing, now=ticker.clock).run(None, qos_rights=True)
