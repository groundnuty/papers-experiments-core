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
from expcore_onedata.fault_hpc import (
    HPC_COMMAND_TIMEOUT_S,
    HPC_STEP_LAUNCH_WAIT_S,
    HPC_STOP_BOUND_S,
    HPC_VERB_WORK_S,
    HpcDriver,
    verb_inner_worst_s,
)
from expcore_onedata.fault_settings import (
    DEFAULT_SETTLE_TIMEOUT_S,
    command_bound_s,
    compose_driver,
    faults_fingerprint,
    heal_container_reserve,
    heal_link_reserve,
    hpc_driver,
    hpc_verb_bounds,
    link_command_s,
    load_faults,
    restore_margin_derivation,
    restore_token_margin_s,
    stop_command_s,
)
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import ADMIN_ENV, NOW, make_federation
from expcore_onedata.tokens import TokenIssuer

# Every bound left at its default: what `up` renders for the layout's stop_seconds of 50.
DEFAULTS = dict.fromkeys(
    (
        "stop_bound_s",
        "settle_timeout_s",
        "relay_wait_s",
        "step_launch_wait_s",
        "command_timeout_s",
        "poll_s",
        "ready_timeout_s",
        "clear_attempts",
    )
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
    the kill's 30 s, the step's end 60 s), a control step that may wait 240 s to start, and the stop waited for 60 s
    more past both and the stop verb's own tail (450 s), a reading every 10 s (each is a Slurm step), 30 s for the
    relay beyond each command's bound, and each command (a relay verb) bounded at 300 s: its launch wait and 60 s of
    work."""
    settings = settings_of(write_hpc_platform(tmp_path, **DEFAULTS))
    assert (settings.backend, settings.compose_file, settings.project) == ("hpc", None, "")
    assert HPC_STOP_BOUND_S == 140.0
    assert (settings.stop_bound_s, settings.settle_timeout_s) == (140.0, 450.0)
    assert (settings.poll_s, settings.relay_wait_s) == (10.0, 30.0)
    assert HPC_STEP_LAUNCH_WAIT_S == settings.step_launch_wait_s == 240.0
    assert HPC_COMMAND_TIMEOUT_S == settings.command_timeout_s == 300.0


@pytest.mark.parametrize(
    ("key", "value"),
    [("compose_file", "compose.yaml"), ("project", "stack-a"), ("command", ["docker", "compose"]), ("docker", ["x"])],
)
def test_o58_an_hpc_table_refuses_every_compose_key_naming_it(tmp_path, key, value):
    """O58: a Compose key in an HPC table would do nothing, so it is refused, by its name."""
    with pytest.raises(ConfigError, match=f"key '{key}' is read only with backend = \"compose\""):
        settings_of(write_hpc_platform(tmp_path, **{key: value}))


@pytest.mark.parametrize(("key", "value"), [("stop_bound_s", 140), ("relay_wait_s", 30), ("step_launch_wait_s", 240)])
def test_o58_a_compose_table_refuses_the_hpc_keys_naming_them(tmp_path, key, value):
    with pytest.raises(ConfigError, match=f"key '{key}' is read only with backend = \"hpc\""):
        settings_of(write_faults(tmp_path, **{key: value}))


def test_o58_a_backend_that_is_not_known_is_refused(tmp_path):
    with pytest.raises(ConfigError, match='key \'backend\' must be one of "compose", "hpc"'):
        settings_of(write_faults(tmp_path, backend="slurm"))


@pytest.mark.parametrize("settle", [380, 200, 140, 5])
def test_o58_an_hpc_settle_that_does_not_outlast_the_stop_bound_is_refused_naming_the_figure(tmp_path, settle):
    """O58: a stop of unknown outcome is waited for `settle_timeout_s`; one at or below the stop's own bound would
    never see a slow stop end, so it is refused at load, naming the bound and the default."""
    with pytest.raises(
        ConfigError,
        match=r"settle_timeout_s.*must exceed stop_bound_s \+ step_launch_wait_s, 380 s.*leave it out for 450",
    ):
        settings_of(write_hpc_platform(tmp_path, **{**DEFAULTS, "settle_timeout_s": settle}))
    assert settings_of(write_hpc_platform(tmp_path, **{**DEFAULTS, "settle_timeout_s": 381})).settle_timeout_s == 381


def test_o58_the_least_token_margin_on_hpc_counts_the_relay_and_the_starts_stop(tmp_path):
    """O58: with every default, the least `[tokens] margin_s` is 5,370 s (2,134 s on Compose): the restore's bound with
    each start's command (the slowest verb, a start, 630 s) and the relay's wait (4,270 s), the slack (300 s), the
    session drain (320 s), and the injector's start's stop, which may run to its own bound and the relay's wait (480
    s). A platform declaring less is refused, naming the figure and the derivation."""
    settings = settings_of(write_hpc_platform(tmp_path, **DEFAULTS))
    assert restore_token_margin_s(settings) == 5370.0
    derivation = restore_margin_derivation(settings)
    assert "slowest command verb 630 s + relay_wait_s 30 s" in derivation
    assert "the injector's start's stop (stop verb 450 s + relay_wait_s 30 s)" in derivation
    assert derivation.endswith("= 5370")
    with pytest.raises(ConfigError, match=r"margin_s = 5369 is too small.*set: \[tokens\] margin_s = 5370"):
        settings_of(write_hpc_platform(tmp_path, margin_s=5369, **DEFAULTS))
    assert settings_of(write_hpc_platform(tmp_path, margin_s=5370, **DEFAULTS)).backend == "hpc"


def test_o58_the_heals_reserves_count_the_relay_the_exec_and_the_stop_on_hpc_and_not_on_compose(tmp_path):
    """O58: a link restart's reserve on HPC holds each command's bound (the slowest verb, 630 s) and relay wait, and a
    link restart's own (570 s) (2,760 s against 690 s on Compose), and a reader's restart its stop's own bound (5,700
    s against 1,560 s)."""
    root = tmp_path / "hpc"
    root.mkdir()
    hpc = settings_of(write_hpc_platform(root, **DEFAULTS))
    bounds = {"command_timeout_s": 120, "settle_timeout_s": 180, "ready_timeout_s": 180}
    compose = settings_of(write_faults(tmp_path, margin_s=100_000, **bounds))
    assert (heal_link_reserve(hpc), heal_link_reserve(compose)) == (2760.0, 690.0)
    assert (heal_container_reserve(hpc), heal_container_reserve(compose)) == (5700.0, 1560.0)


def test_o58_a_relay_verb_is_bounded_at_its_launch_wait_and_60_s_on_hpc_and_120_s_on_compose(tmp_path):
    """O58: the relay's per-verb bound is `[faults] command_timeout_s`. On the HPC backend it defaults to a control
    step's launch wait and the verb's work (`HPC_VERB_WORK_S`, 60 s): 300 s with the default launch wait, 60 s where
    steps start at once. Three provider outages on one HPC federation (17 Nov) showed relay verb steps started 47 to
    103 s after they were issued (a step's creation can wait on the scheduler's controller up to its message timeout,
    200 s), and one round trip took 104.6 s against the former 120 s. A declared value is kept as declared, and a
    Compose table keeps its 120 s."""
    hpc, compose = tmp_path / "hpc", tmp_path / "compose"
    hpc.mkdir()
    compose.mkdir()
    assert HPC_COMMAND_TIMEOUT_S == HPC_STEP_LAUNCH_WAIT_S + HPC_VERB_WORK_S == 300.0
    assert settings_of(write_hpc_platform(hpc, **DEFAULTS)).command_timeout_s == 300.0
    now = settings_of(write_hpc_platform(hpc, **{**DEFAULTS, "step_launch_wait_s": 0}))
    assert now.command_timeout_s == 60.0
    slow = settings_of(write_hpc_platform(hpc, **{**DEFAULTS, "step_launch_wait_s": 200}))
    assert slow.command_timeout_s == 200 + 60.0
    assert settings_of(write_hpc_platform(hpc, **{**DEFAULTS, "command_timeout_s": 90})).command_timeout_s == 90.0
    assert settings_of(write_faults(compose)).command_timeout_s == 120.0


def test_o58_the_step_launch_wait_is_a_declared_setting_of_the_hpc_backend_only(tmp_path):
    """O58: `step_launch_wait_s` is read with `backend = "hpc"` (240 s by default, 0 or more) and refused on a Compose
    table, naming it; a Compose table's settings hold none."""
    assert settings_of(write_hpc_platform(tmp_path, **DEFAULTS)).step_launch_wait_s == 240.0
    assert (
        settings_of(write_hpc_platform(tmp_path, **{**DEFAULTS, "step_launch_wait_s": 200})).step_launch_wait_s == 200
    )
    with pytest.raises(ConfigError, match="step_launch_wait_s' must be a number of at least 0"):
        settings_of(write_hpc_platform(tmp_path, **{**DEFAULTS, "step_launch_wait_s": -1}))
    other = tmp_path / "compose"
    other.mkdir()
    with pytest.raises(ConfigError, match="key 'step_launch_wait_s' is read only with backend = \"hpc\""):
        settings_of(write_faults(other, step_launch_wait_s=240))
    assert settings_of(write_faults(other)).step_launch_wait_s == 0.0


