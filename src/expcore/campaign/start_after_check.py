# Provenance: new
"""A start that failed right after a post-run check found the platform's links failing costs its run no attempt.

**What it covers.** A platform's post-run check can find something that leaves the platform unfit for the next start:
a provider link that does not transfer, or one the check could not even test (the Onedata link check, O35). The check
says so by noting `prerun.NEXT_START_AT_RISK` in the try's note; it is noted for nothing else. Seen live in a running
campaign: a post-run check failed a provider pair as untested, and the run's next attempt, a minute or so later,
failed before the agent's first turn because the start state it induces on the federation did not verify in time. It
was charged, so one more such failure would have exhausted the run for a failure the run never caused.

**The rule.** The run's next attempt in this process after an attempt whose post-run check failed (not deferred) with
`NEXT_START_AT_RISK` noted is the one it covers (`RiskedStarts`). It is set aside uncharged when, and only when, it
ends as a charged infrastructure failure before the agent's first turn (`model_turns == 0`), of a kind that is neither
the core's own (`expcore.errors.CORE_KINDS`: the model's endpoint and key, the budget, the sandbox, the tool server, a
kill, the ceiling, integrity, verification) nor the campaign's health or identity kind: what is left is a failure of the
start a study or a platform makes on the platform (inducing the run's start state, a platform's own start), which is
what a failing link spoils. The attempt is then kept as one an outage overlapped (`outage_kind` set to the campaign's
health kind, `outage_discarded`), as a post-run failure another attempt left already is (`expcore.campaign.attempt`).

**Why it is narrow.**
- *Once.* The mark is taken by the run's next attempt whatever it ends in: a pre-run check that finds an outage, a
  scored result, or this start. A start covered by the rule never marks the run again, even when its own post-run check
  fails on a link too, so of two consecutive attempts at least one is charged and a run can never loop uncharged on a
  link that keeps failing.
- *Only after a link.* A post-run check that failed on anything else (a space, an injection), or was deferred, marks
  nothing. A platform that notes nothing leaves every charge as it was.
- *Only before the agent's first turn.* The agent never met the run, so nothing it did is in question; a failure after
  a turn is charged as always.
- *In this process.* The mark lives in memory: a restart in between forgets it and the start is charged, as before.
  The decision itself is carried on the result (`outage_kind`), in its marker and its row, so a result committed from
  its marker after a restart is classified the same (`attempts.classify`), and no record field is added.
"""

import dataclasses
import threading

from expcore.campaign.attempts import classify
from expcore.campaign.markers import AttemptResult
from expcore.errors import CORE_KINDS

EVENT = "start_spared_after_link_failure"  # the store's event for a start this rule set aside


class RiskedStarts:
    """The runs whose next attempt follows a post-run check that put it at risk; safe from several worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: set[str] = set()

    def mark(self, run_key: str) -> None:
        """The run's latest attempt ended with a failed post-run check that noted the next start at risk."""
        with self._lock:
            self._runs.add(run_key)

    def take(self, run_key: str) -> bool:
        """Whether the run's attempt now beginning follows such a check; the mark is used up either way."""
        with self._lock:
            found = run_key in self._runs
            self._runs.discard(run_key)
            return found


def spared(result: AttemptResult, health_kind: str, identity_kind: str) -> AttemptResult | None:
    """The result set aside uncharged, as an outage overlapped it, when the rule covers it (see the module docstring),
    else None: a charged infrastructure failure before the agent's first turn, of a kind that is a study's or a
    platform's start and not the core's own, the health kind or the identity kind."""
    if result.model_turns != 0 or result.outage_kind is not None:
        return None
    state, charged, _ = classify(result, result.treatment_kinds, result.treatment_targets)
    kind = result.outcome.infra_kind
    if state != "infra_fail" or not charged or kind is None:
        return None
    if kind in CORE_KINDS or kind in (health_kind, identity_kind):
        return None
    return dataclasses.replace(result, outage_kind=health_kind)
