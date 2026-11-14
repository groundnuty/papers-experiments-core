"""The reset's occupancy stage reads the space root's physical size only once every eviction's and every ended
transfer's change can show in it: from the eviction stage's end, the occupancy's longest lag for the deepest file of the
space and the margin (O46). The 14 Nov live slot saw a mirror's size drop 20.03 s after an eviction at depth 3, where
the reset had taken an eviction to show at once; read sooner, the occupancy stage's figure still holds the evicted
bytes, and the quiet stage, which holds the occupancy to it, can certify quiet over a growth no larger than them."""

import pytest
from qos_world import FILES, WAIT_S, federation, provisioner_with, two_providers

from expcore_onedata import resetqos
from expcore_onedata.reset import OCCUPANCY_ROUNDS, ResetFailed, SpaceReset
from expcore_onedata.testing import BETA_ID, DATA, write_platform
from expcore_onedata.testing.fake_data import ROOT_ID, Transfer

HELD = len(FILES) * len(DATA)  # what beta holds of the space at the start: every file of `study`
SLOW_SETTLE_S = 30.0  # the default `transfer_settle_s`: at least a transfer listing's lag behind a job (O07)
LISTING_LAG_S = 25.0  # how long after it ends an outside job is first listed, within that bound


@pytest.fixture
def fake(monkeypatch, ticker):
    return federation(monkeypatch, ticker)


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture(params=["summaries", "listing"])
def scanned(request, client, config, platform_file, ticker, tmp_path, fake):
    return provisioner_with(client, config, platform_file, ticker, tmp_path, scan=request.param)


def land_after_the_occupancy_stage(fake, file_id):
    """Make `file_id`'s bytes land on beta right after the occupancy stage reads the space root's distribution, before
    the quiet stage reads its block maps at T."""
    original, landed = fake._distribution, []

    def distribution(node):
        answer = original(node)
        if node.file_id == ROOT_ID and not landed:  # the first read of the root is the occupancy stage's
            landed.append(True)
            fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
        return answer

    fake._distribution = distribution


def arm(client) -> None:
    """End the fake's setup with one provider call, so that what changes after it is dated and lags."""
    client.get(f"/data/{ROOT_ID}/distribution", provider="alpha")


def land_at_t(monkeypatch, fake, file_id) -> None:
    """Make `file_id`'s bytes land on beta as the quiet stage reads its block maps, at T."""
    original = resetqos.read_pairs

    def landing(p, pairs):
        fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
        return original(p, pairs)

    monkeypatch.setattr(resetqos, "read_pairs", landing)


def outside_job(fake, ticker, kind, file_id, *, number=1) -> None:
    """A job no stage of the reset sent, which ends now on beta: an eviction removes beta's blocks of the file, a
    replication places them; the listings show it ended, with its finish time."""
    if kind == "eviction":
        del fake.blocks[(file_id, BETA_ID)]
    else:
        fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
    now = int(ticker.now)
    fake.transfers[f"tid-outside-{number}"] = Transfer(
        kind, file_id, BETA_ID, ended=True, scheduled=now, started=now, finished=now
    )


def during_the_occupancy_wait(ticker, act):
    """A sleep that runs `act` once, at the end of the occupancy stage's first wait (`occupancy_wait_s(2)`)."""
    done: list[bool] = []

    def sleep(seconds):
        ticker.sleep(seconds)
        if seconds == resetqos.occupancy_wait_s(2) and not done:
            done.append(True)
            act()

    return sleep


def slow_listings(fake, ticker, tmp_path, late=lambda tid, job: tid.startswith("tid-outside")):
    """Hide each ended job `late` picks (an outside job by default) from every transfer listing until `LISTING_LAG_S`
    after it ended, and return a platform file whose `transfer_settle_s` is `SLOW_SETTLE_S`, the bound the reset takes
    such a lag to be within."""
    original = fake._transfer_page

    def page(query, provider, include):
        def shown(tid, job):
            hidden = job.ended and late(tid, job) and ticker.now < job.finished + LISTING_LAG_S
            return include(tid, job) and not hidden

        return original(query, provider, shown)

    fake._transfer_page = page
    directory = tmp_path / "slow"
    directory.mkdir()

    def edit(data):
        two_providers(data)
        data["placement"]["transfer_settle_s"] = SLOW_SETTLE_S

    return write_platform(directory, edit)


