"""The scenario of the live proof of the per-cell kill (L18), and what it must show: one scripted run over a real
sandbox, the same code for both backends (tests/live/test_code_cell_bound.py on Docker and
tests/live/cell_bound_apptainer.py on an HPC node), checked offline over the fake host and the fake node
(tests/onedata/sandbox/test_cell_bound_scenario.py).

The run declares `code_cell_bound_s`, far below the agent's time cap, and a scripted model sends three turns: a cell
that starts a child and a detached grandchild, prints a line and sleeps well past the bound; a cell that prints a
line; the final answer. The loop is the real one and the record is the real record. Nothing here needs a federation,
a token, a model or a network: the data path is none.

`run_scenario` returns the facts, and never asserts, so a red run still reports what it saw; `problems(facts)` lists
what is wrong with them, and an empty list is the proof. The facts hold outcomes, counts and seconds only: no token,
address, name or path. The independent look (`look`) is a callable that returns the lines of the host's process list
that carry the run's marker: `docker top` on Docker, `ps` on a node.
"""

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from expcore.config.models import Limits
from expcore.loop.config import AgentConfig
from expcore.models.adapters import StubAdapter
from expcore.records.build import RunIdentity, record_attempt
from expcore.records.record import RunRecord, decode
from expcore.records.schema import validate
from expcore.replay.replay import replay_record
from expcore_onedata.sandbox import deadline
from expcore_onedata.sandbox.codetool import CodeExecutionProvider

BOUND_S = 60.0  # the declared limit on one cell
SLEEP_S = 180  # how long the cell would run: three times the limit
CAP_S = 600.0  # the agent's time cap: far past the limit, so the limit is what ends the cell
GRACE_S = 5.0  # the sandbox's kill grace, which bounds the kill's escalation (`deadline.margin_s`)
LOOK_FOR_S = 40.0  # how long the look waits for the cell's processes to show
NEXT_LINE = "next cell ran"
IDENTITY = RunIdentity(
    run_key="task=cell-bound|model=scripted|seed=0",
    factors={"task": "cell-bound", "model": "scripted", "seed": 0},
    attempt=1,
)
CODE = {"commit": "c" * 40, "dirty": False, "source": "git", "stale": False, "why": None, "tree_sha256": "d" * 64}

CELL = """
import subprocess, sys, time
marker = {marker!r}
sleeper = "import time; time.sleep(600)"
subprocess.Popen([sys.executable, "-c", sleeper, marker])
subprocess.Popen([sys.executable, "-c", sleeper, marker], start_new_session=True)
print("cell started", flush=True)
time.sleep({sleep_s})
print("cell finished", flush=True)
"""


async def _never(seconds: float) -> None:
    """Stands in for `asyncio.sleep` under a fake clock: the loop's bound passes only when the clock is moved."""
    await asyncio.Event().wait()


def cell_code(marker: str, sleep_s: int = SLEEP_S) -> str:
    """The first cell: a child and a detached grandchild, each carrying the marker in its argv, then a long sleep."""
    return CELL.format(marker=marker, sleep_s=sleep_s)


def limit_text(bound_s: float) -> str:
    """What the code tool tells the agent of a cell the limit killed (`CodeExecutionProvider`)."""
    return f"the code ran past its {bound_s:g} s limit and was killed"


def watch_for(look: Callable[[], list[str]], seen: dict[str, Any], *, wait_s: float, poll_s: float = 2.0) -> None:
    """Look for the cell's processes until two show (the child and the grandchild) or `wait_s` passes, and keep the most
    seen and when. It runs in a thread of its own while the loop runs the cell."""
    began, most = time.monotonic(), 0
    while time.monotonic() - began < wait_s:
        try:
            most = max(most, len(look()))
        except Exception as error:  # a look that failed is a finding, never a crash of the thread
            seen["look_error"] = type(error).__name__
        if most >= 2:
            break
        time.sleep(poll_s)
    seen.update(marked=most, at_s=round(time.monotonic() - began, 3))


