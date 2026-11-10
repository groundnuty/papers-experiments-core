# Provenance: new
"""The fixed prose of the Markdown report (`expcore.report`): what each column and count means, stated beside it.

Kept apart from the rendering so the report's code stays short; the text is part of the report's bytes, so a change
here changes every report, and a replication package's regeneration with it.
"""

NO_KIND = "none"  # the kind column of a condition's row of zeros


def percent(level: float) -> str:
    """A level as a percentage with at most two decimals and no trailing zeros: 0.9 is 90%, 0.975 is 97.5%, never
    rounded to another level's name."""
    return f"{level * 100:.2f}".rstrip("0").rstrip(".") + "%"


def equivalence_header(alpha: float) -> str:
    """The heading of the equivalence interval's column: its level, 1 - 2 alpha."""
    return f"{percent(1 - 2 * alpha)} equivalence interval"


def contrast_notes(alpha: float) -> list[str]:
    """What the conclusion words, the margin, the equivalence interval, the pairs used and the treatment column mean."""
    return [
        "Conclusions: `difference` (significant), `trivial_difference` (significant, but inside the equivalence",
        "margin), `equivalent` (inside the margin, not significant), `not_detected` (anything else; failing to",
        "detect a difference is not evidence that there is none), `untestable` (no usable pair, so nothing was",
        "tested and the contrast is not evidence of anything, least of all of no effect; in the Holm family it",
        "counts as p = 1).",
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
]
