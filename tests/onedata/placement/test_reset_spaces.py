"""Several spaces reset as one (`SpacesReset`): each space's stages in order, the waits they share taken once, the
spaces' own work at once within `[placement] reset_concurrency`, and a failure in one space failing the reset with
every space's record (O07, O11, O46)."""

import threading

import pytest
from fake_reset import FakeReset

from expcore_onedata import resetqos
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.reset import STAGES, ResetFailed, SpaceReset
from expcore_onedata.resetspaces import ResetPlan, SpacesReset
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
from expcore_onedata.testing.world import SPACE_IDS, SPACE_LABELS, SPACE_NAMES, with_spaces

SETTLE_S, POLLS = 5.0, 3  # the tests' `transfer_settle_s` and `transfer_settle_polls`
FILES = [f"study/part-{i:02d}.bin" for i in range(2)]
COUNT = 3  # spaces


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    """Three spaces supported by alpha, their data provider, and beta, which holds every file of each."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    for space_id, name in zip(SPACE_IDS[1:COUNT], SPACE_NAMES[1:COUNT], strict=True):
        federation.add_data_space(space_id, name, [ALPHA_ID, BETA_ID])
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        for space in SPACE_IDS[:COUNT]:
            federation.grant(user_id, BASELINE, space=space)
    for space in SPACE_IDS[:COUNT]:
        for path in FILES:
            federation.blocks[(federation.add_file(path, DATA, space=space), BETA_ID)] = [[0, len(DATA)]]
    return federation


@pytest.fixture
def platform_file(tmp_path):
    def document(data):
        with_placement(data, transfer_settle_s=SETTLE_S, transfer_settle_polls=POLLS, concurrency=4)
        with_spaces(data, COUNT)

    return write_platform(tmp_path, document)


@pytest.fixture
def resets(client, config, platform_file, ticker, tmp_path) -> list[SpaceReset]:
    """One reset per space, each through its own provisioner, all on the test's clock."""
    settings = load_placement(platform_file, config).settings
    return [
        SpaceReset(
            Provisioner(
                client,
                config,
                settings,
                provider_ids=resolve_provider_ids(config),
                log=tmp_path / f"{label}.jsonl",
                space=label,
                clock=ticker.clock,
                sleep=ticker.sleep,
            ),
            now=ticker.clock,
        )
        for label in SPACE_LABELS[:COUNT]
    ]


def evicting(**options) -> list[ResetPlan]:
    return [ResetPlan({"beta": ["study"]}, **options) for _ in range(COUNT)]


def root_reads(fake, ticker) -> dict[str, list[float]]:
    """The instants of every read of each space root's distribution, the occupancy's reads, by space id."""
    seen: dict[str, list[float]] = {}
    original = fake._distribution

    def timed(node):
        for space, root in fake.space_roots.items():
            if node.file_id == root:
                seen.setdefault(space, []).append(ticker.now)
        return original(node)

    fake._distribution = timed
    return seen


def test_o07_the_transfer_settle_is_waited_once_for_every_space(resets, fake, ticker):
    """O07: three spaces with nothing to cancel take their first observations, one `transfer_settle_s` covers every
    space's second, and each space settles on two observations: the stage waits once, not once per space."""
    record = SpacesReset(resets, concurrency=1).run(evicting())
    assert record["ok"] is True
    for space in record["spaces"]:
        transfers = space["stages"]["transfers"]
        assert (transfers["observations"], transfers["cancelled"], transfers["fast"]) == (2, 0, True)
    assert record["shared_waits"]["transfers"] == {"count": 1, "seconds": SETTLE_S}
    assert record["stages"]["transfers"] == SETTLE_S


def test_o07_a_space_with_a_job_takes_its_full_polls_while_the_others_settle_on_two(resets, fake, ticker):
    """O07: the second space's running job is cancelled and that space needs `transfer_settle_polls` clean
    observations after it; the others still settle on two, within the same waits."""
    fake.add_file("other/left.bin", DATA, space=SPACE_IDS[1])
    tid = fake.add_transfer("replication", "study/part-00.bin", BETA_ID, scheduled=ticker.now, ended=False)
    fake.transfers[tid].file_id = fake.id_of("other/left.bin", SPACE_IDS[1])
    record = SpacesReset(resets, concurrency=1).run(evicting())
    observations = [space["stages"]["transfers"]["observations"] for space in record["spaces"]]
    assert observations == [2, 1 + POLLS, 2]
    assert record["shared_waits"]["transfers"] == {"count": POLLS, "seconds": POLLS * SETTLE_S}


