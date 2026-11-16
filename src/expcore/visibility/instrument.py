# Provenance: new
"""The process-visibility instrument: whether the agent's code hid a process or its memory from the harness's watch,
and what that leaves of the run's memory reading (namespace `process_visibility`).

`ProcessVisibility(source)` is an `Instrument` a platform's executor builds for each run whose sandbox keeps the
agent's memory limit with a watch of its own, where `/proc` can hide a process from it (an HPC batch node:
`expcore_onedata.sandbox.apptainer`). `source.process_visibility()` returns the watch's reading once the run's cells
have ended, or None when there is none. The snapshot, under `record.instruments["process_visibility"]`, holds counts,
reasons and byte figures, never a pid, a path or a name:

    {"version": 1, "memory": "within" | "over" | "unknown", "unknown": [<reasons>],
     "hidden_process": 0, "hidden_memory": 0,
     "figures": {"margin_bytes": ..., "start_unexplained_bytes": ..., "unexplained_peak_bytes": ...,
                 "cgroup_anon_bytes": ..., "visible_anon_bytes": ...}}

`memory` is the run's memory reading: `over` when the watch killed it for memory, `unknown` with each reason that held
(memory it could not tell whose, a hidden child, a reading that failed), `within` only when the check held throughout.
`hidden_process` counts the processes a cell's visible process had as children that `/proc` did not show,
`hidden_memory` the cells killed for memory nothing visible explained, and each figure is bytes or None. **No reading,
or one this code does not read, is `"memory": "unknown"`, never within.**
"""

from collections.abc import Mapping
from typing import Any, Protocol

NAMESPACE = "process_visibility"
VERSION = 1
WITHIN, OVER, UNKNOWN = "within", "over", "unknown"
READINGS = (WITHIN, OVER, UNKNOWN)
NOT_WATCHED, NOT_A_READING = "not_watched", "not_a_reading"
SNAPSHOT_KEYS = {"version", "memory", "unknown", "hidden_process", "hidden_memory", "figures"}
FIGURE_KEYS = {
    "margin_bytes", "start_unexplained_bytes", "unexplained_peak_bytes", "cgroup_anon_bytes", "visible_anon_bytes",
}  # fmt: skip


class ProcessVisibilitySource(Protocol):
    """Where the run's watch reading is: a mapping, or None when there is none."""

    def process_visibility(self) -> Mapping[str, Any] | None: ...


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _figure(value: Any) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def readable(data: Any) -> Mapping[str, Any] | None:
    """The snapshot when this code reads it, else None: every key and no other, its reasons non-empty texts, its
    counts whole numbers, its figures numbers of bytes or None; an unknown reading with a reason, and a reading within
    with no reason and nothing hidden."""
    if not isinstance(data, Mapping) or set(data) != SNAPSHOT_KEYS or data["version"] != VERSION:
        return None
    memory, reasons, figures = data["memory"], data["unknown"], data["figures"]
    if memory not in READINGS or not isinstance(reasons, list):
        return None
    if not all(isinstance(reason, str) and reason for reason in reasons):
        return None
    if not _whole(data["hidden_process"]) or not _whole(data["hidden_memory"]):
        return None
    if not isinstance(figures, Mapping) or set(figures) != FIGURE_KEYS or not all(map(_figure, figures.values())):
        return None
    if memory == UNKNOWN and not reasons:
        return None
    if memory == WITHIN and (reasons or data["hidden_process"] or data["hidden_memory"]):
        return None
    return data


def unknown_snapshot(why: str) -> dict[str, Any]:
    """The snapshot of a run with no reading this code reads: unknown, for `why`."""
    return {"version": VERSION, "memory": UNKNOWN, "unknown": [why], "hidden_process": 0, "hidden_memory": 0,
            "figures": dict.fromkeys(sorted(FIGURE_KEYS))}  # fmt: skip


def snapshot(reading: Any) -> dict[str, Any]:
    """The instrument's snapshot of a watch's reading (see the module's text)."""
    if reading is None:
        return unknown_snapshot(NOT_WATCHED)
    data = readable(reading)
    if data is None:
        return unknown_snapshot(NOT_A_READING)
    return {
        "version": VERSION,
        "memory": data["memory"],
        "unknown": list(data["unknown"]),
        "hidden_process": data["hidden_process"],
        "hidden_memory": data["hidden_memory"],
        "figures": {key: data["figures"][key] for key in sorted(FIGURE_KEYS)},
    }


class ProcessVisibility:
    """The `Instrument` of one run's process visibility: one per run, holding the source of its watch's reading."""

    namespace = NAMESPACE

    def __init__(self, source: ProcessVisibilitySource) -> None:
        self._source = source

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: the watch reads every cell."""

    def finish(self) -> dict[str, Any]:
        """The snapshot of the watch's reading, read once the run's cells have ended."""
        return snapshot(self._source.process_visibility())
