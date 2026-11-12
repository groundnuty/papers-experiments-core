"""The Compose driver: commands as argument vectors through an injected runner, identity from the container (O11, O12).

No test runs Docker: the runner is a fake that answers each command as Compose and Docker would.
"""

import subprocess
from pathlib import Path

import pytest

from expcore_onedata.compose import INSPECT_FORMAT, CommandResult, ComposeDriver, DriverError, Observation

COMPOSE = Path("/srv/stack/compose.yaml")


class Runner:
    """Answer commands from a table of argument-vector suffixes; record every command."""

    def __init__(self, answers):
        self.answers = answers
        self.commands: list[list[str]] = []

    def __call__(self, argv, *, timeout_s):
        self.commands.append(list(argv))
        for suffix, answer in self.answers.items():
            if tuple(argv[-len(suffix) :]) == suffix:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise AssertionError(f"unexpected command {argv}")


def driver(answers, probe=lambda service: True, **options):
    runner = Runner(answers)
    return ComposeDriver(COMPOSE, project="stack-a", runner=runner, probe=probe, **options), runner


def test_o11_the_identity_is_the_containers_id_and_start_time():
    """O11: a stop and start keep the id and change the start time, so the identity changes only on a real restart."""
    ps = ("ps", "--all", "--quiet", "store-two")
    inspect = ("inspect", "--format", INSPECT_FORMAT, "cid-9")
    compose, runner = driver(
        {ps: CommandResult(0, "cid-9\n"), inspect: CommandResult(0, "cid-9|2026-11-09T10:00:00Z|true\n")}
    )
    assert compose.observe("store-two") == Observation(True, "cid-9@2026-11-09T10:00:00Z")
    base = ["docker", "compose", "--file", str(COMPOSE), "--project-name", "stack-a"]
    assert runner.commands == [[*base, *ps], ["docker", *inspect]]


def test_o11_a_stopped_container_is_not_running_and_a_missing_one_has_no_identity():
    """O11: a service with no container is down with no identity to compare."""
    ps = ("ps", "--all", "--quiet", "store-two")
    inspect = ("inspect", "--format", INSPECT_FORMAT, "cid-9")
    compose, _ = driver(
        {ps: CommandResult(0, "cid-9\n"), inspect: CommandResult(0, "cid-9|2026-11-09T10:00:00Z|false")}
    )
    assert compose.observe("store-two").running is False
    compose, _ = driver({ps: CommandResult(0, "\n")})
    assert compose.observe("store-two") == Observation(False, None)


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({("ps", "--all", "--quiet", "store-two"): CommandResult(0, "cid-1\ncid-2\n")}, "identity is ambiguous"),
        ({("ps", "--all", "--quiet", "store-two"): CommandResult(1, "", "no such service: store-two")}, "exit code 1"),
        (
            {
                ("ps", "--all", "--quiet", "store-two"): CommandResult(0, "cid-1\n"),
                ("inspect", "--format", INSPECT_FORMAT, "cid-1"): CommandResult(0, "garbled"),
            },
            "could not be read",
        ),
        (
            {("ps", "--all", "--quiet", "store-two"): subprocess.TimeoutExpired(["docker"], 120)},
            "did not finish within 120 s",
        ),
        ({("ps", "--all", "--quiet", "store-two"): FileNotFoundError("docker")}, "could not run"),
    ],
)
def test_o12_a_failed_reading_raises_naming_the_step_and_never_the_service_or_its_output(answers, message):
    """O12: the message says which step failed and how; the service name and the command's output stay out of it."""
    compose, _ = driver(answers)
    with pytest.raises(DriverError, match=message) as caught:
        compose.observe("store-two")
    assert "store-two" not in str(caught.value)
    assert "stack" not in str(caught.value)


