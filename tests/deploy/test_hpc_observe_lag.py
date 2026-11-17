"""The HPC federation's observed read waits, bounded, for Slurm's accounting to catch up with a step the driver stopped
(deploy/hpc-federation/hpcobserve.py, hpcslurm.Slurm, O86).

The driver confirms a stop once `squeue -s` no longer lists the step; `sacct` follows seconds later. A read made in
between saw a step neither listed nor recorded as ended, which is unknown, and in the first study's order (the target is
stopped just before the window's start reading) every run would read unknown. Over a small fake of the two read
commands whose accounting shows the stop only after a time that its fake clock counts: nothing really sleeps, and no
cluster, node or step is made.
"""

import json
import math

import pytest
from hpc_fed_support import hpc_on_path
from observe_lag_support import ENDED_AT, NODE, NOT_CONFIRMED, STOP, Clock, Cluster, Step, state_of

from expcore_onedata import fedwatch


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcobserve

        yield type("Observe", (), {"slurm": modules.slurm, "observe": hpcobserve})


def stopped_alpha(**more):
    return Step("4242.5", "alloc-alpha-run-1", stopped=True, **more)


def read(hpc, state, cluster, clock, *, wait=60.0):
    slurm = hpc.slurm.Slurm(cluster, command_timeout_s=5, sleep=clock.sleep, clock=clock.monotonic)
    return hpc.observe.read(state, slurm, now=lambda: 0.0, accounting_wait_s=wait)


ALPHA_STOPPED = state_of(alpha=("op", 1, "4242.5", [STOP], "stopped"))


@pytest.mark.parametrize(
    "step",
    [
        stopped_alpha(lag=5.0, before="running"),
        stopped_alpha(lag=5.0, before="none"),
        stopped_alpha(lag=5.0, before="completing"),
        stopped_alpha(lag=5.0, before="unended"),
        stopped_alpha(lag=5.0, before="ended", listed_for=5.0),
        stopped_alpha(lag=5.0, before="running", listed_for=5.0),
    ],
    ids=["a row that still runs", "no row yet", "a state not ended", "no end time", "squeue behind", "both behind"],
)
def test_o86_a_stopped_step_whose_accounting_is_behind_is_waited_for_and_read_as_ended(hpc, step):
    """O86: the state says the step was stopped, squeue no longer lists it and accounting does not yet show it ended
    (no row, a row that runs, one not ended, one with no end time; or squeue listing a step accounting has ended): the
    read polls every 2 s until accounting agrees, reads the step as ended with its own end and exit, and records the
    seconds it waited."""
    clock = Clock()
    alpha = read(hpc, ALPHA_STOPPED, Cluster(clock, step), clock)["services"]["alpha"]
    assert (alpha["running"], alpha["state"], alpha["ended_at"], alpha["exit_code"]) == (
        False,
        "CANCELLED",
        ENDED_AT,
        143,
    )
    assert alpha["unknown"] is None
    assert (alpha["step"], alpha["launch"]) == ("4242.5", 1)
    assert alpha["accounting_wait_s_used"] == 6.0  # polls at 0, 2, 4 behind; at 6 s the row has an end
    assert clock.slept == [2.0, 2.0, 2.0]


def test_o86_a_stopped_step_that_already_reads_ended_is_read_at_once(hpc):
    """O86: nothing to wait for: the first read agrees, with no pause and no wait recorded."""
    clock = Clock()
    alpha = read(hpc, ALPHA_STOPPED, Cluster(clock, stopped_alpha()), clock)["services"]["alpha"]
    assert (alpha["running"], alpha["state"], alpha["unknown"]) == (False, "CANCELLED", None)
    assert alpha["accounting_wait_s_used"] == 0.0
    assert clock.slept == []


def test_o86_past_the_bound_a_stopped_step_reads_unknown_accounting_lag_never_running_or_stopped(hpc):
    """O86: accounting never catches up: at the bound the step reads unknown with the reason `accounting_lag`, with
    none of the fields that would claim it ran or ended, and the wait it took is recorded. A step Slurm lists and
    accounting says runs, while the state says it was stopped, is no more `running` than the others."""
    for step in (
        stopped_alpha(lag=1e9, before="none"),
        stopped_alpha(lag=1e9, before="running"),
        stopped_alpha(lag=1e9, before="running", listed_for=1e9),
    ):
        clock = Clock()
        alpha = read(hpc, ALPHA_STOPPED, Cluster(clock, step), clock, wait=10.0)["services"]["alpha"]
        assert alpha["unknown"] == "accounting_lag"
        assert (alpha["running"], alpha["state"], alpha["started_at"], alpha["ended_at"], alpha["exit_code"]) == (
            None,
            None,
            None,
            None,
            None,
        )
        assert (alpha["step"], alpha["launch"], alpha["state_differs"]) == ("4242.5", 1, False)
        assert alpha["accounting_wait_s_used"] == 10.0
        assert clock.slept == [2.0] * 5


