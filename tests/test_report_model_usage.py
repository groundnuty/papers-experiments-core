"""C39: the report says what each model spent, on which basis, and what its requests were served by and retried.

`render_report(..., model_usage=)` takes the figures `campaign.model_usage` reads from the store and the records. Each
figure is shown with its basis (measured, measured plus a request in flight, bound, top-ups, probes), a dollar that is
not known is `n/a` and never 0, a model with no attempt has a row of zeros, and a report given none is the text it was.
"""

from types import SimpleNamespace

import pytest
from campaign_support import LATER, NOW, finished, infra, planned_store, run_one

from expcore.campaign.model_usage import attach_observed, observed_by_model, usage_of
from expcore.campaign.recovery import recover
from expcore.campaign.selection_ledger import Pricing
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price
from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult

CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
RESULT = AnalysisResult((), (), 0.05, {"preregistration_sha256": "e" * 64})
HEADING = "## Model usage and spend"
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=60.0, token_budget=1_900_000, output_cap_chars=100, max_output_tokens=20_000
)
PRICED = ModelSpec(
    key="m1", backend="stub", api_id="m1", status="frozen", price=Price(3.0, 15.0), context_tokens=100_000
)
UNPRICED = ModelSpec(key="m2", backend="stub", api_id="m2", status="frozen")
IDLE = ModelSpec(key="m3", backend="stub", api_id="m3", status="frozen", price=Price(1.0, 2.0), context_tokens=1_000)
TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000}  # 4.5 USD at 3 and 15 a million


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


def render(data: dict, usage=None) -> str:
    return render_report(RESULT, data, study="a-study", code=CODE, core_version="1.2.3", model_usage=usage)


def line(*cells) -> str:
    """A table row as the report writes it."""
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def USD(*figures) -> list[str]:
    """Dollars as the report writes them: six decimals."""
    return [f"{figure:.6f}" for figure in figures]


def section_of(text: str) -> str:
    return text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def rows(section: str, first: str) -> list[str]:
    return [line for line in section.splitlines() if line.startswith(first)]


def turn(served=None, fingerprint=None, retries=()):
    return {"served_model": served, "system_fingerprint": fingerprint, "retries": list(retries)}


def record(model, *turns):
    return SimpleNamespace(factors={"model": model}, transcript=list(turns))


def usage_of_two_models(tmp_path, *, with_records=True):
    """m1 (priced): an analysed run (4.5 USD measured), an attempt that failed at start (measured zero) and an
    interrupted one with no log (its bound, 30 USD); m2 has no price; m3 has no attempt."""
    store, _, manifest = planned_store(tmp_path, models=("m1", "m2"))
    keys = [row.key.text for row in manifest.rows if "model=m1" in row.key.text]
    other = [row.key.text for row in manifest.rows if "model=m2" in row.key.text]
    run_one(store, keys[0], finished(**TOKENS))
    zero = {"model_turns": 0, "input_tokens": 0, "output_tokens": 0, "turn_usage": (), "spend_basis": "measured"}
    run_one(store, keys[1], infra("endpoint", "the fault could not be induced", **zero))
    store.begin_attempt(keys[2], NOW, 1, "")
    recover(store, LATER, 3)
    run_one(store, other[0], finished(input_tokens=2_000, output_tokens=300))
    pricing = Pricing({"m1": PRICED, "m2": UNPRICED, "m3": IDLE}, LIMITS)
    usage = usage_of(store.q, pricing)
    if not with_records:
        return usage
    retried = {"kind": "rate_limit", "wait_s": 1.5}
    records = [
        record("m1", turn("m1-20260101", "fp-a", [retried]), turn("m1-20260101", "fp-a"), turn("m1-20260101", "fp-b")),
        record("m1", turn(None, None), turn("m1-20260101", "fp-a", [retried, {"kind": "server", "wait_s": 2.0}])),
    ]
    return attach_observed(usage, observed_by_model(records))


