# Provenance: new
"""Which of the sandbox's own time bounds ended a cell, if one did (#220, O39).

A cell has three bounds of the sandbox's making, and a result's `bound` names the one that ended it:

- `CELL_TIMER`: the harness's own timer fired at `timeout_s` and killed what ran (a cell of the agent's, `record=True`;
  O17, O34).
- `CONTAINER_TIMEOUT`: the container's `timeout -s KILL`, `CAP_MARGIN_S` after `timeout_s`, killed the cell. It is the
  only bound of a harness step (`record=False`), which starts no timer, and the backstop of a cell of the agent's.
- `HOST_CALL`: `docker exec` did not return within `timeout_s` and `HOST_CALL_MARGIN_S`, and the harness gave up on it.

The timer and the host call are known from their own records. The container's `timeout` is known from nothing it
leaves behind: it ends the cell with exit 137 (GNU `timeout -s KILL`; a `timeout` that reports its own kill as a time
out would give 124), which is also the exit of a cell that kills itself, `os.kill(os.getpid(), 9)`. What tells them
apart is the time. The kill comes only when the cell has run the whole of the limit it was given, and the harness's
clock started before the container's, so a cell that ran for the whole limit by the harness's clock and ended with one
of those codes was ended by the bound. A cell that ended earlier did so by itself, and one that ran the whole limit and
ended with any other code, 0 included, was not killed. The one case it misreads is a cell that kills itself within the
start-up time of `docker exec` (a fraction of a second) before the backstop would have: it is read as the backstop's.
"""

CELL_TIMER = "cell_timer"
CONTAINER_TIMEOUT = "container_timeout"
HOST_CALL = "host_call"
BOUNDS = (CELL_TIMER, CONTAINER_TIMEOUT, HOST_CALL)

# What the container's `timeout` ends a cell with: 137, 128 plus SIGKILL, for GNU `timeout -s KILL`; 124 for a `timeout`
# that reports any time out so. The time, never the code alone, makes either a bound (see the module docstring).
BACKSTOP_EXITS = (137, 124)


def named(*, fired: bool, gave_up: bool, exit_code: int, ran_s: float, limit_s: float) -> str | None:
    """The bound that ended a cell, or None when none did.

    `fired` is the harness's own timer firing, `gave_up` the host giving up on `docker exec`, `exit_code` the cell's,
    `ran_s` how long the harness's clock saw the call take, and `limit_s` the seconds the container's `timeout` was
    given.
    """
    if fired:
        return CELL_TIMER
    if gave_up:
        return HOST_CALL
    if exit_code in BACKSTOP_EXITS and ran_s >= limit_s:
        return CONTAINER_TIMEOUT
    return None
