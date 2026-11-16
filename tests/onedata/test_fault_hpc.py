"""O56: the HPC driver of the fault injector and the heal, on a fake runner (`hpc_support.FakeHpc`).

The driver runs four verbs on a provider's service, each an argv the federation's process model builds: `status`,
`stop`, `start` and `exec` into the running Apptainer service. No test reaches a cluster: the runner answers each
command as the service's status, its stop and start, and op-worker's runner and nodetool inside it would.
"""

import json
import re
import subprocess

import pytest
from fault_support import FakeClock
from hpc_support import PING, RESTART_LINK, TOOL, FakeCommands, FakeHpc, hpc_driver

from expcore_onedata.compose import RESTART_LINK_SCRIPT, CommandResult, DriverError, Observation
from expcore_onedata.fault_hpc import HpcDriver

LABELS = {"store-one": "providers[0]", "store-two": "providers[1]"}
SERVICE = "store-two"


def federation():
    fake = FakeHpc(FakeClock(), LABELS)
    return fake, hpc_driver(fake)


class Answer:
    """A runner that answers every command with `answer` (or raises it), recording argv and bound."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, *, timeout_s):
        self.calls.append((list(argv), timeout_s))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def answering(answer, probe=lambda service: True):
    runner = Answer(answer)
    return HpcDriver(FakeCommands(tuple(LABELS)), runner=runner, timeout_s=7.0, probe=probe), runner


def test_o56_each_verb_runs_the_argv_the_federation_builds_for_the_service_within_the_command_bound():
    """O56: stop, start and status are the argv the process model builds for the service, run through the runner and
    bounded by the command timeout; nothing names a container."""
    fake, driver = federation()
    driver.stop(SERVICE)
    driver.start(SERVICE)
    driver.observe(SERVICE)
    assert fake.calls == [
        ([TOOL, "stop", SERVICE], 10.0),
        ([TOOL, "start", SERVICE], 10.0),
        ([TOOL, "status", SERVICE], 10.0),
    ]


def test_o56_the_identity_is_the_services_own_and_changes_with_every_start_of_a_stopped_service():
    """O56: the status line carries an identity that stays while the service is left alone and changes when it is
    started anew, which is what lets a restore be told from readiness (O11); a start of a running service changes
    nothing. A service has no health check of a container's: its health is None."""
    _, driver = federation()
    first = driver.observe(SERVICE)
    assert (first.running, first.health) == (True, None)
    assert driver.observe(SERVICE) == first
    driver.start(SERVICE)
    assert driver.observe(SERVICE) == first
    driver.stop(SERVICE)
    assert driver.observe(SERVICE).running is False
    driver.start(SERVICE)
    again = driver.observe(SERVICE)
    assert again.running is True
    assert again.identity != first.identity


@pytest.mark.parametrize(
    "answer",
    [
        CommandResult(1, "", "srun: error: the step could not be created\n"),
        CommandResult(0, ""),
        CommandResult(0, "running store-two@launch-1\n"),
        CommandResult(0, json.dumps({"running": "yes", "identity": "x"})),
        CommandResult(0, json.dumps({"identity": "x"})),
        CommandResult(0, json.dumps({"running": True, "identity": 7})),
        CommandResult(0, json.dumps({"running": True, "identity": "a name with spaces"})),
        CommandResult(0, json.dumps([True, "x"])),
        CommandResult(0, json.dumps({"running": True, "identity": "x"}) + "\n" + json.dumps({"running": False})),
        subprocess.TimeoutExpired(["x"], 7),
        FileNotFoundError("srun"),
    ],
)
def test_o56_a_status_that_cannot_be_read_raises_naming_the_step_and_never_its_output(answer):
    """O56: a status command that fails, times out, cannot run or answers anything but one line of the status record
    is a reading that failed (DriverError), never a guess; its message names the step, not the service or the
    output, which can name a node."""
    driver, _ = answering(answer)
    with pytest.raises(DriverError, match="read the service's status") as caught:
        driver.observe(SERVICE)
    assert SERVICE not in str(caught.value)
    assert "srun" not in str(caught.value)


def test_o56_a_running_service_that_names_no_identity_reads_as_running_with_none():
    """O56: a status of a running service whose identity is null cannot show a restore (O11), and a stopped service
    may have none."""
    driver, _ = answering(CommandResult(0, json.dumps({"running": True, "identity": None}) + "\n"))
    assert driver.observe(SERVICE) == Observation(True, None)
    driver, _ = answering(CommandResult(0, json.dumps({"running": False, "identity": None})))
    assert driver.observe(SERVICE) == Observation(False, None)


@pytest.mark.parametrize("verb", ["stop", "start"])
@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (CommandResult(3, "", "hpcop: refused\n"), "failed with exit code 3"),
        (subprocess.TimeoutExpired(["x"], 7), "did not finish within 7 s"),
        (PermissionError("srun"), "could not run (PermissionError)"),
    ],
)
def test_o56_a_stop_or_start_that_fails_raises_as_the_compose_drivers_does(verb, answer, said):
    """O56: a stop or start command that fails, times out or cannot run raises DriverError, naming the step and how it
    ended, as the Compose driver's do, so the injector's journal and recovery treat it exactly alike (a stop that
    raised is of unknown outcome, O11)."""
    driver, _ = answering(answer)
    with pytest.raises(DriverError, match=re.escape(f"the command to {verb} the service {said}")):
        getattr(driver, verb)(SERVICE)


