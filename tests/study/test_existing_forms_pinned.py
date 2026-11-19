"""C41: the forms of a campaign that existed before a run on a lease keep what they write, byte for byte.

A run on a lease (`run --lease`, `expcore-shard/5`) is a new form beside the unsharded campaign, a shard of N
(`expcore-shard/2`), a shard by model (`/3`) and a selection (`/4`). Its code sits in the kernel the old forms use (the
store's attempt numbers, the pin of a shard's facts, the plan check, the start), so this test runs each old form for
real and holds what a host, a clock and an interpreter do not change to `tests/data/existing-forms-pins.json`: the store
schema, the pinned meta rows, the fingerprint but for its environment, the runs, every attempt's number, state and
charge, and the names of the batch's files. The file was made by the kernel before leases existed; its one edit since
is the nullable attempts column `deployment_ids` that B20 adds to each store schema.
"""

import json
from pathlib import Path

import pytest
from existing_forms_support import FORMS, collect
from study_support import clean_environment

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

PINS = Path(__file__).resolve().parents[1] / "data" / "existing-forms-pins.json"


@pytest.fixture(scope="module")
def found(tmp_path_factory, clean_environment) -> dict[str, object]:
    return collect(tmp_path_factory.mktemp("forms"), clean_environment)


@pytest.fixture(scope="module")
def pinned() -> dict[str, object]:
    return json.loads(PINS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("form", FORMS)
def test_c41_an_existing_form_writes_what_it_wrote_before_leases_existed(form, found, pinned):
    """C41: the store schema, the pinned rows, the runs, the attempts' numbers and the batch's files of the form."""
    assert found[form] == pinned[form]


def test_c41_the_pins_cover_exactly_the_old_forms_and_name_their_rules(pinned):
    """C41: four forms, each pinned under its own rule (none for an unsharded campaign), and none of a lease."""
    assert set(pinned) == set(FORMS)
    rules = {
        form: json.loads(facts["meta"]["shard_spec"])["rule"] if "shard_spec" in facts["meta"] else None
        for form, facts in pinned.items()
    }
    assert [rules[form].split(":")[0] if rules[form] else None for form in FORMS] == [
        None,
        "expcore-shard/2",
        "expcore-shard/3",
        "expcore-shard/4",
    ]
    assert "lease_attempt_base" not in {key for facts in pinned.values() for key in facts["meta_keys"]}
