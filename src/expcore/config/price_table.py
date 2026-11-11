# Provenance: new
"""Reading a model entry's `price` table: flat prices, or tiers by the prompt's length (C12, C24).

    [models.<key>.price]               # flat: one price for every request
    input_per_mtok = 0.5
    output_per_mtok = 2.5
    cache_write_per_mtok = 0.625       # optional: the input price when left out
    cache_read_per_mtok = 0.05         # optional: the input price when left out

    [[models.<key>.price.tiers]]       # tiered, in place of the flat keys: two or more tiers
    up_to_prompt_tokens = 128000       # the longest prompt this tier takes; increasing from tier to tier
    input_per_mtok = 0.25
    output_per_mtok = 1.25

    [[models.<key>.price.tiers]]       # the last tier has no boundary: it takes every longer prompt
    input_per_mtok = 0.5
    output_per_mtok = 2.5

Every failure names the file and the table, and the key where there is one.
"""

from expcore.config.loading import Section
from expcore.config.prices import Price, PriceTier, TieredPrice

PRICE_KEYS = ("input_per_mtok", "output_per_mtok", "cache_write_per_mtok", "cache_read_per_mtok")
TIER_KEYS = ("up_to_prompt_tokens", *PRICE_KEYS)


def read_price(section: Section) -> Price | TieredPrice | None:
    """Read the optional `price` table of a model entry; None when the entry has none."""
    table = section.table("price")
    if not table:
        return None
    where = section.section("price")
    if "tiers" in table:
        return _tiered(section, where)
    where.reject_unknown([*PRICE_KEYS, "tiers"])
    return _flat(section, where)


def _flat(entry: Section, where: Section) -> Price:
    """Read one table of prices; its input and output prices are required."""
    values: dict[str, float] = {}
    for key in PRICE_KEYS:
        if key in where.data:
            value = where.data[key]
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise where.fail(key, f"must be a number of dollars per million tokens, got {type(value).__name__}")
            values[key] = float(value)
    for key in ("input_per_mtok", "output_per_mtok"):
        if key not in values:
            raise where.fail(key, "is required in a price table")
    try:
        return Price(**values)
    except ValueError as error:
        raise entry.fail("price", str(error)) from None


def _tiered(entry: Section, where: Section) -> TieredPrice:
    """Read `[[models.<key>.price.tiers]]`: every tier but the last has `up_to_prompt_tokens`, each its own prices."""
    others = sorted(set(where.data) - {"tiers"})
    if others:
        raise where.fail(others[0], "is not allowed beside tiers: a tiered price gives its prices in each tier")
    tiers = where.data["tiers"]
    if not isinstance(tiers, list) or not all(isinstance(tier, dict) for tier in tiers):
        raise where.fail("tiers", f"must be an array of tables, [[{where.where[1:-1]}.tiers]], one per tier")
    built = []
    for number, data in enumerate(tiers, start=1):
        tier = Section(data, where.file, f"{where.where[:-1]}.tiers] tier {number}")
        tier.reject_unknown(TIER_KEYS)
        built.append(PriceTier(tier.optional_integer("up_to_prompt_tokens"), _flat(entry, tier)))
    try:
        return TieredPrice(tuple(built))
    except ValueError as error:
        raise where.fail("tiers", f"cannot price every prompt exactly once: {error}") from None
