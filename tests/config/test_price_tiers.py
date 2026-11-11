"""Price tiers by prompt length: each request is priced at the tier its prompt fell in (C24)."""

import dataclasses

import pytest

from expcore.config.loading import ConfigError
from expcore.config.models import ModelSpec, load_models
from expcore.config.prices import Price, PriceTier, TieredPrice
from expcore.study.pipeline import _digest

HEAD = """
[models.long]
backend = "openai_compat"
api_id = "vendor/long-1"
base_url = "https://llm.example.invalid/v1"
key_env = "TEST_LONG_KEY"
context_tokens = 1000000
"""
LOWER = """
[[models.long.price.tiers]]
up_to_prompt_tokens = 200000
input_per_mtok = 1.25
output_per_mtok = 10.0
cache_read_per_mtok = 0.125
"""
UPPER = """
[[models.long.price.tiers]]
input_per_mtok = 2.5
output_per_mtok = 15.0
cache_read_per_mtok = 0.25
"""
LOW = Price(1.25, 10.0, None, 0.125)
HIGH = Price(2.5, 15.0, None, 0.25)


def table(tmp_path, tiers=LOWER + UPPER):
    path = tmp_path / "models.toml"
    path.write_text(HEAD + tiers, encoding="utf-8")
    return load_models(path)


def test_c24_tiers_load_from_the_model_table_each_with_its_boundary_and_its_prices(tmp_path):
    """C24: a tier has a prompt-token boundary and its own input, output and cache prices; the last has none."""
    assert table(tmp_path).resolve("long").price == TieredPrice((PriceTier(200_000, LOW), PriceTier(None, HIGH)))
    assert table(tmp_path).resolve("long").priced


def test_c24_a_request_is_priced_at_the_tier_its_prompt_fell_in(tmp_path):
    """C24: a prompt at the boundary is in the lower tier, one token more in the next; the whole request, its reply
    and its cache reads included, takes that tier's prices."""
    price = table(tmp_path).resolve("long").price
    assert (price.for_prompt(1), price.for_prompt(200_000), price.for_prompt(200_001)) == (LOW, LOW, HIGH)
    assert price.request_usd(200_000, 1000) == pytest.approx((200_000 * 1.25 + 1000 * 10.0) / 1e6)
    assert price.request_usd(200_001, 1000) == pytest.approx((200_001 * 2.5 + 1000 * 15.0) / 1e6)
    assert price.request_usd(300_000, 0, 0, 100_000) == pytest.approx((200_000 * 2.5 + 100_000 * 0.25) / 1e6)


def test_c24_a_run_is_priced_request_by_request_and_a_tiered_price_refuses_to_price_totals(tmp_path):
    """C24: two requests either side of the boundary cost each its own tier; the totals at either tier would be
    wrong, so pricing totals is refused rather than guessed."""
    price = table(tmp_path).resolve("long").price
    exact = LOW.usd(150_000, 2000) + HIGH.usd(250_000, 3000, 0, 100_000)
    assert price.requests_usd([(150_000, 2000, 0, 0), (250_000, 3000, 0, 100_000)]) == pytest.approx(exact)
    assert exact != pytest.approx(LOW.usd(400_000, 5000, 0, 100_000))
    assert exact != pytest.approx(HIGH.usd(400_000, 5000, 0, 100_000))
    with pytest.raises(ValueError, match="per request"):
        price.usd(400_000, 5000)


def test_c24_the_ceilings_of_a_tiered_price_are_taken_over_every_tier():
    """C24: what bounds an unknown count is the dearest rate of any tier, a cache write's included."""
    price = TieredPrice((PriceTier(100, Price(1.0, 4.0, 5.0)), PriceTier(None, Price(2.0, 3.0))))
    assert price.dearest_per_token() == 5.0 / 1e6
    assert (price.input_ceiling_per_mtok(), price.output_ceiling_per_mtok()) == (5.0, 4.0)
    assert (Price(1.0, 0.5, 2.0, 3.0).input_ceiling_per_mtok(), Price(1.0, 0.5).output_ceiling_per_mtok()) == (3.0, 0.5)


THIRD = "\n[[models.long.price.tiers]]\ninput_per_mtok = 3.0\noutput_per_mtok = 20.0\n"


@pytest.mark.parametrize(
    ("tiers", "fragment"),
    [
        (UPPER, "at least two tiers"),
        (LOWER + UPPER.replace("input_per_mtok = 2.5", "up_to_prompt_tokens = 900000\ninput_per_mtok = 2.5"), "last"),
        (LOWER.replace("up_to_prompt_tokens = 200000\n", "") + UPPER, "up_to_prompt_tokens"),
        (LOWER.replace("200000", "0") + UPPER, "positive integer"),
        (LOWER.replace("200000", "2.5e5") + UPPER, "key 'up_to_prompt_tokens' must be an integer"),
        (LOWER + UPPER.replace("input_per_mtok = 2.5", "up_to_prompt_tokens = 100000\ninput_per_mtok = 2.5") + THIRD,
         "increase"),
        (LOWER.replace("input_per_mtok = 1.25\n", "") + UPPER, "key 'input_per_mtok' is required"),
        (LOWER.replace("cache_read_per_mtok = 0.125", "per_image = 1") + UPPER, "key 'per_image'"),
        ("\n[models.long.price]\ninput_per_mtok = 1.0\noutput_per_mtok = 2.0\n" + LOWER + UPPER, "tiers"),
        ("\n[models.long.price]\ntiers = 3\n", "array of tables"),
    ],
)  # fmt: skip
def test_c24_tiers_that_do_not_price_every_prompt_once_are_refused_naming_the_file(tmp_path, tiers, fragment):
    """C24: one tier, a boundary on the last tier or none on another, one that is not a positive integer or does not
    increase, a missing price or an unknown key, flat prices beside tiers, and tiers that are not tables: each is
    refused when the table is loaded, naming the file and the model."""
    with pytest.raises(ConfigError) as caught:
        table(tmp_path, tiers)
    message = str(caught.value)
    assert message.startswith(f"{tmp_path / 'models.toml'}: [models.long")
    assert fragment in message


def test_c24_a_model_without_tiers_keeps_its_price_and_its_fingerprint():
    """C24 and C06: a flat price has the fields it had before tiers existed, so the digest the campaign fingerprint
    takes of the model entry is unchanged and a campaign started earlier still resumes."""
    spec = ModelSpec(
        key="flat-model",
        backend="openai_compat",
        api_id="vendor/flat-1",
        status="frozen",
        base_url="https://llm.example.invalid/v1",
        key_env="TEST_FLAT_KEY",
        context_tokens=128000,
        price=Price(0.5, 2.5, None, 0.05),
    )
    before = "8bc96d05e697f299fe2e848ac8ef915dadef06b1d8471d9656dc053da4678af2"  # computed before tiers existed
    assert _digest(dataclasses.asdict(spec)) == before
