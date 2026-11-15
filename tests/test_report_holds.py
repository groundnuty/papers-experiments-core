"""The report says the holds a platform said only an operator could end (C11).

A campaign held for an operator starts no run during the hold, and a run held has no attempt, so the tables of the
infrastructure failures say nothing of it. The export lists such a hold (`operator_holds`, only when there is one) and
the report gives it a section of its own under the infrastructure tables, with the platform's reason and what each
column means; a report of an export without the key is the text it was.
"""

from export_support import REASON

from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult

CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
RESULT = AnalysisResult((), (), 0.05, {"preregistration_sha256": "e" * 64})
HEADING = "### Holds that needed an operator"


def export(**changes) -> dict:
    return {
        "planned_runs": 0,
        "exhausted_runs": [],
        "unfinished_runs": [],
        "analysed_attempts": [],
        "infrastructure_by_condition": [],
        "retries_by_kind": [],
        "budget_holds": [],
        **changes,
    }


def render(data: dict) -> str:
    return render_report(RESULT, data, study="a-study", code=CODE, core_version="1.2.3")


def hold(start: str, end: str | None, **more) -> dict:
    held = {"start_utc": start, "end_utc": end, "blocking": False, "charged": True, "checks": 2, "reason": REASON}
    return {**held, **more}


def test_c11_a_hold_an_operator_ended_has_a_row_with_its_reason_and_how_it_ended():
    """C11: each hold has its row: when it began, when a check passed (or `open`: the campaign ended with the gate
    closed), whether it was blocking, whether it charged a run, and the platform's reason with its stuck pairs."""
    data = export(
        operator_holds=[
            hold("2026-01-01T00:10:00Z", "2026-01-01T01:00:00Z"),
            hold("2026-01-01T02:00:00Z", None, blocking=True, charged=False),
        ]
    )
    section = render(data).split(HEADING, 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| 2026")]
    assert rows == [
        f"| 2026-01-01T00:10:00Z | 2026-01-01T01:00:00Z | no | yes | {REASON} |",
        f"| 2026-01-01T02:00:00Z | open | yes | no | {REASON} |",
    ]
    assert "| Since (UTC) | Until (UTC) | Blocking | Charged a run | Reason |" in section
    assert "operator" in section.split("|---", 1)[0]  # the lead says what a hold here is


def test_c11_the_holds_of_a_merged_export_name_their_shard():
    """C11: a merged batch's holds say which shard's platform check met each, in the order the export lists them (the
    join gives them in shard order)."""
    data = export(
        operator_holds=[
            hold("2026-01-01T03:00:00Z", None, shard=1),
            hold("2026-01-01T00:10:00Z", None, shard=2),
        ]
    )
    section = render(data).split(HEADING, 1)[1].split("\n## ", 1)[0]
    assert "| Shard | Since (UTC) | Until (UTC) | Blocking | Charged a run | Reason |" in section
    rows = [line for line in section.splitlines() if line.startswith(("| 1 |", "| 2 |"))]
    assert rows == [
        f"| 1 | 2026-01-01T03:00:00Z | open | no | yes | {REASON} |",
        f"| 2 | 2026-01-01T00:10:00Z | open | no | yes | {REASON} |",
    ]


def test_c11_a_report_of_an_export_without_such_a_hold_is_what_it_was():
    """C11: no section, no heading and no word of it when the export has no `operator_holds`."""
    plain = render(export())
    assert HEADING not in plain
    assert "operator" not in plain
    assert render(export(operator_holds=[])) == plain