def test_o46_one_occupancy_wait_and_one_quiet_wait_and_look_cover_every_space(resets, fake, ticker):
    """O46: the occupancy stage's wait counts from each space's eviction end; one wait, to the latest of those
    deadlines, covers every space, and each space's occupancy is read no earlier than its own. The quiet stage's wait
    from each space's T and its late look at the listings are taken once for all three likewise."""
    seen = root_reads(fake, ticker)
    record = SpacesReset(resets, concurrency=1).run(evicting(qos_rights=True))
    assert record["shared_waits"]["occupancy"]["count"] == 1
    assert record["shared_waits"]["quiet"] == {"count": 2, "seconds": resetqos.quiet_wait_s(2) + SETTLE_S}
    for space_id in SPACE_IDS[:COUNT]:
        evictions = [job for job in fake.transfers.values() if job.kind == "eviction"]
        finished = max(job.finished for job in evictions if fake.space_of(job.file_id) == space_id)
        assert seen[space_id][0] >= finished + resetqos.occupancy_wait_s(2)
    for space in record["spaces"]:
        assert space["ok"]
        assert list(space["stages"]) == list(STAGES)
        assert space["stages"]["quiet"]["waited_s"] == resetqos.quiet_wait_s(2)


def test_o11_the_spaces_reset_at_once_within_the_bound(resets, fake, ticker, monkeypatch):
    """O11: with `reset_concurrency` 2 three spaces' qos stages run two at once, never three: the first two wait for
    each other, so they cannot pass one at a time."""
    running, peak, calls, lock = [0], [0], [0], threading.Lock()
    together = threading.Barrier(2, timeout=10)
    original = SpaceReset._qos

    def qos(self, baseline, run):
        with lock:
            running[0] += 1
            calls[0] += 1
            peak[0], call = max(peak[0], running[0]), calls[0]
        if call <= 2:
            together.wait()  # one at a time, the first would wait here until the bound
        try:
            return original(self, baseline, run)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(SpaceReset, "_qos", qos)
    allowed = {"beta": len(FILES) * len(DATA)}
    record = SpacesReset(resets, concurrency=2).run([ResetPlan(None, allowed, qos_rights=False)] * COUNT)
    assert record["ok"] is True
    assert (peak[0], record["concurrency"]) == (2, 2)


def test_o11_a_failure_in_one_space_fails_the_reset_with_every_spaces_record(resets, fake, ticker):
    """O11: the second space's plan names a path that is not there, so its eviction stage fails; the other spaces end
    that stage, no space runs a later one, and the failure holds every space's record: what each did, and where it
    stopped."""
    seen = root_reads(fake, ticker)
    plans = evicting()
    plans[1] = ResetPlan({"beta": ["study/missing-part"]})
    with pytest.raises(ResetFailed, match=r"spaces\[1\] at its eviction stage") as caught:
        SpacesReset(resets, concurrency=1).run(plans)
    assert caught.value.stage == "eviction"
    record = caught.value.record
    assert (record["ok"], record["failed"]) == (False, ["spaces[1]"])
    failed, *others = record["spaces"][1], record["spaces"][0], record["spaces"][2]
    assert failed["stages"]["eviction"]["ok"] is False
    for space in others:
        assert list(space["stages"]) == list(STAGES[: STAGES.index("eviction") + 1])
        assert space["stages"]["eviction"]["ok"] is True
        assert (space["ok"], space["halted_before"]) == (False, "occupancy")
    assert seen == {}  # no space read its occupancy
    assert fake.held("study/part-00.bin", BETA_ID, SPACE_IDS[0]) == []  # the first space's eviction ran


def test_o09_a_reset_of_several_spaces_names_each_space_once(resets):
    """O09: two resets of one space would run its stages twice over; the reset is refused before anything is sent."""
    with pytest.raises(ValueError, match="once"):
        SpacesReset([resets[0], resets[1], resets[0]])
    with pytest.raises(ValueError, match="one plan per space"):
        SpacesReset(resets).run([ResetPlan()])
