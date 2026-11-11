"""The report of a merged, sharded campaign: a table per shard and condition, and nothing new without shards (#154).

The exports here are dicts in the shape of the frozen spec: a top-level `shards` list (index, label, started_utc,
shares, runs, snapshot_sha256) and, in each run entry, `shard: index`. Nothing here builds a merge.
"""

import pytest

from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult

CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
RESULT = AnalysisResult((), (), 0.05, {"preregistration_sha256": "e" * 64})  # no contrast: only the tables matter
RUN_PARTS = ("analysed_attempts", "exhausted_runs", "unfinished_runs", "excluded_attempts")

SHARDS = [
    {
        "index": 1,
        "label": "shard-1",
        "started_utc": "2026-03-04T05:06:07Z",
        "shares": {"unit_caps": {"model-a": 5}, "budget": None},
        "runs": 10,
        "snapshot_sha256": "1" * 64,
    },
    {
        "index": 2,
        "label": "shard-2",
        "started_utc": "2026-03-04T09:10:11Z",
        "shares": {"unit_caps": {"model-a": 5}, "budget": None},
        "runs": 6,
        "snapshot_sha256": "2" * 64,
    },
]
HEADER = ["Shard", "Started (UTC)", "Condition", "Planned", "Analysed", "Exhausted", "Unfinished", "Left out"]
HEADER += ["Analysed %", "Exhausted %", "Unfinished %"]
SHARD_HEADING = "## Shards"


def export(**changes) -> dict:
    """An export with no run and no infrastructure row; each run entry names a model, as an export's entries do."""
    data = {
        "planned_runs": 0,
        "exhausted_runs": [],
        "unfinished_runs": [],
        "analysed_attempts": [],
        "infrastructure_by_condition": [],
        "retries_by_kind": [],
        "budget_holds": [],
        **changes,
    }
    for part in RUN_PARTS:
        if part in data:
            data[part] = [{"factors": {"model": "model-a"}, **entry} for entry in data[part]]
    return data


def render(data: dict) -> str:
    return render_report(RESULT, data, study="a-study", code=CODE, core_version="1.2.3")


def entries(shard: int, condition: str, count: int, **extra) -> list[dict]:
    """`count` run entries of one condition in one shard; the run key is unique across the export."""
    return [
        {"run_key": f"s{shard}-{condition}-{i}", "condition": condition, "shard": shard, "dispatch_seq": i, **extra}
        for i in range(count)
    ]


def sharded(**changes) -> dict:
    """A two-shard export: shard 1 runs three conditions and shard 2 two of them (it has no run of `cond-c`).

    | shard | condition | analysed | exhausted | unfinished |
    | 1     | cond-a    | 3        | 1         | 0          |
    | 1     | cond-b    | 2        | 0         | 2          |
    | 1     | cond-c    | 2        | 0         | 0          |
    | 2     | cond-a    | 2        | 0         | 1          |
    | 2     | cond-b    | 3        | 0         | 0          |
    """
    base = {
        "planned_runs": 16,
        "analysed_attempts": [
            *entries(1, "cond-a", 3),
            *entries(1, "cond-b", 2),
            *entries(1, "cond-c", 2),
            *entries(2, "cond-a", 2),
            *entries(2, "cond-b", 3),
        ],
        "exhausted_runs": entries(1, "cond-a", 1, attempts=3, reasons=[]),
        "unfinished_runs": [*entries(1, "cond-b", 2, status="pending"), *entries(2, "cond-a", 1, status="pending")],
        "shards": SHARDS,
    }
    return export(**{**base, **changes})


def shard_table(report: str, heading: str = SHARD_HEADING) -> list[list[str]]:
    """The cells of every row of the first table under a heading of the report, the header row first."""
    lines = report.split(heading, 1)[1].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("|"))
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        if not line.startswith("|---"):
            rows.append([cell.strip() for cell in line.strip("|").split(" | ")])
    return rows


def shard_section(report: str) -> str:
    """The text from the shard heading up to the next `## ` heading, the blank line before that heading included."""
    after = report.split(SHARD_HEADING, 1)[1]
    return SHARD_HEADING + after.split("\n## ", 1)[0] + "\n"


