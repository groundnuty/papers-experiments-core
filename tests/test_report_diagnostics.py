"""C40: the report counts, per condition, the attempts whose instrument failed to start and kept a diagnostic.

The export lists them (`attempt_diagnostics`, only when an attempt kept one); the report gives a section of its own
after the infrastructure tables with a row of counts per condition, instrument, kind and state, and a row of zeros for
a condition that had none, so 0 is never mistaken for not reported. What each snapshot says is in the export. A
diagnostic withheld because its executor declared no secrets is counted in a column of its own, with the reason in the
section's notes, and its text is nowhere in the report. A report of an export without the key is the text it was.
"""

from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult

CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
RESULT = AnalysisResult((), (), 0.05, {"preregistration_sha256": "e" * 64})
HEADING = "### Instrument start failures"
STARTED = {"phase": "start", "instrument": "state_inducer", "diagnostic": {"checked": "replicas", "seen": "2 of 3"}}
UNKEPT = {"phase": "start", "instrument": "state_inducer", "ok": False, "error": "TypeError"}
WITHHELD_STUB = {"phase": "start", "ok": False, "error": "Withheld", "why": "the executor declared no secrets"}
NO_TEXT_UNDECLARED = {"phase": "start"}  # a failed start that gave no diagnostic, whose executor declared no secrets
NO_TEXT_DECLARED = {"phase": "start", "instrument": "state_inducer"}  # the same, from an executor that declared


def run(key: str, condition: str) -> dict:
    return {"run_key": key, "digest": key * 2, "condition": condition, "factors": {"model": "m1", "task": key}}


def export(**changes) -> dict:
    return {
        "planned_runs": 3,
        "exhausted_runs": [],
        "unfinished_runs": [],
        "analysed_attempts": [run("a", "mount-read"), run("b", "api-none"), run("c", "download-write")],
        "infrastructure_by_condition": [],
        "retries_by_kind": [],
        "budget_holds": [],
        **changes,
    }


def kept(attempt_id: int, condition: str, diagnostic: dict, *, charged: bool = True, state: str = "infra_fail", **more):
    base = {
        "attempt_id": attempt_id,
        "run_key": "a",
        "condition": condition,
        "model": "m1",
        "attempt": 1,
        "state": state,
        "infra_kind": "sandbox",
        "charged": charged,
        "diagnostic": diagnostic,
    }
    return {**base, **more}


def render(data: dict) -> str:
    return render_report(RESULT, data, study="a-study", code=CODE, core_version="1.2.3")


def section_of(text: str) -> str:
    return text.split(HEADING, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]


def rows(section: str) -> list[str]:
    return [line for line in section.splitlines() if line.startswith("| ") and not line.startswith("| Condition")]


def test_c40_the_report_counts_the_failed_starts_per_condition_instrument_kind_and_state():
    """C40: attempts are counted with how many were charged and how many snapshots could not be kept; an error from
    elsewhere with a diagnostic has no instrument (`-`); a condition with none has a row of zeros."""
    data = export(
        attempt_diagnostics=[
            kept(1, "mount-read", STARTED),
            kept(2, "mount-read", STARTED, charged=False),
            kept(3, "mount-read", UNKEPT),
            kept(4, "api-none", {"diagnostic": {"seen": "x"}}),
        ]
    )
    section = section_of(render(data))
    assert (
        "| Condition | Instrument | Kind | State | Attempts | Charged | Shown | Withheld | No diagnostic | Not kept |"
        in section
    )
    assert rows(section) == [
        "| api-none | - | sandbox | infra_fail | 1 | 1 | 1 | 0 | 0 | 0 |",
        "| download-write | - | - | - | 0 | 0 | 0 | 0 | 0 | 0 |",
        "| mount-read | state_inducer | sandbox | infra_fail | 3 | 2 | 2 | 0 | 0 | 1 |",
    ]


def test_c40_the_section_follows_the_infrastructure_tables_and_says_where_the_snapshots_are():
    text = render(export(attempt_diagnostics=[kept(1, "mount-read", STARTED)]))
    assert text.index("### Infrastructure failures by condition and kind") < text.index(HEADING)
    assert text.index(HEADING) < text.index("## Attempts left out of the analysis")
    assert "attempt_diagnostics" in section_of(text)


def test_c40_a_merged_batchs_rows_name_the_shard():
    data = export(
        attempt_diagnostics=[kept(1, "mount-read", STARTED, shard=1), kept(1, "mount-read", STARTED, shard=2)]
    )
    section = section_of(render(data))
    assert "| Shard | Condition | Instrument |" in section
    assert [r for r in rows(section) if r.startswith(("| 1 |", "| 2 |"))] == [
        "| 1 | mount-read | state_inducer | sandbox | infra_fail | 1 | 1 | 1 | 0 | 0 | 0 |",
        "| 2 | mount-read | state_inducer | sandbox | infra_fail | 1 | 1 | 1 | 0 | 0 | 0 |",
    ]