def test_o86_a_bound_that_is_not_a_whole_number_of_polls_ends_on_the_bound(hpc):
    """O86: the last pause is shortened to what is left of the bound, so the read never waits past it."""
    clock = Clock()
    alpha = read(hpc, ALPHA_STOPPED, Cluster(clock, stopped_alpha(lag=1e9)), clock, wait=5.0)["services"]["alpha"]
    assert clock.slept == [2.0, 2.0, 1.0]
    assert (alpha["unknown"], alpha["accounting_wait_s_used"]) == ("accounting_lag", 5.0)


def test_o86_a_bound_of_zero_never_waits_and_reads_the_lag_as_unknown(hpc):
    """O86: the read is made once; a stopped step it does not settle is `accounting_lag`, not running."""
    clock = Clock()
    alpha = read(hpc, ALPHA_STOPPED, Cluster(clock, stopped_alpha(lag=5.0)), clock, wait=0.0)["services"]["alpha"]
    assert (alpha["unknown"], alpha["running"], alpha["accounting_wait_s_used"]) == ("accounting_lag", None, 0.0)
    assert clock.slept == []


@pytest.mark.parametrize(
    "record",
    [
        ("op", 2, "4242.5", [STOP], "running"),
        ("op", 1, "4242.5", [], "running"),
        ("op", 1, "4242.5", [NOT_CONFIRMED], "stopping"),
        ("op", 1, "4242.5", [NOT_CONFIRMED], "stopped"),
        ("op", 1, "4242.5", [], "stopped"),
        ("op", 1, "4242.5", [STOP], "starting"),
    ],
    ids=[
        "restarted since its stop",
        "never stopped",
        "a stop not confirmed",
        "a last stop not confirmed",
        "stopped with no stop recorded",
        "starting again",
    ],
)
def test_o86_a_step_the_state_does_not_say_was_stopped_is_never_waited_for(hpc, record):
    """O86: a non-empty `stops` outlives a restart, and a stop that was not confirmed is not a stop: with the state
    saying the service was not stopped, a read accounting does not settle is the unknown it was, at once, with no
    pause, never `accounting_lag`."""
    clock = Clock()
    state = state_of(alpha=record)
    step = Step("4242.5", f"alloc-alpha-run-{record[1]}", stopped=True, lag=1e9, before="none")
    alpha = read(hpc, state, Cluster(clock, step), clock)["services"]["alpha"]
    assert alpha["running"] is None
    assert alpha["unknown"]
    assert alpha["unknown"] != "accounting_lag"
    assert alpha["accounting_wait_s_used"] == 0.0
    assert clock.slept == []


def test_o86_a_step_the_state_names_with_another_launch_is_not_waited_for(hpc):
    """O86: the state's step name belongs to another launch than its counter says (a record the driver did not leave
    whole): nothing says this step was the one stopped, so there is no wait."""
    clock = Clock()
    state = state_of(alpha=("op", 2, "4242.5", [STOP], "stopped"))
    state["services"]["alpha"]["step"]["name"] = "alloc-alpha-run-1"
    step = Step("4242.5", "alloc-alpha-run-1", stopped=True, lag=1e9, before="none")
    alpha = read(hpc, state, Cluster(clock, step), clock)["services"]["alpha"]
    assert alpha["unknown"]
    assert alpha["unknown"] != "accounting_lag"
    assert clock.slept == []


def test_o86_a_service_restarted_outside_the_driver_is_not_waited_for(hpc):
    """O86: the state says the step was stopped and squeue lists the service's name under a new id: that is a step
    the driver did not start, read as it is, and nothing of accounting's lag."""
    clock = Clock()
    running = Step("4242.9", "alloc-alpha-run-1")
    alpha = read(hpc, ALPHA_STOPPED, Cluster(clock, running), clock)["services"]["alpha"]
    assert (alpha["step"], alpha["running"], alpha["state_differs"]) == ("4242.9", True, True)
    assert alpha["accounting_wait_s_used"] == 0.0
    assert clock.slept == []


