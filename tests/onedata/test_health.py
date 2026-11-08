"""O04: health compares the providers the federation reports with the declared set and fails on any difference."""

import dataclasses

import pytest
from platform_support import ALPHA_ID, NOW, write_platform

from expcore.campaign.healthgate import Health, read_health
from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.federation import ReportedProvider, SpaceView
from expcore_onedata.health import REFRESH_BEFORE_S, TOKEN_CAP_S, PlatformHealth, compare
from expcore_onedata.tokens import TokenIssuer


def health_for(tmp_path, fake, edit=None, clock=lambda: NOW):
    """A health check over a configuration edited by the test."""
    config = load_platform(write_platform(tmp_path, edit))
    client = OnezoneClient(config, http=fake.client_http())
    return PlatformHealth(config, client, TokenIssuer(client, config, clock=clock), clock=clock)


def both_declared(data):
    data["support"]["providers"] = ["alpha", "beta"]


def test_o04_the_declared_provider_alone_is_healthy(tmp_path, fake):
    """O04: one declared provider and one reported pass."""
    health = health_for(tmp_path, fake)
    assert read_health(health) == Health(True, "ok: the space is supported by exactly the 1 declared provider(s)")


def test_o04_a_declared_multi_provider_support_set_passes(tmp_path, fake):
    """O04: a federation that reports exactly its declared set of two providers is healthy."""
    fake.add_provider("pr-beta", "beta.test")
    assert health_for(tmp_path, fake, both_declared)().ok is True


def test_o04_an_extra_provider_fails_an_exclusive_declaration(tmp_path, fake):
    """O04: with one provider declared, a second supporting provider is a failure, even a declared-elsewhere one."""
    fake.add_provider("pr-beta", "beta.test")
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "declared but not in the support set: providers[1]" in result.detail
    assert "beta" not in result.detail


def test_o04_an_undeclared_provider_fails_and_is_shown_by_its_reported_id(tmp_path, fake):
    """O04: a reported provider the file does not know fails the check; the detail counts it and shows the id the
    federation reports for it, never its host or name."""
    fake.add_provider("pr-stranger", "stranger.test")
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "1 reported provider(s) not declared: pr-stranger" in result.detail
    assert "stranger.test" not in result.detail


def test_o04_a_missing_declared_provider_fails(tmp_path, fake):
    """O04: a declared provider the federation does not report is a failure."""
    result = health_for(tmp_path, fake, both_declared)()
    assert result.ok is False
    assert "missing providers[1]" in result.detail
    assert "beta" not in result.detail


def test_o04_the_wrong_provider_alone_fails_both_ways(tmp_path, fake):
    """O04: one declared and one reported, but not the same id: missing and undeclared at once."""
    fake.providers["pr-stranger"] = fake.providers.pop(ALPHA_ID)
    fake.supports["pr-stranger"] = fake.supports.pop(ALPHA_ID)
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "missing providers[0]" in result.detail
    assert "alpha" not in result.detail
    assert "1 reported provider(s) not declared" in result.detail


def test_o04_two_reported_providers_on_the_declared_host_fail_the_one_host_declaration(tmp_path, fake):
    """O04: a second provider that reports the declared host is matched by its id, which no declaration has, so it
    is undeclared and the check fails; it is not absorbed into the declared provider that shares the host."""
    fake.add_provider("pr-second", "alpha.test")
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "1 reported provider(s) not declared" in result.detail


def test_o04_reported_providers_are_matched_by_id_not_by_host(tmp_path, fake):
    """O04: the declared host reported under an id nobody declared is not the declared provider."""
    fake.providers[ALPHA_ID]["domain"] = "elsewhere.test"  # the declared id on another host still matches by id
    assert health_for(tmp_path, fake)().ok is True
    fake.providers["pr-stranger"] = {"name": "x", "domain": "alpha.test", "online": True}
    fake.supports["pr-stranger"] = {"readonly": False}
    assert "not declared" in health_for(tmp_path, fake)().detail


def test_o04_a_host_only_declaration_that_fits_two_reported_providers_is_ambiguous(tmp_path, fake):
    """O04: a provider declared without an id matches by host; two reported on that host fail as ambiguous."""
    config = load_platform(write_platform(tmp_path))
    host_only = dataclasses.replace(config.providers["alpha"], id=None)
    config = dataclasses.replace(config, providers={**config.providers, "alpha": host_only})
    view = SpaceView(
        "spmock0001",
        config.space.name,
        (
            ReportedProvider("pr-one", "one", "alpha.test", True),
            ReportedProvider("pr-two", "two", "alpha.test", True),
        ),
    )
    result = compare(view, config)
    assert result.ok is False
    assert "ambiguous" in result.detail


