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


def shard_table(report: str) -> list[list[str]]:
    """The cells of every row under the shard heading of the report, the header row first."""
    lines = report.split(SHARD_HEADING, 1)[1].splitlines()
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
    return SHARD_HEADING + after.split("## ", 1)[0]


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
