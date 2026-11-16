# Provenance: new
"""Detecting an agent's process, or its memory, hidden from the harness's watch where `/proc` can hide it, for the
analysis to report (O56).

On an HPC batch node there is no cgroup per container, so the agent's memory limit is a watch of the harness's over
the cell's processes in `/proc`, and `/proc` is mounted `hidepid=invisible`: a process of the agent's that makes itself
non-dumpable has no entry, and the watch can neither count nor kill it. The sandbox checks from outside
(`expcore_onedata.sandbox.apptainer.hidden`): the cgroup's anonymous memory against what the visible processes hold,
and a child of a cell process that `/proc` does not show. An agent's code that makes itself invisible to the harness is
a finding of its own, reported beside the egress and mount guards, and a run whose memory could not be read has an
unknown limit condition, never one within its limit. This package is the core's side, from the record alone:

- `instrument`: `ProcessVisibility`, the instrument (namespace `process_visibility`) whose snapshot holds the memory
  reading, its reasons, the hidden counts and the watch's figures;
- `flags`: `judge`, one run's verdict, `clean`, `flagged` (a hidden process or hidden memory) or `unknown` (a memory
  reading that is unknown, or none);
- `sensitivity`: every run's verdict and the analysis again without the runs that are not clean.

**Detection, not a barrier:** a process that forks twice under the cell's hidden init is a child of no visible process,
hidden memory under the margin is not told from the node's own, and a hidden process's shared memory is not compared.
**A missing reading, or an unknown one, is never clean.**
"""

from expcore.visibility.flags import (
    CLEAN,
    FLAGGED,
    FLAGS,
    HIDDEN_MEMORY,
    HIDDEN_PROCESS,
    HIDING_FLAGS,
    MEMORY_UNKNOWN,
    UNKNOWN_VERDICT,
    VERDICTS,
    RunVisibility,
    judge,
    verdict_of,
)
from expcore.visibility.instrument import (
    NAMESPACE,
    OVER,
    READINGS,
    UNKNOWN,
    WITHIN,
    ProcessVisibility,
    ProcessVisibilitySource,
    snapshot,
)

__all__ = [
    "CLEAN",
    "FLAGGED",
    "FLAGS",
    "HIDDEN_MEMORY",
    "HIDDEN_PROCESS",
    "HIDING_FLAGS",
    "MEMORY_UNKNOWN",
    "NAMESPACE",
    "OVER",
    "READINGS",
    "UNKNOWN",
    "UNKNOWN_VERDICT",
    "VERDICTS",
    "WITHIN",
    "ProcessVisibility",
    "ProcessVisibilitySource",
    "RunVisibility",
    "judge",
    "snapshot",
    "verdict_of",
]
