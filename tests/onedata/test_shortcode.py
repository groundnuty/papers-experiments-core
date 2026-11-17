"""O12: a reason a caller writes into a published record is a short code that holds no name the platform's
configuration gives (`expcore_onedata.shortcode.checked_code`), the rule `shared_admission`'s reason follows."""

import pytest

from expcore_onedata.shortcode import checked_code, configured_names

VALID = "smoke_warmup"
SAYS = "what the hold is for"

NOT_CODES = {
    "empty": "",
    "spaces": "   ",
    "blank": "\n\t",
    "number": 7,
    "none": None,
    "bytes": b"why",
    "sentence": "the smoke warmup",
    "colon": "smoke: warm-up",
    "path": "smoke/warmup",
    "url": "https://example.invalid/warmup",
    "dot": "smoke.warmup",
    "capitals": "Smoke_warmup",
    "underscore-first": "_smoke",
    "digit-first": "9_smoke",
    "hyphen": "smoke-warmup",
    "too-long": "a" * 65,
}


@pytest.mark.parametrize("value", NOT_CODES.values(), ids=NOT_CODES.keys())
def test_o12_a_value_that_is_not_a_short_code_is_refused_naming_the_argument_and_never_echoing_it(config, value):
    """O12: empty, blank, not a text, a sentence, a path, an address, a dotted name, capitals, a first character that
    is not a letter, a hyphen or past 64 characters: each is refused, naming the argument and the form."""
    with pytest.raises(ValueError, match=r"^reason is a short code that says what the hold is for: ") as refused:
        checked_code(value, config, what="reason", says=SAYS)
    if isinstance(value, str) and value.strip():
        assert value not in str(refused.value)


def spelled(name: str) -> str:
    """A configured name as a short code would spell it: lower case, letters and digits only."""
    return "".join(c for c in name.lower() if c.isalnum())


CONFIGURED = {
    "provider-name": lambda c: c.providers["beta"].name,
    "provider-id": lambda c: c.providers["alpha"].id,
    "provider-host": lambda c: c.providers["beta"].url,
    "zone-name": lambda c: c.zone.name,
    "zone-host": lambda c: c.zone.url,
    "space-label": lambda c: c.spaces[0].label,
    "space-name": lambda c: c.spaces[0].space.name,
    "space-id": lambda c: c.spaces[0].space.id,
    "prefix": lambda c: c.spaces[0].space.prefixes[0],
}


@pytest.mark.parametrize("name", CONFIGURED.values(), ids=CONFIGURED.keys())
def test_o12_a_code_that_holds_a_configured_name_is_refused(config, name):
    """O12: a short code holding a name the configuration gives, spelled whole, with underscores between its letters,
    or as the host with its dots made underscores, is refused, naming the configuration and not the code."""
    configured = name(config).split("://")[-1]
    flat = spelled(configured)
    assert flat, "the test's configuration names it"
    dotted = configured.lower().replace(".", "_").replace("-", "_")
    for code in (f"warmup_{flat}", "warmup_" + "_".join(flat), f"warmup_{dotted}"):
        with pytest.raises(ValueError, match="configuration gives") as refused:
            checked_code(code[:64], config, what="reason", says=SAYS)
        assert code[:64] not in str(refused.value)


def test_o12_a_valid_code_is_given_back_as_it_is(config):
    """O12: a short code that holds no configured name comes back exactly as given, up to 64 characters."""
    assert checked_code(VALID, config, what="reason", says=SAYS) == VALID
    assert checked_code("r" * 64, config, what="reason", says=SAYS) == "r" * 64
    assert "smokewarmup" not in configured_names(config)
