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


FAMILY_NOTES = [  # beside the named Holm families' tables (S17)
    "Each Holm family is adjusted on its own, at alpha. Its members' p-values are taken from the smallest up, and the",
    "k-th of a family of m is compared with alpha / (m - k + 1), its step's Level; the first that misses its level",
    "stops the family, so it and every later member are not rejected, whatever their raw p-values. Holm p is the",
    "member's adjusted p-value within its own family, for reading; the decision compares each raw p with its step's",
    "level. An untestable member counts as p = 1 and keeps its family's size. A contrast is in one family at most, and",
    "no family's size or p-values move another's decisions.",
]


def equivalence_family_notes(alpha: float, by_family: bool = False, declared: bool = False) -> list[str]:
    """What the equivalence family's table means (S15); the text names the 1 - 2 alpha interval it replaces.
    `by_family` adds what several named Holm families change (S17): each claim is adjusted within its own.
    `declared` says the claims are in declared equivalence families (S18), each adjusted over its own claims."""
    one = [
        "The preregistration declares these contrasts' equivalence claims as one family. TOST p: the larger of the two"
    ]
    within = [
        "The preregistration declares these contrasts' equivalence claims, and each is adjusted together with the",
        "other claims of its own Holm family only (the Holm family column): that group is the family Holm TOST p,",
        "Interval level and Equivalent are computed over. TOST p: the larger of the two",
    ]
    named = [
        "The preregistration declares these contrasts' equivalence claims in named equivalence families (the",
        "Equivalence family column), each Holm-adjusted over its own claims alone, whatever the Holm families of the",
        "contrasts' significance tests: that family is the one Holm TOST p, Interval level and Equivalent are computed",
        "over, and its TOST steps follow the table. TOST p: the larger of the two",
    ]
    return [
        *(named if declared else within if by_family else one),
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

SMALLEST_NOTES = [  # after the smallest effects' table, only in a report with one (S16)
    "Smallest effect: as preregistered, on the scale of the contrast's estimate; a ratio contrast's is a ratio,",
    "compared as its natural log (in brackets), the scale of its estimate, interval and minimum detectable effect.",
    "Direction: the sign the preregistration claims for A - B (`higher`, `lower`), or `either` when it declares",
    "none. Decision: `meets` (significant, and the estimate at or beyond the smallest effect on the claimed side),",
    "`meets (either direction)` (the same with no direction declared: the estimate's sign says which way),",
    "`opposite_direction` (significant, with the estimate on the other side of zero from the claim, whatever its",
    "size), `falls_short` (significant, and the estimate short of it), `not_significant`, `untestable` (no pair).",
    "Significant is Holm's decision for a member of the family and the raw p-value at alpha for an exploratory",
    "contrast, as for its conclusion.",
    "Interval against it: where the interval lies against the smallest effect on the claimed side (either side",
    "without a direction): it includes it, or excludes it, every value beyond it or every value short of it.",
    "Detects it: `yes` when the minimum detectable effect is at most the smallest effect, so the test reaches the",
    "preregistered power at a true effect of that size; `no` when it is larger; n/a when it cannot be found, `-`",
    "without power settings. A member's minimum detectable effect is at alpha over the family size, so a `yes` is",
    "conservative. At a true effect of exactly the smallest effect, the estimate reaches it only about half the",
    "time, so `meets` is not reached at the preregistered power even when the design detects it.",
    "An equivalence margin is another declaration: it bounds the differences the study calls negligible and",
    "decides `equivalent` and `trivial_difference`, while the smallest effect is what a difference must reach to",
    "count. A contrast that declares both holds two declarations, and neither is derived from the other.",
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

RATIO_MISSING_PAIRS = [  # after MISSING_PAIRS, only in a report with a ratio contrast (S13)
    "- *Numerator None at denominator 0*: a run of a ratio contrast has no numerator and a denominator of 0, a failed "
    "run whose cost was not measured; a confirmatory contrast with any is refused.",
    "- *Zero denominator*: the pair is in a cell whose denominator sums to 0 in either arm, which the "
    'preregistration excludes (`zero_denominator = "exclude"`).',
    "- *Zero numerator*: the pair is in a cell whose numerator sums to 0 in either arm, excluded by the same rule.",
    "- `-`: a cause that does not apply: a contrast of one outcome has none of the three above.",
]

RATIO_NOTES = [
    "A ratio contrast compares arm A's numerator per unit of denominator with arm B's, such as the cost per correct",
    "result. Estimation unit is the cell whose sums give one log ratio: a task attribute (each value of it, its models",
    "pooled) or the model and the attribute (each model's cell of a value). Independent unit is the attribute whose",
    "values are the units of the test, of the resampling and of the minimum detectable effect, and what Clusters",
    "counts: the cells of one value share its response to the treatment, so they are never counted as independent,",
    "and a value's log ratio is the mean of its cells', each model weighing the same. Cells is the number of cells",
    "with a log ratio, then those lost by cause (no pair, or a zero total the rule excludes); a value is dropped only",
    "when all its cells are lost. In each cell, each arm's numerator and denominator are summed over its used pairs,",
    "and a run whose denominator is 0, a failure, still adds its numerator. The cell's value is the natural log of A's",
    "ratio over B's, ln((N_A / D_A) / (N_B / D_B)). The estimate, the intervals, the margin and the minimum detectable",
    "effect in the contrast tables are on that log scale, and the test is the signed-rank test of the values' log",
    "ratios. Ratio (A / B) is exp of the estimate, a geometric mean over the values of A's ratio over B's, and Ratio",
    "interval is exp of the interval. Zero total is the preregistered rule for a cell whose numerator or denominator",
    "sums to 0 in either arm, whose log ratio is undefined or infinite:",
    "`exclude` (the cell is excluded from the contrast, and its pairs are counted by cause in the pairs tables)",
    "or `continuity correction c` (c is added to both sums of both arms in every cell, which keeps every cell",
    "and moves each log ratio most where its sums are small).",
]

RATIO_ARM_COLUMNS_NOTES = [  # after the ratio notes, only for a contrast whose arms read different columns
    "A contrast whose Numerator or Denominator names two columns, each with its arm, reads its own columns in each",
    "arm: in each cell N_A and D_A are summed from arm A's columns and N_B and D_B from arm B's, such as a",
    "sensitivity estimate that puts arm A at its upper-bound cost and arm B at its lower-bound one. Its outcome",
    "names arm A's ratio, then arm B's.",
]

RATIO_BY_MODEL_NOTES = [  # after the ratio contrasts' table, only for a contrast estimated per model's cell
    "Each model's Estimate is the mean log ratio of its own cells, with its ratio and interval from the same",
    "resamples of the values of the independent unit as the contrast's: a breakdown with no test of its own, outside",
    "the Holm family.",
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

SELECTION_NOTES = [
    "This batch was merged from selections: one campaign run in batches, each the runs whose factors have the levels",
    "it names, by one driver. Each run belongs to one selection, and a pair of runs may cross two: a contrast between",
    "conditions of different selections pairs a run of each by task, model and seed, as one driver's runs are paired.",
    "Planned counts the condition's runs in that selection; the three rates are the shares of those planned runs that",
    "were analysed, exhausted or unfinished (a run a partial analysis leaves out is in none of the three), and `n/a`",
    "where the selection ran none of the condition's runs.",
    "",
    "The selections are batches of one design, not a factor of it: the contrasts above pool the runs of every",
    "selection, and every figure in this section is descriptive. A contrast across selections compares runs made at",
    "different times, and possibly on a platform that changed in between; the environment identity every selection",
    "recorded is the same, which the merge checked, and anything it does not cover is for the reader to weigh.",
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

RESET_SETTINGS_NOTES = [
    "The platform reports settings that are in no fingerprint, because they change how it does its work (how a reset",
    "finds what it removes, how long it waits) and not what an agent sees, so a campaign resumes under any of",
    "them. The campaign's store records them at its first start, and every later start compares them with the record.",
    "Each row below is a start at which a setting was not what the record held, with its value before and after",
    "(`absent`: the platform did not report the key on that side); the runs that started after that start ran under",
    "the value after. A start of a store that had no record, made before the settings were recorded, leaves the",
    "settings of the runs before it unknown.",
]
RESET_SETTINGS_UNCHANGED = "No change in them was recorded at any start."

BUDGET_HOLD_NOTES = [
    "Attempts set aside by a budget hold: attempts that ended because a request the token cap could not account for",
    "(a resend after a failure that may have been billed, or a request after a reply without usage) needed a top-up",
    "the campaign's budget could not cover, so it was not sent. They are among the attempts set aside above, and are",
    "counted by the kind of their own outcome: one an outage overlapped, which the table above counts under the",
    "outage's kind, is counted here too. They are counted over every run of the campaign, a run a partial analysis",
    "leaves out included. A model with more of them had its runs stopped by the budget more often than the others.",
]

EGRESS_NOTES = [
    "This batch's runs were checked for egress: their agents ran where the network could not be restricted (an HPC",
    "batch node without network namespaces), so each run's outbound connections were logged through a shim in the",
    "agent's cell and its code cells scanned. A run is flagged for an `outside` connection (an intact log lists a",
    "destination that is neither the data gate nor a loopback address), a `log_unknown` (the log is missing, damaged,",
    "of another run, without the launcher's canary, or the record has no readable egress snapshot: unknown is never",
    "read as clean), or a `static_scan` hit (a URL off the platform, a download, or a network call in the agent's",
    "code). Detection, not prevention: a statically linked program, raw system calls, io_uring or the C library's own",
    "DNS queries pass the shim, which is why the scan is the second line, and neither stops a connection.",
]

MOUNT_GUARD_NOTES = [
    "This batch's mount was guarded: its agents shared a uid with the oneclient that held the mount's token (an HPC",
    "batch node, with no root and no user namespace), so the token could not be kept from them as a separate",
    "container keeps it. oneclient ran on the node outside every cell, its token in a file no cell bound, removed",
    "once the mount was up. A watcher outside the cells logged every event on that file and its directory, sampled",
    "oneclient for a tracer, and proved from the node's `/proc` that each cell it caught ran in a PID namespace of",
    "its own, with no host `/proc` and no private directory in its mounts. A run is `flagged` for a `token_access`",
    "(an event on the token's file or directory while its cells could run) or a `process_access` (a tracer on",
    "oneclient); `unknown` for a `log_unknown` (the log is missing, damaged, of another run or without its canary, or",
    "the record has no readable snapshot) or an `isolation_unproven` (no cell was proved, or a proof failed);",
    "otherwise `clean`. Unknown is never read as clean. Detection, not prevention: inotify names no process, so every",
    "event counts as the agent's; a tracer is sampled, so one that comes and goes between samples is missed;",
    "oneclient's environment and memory are covered only by the proof that no cell can see its process, never",
    "watched; and a cell that starts and ends between two polls is not proved itself.",
]

MOUNT_SENSITIVITY_NOTES = [
    "The preregistered analysis again, without the runs that are not clean (flagged and unknown): each one is left",
    "out as an absent run (`excluded`), so its pairs count as `Unpaired or left out` and its arm's runs as `Left",
    "out`. The analysis above stands; this says whether its conclusions hold without the runs that touched the",
    "mount's token or oneclient's process, or could not be shown not to. A run is left out for what was detected",
    "about it, never for its outcome. The egress section, if any, leaves out its own runs, and a batch checked by",
    "two or more guards also gets a rerun without the runs any of them leaves out.",
]

MOUNT_NOT_APPLICABLE_NOTES = [
    "A run with no mount (a REST run, or a run with no data path) is `not_applicable`: its record carries the",
    "sandbox's statement that the run had none, or, with no snapshot at all, the campaign's fingerprint records that",
    "the run's data path had none. It had no mount exposure to be left out for and it stays in this rerun. It is",
    "never counted as clean, and the egress check, if any, still judges it. A mount run whose record has no",
    "mount-guard snapshot is `unknown`, and left out, whatever it did.",
]

VISIBILITY_NOTES = [
    "This batch's sandbox kept the agent's memory limit with a watch of the harness's over the cell's processes in",
    "`/proc` (an HPC batch node, with no cgroup per container), where `/proc` hides a process of the agent's that",
    "makes itself non-dumpable. The watch checked from outside: at every poll it compared the anonymous memory the",
    "driver's cgroup charged with what the processes `/proc` showed held, and looked for a child of a cell's process",
    "that `/proc` did not show. A run is `flagged` for a `hidden_process` (such a child) or `hidden_memory` (a cell",
    "killed for memory nothing visible explained, past the margin: memory the watch cannot see is taken as past the",
    "limit); `unknown` for a `memory_unknown` (memory that could not be told the run's, a reading that failed, a",
    "margin the start's own cells passed, or no readable snapshot); otherwise `clean`. A run whose memory reading is",
    "unknown has an unknown limit condition, never one within its limit. Detection, not prevention: a process that",
    "forks twice under the cell's hidden init is a child of no visible process, hidden memory under the margin is not",
    "told from the node's own share, and a hidden process's shared memory is not compared; the step's limit bounds",
    "them, and every hidden process ends with its cell.",
]

VISIBILITY_SENSITIVITY_NOTES = [
    "The preregistered analysis again, without the runs that are not clean (flagged and unknown): each one is left",
    "out as an absent run (`excluded`), so its pairs count as `Unpaired or left out` and its arm's runs as `Left",
    "out`. The analysis above stands; this says whether its conclusions hold without the runs whose code hid a",
    "process or its memory from the harness, or whose memory could not be read. A run is left out for what was",
    "detected about it, never for its outcome.",
]

GUARDS_NOTES = [
    "This batch was checked by both guards, the egress check and the mount guard, and each section above reruns the",
    "analysis without its own runs. This one reruns it without every run either leaves out: flagged or unknown to",
    "either, so a run unknown to one guard and clean to the other is left out. It is the rerun to read when the",
    "question is whether the conclusions hold for the runs both guards show clean. A run's egress is `flagged` for an",
    "outside connection or a static-scan hit and `unknown` for an unknown log alone; its mount is the mount guard's",
    "verdict.",
]

GUARDS_SENSITIVITY_NOTES = [
    "The preregistered analysis again, without every run either guard leaves out: each one is left out as an absent",
    "run (`excluded`), so its pairs count as `Unpaired or left out` and its arm's runs as `Left out`. The analysis",
    "above stands, and so do the two reruns of the single guards. A run is left out for what was detected about it,",
    "never for its outcome.",
]


def any_guard_notes(names: str) -> list[str]:
    """The notes of the combined section of a batch checked by guards other than egress and the mount guard alone:
    `names` lists the guards that checked it (O76)."""
    return [
        f"This batch was checked by {names}, and each section above reruns the analysis without its own runs. This one",
        "reruns it without every run any of them leaves out: flagged or unknown to any, so a run unknown to one guard",
        "and clean to the others is left out. A guard that does not apply to a run leaves it out for nothing: a run",
        "with no mount is not applicable to the mount guard and is left out only for another guard that flagged it or",
        "could not read it. It is the rerun to read when the question is whether the conclusions hold for the runs",
        "every guard that applies to them shows clean. A run's egress is `flagged` for an outside connection or a",
        "static-scan hit and `unknown` for an unknown log alone; its mount and its process visibility are those",
        "guards' verdicts.",
    ]


def any_guard_sensitivity_notes(either: str) -> list[str]:
    """The notes of the rerun of the combined section (see `any_guard_notes`); `either` is `either` or `any`."""
    return [
        f"The preregistered analysis again, without every run {either} guard leaves out: each one is left out as an",
        "absent run (`excluded`), so its pairs count as `Unpaired or left out` and its arm's runs as `Left out`. The",
        "analysis above stands, and so do the reruns of the single guards. A run is left out for what was detected",
        "about it, never for its outcome.",
    ]


SENSITIVITY_NOTES = [
    "The preregistered analysis again, without the flagged runs: each one is left out as an absent run (`excluded`),",
    "so its pairs count as `Unpaired or left out` and its arm's runs as `Left out`. The analysis above stands; this",
    "says whether its conclusions hold without the runs whose network use was detected or could not be checked. A run",
    "is left out for what was detected about it, never for its outcome.",
]

HOLD_NOTES = [
    "Each row is a stretch in which the platform's health check said that only an operator could end what it found (a",
    "repair that failed and left its fault in place), so no run started. A held run has no attempt; the hold itself is",
    "in none of the counts above. Until: when a check passed again, or `open` when the campaign ended with the gate",
    "closed. Blocking: the wait outlasted `max_defer_s`, and a campaign that exits when blocked stopped. Charged a",
    "run: one run was charged an attempt for the hold, the run whose check found it, and that attempt is among the",
    "infrastructure failures above. The reason is the platform's own words, naming what is stuck by label only.",
]
