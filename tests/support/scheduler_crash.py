"""A child process that runs a scheduler and dies at a chosen point of the first attempt, the way a kill does (C04).

    python scheduler_crash.py ROOT POINT

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


def main(root: str, point: str) -> None:
    """Run a campaign until its first attempt reaches `point`, then die."""
    if point not in POINTS:
        raise SystemExit(f"unknown point {point!r}; the points are: {', '.join(POINTS)}")

    def hook(at: str, run_key: str) -> None:
        if at == point:
            os._exit(KILLED)

    with build(Path(root), executor=FakeExecutor(), plan=PLAN, crash_hook=hook) as scheduler:
        scheduler.run()
    os._exit(0)


if __name__ == "__main__":
    main(*sys.argv[1:])
