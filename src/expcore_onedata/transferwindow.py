# Provenance: new
"""The run's window on the transfer log, and the split of a provider's gained bytes into transfer bytes and the rest.

**The window.** A job is in the run's window when its `scheduleTime` lies between the whole second the run started in
and the whole second after the block maps were read at its end. The schedule time is the one every job has, a waiting
one included, and it says when someone (the harness, the agent through the tool plane, a QoS requirement) started it.
A job scheduled before the window that had not ended by its start (listed as waiting or ongoing, or ended at or after
the start's whole second, or with no finish time) is not in the record, but it may have placed bytes during the run,
so it is counted as carried over and makes its target's bound unknown.

**The split.** `split_gained` gives, per tracked receiving provider, `transfer_bytes_in`: the `bytesReplicated` of every
job in the window that replicates into it (a replication's or a migration's `replicatingProviderId`; an eviction moves
nothing in), and `on_demand_lower_bound` = `gained_bytes` - `transfer_bytes_in`, floored at zero. `gained_bytes` is
net placement growth from the block maps (`probes.py`), not bytes on the wire. The bound is a lower bound on the bytes
the provider came to hold that no transfer placed (fetched on demand by a read through it), if a job's byte counter
does not under-report what it placed (O01: a job's report is a report). The sum counts every file a job moved, tracked
or not, so the bound only errs low; `difference` keeps the unfloored value so that a negative stays visible.

**A bound is known only when every input to it is established.** Otherwise it is None and `unknown` lists the reasons:
`transfer_not_ended` (a job into the provider had not ended at run end, so its counter was not final),
`transfer_carried_over` (a job into it from before the run was still running when the run began) and
`listings_disagree` (the tracked providers do not list and answer a job into it alike, `transferlog.py`).
"""

import math
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from expcore_onedata.transferrecord import Job

NOT_ENDED = "transfer_not_ended"
CARRIED_OVER = "transfer_carried_over"
DISAGREE = "listings_disagree"


@dataclass(frozen=True)
class Window:
    """The run's window in epoch seconds of this host's clock: from its start to just after its end's readings."""

    start: float
    end: float

    def holds(self, moment: float) -> bool:
        """Tell whether a provider's time, in whole seconds, lies in the window."""
        return math.floor(self.start) <= moment <= math.ceil(self.end)

    def carried_over(self, job: Job) -> bool:
        """Tell whether a job scheduled before the window may have gone on into it."""
        start = math.floor(self.start)
        ended_before = job.listed_as == "ended" and job.finished is not None and 0 < job.finished < start
        return job.scheduled < start and not ended_before

    def bears_on(self, job: Job) -> bool:
        """Tell whether a job can have placed bytes in the run: it is in the window or carried over into it."""
        return self.holds(job.scheduled) or self.carried_over(job)

    def to_json(self) -> dict[str, float]:
        return {"start": self.start, "end": self.end}


def split_gained(
    gained: Mapping[str, int],
    jobs: Iterable[Job],
    name_of: Callable[[str], str | None],
    *,
    flagged: Iterable[tuple[str, str]] = (),
    everywhere: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """Split each tracked provider's gained bytes into what the jobs in the window moved into it and a lower bound on
    the rest. `name_of` names the declared provider of an id; a job into no tracked provider counts for none.
    `flagged` holds `(target id, reason)` of jobs that leave their target's bound unknown, and `everywhere` the
    reasons that leave every bound unknown."""
    into = dict.fromkeys(gained, 0)
    reasons: dict[str, set[str]] = {provider: set(everywhere) for provider in gained}
    for job in jobs:
        name = name_of(job.target_id) if job.target_id else None
        if name in into:
            into[name] += job.bytes_replicated or 0
            if job.listed_as != "ended":
                reasons[name].add(NOT_ENDED)
    for target_id, reason in flagged:
        name = name_of(target_id)
        if name in reasons:
            reasons[name].add(reason)
    out: dict[str, dict[str, Any]] = {}
    for provider, bytes_gained in gained.items():
        difference = bytes_gained - into[provider]
        out[provider] = {
            "gained_bytes": bytes_gained,
            "transfer_bytes_in": into[provider],
            "difference": difference,
            "on_demand_lower_bound": None if reasons[provider] else max(0, difference),
            "unknown": sorted(reasons[provider]) or None,
        }
    return out