def test_c40_a_report_of_an_export_without_the_key_is_the_text_it_was():
    """C40: no heading and no word of it without the key, and an empty list is the same as none."""
    plain = render(export())
    assert HEADING not in plain
    assert "diagnostic" not in plain
    assert render(export(attempt_diagnostics=[])) == plain


def test_c40_a_withheld_diagnostic_is_counted_apart_with_its_reason_and_never_its_text():
    """C40: an attempt whose executor declared no secrets has a stub in the export; the report counts it in Withheld
    (not in Not kept: it failed to be shown, not to be kept), names no instrument, and says why in the notes."""
    data = export(
        attempt_diagnostics=[
            kept(1, "mount-read", WITHHELD_STUB),
            kept(2, "mount-read", WITHHELD_STUB, charged=False),
            kept(3, "mount-read", UNKEPT),
        ]
    )
    section = section_of(render(data))
    assert "| mount-read | - | sandbox | infra_fail | 2 | 1 | 0 | 2 | 0 | 0 |" in rows(section)
    assert "| mount-read | state_inducer | sandbox | infra_fail | 1 | 1 | 0 | 0 | 0 | 1 |" in rows(section)
    notes = " ".join(section.split())
    assert "declared no secrets" in notes
    assert "declare_secrets" in notes
    assert "local log" in notes


def test_c40_a_failed_start_that_gave_no_diagnostic_is_counted_apart_from_a_withheld_one(tmp_path):
    """C40: an undeclared executor's start that gave no diagnostic has the phase alone as its snapshot: nothing was
    withheld from it, since nothing was given, and nothing failed to be kept, so it is counted in its own column and
    the notes say so; a withheld diagnostic and an unkept one stay where they were."""
    data = export(
        attempt_diagnostics=[
            kept(1, "mount-read", NO_TEXT_UNDECLARED),
            kept(2, "mount-read", WITHHELD_STUB),
            kept(3, "mount-read", UNKEPT),
        ]
    )
    section = section_of(render(data))
    assert "| mount-read | - | sandbox | infra_fail | 2 | 2 | 0 | 1 | 1 | 0 |" in rows(section)
    assert "| mount-read | state_inducer | sandbox | infra_fail | 1 | 1 | 0 | 0 | 0 | 1 |" in rows(section)
    notes = " ".join(section.split())
    assert "No diagnostic counts" in notes
    assert "Attempts is the sum of Shown, Withheld, No diagnostic and Not kept" in notes


def cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip("|").split("|")]


def test_c40_the_four_categories_sum_to_the_failed_starts_the_section_shows():
    """C40: over a mix of declared and undeclared executors (a diagnostic shown, a start with none, a withheld one, an
    unkept one, an error from elsewhere that carried one), every row's Shown, Withheld, No diagnostic and Not kept add
    up to its Attempts, and the table's Attempts add up to the failed starts of the export: none counted twice or in
    none, and each count is what the mix holds."""
    mix = [
        kept(1, "mount-read", STARTED),  # declared, a diagnostic: shown
        kept(2, "mount-read", NO_TEXT_DECLARED),  # declared, none given
        kept(3, "mount-read", NO_TEXT_UNDECLARED),  # undeclared, none given
        kept(4, "mount-read", WITHHELD_STUB),  # undeclared, a diagnostic: withheld
        kept(5, "mount-read", UNKEPT),  # declared, could not be kept
        kept(6, "api-none", {"diagnostic": {"seen": "x"}}),  # an error from elsewhere: shown, no instrument
        kept(7, "api-none", NO_TEXT_UNDECLARED, shard=2),
        kept(8, "api-none", WITHHELD_STUB, shard=2),
    ]
    section = section_of(render(export(attempt_diagnostics=mix)))
    table = [cells(row) for row in rows(section) if not row.startswith("| Shard")]
    header = cells(next(line for line in section.splitlines() if line.startswith("| Shard")))
    column = {name: header.index(name) for name in ("Attempts", "Shown", "Withheld", "No diagnostic", "Not kept")}
    for row in table:
        assert int(row[column["Attempts"]]) == sum(int(row[column[name]]) for name in list(column)[1:]), row
    total = {name: sum(int(row[index]) for row in table) for name, index in column.items()}
    assert total == {"Attempts": len(mix), "Shown": 2, "Withheld": 2, "No diagnostic": 3, "Not kept": 1}
