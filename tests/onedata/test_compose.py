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


def driver(answers, **options):
    runner = Runner(answers)
    return ComposeDriver(COMPOSE, project="stack-a", runner=runner, **options), runner


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
