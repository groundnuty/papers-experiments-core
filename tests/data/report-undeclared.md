# Report: a-study

## Provenance

- Preregistration SHA-256: `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`
- Factor schema SHA-256: `4ebc2eee9521897bdbea583f9de6016138b0ff515a14873467c7d18d17fb75d0`
- Code: commit `cccccccccccccccccccccccccccccccccccccccc`, tree `dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd` (clean)
- Core version: 1.2.3
- Scorers: n/a
- Runs: 2 planned, 2 analysed, 0 exhausted (charged infrastructure attempts reached the limit; no scored attempt), 0 unfinished, 0 left out of the analysis
- Partial analysis: no

## Preregistered contrasts (Holm family of 1, alpha = 0.05)

| Contrast | Arms (A vs B, A - B) | Outcome | Estimate | 95% interval | Margin | 90% equivalence interval | p | Holm p | Conclusion | Clusters | Pairs used | Treatment attempts | Tasks dropped | Min. detectable effect |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| score-a-vs-b | cond-a vs cond-b | score | 0.205 | [0.14, 0.2925] | 0.3 | [0.1449, 0.2704] | 0.0078 | 0.0078 | trivial_difference | 8 (dropped 0) | 16 | - | none | 0.1305 |

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
| score-a-vs-b | 16 | 0 | 0 | 0 | 0 | 0 |

## Exploratory contrasts (outside the Holm family)

| Contrast | Arms (A vs B, A - B) | Outcome | Estimate | 95% interval | Margin | 90% equivalence interval | p | Holm p | Conclusion | Clusters | Pairs used | Treatment attempts | Tasks dropped | Min. detectable effect |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| spend-per-win | cond-a vs cond-b | ln(spend/wins) | 0.6296 | [0.304, 0.9391] | - | - | 0.0234 | exploratory | difference (exploratory) | 8 (dropped 0) | 16 | - | none | 0.6624 |

Nothing adjusts these for multiplicity; each is judged by its raw p-value at alpha.

### Pairs of the exploratory contrasts

| Contrast | Pairs used | Treatment outcome None | Treatment excluded | Exhausted run | Unpaired or left out | Outcome None (other) | Numerator None at denominator 0 | Zero denominator | Zero numerator |
|---|---|---|---|---|---|---|---|---|---|
| spend-per-win | 16 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Ratio contrasts

| Contrast | Numerator | Denominator | Estimation unit | Independent unit | Cells | Zero total | Ratio (A / B) | Ratio interval |
|---|---|---|---|---|---|---|---|---|
| spend-per-win | spend | wins | group | group | 8 | exclude | 1.877 | [1.355, 2.558] |

