"""A child process that runs a study through the command line and dies at a chosen point of an attempt, as a kill does.

    python study_crash.py STUDY POINT [NTH]

NTH (default 1) is the attempt, counted from the first of the process, that dies at POINT. `before_marker`: the
'running' row is committed and the executor has returned, nothing else. `after_marker`: the outcome marker is on disk,
the store has not heard of the result. `after_metadata`: the store has recorded the result. The process leaves with exit
status 9 and no clean-up, so nothing runs that a kill would have prevented (C04). A campaign that is never killed ends
with the command's own exit status.
"""

import os
import sys

from expcore.cli import main

KILLED = 9
POINTS = ("before_marker", "after_marker", "after_metadata")


def run(study: str, point: str, nth: str = "1") -> None:
    """Run the study's campaign until its `nth` attempt reaches `point`, then die."""
    if point not in POINTS:
        raise SystemExit(f"unknown point {point!r}; the points are: {', '.join(POINTS)}")
    seen: list[str] = []

    def hook(at: str, run_key: str) -> None:
        if at == point:
            seen.append(run_key)
            if len(seen) == int(nth):
                os._exit(KILLED)

    os._exit(main(["run", study], crash_hook=hook))


if __name__ == "__main__":
    run(*sys.argv[1:])