def test_c39_the_report_has_a_section_with_each_models_spend_on_each_basis(tmp_path):
    """C39: per model, what it spent in all, on the analysed attempts and on the rest, and on each basis: measured,
    measured plus a request in flight, bound, top-ups and probes. The model without a price has `n/a`, never 0."""
    section = section_of(render(export(), usage_of_two_models(tmp_path)))
    spend = section.split("### Spend by model", 1)[1].split("\n### ", 1)[0]
    assert "| Model | Spent (USD) | On analysed attempts | On the rest | Measured | Measured plus in flight |" in spend
    assert rows(spend, "| m1 |") == [
        "| m1 | 34.500000 | 4.500000 | 30.000000 | 4.500000 | 0.000000 | 30.000000 | 0.000000 | 0.000000 |"
    ]
    assert rows(spend, "| m2 |") == ["| m2 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 0.000000 |"]
    assert rows(spend, "| m3 |") == [
        "| m3 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 |"
    ]


def test_c39_each_class_of_attempt_has_its_tokens_and_its_dollars_by_basis(tmp_path):
    """C39: what became of the attempts decides the row; an attempt that failed at start and was charged a measured
    zero has 0 tokens and 0 measured dollars, and is not a bound and not n/a."""
    section = section_of(render(export(), usage_of_two_models(tmp_path)))
    classes = section.split("### Attempts, tokens and spend by what became of the attempt", 1)[1].split("\n### ", 1)[0]
    assert "| Model | Attempts of | Attempts | Input tokens | Output tokens | Cache writes | Cache reads |" in classes
    assert rows(classes, "| m1 | analysed") == [
        line("m1", "analysed runs", 1, 1000000, 100000, 0, 0, 0, 0, *USD(4.5, 0, 0, 0, 4.5))
    ]
    assert rows(classes, "| m1 | infrastructure") == [
        line("m1", "infrastructure failures", 1, 0, 0, 0, 0, 0, 0, *USD(0, 0, 0, 0, 0))
    ]
    assert rows(classes, "| m1 | interrupted") == [
        line("m1", "interrupted attempts", 1, 0, 0, 0, 0, 1, 1, *USD(0, 0, 30, 0, 30))
    ]
    assert rows(classes, "| m2 | analysed") == [
        "| m2 | analysed runs | 1 | 2000 | 300 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a |"
    ]
    assert not rows(classes, "| m3 |")  # a class nobody was charged in is not listed


def test_c39_the_notes_say_what_each_basis_means_and_what_n_a_is(tmp_path):
    """C39: the provenance of the figures is stated beside them: measured, in flight, bound, top-ups, probes, n/a."""
    section = section_of(render(export(), usage_of_two_models(tmp_path)))
    notes = section.split("###", 1)[0]
    for word in ("measured", "bound", "top-up", "probe", "n/a", "gate"):
        assert word in notes


def test_c39_the_provenance_lists_say_which_figures_are_measured_and_which_are_bound(tmp_path):
    """C39: one line in the report's provenance gives the spend and how much of it is measured and how much bound."""
    text = render(export(), usage_of_two_models(tmp_path))
    [line] = [line for line in text.split("## ", 2)[1].splitlines() if line.startswith("- Spend")]
    assert "34.500000 USD on the models with a price" in line  # m2 has no price: not "in all"
    assert "4.500000 measured" in line
    assert "30.000000 at a bound" in line
    assert "n/a for m2" in line
    assert "Model usage and spend" in line


