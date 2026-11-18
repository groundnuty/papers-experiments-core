"""The HPC driver's wait for its held jobs rides out a slow or restarting Slurm controller (deploy/hpc-federation/
hpcslurm.py, `Slurm.wait_running`; hpcdriver.py, `Driver.wait_held`).

Live, 18 Nov: while four ups waited hours for their held jobs, one `scontrol` that did not answer in 60 s ended each of
them and cancelled its held jobs, and the queue age was lost. A read of the held job that fails during the wait (the
command timing out, or a nonzero exit) is a missed poll: said once per streak, backed off, and waited out; the wait
gives up only after `MISSED_POLLS_MAX` reads in a row failed, or at the queue bound. A job Slurm no longer knows
("Invalid job id"), one that ended and one that carries another name are not missed polls.

Slurm is the fake of hpc_fed_support."""

import itertools

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path
from hpc_fleet_support import SEPARATE_LAYOUT

SERVICES, AGENT = "4242", "4343"  # the fake's two job ids under "separate"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


class Rig:
    """A driver over the fake Slurm, whose clock moves 100 s a reading, and whose reads of the services job's record
    fail as a slow controller's do during the wait for it, and only then. `pattern` has one entry per read: True fails
    it as `how` says ("timeout" is the Runner's own error, any other text is scontrol's stderr under status 1), False
    answers PENDING; once it is used up every read answers RUNNING. The wait's clock stands still unless a test sets
    `clock`; the sleeps the wait asks for are kept in `sleeps`, what `up` says in `said`."""

    def __init__(self, hpc, tmp_path, site, pattern, how="timeout", poll_s=0.0, layout=LAYOUT, fake=None):
        self.hpc, self.site, self.fake = hpc, site, fake or FakeSlurm(state="PENDING")
        self.failed, self.sleeps, self.said, self.waiting = [], [], [], False
        self.clock = lambda: 0.0
        (tmp_path / "fed").mkdir(parents=True, exist_ok=True)
        driver = hpc.cli.make_driver(layout, str(tmp_path / "fed" / "root"), runner=self.fake)
        slurm = driver.slurm
        ticks, steps, real, wait = itertools.count(0, 100).__next__, iter(pattern), self.fake.run, driver.wait_held

        def run(argv, timeout):
            if self.waiting and argv[:5] == ["scontrol", "show", "job", "-o", SERVICES]:
                step = next(steps, None)
                self.fake.state = "RUNNING" if step is None else "PENDING"
                if step:
                    self.failed.append(argv)
                    if how == "timeout":
                        raise hpc.slurm.SlurmError(f"scontrol did not answer in {timeout:.0f} s")
                    return hpc.slurm.Completed(1, "", f"slurm_load_jobs error: {how}\n")
            return real(argv, timeout)

        def waited(held):
            self.waiting = True
            try:
                wait(held)
            finally:
                self.waiting = False

        self.fake.run, driver.wait_held, driver.say = run, waited, self.said.append
        driver.sleep, driver.poll_s, slurm.poll_s = (lambda s: None), 0.0, poll_s
        slurm.clock = lambda: self.clock() if self.waiting else ticks()
        slurm.sleep = lambda seconds: self.sleeps.append(seconds) if self.waiting else None
        driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        driver.relay_available = lambda: False
        self.driver = driver

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    @property
    def missed(self):
        return [line for line in self.said if "missed" in line]


@pytest.mark.parametrize(
    "how",
    [
        "timeout",
        "Socket timed out on send/recv operation",
        "Unable to contact slurm controller (connect failure)",
        "Transport endpoint is not connected",
    ],
)
def test_o57_a_read_of_the_held_job_that_fails_is_a_missed_poll_and_up_goes_on_waiting(hpc, tmp_path, site, how):
    """O57: a read of the held job that fails during the wait (the command timing out, or Slurm's words for a controller
    that is slow or restarting) is a missed poll: the wait says so once, goes on, and `up` succeeds when the controller
    answers; nothing is cancelled."""
    rig = Rig(hpc, tmp_path, site, [True, True], how)
    rig.up()
    assert len(rig.failed) == 2
    assert rig.fake.cancelled == []
    assert len(rig.missed) == 1
    assert SERVICES in rig.missed[0]


