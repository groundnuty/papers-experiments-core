"""A child process that runs the first steps of an attempt and dies at a chosen point, the way a kill does (C04).

    python campaign_crash.py DATABASE RUN_DIR POINT

POINT is `before_marker` (the 'running' row is committed, nothing else), `after_marker` (the outcome marker is on disk,
the store has not heard of the result) or `after_metadata` (the store has recorded the result). The process leaves with
exit status 9 and no clean-up, so nothing runs that a kill would have prevented.
"""

import os
import sys
from pathlib import Path

from campaign_support import NOW, finished, make_manifest, make_schema

from expcore.campaign.markers import write_marker
from expcore.campaign.store import Store

KILLED = 9
POINTS = ("before_marker", "after_marker", "after_metadata")


def main(database: str, run_dir: str, point: str) -> None:
    """Begin the first planned run's attempt, then die at `point`."""
    if point not in POINTS:
        raise SystemExit(f"unknown point {point!r}; the points are: {', '.join(POINTS)}")
    schema = make_schema()
    store = Store(Path(database), schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    key = manifest.rows[0].key.text
    begun = store.begin_attempt(key, NOW, os.getpid(), run_dir)
    if point == "before_marker":
        os._exit(KILLED)
    result = finished(input_tokens=10, output_tokens=5)
    write_marker(Path(run_dir), key, begun.id, result)
    if point == "after_marker":
        os._exit(KILLED)
    store.finish_attempt(begun.id, result, 3)
    os._exit(KILLED)


if __name__ == "__main__":
    main(*sys.argv[1:])
