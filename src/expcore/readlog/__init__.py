# Provenance: new
"""Detecting code in an agent's cell that forges a record of the read-log channel, or hides a real read from it, for the
analysis to report (O91, S20).

A study's canonical scripts log every read they make to a datagram socket the harness binds read-only into the cell,
and the study's omission scoring rests on that log. The agent's own code runs in the same cell, so **a forged record
cannot be prevented, only detected**. This package is the detection that reads code, in the core, from the record
alone:

- `scan`: the static scan of the code cells for the channel's names, a Unix datagram socket, a process's environment,
  ways to find the socket, and patches of the client or `socket`;
- `flags`: `judge`, one run's verdict, `flagged` or `clean`;
- `sensitivity`: `read_log_analysis`, every analysed run's verdict and the preregistered analysis again without the
  flagged runs.

A heuristic over text, and the second line: code built at run time passes it, and a run it does not flag is not shown to
have an unforged log. On a REST data path the gate's per-file served table (O22) is the check that does not read code.
The log's own state, intact or unknown, is `expcore_onedata.readlog` (the reader and the instrument).
"""

from expcore.readlog.flags import CLEAN, FLAGGED, RunReadLog, judge
from expcore.readlog.scan import KINDS, NAMESPACE, SCAN_VERSION, scan_cells
from expcore.readlog.sensitivity import ReadLogAnalysis, read_log_analysis

__all__ = [
    "CLEAN",
    "FLAGGED",
    "KINDS",
    "NAMESPACE",
    "SCAN_VERSION",
    "ReadLogAnalysis",
    "RunReadLog",
    "judge",
    "read_log_analysis",
    "scan_cells",
]
