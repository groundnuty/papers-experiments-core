"""O58: the HPC driver of the fault injector and the heal, on a fake runner (`hpc_support.FakeHpc`).

The driver asks for four verbs on a provider's service (`status`, `stop`, `start`, and `exec` of one command of a fixed
set inside the running Apptainer service), each a request a transport carries to the federation's `hpcfed.py`. No test
reaches a cluster: the runner answers each command as the service's status, its stop and start, and op-worker's runner
and nodetool inside it would.
"""

import json
import re
import subprocess

import pytest
from fault_support import FakeClock
from hpc_support import PING, RESTART_LINK, TOOL, FakeHpc, hpc_driver

from expcore_onedata.compose import RESTART_LINK_SCRIPT, CommandResult, DriverError, Observation
from expcore_onedata.fault_hpc import (
    EXEC_COMMANDS,
    HpcDriver,
    Request,
    VerbCommand,
    status_domain_wait,
    stop_evidence,
)
from expcore_onedata.fault_restore import stop_record

LABELS = {"store-one": "providers[0]", "store-two": "providers[1]"}
SERVICE = "store-two"


def federation(**options):
    fake = FakeHpc(FakeClock(), LABELS)
    return fake, hpc_driver(fake, **options)


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
    return HpcDriver(VerbCommand([TOOL], runner), timeout_s=7.0, probe=probe), runner


def status(**record):
    return CommandResult(0, json.dumps(record) + "\n")


def test_o58_each_verb_is_the_federations_command_for_the_service_within_its_bound():
    """O58: status and start are `hpcfed.py <verb> <service>` within the command bound, and the stop within its own
    bound, which outlasts a service's graceful stop, its kill and its step's end; nothing names a container."""
    fake, driver = federation(stop_timeout_s=750.0)
    driver.stop(SERVICE)
    driver.start(SERVICE)
    driver.observe(SERVICE)
    assert fake.calls == [
        ([TOOL, "stop", SERVICE], 750.0),
        ([TOOL, "start", SERVICE], 10.0),
        ([TOOL, "status", SERVICE], 10.0),
    ]


def test_o58_the_identity_is_the_services_own_and_changes_with_every_start_of_a_stopped_service():
    """O58: the status carries an identity that stays while the service is left alone and changes when it is started
    anew, which is what lets a restore be told from readiness (O11); a start of a running service changes nothing."""
    _, driver = federation()
    first = driver.observe(SERVICE)
    assert (first.running, first.health) == (True, "healthy")
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
        status(running="yes", identity="x"),
        status(identity="x"),
        status(running=True, identity=7),
        status(running=True, identity="a name with spaces"),
        status(running=True, identity="x", ready="yes"),
        CommandResult(0, json.dumps([True, "x"])),
        CommandResult(0, json.dumps({"running": True, "identity": "x"}) + "\n" + json.dumps({"running": False})),
        subprocess.TimeoutExpired(["x"], 7),
        FileNotFoundError("hpcfed.py"),
    ],
)
def test_o58_a_status_that_cannot_be_read_raises_naming_the_step_and_never_its_output(answer):
    """O58: a status that fails, has no answer in its bound, cannot be sent or answers anything but one line of the
    status record is a reading that failed (DriverError), never a guess; its message names the step, not the service
    or the output, which can name a node."""
    driver, _ = answering(answer)
    with pytest.raises(DriverError, match="read the service's status") as caught:
        driver.observe(SERVICE)
    assert SERVICE not in str(caught.value)
    assert "srun" not in str(caught.value)


def test_o58_running_with_no_identity_reads_as_none_and_readiness_as_a_health_check():
    """O58: a status of a running service whose identity is null cannot show a restore (O11); `ready` reads as a
    container's health check, `healthy` once the service's own checks passed for this launch, `starting` before, and
    nothing when the status does not say."""
    assert answering(status(running=True, identity=None))[0].observe(SERVICE) == Observation(True, None)
    assert answering(status(running=False, identity=None))[0].observe(SERVICE) == Observation(False, None)
    ready = answering(status(running=True, identity="2:4002:902", ready=True))[0].observe(SERVICE)
    assert ready == Observation(True, "2:4002:902", "healthy")
    waiting = answering(status(running=True, identity="2:4002:902", ready=False))[0].observe(SERVICE)
    assert waiting.health == "starting"


@pytest.mark.parametrize("verb", ["stop", "start"])
@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (CommandResult(3, "", "hpcop: refused\n"), "failed with exit code 3"),
        (subprocess.TimeoutExpired(["x"], 7), "did not finish within 7 s"),
        (PermissionError("hpcfed.py"), "could not run (PermissionError)"),
    ],
)
def test_o58_a_stop_or_start_that_fails_raises_as_the_compose_drivers_does(verb, answer, said):
    """O58: a stop or start that fails, has no answer in its bound or cannot be sent raises DriverError, naming the
    step and how it ended, as the Compose driver's do, so the injector's journal and recovery treat it exactly alike (a
    stop that raised is of unknown outcome, O11)."""
    driver, _ = answering(answer)
    with pytest.raises(DriverError, match=re.escape(f"the command to {verb} the service {said}")):
        getattr(driver, verb)(SERVICE)


