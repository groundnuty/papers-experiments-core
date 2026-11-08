"""A child process that runs a scheduler and dies at a chosen point of an attempt, the way a kill does (C04).

    python scheduler_crash.py ROOT POINT [NTH [BEHAVIOUR]]

NTH (default 1) is the attempt, counted from the first of the process, that dies at POINT. BEHAVIOUR is how every
attempt ends: `finish` (the default), `defect` (a crash in our own code, a harness error) or `endpoint` (an
infrastructure failure of the endpoint).

POINT is `before_marker` (the 'running' row is committed, nothing else), `after_marker` (the outcome marker is on disk,
the store has not heard of the result) or `after_metadata` (the store has recorded the result). The process leaves with
exit status 9 and no clean-up, so nothing runs that a kill would have prevented.
"""

import os
import sys
from pathlib import Path

from scheduler_support import FakeExecutor, build

KILLED = 9
POINTS = ("before_marker", "after_marker", "after_metadata")
PLAN = {"tasks": ("task-a",), "seeds": (1,)}
BEHAVIOURS = {"finish": "finish", "defect": ("defect",), "endpoint": ("infra", "endpoint")}


def main(root: str, point: str, nth: str = "1", behaviour: str = "finish") -> None:
    """Run a campaign until its `nth` attempt reaches `point`, then die."""
    if point not in POINTS:
        raise SystemExit(f"unknown point {point!r}; the points are: {', '.join(POINTS)}")
    seen = []

    def hook(at: str, run_key: str) -> None:
        if at == point:
            seen.append(run_key)
            if len(seen) == int(nth):
                os._exit(KILLED)

    if behaviour not in BEHAVIOURS:
        raise SystemExit(f"unknown behaviour {behaviour!r}; the behaviours are: {', '.join(BEHAVIOURS)}")
    executor = FakeExecutor(default=BEHAVIOURS[behaviour])
    with build(Path(root), executor=executor, plan=PLAN, crash_hook=hook) as scheduler:
        scheduler.run()
    os._exit(0)


if __name__ == "__main__":
    main(*sys.argv[1:])
