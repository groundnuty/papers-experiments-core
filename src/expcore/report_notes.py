# Provenance: new
"""The fixed prose of the Markdown report (`expcore.report`): what each column and count means, stated beside it.

Kept apart from the rendering so the report's code stays short; the text is part of the report's bytes, so a change
here changes every report, and a replication package's regeneration with it.
"""

NO_KIND = "none"  # the kind column of a condition's row of zeros


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

BUDGET_HOLD_NOTES = [
    "Attempts set aside by a budget hold: attempts that ended because a request the token cap could not account for",
    "(a resend after a failure that may have been billed, or a request after a reply without usage) needed a top-up",
    "the campaign's budget could not cover, so it was not sent. They are among the attempts set aside above, and are",
    "counted by the kind of their own outcome: one an outage overlapped, which the table above counts under the",
    "outage's kind, is counted here too. They are counted over every run of the campaign, a run a partial analysis",
    "leaves out included. A model with more of them had its runs stopped by the budget more often than the others.",
]