def test_o58_the_relay_verb_bound_and_the_launch_wait_are_in_the_faults_fingerprint_after_their_defaults(tmp_path):
    """O58: a run's bounds are part of the federation's identity: the HPC relay's verb bound (300.0, the floor of each
    verb's own), the bound each verb gets (`verb_bounds`) and the launch wait they count (240.0) are recorded after
    their defaults, so a platform that leaves them out and one that declares them are one federation, and one that
    declares the former 120 s verb bound, or another launch wait, is another. They are in the platform's own
    fingerprint (`PlatformConfig.fingerprint`) as well as the `[faults]` half."""

    def recorded(name, **changes):
        root = tmp_path / name
        root.mkdir()
        platform = write_hpc_platform(root, **{**DEFAULTS, **changes})
        config = load_platform(platform, require_secrets=False)
        assert config.fingerprint()["faults"] == faults_fingerprint(config)
        return faults_fingerprint(config)["settings"]

    left_out = recorded("a")
    declared = recorded("b", command_timeout_s=300, step_launch_wait_s=240)
    former, quick, slow = (
        recorded("c", command_timeout_s=120),
        recorded("d", step_launch_wait_s=0),
        recorded("e", step_launch_wait_s=200),
    )
    assert (left_out["command_timeout_s"], left_out["step_launch_wait_s"]) == (300.0, 240.0)
    assert left_out["verb_bounds"] == {
        "ping": 570.0,
        "restart_link": 570.0,
        "start": 630.0,
        "status": 600.0,
        "stop": 450.0,
    }
    assert slow["verb_bounds"] == {"ping": 530.0, "restart_link": 530.0, "start": 550.0, "status": 520.0, "stop": 410.0}
    assert declared == left_out
    assert former["command_timeout_s"] == 120.0
    assert former != left_out
    assert (quick["step_launch_wait_s"], quick["command_timeout_s"], quick["settle_timeout_s"]) == (0.0, 60.0, 210.0)
    assert (slow["step_launch_wait_s"], slow["command_timeout_s"], slow["settle_timeout_s"]) == (200.0, 260.0, 410.0)
    assert len({json.dumps(record, sort_keys=True) for record in (left_out, quick, slow)}) == 3


