"""O97: the code tool's side of a restored mount. A cell whose deadline freed the agent's survivor returns the cell's
own timeout after the tool restored the mount inside the agent's paused clock; a restore that fails leaves that call
its timeout and the run's end to the next call; and every call whose deadline fired with a survivor carries it."""

import asyncio
import contextlib

import pytest

from expcore.errors import InfraError
from expcore.records.cell_deadline import problem
from expcore_onedata.sandbox.api import ExecResult
from expcore_onedata.sandbox.codetool import CodeExecutionProvider

TIME_LIMIT = {"reason": "time_limit", "category": "agent", "basis": "the cell ran to its limit"}
CAP = {"reason": "kill_survived", "category": "infrastructure", "basis": "x", "deadline": {"outcome": "mount_aborted"}}
ATTRIBUTION = {
    "verdict": "agent",
    "why": "blocked_on_held_down_holder",
    "target": "providers[1]",
    "space_holder": "providers[1]",
}


def deadline_record(**fields) -> dict:
    return {
        "utc": "2026-01-01T00:00:00.000+00:00", "grace_s": 5.0, "margin_s": 30.0, "outcome": "mount_aborted",
        "restore": "pending", "attribution": dict(ATTRIBUTION), "cell": 1,
        "steps": [{"step": "kill", "t_s": 0.1, "signalled": 1}, {"step": "abort", "t_s": 5.0, "ok": True}], **fields,
    }  # fmt: skip


class Context:
    """A call context that records each wait on infrastructure it was asked for, and whether it was open when asked."""

    def __init__(self) -> None:
        self.cell_bound_s = 30.0
        self.waits: list[str] = []
        self.open = 0

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind: str):
        self.waits.append(kind)
        self.open += 1
        try:
            yield
        finally:
            self.open -= 1


class Sandbox:
    """A sandbox whose one cell hit its deadline with a restore pending; `restore_mount` finishes the record as the
    runner would, and says how many waits were open when it ran."""

    def __init__(self, ctx: Context, record: dict, answer: dict | None = None) -> None:
        self.ctx, self.record, self.answer = ctx, record, answer or {"ok": True, "generation": 2}
        self.restored_in_wait: list[int] = []
        self.calls = 0

    def exec(self, code: str, timeout_s: float) -> ExecResult:
        return ExecResult("partial", "", -9, 31.0, timed_out=True, kill=dict(TIME_LIMIT), restore_pending=True,
                          deadline=self.record)  # fmt: skip

    def restore_mount(self) -> dict:
        self.calls += 1
        self.restored_in_wait.append(self.ctx.open)
        if self.answer.get("ok") is True:
            self.record.update(outcome="mount_restored", restore="restored")
            self.record["steps"].append({"step": "restored", "s": 1.0, "generation": 2})
        else:
            self.record.update(restore="failed")
            self.record["steps"].append({"step": "restore_failed", "s": 1.0, "why": "no"})
        return self.answer


def run(sandbox, ctx):
    return asyncio.run(CodeExecutionProvider(sandbox).execute("python", {"code": "x"}, timeout_s=30.0, ctx=ctx))


def test_o97_the_restore_runs_inside_the_agents_paused_clock_before_the_call_returns():
    """O97: the result says a restore is pending: the tool calls `restore_mount` once, inside one wait of kind
    `sandbox_lifecycle`, and returns the cell's own timeout, which names the study's limit."""
    ctx = Context()
    sandbox = Sandbox(ctx, deadline_record())
    out = run(sandbox, ctx)
    assert sandbox.calls == 1
    assert sandbox.restored_in_wait == [1]
    assert ctx.waits == ["sandbox_lifecycle"]
    assert (out.is_error, out.error_class) == (True, "timeout")
    assert "the code ran past its 30 s limit and was killed" in out.text


