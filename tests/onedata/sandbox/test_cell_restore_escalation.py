"""O97: a cell's deadline whose abort freed the survivors may leave the run standing, when the backend can restore its
mount and the survivors were blocked on what the study's own manipulation holds down (the attribution). Only that
path sets `restore: "pending"`: the abort worked, every process was shown gone, the escalation is restorable and its
verdict is `agent`. Every other freed survivor is `mount_aborted` and ends the run, exactly as before (O34), and a
record the rules do not know ends it too."""

import dataclasses

import pytest
from deadline_support import Clock, Processes, escalation, steps

from expcore_onedata.sandbox import deadline

FREED = {"ok": True, "method": "fusectl", "waiting_before": 1, "waiting_after": 0}
AGENT = {
    "verdict": "agent",
    "why": "blocked_on_held_down_holder",
    "target": "providers[1]",
    "space_holder": "providers[1]",
}
INFRA = {"verdict": "infrastructure", "why": "space_not_held_down", "target": None, "space_holder": "providers[2]"}


class Diagnosing(Processes):
    """A tracker that can say what its wedged process waits on, and keeps what it was asked and given."""

    def __init__(self, *args, found=None, raises: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.found, self.raises, self.asked = found, raises, []

    def diagnose(self, left: list[dict], timeout_s: float):
        self.asked.append(([p["pid"] for p in left], timeout_s))
        if self.raises:
            raise OSError("/proc could not be read")
        return self.found


def freed_by_the_abort(clock: Clock, procs: Processes, **fields):
    """The escalation over `procs`, with an abort that frees the wedged process, and `fields` of the Escalation."""

    def abort(timeout_s: float) -> dict:
        procs.release()
        return dict(FREED)

    esc, calls = escalation(clock, abort=abort)
    return dataclasses.replace(esc, **fields), calls


def attribute_as(verdict: dict):
    seen = []

    def attribute(diagnostics):
        seen.append(diagnostics)
        return verdict

    attribute.seen = seen
    return attribute


# ------------------------------------------------------------------------------------------------ the one pending path
def test_o97_a_restorable_abort_that_freed_the_agents_survivor_leaves_a_restore_pending_and_does_not_end_the_run():
    """O97: restorable, every survivor freed by the abort, verdict `agent`: `mount_aborted` with `restore: "pending"`.
    It is still an escalation (`mount_aborted` stays in `ESCALATED`), but `ends_run` says it ends nothing, and no stop
    is ordered."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, wedged=(12,), found={"survivors": [{"comm": "reader"}], "survivors_cut": 0})
    esc, _ = freed_by_the_abort(clock, procs, restorable=True, attribute=attribute_as(AGENT))
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert record["restore"] == deadline.RESTORE_PENDING
    assert deadline.MOUNT_ABORTED in deadline.ESCALATED
    assert not deadline.ends_run(record)
    assert steps(record) == ["kill", "survived", "abort", "ended"]  # no stop step: nothing is stopped


def test_o97_the_attribution_and_the_diagnostics_are_read_before_the_abort_and_recorded():
    """O97: the survivors are diagnosed at the `survived` step, before the mount is aborted (the abort ends their wait),
    each given only the margin left; the diagnostic keys and the attribution go into the record, nothing else the
    tracker answered does."""
    clock = Clock()
    found = {"survivors": [{"comm": "reader"}], "survivors_cut": 0, "connection": {"fusectl": "writable"}, "extra": 1}
    procs = Diagnosing({12: "reader"}, wedged=(12,), found=found)
    attribute = attribute_as(AGENT)
    esc, calls = freed_by_the_abort(clock, procs, restorable=True, attribute=attribute)
    record = deadline.enforce(procs, esc)
    assert procs.asked == [([12], pytest.approx(esc.margin_s - 3.0, abs=0.5))]
    assert attribute.seen == [found]  # the attribution sees the whole reading
    assert record["survivors"] == found["survivors"]
    assert record["survivors_cut"] == 0
    assert record["connection"] == {"fusectl": "writable"}
    assert "extra" not in record
    assert record["attribution"] == AGENT
    assert steps(record).index("survived") < steps(record).index("abort")
    assert calls == [("abort", pytest.approx(esc.margin_s - 3.0, abs=0.5))]


# ------------------------------------------------------------------------------------ every other path stays as it was
def test_o97_an_infrastructure_verdict_stays_mount_aborted_and_ends_the_run():
    """O97: a restorable abort whose survivors are not the agent's (a link wedge, no injection) is `mount_aborted` with
    no restore, so it ends the run as `cell_unkillable`, as before; the verdict is on the record."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, wedged=(12,), found={"survivors": []})
    esc, _ = freed_by_the_abort(clock, procs, restorable=True, attribute=attribute_as(INFRA))
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert "restore" not in record
    assert deadline.ends_run(record)
    assert record["attribution"] == INFRA


def test_o97_an_escalation_that_is_not_restorable_never_leaves_a_restore_pending_whatever_the_verdict():
    """O97: Docker's and every backend's abort with no restore: the verdict is recorded, the outcome is `mount_aborted`,
    and the run ends."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, wedged=(12,), found={"survivors": []})
    esc, _ = freed_by_the_abort(clock, procs, restorable=False, attribute=attribute_as(AGENT))
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert "restore" not in record
    assert deadline.ends_run(record)


def test_o97_a_restorable_escalation_with_no_attribution_fails_closed_and_adds_nothing_to_the_record():
    """O97: restorable but no `attribute`: no verdict is asked or recorded, and the freed survivor ends the run."""
    clock = Clock()
    procs = Processes({12: "reader"}, wedged=(12,))
    esc, _ = freed_by_the_abort(clock, procs, restorable=True)
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert "restore" not in record
    assert "attribution" not in record
    assert deadline.ends_run(record)


def test_o97_an_escalation_with_neither_hook_leaves_the_record_exactly_as_before():
    """O97: no `diagnose`, no `attribute`, not restorable: the record has the keys it always had and no others."""
    clock = Clock()
    procs = Processes({12: "reader"}, wedged=(12,))
    esc, _ = freed_by_the_abort(clock, procs)
    record = deadline.enforce(procs, esc)
    assert sorted(record) == ["grace_s", "margin_s", "outcome", "steps", "utc"]
    assert steps(record) == ["kill", "survived", "abort", "ended"]


@pytest.mark.parametrize(
    "answer",
    [None, "agent", {}, {"verdict": "maybe"}, {"verdict": None}, {"why": "x"}, ["agent"]],
    ids=["none", "a bare string", "empty", "unknown verdict", "null verdict", "no verdict", "a list"],
)
def test_o97_an_attribution_that_is_no_verdict_is_infrastructure(answer):
    """O97: an answer that does not name a verdict fails closed to `infrastructure` with the reason
    `blocking_object_unknown`, and no restore is pending."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, wedged=(12,), found={"survivors": []})
    esc, _ = freed_by_the_abort(clock, procs, restorable=True, attribute=lambda diagnostics: answer)
    record = deadline.enforce(procs, esc)
    assert record["attribution"] == {"verdict": "infrastructure", "why": deadline.UNKNOWN_BLOCK}
    assert "restore" not in record
    assert deadline.ends_run(record)


def test_o97_an_attribution_that_raises_is_infrastructure_and_the_abort_still_runs():
    """O97: a raise in the attribution is a `raised` step, never an exception that skips the abort (the abort is what
    frees the cell): the cell is freed, `mount_aborted`, no restore, the run ends."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, wedged=(12,), found={"survivors": []})

    def attribute(diagnostics):
        raise KeyError("fd")

    esc, calls = freed_by_the_abort(clock, procs, restorable=True, attribute=attribute)
    record = deadline.enforce(procs, esc)
    assert [c[0] for c in calls] == ["abort"]
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert "restore" not in record
    assert record["attribution"]["verdict"] == "infrastructure"
    raised = [s for s in record["steps"] if s["step"] == "raised"]
    assert [(s["during"], s["error"]) for s in raised] == [("attribution", "the attribution raised KeyError")]


def test_o97_a_diagnosis_that_raises_is_recorded_and_attributed_to_nobody():
    """O97: the tracker's `diagnose` raising is a `raised` step; the attribution then gets no reading (None) and is
    asked for its verdict all the same, which fails closed; the abort still runs and frees the cell."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, wedged=(12,), raises=True)
    attribute = attribute_as(INFRA)
    esc, calls = freed_by_the_abort(clock, procs, restorable=True, attribute=attribute)
    record = deadline.enforce(procs, esc)
    assert attribute.seen == [None]
    assert [c[0] for c in calls] == ["abort"]
    raised = [s for s in record["steps"] if s["step"] == "raised"]
    assert [(s["during"], s["error"]) for s in raised] == [("diagnosis", "the diagnosis raised OSError")]
    assert "survivors" not in record
    assert "restore" not in record


def test_o97_survivors_that_could_not_be_listed_are_never_attributed_to_the_agent():
    """O97: a listing that fails at the `survived` step (unknown is never gone) is `infrastructure`, and the
    attribution is not asked, even by an escalation whose attribute would say `agent`. A later clean listing after the
    abort does not change it."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, wedged=(12,), found={"survivors": []})
    asked = attribute_as(AGENT)
    listings = {"n": 0}
    base = procs.processes

    def flaky(timeout_s: float):
        listings["n"] += 1
        return None if procs.wedged and listings["n"] >= 3 else base(timeout_s)

    procs.processes = flaky
    esc, _ = freed_by_the_abort(clock, procs, restorable=True, attribute=asked)
    record = deadline.enforce(procs, esc)
    assert record["attribution"] == {"verdict": "infrastructure", "why": deadline.UNKNOWN_BLOCK}
    assert asked.seen == []
    assert "restore" not in record


def test_o97_an_abort_that_freed_nothing_is_sandbox_stopped_and_never_restorable_even_for_the_agent():
    """O97: the verdict is the agent's but the abort did not free the process: `sandbox_stopped` and the stop, as in
    O34. A restore is for a mount whose abort worked."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, wedged=(12,), found={"survivors": []})
    esc, _ = escalation(clock, abort=lambda _: dict(FREED))  # the abort says ok and frees nothing
    esc = dataclasses.replace(esc, restorable=True, attribute=attribute_as(AGENT))
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.SANDBOX_STOPPED
    assert "restore" not in record
    assert deadline.ends_run(record)
    assert steps(record)[-1] == "stop"


def test_o97_a_raise_after_the_abort_is_mount_aborted_with_no_restore():
    """O97: a kill that raises while waiting for the freed process makes the fallback decide; the mount was aborted, so
    the outcome is at least `mount_aborted` and, as the clean path alone sets a restore, it is not pending."""
    late = {"pid": 21, "comm": "late", "state": "R", "start": "21"}  # a process that appears once the mount is aborted

    class KillRaisesForTheLateOne(Diagnosing):
        def kill(self, procs, timeout_s):
            if any(p["pid"] == late["pid"] for p in procs):
                raise OSError("the host's kill could not run")
            super().kill(procs, timeout_s)

    clock = Clock()
    procs = KillRaisesForTheLateOne({12: "reader"}, wedged=(12,), found={"survivors": []})

    def abort(timeout_s: float) -> dict:
        procs.release()
        procs.procs[late["pid"]] = dict(late)
        return dict(FREED)

    esc, _ = escalation(clock, abort=abort)
    esc = dataclasses.replace(esc, restorable=True, attribute=attribute_as(AGENT))
    record = deadline.enforce(procs, esc, fallback=lambda: [])
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert "restore" not in record
    assert deadline.ends_run(record)
    assert "raised" in steps(record)


def test_o97_the_clean_kill_is_untouched_and_never_diagnosed():
    """O97: a cell whose processes all end at the kill is `killed`: no diagnosis, no attribution, no restore."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, found={"survivors": []})  # not wedged: the kill takes
    esc, _ = escalation(clock)
    esc = dataclasses.replace(esc, restorable=True, attribute=attribute_as(AGENT))
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.KILLED
    assert procs.asked == []
    assert not deadline.ends_run(record)
    assert sorted(record) == ["grace_s", "margin_s", "outcome", "steps", "utc"]


# --------------------------------------------------------------------------------------------------------- ends_run
RESTORE_STATES = [None, *deadline.RESTORE_STATES, "unknown_state"]


@pytest.mark.parametrize("restore", RESTORE_STATES)
@pytest.mark.parametrize("outcome", [*deadline.RECORDED, "from_a_newer_writer", None])
def test_o97_ends_run_holds_for_every_outcome_and_restore_state_and_fails_closed(outcome, restore):
    """O97: a run is not ended by a clean kill, by `mount_restored`, or by a `mount_aborted` whose restore is pending,
    under way or not needed; every other record ends it: the escalations, a failed restore, a `mount_aborted` with no
    restore, and any outcome or state it does not know."""
    record = {"outcome": outcome, "restore": restore}
    standing = outcome in (deadline.KILLED, deadline.MOUNT_RESTORED) or (
        outcome == deadline.MOUNT_ABORTED
        and restore in (deadline.RESTORE_PENDING, deadline.RESTORE_RUNNING, "not_needed")
    )
    assert deadline.ends_run(record) is (not standing)


def test_o97_mount_restored_is_not_an_outcome_the_escalation_decides_and_is_not_escalated():
    """O97: `enforce` never returns `mount_restored`, so it is not in `OUTCOMES` or `SEVERITY` (O34's order is the
    escalation's); it is a recorded outcome, and it is not in `ESCALATED`, so it is not infrastructure."""
    assert deadline.MOUNT_RESTORED not in deadline.OUTCOMES
    assert deadline.MOUNT_RESTORED not in deadline.SEVERITY
    assert deadline.MOUNT_RESTORED not in deadline.ESCALATED
    assert deadline.MOUNT_RESTORED in deadline.RECORDED
    assert deadline.MOUNT_ABORTED in deadline.ESCALATED
