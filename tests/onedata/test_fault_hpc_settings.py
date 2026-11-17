"""O58: `[faults]` of an HPC federation (`backend = "hpc"`): its keys, its defaults and refusals, the least token margin
its stop and relay add, the heal's reserves, and the driver the platform builds for it. A table without `backend` is
the Compose one, loaded exactly as before.
"""

import hashlib
import json

import pytest
from fault_support import SERVICES, write_faults
from hpc_support import write_hpc_platform

from expcore.config.loading import ConfigError
from expcore_onedata import health as health_module
from expcore_onedata.client import OnezoneClient
from expcore_onedata.compose import CommandResult
from expcore_onedata.config import load_platform
from expcore_onedata.fault_hpc import HPC_COMMAND_TIMEOUT_S, HPC_STOP_BOUND_S, HpcDriver
from expcore_onedata.fault_settings import (
    DEFAULT_SETTLE_TIMEOUT_S,
    compose_driver,
    faults_fingerprint,
    heal_container_reserve,
    heal_link_reserve,
    hpc_driver,
    load_faults,
    restore_margin_derivation,
    restore_token_margin_s,
)
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import ADMIN_ENV, NOW, make_federation
from expcore_onedata.tokens import TokenIssuer

# Every bound left at its default: what `up` renders for the layout's stop_seconds of 50.
DEFAULTS = dict.fromkeys(
    ("stop_bound_s", "settle_timeout_s", "relay_wait_s", "poll_s", "ready_timeout_s", "clear_attempts")
)
DEFAULTS.update(session_drain_s=None, admission_timeout_s=None)


def settings_of(platform):
    return load_faults(platform, load_platform(platform, require_secrets=False))


def test_o58_a_table_without_backend_is_the_compose_one_loaded_as_before(tmp_path):
    """O58, C06: no `backend` key is `"compose"`: its file, project and defaults as before, no HPC bound read, and its
    fingerprint hashes each target's backend and service, never its project (each deployment's own name)."""
    platform = write_faults(tmp_path)
    settings = settings_of(platform)
    assert (settings.backend, settings.stop_bound_s, settings.relay_wait_s) == ("compose", 0.0, 0.0)
    assert settings.compose_file == (tmp_path / "compose.yaml").resolve()
    assert (settings.project, settings.poll_s) == ("stack-a", 1)
    assert DEFAULT_SETTLE_TIMEOUT_S == 180.0
    targets = faults_fingerprint(load_platform(platform, require_secrets=False))["targets"]
    assert targets["providers[1]"] == hashlib.sha256(f"compose:{SERVICES['beta']}".encode()).hexdigest()


def test_o58_an_hpc_table_has_no_compose_file_and_its_own_defaults(tmp_path):
    """O58: `backend = "hpc"`: no Compose file or project; a service's stop bounded at 140 s (the layout's 50 s stop,
    the kill's 30 s, the step's end 60 s) and waited for 60 s more (200 s), a reading every 10 s (each is a Slurm
    step), 30 s for the relay beyond each command's bound, and each command (a relay verb) bounded at 240 s."""
    settings = settings_of(write_hpc_platform(tmp_path, **DEFAULTS))
    assert (settings.backend, settings.compose_file, settings.project) == ("hpc", None, "")
    assert HPC_STOP_BOUND_S == 140.0
    assert (settings.stop_bound_s, settings.settle_timeout_s) == (140.0, 200.0)
    assert (settings.poll_s, settings.relay_wait_s) == (10.0, 30.0)
    assert HPC_COMMAND_TIMEOUT_S == settings.command_timeout_s == 240.0


@pytest.mark.parametrize(
    ("key", "value"),
    [("compose_file", "compose.yaml"), ("project", "stack-a"), ("command", ["docker", "compose"]), ("docker", ["x"])],
)
def test_o58_an_hpc_table_refuses_every_compose_key_naming_it(tmp_path, key, value):
    """O58: a Compose key in an HPC table would do nothing, so it is refused, by its name."""
    with pytest.raises(ConfigError, match=f"key '{key}' is read only with backend = \"compose\""):
        settings_of(write_hpc_platform(tmp_path, **{key: value}))


