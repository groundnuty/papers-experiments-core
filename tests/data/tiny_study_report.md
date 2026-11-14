# Report: tiny-study

## Provenance

- Preregistration SHA-256: `365aa74b9a821b15e50307253dd69d5f9c424622ed9b42d7de443b79d9d182f9`
- Factor schema SHA-256: `dccda65c82f6030717ea12ca7a0c9654bda6e9073e04e93a0663bf6d313796e5`
- Code: (not pinned)
- Core version: (not pinned)
- Scorers: tiny `1`
- Runs: 22 planned, 22 analysed, 0 exhausted (charged infrastructure attempts reached the limit; no scored attempt), 0 unfinished, 0 left out of the analysis
- Partial analysis: no

## Preregistered contrasts (Holm family of 2, alpha = 0.05)

| Contrast | Arms (A vs B, A - B) | Outcome | Estimate | 95% interval | Margin | 90% equivalence interval | p | Holm p | Conclusion | Clusters | Pairs used | Treatment attempts | Tasks dropped | Min. detectable effect |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| extended-vs-baseline | extended vs baseline | accuracy | 0.375 | [0.25, 0.5] | 0.2 | [0.25, 0.5] | 0.5000 | 1.0000 | not_detected | 2 (dropped 0) | 6 | - | dune | n/a |
| relay-vs-baseline | relay-down vs baseline | accuracy | 0.125 | [-0.25, 0.5] | - | - | 1.0000 | 1.0000 | not_detected | 2 (dropped 0) | 8 | scored | none | n/a |

Conclusions: `difference` (significant), `trivial_difference` (significant, but inside the equivalence
margin), `equivalent` (inside the margin, not significant), `not_detected` (anything else; failing to
detect a difference is not evidence that there is none), `untestable` (no usable pair, so nothing was
tested and the contrast is not evidence of anything, least of all of no effect; in the Holm family it
counts as p = 1).

Margin: the preregistered equivalence margin, plus or minus; `-` when the contrast has none, and then it
can never conclude `equivalent` or `trivial_difference`.
90% equivalence interval: the cluster-bootstrap interval at 1 - 2 alpha. The contrast is inside the
margin when this interval lies within plus or minus the margin, which `equivalent` and `trivial_difference`
need; n/a when no pair was used, `-` without a margin.

Pairs used: pairs with the outcome on both sides; each missing pair is counted by its cause in the pairs
table of its section ("Missing pairs by cause" says what each cause counts). For an untestable contrast
the cell gives those causes with their counts.

Treatment attempts: how the contrast's treatment attempts (runs that ended in an infrastructure failure their
condition declares as its treatment) entered it. `scored`: the preregistration says the scorer returns values
for them. `excluded`: the preregistration leaves every pair with one out of the contrast, and the count says
how many; the contrast then says nothing about the outage itself. `not declared`: pairs were lost because a
treatment attempt's outcome is None, which only an exploratory contrast may do. `-`: nothing declared,
nothing lost.

Min. detectable effect: the smallest effect this contrast's number of clusters could detect with the
preregistered power; n/a when it cannot be found (no spread between clusters, or too few clusters);
`-` for an untestable contrast, which has none.

### Pairs of the family's contrasts

| Contrast | Pairs used | Treatment outcome None | Treatment excluded | Exhausted run | Unpaired or left out | Outcome None (other) |
|---|---|---|---|---|---|---|
| extended-vs-baseline | 6 | 0 | 0 | 0 | 2 | 0 |
| relay-vs-baseline | 8 | 0 | 0 | 0 | 0 | 0 |

## Exploratory contrasts (outside the Holm family)

| Contrast | Arms (A vs B, A - B) | Outcome | Estimate | 95% interval | Margin | 90% equivalence interval | p | Holm p | Conclusion | Clusters | Pairs used | Treatment attempts | Tasks dropped | Min. detectable effect |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| tools-effect-on-rounds | factor tools: extended vs basic | rounds | -1.5 | [-2, -1] | - | - | 0.5000 | exploratory | not_detected (exploratory) | 2 (dropped 0) | 6 | - | dune | n/a |

Nothing adjusts these for multiplicity; each is judged by its raw p-value at alpha.

### Pairs of the exploratory contrasts

| Contrast | Pairs used | Treatment outcome None | Treatment excluded | Exhausted run | Unpaired or left out | Outcome None (other) |
|---|---|---|---|---|---|---|
| tools-effect-on-rounds | 6 | 0 | 0 | 0 | 10 | 0 |

## Missing pairs by cause

A pair is two runs that differ only in the contrast's arm. Each pair is used or is missing for one cause, counted
in the pairs tables above:

