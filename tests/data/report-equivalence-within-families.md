# Report: a-study

## Provenance

- Preregistration SHA-256: `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`
- Factor schema SHA-256: `4ebc2eee9521897bdbea583f9de6016138b0ff515a14873467c7d18d17fb75d0`
- Code: commit `cccccccccccccccccccccccccccccccccccccccc`, tree `dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd` (clean)
- Core version: 1.2.3
- Scorers: n/a
- Runs: 2 planned, 2 analysed, 0 exhausted (charged infrastructure attempts reached the limit; no scored attempt), 0 unfinished, 0 left out of the analysis
- Partial analysis: no

## Preregistered contrasts (2 Holm families, each adjusted on its own at alpha = 0.05)

### Holm family phase1 (2 contrasts)

| Contrast | Arms (A vs B, A - B) | Outcome | Estimate | 95% interval | Margin | 90% equivalence interval | p | Holm p | Conclusion | Clusters | Pairs used | Treatment attempts | Tasks dropped |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| near | cond-a vs cond-b | near | 0.005 | [-0.005641, 0.01625] | 0.2 | [-0.00375, 0.015] | 0.4531 | 0.4531 | equivalent | 8 (dropped 0) | 16 | - | none |
| wide | cond-a vs cond-b | wide | 0.0975 | [0.04869, 0.14] | 0.2 | [0.0575, 0.1363] | 0.0234 | 0.0469 | trivial_difference | 8 (dropped 0) | 16 | - | none |

Holm steps of phase1:

| Step | Contrast | p | Level | Holm p | Rejected |
|---|---|---|---|---|---|
| 1 | wide | 0.0234 | 0.025 (alpha/2) | 0.0469 | yes |
| 2 | near | 0.4531 | 0.05 (alpha/1) | 0.4531 | no |

### Holm family phase2 (1 contrast)

| Contrast | Arms (A vs B, A - B) | Outcome | Estimate | 95% interval | Margin | 90% equivalence interval | p | Holm p | Conclusion | Clusters | Pairs used | Treatment attempts | Tasks dropped |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| edge | cond-a vs cond-b | edge | 0.125 | [0.05578, 0.1988] | 0.2 | [0.05869, 0.1914] | 0.0391 | 0.0391 | difference | 8 (dropped 0) | 16 | - | none |

Holm steps of phase2:

| Step | Contrast | p | Level | Holm p | Rejected |
|---|---|---|---|---|---|
| 1 | edge | 0.0391 | 0.05 (alpha/1) | 0.0391 | yes |

Each Holm family is adjusted on its own, at alpha. Its members' p-values are taken from the smallest up, and the
k-th of a family of m is compared with alpha / (m - k + 1), its step's Level; the first that misses its level
stops the family, so it and every later member are not rejected, whatever their raw p-values. Holm p is the
member's adjusted p-value within its own family, for reading; the decision compares each raw p with its step's
level. An untestable member counts as p = 1 and keeps its family's size. A contrast is in one family at most, and
no family's size or p-values move another's decisions.

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

### Pairs of the families' contrasts

| Contrast | Pairs used | Treatment outcome None | Treatment excluded | Exhausted run | Unpaired or left out | Outcome None (other) |
|---|---|---|---|---|---|---|
| near | 16 | 0 | 0 | 0 | 0 | 0 |
| wide | 16 | 0 | 0 | 0 | 0 | 0 |
| edge | 16 | 0 | 0 | 0 | 0 | 0 |

### Equivalence family (Holm over two one-sided tests, within each Holm family)

| Contrast | Holm family | Margin | TOST p | Holm TOST p | Interval level | Adjusted interval | Equivalent |
|---|---|---|---|---|---|---|---|
| near | phase1 | 0.2 | <0.0001 | <0.0001 | 95% | [-0.008954, 0.01895] | yes |
| wide | phase1 | 0.2 | 0.0049 | 0.0049 | 90% | [0.04218, 0.1528] | yes |
| edge | phase2 | 0.2 | 0.0633 | 0.0633 | 90% | [0.04304, 0.207] | no |

The preregistration declares these contrasts' equivalence claims, and each is adjusted together with the
other claims of its own Holm family only (the Holm family column): that group is the family Holm TOST p,
Interval level and Equivalent are computed over. TOST p: the larger of the two
one-sided tests' p-values, for an effect above minus the margin and for one below the margin (for the
Wilcoxon cluster bootstrap, t-tests of the mean cluster difference on clusters - 1 degrees of freedom; n/a
with fewer than two clusters or no spread between them). Holm TOST p: Holm-adjusted over the family, for
reading; the decision compares each raw TOST p with its step's level. A member is `equivalent` only when Holm
rejects,
so for these contrasts the conclusion rests on this table, not the 90% equivalence interval beside it,
which can lie inside the margin while the family's decision is no. Adjusted interval: the test's own interval
at 1 - 2 times the level Holm compared the member with (Interval level), inside the margin exactly when the
TOST p is at most that level; an untestable member has none and counts as p = 1.

## Exploratory contrasts (outside every Holm family)

| Contrast | Arms (A vs B, A - B) | Outcome | Estimate | 95% interval | Margin | 90% equivalence interval | p | Holm p | Conclusion | Clusters | Pairs used | Treatment attempts | Tasks dropped |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| side | cond-a vs cond-b | near | 0.005 | [-0.003125, 0.01625] | - | - | 0.4531 | exploratory | not_detected (exploratory) | 8 (dropped 0) | 16 | - | none |

Nothing adjusts these for multiplicity; each is judged by its raw p-value at alpha.

### Pairs of the exploratory contrasts

| Contrast | Pairs used | Treatment outcome None | Treatment excluded | Exhausted run | Unpaired or left out | Outcome None (other) |
|---|---|---|---|---|---|---|
| side | 16 | 0 | 0 | 0 | 0 | 0 |

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
| near | cond-a | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| near | cond-b | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| wide | cond-a | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| wide | cond-b | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| edge | cond-a | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| edge | cond-b | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| side | cond-a | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |
| side | cond-b | 16 | 16 | 100% | 16 | 0 | 0 | 0 | 0 | 0 |

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
near: estimate 0.005, p 0.4531, Holm p 0.4531 (family phase1), equivalent, Holm TOST p <0.0001
wide: estimate 0.0975, p 0.0234, Holm p 0.0469 (family phase1), trivial_difference, Holm TOST p 0.0049
edge: estimate 0.125, p 0.0391, Holm p 0.0391 (family phase2), difference, Holm TOST p 0.0633
side: estimate 0.005, p 0.4531, Holm p exploratory, not_detected (exploratory)
