# Provenance: new
"""A start, or a pre-run outage, right after a failed post-run check costs its run no attempt, once.

**Why.** A failed post-run health check is the platform saying it was unhealthy at the end of the run's previous
attempt. The next attempt starts on that platform, and an infrastructure failure of it before the agent's first turn
cannot be the agent's doing: the agent never met the run. Seen live in a running campaign: a post-run check failed a
provider pair as untested, and the run's next attempt, a minute or so later, failed before the agent's first turn
because the start state it induces on the federation did not verify in time. It was charged, so one more such failure
would have exhausted the run for a fault the run never caused. Seen live again: a run's second attempt failed its
post-run link check, and its third attempt's pre-run check failed at once (a failed heal that only an operator can
mend), which was charged as the outage's one attempt, and the run was exhausted. Nothing of that attempt ran, and the
fault was the one the previous attempt had already left.

**The rule.** After an attempt whose post-run check failed and was not deferred (`RiskedStarts.mark`), the run's next
attempt in this process is covered. It is set aside uncharged in either of two cases. (1) The run's pre-run health
check finds an outage (`AttemptRunner._charge_outage`): the attempt is the outage's one, nothing was run, and it is set
aside as it is, `spared` with the same decision, its detail led by `REASON`. The gate is told beforehand
(`HealthGate.before_run(covered=...)`, with `RiskedStarts.marked`, which takes nothing), so the outage's record and the
`health_gate_closed` event say no run was charged, as the run's own row does, and the report's hold section agrees.
(2) The attempt starts, and the executor's own result is a charged infrastructure failure before the agent's first
turn, whatever its kind (`turnless`, `spared`). With a record, that is one with no completed turn
(`model_turns == 0`). With none (the executor raised, and `model_turns` is then a
default, not a count) it is shown two ways. When a call log is kept (a campaign with a budget), the log alone decides,
as it does for spend: the usage must be known to be no request (`turn_usage == ()`: the complete log held none,
`no_request`, or a first-turn refusal sent none). When none is kept (no budget: a lease of a free model, whose grant is
0, has none), the row's call log and usage are NULL whatever the executor did, so they prove nothing; the attempt's own
backend, built for it alone, must then say it started no request (`no_request.counted_none`: `requests_sent` is the
whole number 0, every resend counted; a backend that keeps no count confirms nothing). The incident's start was of
this second kind: a lease of a free model, its row's usage NULL, its induction failing before any model call. An
executor that raised with a request in its log, or whose backend counted one, or kept no count, is charged as
before. The
attempt is then kept as one an outage overlapped (`outage_kind` set to the campaign's health kind, `outage_discarded`),
as a post-run failure another attempt left already is (`expcore.campaign.attempt`), and its outcome's detail says why
it was not charged, first (`REASON`): the row's `detail` and the marker carry it. An event of kind `EVENT` is written
in the result's transaction too.

**The bound: at most one attempt spared per failed post-run check, and never two in a row.**
- *Once.* The mark is taken by the run's next attempt whatever it ends in: a pre-run check that finds an outage, a
  scored result, or a start failure. A start set aside by this rule never marks the run again, even when its own
  post-run check fails too, so the start failure after it is charged unless something else already spares it (an
  outage the gate was closed for, a platform's hold): this rule adds at most one uncharged attempt after each failed
  check of an attempt it did not itself spare, so on a platform that keeps failing its checks the run still spends
  its attempts and reaches its limit. A pre-run outage set aside this way never ran a post-run check, so it marks
  nothing either: the next pre-run outage, and the next start failure, are charged, whichever of the two the mark was
  spent on.
- *The gate stays closed.* A pre-run outage closes the health gate (`HealthGate._close`), whether it is spared or not,
  and a run that has not passed its pre-run check yet is held until the dispatcher's own check passes, so sparing it
  opens no loop of free attempts of its own. What the rule leaves as it was: a post-run check that charges no one
  (`Health.charge`), or that fails while the gate is closed, discards its attempt uncharged and still marks the run,
  so a platform that alternates such answers with chargeable pre-run failures can make attempts free, as it could with
  the start rule; and a worker already past its own pre-run check when another run's check closes the gate still starts.
  The spare event is written in the result's transaction, so a result committed from its marker after a kill has the
  row's detail and no event.
- *Only after a failed check.* A post-run check that passed, or was deferred (it verified nothing and found nothing
  failing), marks nothing.
- *Only before the agent's first turn.* A failure after a completed turn is charged as always.
- *Only the executor's own failure.* The rule looks at the result before the post-run check and the instruments judge
  it, so this attempt's own post-run failure, an instrument's refusal and the endpoint's identity are charged as before.
  A pre-run outage with no failed post-run check before it is charged as the outage's one attempt, as always.
- *In this process.* The mark lives in memory: a restart in between forgets it and the start, or the pre-run outage,
  is charged, as before.
  The decision itself is carried on the result (`outage_kind`, `detail`), in its marker and its row, so a result
  committed from its marker after a restart is classified the same (`attempts.classify`), and no field is added to the
  marker, the store or the record.
"""

import dataclasses
import threading
from typing import Any

from expcore.campaign.attempts import classify
from expcore.campaign.markers import AttemptResult
from expcore.campaign.no_request import counted_none

EVENT = "start_spared_after_failed_check"  # the store's event for a start this rule set aside
REASON = "set aside uncharged: the run's previous attempt's post-run health check failed"  # the detail's lead


class RiskedStarts:
    """The runs whose next attempt follows a failed post-run check; safe from several worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: set[str] = set()

    def mark(self, run_key: str) -> None:
        """The run's latest attempt ended with a failed, not deferred, post-run check."""
        with self._lock:
            self._runs.add(run_key)

    def marked(self, run_key: str) -> bool:
        """Whether the run's next attempt follows such a check, with the mark left as it is (a try may still be left
        unstarted, and then the run's next attempt is the one that follows the check)."""
        with self._lock:
            return run_key in self._runs

    def take(self, run_key: str) -> bool:
        """Whether the run's attempt now beginning follows such a check; the mark is used up either way."""
        with self._lock:
            found = run_key in self._runs
            self._runs.discard(run_key)
            return found


def turnless(result: AttemptResult, *, recorded: bool, logged: bool, backend: Any) -> bool:
    """Whether the attempt is shown to have taken no turn of the agent (see the module docstring). `recorded`: the
    executor returned a record, whose `model_turns` counts its completed turns; `logged`: the attempt kept a call log
    (a budget), which then alone decides for an executor that raised; `backend`: the attempt's own backend, whose
    `requests_sent` decides when no log is kept."""
    if recorded:
        return result.model_turns == 0
    if result.turn_usage == ():
        return True
    return not logged and counted_none(backend)


def spared(result: AttemptResult, health_kind: str) -> AttemptResult | None:
    """The executor's result set aside uncharged, as an outage overlapped it, with `REASON` leading its detail, when
    it is a charged infrastructure failure (the caller has shown it took no turn, `turnless`); else None."""
    if result.outage_kind is not None:
        return None
    state, charged, _ = classify(result, result.treatment_kinds, result.treatment_targets)
    if state != "infra_fail" or not charged:
        return None
    outcome = dataclasses.replace(result.outcome, detail=f"{REASON}; {result.outcome.detail}")
    return dataclasses.replace(result, outcome=outcome, outage_kind=health_kind)
