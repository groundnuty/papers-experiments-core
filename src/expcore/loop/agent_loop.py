# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/loop.py, blob 7e8f2022a0c02e4c99747bd593e61d8b5ad32f6a
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:933-936
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:611-617, 905-919
#   changes: see git history of this file from its import commit
"""The agent loop: model call -> tool calls -> capped results -> next call, until the model answers without a tool
call, a terminal tool succeeds, or a cap or a failure ends the attempt.

One model call per round and every tool call of a round executed in order, with the results appended together; the
tool box and the model backend come from their own modules. Added to that base: a closed outcome vocabulary, the
injected clock and sleep, the time charged to the agent only (L04: waits on infrastructure are measured live and
the agent's clock pauses in them; a call is cut when the agent's time or the ceiling is used up), calls answered as skipped after a call that ends
the attempt (L09), and a crash in harness, backend or tool code recorded as a harness defect (L08): only the errors the
model layer raises on purpose (infrastructure, an abandoned request, a context overflow, a provider error status) map
to their own endings.

The loop never raises for an ending. Infrastructure failures, defects and caps all come back as a `LoopResult` that
holds everything the attempt did so far, so an unscored ending still leaves a partial record (R09).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.loop.config import AgentConfig, config_sha256_of
from expcore.loop.endings import cannot_start, cut_ending, time_ending
from expcore.loop.leaks import leaked_tool
from expcore.loop.outcomes import Outcome, harness_defect_of
from expcore.loop.records import LoopResult, ToolCallRecord, TurnRecord
from expcore.loop.timer import AgentTimer, Tick, WaitContext, waited_since
from expcore.loop.toolbox import ToolBox, ToolRun
from expcore.loop.watch import Cut, Watcher
from expcore.models.adapters import ModelBackend, ModelTurn, ToolCall
from expcore.models.retry import classify

MALFORMED_RAW_LIMIT = 2000  # characters of the model's own arguments kept in a malformed-call event


class _End(Exception):
    """Internal: the attempt ends here with this outcome."""

    def __init__(self, outcome: Outcome):
        super().__init__(outcome.name)
        self.outcome = outcome


async def run_agent(
    backend: ModelBackend,
    toolbox: ToolBox,
    system: str,
    user: str,
    config: AgentConfig,
    *,
    timer: AgentTimer | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> LoopResult:
    """Run one attempt and return how it went.

    `clock` and `sleep` are injected so a test or a replay controls time; the default is monotonic time. A caller
    that builds the backend with `time_left` passes the same `timer` to both (`config.timer(clock)`), so the
    backend's retry policy and the loop read one account of the agent's time.

    Cancelling the run from outside cancels the call in flight, waits for it up to `config.cleanup_deadline_s`, and
    raises `CancelledError`. No result is returned then; a caller that passed its own `timer` finds there how long the
    cleanup took (`cleanup_s`) and whether it was given up on (`cleanup_timed_out`).
    """
    if toolbox.cap_chars != config.limits.output_cap_chars:
        raise ValueError(
            f"the tool box caps output at {toolbox.cap_chars} characters but the run configuration says "
            f"{config.limits.output_cap_chars}: the configuration hash must describe what the model saw"
        )
    if timer is None:
        timer = config.timer(clock)
    elif timer.time_cap_s != config.limits.time_cap_s or timer.ceiling_s != config.ceiling_s:
        raise ValueError("the timer's caps differ from the run configuration")
    elif timer.started:
        raise ValueError("the timer has already started; the loop starts it")
    return await _Run(backend, toolbox, system, user, config, timer, sleep).execute()


class _Run:
    """The state of one attempt. Its methods raise `_End` to finish; `execute` turns that into a result."""

    def __init__(
        self,
        backend: ModelBackend,
        toolbox: ToolBox,
        system: str,
        user: str,
        config: AgentConfig,
        timer: AgentTimer,
        sleep: Callable[[float], Awaitable[None]],
    ):
        self.backend, self.toolbox, self.system, self.user = backend, toolbox, system, user
        self.config, self.timer = config, timer
        self.watcher = Watcher(timer, sleep, cleanup_deadline_s=config.cleanup_deadline_s)
        self.turns: list[TurnRecord] = []
        self.calls: list[ToolCallRecord] = []
        self.rounds = 0
        self.tokens = 0
        self.usage_incomplete = False
        self.malformed_calls = 0
        self.malformed_events: list[dict[str, Any]] = []
        self.final_text: str | None = None
        self.outcome: Outcome | None = None
        self._request_waits_left_open = 0  # waits the last model request left open, for its turn record
        self._request_waited_by_kind: dict[str, float] = {}  # what the last model request waited, by kind

    async def execute(self) -> LoopResult:
        """Run the loop to its ending."""
        try:
            self.timer.start()
            self.backend.begin(self.system, self.user, self.toolbox.definitions())
            await self._loop()
        except _End as end:
            self.outcome = end.outcome
        except Exception as exc:
            self.outcome = self._defect("the loop", exc)
        return self._result()

    # --- the loop -------------------------------------------------------------------------------------------------

    async def _loop(self) -> None:
        """One iteration per model reply; ends by raising `_End`."""
        while True:
            tick = self.timer.check("before_model")
            self._guard(tick)
            self._guard_budget()  # no request is made once the budget is used up
            turn = await self._step(tick)
            self._note(turn)
            self._count_malformed(turn)  # before any cap check: a capped round is still the round the model asked for
            calls = turn.tool_calls
            if (
                not calls and turn.hit_output_limit
            ):  # a reply cut at the output limit is no answer, whatever the budget (B12)
                self.final_text = turn.text
                raise _End(Outcome("output_cap", f"the final reply stopped at the output limit ({turn.stop_reason})"))
            self._guard_budget(turn)  # a reply that reaches the budget executes none of its calls
            if not calls:
                self.final_text = turn.text  # "" is an empty reply; None, left alone, means there was no reply (L11)
                leak = self._leak(turn)
                if leak is not None:
                    raise _End(leak)
                raise _End(Outcome("finished", "the model answered without a tool call"))
            if self.rounds + 1 > self.config.limits.max_tool_rounds:  # rounds are counted, not calls (L03)
                self._skip_all(turn, "cap")
                raise _End(Outcome("turn_cap", f"{self.config.limits.max_tool_rounds} rounds of tool calls used"))
            self.rounds += 1
            if self.config.malformed_policy == "end" and any(c.parse_error for c in calls):
                self._end_on_malformed(turn)
            await self._round(turn)

    def _leak(self, turn: ModelTurn) -> Outcome | None:
        """The ending for a tool-free reply that holds a call to an offered tool as text, in its reply or its
        reasoning; the call is not run and the model is not asked again (L10)."""
        for channel, text in (("text", turn.text), ("reasoning", turn.reasoning)):
            tool = leaked_tool(text, self.toolbox.names)
            if tool is not None:
                return Outcome(
                    "leaked_tool_call", f"the reply's {channel} holds a call to {tool!r} written out; it was not run"
                )
        return None

    def _guard_budget(self, turn: ModelTurn | None = None) -> None:
        """End the attempt as a token cap once the tokens used reach the budget; the budget is exhausted at the budget,
        not past it. A reply that did so has its calls logged as skipped."""
        budget = self.config.limits.token_budget
        if self.tokens >= budget:
            if turn is not None:
                self._skip_all(turn, "cap")
            raise _End(Outcome("token_cap", f"{self.tokens} tokens used, budget {budget}"))

    def _guard(self, tick: Tick) -> None:
        """End the attempt when the agent's time is used up (checked first), or the whole-time ceiling is passed."""
        over = time_ending(tick, self.config, self.timer.infra_wait_s)
        if over is not None:
            raise _End(over)

    # --- model calls ----------------------------------------------------------------------------------------------

    async def _step(self, tick: Tick) -> ModelTurn:
        """Ask the model for its next reply, cut when the agent's time or the ceiling is used up (L04)."""
        by_kind0 = self.timer.infra_wait_by_kind
        try:
            return await self.watcher.run(self.backend.step(), tick)
        except Cut as cut:
            self.usage_incomplete = True  # the request in flight was given up: its usage is unknown
            raise _End(cut_ending("the model call", cut.tick, self.config)) from None
        except RequestAbandoned:
            self.usage_incomplete = True  # accepted, then slow, never resent (B03)
            raise _End(
                Outcome("time_cap", "the request went out and was not answered in time; it was not resent")
            ) from None
        except InfraError as exc:  # the kind goes through unchanged (C02)
            raise _End(Outcome("infrastructure", exc.detail, infra_kind=exc.kind)) from None
        except (
            ContextOverflow
        ) as exc:  # the conversation outgrew the window: the attempt's own ending, not infrastructure
            raise _End(Outcome("error", str(exc), error_type="ContextOverflow")) from None
        except Exception as exc:
            if classify(exc) is None:  # not an error the endpoint answered with: a crash in our own code (L08)
                raise _End(self._defect("the model backend", exc)) from None
            # A provider error the retry layer passes on unchanged (a plain 400, a 404): the attempt's own ending.
            raise _End(Outcome("error", f"{type(exc).__name__}: {exc}", error_type=type(exc).__name__)) from None
        finally:
            self._request_waits_left_open = self.timer.close_open_waits()  # a wait belongs to its request
            self._request_waited_by_kind = waited_since(by_kind0, self.timer.infra_wait_by_kind)

    def _note(self, turn: ModelTurn) -> None:
        """Count the reply's tokens (a missing count adds nothing and marks the totals incomplete, B10) and record it."""
        if turn.input_tokens is None or turn.output_tokens is None:
            self.usage_incomplete = True
        self.tokens += (turn.input_tokens or 0) + (turn.output_tokens or 0)
        self.turns.append(
            TurnRecord(
                index=len(self.turns),
                text=turn.text,
                reasoning=turn.reasoning,
                stop_reason=turn.stop_reason,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                latency_s=turn.latency_s,
                tool_call_ids=[c.id for c in turn.tool_calls],
                served_model=turn.served_model,
                system_fingerprint=turn.system_fingerprint,
                retries=turn.retries,
                waits_left_open=self._request_waits_left_open,
                infra_wait_by_kind=self._request_waited_by_kind,
            )
        )

    # --- tool rounds ----------------------------------------------------------------------------------------------

    def _count_malformed(self, turn: ModelTurn) -> None:
        """Count the reply's malformed calls against the round it would be (L01)."""
        for call in turn.tool_calls:
            if call.parse_error:
                self.malformed_calls += 1
                raw = (call.raw_arguments or "")[:MALFORMED_RAW_LIMIT]
                self.malformed_events.append(
                    {
                        "round": self.rounds + 1,
                        "call_id": call.id,
                        "name": call.name,
                        "error": call.parse_error,
                        "raw": raw,
                    }
                )

    def _end_on_malformed(self, turn: ModelTurn) -> None:
        """The declared alternative to report-and-continue: end the attempt, with every call of the reply logged."""
        for call in turn.tool_calls:
            run = self.toolbox.malformed(call) if call.parse_error else self.toolbox.skipped(call, "ended")
            self._log(len(self.turns) - 1, call, run, self.rounds, None, None)
        raise _End(Outcome("malformed_end", "the model sent tool-call arguments that were not valid JSON"))

    async def _round(self, turn: ModelTurn) -> None:
        """Run the calls of one reply in order and hand all the results to the backend together.

        A call that ends the attempt (a terminal tool, the time cap, a failure) leaves the calls after it answered
        as skipped, so every call of the reply has a result and the history stays valid (L09).
        """
        index = len(self.turns) - 1
        runs: list[ToolRun] = []
        stop: Outcome | None = None
        for call in turn.tool_calls:
            if stop is not None:
                runs.append(self._skip(index, call, "time" if stop.name == "time_cap" else "ended"))
                continue
            run, stop = await self._call(index, call)
            runs.append(run)
        self.backend.add_tool_results([run.result for run in runs])
        if stop is not None:
            raise _End(stop)

    async def _call(self, index: int, call: ToolCall) -> tuple[ToolRun, Outcome | None]:
        """Run one call. Returns its run and, when the call ends the attempt, the outcome."""
        if call.parse_error:  # report-and-continue: tell the model, run nothing
            run = self.toolbox.malformed(call)
            self._log(index, call, run, self.rounds, None, None)
            return run, None
        tick = self.timer.check("before_tool")
        late = tick.remaining_s < self.config.min_call_s  # no call starts with under a second left (L05)
        if late or tick.ceiling_passed:
            stop = cannot_start(tick, self.config, too_late=late)
            return self._skip(index, call, "time"), stop
        waited0 = (self.timer.infra_wait_s, self.timer.infra_wait_by_kind)
        try:
            # The provider is told the agent's time left and marks its waits on infrastructure through the context; the
            # loop cuts the call if the agent's time, which leaves those waits out, or the ceiling is used up (L04, L05).
            ctx = WaitContext(self.timer)
            run = await self.watcher.run(self.toolbox.run(call, timeout_s=tick.remaining_s, ctx=ctx), tick)
        except Cut as cut:
            cut_run = self.toolbox.skipped(call, "cut")
            self._log(index, call, cut_run, self.rounds, tick, cut.tick, status="cut", waited=self._waited(waited0))
            return cut_run, cut_ending(f"tool {call.name!r}", cut.tick, self.config)
        except InfraError as exc:
            return self._failed(index, call, tick, f"infrastructure: {exc.kind}", waited0), Outcome(
                "infrastructure", exc.detail, infra_kind=exc.kind
            )
        except Exception as exc:
            return self._failed(index, call, tick, f"{type(exc).__name__}: {exc}", waited0), self._defect(
                f"tool {call.name!r}", exc
            )
        left = self.timer.close_open_waits()  # a wait belongs to its call: one left open ends with the call (L04)
        after = self.timer.check("after_tool")
        self._log(index, call, run, self.rounds, tick, after, waited=self._waited(waited0), waits_left_open=left)
        if run.ends_attempt:
            over = time_ending(after, self.config, self.timer.infra_wait_s)  # a late result is not accepted (L04)
            if over is not None:
                detail = f"the terminal tool {call.name!r} returned, but {over.detail}"
                return run, Outcome(over.name, detail, infra_kind=over.infra_kind)
            return run, Outcome("finished", f"a call to the terminal tool {call.name!r} succeeded")
        return run, None

    def _waited(self, before: tuple[float, dict[str, float]]) -> tuple[float, dict[str, float]]:
        """The time the call just made spent waiting on infrastructure, in all and by kind: the timer's totals now, less
        what they were."""
        return self.timer.infra_wait_s - before[0], waited_since(before[1], self.timer.infra_wait_by_kind)

    def _failed(
        self, index: int, call: ToolCall, tick: Tick, detail: str, waited0: tuple[float, dict[str, float]]
    ) -> ToolRun:
        """Log a call whose provider raised, and answer it as skipped so its reply stays whole."""
        run = self.toolbox.skipped(call, "failed")
        left = self.timer.close_open_waits()
        after = self.timer.check("after_tool")
        self._log(
            index,
            call,
            run,
            self.rounds,
            tick,
            after,
            status="failed",
            detail=detail,
            waited=self._waited(waited0),
            waits_left_open=left,
        )
        return run

    def _skip(self, index: int, call: ToolCall, reason: str) -> ToolRun:
        """Answer and log a call that did not run (L09)."""
        run = self.toolbox.skipped(call, reason)
        self._log(index, call, run, self.rounds, None, None)
        return run

    def _skip_all(self, turn: ModelTurn, reason: str) -> None:
        """Log the calls of a reply that ended the attempt before any of them ran; the backend is given no results."""
        for call in turn.tool_calls:
            self._log(len(self.turns) - 1, call, self.toolbox.skipped(call, reason), 0, None, None)

    def _log(
        self,
        index: int,
        call: ToolCall,
        run: ToolRun,
        round_no: int,
        started: Tick | None,
        ended: Tick | None,
        *,
        status: str | None = None,
        detail: str = "",
        waited: tuple[float, dict[str, float]] = (0.0, {}),
        waits_left_open: int = 0,
    ) -> None:
        """Append the record of one call: what the model sent, what it saw, and the full raw result."""
        self.calls.append(
            run.record(
                call,
                turn=index,
                round_no=round_no,
                started_s=self.timer.since_start(started.now) if started else None,
                ended_s=self.timer.since_start(ended.now) if ended else None,
                status=status,
                detail=detail,
                waited_s=waited[0],
                waits_left_open=waits_left_open,
                terminal=self.toolbox.is_terminal(call.name),
                waited_by_kind=waited[1],
            )
        )

    # --- the ending -----------------------------------------------------------------------------------------------

    def _defect(self, where: str, exc: BaseException) -> Outcome:
        """Record a crash in our own code: the exception class and a stack hash without line numbers (L08)."""
        defect = harness_defect_of(exc)
        detail = f"{where} failed inside the harness ({defect.exception}, stack {defect.stack_sha256[:12]}): {exc}"
        return Outcome("harness_defect", detail, defect=defect)

    def _result(self) -> LoopResult:
        """Assemble the result: everything the attempt did, whatever ended it."""
        assert self.outcome is not None  # execute() sets it on every path
        self.timer.close_open_waits()  # whatever path ended the attempt, no wait is left open in the totals
        end = self.timer.check("end") if self.timer.started else None
        info = self.backend.info()
        usage = info.get("usage_seen") or {}
        # The terminal tools change what the run does, so they are part of the configuration the record keeps and hashes.
        record = {**self.config.as_dict(), "terminal_tools": self.toolbox.terminal_names}
        incomplete = self.usage_incomplete or bool(info.get("abandoned_request")) or bool(usage.get("missing"))
        return LoopResult(
            outcome=self.outcome,
            turns=self.turns,
            tool_calls=self.calls,
            final_text=self.final_text,
            rounds=self.rounds,
            malformed_calls=self.malformed_calls,
            malformed_events=self.malformed_events,
            tokens_used=self.tokens,
            usage_incomplete=incomplete,
            agent_s=end.agent_s if end else 0.0,
            elapsed_s=end.elapsed_s if end else 0.0,
            infra_wait_s=self.timer.infra_wait_s,
            infra_wait_by_kind=self.timer.infra_wait_by_kind,
            cleanup_s=self.timer.cleanup_s,
            cleanup_timed_out=self.timer.cleanup_timed_out,
            abandoned_threads=self.toolbox.abandoned_threads(),
            clock_readings=list(self.timer.readings),
            transcript=self.backend.transcript(),
            backend_info=info,
            tool_manifest_sha256=self.toolbox.manifest_sha256,
            config_sha256=config_sha256_of(record),
            config=record,
        )
