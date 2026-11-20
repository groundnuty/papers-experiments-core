# Provenance: new
"""The run's helper processes on the node: the data gate and the egress collector, started, read and stopped (O56).

Each is a process of the driver's uid in a session of its own (`NodeOps.spawn`), its streams in two owner-only files
of the run's directory, named by its pid and start time so that no signal reaches a process that took its pid over.
It is ready when its stdout holds its ready line (`gate ready`, `egress collector ready`), and a helper that ends first,
or does not say it in time, fails the start. A stop is SIGTERM, a wait, then SIGKILL and a wait; it is shown gone, or
the stop raises: a teardown never leaves a helper it could not show gone unsaid.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer.node import NodeOps, Spawned

READY_WAIT_S = 30.0
TERM_WAIT_S = 3.0  # the gate's own teardown budget is 1 s (gate.py), the collector's a read of what is queued
KILL_WAIT_S = 5.0
POLL_S = 0.1


@dataclass(frozen=True)
class Helper:
    """A started helper: its role, its process and the files of its streams."""

    role: str
    process: Spawned
    stdout: Path
    stderr: Path

    def record(self) -> dict[str, Any]:
        return {"role": self.role, **self.process.record()}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def start(
    node: NodeOps,
    role: str,
    argv: Sequence[str],
    directory: Path,
    ready: str,
    *,
    noted: Callable[[Helper], None],
    env: Mapping[str, str] | None = None,
    wait_s: float = READY_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Helper:
    """Spawn a helper and wait for its ready line. `env` is the run's tag, so the helper is found by it even if the
    driver dies before the run's record names it; `noted` is given it as soon as it exists, before the wait, so a
    teardown finds it whatever happens next."""
    stdout, stderr = directory / f"{role}.stdout", directory / f"{role}.stderr"
    helper = Helper(role, node.spawn(argv, stdout=stdout, stderr=stderr, env=env), stdout, stderr)
    noted(helper)
    until = clock() + wait_s
    while True:
        if ready in _read(helper.stdout).splitlines():
            return helper
        if node.alive(helper.process.pid, helper.process.start) is False:
            raise SandboxError(f"the {role} ended before it was ready")
        if clock() >= until:
            raise SandboxError(f"the {role} did not say it was ready within {wait_s:g} s")
        sleep(POLL_S)


def _wait(node: NodeOps, process: Spawned, wait_s: float, sleep: Callable[[float], None], clock) -> bool | None:
    """Whether the process is gone within `wait_s`; None when that could not be told."""
    until = clock() + wait_s
    while True:
        state = node.alive(process.pid, process.start)
        if state is not True:
            return None if state is None else True
        if clock() >= until:
            return False
        sleep(POLL_S)


def wait_gone(
    node: NodeOps,
    process: Spawned,
    wait_s: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool | None:
    """Whether the process is shown gone within `wait_s` (False when it is still there, None when that could not be
    told): the wait `stop` makes after each signal, for a caller that signals by itself."""
    return _wait(node, process, wait_s, sleep, clock)


def stop(
    node: NodeOps,
    helper: Helper,
    *,
    term_wait_s: float = TERM_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """SIGTERM, a wait, SIGKILL, a wait: the helper shown gone, or `SandboxError`. The record of how it ended: whether
    the TERM was sent and was enough, and its exit status when the node knows it."""
    note: dict[str, Any] = {"term": node.signal(helper.process.pid, helper.process.start, signal.SIGTERM)}
    gone = _wait(node, helper.process, term_wait_s, sleep, clock)
    note["ended_on_term"] = gone
    if gone is False:
        note["kill"] = node.signal(helper.process.pid, helper.process.start, signal.SIGKILL)
        gone = _wait(node, helper.process, KILL_WAIT_S, sleep, clock)
    note["exit_status"] = node.exit_status(helper.process)
    if gone is not True:
        raise SandboxError(f"the {helper.role} could not be shown gone after its stop")
    return note


def output(helper: Helper) -> str:
    """What the helper wrote on both its streams (no secret: neither the gate nor the collector prints one)."""
    return _read(helper.stderr) + _read(helper.stdout)


__all__ = ["Helper", "output", "start", "stop", "wait_gone"]
