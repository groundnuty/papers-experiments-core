"""The HPC driver's wait for its held jobs, and the reads right after they start, ride out a slow or restarting Slurm
controller (deploy/hpc-federation/hpcslurm.py, `Slurm.ride_out`, `wait_running`, `checked`; hpcdriver.py,
`Driver.wait_held`, `keep_off_other_federations`).

Live, 18 Nov: while four ups waited hours for their held jobs, one `scontrol` that did not answer in 60 s ended each of
them and cancelled its held jobs, and the queue age was lost. A read of the held job that fails during the wait (the
command timing out, or a nonzero exit) is a missed poll: said once per streak, backed off, and waited out; the wait
gives up only once every read has failed for `MISSED_POLLS_GIVE_UP_S` (600 s) since the streak's first, or at the queue
bound. A job Slurm no longer knows ("Invalid job id"), one that ended and one that carries another name are not missed
polls.

Slurm is the fake of hpc_fed_support."""

import itertools

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path
from hpc_fleet_support import SEPARATE_LAYOUT

SERVICES, AGENT = "4242", "4343"  # the fake's two job ids under "separate"
SHOW_SERVICES = ["scontrol", "show", "job", "-o", SERVICES]
# With a poll interval of 5 s the reads of one streak are at 0, 5, 15, 35, 75, 135, 195, 255, 315, 375, 435, 495 and
# 555 s (sleeps of 5, 10, 20, 40, then 60 each): the 14th, at 615 s, is the first that passes 600 s.
READS_BEFORE_GIVING_UP = 13


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
    """A driver over the fake Slurm, whose reads fail as a slow controller's do in one `phase` of `up`, and only there:
    "submit" (the read-back of the held job just submitted), "wait" (the wait for it to run), "readback" (the read of
    the running job, before the placement) or "exclusion" (the second read of the account's jobs, `squeue --me`). The
    failing read is the services job's `scontrol show job` (`squeue --me` in "exclusion"). `pattern` has one entry per
    read of the phase: True fails it as `how` says ("timeout" is the Runner's own error, any other text is the
    command's stderr under status 1); in "wait" False answers PENDING and, once the pattern is used up, every read
    answers RUNNING; in the other phases False and a used-up pattern answer as the fake does. In a phase the clock is
    `now`, which only the sleeps move (`sleeps` keeps them): a read takes no time. `failed_at` is `now` at each failed
    read; what `up` says is in `said`."""

    def __init__(self, hpc, tmp_path, site, pattern, how="timeout", poll_s=5.0, layout=LAYOUT, fake=None, phase="wait"):
        self.hpc, self.site, self.phase = hpc, site, None
        self.fake = fake or FakeSlurm(state="PENDING" if phase == "wait" else "RUNNING")
        self.now, self.failed_at, self.sleeps, self.said = 0.0, [], [], []
        (tmp_path / "fed").mkdir(parents=True, exist_ok=True)
        driver = hpc.cli.make_driver(layout, str(tmp_path / "fed" / "root"), runner=self.fake)
        slurm = driver.slurm
        ticks, steps, real = itertools.count(0, 100).__next__, iter(pattern), self.fake.run

        def run(argv, timeout):
            squeue = argv[:2] == ["squeue", "--me"]
            if self.phase == phase and (squeue if phase == "exclusion" else argv[:5] == SHOW_SERVICES):
                step = next(steps, None)
                if phase == "wait":
                    self.fake.state = "RUNNING" if step is None else "PENDING"
                if step:
                    self.failed_at.append(self.now)
                    if how == "timeout":
                        raise hpc.slurm.SlurmError(f"{argv[0]} did not answer in {timeout:.0f} s")
                    return hpc.slurm.Completed(1, "", f"{argv[0]}: error: {how}\n")
            return real(argv, timeout)

        def within(name, method):
            def wrapped(*args):
                self.phase = name
                try:
                    return method(*args)
                finally:
                    self.phase = None

            return wrapped

        wait = driver.wait_held

        def waited(held):  # the read-back of the running job follows the wait, until the exclusion read
            self.phase = "wait"
            try:
                wait(held)
            except BaseException:
                self.phase = None
                raise
            self.phase = "readback"

        release = driver.release_held

        def released(held):  # a failure ends the phase it was in: the release's own reads are never the injected ones
            self.phase = None
            return release(held)

        def sleep(seconds):
            if self.phase is not None:
                self.sleeps.append(seconds)
                self.now += seconds

        self.fake.run, driver.say = run, self.said.append
        driver.submit = within("submit", driver.submit)
        driver.wait_held, driver.release_held = waited, released
        driver.keep_off_other_federations = within("exclusion", driver.keep_off_other_federations)
        driver.sleep, driver.poll_s, slurm.poll_s, slurm.sleep = (lambda s: None), 0.0, poll_s, sleep
        slurm.clock = lambda: self.now if self.phase is not None else ticks()
        driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        driver.relay_available = lambda: False
        self.driver = driver

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    @property
    def missed(self):
        return [line for line in self.said if "missed" in line]

    @property
    def streak_s(self):
        """How long the failed reads lasted, from the first to the last."""
        return self.failed_at[-1] - self.failed_at[0]


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
    assert len(rig.failed_at) == 2
    assert rig.fake.cancelled == []
    assert len(rig.missed) == 1
    assert SERVICES in rig.missed[0]