def test_o57_reads_that_fail_in_a_row_end_the_wait_and_release_every_held_job_as_before(hpc, tmp_path, site):
    """O57: the wait gives up only after `MISSED_POLLS_MAX` reads in a row failed, and then fails as it always did: it
    says what the last read said and how many failed, and releases every held job of this up, by its id."""
    limit = hpc.slurm.MISSED_POLLS_MAX
    fake = FakeSlurm(state="PENDING", job_states={AGENT: "PENDING"})
    rig = Rig(hpc, tmp_path, site, [True] * limit, layout=SEPARATE_LAYOUT, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError, match="scontrol did not answer in 60 s") as refused:
        rig.up()
    assert len(rig.failed) == limit
    assert f"{limit} reads of the held job in a row failed" in str(refused.value)
    assert sorted(fake.cancelled) == [SERVICES, AGENT]
    assert str(refused.value).endswith(f"the held job(s) {SERVICES}, {AGENT} were cancelled")


def test_o57_an_answer_ends_a_streak_of_failed_reads_and_the_next_one_is_said_again(hpc, tmp_path, site):
    """O57: only reads that fail in a row count: one fewer than the limit, an answer (the job still pending), and as
    many again do not end the wait. Each streak is said once, at its first read."""
    short = hpc.slurm.MISSED_POLLS_MAX - 1
    rig = Rig(hpc, tmp_path, site, [True] * short + [False] + [True] * short, poll_s=5.0)
    rig.up()
    assert len(rig.failed) == 2 * short
    assert len(rig.missed) == 2
    assert rig.fake.cancelled == []


def test_o57_the_backoff_doubles_from_the_poll_interval_to_five_minutes_and_an_answer_restores_it(hpc, tmp_path, site):
    """O57: after a missed poll the wait sleeps the poll interval, then twice it and so on, never more than 300 s; the
    answer that ends a streak is followed by the ordinary poll interval, and the next streak starts from it again."""
    rig = Rig(hpc, tmp_path / "short", site, [True] * 3 + [False] + [True] * 2, poll_s=5.0)
    rig.up()
    assert rig.sleeps == [5.0, 10.0, 20.0, 5.0, 5.0, 10.0]
    rig = Rig(hpc, tmp_path / "long", site, [True] * (hpc.slurm.MISSED_POLLS_MAX - 1), poll_s=5.0)
    rig.up()
    assert rig.sleeps == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]


def test_o57_the_queue_bound_still_ends_a_wait_whose_reads_keep_failing(hpc, tmp_path, site):
    """O57: a streak that has not reached the limit does not outlast the queue bound: at the bound the wait cancels the
    held job and fails as it always did, saying the bound."""
    rig = Rig(hpc, tmp_path, site, [True] * 50)
    rig.clock = itertools.count(0, 400).__next__  # 400 s a reading: the 1800 s bound is passed at the fifth failure
    with pytest.raises(hpc.slurm.SlurmError, match="did not start within 1800 s"):
        rig.up()
    assert 0 < len(rig.failed) < hpc.slurm.MISSED_POLLS_MAX
    assert rig.fake.cancelled == [SERVICES]


def test_o57_a_job_slurm_no_longer_knows_is_no_missed_poll(hpc, tmp_path, site):
    """O57: "Invalid job id" is Slurm saying the job is gone, not a controller that did not answer: the first such read
    ends the wait (the job is cancelled by its id) exactly as before, and nothing is said as a missed poll."""
    rig = Rig(hpc, tmp_path, site, [True], how="Invalid job id specified")
    with pytest.raises(hpc.slurm.SlurmError, match="the held job is gone before it ran"):
        rig.up()
    assert len(rig.failed) == 1
    assert rig.missed == []
    assert rig.fake.cancelled == [SERVICES]
