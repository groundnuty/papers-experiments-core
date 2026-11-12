"""O11: the probe behind `ComposeDriver.serves`: the platform's authenticated REST read through the restored provider.

A read that succeeds means the provider serves; a refused connection, a reset, a status of 400 or more mean it does not,
yet. Every test runs against the fake federation's data API; nothing contacts a deployment.
"""

import pytest
from fault_support import SERVICES, FakeServices, faults_table, write_faults

from expcore.config.loading import ConfigError
from expcore_onedata import health as health_module
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_serves import PROBE_TIMEOUT_S, provider_serves
from expcore_onedata.fault_settings import DEFAULT_READY_TIMEOUT_S, load_faults
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import ADMIN_ENV, ALPHA_ID, BETA_ID, NOW, make_federation, write_platform
from expcore_onedata.tokens import TokenIssuer

SUPPORTING, UNSUPPORTING = SERVICES["alpha"], SERVICES["beta"]  # alpha supports the declared space, beta none
SPACE_READ = "/spaces/spmock0001"


@pytest.fixture
def data(monkeypatch):
    federation = make_federation()
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    return federation


@pytest.fixture
def probe_for(tmp_path, data):
    platform = write_faults(tmp_path)
    config = load_platform(platform)
    client = OnezoneClient(config, http=data.client_http())
    return provider_serves(config, load_faults(platform, config), client)


def reads(data):
    return [(provider, route) for provider, method, route in data.provider_calls if method == "GET"]


def test_o11_a_provider_that_supports_a_declared_space_is_asked_for_that_space(probe_for, data):
    """O11: the authenticated read of the space through the provider, as the platform layer already makes it."""
    assert probe_for(SUPPORTING) is True
    assert reads(data) == [(ALPHA_ID, SPACE_READ)]
    assert data.requests[-1].token == data.admin_token  # authenticated as the platform's admin credential


def test_o11_a_provider_that_supports_no_declared_space_is_asked_for_its_configuration(probe_for, data):
    """O11: the provider has no space of the platform's to read, so its configuration shows it answering."""
    assert probe_for(UNSUPPORTING) is True
    assert reads(data) == [(BETA_ID, "/configuration")]


def test_o11_a_refused_connection_is_not_serving_yet_and_the_next_read_may_find_it_serving(probe_for, data):
    """O11: a provider that refuses connections after its restart answers no, without raising, and yes once it
    accepts them."""
    data.resets.append(["/spaces/", 2, ALPHA_ID])
    assert [probe_for(SUPPORTING) for _ in range(3)] == [False, False, True]


@pytest.mark.parametrize("status", [401, 404, 500, 503])
def test_o11_an_answer_of_400_or_more_is_not_serving_yet(probe_for, data, status):
    """O11: a provider that answers an error (still starting, not yet connected to the zone) does not serve."""
    data.statuses.append(["/spaces/", 1, status])
    assert [probe_for(SUPPORTING) for _ in range(2)] == [False, True]


def test_o11_a_service_no_provider_is_declared_for_is_refused_without_naming_it(probe_for):
    """O12: asking about a service `[faults.services]` does not list is a mistake of the caller, and says so by the
    table, never by the service."""
    with pytest.raises(ConfigError, match=r"\[faults.services\]") as caught:
        probe_for("unlisted-service")
    assert "unlisted-service" not in str(caught.value)


def test_o11_every_read_is_bounded():
    """O11: a provider that accepts a connection and never answers costs one bounded read per poll."""
    assert 0 < PROBE_TIMEOUT_S <= 30


# --- the platform's health check ----------------------------------------------------------------------------------


def test_o11_the_health_checks_compose_driver_probes_through_the_checks_own_client(tmp_path, data, monkeypatch):
    """O11: the fault gate that `PlatformHealth` builds recovers through a Compose driver whose probe reads through the
    check's client; a test replaces the driver, and sees the probe it would have been given."""
    built = {}

    def spy(settings, runner=None, *, probe):
        built["probe"] = probe
        return FakeServices()

    monkeypatch.setattr(health_module, "compose_driver", spy)
    platform = write_faults(tmp_path)
    config = load_platform(platform)
    client = OnezoneClient(config, http=data.client_http())
    PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW)
    assert built["probe"](SUPPORTING) is True
    assert reads(data) == [(ALPHA_ID, SPACE_READ)]


# --- the setting --------------------------------------------------------------------------------------------------


def test_o11_the_ready_timeout_defaults_above_the_measured_start_up_and_is_read_from_the_table(tmp_path, data):
    """O11: 180 s by default, against the 48 to 52 s a restarted provider took in the live run; `ready_timeout_s` in
    `[faults]` sets it."""
    assert DEFAULT_READY_TIMEOUT_S == 180

    def without_the_key(document):
        document["faults"] = {key: value for key, value in faults_table().items() if key != "ready_timeout_s"}

    default = write_platform(tmp_path, without_the_key)
    config = load_platform(default)
    assert load_faults(default, config).ready_timeout_s == 180
    (tmp_path / "other").mkdir()
    other = write_faults(tmp_path / "other", ready_timeout_s=240)
    assert load_faults(other, load_platform(other)).ready_timeout_s == 240


@pytest.mark.parametrize("value", [-1, "soon", True])
def test_o11_a_ready_timeout_that_is_not_a_number_of_at_least_zero_is_refused(tmp_path, data, value):
    """O11: the same rule as the other bounds, naming the key."""
    platform = write_faults(tmp_path, ready_timeout_s=value)
    with pytest.raises(ConfigError, match="ready_timeout_s"):
        load_faults(platform, load_platform(platform))
