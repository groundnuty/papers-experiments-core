# Provenance: new
"""What a health check is told of the attempt it judges (O82).

A check that decides whom a failure is charged to (a marker an attempt left behind, an injection it made) has to tell
the attempt it judges from the ones it merely meets: the gate runs the pre-run checks of some runs, the post-run checks
of others and its own, in threads of their own at the same moment. The gate hands such a check a `CheckContext`, the
identity of the attempt the check is judging, as an argument of the call, so no check has to read the stack of its
caller to find out which of them it is.

**One rule decides whether a check is handed it** (`takes_context`, read once, when the gate is built): a check whose
call signature has a parameter named `context` receives the context, passed by keyword (`check(context=...)`), whether
the parameter has a default or not. Every other check is called with no argument, exactly as before: a check with
`*args` or `**kwargs`, with a parameter of another name even when it has a default (`timeout=30`), a mock, and a
callable whose signature cannot be read. Only the name opts in, so no check that works today is handed something it
does not expect.

A signature that cannot be read one way is **refused when the gate is built**, with a `ConfigError` that names the
parameter, and not at the first call, where it read as a failed check for ever: a `context` that cannot be passed by
keyword (positional-only, `*context`, `**context`), and a required parameter the gate would never fill
(`def check(ctx)`, which reads as a context that was meant to be received, or `def check(context, other)`).

The context is **None** for a check that judges no attempt: the dispatcher's own check (the gate re-checking while it is
closed), and a call of `before_run` or `after_run` that names none. A check that takes the context takes the None.
"""

import inspect
from dataclasses import dataclass
from typing import Any

from expcore.config.loading import ConfigError

PRE_RUN, POST_RUN = "pre_run", "post_run"
PHASES = (PRE_RUN, POST_RUN)
PARAMETER = "context"  # the name that opts a check in
_UNPASSABLE = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
_FILLED = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
)


@dataclass(frozen=True)
class CheckContext:
    """The attempt a health check is judging.

    `run_key` is the run's key. `attempt` is the number the store gives the attempt (`BegunAttempt.number`, the
    `RunIdentity`'s `attempt`): one more than the run's charged attempts when it starts, so a pre-run check is given the
    number the try will have, and the attempt that follows it is the same number. An attempt set aside uncharged, or
    interrupted, is run again under the number it had, so two attempts of one run can share it: `attempt_id` tells
    them apart. `phase` is `PRE_RUN` (before the attempt) or `POST_RUN` (after its result). `attempt_id` is the store's
    id of the attempt's row, which `liveness.owner` names too: set in a post-run check, None in a pre-run check, which
    runs before the row exists."""

    run_key: str
    attempt: int
    phase: str
    attempt_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_key, str) or not self.run_key:
            raise ValueError(f"CheckContext: the run key is a non-empty string, not {self.run_key!r}")
        if self.phase not in PHASES:
            raise ValueError(f"CheckContext: the phase is one of {', '.join(PHASES)}, not {self.phase!r}")
        if not _whole(self.attempt):
            raise ValueError(f"CheckContext: the attempt is a positive whole number, not {self.attempt!r}")
        if self.phase == PRE_RUN and self.attempt_id is not None:
            raise ValueError("CheckContext: a pre-run check runs before the attempt's row exists, so it has no id")
        if self.phase == POST_RUN and not _whole(self.attempt_id):
            raise ValueError(
                f"CheckContext: a post-run check judges a stored attempt, so its id is a positive whole number, "
                f"not {self.attempt_id!r}"
            )


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _name_of(check: Any) -> str:
    return getattr(check, "__qualname__", None) or type(check).__qualname__


def takes_context(check: Any) -> bool:
    """Whether the gate hands `check` a `CheckContext`: True when its signature has a parameter named `context`, False
    for every other check. Raise ConfigError for a signature that is ambiguous (see the module docstring); a callable
    whose signature cannot be read is not one, and is called with no argument."""
    try:
        parameters = inspect.signature(check).parameters
    except (TypeError, ValueError):
        return False
    name = _name_of(check)
    declared = parameters.get(PARAMETER)
    if declared is not None and declared.kind in _UNPASSABLE:
        raise ConfigError(
            f"the health check {name} declares '{PARAMETER}' as {declared.kind.description}, and the gate passes the "
            f"context by keyword: declare it as '{PARAMETER}' (or '*, {PARAMETER}'), or take no argument"
        )
    for parameter in parameters.values():
        if parameter.name != PARAMETER and parameter.default is parameter.empty and parameter.kind in _FILLED:
            hint = f"; rename it '{PARAMETER}' to receive the attempt's CheckContext" if declared is None else ""
            raise ConfigError(
                f"the health check {name} requires the parameter '{parameter.name}', which the gate never fills: it "
                f"calls a check with no argument, or with the keyword '{PARAMETER}' when the check declares one{hint}"
            )
    return declared is not None
