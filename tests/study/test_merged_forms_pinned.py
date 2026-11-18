"""C43: a merge of shards and one driver of the whole keep what they write, byte for byte, beside the merge by run.

The merge by run (`merge --ledger`, `expcore-merge/2`) adds its own branches to the check, the analysis, the export and
the report. Each is taken only for a batch merged by run, so today's forms must write what they wrote before it
existed: this test runs two shards of the example study merged, and one driver of the whole, through `check`,
`analyze` and `report`, and compares `merge.json`, `export.json`, `analysis.json` and `report.md` with
`tests/data/merged-forms-pins.json`, made by the core before the merge by run existed. What a clock or a host writes
in them is masked (`merged_forms_support.normalized`); everything else is compared.
"""

import json
from pathlib import Path

import pytest
from merged_forms_support import FORMS, OUTPUTS, collect
from study_support import clean_environment

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

PINS = Path(__file__).resolve().parents[1] / "data" / "merged-forms-pins.json"


@pytest.fixture(scope="module")
def found(tmp_path_factory, clean_environment) -> dict[str, dict[str, str | None]]:
    return collect(tmp_path_factory.mktemp("merged-forms"), clean_environment)


@pytest.fixture(scope="module")
def pinned() -> dict[str, dict[str, str | None]]:
    return json.loads(PINS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("output", OUTPUTS)
@pytest.mark.parametrize("form", FORMS)
def test_c43_an_old_form_writes_what_it_wrote_before_the_merge_by_run(form, output, found, pinned):
    """C43: merge.json, the export, analysis.json and the report of a merge of shards and of one driver."""
    assert found[form][output] == pinned[form][output]


def test_c43_the_pins_cover_both_old_forms_and_every_output(pinned):
    """C43: a merge of shards (with its merge.json) and one driver (without one), each with all four outputs."""
    assert set(pinned) == set(FORMS)
    assert all(set(pinned[form]) == set(OUTPUTS) for form in FORMS)
    assert pinned["merged-shards"]["merge.json"] is not None
    assert pinned["unsharded"]["merge.json"] is None
    assert '"schema": "expcore-merge/1"' in pinned["merged-shards"]["merge.json"]
