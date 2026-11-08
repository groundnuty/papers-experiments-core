"""O04: health compares the providers the federation reports with the declared set and fails on any difference."""

import pytest
from platform_support import NOW, write_platform

from expcore.campaign.healthgate import Health, read_health
from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.health import REFRESH_BEFORE_S, TOKEN_CAP_S, PlatformHealth
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
    assert "declared but not in the support set: beta" in result.detail


def test_o04_an_undeclared_provider_fails_and_is_counted_not_named(tmp_path, fake):
    """O04: a reported provider the file does not know fails the check; the detail counts it and shows no host."""
    fake.add_provider("pr-stranger", "stranger.test")
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "1 reported provider(s) not declared" in result.detail
    assert "stranger" not in result.detail


def test_o04_a_missing_declared_provider_fails(tmp_path, fake):
    """O04: a declared provider the federation does not report is a failure."""
    result = health_for(tmp_path, fake, both_declared)()
    assert result.ok is False
    assert "missing beta" in result.detail


def test_o04_the_wrong_provider_alone_fails_both_ways(tmp_path, fake):
    """O04: one declared and one reported, but not the same: missing and undeclared at once."""
    fake.providers["prmock0001"]["domain"] = "stranger.test"
    result = health_for(tmp_path, fake)()
    assert result.ok is False
    assert "missing alpha" in result.detail
    assert "1 reported provider(s) not declared" in result.detail


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
    assert "offline: alpha" in result.detail
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
