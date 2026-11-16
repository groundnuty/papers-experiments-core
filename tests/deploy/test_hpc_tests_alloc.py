"""The login node's side of the HPC test runner (deploy/hpc-tests/alloc.sh), against a fake Slurm and a fake clock.

The allocation is shared with jobs that are not ours (another study and other lanes run under the same account), so
every test here also checks what the script must never do: list, renew or cancel a job its ledger does not hold, put
the account on a command line, or cancel anything but the one job id it means.
"""

import fcntl
import re
import subprocess
from datetime import UTC, datetime

import hpct_fakes as fakes
import pytest

NOW = int(datetime(2026, 11, 16, 21, 0, tzinfo=UTC).timestamp())


def slurm_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S")


@pytest.fixture
def login(tmp_path):
    bin_dir, fake, home = tmp_path / "bin", tmp_path / "fake", tmp_path / "home"
    bin_dir.mkdir()
    fake.mkdir()
    fakes.slurm(bin_dir, fake)
    directory = fakes.login_dir(home)
    (fake / "now").write_text(str(NOW))
    env = fakes.environment(bin_dir, fake, home)

    def alloc(*words, check=None):
        out = subprocess.run(
            ["bash", str(directory / "alloc.sh"), *words],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        if check is not None:
            assert out.returncode == check, out.stdout + out.stderr
        return out

    def queue(*rows):
        (fake / "squeue").write_text("".join(f"{'|'.join(map(str, row))}\n" for row in rows))

    def ledger(*ids):
        (directory / "ledger").write_text("".join(f"{i} expcore-tests 1 -\n" for i in ids))

    return type(
        "Login",
        (),
        {
            "alloc": staticmethod(alloc),
            "queue": staticmethod(queue),
            "ledger": staticmethod(ledger),
            "fake": fake,
            "dir": directory,
        },
    )


def running(job, end, name="expcore-tests"):
    return (job, name, "RUNNING", slurm_time(end - 43200), slurm_time(end), 16, "30G")


def test_o64_a_submit_asks_for_a_bounded_unrequeued_sleeping_job_with_the_account_off_the_command_line(login):
    """O64: an allocation is `exec sleep infinity` with --no-requeue, a --time and nothing exported; the account and
    partition reach sbatch through its input variables, never its argv; the id goes into the ledger."""
    out = login.alloc("submit", check=0)
    assert out.stdout.strip() == "SUBMITTED 700"
    (call,) = fakes.calls(login.fake / "sbatch.jsonl")
    argv = call["argv"]
    for word in (
        "--no-requeue",
        "--time=12:00:00",
        "--export=NONE",
        "--job-name=expcore-tests",
        "--cpus-per-task=16",
        "--mem=30G",
        "--chdir=/",
        "--wrap=exec sleep infinity",
    ):
        assert word in argv
    assert not any("acct-x" in word or "std-part" in word for word in argv)
    assert (call["account"], call["partition"]) == ("acct-x", "std-part")
    assert (login.dir / "ledger").read_text().split()[:2] == ["700", "expcore-tests"]
    assert fakes.calls(login.fake / "scancel.jsonl") == []


@pytest.mark.parametrize(
    "record",
    [
        fakes.RECORD.replace("Requeue=0", "Requeue=1"),
        fakes.RECORD.replace("TimeLimit=12:00:00", "TimeLimit=00:15:00"),
        fakes.RECORD.replace("NumCPUs=16", "NumCPUs=32"),
        fakes.RECORD.replace("MinMemoryNode=30G", "MinMemoryNode=36G"),
        fakes.RECORD.replace("Partition=std-part", "Partition=other"),
        "",
    ],
)
def test_o64_a_job_slurm_recorded_otherwise_is_cancelled_by_its_own_id_and_no_other(login, record):
    """O64: what Slurm recorded is read back; a job that is not as asked (or whose record cannot be read) is cancelled
    by the id sbatch has just printed, and that is the only cancel."""
    (login.fake / "record").write_text(record)
    login.queue(running(77, NOW + 3600, name="expcore-tests"))  # not in the ledger: someone else's
    out = login.alloc("submit", check=1)
    assert "NOT AS ASKED" in out.stderr
    assert fakes.calls(login.fake / "scancel.jsonl") == [["700"]]


def test_the_guard_refuses_a_submit_once_the_limit_of_alive_allocations_of_ours_is_reached(login):
    login.ledger(500, 501)
    login.queue(running(500, NOW + 3600), running(501, NOW + 40000))
    out = login.alloc("submit", check=3)
    assert "nothing submitted" in out.stderr
    assert fakes.calls(login.fake / "sbatch.jsonl") == []


def test_a_failed_squeue_submits_nothing(login):
    (login.fake / "squeue-fail").write_text("")
    login.alloc("submit", check=2)
    assert fakes.calls(login.fake / "sbatch.jsonl") == []


def test_o64_the_listing_holds_only_ledger_jobs_under_our_name_in_epoch_seconds(login):
    """O64: `list` names a job only when the ledger holds its id AND squeue shows it under our name: a job of the same
    account outside the ledger (even one with our name), or a ledger id now under another name, is not ours."""
    login.ledger(500, 501)
    login.queue(running(500, NOW + 3600), running(600, NOW + 9000), running(501, NOW + 9000, name="another-study"))
    out = login.alloc("list", check=0).stdout.splitlines()
    assert out[0] == f"NOW {NOW}"
    assert out[1] == f"JOB 500 expcore-tests RUNNING {NOW + 3600 - 43200} {NOW + 3600} 16 30G"
    assert [line for line in out if line.startswith("JOB")] == [out[1]]
    assert out[-1].startswith("RENEWER status=none")


def test_o64_a_release_cancels_only_a_ledger_job_alive_under_our_name(login):
    """O64: `release` cancels one job, by its id, only when the ledger holds it and squeue shows it under our name;
    anything else is refused and nothing is cancelled."""
    login.ledger(500, 501)
    login.queue(running(500, NOW + 3600), running(600, NOW + 9000), running(501, NOW + 9000, name="another-study"))
    assert "not in this ledger" in login.alloc("release", "600", check=3).stderr
    assert "not alive under the name" in login.alloc("release", "501", check=3).stderr
    login.alloc("release", "-u", check=2)
    assert fakes.calls(login.fake / "scancel.jsonl") == []
    assert login.alloc("release", "500", check=0).stdout.strip() == "RELEASED 500"
    assert fakes.calls(login.fake / "scancel.jsonl") == [["500"]]


def test_o64_every_cancel_in_the_script_is_of_one_job_id(login):
    """O64: the script cancels by a job id it holds, never by user, name or state."""
    lines = [x for x in (fakes.DEPLOY / "alloc.sh").read_text().splitlines() if re.search(r"\bscancel\b", x)]
    calls = [x for x in lines if not x.lstrip().startswith("#")]
    assert calls
    assert all(re.search(r'scancel "\$id"', x) for x in calls), calls


def renew_once(login, until=NOW + 86400, wanted=NOW + 86400, idle="0"):
    """One decision; the idle release is off (--idle 0) unless a test asks for it."""
    if wanted is not None:
        (login.dir / "renew.wanted").write_text(f"{wanted}\n")
    return login.alloc("renew", "--until", str(until), "--idle", idle, "--once", check=0)


def test_o64_the_successor_is_submitted_from_the_clock_to_begin_shortly_before_the_allocation_ends(login):
    """O64: with our running allocation ending within the lead (3 h), its successor is submitted at once with
    --begin at the end less the overlap (15 min), from the clock read now; the job outside the ledger is ignored."""
    login.ledger(500)
    login.queue(running(500, NOW + 3600), running(600, NOW + 60))
    out = renew_once(login)
    (call,) = fakes.calls(login.fake / "sbatch.jsonl")
    assert f"--begin={slurm_time(NOW + 3600 - 900)}" in call["argv"]
    assert "SUBMITTED 700" in out.stdout
    assert fakes.calls(login.fake / "scancel.jsonl") == []


@pytest.mark.parametrize(
    ("rows", "until"),
    [
        ([running(500, NOW + 4 * 3600)], NOW + 86400),  # ends after the lead: not yet
        ([running(500, NOW + 3600), (501, "expcore-tests", "PENDING", "N/A", "N/A", 16, "30G")], NOW + 86400),
        ([running(500, NOW + 3600)], NOW + 3600 - 900),  # its successor would begin at --until
    ],
)
def test_no_successor_is_submitted_too_early_twice_or_past_until(login, rows, until):
    login.ledger(500, 501)
    login.queue(*rows)
    renew_once(login, until)
    assert fakes.calls(login.fake / "sbatch.jsonl") == []


def test_o64_with_no_allocation_of_ours_alive_one_is_submitted_now(login):
    """O64: when the allocation has ended with no successor (the renewer was down, or the queue was slow), the next
    poll submits one to begin now: the gap lasts one poll, not until someone notices."""
    login.ledger(400)
    login.queue(running(600, NOW + 3600))
    renew_once(login)
    (call,) = fakes.calls(login.fake / "sbatch.jsonl")
    assert f"--begin={slurm_time(NOW)}" in call["argv"]


@pytest.mark.parametrize("wanted", [None, NOW, NOW + 60])
def test_o64_no_successor_is_submitted_unless_lanes_said_they_need_it_until_after_it_begins(login, wanted):
    """O64: renewal is a deliberate step: billing runs idle or busy, so the loop submits a successor only while
    renew.wanted names a time still ahead and after the successor's begin; without it (or after it) the running
    allocation simply ends."""
    login.ledger(500)
    login.queue(running(500, NOW + 3600))
    renew_once(login, wanted=wanted)
    assert fakes.calls(login.fake / "sbatch.jsonl") == []


def test_want_and_unwant_set_and_clear_the_marker_the_listing_shows(login):
    login.alloc("want", "--until", str(NOW - 1), check=2)
    assert login.alloc("want", "--until", str(NOW + 7200), check=0).stdout.strip() == f"WANTED until {NOW + 7200}"
    assert f"wanted={NOW + 7200}" in login.alloc("list", check=0).stdout
    login.alloc("unwant", check=0)
    assert "wanted=-" in login.alloc("list", check=0).stdout


def test_the_renewer_stops_after_a_submit_that_was_not_as_asked(login):
    (login.fake / "record").write_text(fakes.RECORD.replace("Requeue=0", "Requeue=1"))
    login.ledger(400)
    login.alloc("want", "--until", str(NOW + 86400), check=0)
    login.alloc("renew", "--until", str(NOW + 86400), "--poll", "1", check=0)
    assert len(fakes.calls(login.fake / "sbatch.jsonl")) == 1
    assert "status=ended:submit-not-as-asked" in (login.dir / "renew.state").read_text()
    assert not (login.dir / "renew.lock").exists()


def test_the_loop_honours_the_stop_file_and_a_second_renewer_is_refused(login):
    (login.dir / "renew.stop").write_text("")
    login.alloc("renew", "--until", str(NOW + 86400), "--poll", "1", check=0)
    assert "status=ended:stop-file" in (login.dir / "renew.state").read_text()
    (login.dir / "renew.lock").mkdir()
    (login.dir / "renew.state").write_text(
        f"pid=1 heartbeat={NOW - 10} until=1 submitted=0 max=4 poll=300 status=running\n"
    )
    assert "a renewer is running" in login.alloc("renew", "--until", str(NOW + 86400), check=3).stderr
    assert "RENEWER status=running" in login.alloc("list", check=0).stdout


def test_renew_start_runs_a_frozen_copy_detached_and_removes_it_when_it_ends(login):
    out = login.alloc("renew-start", "--until", str(NOW - 1), "--poll", "1", check=0)
    assert "RENEWER" in out.stdout
    log = (login.dir / "renew.log").read_text()
    assert "renewer ended: until" in log
    assert not list(login.dir.glob(".renew-*.sh"))
    assert not (login.dir / "renew.lock").exists()


def steps(login, job, *rows):
    (login.fake / f"sacct-{job}").write_text("".join("|".join(map(str, row)) + "\n" for row in rows))


def test_o64_an_allocation_of_ours_idle_for_three_hours_is_released_by_its_id_and_the_marker_cleared(login):
    """O64: a RUNNING allocation of ours that has run no test step for 3 h (from its start, when it never ran one) is
    released by its own id; renew.wanted is cleared so the loop does not submit another at once; an idle job of the
    account outside the ledger is never touched."""
    login.ledger(500)
    login.queue(running(500, NOW + 8 * 3600), running(600, NOW + 60))  # 500 started 4 h ago and ran nothing
    steps(login, 500, ("500.batch", slurm_time(NOW - 4 * 3600), "Unknown", "RUNNING"))
    out = renew_once(login, wanted=NOW + 86400, idle="10800")
    assert fakes.calls(login.fake / "scancel.jsonl") == [["500"]]
    assert "RELEASED 500" in out.stdout
    assert not (login.dir / "renew.wanted").exists()
    assert fakes.calls(login.fake / "sbatch.jsonl") == []


@pytest.mark.parametrize(
    "rows",
    [
        [
            ("500.batch", "x", "Unknown", "RUNNING"),
            ("500.0", slurm_time(NOW - 7200), slurm_time(NOW - 3600), "COMPLETED"),
        ],
        [("500.batch", "x", "Unknown", "RUNNING"), ("500.3", slurm_time(NOW - 600), "Unknown", "RUNNING")],
    ],
)
def test_an_allocation_that_ran_a_step_within_three_hours_or_runs_one_now_is_kept(login, rows):
    login.ledger(500)
    login.queue(running(500, NOW + 8 * 3600))
    steps(login, 500, *rows)
    renew_once(login, wanted=None, idle="10800")
    assert fakes.calls(login.fake / "scancel.jsonl") == []


def test_no_idle_release_on_an_unreadable_record_or_with_idle_off(login):
    login.ledger(500)
    login.queue(running(500, NOW + 8 * 3600))
    (login.fake / "sacct-fail").write_text("")
    renew_once(login, wanted=None, idle="10800")
    (login.fake / "sacct-fail").unlink()
    login.alloc("renew", "--until", str(NOW + 86400), "--idle", "0", "--once", check=0)
    assert fakes.calls(login.fake / "scancel.jsonl") == []


def test_an_idle_release_also_releases_our_queued_successor(login):
    login.ledger(500, 501)
    login.queue(running(500, NOW + 8 * 3600), (501, "expcore-tests", "PENDING", "N/A", "N/A", 16, "30G"))
    steps(login, 500, ("500.batch", slurm_time(NOW - 4 * 3600), "Unknown", "RUNNING"))
    renew_once(login, wanted=None, idle="10800")
    assert fakes.calls(login.fake / "scancel.jsonl") == [["500"], ["501"]]


def test_o64_no_idle_release_while_accounting_lags_or_a_step_has_just_started(login):
    """O64: "no steps" is read only from accounting that lists the job's batch step, and the controller's live list
    of steps is asked right before the release: a step a lane has just started keeps the allocation."""
    login.ledger(500)
    login.queue(running(500, NOW + 8 * 3600))
    steps(login, 500)  # accounting knows nothing of the job yet
    renew_once(login, wanted=None, idle="10800")
    steps(login, 500, ("500.batch", slurm_time(NOW - 4 * 3600), "Unknown", "RUNNING"))
    (login.fake / "squeue-steps").write_text("500.batch\n500.7\n")
    renew_once(login, wanted=None, idle="10800")
    assert fakes.calls(login.fake / "scancel.jsonl") == []


def test_o64_no_idle_release_while_a_run_holds_the_use_lock(login):
    """O64: every run holds the login directory's use lock shared from before its step starts to after it ends; the
    idle release takes it exclusively without waiting, so an allocation a run is using, or is about to use, is kept."""
    login.ledger(500)
    login.queue(running(500, NOW + 8 * 3600))
    steps(login, 500, ("500.batch", slurm_time(NOW - 4 * 3600), "Unknown", "RUNNING"))
    with (login.dir / "use.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_SH)
        out = renew_once(login, wanted=None, idle="10800")
    assert "is in use, not idle" in out.stdout
    assert fakes.calls(login.fake / "scancel.jsonl") == []
    renew_once(login, wanted=None, idle="10800")
    assert fakes.calls(login.fake / "scancel.jsonl") == [["500"]]