def root_reads(fake, ticker) -> list[float]:
    """Record the instant of every read of the space root's distribution, the occupancy's reads."""
    seen, original = [], fake._distribution

    def timed(node):
        if node.file_id == ROOT_ID:
            seen.append(ticker.now)
        return original(node)

    fake._distribution = timed
    return seen


def test_o46_a_growth_before_t_is_not_hidden_by_an_evictions_drop_that_had_not_shown(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the false certification. The plan evicts `study` from beta, which may keep as much as it held, and a
    replication of another file lands on beta after the occupancy stage, before T. Read right after the eviction, the
    occupancy stage's figure still held the evicted bytes; the quiet stage's read showed the drop and the landing
    together, no more than that figure, and certified quiet. Read once the eviction can show, the figure is 0, and the
    landing is growth over it: the reset fails at its quiet stage."""
    land_after_the_occupancy_stage(fake, fake.add_file("other/late.bin", DATA))
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*more of the space after the quiet wait") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, {"beta": HELD}, qos_rights=True)
    assert caught.value.record["stages"]["occupancy"]["physical_bytes"]["providers[1]"] == 0


def test_o46_an_earlier_runs_eviction_that_had_not_shown_hides_no_growth_with_an_empty_plan(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the eviction need not be the reset's. An agent evicted `study` from beta just before the reset began, and
    the plan evicts nothing; the occupancy stage still waits from the eviction stage's end, so its figure shows the
    agent's drop, and a landing before T is growth over it."""
    arm(client)
    for path in FILES:
        del fake.blocks[(fake.id_of(path), BETA_ID)]
    land_after_the_occupancy_stage(fake, fake.add_file("other/late.bin", DATA))
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*more of the space after the quiet wait") as caught:
        SpaceReset(provisioner, now=ticker.clock).run(None, {"beta": HELD}, qos_rights=True)
    assert caught.value.record["stages"]["occupancy"]["physical_bytes"]["providers[1]"] == 0


def test_o46_an_earlier_runs_replication_that_had_not_shown_fails_the_occupancy_stage(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the other direction, with the quiet check skipped. An earlier run's replication into beta ended just before
    the reset began, on a file four directories down; read at once, the occupancy showed beta holding nothing and the
    reset passed. Read once it can show, beta holds bytes beyond what the reset allows."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))  # the setup's: beta holds nothing of the space
    late = fake.add_file("a/b/c/d/late.bin", DATA)
    arm(client)
    fake.blocks[(late, BETA_ID)] = [[0, len(DATA)]]
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage.*beyond what the reset allows"):
        SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False)


def test_o46_with_nothing_allowed_a_reset_that_evicts_a_mirror_passes_its_occupancy_stage(scanned, fake, ticker):
    """O46: read right after the eviction, the occupancy showed beta still holding what it had just evicted, and a
    reset that allows beta nothing failed there; read once the eviction can show, beta holds nothing, and the stage
    records the wait."""
    occupancy = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=False)["stages"]["occupancy"]
    assert occupancy["physical_bytes"]["providers[1]"] == 0
    assert (occupancy["waited_s"], occupancy["depth"]) == (WAIT_S, 2)


@pytest.mark.parametrize(("extra", "depth"), [(None, 2), ("deep/er/still/data.bin", 4)])
def test_o46_the_occupancy_is_read_no_earlier_than_the_last_evictions_finish_and_the_wait(
    scanned, fake, ticker, extra, depth
):
    """O46: the wait runs from the eviction stage's end, at or after every eviction's finish time, for the deepest
    regular file of the space, which an earlier run's eviction could have reached: with a file four directories down,
    the wait is that depth's even though the plan's files are one directory down, and even with no QoS right."""
    if extra:
        fake.add_file(extra, DATA)
    seen = root_reads(fake, ticker)
    record = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=False)
    finished = max(job.finished for job in fake.transfers.values() if job.kind == "eviction")
    assert seen[0] >= finished + resetqos.occupancy_wait_s(depth)
    assert record["stages"]["occupancy"]["waited_s"] == resetqos.occupancy_wait_s(depth)
    assert record["stages"]["occupancy"]["depth"] == depth


