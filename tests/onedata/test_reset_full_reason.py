"""O51 with O12: a caller's reason for a full reset goes into the published record, so it is a short code that holds
no name the platform's configuration gives (`resetshortcut.checked_reason`)."""

import pytest

from expcore_onedata.resetshortcut import checked_reason

VALID = "owner_ledger_fell_back"


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "   ",
        "\n\t",
        7,
        None,
        b"why",
        "the owner ledger fell back",
        "ledger/fell/back",
        "https://zone.test/ledger",
        "ledger.fell",
        "Owner_ledger",
        "_ledger",
        "9_ledger",
        "ledger-fell",
        "a" * 65,
    ],
    ids=[
        "empty",
        "spaces",
        "blank",
        "number",
        "none",
        "bytes",
        "sentence",
        "path",
        "url",
        "dot",
        "capitals",
        "underscore-first",
        "digit-first",
        "hyphen",
        "too-long",
    ],
)
def test_o51_a_full_reason_that_is_not_a_short_code_is_refused(config, reason):
    """O51: empty, blank, not a text, a sentence, a path, an address, a dotted name, capitals, a first character that
    is not a letter, a hyphen or past 64 characters: each is refused, naming the form."""
    with pytest.raises(ValueError, match="full_reason is a short code"):
        checked_reason(reason, config)


def spelled(name: str) -> str:
    """A configured name as a short code would spell it: lower case, letters and digits only."""
    return "".join(c for c in name.lower() if c.isalnum())


@pytest.mark.parametrize(
    "name",
    [
        lambda c: c.providers["beta"].name,
        lambda c: c.providers["alpha"].id,
        lambda c: c.providers["beta"].url,
        lambda c: c.zone.name,
        lambda c: c.zone.url,
        lambda c: c.spaces[0].space.name,
        lambda c: c.spaces[0].space.id,
        lambda c: c.spaces[0].space.prefixes[0],
    ],
    ids=["provider-name", "provider-id", "provider-host", "zone-name", "zone-host", "space-name", "space-id", "prefix"],
)
def test_o51_a_full_reason_that_holds_a_configured_name_is_refused(config, name):
    """O12: a short code holding a name the configuration gives, spelled whole or with underscores between its
    letters, is refused, naming the configuration."""
    flat = spelled(name(config).split("://")[-1])
    assert flat, "the test's configuration names it"
    for reason in (f"ledger_{flat}", "ledger_" + "_".join(flat)):
        with pytest.raises(ValueError, match="configuration"):
            checked_reason(reason[:64], config)


def test_o51_a_valid_full_reason_is_given_back_as_it_is(config):
    """O51: a short code that holds no configured name comes back exactly as given, up to 64 characters."""
    assert checked_reason(VALID, config) == VALID
    assert checked_reason("r" * 64, config) == "r" * 64