def test_o58_the_hpc_driver_hands_the_relay_each_verb_its_own_bound(tmp_path):
    """O58: the driver `[faults]` describes sends a status within 600 s, a start within 630 s, a ping and a link restart
    within 570 s and a stop within 450 s: each verb's own inner worst case with a 240 s launch wait in every step, and
    30 s (`fault_hpc.verb_bounds`), none below the declared floor of 300 s."""
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
        ("status", None, 600.0),
        ("start", None, 630.0),
        ("exec", "ping", 570.0),
        ("stop", None, 450.0),
        ("exec", "restart_link", 570.0),
    ]
    assert hpc_verb_bounds(settings) == {
        "status": 600.0,
        "start": 630.0,
        "ping": 570.0,
        "stop": 450.0,
        "restart_link": 570.0,
    }


@pytest.mark.parametrize("launch", [0.0, 200.0, 240.0])
def test_o58_every_verbs_relay_bound_exceeds_its_inner_worst_case_by_the_margin_and_the_floor(tmp_path, launch):
    """O58: each verb's relay bound is its own inner worst case (`verb_inner_worst_s`: every step at its whole bound,
    the declared launch wait counted in each, every wait at its whole wait) and 30 s, and never below the declared
    floor: `command_timeout_s` for a status, a start and an exec (the launch wait and 60 s), `settle_timeout_s` for a
    stop. Held for a launch wait of 0, of 200 s (a controller's message timeout, the longest a step's creation can
    wait with no error) and of 240 s; the heal's and the restore's reserves are counted from these bounds, so they
    hold it too."""
    settings = settings_of(write_hpc_platform(tmp_path, **{**DEFAULTS, "step_launch_wait_s": launch}))
    inner = verb_inner_worst_s(launch, settings.stop_bound_s)
    bounds = hpc_verb_bounds(settings)
    assert set(bounds) == set(inner) == {"status", "start", "stop", "ping", "restart_link"}
    for verb, worst in inner.items():
        assert bounds[verb] >= worst + 30, verb
    for verb in ("status", "start", "ping", "restart_link"):
        assert bounds[verb] >= settings.command_timeout_s == launch + HPC_VERB_WORK_S, verb
    assert bounds["stop"] >= settings.settle_timeout_s == inner["stop"] + 30
    assert command_bound_s(settings) == max(bounds["status"], bounds["start"], bounds["ping"]) + settings.relay_wait_s
    assert stop_command_s(settings) == bounds["stop"] + settings.relay_wait_s
    assert link_command_s(settings) == bounds["restart_link"] + settings.relay_wait_s


def test_o58_a_declared_floor_or_settle_above_a_verbs_inner_worst_case_is_kept_and_below_it_is_not_taken(tmp_path):
    """O58: a declared `command_timeout_s` above every verb's own worst case is every verb's bound (a study that wants
    more room has it), and one below them (60 s, with a launch wait of 240 s) gives each verb its own worst case and
    margin instead: a relay never cuts a verb short of what it may legitimately take."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    roomy = hpc_verb_bounds(settings_of(write_hpc_platform(tmp_path / "a", **{**DEFAULTS, "command_timeout_s": 900})))
    assert (roomy["status"], roomy["start"], roomy["ping"], roomy["restart_link"]) == (900.0,) * 4
    tight = hpc_verb_bounds(settings_of(write_hpc_platform(tmp_path / "b", **{**DEFAULTS, "command_timeout_s": 60})))
    assert tight == {"status": 600.0, "start": 630.0, "ping": 570.0, "stop": 450.0, "restart_link": 570.0}


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
