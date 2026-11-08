# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:137-148, 430-451
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:277-296, 805-822
"""How an attempt ends: a closed vocabulary, validated when an outcome is built, and the harness-defect hash.

The vocabulary is closed so a typo never reaches a record and the analysis can enumerate every ending:

- `finished`: the model answered without a tool call, or a call to a terminal tool succeeded.
- `turn_cap`, `time_cap`, `token_cap`, `output_cap`: a cap ended it. These are the agent's own outcomes.
- `malformed_end`: the model sent malformed tool-call arguments and the run's policy ends the attempt (L01).
- `error`: the model or endpoint failed in a way that is the attempt's own (a context overflow, a plain 400).
- `infrastructure`: the endpoint, the sandbox or a tool server failed; carries the kind from `expcore.errors`
  unchanged (C02). The attempt is set aside and re-run, never scored.
- `harness_defect`: a crash in harness or tool code on valid input (L08); carries a line-free stack hash. Set aside
  and re-run after the fix, never scored as the agent's failure.
"""

import hashlib
import json
import traceback
from dataclasses import dataclass
from pathlib import Path

from expcore.errors import infra_kinds

OUTCOMES = (
    "finished",
    "turn_cap",
    "time_cap",
    "token_cap",
    "output_cap",
    "malformed_end",
    "harness_defect",
    "error",
    "infrastructure",
)
CAP_OUTCOMES = frozenset({"turn_cap", "time_cap", "token_cap", "output_cap"})
SET_ASIDE_OUTCOMES = frozenset({"infrastructure", "harness_defect"})
DETAIL_LIMIT = 500
# Frames are named relative to the directory that holds the `expcore` package, so one defect has one hash on every
# machine; frames of other code are named by file only.
_PACKAGE_PARENT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class HarnessDefect:
    """The identity of a crash in our own code: the exception class and the hash of its line-free stack."""

    exception: str
    stack_sha256: str


@dataclass(frozen=True)
class Outcome:
    """One ending from the closed vocabulary, with the field its kind requires and no other."""

    name: str
    detail: str = ""
    infra_kind: str | None = None
    error_type: str | None = None
    defect: HarnessDefect | None = None

    def __post_init__(self) -> None:
        """Refuse an unknown name, a missing or stray kind, error type or defect, and shorten the detail line."""
        if self.name not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.name!r}; the outcomes are: {', '.join(OUTCOMES)}")
        if (self.name == "infrastructure") != (self.infra_kind is not None):
            raise ValueError("an infrastructure outcome carries an infra_kind, and no other outcome does")
        if self.infra_kind is not None and self.infra_kind not in infra_kinds():
            raise ValueError(
                f"unknown infrastructure kind {self.infra_kind!r}; known kinds: {', '.join(infra_kinds())}"
            )
        if (self.name == "error") != (self.error_type is not None):
            raise ValueError("an error outcome carries an error_type, and no other outcome does")
        if (self.name == "harness_defect") != (self.defect is not None):
            raise ValueError("a harness_defect outcome carries its defect, and no other outcome does")
        object.__setattr__(self, "detail", self.detail[:DETAIL_LIMIT])

    @property
    def is_cap(self) -> bool:
        """Tell whether a cap ended the attempt."""
        return self.name in CAP_OUTCOMES

    @property
    def set_aside(self) -> bool:
        """Tell whether the attempt is set aside and re-run instead of scored (infrastructure, harness defect)."""
        return self.name in SET_ASIDE_OUTCOMES


def harness_defect_of(exc: BaseException) -> HarnessDefect:
    """Identify a crash by its exception class and the (path, function) of each stack frame, without line numbers.

    Frames run from where the exception was caught to where it was raised. Leaving the line numbers out means an
    unrelated edit to the same file does not split one defect into two hashes.
    """
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        path = Path(frame.filename).resolve()
        try:
            where = path.relative_to(_PACKAGE_PARENT).as_posix()
        except ValueError:
            where = path.name
        frames.append([where, frame.name])
    name = type(exc).__name__
    stack = hashlib.sha256(json.dumps([name, frames], separators=(",", ":")).encode("utf-8")).hexdigest()
    return HarnessDefect(name, stack)
