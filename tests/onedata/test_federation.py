"""O03: discovery returns every provider of the space; the agent is pinned to its declared local provider or refused."""

import pytest
from platform_support import CEILING_S

from expcore_onedata.federation import (
    FederationUnreadable,
    PinRefused,
    parse_scope,
    pin_local_provider,
    pin_provider,
    read_space,
)
from expcore_onedata.tokens import data_path_spec

SPACE = "spmock0001"


@pytest.fixture
def token(issuer):
    return issuer.mint(data_path_spec(SPACE), "u-svc", ceiling_s=CEILING_S).reveal()


def two_providers(fake):
    fake.add_provider("pr-beta", "beta.test")


def test_o03_discovery_returns_every_provider_of_the_space(client, fake, token):
    """O03: both providers of a two-provider space are found, whichever one the agent will be pinned to."""
    two_providers(fake)
    view = read_space(client, token, SPACE)
    assert {p.domain for p in view.providers} == {"alpha.test", "beta.test"}
    assert view.name == fake.space_name
    assert all(p.online is True for p in view.providers)


def test_o03_a_two_provider_space_yields_only_the_pinned_provider(client, config, fake, token):
    """O03: the agent is pinned to the declared local provider and to no other, whatever order they are listed in."""
    two_providers(fake)
    view = read_space(client, token, SPACE)
    pinned = pin_local_provider(view, config)
    assert pinned.provider.name == "alpha"
    assert pinned.provider.url == "https://alpha.test"
    assert pinned.reported.domain == "alpha.test"
    reversed_view = type(view)(view.space_id, view.name, tuple(reversed(view.providers)))
    assert pin_local_provider(reversed_view, config).provider.name == "alpha"


def test_o03_no_declared_local_provider_is_a_refusal(client, fake, token):
    """O03: without a pin there is no provider to read through, so the agent is refused."""
    two_providers(fake)
    with pytest.raises(PinRefused, match="no local provider"):
        pin_provider(read_space(client, token, SPACE), None)


def test_o03_a_local_provider_the_space_does_not_list_is_a_refusal(client, config, fake, token):
    """O03: when the space does not list the declared provider, the agent is not pinned to some other one."""
    fake.providers.clear()
    fake.supports.clear()
    fake.add_provider("pr-beta", "beta.test")
    with pytest.raises(PinRefused, match="not among the 1 providers"):
        pin_local_provider(read_space(client, token, SPACE), config)


def test_o03_a_declared_id_pins_by_id_not_by_domain(client, config, fake, token):
    """O03: when the file declares the provider's id, only that id matches, even if another provider has the domain."""
    import dataclasses

    two_providers(fake)
    fake.providers["pr-beta"]["domain"] = "alpha.test"  # a second provider claiming the same domain
    view = read_space(client, token, SPACE)
    with pytest.raises(PinRefused, match="more than one"):
        pin_local_provider(view, config)
    alpha = dataclasses.replace(config.providers["alpha"], id="prmock0001")
    assert pin_provider(view, alpha).reported.id == "prmock0001"
    wrong = dataclasses.replace(config.providers["alpha"], id="pr-unknown")
    with pytest.raises(PinRefused, match="not among"):
        pin_provider(view, wrong)


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "text",
        {},
        {"dataAccessScope": {}},
        {"dataAccessScope": {"spaces": {}}},
        {"dataAccessScope": {"spaces": {SPACE: {"name": "n"}}}},
        {"dataAccessScope": {"spaces": {SPACE: {"name": "n", "supports": {}}}}},
        {"dataAccessScope": {"spaces": {SPACE: {"name": "n", "supports": "p"}}}},
        {"dataAccessScope": {"spaces": {SPACE: {"name": "n", "supports": [1, 2]}}}},
        {"dataAccessScope": {"spaces": {SPACE: {"name": "n", "supports": []}}}},
        {"dataAccessScope": {"spaces": {SPACE: {"name": "n", "supports": {"p": {}}}}, "providers": []}},
    ],
)
def test_o03_an_answer_without_the_space_or_its_providers_is_unreadable_not_empty(answer):
    """O03: nothing is guessed from a scope answer that does not describe the space."""
    with pytest.raises(FederationUnreadable):
        parse_scope(answer, SPACE)


def test_o03_a_provider_without_details_is_listed_but_matches_nothing():
    """O03: a supporting provider whose details Onezone omits is still reported, with unknown fields."""
    view = parse_scope({"dataAccessScope": {"spaces": {SPACE: {"name": "n", "supports": {"provider-one": {}}}}}}, SPACE)
    assert [(p.id, p.name, p.domain, p.online) for p in view.providers] == [("provider-one", None, None, None)]


def test_o03_supports_may_be_a_list_of_provider_ids_as_well_as_a_mapping():
    """O03: the earlier code only iterated the supports for ids, so a list of ids is read like a mapping keyed by id."""
    answer = {
        "dataAccessScope": {
            "spaces": {SPACE: {"name": "n", "supports": ["pr-b", "pr-a"]}},
            "providers": {"pr-a": {"domain": "A.test"}, "pr-b": {"domain": "b.test"}},
        }
    }
    view = parse_scope(answer, SPACE)
    assert [(p.id, p.domain) for p in view.providers] == [("pr-a", "a.test"), ("pr-b", "b.test")]
