# Provenance: new
"""The fixed prose of the Markdown report (`expcore.report`): what each column and count means, stated beside it.

Kept apart from the rendering so the report's code stays short; the text is part of the report's bytes, so a change
here changes every report, and a replication package's regeneration with it.
"""

NO_KIND = "none"  # the kind column of a condition's row of zeros
UNKNOWN = "n/a"  # a value that is missing or cannot be computed, never shown as zero


def percent(level: float) -> str:
    """A level as a percentage in as many digits as it has, up to ten significant ones: 0.9 is 90%, 0.975 is 97.5%,
    0.99999 is 99.999%, never rounded to another level's name (only float noise past the tenth digit goes)."""
    return f"{level * 100:.10g}%"


def equivalence_header(alpha: float) -> str:
    """The heading of the equivalence interval's column: its level, 1 - 2 alpha."""
    return f"{percent(1 - 2 * alpha)} equivalence interval"


def contrast_notes(alpha: float, family: bool = True) -> list[str]:
    """What the conclusion words, the margin, the equivalence interval, the pairs used and the treatment column mean.

    `family` is False for a preregistration with no Holm family (S12), whose notes then say nothing of one.
    """
    untestable = [
        "tested and the contrast is not evidence of anything, least of all of no effect; in the Holm family it",
        "counts as p = 1).",
    ]
    if not family:
        untestable = ["tested and the contrast is not evidence of anything, least of all of no effect)."]
    return [
        "Conclusions: `difference` (significant), `trivial_difference` (significant, but inside the equivalence",
        "margin), `equivalent` (inside the margin, not significant), `not_detected` (anything else; failing to",
        "detect a difference is not evidence that there is none), `untestable` (no usable pair, so nothing was",
        *untestable,
        "",
        "Margin: the preregistered equivalence margin, plus or minus; `-` when the contrast has none, and then it",
        "can never conclude `equivalent` or `trivial_difference`.",
        f"{equivalence_header(alpha)}: the cluster-bootstrap interval at 1 - 2 alpha. The contrast is inside the",
        "margin when this interval lies within plus or minus the margin, which `equivalent` and `trivial_difference`",
        "need; n/a when no pair was used, `-` without a margin.",
        "",
        "Pairs used: pairs with the outcome on both sides; each missing pair is counted by its cause in the pairs",
        'table of its section ("Missing pairs by cause" says what each cause counts). For an untestable contrast',
        "the cell gives those causes with their counts.",
        "",
        "Treatment attempts: how the contrast's treatment attempts (runs that ended in an infrastructure failure their",
        "condition declares as its treatment) entered it. `scored`: the preregistration says the scorer returns values",
        "for them. `excluded`: the preregistration leaves every pair with one out of the contrast, and the count says",
        "how many; the contrast then says nothing about the outage itself. `not declared`: pairs were lost because a",
        "treatment attempt's outcome is None, which only an exploratory contrast may do. `-`: nothing declared,",
        "nothing lost.",
    ]


def equivalence_family_notes(alpha: float) -> list[str]:
    """What the equivalence family's table means (S15); the text names the 1 - 2 alpha interval it replaces."""
    return [
        "The preregistration declares these contrasts' equivalence claims as one family. TOST p: the larger of the two",
        "one-sided tests' p-values, for an effect above minus the margin and for one below the margin (for the",
        "Wilcoxon cluster bootstrap, t-tests of the mean cluster difference on clusters - 1 degrees of freedom; n/a",
        "with fewer than two clusters or no spread between them). Holm TOST p: Holm-adjusted over the family, for",
        "reading; the decision compares each raw TOST p with its step's level. A member is `equivalent` only when Holm",
        "rejects,",
        f"so for these contrasts the conclusion rests on this table, not the {equivalence_header(alpha)} beside it,",
        "which can lie inside the margin while the family's decision is no. Adjusted interval: the test's own interval",
        "at 1 - 2 times the level Holm compared the member with (Interval level), inside the margin exactly when the",
        "TOST p is at most that level; an untestable member has none and counts as p = 1.",
    ]