@pytest.mark.parametrize(
    "request_",
    [
        lambda: Request("restart", SERVICE),
        lambda: Request("status", "-rf"),
        lambda: Request("status", "a service"),
        lambda: Request("exec", SERVICE),
        lambda: Request("exec", SERVICE, "bash"),
        lambda: Request("status", SERVICE, "ping"),
    ],
)
def test_o58_a_request_is_one_verb_on_one_service_and_an_exec_one_allowed_command(request_):
    """O58: a request names a verb of the four, a service name (never an option or words), and, for an exec only, one
    command of the allowed set (the heal's ping and link restart); anything else is refused before it is sent."""
    with pytest.raises(DriverError):
        request_()
    assert set(EXEC_COMMANDS) == {"ping", "restart_link"}


def test_o58_a_transport_that_cannot_send_is_told_by_its_errors_type_only():
    """O58: a request that cannot be sent (the relay's directory unreadable, say) fails naming the step and the
    error's type, never a path."""

    def unreadable(request, *, timeout_s):
        raise PermissionError(13, "Permission denied", "/private/root/fault-relay")

    driver = HpcDriver(unreadable, timeout_s=7.0, probe=lambda service: True)
    with pytest.raises(DriverError, match=r"could not run \(PermissionError\)") as caught:
        driver.stop(SERVICE)
    assert "/private" not in str(caught.value)


def test_o58_serves_asks_the_probe_only_of_a_running_ready_service_and_raises_when_its_status_cannot_be_read():
    """O58: a service serves when it runs, its own checks passed for this launch, and the probe finds its provider
    answering REST; a stopped or not yet ready one is not asked about, and a status that cannot be read raises."""
    fake, driver = federation()
    assert driver.serves(SERVICE) is True
    reads = len(fake.reads)
    fake.not_ready.add(SERVICE)
    assert driver.serves(SERVICE) is False
    driver.stop(SERVICE)
    assert driver.serves(SERVICE) is False
    assert len(fake.reads) == reads
    fake.unreadable.add(SERVICE)
    with pytest.raises(DriverError):
        driver.serves(SERVICE)
    asked: list[str] = []
    driver, _ = answering(status(running=True, identity="x"), probe=asked.append)
    assert driver.serves(SERVICE) is False  # a probe that answers anything but True is no serving
    assert asked == [SERVICE]


def test_o58_a_figure_beyond_a_floats_range_is_no_figure_and_raises_nothing():
    """O58 (Codex, 17 Nov 2026): a whole number of 400 digits in a verb's answer (a corrupt or crafted record) made
    `math.isfinite` raise OverflowError, turning a verb that had succeeded into an exception. It is no figure, as an
    infinity is: the stop's evidence keeps the figures that are numbers, and a status gives no domain wait."""
    huge = "1" + "0" * 400
    answer = f'{{"stopped": true, "ended": "kill", "seconds": {huge}, "service_seconds": 630.5}}'
    assert stop_evidence(answer) == {"stopped": True, "ended": "kill", "seconds": None, "service_seconds": 630.5}
    assert stop_evidence(answer.replace("630.5", huge)) == {
        "stopped": True, "ended": "kill", "seconds": None, "service_seconds": None}  # fmt: skip
    assert status_domain_wait(f'{{"domain_wait_s": {huge}}}') is None
    assert status_domain_wait('{"domain_wait_s": -' + huge + "}") is None
    assert status_domain_wait('{"domain_wait_s": 12}') == 12.0  # a whole number that fits is the figure it was


def test_o58_the_stops_evidence_is_kept_checked_and_nothing_else_of_its_answer():
    """O58: the stop's answer says whether it was confirmed, how it ended, how long the verb took to its verdict and
    how long the service took to end after its SIGTERM; the driver keeps those, checked, for the journal, and nothing
    else (the answer may name a node)."""
    fake, driver = federation()
    fake.exit_after_s = 4.5
    driver.stop(SERVICE)
    assert driver.last_stop == {"stopped": True, "ended": "exit", "seconds": 4.5, "service_seconds": 4.5}
    answer = '{"stopped": true, "ended": "kill", "seconds": 691.25, "service_seconds": 630.5, "node": "n-1"}'
    assert stop_evidence(answer) == {"stopped": True, "ended": "kill", "seconds": 691.25, "service_seconds": 630.5}
    assert stop_evidence('{"ended": "melted", "seconds": -1, "stopped": "yes", "service_seconds": NaN}') is None
    assert stop_evidence('{"ended": "none", "seconds": true}') == {
        "stopped": None,
        "ended": "none",
        "seconds": None,
        "service_seconds": None,
    }
    assert stop_evidence("") is None
    driver, _ = answering(CommandResult(0, ""))
    driver.stop(SERVICE)
    assert driver.last_stop is None


