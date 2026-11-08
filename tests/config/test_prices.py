"""Prices in the model table: the cost of tokens is known or explicitly free (C12)."""

import pytest

from expcore.config.loading import ConfigError
from expcore.config.models import ModelSpec, ModelTable, load_models
from expcore.config.prices import Price

TABLE = """
[models.paid]
backend = "openai_compat"
api_id = "vendor/paid-1"
base_url = "https://llm.example.invalid/v1"
key_env = "TEST_PAID_KEY"

[models.paid.price]
input_per_mtok = 3.0
output_per_mtok = 15
cache_read_per_mtok = 0.3

[models.hosted]
backend = "openai_compat"
api_id = "vendor/hosted-1"
base_url = "https://llm.example.invalid/v1"
free = true

[models.unknown]
backend = "openai_compat"
api_id = "vendor/unknown-1"
base_url = "https://llm.example.invalid/v1"
"""


def table(tmp_path, text=TABLE):
    path = tmp_path / "models.toml"
    path.write_text(text, encoding="utf-8")
    return load_models(path)


def test_c12_the_price_table_loads_and_a_free_model_is_priced_without_one(tmp_path):
    """C12: a model is priced when it has a price table or is marked free; one with neither has an unknown cost."""
    models = table(tmp_path)
    paid = models.resolve("paid")
    assert paid.price == Price(3.0, 15.0, None, 0.3)
    assert [models.resolve(k).priced for k in ("paid", "hosted", "unknown")] == [True, True, False]


def test_c12_a_study_run_with_an_unpriced_model_is_refused_naming_it(tmp_path):
    """C12: refuse a study run with unpriced models, unless they are explicitly marked free."""
    models = table(tmp_path)
    models.require_priced(["paid", "hosted"])
    with pytest.raises(ConfigError, match=r"no price for model\(s\) unknown:"):
        models.require_priced(["paid", "unknown", "hosted"])


def test_c12_a_price_table_needs_both_main_prices_and_numbers(tmp_path):
    """C12: a price table with a missing, text, negative or unknown entry is refused, naming the file and the key."""
    head = TABLE.split("[models.paid.price]")[0]
    for body, message in (
        ("output_per_mtok = 1.0", "input_per_mtok"),
        ('input_per_mtok = "cheap"\noutput_per_mtok = 1', "input_per_mtok"),
        ("input_per_mtok = -1\noutput_per_mtok = 1", "not below zero"),
        ("input_per_mtok = 1\noutput_per_mtok = 1\nper_image = 2", "per_image"),
    ):
        with pytest.raises(ConfigError, match=message):
            table(tmp_path, head + "[models.paid.price]\n" + body + "\n")


def test_c12_a_model_that_is_free_and_priced_is_refused():
    """C12: free and a price together say two things about one cost."""
    with pytest.raises(ConfigError, match="free or has a price"):
        ModelSpec(key="m", backend="stub", api_id="m", status="frozen", free=True, price=Price(1.0, 1.0))


def test_c12_cost_counts_cache_tokens_as_parts_of_the_input_at_their_own_rates():
    """C12: cache writes and reads are parts of the input tokens, each at its price (the input price if none)."""
    price = Price(input_per_mtok=3.0, output_per_mtok=15.0, cache_write_per_mtok=3.75, cache_read_per_mtok=0.3)
    assert price.usd(1_000_000, 1_000_000) == pytest.approx(18.0)
    assert price.usd(1_000_000, 0, cache_creation_tokens=200_000, cache_read_tokens=500_000) == pytest.approx(
        0.3 * 3.0 + 0.2 * 3.75 + 0.5 * 0.3
    )
    plain = Price(3.0, 15.0)
    assert plain.usd(1_000_000, 0, cache_creation_tokens=1_000_000) == pytest.approx(3.0)
    assert price.dearest_per_token() == pytest.approx(15.0 / 1_000_000)
    with pytest.raises(ValueError, match="parts of the input"):
        price.usd(10, 0, cache_read_tokens=11)


def test_a_table_built_in_memory_reports_unpriced_models_in_the_order_given():
    models = ModelTable({k: ModelSpec(key=k, backend="stub", api_id=k, status="frozen") for k in ("a", "b")})
    with pytest.raises(ConfigError, match=r"no price for model\(s\) b, a"):
        models.require_priced(["b", "a"])
