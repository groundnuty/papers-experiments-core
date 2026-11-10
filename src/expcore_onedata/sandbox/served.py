# Provenance: new
"""The platform's instrument of the bytes a run's agent read through its data path (O20).

`ServedBytes(sandbox)` is an `Instrument` (namespace `served_bytes`) a study's executor builds for each run, once
`SandboxPlatform.launch` has yielded the run's sandbox, and runs with the others: `start_instruments` before
`record_run`, then `sandbox.collect(...)`, then `finish_instruments`. `finish` records the sandbox's count
(`DockerSandbox.served_bytes`), so a scorer reads it at `record.instruments["served_bytes"]` and replay serves the
recorded value; nothing is measured again. Its snapshot:

  {"version": 1, "data_path": "rest", "listing": "complete", "served_bytes": 51234, "content_bytes": 40960,
   "listing_bytes": 10240, "other_bytes": 34, "gate_answer_bytes": 0, "unknown": []}

What each count holds is in `gatelog` (the bytes of the platform's answers the gate's socket accepted for the agent,
by what they were; the gate's own answers apart). A REST data path is counted from the log of the gate the collection
stopped, so `finish` before `collect` records every count unknown (`gate_not_collected`); a mount's reads go through
oneclient, which no gate sees, so its counts are unknown (`mount_not_through_gate`); a run with no data path read
nothing through one, zero. Every count is None whenever `unknown` names a reason, and unknown is never zero.
"""

from collections.abc import Mapping
from typing import Any, Protocol

NAMESPACE = "served_bytes"
VERSION = 1


class CountsServed(Protocol):
    """A sandbox that counts the bytes its agent read through its data path (`DockerSandbox`)."""

    def served_bytes(self) -> dict[str, Any]: ...


class ServedBytes:
    """The `Instrument` of the bytes one run's agent read through its data path: one per run, holding its sandbox."""

    namespace = NAMESPACE

    def __init__(self, sandbox: CountsServed) -> None:
        self._sandbox = sandbox

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: the gate's log begins with the sandbox."""

    def finish(self) -> dict[str, Any]:
        """The count, as the sandbox's collection left it."""
        return {"version": VERSION, **self._sandbox.served_bytes()}


__all__ = ["NAMESPACE", "VERSION", "CountsServed", "ServedBytes"]