def test_c29_a_merged_batch_has_a_row_per_shard_and_condition_with_its_label_start_and_run_counts():
    """C29: the report of a merged batch gives each shard's label and start, and per condition the runs analysed,
    exhausted and unfinished in that shard, a row of zeros for a condition the shard ran none of."""
    rows = shard_table(render(sharded()))
    assert rows[0] == HEADER
    assert [row[:8] for row in rows[1:]] == [
        ["shard-1", "2026-03-04T05:06:07Z", "cond-a", "4", "3", "1", "0", "0"],
        ["shard-1", "2026-03-04T05:06:07Z", "cond-b", "4", "2", "0", "2", "0"],
        ["shard-1", "2026-03-04T05:06:07Z", "cond-c", "2", "2", "0", "0", "0"],
        ["shard-2", "2026-03-04T09:10:11Z", "cond-a", "3", "2", "0", "1", "0"],
        ["shard-2", "2026-03-04T09:10:11Z", "cond-b", "3", "3", "0", "0", "0"],
        ["shard-2", "2026-03-04T09:10:11Z", "cond-c", "0", "0", "0", "0", "0"],
    ]


def test_c29_each_condition_has_its_outcome_rates_in_every_shard_so_a_reader_can_compare_the_hosts():
    """C29: each rate is the share of the condition's planned runs in that shard that ended that way; a shard that
    ran none of the condition's runs shows n/a, never 0%."""
    rows = shard_table(render(sharded()))
    assert [row[8:] for row in rows[1:]] == [
        ["75.0%", "25.0%", "0.0%"],
        ["50.0%", "0.0%", "50.0%"],
        ["100.0%", "0.0%", "0.0%"],
        ["66.7%", "0.0%", "33.3%"],
        ["100.0%", "0.0%", "0.0%"],
        ["n/a", "n/a", "n/a"],
    ]


def test_c29_the_section_says_shards_check_that_hosts_are_interchangeable_and_no_test_across_them_is_made():
    """C29: the shards are a check that the hosts are interchangeable, not a factor of the design: no test across
    shards is made, and the contrasts pool every shard's runs."""
    section = shard_section(render(sharded()))
    text = " ".join(section.split())
    assert "check that the hosts are interchangeable" in text
    assert "not a factor of the design" in text
    assert "no test across shards is made" in text


def test_c29_the_section_follows_the_conditions_and_precedes_the_attempts_left_out():
    """C29: the shard section sits between the condition tables and the list of attempts left out."""
    report = render(sharded())
    assert report.index("### Budget holds by condition and model") < report.index(SHARD_HEADING)
    assert report.index(SHARD_HEADING) < report.index("## Attempts left out of the analysis")


def test_c29_runs_a_partial_analysis_left_out_are_counted_in_their_shard_and_keep_the_rates_of_the_planned_runs():
    """C29: a left-out run stays in its shard's planned runs, so the rates are of the planned runs, not of the
    analysed ones."""
    left = entries(2, "cond-b", 1, attempt=1, reason="not replayed")
    data = sharded(
        planned_runs=16,
        analysed_attempts=[
            *entries(1, "cond-a", 3),
            *entries(1, "cond-b", 2),
            *entries(1, "cond-c", 2),
            *entries(2, "cond-a", 2),
            *entries(2, "cond-b", 2),
        ],
        excluded_attempts=left,
        partial=True,
        running_attempts=0,
    )
    rows = {(row[0], row[2]): row for row in shard_table(render(data))[1:]}
    assert rows[("shard-2", "cond-b")][3:] == ["3", "2", "0", "0", "1", "66.7%", "0.0%", "0.0%"]


def test_c29_a_report_without_shards_has_no_shard_section_and_a_sharded_one_differs_only_by_that_section():
    """C29: without `shards` the report has no shard section, whatever else the run entries carry; with it, the rest of
    the text is the same bytes, so a reader of an unmerged batch sees nothing new."""
    unsharded = render({key: value for key, value in sharded().items() if key != "shards"})
    with_shards = render(sharded())
    assert SHARD_HEADING not in unsharded
    assert "shard" not in unsharded.lower()
    assert with_shards.replace(shard_section(with_shards), "", 1) == unsharded


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"run_key": "orphan", "shard": 3}, "run 'orphan' is in shard 3, which the export's shards do not list"),
        ({"run_key": "unnamed"}, "run 'unnamed' names no shard, but the export lists shards"),
    ],
)
def test_c29_a_run_that_names_no_shard_or_one_the_export_does_not_list_is_refused(entry, message):
    """C29: an export whose `shards` does not account for every run's shard is refused, naming the run."""
    extra = {"condition": "cond-a", "dispatch_seq": 99, **entry}
    base = sharded()
    data = sharded(planned_runs=17, analysed_attempts=[*base["analysed_attempts"], extra])
    with pytest.raises(ValueError, match=message):
        render(data)


@pytest.mark.parametrize("shards", [[], None, "shard-1", [{"label": "shard-1"}]])
def test_c29_a_shards_value_that_is_not_a_list_of_shards_is_refused(shards):
    """C29: `shards` present but empty, not a list, or holding an entry without an index is a malformed export."""
    with pytest.raises(ValueError, match="shards"):
        render(sharded(shards=shards))


