# Provenance: new
"""The record of each fixed wait a reset takes: what bounds it, from which event, and how it ended (O07, O46, O51).

A reset's fixed waits are lower bounds counted from an event: the transfer settle from a space's previous observation of
the listings, the occupancy wait from the eviction stage's end (or from the end of a job that ran during it), the quiet
wait from the quiet stage's block-map reading at T, and each late look from the occupancy reading it vouches for. Each
stage that takes one records it in `wait_records`, one entry per wait:

- `why`: what it waits out: `transfer_settle` (the listing's lag, O07), `occupancy_lag` (the occupancy's lag for the
  deepest file, O46, `[placement] wait_mode = "fixed"`), `never_holds_counter` (the same lag, kept in `event` mode for
  a space with a provider declared never to hold its bytes, since only the counter can skip one), `block_maps` (`event`
  mode: no wait, the files' block maps show an eviction and a placement at once), `quiet_lag` (the quiet wait, fixed in
  both modes) or `late_look` (the listing's lag after a reading);
- `bound_s`: the seconds that must pass from the anchor before the read the wait guards;
- `anchor`: the event it counts from (`observation`, `eviction_end`, `job_end`, `quiet_maps`, `occupancy_reading` or
  `quiet_reading`);
- `observed_s`: the seconds this reset's clock counted from the anchor to that read, at least `bound_s`;
- `credited_s`: the part of the bound that had already passed when the wait began, so it was not slept: a space whose
  own event came earlier than another's, under one shared wait, or one whose work took that long;
- `polls`: how many such waits the stage took (the settle's observations less one, the occupancy's rounds, one
  otherwise);
- `ended`: `event` (a bound of 0: the read follows its event at once), `credited` (nothing slept: the bound had passed)
  or `bound` (the wait ran to its bound).

Each space's record also says whether its waits were taken once with other spaces' (`shared_waits`, true for a reset of
several spaces through `resetspaces.SpacesReset`) and under which `wait_mode`.
"""

from typing import Any


def entry(why: str, bound_s: float, anchor: str, observed_s: float, slept_s: float, polls: int = 1) -> dict[str, Any]:
    """One wait's record: `slept_s` is what the wait itself slept, the rest of the bound having passed before it."""
    bound = round(max(bound_s, 0.0), 3)
    observed = round(observed_s, 3)
    credited = round(min(bound, max(0.0, observed - slept_s)), 3)  # the anchor to the wait's start, within the bound
    ended = "event" if bound == 0 else ("credited" if slept_s <= 0 else "bound")
    return {
        "why": why,
        "bound_s": bound,
        "anchor": anchor,
        "observed_s": observed,
        "credited_s": credited,
        "polls": polls,
        "ended": ended,
    }


__all__ = ["entry"]
