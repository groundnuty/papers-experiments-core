# Provenance: new
"""The vocabulary of attempts: their states, how each is charged, which are treatment, and the store's refusals.

Charging (C03). An attempt counts against the attempt limit only when the infrastructure failed and the cause was not
the key's (`UNCHARGED_KINDS`) and no outage overlapped it. Interrupted, outage-discarded and harness-error attempts are
never charged. The charge is decided once, when the attempt finishes, and stored in the row.

Treatment. A condition may declare infrastructure kinds that are its treatment (`FactorSchema.treatment_kinds`). An
attempt that ends as infrastructure of such a kind, under that condition, is a scored final attempt: its state is
'done', its run is done, `treatment` is set, and it is neither charged nor run again. Without the declaration, running
again until the run looks healthy would remove the very fault the condition is meant to measure. Two things are never
treatment: a kind the registry marks as not treatable (the schema refuses to declare one), and a failure before the
agent's first turn, which happened before the agent met the condition (`Outcome.is_treatment`: at least one completed
model turn is needed). Both are set aside and run again as any other infrastructure failure. The first-turn rule is
the record's schema version 2; a result from a version-1 record or from a schema-1 marker is decided without it.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import NamedTuple

from expcore.campaign.markers import AttemptResult

STATES = ("running", "done", "infra_fail", "outage_discarded", "interrupted", "harness_error")
RUN_STATUSES = ("pending", "done", "infra_exhausted", "harness_error")
# An infrastructure failure the key caused, not the attempt's run: it holds the model but costs the run no attempt.
UNCHARGED_KINDS = frozenset({"key_rejected"})
# How the lease an attempt ran under was obtained. A refused lease (every key quarantined, a timeout) starts no attempt
# and so has no row: it is in the lease wait log and the event log. `none`: the run's condition needs no lease.
LEASE_OUTCOMES = ("granted", "none")


class AttemptRefused(RuntimeError):
    """A transaction refused to start or finish an attempt because the run or the attempt is not in the right state."""


class TreatmentKindsMismatch(AttemptRefused):
    """The record of an attempt carries other treatment kinds than its run's condition declares.

    The attempt stays running, so recovery treats it like any interrupted attempt.
    """


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
    """A started attempt: the store's id for it, its number among the run's charged attempts (1 for the first), and its
    place in the campaign's dispatch sequence."""

    id: int
    number: int
    dispatch_seq: int = 0


class Classified(NamedTuple):
    """How an attempt ends: its state, whether it is charged, and whether it is a scored treatment attempt."""

    state: str
    charged: bool
    treatment: bool = False


def classify(result: AttemptResult, treatment_kinds: Collection[str] = ()) -> Classified:
    """Return the state an attempt ends in, whether it is charged and whether it is treatment, by cause (C01, C03).

    An infrastructure outcome is never scored as the agent's: it is set aside and run again, unless its kind is one
    the run's condition declares as its treatment and the model completed a turn, when it is a scored final attempt
    (see the module docstring). A
    crash in our own code is set aside and not charged. Any other outcome, a cap or an answer, is the agent's own and
    ends the run. An outage the caller flags on an attempt that did not end as infrastructure sets its result aside.
    """
    outcome = result.outcome
    if outcome.is_treatment(treatment_kinds, result.model_turns, first_turn_rule=result.first_turn_rule):
        return Classified("done", False, True)
    if result.outage_kind is not None:
        return Classified("outage_discarded", False)
    if outcome.name == "infrastructure":
        return Classified("infra_fail", outcome.infra_kind not in UNCHARGED_KINDS)
    if outcome.name == "harness_defect":
        return Classified("harness_error", False)
    return Classified("done", False)