def test_o11_stop_and_start_name_the_service_and_fail_on_a_nonzero_exit():
    """O11: the commands the injector relies on, and a failure it can see."""
    compose, runner = driver(
        {("stop", "store-two"): CommandResult(0, ""), ("start", "store-two"): CommandResult(3, "", "boom store-two")},
        command=("docker-compose",),
    )
    compose.stop("store-two")
    with pytest.raises(DriverError, match="start the service failed with exit code 3") as caught:
        compose.start("store-two")
    assert "store-two" not in str(caught.value)
    assert runner.commands[0] == [
        "docker-compose",
        "--file",
        str(COMPOSE),
        "--project-name",
        "stack-a",
        "stop",
        "store-two",
    ]


PS = ("ps", "--all", "--quiet", "store-two")
INSPECT = ("inspect", "--format", INSPECT_FORMAT, "cid-9")


def inspected(line):
    return {PS: CommandResult(0, "cid-9\n"), INSPECT: CommandResult(0, line)}


def test_o11_a_container_with_a_health_check_reports_its_status_in_the_observation():
    """O11: the inspection's fourth field is the container's health status; the identity is unchanged by it."""
    compose, _ = driver(inspected("cid-9|2026-11-09T10:00:00Z|true|starting\n"))
    assert compose.observe("store-two") == Observation(True, "cid-9@2026-11-09T10:00:00Z", "starting")


@pytest.mark.parametrize("line", ["cid-9|T|true|", "cid-9|T|true|none", "cid-9|T|true"])
def test_o11_a_container_with_no_health_check_has_no_health_status(line):
    """O11: an empty field, Docker's `none`, or an answer without the field all say there is no health check."""
    compose, _ = driver(inspected(line + "\n"))
    assert compose.observe("store-two").health is None


@pytest.mark.parametrize("line", ["cid-9|T|true|healthy|extra", "cid-9|T|true|Health Status 1"])
def test_o11_an_inspection_with_a_malformed_health_field_cannot_be_read(line):
    """O11: a health field that is not a word is no reading, so the restore is not judged by it."""
    compose, _ = driver(inspected(line + "\n"))
    with pytest.raises(DriverError, match="could not be read"):
        compose.observe("store-two")


@pytest.mark.parametrize(
    ("line", "probe_answer", "serves", "asked"),
    [
        ("cid-9|T|true|healthy", True, True, True),
        ("cid-9|T|true|healthy", False, False, True),
        ("cid-9|T|true|starting", True, False, False),
        ("cid-9|T|true|unhealthy", True, False, False),
        ("cid-9|T|true|", True, True, True),
        ("cid-9|T|true|", False, False, True),
        ("cid-9|T|false|", True, False, False),
    ],
)
def test_o11_a_service_serves_when_its_container_is_healthy_or_has_no_health_check_and_the_probe_answers(
    line, probe_answer, serves, asked
):
    """O11: `serves` needs a running container, a healthy one when it has a health check, and the probe's yes; a
    container that is not yet healthy is not even probed."""
    probed = []

    def probe(service):
        probed.append(service)
        return probe_answer

    compose, _ = driver(inspected(line + "\n"), probe=probe)
    assert compose.serves("store-two") is serves
    assert probed == (["store-two"] if asked else [])


def test_o11_a_service_with_no_container_does_not_serve_and_an_unreadable_one_raises():
    """O11: no container is nothing serving; a reading that fails is the caller's to ask again."""
    compose, _ = driver({PS: CommandResult(0, "\n")})
    assert compose.serves("store-two") is False
    compose, _ = driver({PS: CommandResult(1, "", "no such service")})
    with pytest.raises(DriverError, match="exit code 1"):
        compose.serves("store-two")


def test_o11_the_driver_cannot_be_built_without_a_probe():
    """O11: a restore is not verified without asking whether the provider serves, so the probe is not optional."""
    with pytest.raises(TypeError, match="probe"):
        ComposeDriver(COMPOSE, project="stack-a", runner=Runner({}))  # type: ignore[call-arg]