A ratio contrast compares arm A's numerator per unit of denominator with arm B's, such as the cost per correct
result. Estimation unit is the cell whose sums give one log ratio: a task attribute (each value of it, its models
pooled) or the model and the attribute (each model's cell of a value). Independent unit is the attribute whose
values are the units of the test, of the resampling and of the minimum detectable effect, and what Clusters
counts: the cells of one value share its response to the treatment, so they are never counted as independent,
and a value's log ratio is the mean of its cells', each model weighing the same. Cells is the number of cells
with a log ratio, then those lost by cause (no pair, or a zero total the rule excludes); a value is dropped only
when all its cells are lost. In each cell, each arm's numerator and denominator are summed over its used pairs,
and a run whose denominator is 0, a failure, still adds its numerator. The cell's value is the natural log of A's
ratio over B's, ln((N_A / D_A) / (N_B / D_B)). The estimate, the intervals, the margin and the minimum detectable
effect in the contrast tables are on that log scale, and the test is the signed-rank test of the values' log
ratios. Ratio (A / B) is exp of the estimate, a geometric mean over the values of A's ratio over B's, and Ratio
interval is exp of the interval. Zero total is the preregistered rule for a cell whose numerator or denominator
sums to 0 in either arm, whose log ratio is undefined or infinite:
`exclude` (the cell is excluded from the contrast, and its pairs are counted by cause in the pairs tables)
or `continuity correction c` (c is added to both sums of both arms in every cell, which keeps every cell
and moves each log ratio most where its sums are small).

## Missing pairs by cause

A pair is two runs that differ only in the contrast's arm. Each pair is used or is missing for one cause, counted
in the pairs tables above:

- *Treatment outcome None*: the outcome is None on a treatment attempt, whatever became of the other planned run.
- *Treatment excluded*: a run is a treatment attempt and the preregistration declares `treatment_outcomes = "excluded"` for the contrast; left out whatever its outcome.
- *Exhausted run*: the run of either arm used all its attempts on infrastructure failures.
- *Unpaired or left out*: the other run is unfinished, left out of a partial analysis, or not in the plan (a task one arm does not run).
- *Outcome None (other)*: both runs analysed, and the outcome is None on an ordinary run only.
- *Numerator None at denominator 0*: a run of a ratio contrast has no numerator and a denominator of 0, a failed run whose cost was not measured; a confirmatory contrast with any is refused.
- *Zero denominator*: the pair is in a cell whose denominator sums to 0 in either arm, which the preregistration excludes (`zero_denominator = "exclude"`).
- *Zero numerator*: the pair is in a cell whose numerator sums to 0 in either arm, excluded by the same rule.
- `-`: a cause that does not apply: a contrast of one outcome has none of the three above.

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
| score-a-vs-b | cond-a | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| score-a-vs-b | cond-b | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| spend-per-win | cond-a | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| spend-per-win | cond-b | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |

## Conditions

Planned runs are analysed, exhausted (charged infrastructure attempts reached the limit; no scored attempt),
unfinished, or left out of a partial analysis. Attempts set aside are the condition's set-aside
infrastructure attempts, as in the table below. Median dispatch position is the median place, in the
campaign's dispatch sequence, of each analysed run's scored attempt, where it has one (a database older than
the dispatch column has none); a condition whose runs came late has a higher one.

| Condition | Planned | Analysed | Exhausted | Unfinished | Left out | Attempts set aside | Median dispatch position |
|---|---|---|---|---|---|---|---|
| cond-a | 1 | 1 | 0 | 0 | 0 | 0 | 1 |
| cond-b | 1 | 1 | 0 | 0 | 0 | 0 | 2 |

### Infrastructure failures by condition and kind

| Condition | Kind | Declared treatment | Set aside (all runs) | Scored as treatment | Left out |
|---|---|---|---|---|---|
| cond-a | none | no | 0 | 0 | 0 |
| cond-b | none | no | 0 | 0 | 0 |

- *Kind* `none`: the condition met no infrastructure failure and declares no treatment kind; every count is 0.
- *Set aside*: attempts that ended as an infrastructure failure of a kind the condition does not declare as treatment, or before the model's first turn, or that were discarded for an outage. Counted over every run of the campaign whatever its status (an exhausted run's last attempt included), except runs a partial analysis leaves out.
- *Scored as treatment*: attempts that ended as an infrastructure failure of a kind the condition declares as treatment, after the model's first turn; each is its run's final, scored attempt. Runs a partial analysis leaves out are not included.
- *Left out*: the set-aside and treatment attempts of runs a partial analysis leaves out, and only those.
- *Kind* `budget_hold`: no failure of the infrastructure, but the campaign's budget: a request was not sent because the top-up it needed would have passed the ceiling. Set aside like a failure, and never a treatment.

### Budget holds by condition and model

| Condition | Model | Attempts set aside by a budget hold |
|---|---|---|
| cond-a | model-a | 0 |
| cond-b | model-a | 0 |

Attempts set aside by a budget hold: attempts that ended because a request the token cap could not account for
(a resend after a failure that may have been billed, or a request after a reply without usage) needed a top-up
the campaign's budget could not cover, so it was not sent. They are among the attempts set aside above, and are
counted by the kind of their own outcome: one an outage overlapped, which the table above counts under the
outage's kind, is counted here too. They are counted over every run of the campaign, a run a partial analysis
leaves out included. A model with more of them had its runs stopped by the budget more often than the others.

## Attempts left out of the analysis

None.
score-a-vs-b: estimate 0.205, p 0.0078, Holm p 0.0078, trivial_difference
spend-per-win: estimate 0.6296 (ln ratio), p 0.0234, Holm p exploratory, difference (exploratory)
