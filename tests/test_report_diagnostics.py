"""C40: the report counts, per condition, the attempts whose instrument failed to start and kept a diagnostic.

The export lists them (`attempt_diagnostics`, only when an attempt kept one); the report gives a section of its own
after the infrastructure tables with a row of counts per condition, instrument, kind and state, and a row of zeros for
a condition that had none, so 0 is never mistaken for not reported. What each snapshot says is in the export. A report
of an export without the key is the text it was.
"""

from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult

CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
RESULT = AnalysisResult((), (), 0.05, {"preregistration_sha256": "e" * 64})
HEADING = "### Instrument start failures"
STARTED = {"phase": "start", "instrument": "state_inducer", "diagnostic": {"checked": "replicas", "seen": "2 of 3"}}
UNKEPT = {"phase": "start", "instrument": "state_inducer", "ok": False, "error": "TypeError"}


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
    assert "| Condition | Instrument | Kind | State | Attempts | Charged | Not kept |" in section
    assert rows(section) == [
        "| api-none | - | sandbox | infra_fail | 1 | 1 | 0 |",
        "| download-write | - | - | - | 0 | 0 | 0 |",
        "| mount-read | state_inducer | sandbox | infra_fail | 3 | 2 | 1 |",
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
        "| 1 | mount-read | state_inducer | sandbox | infra_fail | 1 | 1 | 0 |",
        "| 2 | mount-read | state_inducer | sandbox | infra_fail | 1 | 1 | 0 |",
    ]


def test_c40_a_report_of_an_export_without_the_key_is_the_text_it_was():
    """C40: no heading and no word of it without the key, and an empty list is the same as none."""
    plain = render(export())
    assert HEADING not in plain
    assert "diagnostic" not in plain
    assert render(export(attempt_diagnostics=[])) == plain