@pytest.mark.parametrize(("key", "value"), [("stop_bound_s", 140), ("relay_wait_s", 30)])
def test_o58_a_compose_table_refuses_the_hpc_keys_naming_them(tmp_path, key, value):
    with pytest.raises(ConfigError, match=f"key '{key}' is read only with backend = \"hpc\""):
        settings_of(write_faults(tmp_path, **{key: value}))


def test_o58_a_backend_that_is_not_known_is_refused(tmp_path):
    with pytest.raises(ConfigError, match='key \'backend\' must be one of "compose", "hpc"'):
        settings_of(write_faults(tmp_path, backend="slurm"))


@pytest.mark.parametrize("settle", [140, 100, 5])
def test_o58_an_hpc_settle_that_does_not_outlast_the_stop_bound_is_refused_naming_the_figure(tmp_path, settle):
    """O58: a stop of unknown outcome is waited for `settle_timeout_s`; one at or below the stop's own bound would
    never see a slow stop end, so it is refused at load, naming the bound and the default."""
    with pytest.raises(ConfigError, match=r"settle_timeout_s.*must exceed stop_bound_s, 140 s.*leave it out for 200"):
        settings_of(write_hpc_platform(tmp_path, **{**DEFAULTS, "settle_timeout_s": settle}))
    assert settings_of(write_hpc_platform(tmp_path, **{**DEFAULTS, "settle_timeout_s": 141})).settle_timeout_s == 141


def test_o58_the_least_token_margin_on_hpc_counts_the_relay_and_the_starts_stop(tmp_path):
    """O58: with every default, the least `[tokens] margin_s` is 2,950 s (2,134 s on Compose): the restore's bound with
    each start's command (240 s) and the relay's wait (2,100 s), the slack (300 s), the session drain (320 s), and the
    injector's start's stop, which may run to its own bound and the relay's wait (230 s). A platform declaring less is
    refused, naming the figure and the derivation."""
    settings = settings_of(write_hpc_platform(tmp_path, **DEFAULTS))
    assert restore_token_margin_s(settings) == 2950.0
    derivation = restore_margin_derivation(settings)
    assert "command_timeout_s 240 s + relay_wait_s 30 s" in derivation
    assert "the injector's start's stop (settle_timeout_s 200 s + relay_wait_s 30 s)" in derivation
    assert derivation.endswith("= 2950")
    with pytest.raises(ConfigError, match=r"margin_s = 2949 is too small.*set: \[tokens\] margin_s = 2950"):
        settings_of(write_hpc_platform(tmp_path, margin_s=2949, **DEFAULTS))
    assert settings_of(write_hpc_platform(tmp_path, margin_s=2950, **DEFAULTS)).backend == "hpc"


def test_o58_the_heals_reserves_count_the_relay_the_exec_and_the_stop_on_hpc_and_not_on_compose(tmp_path):
    """O58: a link restart's reserve on HPC holds each command's bound (240 s) and relay wait and the exec's step
    (1,230 s against 690 s on Compose), and a reader's restart its stop's own bound (2,610 s against 1,560 s)."""
    root = tmp_path / "hpc"
    root.mkdir()
    hpc = settings_of(write_hpc_platform(root, **DEFAULTS))
    bounds = {"command_timeout_s": 120, "settle_timeout_s": 180, "ready_timeout_s": 180}
    compose = settings_of(write_faults(tmp_path, margin_s=100_000, **bounds))
    assert (heal_link_reserve(hpc), heal_link_reserve(compose)) == (1230.0, 690.0)
    assert (heal_container_reserve(hpc), heal_container_reserve(compose)) == (2610.0, 1560.0)


def test_o58_a_relay_verb_is_bounded_at_240_s_on_hpc_and_a_compose_command_stays_at_120_s(tmp_path):
    """O58: the relay's per-verb bound is `[faults] command_timeout_s`. On the HPC backend it defaults to 240 s
    (`HPC_COMMAND_TIMEOUT_S`): three provider outages on one HPC federation (17 Nov) showed relay verb steps started
    47 to 103 s after they were issued, and one round trip took 104.6 s against the former 120 s. A declared value is
    kept as declared, and a Compose table keeps its 120 s."""
    hpc, compose = tmp_path / "hpc", tmp_path / "compose"
    hpc.mkdir()
    compose.mkdir()
    assert HPC_COMMAND_TIMEOUT_S == 240.0
    assert settings_of(write_hpc_platform(hpc, **DEFAULTS)).command_timeout_s == 240.0
    assert settings_of(write_hpc_platform(hpc, **{**DEFAULTS, "command_timeout_s": 90})).command_timeout_s == 90.0
    assert settings_of(write_faults(compose)).command_timeout_s == 120.0