def test_o04_one_declared_provider_reported_twice_is_ambiguous(tmp_path, fake):
    """O04: the same declared id listed twice in the scope answer is not one provider matching once."""
    config = load_platform(write_platform(tmp_path))
    twice = ReportedProvider(ALPHA_ID, "a", "alpha.test", True)
    result = compare(SpaceView("spmock0001", config.space.name, (twice, twice)), config)
    assert result.ok is False
    assert "ambiguous" in result.detail


def test_o04_an_unreadable_answer_is_unhealthy_never_a_pass(tmp_path, fake):
    """O04: a scope answer with no supporting provider is a failed check, not an empty federation that matches."""
    fake.supports.clear()
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "lists no supporting provider" in result.detail


def test_o04_a_federation_that_errors_is_unhealthy_and_does_not_raise(tmp_path, fake):
    """O04: any failure of the call, with its cause by name only, is Health(False, ...)."""
    health = health_for(tmp_path, fake)
    fake.status_override = 503
    result = health()
    assert result.ok is False
    assert "503" in result.detail
    assert "zone.test" not in result.detail
    assert fake.admin_token not in result.detail


def test_o04_an_unexpected_exception_is_unhealthy_and_shows_only_its_class(tmp_path, fake):
    """O04: a defect inside the check is reported as a failed check without its text."""
    fake.examine_hook = lambda answer: (_ for _ in ()).throw(RuntimeError("secret detail"))
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "secret detail" not in result.detail


def test_o04_a_space_under_another_name_or_an_offline_provider_fails(tmp_path, fake):
    """O04: the space's name must be the declared one and a declared provider must be online."""
    fake.providers["prmock0001"]["online"] = False
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "offline: providers[0]" in result.detail
    assert "alpha" not in result.detail
    fake.providers["prmock0001"]["online"] = True
    fake.space_name = "renamed-space"
    assert "another name" in health_for(tmp_path, fake)().detail


def test_o04_the_check_reads_with_a_read_only_data_path_token_and_reuses_it(tmp_path, fake):
    """O04: one short-lived token of the service principal, read-only and path-scoped, serves repeated checks."""
    health = health_for(tmp_path, fake)
    assert health().ok
    assert health().ok
    assert len(fake.issued) == 1
    record = next(iter(fake.issued.values()))
    assert record["subject"]["id"] == "u-svc"
    assert [c["type"] for c in record["caveats"]] == ["time", "data.readonly", "interface", "data.path"]


def test_o04_a_token_near_its_end_is_replaced(tmp_path, fake):
    """O04: a cached token is replaced before it expires."""
    now = [NOW]
    health = health_for(tmp_path, fake, clock=lambda: now[0])
    assert health().ok
    first = next(iter(fake.issued))
    now[0] += TOKEN_CAP_S + 900 + 300 - REFRESH_BEFORE_S + 1  # past the point where the token is replaced
    assert health().ok
    assert len(fake.issued) == 2
    assert first in fake.issued


def test_o04_the_check_needs_a_service_principal(tmp_path, fake):
    """O04: without a configured service user there is no token to read with, which fails at construction."""
    with pytest.raises(ConfigError, match="service"):
        health_for(tmp_path, fake, lambda d: d["principals"].pop("service"))


def test_o04_a_provider_without_an_online_flag_is_unhealthy_naming_the_field(tmp_path, fake):
    """O04: nothing is guessed from a scope answer whose provider lacks a field the check reads."""
    del fake.providers["prmock0001"]["online"]
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "'online'" in result.detail


@pytest.mark.parametrize(
    "edit", [lambda d: d["providers"]["alpha"].pop("id"), lambda d: d["providers"]["beta"].update(id=ALPHA_ID)]
)
def test_o04_every_declared_provider_needs_its_own_id(edit, tmp_path, fake):
    """O04: the file must give each declared provider an id, and no two the same, so reports can be matched by id."""
    with pytest.raises(ConfigError, match="id"):
        load_platform(write_platform(tmp_path, edit))