def test_o46_the_listings_are_read_after_the_wait_and_before_the_occupancy(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46, O01: a transfer can start during the wait, so the occupancy stage requires no transfer running after it,
    and reads the occupancy only then."""
    events: list[tuple] = []
    listing, distribution = fake._list_transfers, fake._distribution

    def listed(*args):
        events.append(("listing",))
        return listing(*args)

    def read(node):
        if node.file_id == ROOT_ID:
            events.append(("occupancy",))
        return distribution(node)

    def sleep(seconds):
        events.append(("sleep", seconds))
        ticker.sleep(seconds)

    fake._list_transfers, fake._distribution = listed, read
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=sleep)
    SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=False)
    first = events.index(("occupancy",))
    wait = max(i for i, event in enumerate(events[:first]) if event[0] == "sleep")
    assert events[wait] == ("sleep", resetqos.occupancy_wait_s(2))
    assert ("listing",) in events[wait:first]


def test_o46_an_eviction_that_ends_during_the_occupancy_wait_makes_the_stage_wait_again(
    client, config, platform_file, ticker, tmp_path, fake, monkeypatch
):
    """O46: a job no stage sent can end during the wait, after the instant it counts from. Here an eviction of a file
    beta may keep ends as the wait ends; read then, the figure still held its bytes, and a landing at T, no larger,
    passed the quiet stage. The ended listing's first page shows a job the stage had not seen, so the stage waits again
    from when none runs, reads a figure without the evicted bytes, and the landing fails the quiet stage."""
    kept, late = fake.add_file("other/kept.bin", DATA), fake.add_file("other/late.bin", DATA)
    fake.blocks[(kept, BETA_ID)] = [[0, len(DATA)]]
    land_at_t(monkeypatch, fake, late)
    sleep = during_the_occupancy_wait(ticker, lambda: outside_job(fake, ticker, "eviction", kept))
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=sleep)
    with pytest.raises(ResetFailed, match=r"quiet stage.*more of the space after the quiet wait") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, {"beta": len(DATA)}, qos_rights=True)
    occupancy = caught.value.record["stages"]["occupancy"]
    assert (occupancy["rounds"], occupancy["physical_bytes"]["providers[1]"]) == (2, 0)
    assert occupancy["waited_s"] == 2 * resetqos.occupancy_wait_s(2)


def test_o46_a_replication_that_ends_during_the_occupancy_wait_is_in_the_figure(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the same for a placement, with the quiet check skipped: a replication into beta ends as the wait ends;
    read then, the occupancy showed beta holding nothing and the reset passed. The stage waits again, and beta holds
    bytes beyond what the reset allows."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))  # the setup's: beta holds nothing of the space
    late = fake.add_file("other/late.bin", DATA)
    sleep = during_the_occupancy_wait(ticker, lambda: outside_job(fake, ticker, "replication", late))
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=sleep)
    with pytest.raises(ResetFailed, match=r"occupancy stage.*beyond what the reset allows"):
        SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False)


def test_o46_an_eviction_that_ends_during_the_quiet_stage_fails_it(
    client, config, platform_file, ticker, tmp_path, fake, monkeypatch
):
    """O46: after the occupancy stage's figure, an eviction that ends before the quiet stage's read can hide a growth
    no larger than it there. Here one ends at T, beside a landing of the same size, and the read shows no more than the
    figure: the stage fails, since a transfer ran or ended in its window, and certifies nothing."""
    kept, late = fake.add_file("other/kept.bin", DATA), fake.add_file("other/late.bin", DATA)
    fake.blocks[(kept, BETA_ID)] = [[0, len(DATA)]]
    original = resetqos.read_pairs

    def at_t(p, pairs):
        outside_job(fake, ticker, "eviction", kept)
        fake.blocks[(late, BETA_ID)] = [[0, len(DATA)]]
        return original(p, pairs)

    monkeypatch.setattr(resetqos, "read_pairs", at_t)
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*ran or ended.*certifies nothing") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, {"beta": len(DATA)}, qos_rights=True)
    assert caught.value.record["stages"]["occupancy"]["physical_bytes"]["providers[1]"] == len(DATA)


def test_o46_a_replication_listed_late_after_the_occupancy_wait_is_still_waited_for(
    client, config, ticker, tmp_path, fake
):
    """O46, O07: a transfer listing shows a job up to `transfer_settle_s` late. A replication into beta ends as the
    wait ends and is listed 25 s later; observed at once, the listings showed nothing new and the reading, beta holding
    nothing, passed with the quiet check skipped. With that check skipped the stage observes the listings
    `transfer_settle_s` after its reading, sees the job, waits again, and beta holds bytes beyond what it may."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))  # the setup's: beta holds nothing of the space
    late = fake.add_file("other/late.bin", DATA)
    platform = slow_listings(fake, ticker, tmp_path)
    sleep = during_the_occupancy_wait(ticker, lambda: outside_job(fake, ticker, "replication", late))
    provisioner = provisioner_with(client, config, platform, ticker, tmp_path, sleep=sleep)
    with pytest.raises(ResetFailed, match=r"occupancy stage.*beyond what the reset allows"):
        SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False)