def run_scenario(
    sandbox: Any,
    look: Callable[[], list[str]],
    marker: str | None = None,
    *,
    bound_s: float = BOUND_S,
    sleep_s: int = SLEEP_S,
    cap_s: float = CAP_S,
    look_for_s: float = LOOK_FOR_S,
    clock: Callable[[], float] | None = None,
    record_out: Path | None = None,
) -> dict[str, Any]:
    """Run the scripted attempt over the sandbox and return the facts. `marker` is a string no other process carries."""
    marker = marker or f"cellbound-{uuid.uuid4().hex[:12]}"
    limits = Limits(
        max_tool_rounds=5, time_cap_s=cap_s, token_budget=100_000, output_cap_chars=8000, max_output_tokens=4096
    )
    config = AgentConfig(limits, code_cell_bound_s=bound_s)
    script = [
        [("python", {"code": cell_code(marker, sleep_s)})],
        [("python", {"code": f"print({NEXT_LINE!r})"})],
        "done",
    ]
    before: dict[str, Any] = {"marked": 0, "at_s": None}
    watcher = threading.Thread(target=watch_for, args=(look, before), kwargs={"wait_s": look_for_s}, daemon=True)
    options = {} if clock is None else {"clock": clock, "sleep": _never}  # a fake clock moves only when a test says
    watcher.start()
    record = asyncio.run(
        record_attempt(
            StubAdapter(script), [CodeExecutionProvider(sandbox)], "system", "task", config, IDENTITY,
            code=dict(CODE), core_version="0.1.0", **options,
        )
    )  # fmt: skip
    watcher.join(look_for_s + 10)
    after = len(look())
    text = record.dumps()
    if record_out is not None:
        record_out.write_text(text, encoding="utf-8")
    first, second = [*record.tool_calls, {}, {}][:2]
    cell = (sandbox.metadata().get("cell_deadlines") or [{}])[0]
    return {
        "bound_s": bound_s, "sleep_s": sleep_s, "cap_s": cap_s,
        "before": before, "after": {"marked": after},
        "call_1": {k: first.get(k) for k in ("status", "error_class", "elapsed_s", "text")},
        "call_2": {k: second.get(k) for k in ("status", "error_class", "text")},
        "outcome": {"name": record.outcome["name"], "infra_kind": record.outcome["infra_kind"]},
        "final_text": bool(record.totals.get("final_text")),
        "deadline": {"outcome": cell.get("outcome"), "steps": [s.get("step") for s in cell.get("steps", [])]},
        "record": {
            "valid": validate(decode(text)).startswith("expcore-record/"),
            "bound_s": record.agent_config.get("code_cell_bound_s"),
            "tools": record.agent_config.get("code_cell_tools"),
            "replay_identical": replay_record(RunRecord.from_json(decode(text))).dumps() == text,
        },
    }  # fmt: skip


def problems(facts: dict[str, Any], *, grace_s: float = GRACE_S) -> list[str]:
    """What is wrong with the facts: empty when the kill was shown live (the eight points of the plan)."""
    found: list[str] = []
    bound_s, call_1, call_2 = facts["bound_s"], facts["call_1"], facts["call_2"]
    if facts["before"]["marked"] < 2:
        found.append(
            f"before the bound {facts['before']['marked']} marked process(es) were seen, not the child and grandchild"
        )
    elapsed = call_1.get("elapsed_s")
    if elapsed is None or not bound_s - 1.0 <= elapsed <= bound_s + deadline.margin_s(grace_s):
        found.append(
            f"the call took {elapsed} s, not about {bound_s:g} s (at most {deadline.margin_s(grace_s):g} s over)"
        )
    if facts["deadline"]["outcome"] != deadline.KILLED or "kill" not in facts["deadline"]["steps"]:
        found.append(f"the sandbox's deadline record is {facts['deadline']}, not a kill that took")
    if facts["after"]["marked"] != 0:
        found.append(f"{facts['after']['marked']} marked process(es) were still there after the call")
    if (call_1.get("status"), call_1.get("error_class")) != ("error", "timeout") or not str(
        call_1.get("text")
    ).endswith(limit_text(bound_s)):
        found.append(f"the agent was not told the limit: {call_1}")
    if call_2.get("status") != "ok" or NEXT_LINE not in str(call_2.get("text")):
        found.append(f"the next cell did not run: {call_2}")
    if facts["outcome"] != {"name": "finished", "infra_kind": None} or not facts["final_text"]:
        found.append(f"the run did not finish with its answer: {facts['outcome']}")
    record = facts["record"]
    if not (
        record["valid"]
        and record["bound_s"] == bound_s
        and record["tools"] == ["python"]
        and record["replay_identical"]
    ):
        found.append(f"the record is not as expected: {record}")
    return found
