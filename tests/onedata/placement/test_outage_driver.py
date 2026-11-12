"""O28, O11: the outage driver stops and starts a service through Docker Compose and counts a restore only when the
service's identity changed; its settings are read from `[faults]` with the keys the fault injector of #27 uses, and a
mistake in them names its position, never a name."""

import subprocess

import pytest
from outage_support import SERVICE

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.day0.outage_driver import (
    CommandResult,
    ComposeServices,
    DriverError,
    Observation,
    Outage,
    OutageError,
    compose_services,
    load_outage_settings,
)
from expcore_onedata.testing import ADMIN_ENV, Ticker, with_placement, write_platform

INSPECT = "{{.Id}}|{{.State.StartedAt}}|{{.State.Running}}"


class Runner:
    """A command runner that records what it was asked and answers from a script of (prefix, result) pairs."""

    def __init__(self, answers=None):
        self.argvs: list[list[str]] = []
        self.answers = answers or {}
        self.timeouts: list[float] = []

    def __call__(self, argv, *, timeout_s):
        self.argvs.append(list(argv))
        self.timeouts.append(timeout_s)
        for word, result in self.answers.items():
            if word in argv:
                if isinstance(result, BaseException):
                    raise result
                return result
        return CommandResult(0, "")


def compose(runner, **options):
    return ComposeServices("/srv/compose.yaml", runner=runner, timeout_s=7.0, **options)


def test_o28_stop_and_start_are_compose_commands_without_a_shell_and_bounded():
    """O28: stop and start are compose commands without a shell and bounded."""
    runner = Runner()
    driver = compose(runner, project="proj")
    driver.stop("svc")
    driver.start("svc")
    base = ["docker", "compose", "--file", "/srv/compose.yaml", "--project-name", "proj"]
    assert runner.argvs == [[*base, "stop", "svc"], [*base, "start", "svc"]]
    assert set(runner.timeouts) == {7.0}


def test_o28_an_observation_is_the_containers_id_and_start_time_and_whether_it_runs():
    """O28: an observation is the containers id and start time and whether it runs."""
    runner = Runner(
        {"ps": CommandResult(0, "cid-1\n"), "inspect": CommandResult(0, "cid-1|2026-11-12T10:00:00Z|true\n")}
    )
    seen = compose(runner).observe("svc")
    assert seen == Observation(True, "cid-1@2026-11-12T10:00:00Z")
    assert ["docker", "inspect", "--format", INSPECT, "cid-1"] in runner.argvs


def test_o28_a_service_with_no_container_runs_not_and_has_no_identity():
    """O28: a service with no container runs not and has no identity."""
    assert compose(Runner({"ps": CommandResult(0, "")})).observe("svc") == Observation(False, None)


@pytest.mark.parametrize(
    "answers",
    [
        {"ps": CommandResult(0, "a\nb\n")},
        {"ps": CommandResult(0, "a\n"), "inspect": CommandResult(0, "garbage")},
        {"ps": CommandResult(0, "a\n"), "inspect": CommandResult(0, "a|t|maybe")},
        {"ps": CommandResult(3, "")},
        {"ps": subprocess.TimeoutExpired("docker", 7)},
        {"ps": FileNotFoundError("docker")},
    ],
)
def test_o28_an_unreadable_service_is_a_driver_error_that_names_the_step_and_no_service(answers):
    """O28: an unreadable service is a driver error that names the step and no service."""
    with pytest.raises(DriverError) as caught:
        compose(Runner(answers)).observe("svc-secret-name")
    assert "svc-secret-name" not in str(caught.value)
    assert "/srv" not in str(caught.value)


def test_o28_a_failed_stop_says_the_step_and_the_exit_code_and_nothing_the_command_printed():
    """O28: a failed stop says the step and the exit code and nothing the command printed."""
    runner = Runner({"stop": CommandResult(2, "out containing svc-secret", "err containing svc-secret")})
    with pytest.raises(DriverError, match="stop the service failed with exit code 2") as caught:
        compose(runner).stop("svc-secret")
    assert "svc-secret" not in str(caught.value)


# -- the settings ----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _admin_credential(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-for-the-config-tests")


def settings_for(tmp_path, **faults):
    def edit(data):
        with_placement(data)
        data["faults"] = {"sanctioned": True, "compose_file": "compose.yaml", "services": {"alpha": SERVICE}, **faults}

    directory = tmp_path / "s"
    directory.mkdir(exist_ok=True)
    path = write_platform(directory, edit)
    return load_outage_settings(path, load_platform(path))


def test_o28_the_faults_table_is_read_with_the_injectors_keys_and_defaults(tmp_path):
    """O28: the faults table is read with the injectors keys and defaults."""
    settings = settings_for(tmp_path, project="proj", journal="j", admission_timeout_s=5, health_wait_s=3)
    assert settings.sanctioned is True
    assert settings.services == {"alpha": SERVICE}
    assert settings.project == "proj"
    assert settings.compose_file.name == "compose.yaml"
    assert (settings.poll_s, settings.settle_timeout_s, settings.command_timeout_s, settings.clear_attempts) == (
        2.0,
        180.0,
        120.0,
        3,
    )
    assert compose_services(settings, Runner()) is not None


def test_o28_a_table_that_does_not_say_true_is_not_a_sanction(tmp_path):
    """O28: a table that does not say true is not a sanction."""
    assert settings_for(tmp_path, sanctioned=False).sanctioned is False


