# Provenance: new
"""The vocabulary of attempts: their states, how each is charged, which are treatment, and the store's refusals.

Charging (C03). An attempt counts against the attempt limit only when the infrastructure failed and the cause was not
the key's (`UNCHARGED_KINDS`) and no outage overlapped it. Interrupted, outage-discarded and harness-error attempts are
never charged. The charge is decided once, when the attempt finishes, and stored in the row.

Treatment. A condition may declare infrastructure kinds that are its treatment (`FactorSchema.treatment_kinds`). An
attempt that ends as infrastructure of such a kind, under that condition, is a scored final attempt: its state is
'done', its run is done, `treatment` is set, and it is neither charged nor run again. Without the declaration, running
again until the run looks healthy would remove the very fault the condition is meant to measure.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import NamedTuple

from expcore.campaign.markers import AttemptResult

STATES = ("running", "done", "infra_fail", "outage_discarded", "interrupted", "harness_error")
RUN_STATUSES = ("pending", "done", "infra_exhausted", "harness_error")
# An infrastructure failure the key caused, not the attempt's run: it holds the model but costs the run no attempt.
UNCHARGED_KINDS = frozenset({"key_rejected"})


class AttemptRefused(RuntimeError):
    """A transaction refused to start or finish an attempt because the run or the attempt is not in the right state."""


class ResumeRefused(RuntimeError):
    """A resume would continue a different experiment than the one the store records."""


class PinMismatch(ResumeRefused):
    """A campaign-level fact differs from the value pinned at the first start."""

    def __init__(self, key: str, pinned: str, now: str):
        super().__init__(
            f"{key} is pinned to {pinned} in this campaign, but is now {now}: refusing to resume with a different one "
            "(re-pin only with the explicit operator flag)"
        )
        self.key, self.pinned, self.now = key, pinned, now


class SchemaMismatch(ResumeRefused):
    """The database was made for other factors than the schema given."""


@dataclass(frozen=True)
class BegunAttempt:
    """A started attempt: the store's id for it, and its number among the run's charged attempts (1 for the first)."""

    id: int
    number: int


class Classified(NamedTuple):
    """How an attempt ends: its state, whether it is charged, and whether it is a scored treatment attempt."""

    state: str
    charged: bool
    treatment: bool = False


def classify(result: AttemptResult, treatment_kinds: Collection[str] = ()) -> Classified:
    """Return the state an attempt ends in, whether it is charged and whether it is treatment, by cause (C01, C03).

    An infrastructure outcome is never scored as the agent's: it is set aside and run again, unless its kind is one
    the run's condition declares as its treatment, when it is a scored final attempt (see the module docstring). A
    crash in our own code is set aside and not charged. Any other outcome, a cap or an answer, is the agent's own and
    ends the run. An outage the caller flags on an attempt that did not end as infrastructure sets its result aside.
    """
    outcome = result.outcome
    if outcome.name == "infrastructure" and outcome.infra_kind in treatment_kinds:
        return Classified("done", False, True)
    if result.outage_kind is not None:
        return Classified("outage_discarded", False)
    if outcome.name == "infrastructure":
        return Classified("infra_fail", outcome.infra_kind not in UNCHARGED_KINDS)
    if outcome.name == "harness_defect":
        return Classified("harness_error", False)
    return Classified("done", False)