def test_c39_a_campaign_whose_prices_are_unknown_says_why_and_still_gives_the_tokens(tmp_path):
    """C39: with no prices the campaign can be shown to have run under, every dollar is n/a and the reason is in the
    provenance line; the tokens are the store's."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, manifest.rows[0].key.text, finished(**TOKENS))
    usage = usage_of(store.q, None, why="the study's models.toml entries are not the ones the campaign ran under")
    text = render(export(), usage)
    assert "models.toml entries are not the ones the campaign ran under" in text.split("## ", 2)[1]
    classes = section_of(text).split("### Attempts, tokens and spend", 1)[1]
    assert "| m1 | analysed runs | 1 | 1000000 | 100000 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a |" in classes


def test_c39_the_retries_are_shown_per_model_by_kind_with_their_waits(tmp_path):
    """C39: the attempts set aside as infrastructure by kind (from the export), and the retries inside the requests
    (from the records) by kind with their waits; a model with no record has zeros."""
    data = export(
        retries_by_kind=[
            {
                "condition": "a",
                "model": "m1",
                "kind": "endpoint",
                "state": "infra_fail",
                "charged": True,
                "attempts": 2,
            },
            {
                "condition": "b",
                "model": "m1",
                "kind": "endpoint",
                "state": "infra_fail",
                "charged": True,
                "attempts": 1,
            },
            {
                "condition": "b",
                "model": "m1",
                "kind": "sandbox",
                "state": "outage_discarded",
                "charged": False,
                "attempts": 4,
            },
        ]
    )
    section = section_of(render(data, usage_of_two_models(tmp_path)))
    attempts = section.split("### Attempts set aside as infrastructure", 1)[1].split("\n### ", 1)[0]
    assert rows(attempts, "| m1 |") == [
        "| m1 | endpoint | infra_fail | yes | 3 |",
        "| m1 | sandbox | outage_discarded | no | 4 |",
    ]
    assert rows(attempts, "| m2 |") == ["| m2 | - | - | - | 0 |"]
    requests = section.split("### Requests retried inside an attempt", 1)[1].split("\n### ", 1)[0]
    assert rows(requests, "| m1 |") == [
        "| m1 | 2 | 5 | 2 | 3 | 5.00 |",
        "| m1 | rate_limit | 2 | 3.00 |",
        "| m1 | server | 1 | 2.00 |",
    ]
    assert rows(requests, "| m2 |") == ["| m2 | 0 | 0 | 0 | 0 | 0.00 |"]


def test_c39_the_served_model_ids_and_fingerprints_are_counted_and_not_reported_is_said(tmp_path):
    """C39: each served id and fingerprint a model's turns carried, with the turns and the records that carried it; a
    turn that carried neither is `not reported`, never the requested id."""
    section = section_of(render(export(), usage_of_two_models(tmp_path)))
    served = section.split("### Served model ids and fingerprints", 1)[1].split("\n### ", 1)[0]
    assert rows(served, "| m1 |") == [
        "| m1 | not reported | not reported | 1 | 1 |",
        "| m1 | m1-20260101 | fp-a | 3 | 2 |",
        "| m1 | m1-20260101 | fp-b | 1 | 1 |",
    ]
    assert rows(served, "| m2 |") == ["| m2 | - | - | 0 | 0 |"]


def test_c39_without_the_figures_the_report_is_the_text_it_was():
    """C39: no heading, no provenance line and no word of the section for a report given no model usage."""
    plain = render(export())
    assert HEADING not in plain
    assert "- Spend" not in plain
    assert render(export(), None) == plain


def test_c39_the_section_follows_the_conditions_and_precedes_the_attempts_left_out(tmp_path):
    text = render(export(), usage_of_two_models(tmp_path))
    assert text.index("## Conditions") < text.index(HEADING) < text.index("## Attempts left out of the analysis")


@pytest.mark.parametrize(
    "bad", [{"models": [{"model": "m1"}]}, {"format": "model-usage-9", "models": [], "dollars": {}}]
)
def test_c39_figures_of_another_shape_are_refused_not_shown(bad):
    """C39: a figure the report cannot place is an error, never a guessed row."""
    with pytest.raises(ValueError, match="model-usage-1"):
        render(export(), bad)