@pytest.mark.parametrize(
    ("faults", "message"),
    [
        ({"sanctioned": "yes"}, "must be true or false"),
        ({"services": {}}, "must name the service of at least one provider"),
        ({"services": {"unknown-provider": "svc"}}, "entry 0 is not a provider declared"),
        ({"services": {"alpha": "bad service"}}, "entry 0 must be a service name"),
        ({"settle_timeout_s": -1}, "must be a number of at least 0"),
        ({"clear_attempts": 0}, "must be an integer of at least 1"),
        ({"surprise": 1}, "is not a known key"),
    ],
)
def test_o28_a_mistake_in_the_faults_table_names_the_key_or_the_entrys_position_and_never_a_name(
    tmp_path, faults, message
):
    """O28: a mistake in the faults table names the key or the entrys position and never a name."""
    with pytest.raises(ConfigError, match=message) as caught:
        settings_for(tmp_path, **faults)
    assert "unknown-provider" not in str(caught.value)
    assert "bad service" not in str(caught.value)


# -- taking a service down and restoring it ---------------------------------------------------------------------------


class Service:
    """A scripted service: what each reading shows, the stop and the start doing what a test says."""

    def __init__(self):
        self.running, self.generation = True, 1
        self.stop_ok, self.starts_up, self.new_identity = True, True, True
        self.calls: list[str] = []

    def stop(self, service):
        self.calls.append("stop")
        if not self.stop_ok:
            raise DriverError("the command to stop the service failed with exit code 1")
        self.running = False

    def start(self, service):
        self.calls.append("start")
        if self.starts_up:
            self.generation += 1 if (not self.running and self.new_identity) else 0
            self.running = True

    def observe(self, service):
        return Observation(self.running, f"id@{self.generation}")


def outage_of(service, ticker, **options):
    settings = {
        "settle_timeout_s": 5.0,
        "poll_s": 1.0,
        "clear_attempts": 3,
        "clock": ticker.clock,
        "sleep": ticker.sleep,
    }
    return Outage(service, "svc", **{**settings, **options})


def test_o28_taking_down_reads_the_identity_stops_and_sees_the_service_down():
    """O28: taking down reads the identity stops and sees the service down."""
    ticker, service = Ticker(), Service()
    facts = outage_of(service, ticker).take_down()
    assert facts["seen_down"] is True
    assert len(facts["identity_before_sha256"]) == 64
    assert service.running is False
    assert service.calls == ["stop"]


def test_o28_a_service_that_is_not_running_is_not_stopped():
    """O28: a service that is not running is not stopped."""
    ticker, service = Ticker(), Service()
    service.running = False
    with pytest.raises(OutageError, match="not running before the stop") as caught:
        outage_of(service, ticker).take_down()
    assert caught.value.stop_sent is False
    assert service.calls == []


def test_o28_a_stop_that_never_shows_in_the_service_is_not_an_outage_and_its_restore_finds_it_as_it_was():
    """O28: a stop that never shows in the service is not an outage and its restore finds it as it was."""
    ticker, service = Ticker(), Service()
    service.stop = lambda name: service.calls.append("stop")  # type: ignore[method-assign]  (returns, does nothing)
    outage = outage_of(service, ticker)
    with pytest.raises(OutageError, match="not seen down within 5 s") as caught:
        outage.take_down()
    assert caught.value.stop_sent is True
    assert outage.restore()["how"] == "never_seen_down"
    assert outage.restore()["verified"] is True


def test_o28_a_restore_is_verified_by_a_changed_identity_and_repeating_it_changes_nothing():
    """O28: a restore is verified by a changed identity and repeating it changes nothing."""
    ticker, service = Ticker(), Service()
    outage = outage_of(service, ticker)
    outage.take_down()
    first = outage.restore()
    assert (first["verified"], first["how"], first["identity_changed"]) == (True, "restored", True)
    again = outage.restore()
    assert again["verified"] is True
    assert service.generation == 2


def test_o28_readiness_alone_is_not_a_restore():
    """O28: readiness alone is not a restore."""
    ticker, service = Ticker(), Service()
    service.new_identity = False
    outage = outage_of(service, ticker)
    outage.take_down()
    result = outage.restore()
    assert (result["verified"], result["how"], result["identity_changed"]) == (False, "identity_unchanged", False)


def test_o28_a_start_that_fails_is_tried_again_up_to_the_attempts_and_then_is_not_a_restore():
    """O28: a start that fails is tried again up to the attempts and then is not a restore."""
    ticker, service = Ticker(), Service()
    service.starts_up = False
    outage = outage_of(service, ticker, clear_attempts=2)
    outage.take_down()
    result = outage.restore()
    assert (result["verified"], result["how"], result["attempts"]) == (False, "not_running_again", 2)
    assert service.calls == ["stop", "start", "start"]


def test_o28_nothing_to_restore_when_nothing_was_stopped():
    """O28: nothing to restore when nothing was stopped."""
    ticker, service = Ticker(), Service()
    assert outage_of(service, ticker).restore() == {"verified": True, "how": "not_injected", "attempts": 0}
    assert service.calls == []


def test_o28_the_waits_are_bounded_by_the_settle_timeout_on_the_given_clock():
    """O28: the waits are bounded by the settle timeout on the given clock."""
    ticker, service = Ticker(), Service()
    service.starts_up = False
    outage = outage_of(service, ticker, settle_timeout_s=4.0, clear_attempts=1)
    outage.take_down()
    began = ticker.now
    outage.restore()
    assert 4.0 <= ticker.now - began <= 6.0  # one attempt, polled once a second to the bound