def test_o58_the_relay_verb_bound_is_in_the_faults_fingerprint_after_its_default(tmp_path):
    """O58: a run's bounds are part of the federation's identity: the HPC relay's verb bound is recorded after its
    default (240.0), so a platform that leaves it out and one that declares 240 are one federation, and one that
    declares the former 120 is another."""

    def recorded(name, **changes):
        root = tmp_path / name
        root.mkdir()
        platform = write_hpc_platform(root, **{**DEFAULTS, **changes})
        return faults_fingerprint(load_platform(platform, require_secrets=False))["settings"]

    left_out = recorded("a")
    declared, former = recorded("b", command_timeout_s=240), recorded("c", command_timeout_s=120)
    assert left_out["command_timeout_s"] == 240.0
    assert declared == left_out
    assert former["command_timeout_s"] == 120.0
    assert former != left_out


def test_o58_the_hpc_driver_hands_the_relay_240_s_for_each_verb_and_a_stop_its_settle_bound(tmp_path):
    """O58: the driver `[faults]` describes sends a status, a start and a ping within `command_timeout_s` (240 s), a
    stop within `settle_timeout_s` (200 s, above the stop's own 140 s) and a link restart within its own RPC bound
    (120 s, 30 s and the exec's 60 s step), which no setting here moves."""
    sent = []

    def transport(request, *, timeout_s):
        sent.append((request.verb, request.command, timeout_s))
        return CommandResult(0, json.dumps({"running": True, "identity": "1:4001:901"}) + "\n")

    settings = settings_of(write_hpc_platform(tmp_path, **DEFAULTS))
    driver = hpc_driver(settings, probe=lambda service: True, transport=transport)
    service = SERVICES["beta"]
    driver.observe(service)
    driver.start(service)
    driver.ping(service)
    driver.stop(service)
    driver.restart_link(service)
    assert sent == [
        ("status", None, 240.0),
        ("start", None, 240.0),
        ("exec", "ping", 240.0),
        ("stop", None, 200.0),
        ("exec", "restart_link", 210.0),
    ]


def test_o58_each_backend_has_its_own_driver_and_refuses_the_other(tmp_path):
    """O58: an HPC table builds the HPC driver, its stop bounded by `settle_timeout_s`; a Compose driver is never built
    from it, nor an HPC driver from a Compose table. The fingerprint names an HPC target by its service alone."""
    platform = write_hpc_platform(tmp_path)
    settings = settings_of(platform)
    driver = hpc_driver(settings, probe=lambda service: True)
    assert isinstance(driver, HpcDriver)
    with pytest.raises(ConfigError, match='backend = "hpc" has no Compose driver'):
        compose_driver(settings, probe=lambda service: True)
    other = tmp_path / "compose"
    other.mkdir()
    with pytest.raises(ConfigError, match='backend = "compose" has no HPC driver'):
        hpc_driver(settings_of(write_faults(other)), probe=lambda service: True)
    targets = faults_fingerprint(load_platform(platform, require_secrets=False))["targets"]
    assert targets["providers[1]"] == hashlib.sha256(f"hpc:{SERVICES['beta']}".encode()).hexdigest()


def test_o58_the_platforms_health_check_recovers_an_hpc_federation_through_the_hpc_driver(tmp_path, monkeypatch):
    """O58: the fault gate that `PlatformHealth` builds for an HPC table recovers through the HPC driver, whose probe
    reads through the check's client, and never builds a Compose driver."""
    federation = make_federation()
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    built = {}

    def spy(settings, *, probe, transport=None):
        built["backend"] = settings.backend
        return HpcDriver(lambda request, *, timeout_s: None, probe=probe)

    def never(*args, **kwargs):
        raise AssertionError("a Compose driver was built for an HPC federation")

    monkeypatch.setattr(health_module, "hpc_driver", spy)
    monkeypatch.setattr(health_module, "compose_driver", never)
    config = load_platform(write_hpc_platform(tmp_path))
    client = OnezoneClient(config, http=federation.client_http())
    PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW)
    assert built == {"backend": "hpc"}