def test_o97_the_call_carries_the_deadline_as_it_stands_after_the_restore():
    """O97: the call's `cell_deadline` is the record after the restore: `mount_restored`, with the restore's step, the
    verdict and the survivors the backend read; and it is a shape the record's schema accepts."""
    ctx = Context()
    out = run(Sandbox(ctx, deadline_record(survivors=[{"comm": "python3", "state": "D"}])), ctx)
    held = out.cell_deadline
    assert held["outcome"] == "mount_restored"
    assert held["restore"] == "restored"
    assert [s["step"] for s in held["steps"]] == ["kill", "abort", "restored"]
    assert held["attribution"]["verdict"] == "agent"
    assert held["survivors"] == [{"comm": "python3", "state": "D"}]
    assert "cell" not in held  # the runner's own bookkeeping is not part of the call's field
    assert problem(held) is None


def test_o97_a_restore_that_fails_leaves_this_call_its_timeout_and_never_raises_here():
    """O97: the failed restore ends the run (the next call raises `sandbox`, `cells`); this call returns its own result,
    with the deadline saying `failed`."""
    ctx = Context()
    out = run(Sandbox(ctx, deadline_record(), {"ok": False, "why": "no"}), ctx)
    assert (out.is_error, out.error_class) == (True, "timeout")
    assert out.cell_deadline["restore"] == "failed"
    assert out.cell_deadline["outcome"] == "mount_aborted"


def test_o97_a_sandbox_that_reports_a_pending_restore_and_has_none_fails_as_sandbox():
    """O97: a pending restore with no `restore_mount` is inconsistent: the call fails as kind `sandbox`, never returns a
    result that leaves a dead mount for the next cell."""

    class NoRestore:
        def exec(self, code, timeout_s):
            return ExecResult("", "", -9, 31.0, timed_out=True, kill=dict(TIME_LIMIT), restore_pending=True)

    with pytest.raises(InfraError) as raised:
        run(NoRestore(), Context())
    assert raised.value.kind == "sandbox"


def test_o97_a_cell_with_no_pending_restore_never_asks_for_one_and_a_clean_kill_carries_no_deadline():
    """O97: a cell killed cleanly (no survivor, so no record) is the agent's timeout as before: no restore, no wait, no
    field."""

    class Clean:
        calls = 0

        def exec(self, code, timeout_s):
            return ExecResult("partial", "", -9, 31.0, timed_out=True, kill=dict(TIME_LIMIT))

        def restore_mount(self):
            Clean.calls += 1

    ctx = Context()
    out = run(Clean(), ctx)
    assert (Clean.calls, ctx.waits, out.cell_deadline) == (0, [], None)


def test_o97_an_escalated_deadline_carries_its_record_too_and_still_restores_nothing():
    """O97: a deadline that ended the run (the infrastructure path: no restore) is on the call as well, with its
    outcome and verdict, and no wait is opened; the call keeps the cell's own result (O72)."""
    record = deadline_record(attribution={"verdict": "infrastructure", "why": "space_not_held_down", "target": None,
                                          "space_holder": None})  # fmt: skip
    del record["restore"]

    class Ended:
        calls = 0

        def exec(self, code, timeout_s):
            return ExecResult("partial", "", -9, 31.0, timed_out=True, kill=dict(CAP), deadline=record)

        def restore_mount(self):
            Ended.calls += 1

    ctx = Context()
    out = run(Ended(), ctx)
    assert (Ended.calls, ctx.waits) == (0, [])
    assert out.cell_deadline["outcome"] == "mount_aborted"
    assert out.cell_deadline["attribution"]["verdict"] == "infrastructure"
    assert (out.is_error, out.error_class) == (True, "timeout")


def test_o97_a_record_the_schema_would_refuse_is_cut_down_and_never_put_on_the_call():
    """O97: a call never carries what its record would refuse: a step with a value that is not plain JSON is cut to the
    names of the steps, and a record with no time of its cap (never decided) has no field."""
    ctx = Context()
    record = deadline_record(steps=[{"step": "abort", "ok": object()}, {"step": "ended"}])
    out = run(Sandbox(ctx, record), ctx)
    assert [s["step"] for s in out.cell_deadline["steps"]] == ["abort", "ended", "restored"]
    assert problem(out.cell_deadline) is None
    undecided = {"outcome": "survived_kill", "grace_s": 5.0, "margin_s": 30.0, "steps": [], "why": "x"}
    assert run(Sandbox(Context(), undecided), Context()).cell_deadline is None