def test_o58_a_stop_answered_unconfirmed_keeps_its_evidence_and_fails():
    """O58: a stop the verb could not confirm (exit 1) still says how long it took: the driver keeps that evidence,
    then raises DriverError as for any failed command, so the injector journals the stop of unknown outcome with its
    duration; a stop with no answer keeps none."""
    fake, driver = federation()
    fake.exit_after_s, fake.stop_unconfirmed = 4.5, True
    with pytest.raises(DriverError, match="exit code 1"):
        driver.stop(SERVICE)
    assert driver.last_stop == {"stopped": False, "ended": "exit", "seconds": 74.5, "service_seconds": 4.5}
    driver, _ = answering(subprocess.TimeoutExpired(["hpc-service"], 1.0))
    with pytest.raises(DriverError, match="did not finish"):
        driver.stop(SERVICE)
    assert driver.last_stop is None


def test_o58_ping_execs_the_runner_inside_the_providers_service_and_is_true_only_for_pong():
    """O58: the heal's ping is op-worker's runner exec'd inside the provider's Apptainer service, within the command
    bound, True only when it prints `pong`; a failure, no answer in time or any other answer is no pong."""
    fake, driver = federation()
    assert driver.ping(SERVICE) is True
    assert fake.calls[-1] == ([TOOL, "exec", SERVICE, "--", *PING], 10.0)
    fake.pong["providers[1]"] = False
    assert driver.ping(SERVICE) is False
    for answer in (CommandResult(0, ""), CommandResult(0, "pang\n"), subprocess.TimeoutExpired(["x"], 7), OSError()):
        assert answering(answer)[0].ping(SERVICE) is False


def test_o58_restart_link_execs_the_nodetool_call_inside_the_providers_service_with_its_bound():
    """O58: the link restart is the Compose driver's very command (`RESTART_LINK_SCRIPT`: rtransfer_config's
    restart_link through nodetool's rpcterms, arity 0, a 120 s RPC), exec'd inside the provider's Apptainer service so
    env.sh finds the service's own epmd port and cookie, bounded 30 s above the RPC and 60 s more for the step."""
    fake, driver = federation()
    assert driver.restart_link(SERVICE) == "ok"
    assert fake.calls[-1] == ([TOOL, "exec", SERVICE, "--", "bash", "-c", RESTART_LINK_SCRIPT], 210.0)
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
        (subprocess.TimeoutExpired(["x"], 210), "command_timeout"),
        (FileNotFoundError("hpcfed.py"), "not_run"),
    ],
)
def test_o58_the_link_restarts_result_is_the_same_word_as_on_compose(answer, word):
    """O58: the result is one word of the Compose driver's (`link_result`): a failure the exec does not describe, a
    step that could not be made among them, may have reached the node and is never `not_run`, so it is never retried;
    the output, which names the node, is never kept."""
    driver, _ = answering(answer)
    assert driver.restart_link(SERVICE) == word


def test_o58_the_rest_side_is_the_probes_as_on_compose():
    """O58: the session gate, the link check, the transfer listings and the gate's probe file are the probe's, read
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


def test_o58_the_driver_is_never_built_without_a_probe():
    """O58: as the Compose driver, it cannot take a running service for one that serves without asking."""
    with pytest.raises(TypeError):
        HpcDriver(VerbCommand([TOOL], Answer(CommandResult(0, ""))))  # type: ignore[call-arg]


def test_o96_a_stop_that_kept_the_service_with_no_holder_keeps_the_word_and_fails_the_injection():
    """O96: a stop whose evidence says `"holder": "failed"` (no holder step beside the kept work directory) keeps
    that word with the evidence, which the injector journals with the stop (`stop_record`), and raises DriverError, so
    the injector brings the provider back at once and the attempt is set aside; an answer without the word, or with
    any other, keeps the four figures alone, as before."""
    answer = '{"stopped": true, "ended": "exit", "seconds": 40.0, "service_seconds": 30.0, "holder": "failed"}\n'
    driver, _ = answering(CommandResult(0, answer))
    with pytest.raises(DriverError, match="no holder step"):
        driver.stop(SERVICE)
    kept = {"stopped": True, "ended": "exit", "seconds": 40.0, "service_seconds": 30.0}
    assert driver.last_stop == {**kept, "holder": "failed"}
    assert stop_record(driver)["stop"]["holder"] == "failed"
    assert stop_evidence(answer.replace('"failed"', '"n-1"')) == kept
