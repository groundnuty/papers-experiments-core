# Provenance: new
"""One generation of a run's host mount: a oneclient, its mount, its watcher and its log (O97).

A run's mount is made once, at the start (generation 1). When a cell's deadline aborts its FUSE connection and the
survivors were the agent's (`deadline.Escalation.restorable`), the host mount makes a fresh one at the same mount point
before the next cell (`HostMount.restore`): generation 2, then 3, and so on. Each generation has a oneclient and a
mount-watcher of its own, because the watcher is tied to one oneclient and one armed window (`mountwatch`): any event on
the token file after `armed` counts as the agent's access, and a restore writes the token file again. So each
generation is watched from its own `armed` to its own `disarmed`, in a log of its own, and the run's guard reads them
all (`HostMount.mount_watch_logs`). One run nonce serves every generation.

Names: generation 1 keeps the files of a run that is never restored (`mountwatch.jsonl`, `oneclient.stdout`), and
generation g >= 2 has `mountwatch-<g>.jsonl`, `oneclient-<g>.stdout`: a stream file is made new for every process
(`NodeOps.spawn` refuses one that exists).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from expcore_onedata.sandbox.apptainer import helpers


def suffix(generation: int) -> str:
    """What a file or role of a generation is named with beyond generation 1's names: nothing for 1, `-<g>` after."""
    return "" if generation == 1 else f"-{generation}"


@dataclass
class Generation:
    """What one generation of the mount holds and records; `HostMount` keeps them in order, the live one last."""

    number: int
    oneclient: helpers.Helper | None = None
    watch: helpers.Helper | None = None
    device: str | None = None  # the mount's device ("0:N") when it came up: the connection's identity (`fusectl.conn`)
    fusectl: str | None = None  # `fusectl.state` of its connection when it came up
    wait_s: float | None = None  # oneclient's start to its mount
    abort: dict[str, Any] | None = None  # the record of the abort that ended it (`HostMount.abort`), if one did
    before: dict[str, dict[str, Any]] = field(default_factory=dict)  # a role's state just before the harness ended it
    closed: bool = False  # its watch is closed: stopped, its log read
    log: bytes | None = None  # its watcher's log, once closed
    oneclient_log: bytes | None = None  # oneclient's INFO log of this generation, read before the next one starts

    def record(self) -> dict[str, Any]:
        """What the run's record keeps of the generation: never a path, a host or a secret."""
        return {"wait_s": self.wait_s, "fusectl": self.fusectl, "abort": dict(self.abort) if self.abort else None}


__all__ = ["Generation", "suffix"]