- *Treatment outcome None*: the outcome is None on a treatment attempt, whatever became of the other planned run.
- *Treatment excluded*: a run is a treatment attempt and the preregistration declares `treatment_outcomes = "excluded"` for the contrast; left out whatever its outcome.
- *Exhausted run*: the run of either arm used all its attempts on infrastructure failures.
- *Unpaired or left out*: the other run is unfinished, left out of a partial analysis, or not in the plan (a task one arm does not run).
- *Outcome None (other)*: both runs analysed, and the outcome is None on an ordinary run only.

### Runs of each arm

Each arm's own planned runs on the contrast's tasks (S14), whatever became of their partners: a pair lost to one
run's None is one run lost here, in the arm that lost it. Finished: the runs with an analysed attempt, the
population the contrast's outcome is defined over, and Finish rate their share of the planned runs (n/a for an
arm with none planned). Measured: the finished runs whose outcome is known. The other columns give the runs
lost by cause: the outcome None on a treatment attempt or on another run, or no analysed attempt (exhausted,
unfinished, or left out of a partial analysis). An arm that finished fewer of its runs is compared on fewer, and
perhaps other, runs than its partner.

| Contrast | Arm | Planned | Finished | Finish rate | Measured | Treatment outcome None | Outcome None | Exhausted | Unfinished | Left out |
|---|---|---|---|---|---|---|---|---|---|---|
| extended-vs-baseline | extended | 6 | 6 | 100% | 6 | 0 | 0 | 0 | 0 | 0 |
| extended-vs-baseline | baseline | 6 | 6 | 100% | 6 | 0 | 0 | 0 | 0 | 0 |
| relay-vs-baseline | relay-down | 8 | 8 | 100% | 8 | 0 | 0 | 0 | 0 | 0 |
| relay-vs-baseline | baseline | 8 | 8 | 100% | 8 | 0 | 0 | 0 | 0 | 0 |
| tools-effect-on-rounds | extended | 6 | 6 | 100% | 6 | 0 | 0 | 0 | 0 | 0 |
| tools-effect-on-rounds | basic | 6 | 6 | 100% | 6 | 0 | 0 | 0 | 0 | 0 |

## Conditions

Planned runs are analysed, exhausted (charged infrastructure attempts reached the limit; no scored attempt),
unfinished, or left out of a partial analysis. Attempts set aside are the condition's set-aside
infrastructure attempts, as in the table below. Median dispatch position is the median place, in the
campaign's dispatch sequence, of each analysed run's scored attempt, where it has one (a database older than
the dispatch column has none); a condition whose runs came late has a higher one.

| Condition | Planned | Analysed | Exhausted | Unfinished | Left out | Attempts set aside | Median dispatch position |
|---|---|---|---|---|---|---|---|
| baseline | 8 | 8 | 0 | 0 | 0 | 0 | 13 |
| extended | 6 | 6 | 0 | 0 | 0 | 0 | 6.5 |
| relay-down | 8 | 8 | 0 | 0 | 0 | 0 | 14.5 |

### Infrastructure failures by condition and kind

| Condition | Kind | Declared treatment | Set aside (all runs) | Scored as treatment | Left out |
|---|---|---|---|---|---|
| baseline | none | no | 0 | 0 | 0 |
| extended | none | no | 0 | 0 | 0 |
| relay-down | knowledge | yes | 0 | 2 | 0 |

- *Kind* `none`: the condition met no infrastructure failure and declares no treatment kind; every count is 0.
- *Set aside*: attempts that ended as an infrastructure failure of a kind the condition does not declare as treatment, or before the model's first turn, or that were discarded for an outage. Counted over every run of the campaign whatever its status (an exhausted run's last attempt included), except runs a partial analysis leaves out.
- *Scored as treatment*: attempts that ended as an infrastructure failure of a kind the condition declares as treatment, after the model's first turn; each is its run's final, scored attempt. Runs a partial analysis leaves out are not included.
- *Left out*: the set-aside and treatment attempts of runs a partial analysis leaves out, and only those.
- *Kind* `budget_hold`: no failure of the infrastructure, but the campaign's budget: a request was not sent because the top-up it needed would have passed the ceiling. Set aside like a failure, and never a treatment.

### Budget holds by condition and model

| Condition | Model | Attempts set aside by a budget hold |
|---|---|---|
| baseline | alpha-stub | 0 |
| baseline | beta-stub | 0 |
| extended | alpha-stub | 0 |
| extended | beta-stub | 0 |
| relay-down | alpha-stub | 0 |
| relay-down | beta-stub | 0 |

Attempts set aside by a budget hold: attempts that ended because a request the token cap could not account for
(a resend after a failure that may have been billed, or a request after a reply without usage) needed a top-up
the campaign's budget could not cover, so it was not sent. They are among the attempts set aside above, and are
counted by the kind of their own outcome: one an outage overlapped, which the table above counts under the
outage's kind, is counted here too. They are counted over every run of the campaign, a run a partial analysis
leaves out included. A model with more of them had its runs stopped by the budget more often than the others.

## Attempts left out of the analysis

None.
