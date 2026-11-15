# Provenance: new
"""Detecting an agent's outbound network use where it cannot be blocked, for the analysis to report (issue #179).

On an HPC batch node an unprivileged user gets no network namespace, so an agent's network cannot be restricted. The
study's PI ruled (13 Nov 2026) that egress is acceptable there if it is detected: every run's outbound connections are
logged and classified, its code is scanned, flagged runs are reported, and the confirmatory contrasts are run again
without them as a sensitivity analysis. This package is that detection, in the core, from the record alone:

- `declare`: the run's `Declaration` (the data gate's endpoints, the loopback ranges, the platform's names) and the
  classification of a destination as `gate`, `loopback` or `outside`;
- `log`: the collector's log read and checked, intact or unknown with its reasons;
- `instrument`: `EgressLog`, the instrument (namespace `egress`) whose snapshot holds the counts by class and the
  outside destinations;
- `scan`: the static scan of the agent's code cells for URLs and download or network calls;
- `flags`: `judge`, one run's verdict, flagged for an outside connection, an unknown log or a scan hit.

The log comes from the HPC bundle's shim and collector (`deploy/hpc-federation/bundle/egresslog.c`,
`egress-collector.py`). **Detection, not a barrier:** a statically linked program, raw system calls, io_uring and
glibc's own DNS queries pass the shim, which is why the scan is the second line, and neither prevents egress. **A
missing or damaged log is unknown, never clean.**
"""

from expcore.egress.declare import CLASSES, GATE, LOOPBACK, OUTSIDE, Declaration, declaration
from expcore.egress.flags import FLAGS, LOG_UNKNOWN, OUTSIDE_FLAG, STATIC_SCAN, RunEgress, judge
from expcore.egress.instrument import INTACT, NAMESPACE, UNKNOWN, EgressLog, EgressSource, snapshot
from expcore.egress.log import Reading, read_log
from expcore.egress.scan import code_cells, scan_cells

__all__ = [
    "CLASSES",
    "FLAGS",
    "GATE",
    "INTACT",
    "LOG_UNKNOWN",
    "LOOPBACK",
    "NAMESPACE",
    "OUTSIDE",
    "OUTSIDE_FLAG",
    "STATIC_SCAN",
    "UNKNOWN",
    "Declaration",
    "EgressLog",
    "EgressSource",
    "Reading",
    "RunEgress",
    "code_cells",
    "declaration",
    "judge",
    "read_log",
    "scan_cells",
    "snapshot",
]