# --- what the hosts' infrastructure and clocks looked like (descriptive; no test across shards) -------------------

KINDS_HEADING = "### Infrastructure attempts by shard and kind"
TIME_HEADING = "### Time by shard"
NOT_RECORDED = "not recorded"


def annotate(data: dict, shard: int, field: str, values: list) -> dict:
    """Set `field` on the analysed entries of one shard, in order, one value each; None leaves the entry without it."""
    mine = [entry for entry in data["analysed_attempts"] if entry["shard"] == shard]
    for entry, value in zip(mine, values, strict=True):
        if value is not None:
            entry[field] = value
    return data


def retries(shard: int, kind: str, attempts: int, state: str = "infra_fail", charged: bool = True) -> dict:
    """A row of the export's `retries_by_kind`, with the shard whose store it was counted in."""
    row = {"condition": "cond-a", "model": "model-a", "kind": kind, "state": state, "charged": charged}
    return {"shard": shard, **row, "attempts": attempts}


def infrastructure() -> dict:
    """Shard 1: 5 timeout and 1 dns attempts set aside, of which its exhausted run's 3 charged ones (2 timeout, 1
    dns); shard 2: 4 timeout set aside, none exhausted, and one analysed run that scored a timeout as treatment."""
    data = sharded(
        exhausted_runs=entries(1, "cond-a", 1, attempts=3, reasons=["timeout: a", "timeout: b", "dns: c"]),
        shard_retries_by_kind=[
            retries(1, "timeout", 3),
            retries(1, "timeout", 2, "outage_discarded", False),
            retries(1, "dns", 1),
            retries(2, "timeout", 4, charged=False),
        ],
    )
    annotate(data, 2, "treatment", [True, None, None, None, None])
    return annotate(data, 2, "infra_kind", ["timeout", None, None, None, None])


def test_c29_each_shard_shows_its_attempts_set_aside_by_kind_beside_what_its_exhausted_and_treatment_runs_show():
    """C29: per shard and kind, the attempts set aside in all its runs (the export's per-shard retries), the charged
    attempts of its exhausted runs by their kind, and the attempts scored as treatment; a kind a shard never met is a
    row of zeros, never a gap."""
    rows = shard_table(render(infrastructure()), KINDS_HEADING)
    assert rows[0] == ["Shard", "Kind", "Set aside (all runs)", "Charged in exhausted runs", "Scored as treatment"]
    assert rows[1:] == [
        ["shard-1", "dns", "1", "1", "0"],
        ["shard-1", "timeout", "5", "2", "0"],
        ["shard-2", "dns", "0", "0", "0"],
        ["shard-2", "timeout", "4", "0", "1"],
    ]


def test_c29_set_asides_the_export_keeps_only_as_a_sum_over_every_shard_read_not_recorded_never_zero():
    """C29: an export without per-shard retries has the sum over the shards (`retries_by_kind`) and nothing a shard
    can be given: the column says not recorded, and the two columns the run entries do carry are still counted."""
    data = infrastructure()
    del data["shard_retries_by_kind"]
    rows = shard_table(render(data), KINDS_HEADING)
    assert [row[1:] for row in rows[1:]] == [
        ["dns", NOT_RECORDED, "1", "0"],
        ["timeout", NOT_RECORDED, "2", "0"],
        ["dns", NOT_RECORDED, "0", "0"],
        ["timeout", NOT_RECORDED, "0", "1"],
    ]


def test_c29_a_merged_batch_that_met_no_infrastructure_failure_has_a_row_of_zeros_per_shard():
    """C29: with no kind in any shard, each shard has a row of kind none: 0 is never mistaken for not reported."""
    rows = shard_table(render(sharded(shard_retries_by_kind=[])), KINDS_HEADING)
    assert rows[1:] == [["shard-1", "none", "0", "0", "0"], ["shard-2", "none", "0", "0", "0"]]


TIME_HEADER = ["Shard", "Runs timed", "Run wall time median (s)", "Run wall time p90 (s)", "Turns timed"]
TIME_HEADER += ["Turn latency median (s)", "Turn latency p90 (s)", "Resets timed", "Reset median (s)"]


def timed() -> dict:
    """Shard 1's 7 analysed runs took 10 to 70 s (one each of 10, 20, ..., 70) and had one turn of 1 to 7 s each;
    shard 2's 5 took 100 to 500 s and had three turns each, of j, 2j and 3j seconds for run j."""
    data = sharded()
    annotate(data, 1, "wall_s", [10.0 * k for k in range(1, 8)])
    annotate(data, 1, "turn_latency_s", [[float(k)] for k in range(1, 8)])
    annotate(data, 2, "wall_s", [100.0 * j for j in range(1, 6)])
    return annotate(data, 2, "turn_latency_s", [[float(j), 2.0 * j, 3.0 * j] for j in range(1, 6)])