@pytest.mark.parametrize("failing", ["squeue_rc", "sacct_rc"])
def test_o86_a_command_that_fails_is_not_the_lag_of_accounting_and_is_not_waited_for(hpc, failing):
    """O86: a Slurm command that fails keeps its own reason, at once: only an answer that came and does not yet agree
    is waited for."""
    clock = Clock()
    cluster = Cluster(clock, stopped_alpha(lag=5.0), **{failing: 1})
    alpha = read(hpc, ALPHA_STOPPED, cluster, clock)["services"]["alpha"]
    assert alpha["running"] is None
    assert alpha["unknown"] != "accounting_lag"
    assert ("squeue" if failing == "squeue_rc" else "sacct") in alpha["unknown"]
    assert alpha["accounting_wait_s_used"] == 0.0
    assert clock.slept == []


def test_o86_a_command_that_starts_failing_during_the_wait_ends_it_with_its_reason(hpc):
    """O86: accounting behind, then `sacct` failing: the wait stops there with that failure, not at the bound."""
    clock = Clock()
    cluster = Cluster(clock, stopped_alpha(lag=1e9))
    sleep = clock.sleep

    def failing_after_the_first_pause(seconds):
        sleep(seconds)
        cluster.sacct_rc = 1

    clock.sleep = failing_after_the_first_pause
    alpha = read(hpc, ALPHA_STOPPED, cluster, clock)["services"]["alpha"]
    assert "sacct" in alpha["unknown"]
    assert alpha["unknown"] != "accounting_lag"
    assert alpha["accounting_wait_s_used"] == 2.0


def test_o86_services_share_one_bound_and_each_records_what_it_waited(hpc):
    """O86: the zone never settles, a provider settles after 5 s, another is running: one bound for the whole read.
    Each service has its own wait recorded, the unsettled one reads `accounting_lag`, and the running one is read
    without a pause."""
    clock = Clock()
    state = state_of(
        zone=("oz", 1, "4242.3", [STOP], "stopped"),
        alpha=("op", 1, "4242.5", [STOP], "stopped"),
        beta=("op", 1, "4242.6", [], "running"),
    )
    cluster = Cluster(
        clock,
        Step("4242.3", "alloc-zone-run-1", stopped=True, lag=1e9, before="none"),
        stopped_alpha(lag=5.0),
        Step("4242.6", "alloc-beta-run-1"),
    )
    doc = read(hpc, state, cluster, clock, wait=20.0)["services"]
    assert (doc["zone"]["unknown"], doc["zone"]["accounting_wait_s_used"]) == ("accounting_lag", 20.0)
    assert (doc["alpha"]["running"], doc["alpha"]["accounting_wait_s_used"]) == (False, 6.0)
    assert (doc["beta"]["running"], doc["beta"]["accounting_wait_s_used"]) == (True, 0.0)
    assert sum(clock.slept) == 20.0


def test_o86_the_wait_runs_only_the_two_reads(hpc):
    """O86: while it waits the read still runs nothing but `squeue` and `sacct`."""
    clock = Clock()
    cluster = Cluster(clock, stopped_alpha(lag=1e9))
    read(hpc, ALPHA_STOPPED, cluster, clock, wait=10.0)
    assert len(cluster.calls) > 6
    assert all(call[0] in ("squeue", "sacct") for call in cluster.calls)


@pytest.mark.parametrize("wait", [-1.0, 300.5, math.nan, math.inf, True, "60", None])
def test_o86_a_bound_outside_zero_to_three_hundred_is_refused(hpc, wait):
    """O86: the bound is a number of seconds from 0 to 300; anything else is no bound."""
    clock = Clock()
    with pytest.raises(ValueError, match="accounting_wait_s"):
        read(hpc, ALPHA_STOPPED, Cluster(clock, stopped_alpha()), clock, wait=wait)


@pytest.mark.parametrize(
    ("step", "behind"),
    [
        (Step("4242.5", "n", stopped=True, lag=9, before="none"), True),
        (Step("4242.5", "n", stopped=True, lag=9, before="running"), True),
        (Step("4242.5", "n", stopped=True, lag=9, before="completing"), True),
        (Step("4242.5", "n", stopped=True, lag=9, before="unended"), True),
        (Step("4242.5", "n", stopped=True, lag=0, listed_for=9), True),
        (Step("4242.5", "n"), True),
        (Step("4242.5", "n", stopped=True, lag=0), False),
    ],
    ids=["no row", "runs", "not ended", "no end", "ended but listed", "listed and runs", "ended"],
)
def test_o86_accounting_behind_names_only_the_answers_a_stopped_step_can_give_while_accounting_is_behind(
    hpc, step, behind
):
    """O86: `accounting_behind(answer)` is true for the answers that a lagging accounting explains (no row, a row that
    runs, one not ended, squeue listing an ended step) and for a step that runs, and false for an ended one."""
    clock = Clock()
    slurm = hpc.slurm.Slurm(Cluster(clock, step), command_timeout_s=5)
    assert hpc.slurm.accounting_behind(slurm.step_observed("4242.5", "n")) is behind