@pytest.mark.parametrize(
    ("phase", "how"),
    [
        ("submit", "timeout"),
        ("readback", "timeout"),
        ("readback", "Socket timed out on send/recv operation"),
        ("exclusion", "timeout"),
        ("exclusion", "Unable to contact slurm controller (connect failure)"),
    ],
)
def test_o57_a_read_made_after_the_job_started_that_fails_is_read_again_and_up_succeeds(
    hpc, tmp_path, site, phase, how
):
    """O57: the reads that follow the jobs' start meet the same slow controller, and failing there loses jobs that
    already run: the read-back of the held job (just submitted, and running), and the second read of the account's jobs
    that keeps the federation off its neighbours' nodes. A read that fails there is read again, backed off, and `up`
    succeeds when the controller answers; nothing is cancelled."""
    rig = Rig(hpc, tmp_path, site, [True, True], how, phase=phase)
    rig.up()
    assert len(rig.failed_at) == 2
    assert rig.sleeps[:2] == [5.0, 10.0]
    assert rig.fake.cancelled == []
    assert len(rig.missed) == 1


@pytest.mark.parametrize(
    ("phase", "said", "released"),
    [
        ("readback", "scontrol did not answer in 60 s", [SERVICES]),  # the agent's job is submitted after it
        ("exclusion", "the account's live federation jobs could not be read", [SERVICES, AGENT]),
    ],
)
def test_o57_a_read_after_the_start_that_fails_for_ten_minutes_releases_every_held_job_as_before(
    hpc, tmp_path, site, phase, said, released
):
    """O57: those reads too give up only once every one has failed for `MISSED_POLLS_GIVE_UP_S` since the streak's
    first, not before, and then fail as they always did: what the last read said, how long, and every held job of this
    up released by its id (under "separate" the read-back of the services' job comes before the agent's job exists)."""
    fake = FakeSlurm(state="RUNNING")
    rig = Rig(hpc, tmp_path, site, [True] * 1000, layout=SEPARATE_LAYOUT, fake=fake, phase=phase)
    with pytest.raises(hpc.slurm.SlurmError, match=said) as refused:
        rig.up()
    assert rig.streak_s >= hpc.slurm.MISSED_POLLS_GIVE_UP_S > rig.failed_at[-2] - rig.failed_at[0]
    assert f"{len(rig.failed_at)} reads in a row failed over {rig.streak_s:.0f} s" in str(refused.value)
    assert sorted(fake.cancelled) == released
    assert str(refused.value).endswith(f"the held job(s) {', '.join(released)} were cancelled")


def test_o57_a_held_job_whose_id_was_reused_during_the_retries_is_never_cancelled(hpc, tmp_path, site):
    """O57: ten minutes of failed reads is long enough for a held job to end and Slurm to reuse its id. The release that
    follows the give-up reads each job first, as every other release does, so the job that now carries another name is
    not cancelled and is named, and the other held job is."""
    fake = FakeSlurm(state="RUNNING")
    rig = Rig(hpc, tmp_path, site, [True] * 1000, layout=SEPARATE_LAYOUT, fake=fake, phase="exclusion")
    real = fake.run

    def reused(argv, timeout):
        if len(rig.failed_at) >= 3:  # the agent's id is another job's from the third failed read on
            fake.component_recorded[(AGENT, 0)] = {"JobName": "somebody-elses-analysis"}
        return real(argv, timeout)

    fake.run = reused
    with pytest.raises(hpc.slurm.SlurmError, match="reads in a row failed over") as refused:
        rig.up()
    assert fake.cancelled == [SERVICES]
    assert f"job(s) {AGENT} carry another job's name and were not cancelled" in str(refused.value)


