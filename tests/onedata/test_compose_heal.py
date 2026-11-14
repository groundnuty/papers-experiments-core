"""O35: the Compose driver's two commands of the link heal, `ping` and `restart_link`, on a fake runner.

No test runs Docker: the runner answers each command as Compose, Docker and node_package's nodetool would.
"""

import subprocess
from pathlib import Path

import pytest

from expcore_onedata.compose import (
    LINK_RESULTS,
    RESTART_LINK_SCRIPT,
    CommandResult,
    ComposeDriver,
    DriverError,
)

COMPOSE = Path("/srv/stack/compose.yaml")
NODE_LINE = "RPC to 'op_worker@node-host.internal' failed: timeout\n"


class Runner:
    """List one container for the service; answer an exec with `answer` (or raise it); record argv and bound."""

    def __init__(self, answer, containers="cid-7\n"):
        self.answer, self.containers = answer, containers
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, *, timeout_s):
        self.calls.append((list(argv), timeout_s))
        if "ps" in argv:
            return CommandResult(0, self.containers)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def driver(answer, **runner):
    fake = Runner(answer, **runner)
    return ComposeDriver(COMPOSE, project="stack-a", runner=fake, probe=lambda service: True), fake


def test_o35_ping_execs_the_runner_in_the_services_container_and_is_true_only_for_pong():
    """O35: the image's runner pings its node from inside the container found by the project and the service."""
    found, runner = driver(CommandResult(0, "pong\n"))
    assert found.ping("store-two") is True
    listed = runner.calls[0][0]
    assert listed[-4:] == ["ps", "--all", "--quiet", "store-two"]
    assert "stack-a" in listed
    assert runner.calls[1][0] == ["docker", "exec", "cid-7", "/usr/sbin/op_worker", "ping"]
    for answer in (CommandResult(1, "Node 'op_worker@node-host' not responding to pings.\n"), CommandResult(0, "")):
        assert driver(answer)[0].ping("store-two") is False
    assert driver(subprocess.TimeoutExpired(["x"], 1))[0].ping("store-two") is False


def test_o35_restart_link_calls_the_arity_0_function_through_nodetool_rpcterms_with_its_bound():
    """O35: `rtransfer_config:restart_link()` through nodetool's `rpcterms` with the trailing empty argument (arity
    0), an RPC timeout of 120 s, and a command bound above it, so nodetool's own timeout answer is read."""
    found, runner = driver(CommandResult(0, "ok\n"))
    assert found.restart_link("store-two") == "ok"
    argv, bound = runner.calls[1]
    assert argv == ["docker", "exec", "cid-7", "bash", "-c", RESTART_LINK_SCRIPT]
    assert RESTART_LINK_SCRIPT == (
        '. /usr/lib/op_worker/lib/env.sh && $NODETOOL -rpctimeout 120000 rpcterms rtransfer_config restart_link ""'
    )
    assert bound == 150.0


@pytest.mark.parametrize(
    ("answer", "word"),
    [
        (CommandResult(1, NODE_LINE), "badrpc_timeout"),
        (CommandResult(1, "RPC to 'op_worker@node-host.internal' failed: nodedown\n"), "badrpc"),
        (CommandResult(1, "Node 'op_worker@node-host.internal' not responding to pings.\n"), "no_node"),
        (CommandResult(0, "{error,already_started}\n"), "unexpected"),
        (CommandResult(2, "", "bash: line 1: env.sh: No such file\n"), "unexpected"),
        (subprocess.TimeoutExpired(["x"], 150), "command_timeout"),
        (FileNotFoundError("docker"), "not_run"),
    ],
)
def test_o35_restart_links_result_is_a_word_and_a_badrpc_is_no_driver_error(answer, word):
    """O35: nodetool's badrpc exits 1, which is a result (the restart may be running on the node), never a failed
    command; the word is all that is kept, never the line, which names the node."""
    result = driver(answer)[0].restart_link("store-two")
    assert result == word
    assert result in LINK_RESULTS
    assert "node-host" not in result


def test_o35_a_service_without_exactly_one_container_is_not_execd_into():
    """O35: no container, or several, is a DriverError before anything is sent to a node."""
    for containers in ("", "cid-1\ncid-2\n"):
        found, runner = driver(CommandResult(0, "ok\n"), containers=containers)
        with pytest.raises(DriverError):
            found.restart_link("store-two")
        with pytest.raises(DriverError):
            found.ping("store-two")
        assert all("exec" not in argv for argv, _ in runner.calls)