def test_c29_each_shard_shows_the_median_and_p90_of_its_runs_wall_time_and_of_its_turns_latency_with_n():
    """C29: the analysed runs' wall time and the model turns' latency, per shard, as median and p90 with the number of
    values each rests on; p90 interpolates between the order statistics, so 7 values of 10 to 70 have a p90 of 64."""
    rows = shard_table(render(timed()), TIME_HEADING)
    assert rows[0] == TIME_HEADER
    assert [row[:7] for row in rows[1:]] == [
        ["shard-1", "7", "40", "64", "7", "4", "6.4"],
        ["shard-2", "5", "300", "460", "15", "5", "11.2"],
    ]


def test_c29_a_figure_no_run_carries_reads_not_recorded_and_a_shard_with_no_analysed_run_reads_n_a():
    """C29: a shard whose analysed runs carry no wall time, turn latency or reset time says not recorded, with N 0;
    only a shard with no analysed run at all reads n/a, since there was nothing to time."""
    data = sharded(
        analysed_attempts=entries(1, "cond-a", 3),
        exhausted_runs=[],
        unfinished_runs=[*entries(1, "cond-b", 4, status="pending"), *entries(2, "cond-a", 2, status="pending")],
        planned_runs=9,
    )
    rows = shard_table(render(data), TIME_HEADING)
    assert rows[1] == ["shard-1", "0", NOT_RECORDED, NOT_RECORDED, "0", NOT_RECORDED, NOT_RECORDED, "0", NOT_RECORDED]
    assert rows[2] == ["shard-2", "0", "n/a", "n/a", "0", "n/a", "n/a", "0", "n/a"]


def test_c29_a_figure_only_some_of_a_shards_runs_carry_is_taken_from_those_and_its_n_says_how_many():
    """C29: N is the number of values the figure rests on, so a shard whose runs carry it only in part shows fewer
    than its analysed runs; reset time is read the same way as the other two figures."""
    data = sharded()
    annotate(data, 1, "wall_s", [10.0, None, 30.0, None, None, None, None])
    annotate(data, 2, "reset_s", [3.0, 4.0, None, None, None])
    rows = shard_table(render(data), TIME_HEADING)
    assert rows[1][1:4] == ["2", "20", "28"]  # median of 10 and 30; p90 interpolates to 10 + 0.9 * 20
    assert rows[1][7:] == ["0", NOT_RECORDED]
    assert rows[2][7:] == ["2", "3.5"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"shard_retries_by_kind": [retries(9, "timeout", 1)]}, "shard_retries_by_kind names shard 9"),
        ({"analysed_attempts": [{"run_key": "x", "condition": "cond-a", "shard": 1, "wall_s": "slow"}]}, "wall_s"),
        (
            {"analysed_attempts": [{"run_key": "x", "condition": "cond-a", "shard": 1, "turn_latency_s": [1.0, -2.0]}]},
            "-2.0",
        ),
    ],
)
def test_c29_a_per_shard_row_of_an_unlisted_shard_or_a_figure_that_is_not_seconds_is_refused(changes, message):
    """C29: the section never rests on a guess: a retries row of a shard the export does not list, or a wall time or
    latency that is not a finite, non-negative number, is refused, naming it."""
    data = sharded(**{"planned_runs": 1, "exhausted_runs": [], "unfinished_runs": [], **changes})
    with pytest.raises(ValueError, match=message):
        render(data)


def test_c29_the_per_shard_figures_in_an_export_without_shards_are_not_read_and_change_no_byte_of_the_report():
    """C29: a batch that was not merged has no section, so the optional per-run figures of a merged export, were an
    export to carry them without `shards`, change nothing."""
    plain = {key: value for key, value in sharded().items() if key != "shards"}
    figures = {key: value for key, value in timed().items() if key != "shards"}
    figures["shard_retries_by_kind"] = [retries(1, "timeout", 3)]
    assert render(figures) == render(plain)


def test_c29_the_section_gives_no_score_by_shard_and_says_every_figure_is_descriptive():
    """C29: pairs never cross shards, so a host effect meets both arms alike and a score by shard would show which
    instances landed where: the section shows completion and infrastructure only, and says no test is made."""
    text = " ".join(shard_section(render(timed())).split())
    assert "descriptive" in text
    assert "no score is shown by shard" in text
    assert "Estimate" not in text