NO_CLAIM = (  # the summary line of a preregistration with no Holm family (S12)
    "the preregistration declares `exploratory_only = true`, so it makes no confirmatory claim: every contrast is "
    "exploratory, judged by its raw p-value at alpha with nothing adjusting for multiplicity, and none may be read as "
    "a confirmatory test"
)

MDE_NOTES = [
    "Min. detectable effect: the smallest effect this contrast's number of clusters could detect with the",
    "preregistered power; n/a when it cannot be found (no spread between clusters, or too few clusters);",
    "`-` for an untestable contrast, which has none.",
]

MISSING_PAIRS = [
    "## Missing pairs by cause",
    "",
    "A pair is two runs that differ only in the contrast's arm. Each pair is used or is missing for one cause, counted",
    "in the pairs tables above:",
    "",
    "- *Treatment outcome None*: the outcome is None on a treatment attempt, whatever became of the other planned run.",
    "- *Treatment excluded*: a run is a treatment attempt and the preregistration declares "
    '`treatment_outcomes = "excluded"` for the contrast; left out whatever its outcome.',
    "- *Exhausted run*: the run of either arm used all its attempts on infrastructure failures.",
    "- *Unpaired or left out*: the other run is unfinished, left out of a partial analysis, or not in the plan (a "
    "task one arm does not run).",
    "- *Outcome None (other)*: both runs analysed, and the outcome is None on an ordinary run only.",
]

ARM_LOST_HEADINGS = ["Treatment outcome None", "Outcome None", "Exhausted", "Unfinished", "Left out"]  # LOST_CAUSES

ARM_RUNS_NOTES = [
    "Each arm's own planned runs on the contrast's tasks (S14), whatever became of their partners: a pair lost to one",
    "run's None is one run lost here, in the arm that lost it. Finished: the runs with an analysed attempt, the",
    "population the contrast's outcome is defined over, and Finish rate their share of the planned runs (n/a for an",
    "arm with none planned). Measured: the finished runs whose outcome is known. The other columns give the runs",
    "lost by cause: the outcome None on a treatment attempt or on another run, or no analysed attempt (exhausted,",
    "unfinished, or left out of a partial analysis). An arm that finished fewer of its runs is compared on fewer, and",
    "perhaps other, runs than its partner.",
]

DESCRIPTIVE_NOTES = [
    "Each one-arm estimate is descriptive: no test is made, it has no conclusion word, and it is in no Holm family.",
    "A rate is the mean of its outcome over the arm's runs that have it, averaged per task and model, then per",
    "cluster and model, then over each cluster's models and over the clusters, each with equal weight (a share for an",
    "outcome of 0 or 1). A ratio of totals is the sum of the numerator over the sum of the denominator across every",
    "run of the arm that has both. The interval resamples whole clusters at the preregistration's bootstrap; a",
    "replicate whose drawn denominators sum to 0 has no ratio and is counted under Undefined replicates, and the",
    "interval is n/a when fewer than half are defined. Finish rate is the share of the arm's planned runs that",
    "finished.",
]

CONDITION_NOTES = [
    "Planned runs are analysed, exhausted (charged infrastructure attempts reached the limit; no scored attempt),",
    "unfinished, or left out of a partial analysis. Attempts set aside are the condition's set-aside",
    "infrastructure attempts, as in the table below. Median dispatch position is the median place, in the",
    "campaign's dispatch sequence, of each analysed run's scored attempt, where it has one (a database older than",
    "the dispatch column has none); a condition whose runs came late has a higher one.",
]

DEPLOYMENT_NOTES = [
    "A model listed here declares `accepted_fingerprints`: the deployments of its endpoint, each named by its serving",
    "fingerprint, that the study decided are the same model. A reply served under any other fingerprint set its run",
    "aside, so these counts are over the analysed runs alone. *Turns*: the analysed replies each fingerprint served,",
    "and their share of the model's analysed replies. *Runs*: the analysed runs it served at least one reply of, and",
    "their share of the model's analysed runs; a run served by two deployments counts for both, so the shares of runs",
    "can add up to more than 100%. *Confirmed by*: the admission probe or the first run whose reply showed the",
    "fingerprint under the model's own id; `unconfirmed` for a declared fingerprint no reply has shown. A run without",
    "a recorded mix (one recovered from its outcome marker) is counted in neither column.",
]