@pytest.mark.parametrize(
    "case",
    ["squeue", "sacct", "name", "twice", "id", "unreadable"],
)
def test_o86_accounting_behind_is_false_for_a_failure_an_ambiguity_and_a_row_that_cannot_be_read(hpc, case):
    """O86: a failed command, a step listed under another name, a row twice, an id that is not a step id and a row
    that is not one are no lag: they keep their own reasons."""
    clock = Clock()
    step = Step("4242.5", "n", stopped=True, lag=9, before="none")
    cluster = Cluster(clock, step, squeue_rc=int(case == "squeue"), sacct_rc=int(case == "sacct"))
    asked = ("4242.5", "n")
    if case == "name":
        cluster = Cluster(clock, Step("4242.5", "another"))
    elif case == "twice":
        cluster = Cluster(clock, Step("4242.5", "n"), Step("4242.5", "n"))
    elif case == "id":
        asked = ("4242.5; rm", "n")
    elif case == "unreadable":
        cluster.run = garbage
    seen = hpc.slurm.Slurm(cluster, command_timeout_s=5).step_observed(*asked)
    assert seen["running"] is None
    assert hpc.slurm.accounting_behind(seen) is False


def garbage(argv, timeout):
    from hpcslurm import Completed

    return Completed(0, "garbage\n", "")


class Sink:
    text = ""

    def write(self, value):
        self.text += value

    def flush(self):
        pass


def test_o86_the_command_takes_the_bound_prints_the_wait_and_changes_nothing(hpc, tmp_path):
    """O86: `hpcobserve.py --root R --accounting-wait-s 10` prints one JSON line with each service's wait, leaves the
    state file as it was, and with no flag waits 60 s by default."""
    root = tmp_path / "root"
    root.mkdir()
    file = root / "hpc-federation.json"
    file.write_text(json.dumps(ALPHA_STOPPED), encoding="utf-8")
    before = (file.read_bytes(), file.stat().st_mtime_ns, sorted(p.name for p in root.iterdir()))
    for arguments, total in (([], 60.0), (["--accounting-wait-s", "10"], 10.0), (["--accounting-wait-s", "0"], 0.0)):
        clock = Clock()
        cluster = Cluster(clock, stopped_alpha(lag=1e9))
        out = Sink()
        code = hpc.observe.main(
            ["--root", str(root), *arguments],
            runner=cluster, out=out, err=Sink(), sleep=clock.sleep, clock=clock.monotonic,
        )  # fmt: skip
        assert code == 0
        (line,) = out.text.splitlines()
        alpha = json.loads(line)["services"]["alpha"]
        assert (alpha["unknown"], alpha["accounting_wait_s_used"]) == ("accounting_lag", total)
        assert sum(clock.slept) == total
    assert (file.read_bytes(), file.stat().st_mtime_ns, sorted(p.name for p in root.iterdir())) == before


@pytest.mark.parametrize("value", ["-1", "301", "nan", "inf", "soon", ""])
def test_o86_the_command_refuses_a_bound_that_is_not_zero_to_three_hundred(hpc, tmp_path, capsys, value):
    """O86: a bad `--accounting-wait-s` is a usage error (exit 2) that names the option, before anything is read."""
    cluster = Cluster(Clock())
    with pytest.raises(SystemExit) as stopped:
        hpc.observe.main(["--root", str(tmp_path), "--accounting-wait-s", value], runner=cluster)
    assert stopped.value.code == 2
    assert "accounting-wait-s" in capsys.readouterr().err
    assert cluster.calls == []


def test_o86_the_commands_defaults_are_the_watchs(hpc):
    """O86: the package cannot import the deploy script, so the bound's default, its ceiling and the word for the lag
    are written in both places; they must agree."""
    assert hpc.observe.DEFAULT_ACCOUNTING_WAIT_S == fedwatch.DEFAULT_ACCOUNTING_WAIT_S == 60.0
    assert hpc.observe.MAX_ACCOUNTING_WAIT_S == fedwatch.MAX_ACCOUNTING_WAIT_S == 300.0
    assert hpc.observe.LAG == fedwatch.ACCOUNTING_LAG == "accounting_lag"
    assert hpc.observe.POLL_S == 2.0


def test_o86_what_the_wait_adds_names_no_path_or_node(hpc):
    """O86: a service that reads `accounting_lag` names no node, address or path."""
    clock = Clock()
    doc = read(hpc, ALPHA_STOPPED, Cluster(clock, stopped_alpha(lag=1e9)), clock, wait=4.0)
    text = json.dumps(doc["services"])
    assert NODE not in text
    assert "/" not in text