def test_o57_reads_that_all_fail_for_ten_minutes_end_the_wait_and_release_every_held_job_as_before(hpc, tmp_path, site):
    """O57: the wait gives up once every read has failed for `MISSED_POLLS_GIVE_UP_S` since the streak's first, not
    before, and then fails as it always did: it says what the last read said and for how long, and releases every held
    job of this up, by its id. The backoff never sleeps longer than its cap."""
    fake = FakeSlurm(state="PENDING")
    rig = Rig(hpc, tmp_path, site, [True] * 1000, layout=SEPARATE_LAYOUT, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError, match="scontrol did not answer in 60 s") as refused:
        rig.up()
    assert len(rig.failed_at) == READS_BEFORE_GIVING_UP + 1
    assert rig.streak_s >= hpc.slurm.MISSED_POLLS_GIVE_UP_S > rig.failed_at[-2] - rig.failed_at[0]
    assert f"{len(rig.failed_at)} reads in a row failed over {rig.streak_s:.0f} s" in str(refused.value)
    assert max(rig.sleeps) == hpc.slurm.MISSED_POLL_BACKOFF_MAX_S == 60.0
    assert fake.cancelled == [SERVICES]  # the agent's job is submitted once the services' runs: none exists yet
    assert str(refused.value).endswith(f"the held job(s) {SERVICES} were cancelled")


def test_o57_an_answer_ends_a_streak_of_failed_reads_and_the_next_one_is_said_again(hpc, tmp_path, site):
    """O57: only an unbroken streak counts: one read short of the limit, an answer (the job still pending), and as many
    again, with more than the limit's time in failed reads all told, do not end the wait. Each streak is said once, at
    its first read."""
    short = READS_BEFORE_GIVING_UP
    rig = Rig(hpc, tmp_path, site, [True] * short + [False] + [True] * short)
    rig.up()
    assert len(rig.failed_at) == 2 * short
    assert rig.failed_at[-1] - rig.failed_at[0] > hpc.slurm.MISSED_POLLS_GIVE_UP_S
    assert rig.failed_at[short - 1] - rig.failed_at[0] < hpc.slurm.MISSED_POLLS_GIVE_UP_S
    assert len(rig.missed) == 2
    assert rig.fake.cancelled == []


def test_o57_the_backoff_doubles_from_the_poll_interval_to_a_minute_and_an_answer_restores_it(hpc, tmp_path, site):
    """O57: after a missed poll the wait sleeps the poll interval, then twice it and so on, never more than 60 s, so
    that several reads fit in the ten minutes; the answer that ends a streak is followed by the ordinary poll interval,
    and the next streak starts from it again."""
    rig = Rig(hpc, tmp_path / "short", site, [True] * 3 + [False] + [True] * 2)
    rig.up()
    assert rig.sleeps == [5.0, 10.0, 20.0, 5.0, 5.0, 10.0]
    rig = Rig(hpc, tmp_path / "long", site, [True] * 7)
    rig.up()
    assert rig.sleeps == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0]


def test_o57_the_queue_bound_still_ends_a_wait_whose_reads_keep_failing(hpc, tmp_path, site):
    """O57: a streak shorter than the limit does not outlast the queue bound: at the bound the wait fails as it always
    did, saying the bound, and the held job is released. Here the job waits 1500 s, then the reads fail."""
    rig = Rig(hpc, tmp_path, site, [False] * 15 + [True] * 50, poll_s=100.0)
    with pytest.raises(hpc.slurm.SlurmError, match="did not start within 1800 s, and the last read of it failed"):
        rig.up()
    assert 0 < rig.streak_s < hpc.slurm.MISSED_POLLS_GIVE_UP_S
    assert rig.fake.cancelled == [SERVICES]


def test_o57_a_first_failed_read_past_the_queue_bound_ends_the_wait_without_saying_it_goes_on(hpc, tmp_path, site):
    """O57: a read that fails once the bound has passed ends the wait at once (it is no missed poll to wait out), and
    nothing is said about waiting on."""
    rig = Rig(hpc, tmp_path, site, [False] * 18 + [True] * 5, poll_s=100.0)
    with pytest.raises(hpc.slurm.SlurmError, match="did not start within 1800 s, and the last read of it failed"):
        rig.up()
    assert len(rig.failed_at) == 1
    assert rig.missed == []
    assert rig.fake.cancelled == [SERVICES]


def test_o57_a_job_slurm_no_longer_knows_is_no_missed_poll(hpc, tmp_path, site):
    """O57: "Invalid job id" is Slurm saying the job is gone, not a controller that did not answer: the first such read
    ends the wait (the job is cancelled by its id) exactly as before, and nothing is said as a missed poll."""
    rig = Rig(hpc, tmp_path, site, [True], how="Invalid job id specified")
    with pytest.raises(hpc.slurm.SlurmError, match="the held job is gone before it ran"):
        rig.up()
    assert len(rig.failed_at) == 1
    assert rig.missed == []
    assert rig.fake.cancelled == [SERVICES]