COUNT_DEFINITIONS = [
    f"- *Kind* `{NO_KIND}`: the condition met no infrastructure failure and declares no treatment kind; every count is "
    "0.",
    "- *Set aside*: attempts that ended as an infrastructure failure of a kind the condition does not declare as "
    "treatment, or before the model's first turn, or that were discarded for an outage. Counted over every run of "
    "the campaign whatever its status (an exhausted run's last attempt included), except runs a partial analysis "
    "leaves out.",
    "- *Scored as treatment*: attempts that ended as an infrastructure failure of a kind the condition declares as "
    "treatment, after the model's first turn; each is its run's final, scored attempt. Runs a partial analysis leaves "
    "out are not included.",
    "- *Left out*: the set-aside and treatment attempts of runs a partial analysis leaves out, and only those.",
    "- *Kind* `budget_hold`: no failure of the infrastructure, but the campaign's budget: a request was not sent "
    "because the top-up it needed would have passed the ceiling. Set aside like a failure, and never a treatment.",
]

SHARD_NOTES = [
    "This batch was merged from shards: one campaign run as several parts, each by one driver on its own host. Each",
    "run belongs to one shard, and a pair of runs is never split between two. Planned counts the condition's runs in",
    "that shard; the three rates are the shares of those planned runs that were analysed, exhausted or unfinished (a",
    "run a partial analysis leaves out is in none of the three), and `n/a` where the shard ran none of the",
    "condition's runs.",
    "",
    "The shards are a check that the hosts are interchangeable, not a factor of the design: no test across shards is",
    "made, the contrasts above pool the runs of every shard, every figure in this section is descriptive, and no",
    "score is shown by shard. A pair never crosses shards, so a host effect meets both arms alike, and a score by",
    "shard would mostly show which instances landed on which host. A difference between shards is for the reader to",
    "weigh: a condition whose figures differ by shard is worth a look at the shards before it is read as an effect.",
]

SHARD_KIND_NOTES = [
    "Set aside (all runs): attempts that ended as an infrastructure failure, or were discarded for an outage, in",
    "every run of the shard, by the kind of their outcome as the batch's retries are counted; `not recorded` for an",
    "export that does not carry them per shard. Charged in exhausted runs: the charged attempts of the",
    "shard's exhausted runs, by kind; where the first column is recorded they are among it. Scored as treatment: the",
    "shard's analysed attempts that ended as an infrastructure failure their condition declares as its treatment.",
    f"Kind `{NO_KIND}`: the shard met no infrastructure failure. Kind `unknown`: an attempt that recorded none.",
]

SHARD_TIME_NOTES = [
    "Each figure is the median and the 90th percentile (interpolated between the sorted values) of the N values the",
    "export carries, in seconds. Run wall time: the wall time of each analysed run's scored attempt. Turn latency:",
    "the latency of each model reply in those attempts. Reset time: the time each run's environment reset took; the",
    "core does not record it, so it is `not recorded` unless the export carries it. N can be below the shard's",
    "analysed runs when only some of them carry the figure; `not recorded` where none does, and `n/a` for a shard",
    "with no analysed run.",
]

BUDGET_HOLD_NOTES = [
    "Attempts set aside by a budget hold: attempts that ended because a request the token cap could not account for",
    "(a resend after a failure that may have been billed, or a request after a reply without usage) needed a top-up",
    "the campaign's budget could not cover, so it was not sent. They are among the attempts set aside above, and are",
    "counted by the kind of their own outcome: one an outage overlapped, which the table above counts under the",
    "outage's kind, is counted here too. They are counted over every run of the campaign, a run a partial analysis",
    "leaves out included. A model with more of them had its runs stopped by the budget more often than the others.",
]