def test_o56_a_service_the_federation_does_not_hold_is_refused_before_any_command_runs():
    """O56: a service the process model cannot address (not in the federation's state) is refused before anything
    runs, and a model that cannot build the command at all (its state unreadable) is told by its error's type only."""
    driver, runner = answering(CommandResult(0, ""))
    for call in (driver.stop, driver.start, driver.observe, driver.ping, driver.restart_link):
        with pytest.raises(DriverError):
            call("store-nine")
    assert runner.calls == []

    class Unreadable(FakeCommands):
        def stop(self, service):
            raise PermissionError(13, "Permission denied", "/private/state/file.json")

    driver = HpcDriver(Unreadable(tuple(LABELS)), runner=runner, timeout_s=7.0, probe=lambda service: True)
    with pytest.raises(DriverError, match=r"could not be addressed \(PermissionError\)") as caught:
        driver.stop(SERVICE)
    assert "/private" not in str(caught.value)


def test_o56_serves_asks_the_probe_only_of_a_running_service_and_raises_when_its_status_cannot_be_read():
    """O56: a service serves when it runs and the probe finds its provider answering REST; a stopped one is not asked
    about, and a status that cannot be read raises as `observe` does."""
    fake, driver = federation()
    assert driver.serves(SERVICE) is True
    driver.stop(SERVICE)
    reads = len(fake.reads)
    assert driver.serves(SERVICE) is False
    assert len(fake.reads) == reads
    fake.unreadable.add(SERVICE)
    with pytest.raises(DriverError):
        driver.serves(SERVICE)
    asked: list[str] = []
    driver, _ = answering(CommandResult(0, json.dumps({"running": True, "identity": "x"})), probe=asked.append)
    assert driver.serves(SERVICE) is False  # a probe that answers anything but True is no serving
    assert asked == [SERVICE]


def test_o56_ping_execs_the_runner_inside_the_providers_service_and_is_true_only_for_pong():
    """O56: the heal's ping is op-worker's runner exec'd inside the provider's Apptainer service (the process model's
    exec), bounded by the command timeout, True only when it prints `pong`; a failure, a timeout or any other answer
    is no pong."""
    fake, driver = federation()
    assert driver.ping(SERVICE) is True
    assert fake.calls[-1] == ([TOOL, "exec", SERVICE, "--", *PING], 10.0)
    fake.pong["providers[1]"] = False
    assert driver.ping(SERVICE) is False
    for answer in (CommandResult(0, ""), CommandResult(0, "pang\n"), subprocess.TimeoutExpired(["x"], 7), OSError()):
        assert answering(answer)[0].ping(SERVICE) is False


def test_o56_restart_link_execs_the_nodetool_call_inside_the_providers_service_with_its_bound():
    """O56: the link restart is the Compose driver's very command (`RESTART_LINK_SCRIPT`: rtransfer_config's
    restart_link through nodetool's rpcterms, arity 0, a 120 s RPC), exec'd inside the provider's Apptainer service
    so env.sh finds the service's own epmd port and cookie, with the command bounded 30 s above the RPC."""
    fake, driver = federation()
    assert driver.restart_link(SERVICE) == "ok"
    assert fake.calls[-1] == ([TOOL, "exec", SERVICE, "--", "bash", "-c", RESTART_LINK_SCRIPT], 150.0)
    assert RESTART_LINK == ["bash", "-c", RESTART_LINK_SCRIPT]


@pytest.mark.parametrize(
    ("answer", "word"),
    [
        (CommandResult(0, "ok\n"), "ok"),
        (CommandResult(1, "RPC to 'op_worker@node-host.internal' failed: timeout\n"), "badrpc_timeout"),
        (CommandResult(1, "RPC to 'op_worker@node-host.internal' failed: nodedown\n"), "badrpc"),
        (CommandResult(1, "Node 'op_worker@node-host.internal' not responding to pings.\n"), "no_node"),
        (CommandResult(0, "{error,already_started}\n"), "unexpected"),
        (CommandResult(1, "", "srun: error: the step could not be created\n"), "unexpected"),
        (subprocess.TimeoutExpired(["x"], 150), "command_timeout"),
        (FileNotFoundError("srun"), "not_run"),
    ],
)
def test_o56_the_link_restarts_result_is_the_same_word_as_on_compose(answer, word):
    """O56: the result is one word of the Compose driver's (`link_result`): a failure the exec does not describe, a
    step that could not be made among them, may have reached the node and is never `not_run`, so it is never retried;
    the output, which names the node, is never kept."""
    driver, _ = answering(answer)
    assert driver.restart_link(SERVICE) == word


def test_o56_the_rest_side_is_the_probes_as_on_compose():
    """O56: the session gate, the link check, the transfer listings and the gate's probe file are the probe's, read
    over REST as the Compose driver reads them; a probe without them leaves the driver without them too."""
    fake, driver = federation()
    assert driver.has_sessions is True
    assert driver.arm_gate(SERVICE) == "gate-probe-1"
    assert driver.sessions_ready(SERVICE, "gate-probe-1") is None
    assert driver.link_matrix(SERVICE).ok is True
    assert driver.restored_links(SERVICE) is None
    assert driver.active_transfers() == 0
    fake.stuck = {("providers[0]", "providers[1]")}
    assert driver.restored_links(SERVICE) == "stuck: providers[1]->providers[0]"
    bare, _ = answering(CommandResult(0, ""))
    assert (bare.has_sessions, bare.arm_gate(SERVICE), bare.sessions_ready(SERVICE, None)) == (False, None, None)
    assert (bare.link_matrix(SERVICE), bare.restored_links(SERVICE), bare.active_transfers()) == (None, None, None)


def test_o56_the_driver_is_never_built_without_a_probe():
    """O56: as the Compose driver, it cannot take a running service for one that serves without asking."""
    with pytest.raises(TypeError):
        HpcDriver(FakeCommands(tuple(LABELS)), runner=Answer(CommandResult(0, "")))  # type: ignore[call-arg]