def test_o46_an_eviction_listed_late_during_the_quiet_stage_fails_it(
    client, config, ticker, tmp_path, fake, monkeypatch
):
    """O46, O07: an eviction of a file beta may keep ends at T, beside a landing of its size, and is listed 25 s later.
    The quiet stage's reading at T + 20 s shows the drop and the landing together, no more than the figure, and
    observed at once the listings showed nothing new; the stage observes them `transfer_settle_s` after its reading,
    sees the job, and fails."""
    kept, late = fake.add_file("other/kept.bin", DATA), fake.add_file("other/late.bin", DATA)
    fake.blocks[(kept, BETA_ID)] = [[0, len(DATA)]]
    platform = slow_listings(fake, ticker, tmp_path)
    original = resetqos.read_pairs

    def at_t(p, pairs):
        outside_job(fake, ticker, "eviction", kept)
        fake.blocks[(late, BETA_ID)] = [[0, len(DATA)]]
        return original(p, pairs)

    monkeypatch.setattr(resetqos, "read_pairs", at_t)
    provisioner = provisioner_with(client, config, platform, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*ran or ended.*certifies nothing"):
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, {"beta": len(DATA)}, qos_rights=True)


@pytest.mark.parametrize("rights", [True, False])
def test_o46_the_resets_own_evictions_listed_late_are_no_new_job(client, config, ticker, tmp_path, fake, rights):
    """O46, O07: the reset's own eviction jobs can be listed late too, after the first pages read at the eviction
    stage's end. They had ended by then, so a look that finds them later counts no new job: the reset passes in one
    wait, whether the quiet stage runs or not, where taking them for new would fail every reset that evicts."""
    platform = slow_listings(fake, ticker, tmp_path, late=lambda tid, job: job.kind == "eviction")
    provisioner = provisioner_with(client, config, platform, ticker, tmp_path)
    record = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=rights)
    assert record["ok"]
    assert record["stages"]["occupancy"]["rounds"] == 1
    assert record["stages"]["quiet"]["ran"] is rights


def test_o46_transfers_that_keep_ending_during_the_occupancy_wait_fail_the_stage_at_its_bound(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: a job ending in every wait leaves no reading that shows them all; after `OCCUPANCY_ROUNDS` waits the stage
    fails, closed, rather than read a figure that may miss one."""
    late = fake.add_file("other/late.bin", DATA)
    jobs = iter(range(1, 100))

    def sleep(seconds):
        ticker.sleep(seconds)
        if seconds == resetqos.occupancy_wait_s(2):
            outside_job(fake, ticker, "replication", late, number=next(jobs))

    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path, sleep=sleep)
    with pytest.raises(ResetFailed, match=rf"occupancy stage.*kept ending or running.*{OCCUPANCY_ROUNDS}"):
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, {"beta": len(DATA)}, qos_rights=False)


@pytest.mark.parametrize(("depth", "wait"), [(1, 15.0), (2, 20.0), (3, 25.0), (4, 30.0)])
def test_o46_the_occupancy_stages_wait_is_the_lag_for_the_depth_and_the_margin(depth, wait):
    """O46: the wait from the eviction stage's end is the replication formula's lag, 10 + 5 x (L - 1) s, applied to an
    eviction too, and the margin of 5 s."""
    assert resetqos.occupancy_wait_s(depth) == resetqos.occupancy_lag_s(depth) + resetqos.QUIET_MARGIN_S == wait


def test_o46_the_live_slots_measures_fit_the_waits():
    """O46: the 14 Nov live slot (op-worker f118ed9) saw a placement show in a mirror's root size 10.76 s after it at
    depth 2 and 15.91 s at depth 3, within the lag; and an eviction at depth 3 show 20.03 s after it was scheduled, the
    job's own run included: past the lag alone counted from the scheduling, within the wait, and the wait counts from
    the eviction stage's end, later still."""
    assert resetqos.occupancy_lag_s(2) >= 10.76
    assert resetqos.occupancy_lag_s(3) >= 15.91
    assert resetqos.occupancy_lag_s(3) < 20.03 <= resetqos.occupancy_wait_s(3)
