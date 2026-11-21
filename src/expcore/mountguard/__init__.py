# Provenance: new
"""Detecting an agent's access to the mount's token or to oneclient's process where it cannot be prevented, for the
analysis to report (#179, milestone 2).

On an HPC batch node the agent's cell and oneclient run as one uid: no root, no user namespace, and FUSE without
`user_allow_other`, so a mount is visible only to the uid that made it. "The agent holds no token" cannot be enforced
as the Docker sidecar enforces it. The study's PI ruled (16 Nov 2026) that the mount path stays on HPC with detection
and report, as for egress: oneclient runs on the host, outside the cell, with its token in a file the cell's binds do
not reach; a host watcher logs every access to that file and its directory, samples oneclient for a tracer, and proves
from outside that the cell runs in its own PID namespace with no view of the host's processes; flagged runs are
reported, and the analysis is run again without them. This package is the core's side, from the record alone:

- `log`: the watcher's log read and checked, intact or unknown with its reasons, the accesses in the agent's window
  and the isolation proofs;
- `instrument`: `MountGuard`, the instrument (namespace `mount_guard`) whose snapshot holds the counts and reasons;
- `flags`: `judge`, one run's verdict, `clean`, `flagged` (an access), `unknown` (a log or an isolation not shown) or
  `not_applicable` (a run with no mount: a REST run, a run with no data path, as its record says);
- `campaign`: the data path of each level of a study's data-path factor, as the campaign's fingerprint records it;
- `sensitivity`: every run's verdict and the analysis again without the runs that are not clean; a run the guard does
  not apply to stays in it.

The watcher is `expcore_onedata.sandbox.mountwatch`. **Detection, not a barrier:** inotify names no process, so every
event in the window is counted as the agent's; it sees an open or a read, never a read of memory the file was mapped
into; a tracer is sampled, so one that attaches and leaves between samples is missed; and oneclient's `/proc` entries
(its environment, which holds the token, and its memory) are covered only by the proof that the cell cannot see the
host's processes at all, never watched. **A missing or damaged log, or an unproven isolation, is unknown, never
clean.**
"""

from expcore.mountguard.flags import (
    ACCESS_FLAGS,
    CLEAN,
    FLAGGED,
    FLAGS,
    ISOLATION_UNPROVEN,
    LOG_UNKNOWN,
    NOT_APPLICABLE,
    PROCESS_ACCESS,
    TOKEN_ACCESS,
    UNKNOWN_VERDICT,
    VERDICTS,
    RunMountGuard,
    judge,
    verdict_of,
)
from expcore.mountguard.instrument import (
    GENERATIONS,
    INTACT,
    NAMESPACE,
    PROVEN,
    UNKNOWN,
    UNPROVEN,
    MountGuard,
    MountRunSource,
    MountWatchSource,
    NoMount,
    aggregate,
    combine,
    logs_of,
    mount_guard_for,
    not_applicable_snapshot,
    snapshot,
)
from expcore.mountguard.log import Reading, read_log

__all__ = [
    "ACCESS_FLAGS",
    "CLEAN",
    "FLAGGED",
    "FLAGS",
    "GENERATIONS",
    "INTACT",
    "ISOLATION_UNPROVEN",
    "LOG_UNKNOWN",
    "NAMESPACE",
    "NOT_APPLICABLE",
    "PROCESS_ACCESS",
    "PROVEN",
    "TOKEN_ACCESS",
    "UNKNOWN",
    "UNKNOWN_VERDICT",
    "UNPROVEN",
    "VERDICTS",
    "MountGuard",
    "MountRunSource",
    "MountWatchSource",
    "NoMount",
    "Reading",
    "RunMountGuard",
    "aggregate",
    "combine",
    "judge",
    "logs_of",
    "mount_guard_for",
    "not_applicable_snapshot",
    "read_log",
    "snapshot",
    "verdict_of",
]
