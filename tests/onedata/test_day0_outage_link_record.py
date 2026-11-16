"""O35: the day-0 outage check's restore facts say when a link check's record was not written, and its report shows it.

The check reads what the injector did from the injector's snapshot (`day0.outage_fault.Outage.restored`). A restore's
link check writes the federation's record, and one that could not be serialized or written leaves none: the snapshot
says so, and so must the facts the day-0 report holds, in a line of their own, not in the log alone. These tests give
`restored` the snapshot a restore leaves, with and without the fact."""

from expcore_onedata.day0.outage_fault import Outage
from expcore_onedata.day0.report import render_markdown
from expcore_onedata.linkrecord import fact

STEPS = [
    {"step": "cleared", "ok": True, "how": "restored"},
    {"step": "verified", "ok": True, "identity_changed": True, "identity_after_sha256": "ab" * 32, "ready_after_s": 1},
]


class Injector:
    """What `Outage` asks of the injector once the outage has ended: its snapshot."""

    def __init__(self, **top) -> None:
        self._top = top

    def snapshot(self):
        return {"steps": list(STEPS), **self._top}


def restored(**top):
    return Outage(Injector(**top), "run", None).restored()  # type: ignore[arg-type]


def test_o35_a_restore_whose_link_record_was_written_has_no_such_fact():
    """O35: both keys are absent from the restore's facts, not null, when the record was written."""
    facts = restored()
    assert facts["verified"] is True
    assert "link_record" not in facts
    assert "link_record_reason" not in facts


def test_o35_a_restore_whose_link_record_was_not_written_says_so_with_the_reason():
    """O35: the facts hold `link_record: "not_written"` and why, and the restore is verified all the same."""
    facts = restored(**fact("lock_not_taken"))
    assert facts["verified"] is True  # the record is advisory: the restore stands
    assert (facts["link_record"], facts["link_record_reason"]) == ("not_written", "lock_not_taken")


def test_o35_the_day0_report_shows_the_fact_as_one_line_of_the_restore_facts():
    """O35: the report lists each fact of the restore as a bullet, so the record not written is one line of it."""
    facts = restored(**fact("write_failed"))
    report = {
        "utc": "20261116T000000Z",
        "counts": {"pass": 1, "fail": 0, "unknown": 0},
        "providers": ["providers[0]"],
        "mutation_allowed": True,
        "platform_file_sha256": "0" * 64,
        "checks": [
            {"name": "outage", "title": "Outage", "status": "pass", "summary": "ok", "facts": {"restore": facts}},
        ],
    }
    lines = render_markdown(report).splitlines()
    assert '  - link_record: "not_written"' in lines
    assert '  - link_record_reason: "write_failed"' in lines
