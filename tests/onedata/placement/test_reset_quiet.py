"""The reset's quiet stage: after the eviction and the occupancy, the block maps of the files the eviction plan covered
and of the files a removed requirement applied to, and the occupancy, are read again after a wait, and a replication
that landed after the eviction fails the reset (O33)."""

import pytest
from qos_world import FILES, WAIT_S, calls, federation, provisioner_with, two_providers

from expcore_onedata import resetqos
from expcore_onedata.provision import Provisioner
from expcore_onedata.reset import STAGES, ResetFailed, SpaceReset
from expcore_onedata.testing import BETA_ID, DATA, write_platform
from expcore_onedata.testing.fake_data import ROOT_ID

LAGGING_OCCUPANCY = (
    "defect 2 of the #209 review, shown by the fake's lagging occupancy: the quiet stage reads the occupancy once, as "
    "its wait ends, and bytes that landed in the last seconds of it are not in it yet (the lag, 15 s at depth 2); "
    "replaced with the occupancy read at T + lag + margin"
)


@pytest.fixture
def fake(monkeypatch, ticker):
    return federation(monkeypatch, ticker)


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture(params=["summaries", "listing"])
def scanned(request, client, config, platform_file, ticker, tmp_path, fake) -> Provisioner:
    return provisioner_with(client, config, platform_file, ticker, tmp_path, scan=request.param)


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
    assert WAIT_S not in ticker.slept


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
    assert WAIT_S in ticker.slept


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


def test_o33_a_requirement_added_and_removed_by_the_agent_fails_the_reset_when_its_replication_lands_late(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: the gap in #209: the agent adds a requirement and removes it before the run ends. Its replication creates
    no transfer document and no requirement is left, so the reset sees nothing to settle; it lands after the eviction.
    The caller's rights include the right to manage QoS, so the quiet check ran, and the block map of the evicted
    file, read again after the wait, shows beta holding it again."""
    file_id = fake.id_of(FILES[3])

    def sleep(seconds):
        ticker.sleep(seconds)
        if seconds == WAIT_S:  # the replication lands while the reset waits
            fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]

    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=sleep)
    del fake.qos[fake.add_qos(FILES[3])]  # added, then removed: the reset finds none
    with pytest.raises(ResetFailed, match=r"quiet stage.*grew after the eviction") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)
    assert caught.value.record["stages"]["qos"]["removed"] == 0
    assert list(caught.value.record["stages"]) == list(STAGES[:-1])
    assert caught.value.record["stages"]["quiet"]["seconds"] >= WAIT_S


@pytest.mark.xfail(strict=True, raises=pytest.fail.Exception, reason=LAGGING_OCCUPANCY)
def test_o33_a_late_replication_of_a_file_beta_did_not_hold_is_caught_by_the_occupancy(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: a replication that was still landing when the run ended has placed nothing yet, so no block map shows it,
    and its file is not one the eviction plan covers (it is outside `study`, which the plan evicts); it lands after the
    eviction, and the occupancy read after the quiet wait, set against the occupancy stage's, shows beta holding bytes
    of the space again. The failure names the occupancy, so the block maps did not see it."""
    file_id = fake.add_file("other/late.bin", DATA)

    def sleep(seconds):
        ticker.sleep(seconds)
        if seconds == WAIT_S:
            fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]

    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=sleep)
    with pytest.raises(ResetFailed, match=r"quiet stage.*more of the space after the quiet wait") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)
    assert caught.value.record["stages"]["quiet"]["seconds"] >= WAIT_S


def test_o33_the_wait_after_a_rights_run_reaches_the_deepest_file_of_the_space(scanned, fake, ticker):
    """O33: with no requirement to name the files a late replication could reach, the wait is twice the occupancy's lag
    for the deepest file of the space; a run with no QoS right waits for the files it knows alone."""
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
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})
    assert list(caught.value.record["stages"]) == list(STAGES[:-1])
    assert caught.value.record["stages"]["quiet"]["seconds"] >= WAIT_S


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
    record = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]})
    assert record["stages"]["quiet"]["waited_s"] == resetqos.quiet_wait_s(4)


def test_o33_on_a_large_space_the_quiet_stage_reads_only_the_maps_of_the_evicted_files(
    scanned, fake, ticker, monkeypatch
):
    """O33: the quiet stage is bounded by the eviction plan. In a space of 1,500 more files that beta does not hold,
    the plan evicts the six files of `study`; the stage reads those six files' maps on the two providers before the wait
    and again after it, and no file outside the plan has a map read anywhere in the reset, where a read of every file
    of the space on beta before the eviction (1,506 maps) was made until the plan bounded it."""
    for i in range(1500):
        fake.add_file(f"bulk/f-{i:04d}.bin", b"x")
    read, original = [], resetqos.read_maps

    def counted(p, files, providers):
        before = len(calls(fake, "/distribution"))
        found = original(p, files, providers)
        read.append(len(calls(fake, "/distribution")) - before)
        return found

    monkeypatch.setattr(resetqos, "read_maps", counted)
    quiet = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)["stages"]["quiet"]
    assert read == [len(FILES) * 2, len(FILES) * 2]  # each evicted file on both providers, before and after the wait
    assert (quiet["files"], quiet["held_files"], quiet["held_source"]) == (len(FILES), len(FILES), "eviction plan")
    mapped = {c[2].split("/")[2] for c in calls(fake, "/distribution")}
    assert mapped == {fake.id_of(path) for path in FILES} | {ROOT_ID}  # the root's is the occupancy's, not a file's


def test_o33_a_plan_of_file_paths_bounds_the_quiet_stage_to_those_files(scanned, fake, ticker, monkeypatch):
    """O33: a plan that names files, not a directory, evicts them together; the quiet stage reads those files' maps
    alone, and beta keeps the four it was allowed."""
    read, original = [], resetqos.read_maps

    def counted(p, files, providers):
        read.append(sorted(files))
        return original(p, files, providers)

    monkeypatch.setattr(resetqos, "read_maps", counted)
    plan = {"beta": FILES[:2]}
    quiet = SpaceReset(scanned, now=ticker.clock).run(plan, {"beta": 4 * len(DATA)}, qos_rights=True)["stages"]["quiet"]
    assert read == [sorted(fake.id_of(path) for path in FILES[:2])] * 2
    assert (quiet["files"], quiet["held_files"], quiet["held_source"]) == (2, 2, "eviction plan")


@pytest.mark.xfail(strict=True, raises=pytest.fail.Exception, reason=LAGGING_OCCUPANCY)
def test_o33_with_an_empty_plan_the_occupancy_is_still_read_after_the_wait(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O33: nothing is evicted, so no block map is read, but the quiet stage still waits and reads the occupancy:
    a replication that lands on any file during the wait fails the reset on it, and with none the record has the
    occupancy and the wait."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    quiet = SpaceReset(provisioner_with(client, config, platform_file, ticker, tmp_path), now=ticker.clock).run(
        None, qos_rights=True
    )["stages"]["quiet"]
    assert (quiet["ran"], quiet["files"], quiet["held_files"], quiet["waited_s"]) == (True, 0, 0, WAIT_S)
    assert quiet["physical_bytes"]["providers[1]"] == 0
    file_id = fake.id_of(FILES[0])

    def sleep(seconds):
        ticker.sleep(seconds)
        if seconds == WAIT_S:
            fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]

    landing = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=sleep)
    with pytest.raises(ResetFailed, match=r"quiet stage.*more of the space after the quiet wait"):
        SpaceReset(landing, now=ticker.clock).run(None, qos_rights=True)
