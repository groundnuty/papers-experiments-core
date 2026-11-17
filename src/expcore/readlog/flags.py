# Provenance: new
"""One run's read-log verdict, from its record alone: the scan of its code cells (O91, S20).

`judge(record, condition)` scans the code cells of a record (`expcore.egress.scan.code_cells`) with
`expcore.readlog.scan` and returns a `RunReadLog`. It is computed at analysis time and never stored in the record. A run
is `flagged` when any cell has a hit, `clean` otherwise: **`clean` says only that the scan found nothing in the text,
never that the log is unforged**. The state of the log itself, intact or unknown, is the instrument's snapshot and not
this verdict's. A run whose record carries no `read_log` snapshot is judged by its code like any other.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore.egress.scan import code_cells
from expcore.readlog.scan import KINDS, scan_cells

CLEAN, FLAGGED = "clean", "flagged"  # the verdict of a run, as the guards read it


@dataclass(frozen=True)
class RunReadLog:
    """One run's verdict: the scan of its code cells under its run key, attempt and condition."""

    run_key: str
    attempt: int
    condition: str
    scan: Mapping[str, Any]

    @property
    def flagged(self) -> bool:
        return bool(self.scan["flagged"])

    @property
    def verdict(self) -> str:
        return FLAGGED if self.flagged else CLEAN

    @property
    def kinds(self) -> tuple[str, ...]:
        """The kinds of hit the run has, in the order of `KINDS`."""
        held = {hit["kind"] for hit in self.scan["hits"]}
        return tuple(kind for kind in KINDS if kind in held)


def judge(record: Any, condition: str) -> RunReadLog:
    """The read-log verdict of one analysed run: the scan of the code cells the egress scan reads."""
    return RunReadLog(record.run_key, record.attempt, condition, scan_cells(code_cells(record)))
