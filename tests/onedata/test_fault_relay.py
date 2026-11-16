"""O58: the fault relay of an HPC federation (`expcore_onedata.fault_relay`): the dispatcher's requests, as files on the
federation's root, run by a relay on the login node, which runs only its own federation's fault verbs.

Each test runs the relay in this process: a client's wait for an answer serves the relay once (`serving`), so a request
is answered as the relay would answer it between two of the client's looks, and nothing runs but a fake runner.
"""

import errno
import json
import os
import stat
import subprocess
import sys
import threading
import time
import uuid

import pytest
from fault_support import OUTAGE_TARGET, SERVICES, FakeClock, injector_for
from hpc_support import FEDERATION, write_hpc_platform, write_state

from expcore.campaign import prerun
from expcore.errors import InfraError
from expcore_onedata.admission import FederationBusy, run_admission
from expcore_onedata.compose import CommandResult, DriverError
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_hpc import HpcDriver, Request
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.fault_relay import (
    FORMAT,
    RELAY_NOTE,
    Relay,
    RelayClient,
    RelayDown,
    beat,
    clean_environment,
    relay_dir,
)
from expcore_onedata.fault_settings import hpc_driver, load_faults
from expcore_onedata.sandbox.kinds import FEDERATION_BUSY, PROVIDER_OUTAGE

SERVICE = SERVICES["beta"]
PREFIX = ["/opt/venv/bin/python", "hpcfed.py", "--root", "ROOT", "--layout", "LAYOUT"]
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}


class Verbs:
    """The federation's verbs behind the relay: every command recorded, each answered `answer`."""

    def __init__(self, answer=None):
        self.answer = answer or CommandResult(0, json.dumps({"running": True, "identity": "1:4001:901"}) + "\n")
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, *, timeout_s):
        self.calls.append((list(argv), timeout_s))
        return self.answer


def root_of(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    write_state(root, tuple(SERVICES.values()))
    return root


def relay_of(root, verbs):
    return Relay(root, PREFIX, runner=verbs, environ={"PATH": "/usr/bin:/bin", "HOME": "/home/someone"})


def serving(relay, clock):
    """A client's pause between two looks for its answer, in which the relay answers what waits."""

    def pause(seconds):
        clock.sleep(seconds)
        relay.serve_once()

    return pause


def client_of(root, relay, clock, **options):
    return RelayClient(root, wait_s=5, clock=clock, sleep=serving(relay, clock), **options)


def test_o58_a_request_is_run_by_the_relay_as_the_federations_verb_and_answered_as_its_end(tmp_path):
    """O58: the client writes the request, the relay takes it, runs `hpcfed.py <verb> <service>` with the prefix it
    was started with, within the request's bound, and answers with the command's end; every file is the user's alone,
    and none is left once the answer is read."""
    root, verbs = root_of(tmp_path), Verbs()
    relay, clock = relay_of(root, verbs), FakeClock()
    with relay.holding():
        done = client_of(root, relay, clock)(Request("status", SERVICE), timeout_s=10)
        ((argv, bound),) = verbs.calls
        assert argv == [*PREFIX, "status", SERVICE]
        assert 0 < bound <= 10
        assert done == verbs.answer
        directory = relay_dir(root)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for name in ("requests", "taken", "answers"):
            assert stat.S_IMODE((directory / name).stat().st_mode) == 0o700
            assert list((directory / name).iterdir()) == []
        assert stat.S_IMODE((directory / "heartbeat.json").stat().st_mode) == 0o600


def test_o58_an_exec_runs_only_the_allowed_command_by_its_name(tmp_path):
    """O58: an exec request carries a command's name, never an argv; the relay runs that command's fixed argv inside
    the service through the federation's exec verb."""
    root, verbs = root_of(tmp_path), Verbs(CommandResult(0, "pong\n"))
    relay, clock = relay_of(root, verbs), FakeClock()
    with relay.holding():
        driver = HpcDriver(client_of(root, relay, clock), timeout_s=10, probe=lambda service: True)
        assert driver.ping(SERVICE) is True
    ((argv, _),) = verbs.calls
    assert argv == [*PREFIX, "exec", SERVICE, "--", "/usr/sbin/op_worker", "ping"]


def write_request(root, relay, **fields):
    """A request file as a client writes it, with `fields` changed; its id."""
    key = uuid.uuid4().hex
    record = {
        "format": FORMAT,
        "id": key,
        "relay": relay.nonce,
        "federation": FEDERATION,
        "verb": "status",
        "service": SERVICE,
        "bound_s": 10,
        "deadline": 4_000_000_000,
        **fields,
    }
    record = {name: value for name, value in record.items() if value is not None}
    (relay_dir(root) / "requests" / f"{key}.json").write_text(json.dumps(record), encoding="utf-8")
    return key


def answer(root, key):
    return json.loads((relay_dir(root) / "answers" / f"{key}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("fields", "word"),
    [
        ({"service": "store-nine"}, "not_allowed"),  # a service this federation's state does not list
        ({"service": "zone"}, "not_allowed"),  # the zone: listed, but no provider, so never stopped or exec'd into
        ({"federation": "hpcop-other-99999999"}, "other_federation"),
        ({"verb": "exec", "command": "bash"}, "not_allowed"),
        ({"verb": "exec", "command": None}, "not_allowed"),
        ({"verb": "restart"}, "not_allowed"),
        ({"argv": ["rm", "-rf", "/"]}, "unknown_key"),
        ({"deadline": 1}, "expired"),
        ({"bound_s": "10"}, "malformed"),
    ],
)
def test_o58_the_relay_refuses_another_federations_service_an_unlisted_command_and_anything_else(
    tmp_path, fields, word
):
    """O58: the relay runs only the fault verbs, for its own federation's services, with an allowed command for an
    exec; anything else, a free-form argv among them, is answered refused, with a word, and nothing runs."""
    root, verbs = root_of(tmp_path), Verbs()
    relay = relay_of(root, verbs)
    with relay.holding():
        key = write_request(root, relay, **fields)
        assert relay.serve_once() == 1
    assert answer(root, key)["refused"] == word
    assert verbs.calls == []


def test_o58_a_request_written_again_under_an_id_the_relay_took_never_runs_twice(tmp_path):
    """O58: the same request file written again (same id, same relay, a deadline still ahead) after the relay took and
    answered it: refused as `replayed`, nothing runs, and the first answer is never overwritten."""
    root, verbs = root_of(tmp_path), Verbs()
    relay = relay_of(root, verbs)
    with relay.holding():
        key = write_request(root, relay, verb="exec", command="restart_link")
        replay = (relay_dir(root) / "requests" / f"{key}.json").read_text(encoding="utf-8")
        assert relay.serve_once() == 1
        first = answer(root, key)
        (relay_dir(root) / "requests" / f"{key}.json").write_text(replay, encoding="utf-8")
        assert relay.serve_once() == 1
    assert len(verbs.calls) == 1
    assert answer(root, key) == first
    assert "refused" not in first


@pytest.mark.parametrize(
    "fields",
    [
        {"verb": "exec", "command": []},
        {"service": []},
        {"service": {"a": 1}},
        {"verb": ["stop"]},
        {"deadline": float("nan")},
        {"bound_s": float("inf")},
        {"bound_s": 0},
        {"bound_s": -5},
    ],
    ids=[
        "command a list",
        "service a list",
        "service a dict",
        "verb a list",
        "nan deadline",
        "inf bound",
        "zero",
        "neg",
    ],
)
def test_o58_a_malformed_request_is_refused_and_the_relay_goes_on_serving(tmp_path, fields):
    """O58: a field of the wrong type, or a deadline or bound that is no finite positive number, is refused as
    `malformed` with nothing run, and never stops the relay: the next request is served."""
    root, verbs = root_of(tmp_path), Verbs()
    relay = relay_of(root, verbs)
    with relay.holding():
        bad = write_request(root, relay, **fields)
        assert relay.serve_once() == 1
        good = write_request(root, relay)
        assert relay.serve_once() == 1
    assert answer(root, bad)["refused"] == "malformed"
    assert "refused" not in answer(root, good)
    assert len(verbs.calls) == 1


def test_o58_a_verb_that_fails_after_it_may_have_run_is_unknown_never_not_run(tmp_path):
    """O58: the runner raising anything but a failure to start (its output undecodable, say) once the verb may have
    run is answered as a command with no answer, never `not_run`, so a stop or a link restart is never retried as
    one that did not happen."""

    def broken(argv, *, timeout_s):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    root = root_of(tmp_path)
    relay = relay_of(root, broken)
    with relay.holding():
        key = write_request(root, relay, verb="exec", command="restart_link")
        assert relay.serve_once() == 1
    assert answer(root, key)["timed_out"] is True


def test_o58_a_relay_restart_never_replays_a_request_made_for_the_relay_before_it(tmp_path):
    """O58: a request names the relay it was made for (its heartbeat's nonce). A relay that stopped before it took one
    leaves it waiting; the next relay of the same root answers it refused (`other_relay`) and runs nothing, so a
    restart never runs an old request."""
    root, verbs = root_of(tmp_path), Verbs()
    earlier = relay_of(root, verbs)
    with earlier.holding():
        key = write_request(root, earlier)
    later = relay_of(root, verbs)
    with later.holding():
        assert later.serve_once() == 1
    assert later.nonce != earlier.nonce
    assert answer(root, key)["refused"] == "other_relay"
    assert verbs.calls == []


def test_o58_a_request_the_relay_never_takes_is_withdrawn_and_reads_as_not_sent(tmp_path):
    """O58: no answer by the request's deadline, and the relay never took it: the client withdraws it (the file is
    gone, so nothing will ever run it) and fails as nothing sent; a stop that fails so is still of unknown outcome to
    the injector, which never takes it for done."""
    root, verbs = root_of(tmp_path), Verbs()
    relay, clock = relay_of(root, verbs), FakeClock()
    with relay.holding():
        client = RelayClient(root, wait_s=1, clock=clock, sleep=clock.sleep)  # the relay never looks
        with pytest.raises(RelayDown):
            client(Request("stop", SERVICE), timeout_s=3)
        assert list((relay_dir(root) / "requests").iterdir()) == []
        assert relay.serve_once() == 0
    assert verbs.calls == []


def test_o58_a_request_the_relay_took_and_never_answered_is_unknown_never_done(tmp_path):
    """O58: the relay took the request (it may have run it, or still runs it) and no answer came by its deadline: the
    client cannot withdraw it and raises a command with no answer (`TimeoutExpired`), which the driver never reads as
    done."""
    root, verbs = root_of(tmp_path), Verbs()
    relay, clock = relay_of(root, verbs), FakeClock()

    def taken_and_lost(seconds):  # the relay takes the request and dies before it answers
        clock.sleep(seconds)
        for path in (relay_dir(root) / "requests").glob("*.json"):
            os.rename(path, relay_dir(root) / "taken" / path.name)

    with relay.holding():
        client = RelayClient(root, wait_s=1, clock=clock, sleep=taken_and_lost)
        with pytest.raises(subprocess.TimeoutExpired):
            client(Request("start", SERVICE), timeout_s=3)
        driver = HpcDriver(client, timeout_s=3, probe=lambda service: True)
        with pytest.raises(DriverError, match="did not finish"):
            driver.start(SERVICE)
        assert driver.restart_link(SERVICE) == "command_timeout"


def test_o58_an_outage_whose_stop_the_relay_never_answers_is_journalled_unknown_and_stays_open(tmp_path):
    """O58: the relay takes every request and answers none: the injector's stop is of unknown outcome (journalled
    `stop_unknown`), its restore never sees the provider down (every status unanswered), so the injection stays open,
    `stop_unsettled`, and the start fails as `injector_failure`: never a stop or a restore taken for done."""
    platform = write_hpc_platform(tmp_path)
    clock = FakeClock()
    relay = relay_of(tmp_path, Verbs())

    def taken_and_lost(seconds):
        clock.sleep(seconds)
        for path in (relay_dir(tmp_path) / "requests").glob("*.json"):
            os.rename(path, relay_dir(tmp_path) / "taken" / path.name)

    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    relayed = RelayClient(tmp_path, wait_s=settings.relay_wait_s, clock=clock, sleep=taken_and_lost)
    read = []

    def transport(request, *, timeout_s):  # the identity before the injection is answered; nothing after it is
        if request.verb == "status" and not read:
            read.append(request)
            return CommandResult(0, json.dumps({"running": True, "identity": "1:4001:901"}) + "\n")
        return relayed(request, timeout_s=timeout_s)

    driver = hpc_driver(settings, probe=lambda service: True, transport=transport)
    with relay.holding():
        injector = injector_for(platform, driver, clock=clock)
        with pytest.raises(InfraError) as caught:
            injector.start(RUN)
    assert (caught.value.kind, caught.value.target) == ("injector_failure", OUTAGE_TARGET)
    lines = [json.loads(line) for line in (tmp_path / "fault-journal" / JOURNAL).read_text().splitlines()]
    events = [(line["event"], line.get("how")) for line in lines]
    assert events == [
        ("injecting", None),
        ("stopping", None),
        ("stop_unknown", None),
        ("clear_failed", "stop_unsettled"),
    ]
    assert "cleared" not in [event for event, _ in events]


def test_o58_a_dead_relay_refuses_the_next_outage_run_at_its_admission_uncharged(tmp_path):
    """O58: the fault gate's pre-run check notes whether the relay beats; with none, an outage run is refused at its
    admission as the hold `federation_busy` (no attempt used), while a baseline run is admitted; once a relay beats
    and a fresh check saw it, the outage run is admitted."""
    platform = write_hpc_platform(tmp_path)
    config = load_platform(platform, require_secrets=False)
    settings = load_faults(platform, config)
    gate = FaultGate(settings, hpc_driver(settings, probe=lambda service: True, transport=Verbs()))
    admit = run_admission(config)
    outage = ((PROVIDER_OUTAGE,), {PROVIDER_OUTAGE: OUTAGE_TARGET}, threading.Event(), False)
    with prerun.opened():
        assert gate().ok
        assert prerun.current()[RELAY_NOTE]["alive"] is False
        with pytest.raises(FederationBusy, match="no live fault relay") as refused, admit(*outage):
            pass
        with admit((), {}, threading.Event(), False) as mode:
            assert mode == "shared"
    assert refused.value.kind == FEDERATION_BUSY
    relay = relay_of(tmp_path, Verbs())
    with relay.holding(), prerun.opened():
        assert gate().ok
        assert prerun.current()[RELAY_NOTE] == {"alive": True, "nonce": relay.nonce}
        with admit(*outage) as mode:
            assert mode == "exclusive"


def test_o58_a_relay_that_stops_beating_after_the_check_refuses_the_outage_run_at_its_admission(tmp_path):
    """O58: the check saw the relay, which then stopped: the admission reads the heartbeat again and refuses."""
    platform = write_hpc_platform(tmp_path)
    config = load_platform(platform, require_secrets=False)
    settings = load_faults(platform, config)
    gate = FaultGate(settings, hpc_driver(settings, probe=lambda service: True, transport=Verbs()))
    relay = relay_of(tmp_path, Verbs())
    outage = ((PROVIDER_OUTAGE,), {PROVIDER_OUTAGE: OUTAGE_TARGET}, threading.Event(), False)
    with prerun.opened():
        with relay.holding():
            assert gate().ok
        assert beat(tmp_path) is None  # its heartbeat goes when it stops
        with pytest.raises(FederationBusy, match="not answering"), run_admission(config)(*outage):
            pass


def test_o58_a_stale_heartbeat_is_a_relay_not_answering(tmp_path):
    """O58: a heartbeat whose wall time is more than 30 s from the reader's is no live relay."""
    root = root_of(tmp_path)
    relay = relay_of(root, Verbs())
    with relay.holding():
        written = json.loads((relay_dir(root) / "heartbeat.json").read_text())["wall"]
        assert beat(root, lambda: written + 29) is not None
        assert beat(root, lambda: written + 31) is None


def test_o58_the_relay_never_holds_a_secret_in_its_environment_and_its_verbs_get_a_clean_one(tmp_path):
    """O58: a relay started with a secret-like name in its environment refuses to start (names only in the refusal),
    and the verbs it runs get a few plain names of its environment, never anything else."""
    root = root_of(tmp_path)
    with pytest.raises(PermissionError, match="ONEZONE_TOKEN") as refused:
        Relay(root, PREFIX, environ={"PATH": "/usr/bin", "ONEZONE_TOKEN": "abc123"})
    assert "abc123" not in str(refused.value)
    assert clean_environment({"PATH": "/usr/bin", "HOME": "/h", "PYTHONPATH": "/x", "LD_PRELOAD": "/y"}) == {
        "PATH": "/usr/bin",
        "HOME": "/h",
    }


def test_o58_a_second_relay_of_the_same_root_refuses_to_start(tmp_path):
    """O58: one relay per federation: a second one finds the first's lock held and refuses, so no request is taken
    twice."""
    root = root_of(tmp_path)
    first, second = relay_of(root, Verbs()), relay_of(root, Verbs())
    with first.holding(), pytest.raises(PermissionError, match="another fault relay"), second.holding():
        pass


@pytest.fixture
def kills(monkeypatch):
    """Every `os.kill` made while the test runs, recorded as (pid, signal) and then really made, so a test's own cleanup
    still works."""
    sent: list[tuple[int, int]] = []
    real = os.kill

    def kill(pid, signum):
        sent.append((pid, signum))
        real(pid, signum)

    monkeypatch.setattr(os, "kill", kill)
    return sent


def refused_by_the_kernel(*_args, **_kwargs):
    raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))


def without_pidfd_open(monkeypatch, fault_relay, python_has_it):
    """A kernel with no `pidfd_open` system call (ENOSYS): `os.pidfd_open` raises it where this Python has the function,
    else the raw call does. No other pidfd call is allowed after it."""
    if python_has_it:
        monkeypatch.setattr(fault_relay.os, "pidfd_open", refused_by_the_kernel, raising=False)
    else:
        monkeypatch.delattr(fault_relay.os, "pidfd_open", raising=False)

        def syscall(number, *args):
            assert number == fault_relay.PIDFD_OPEN, f"system call {number} tried after pidfd_open was refused"
            raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))

        monkeypatch.setattr(fault_relay, "_syscall", syscall)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("pidfd_send_signal tried after pidfd_open was refused")

    monkeypatch.setattr(fault_relay.signal, "pidfd_send_signal", forbidden, raising=False)


@pytest.fixture
def sleeper():
    """A process of ours to signal, ended and reaped whatever the test does."""
    child = subprocess.Popen(["sleep", "30"])
    yield child
    child.kill()
    child.wait()


@pytest.mark.parametrize("raw", [False, True])
def test_o58_the_relay_is_stopped_only_while_its_pid_is_still_the_relay_and_without_the_python_pidfd_calls(
    tmp_path, monkeypatch, kills, sleeper, raw
):
    """O58: `down` stops the relay by its heartbeat's pid and start tick, through a pidfd held across the check, so a
    PID another process took is never signalled; a Python built without `os.pidfd_open` (uv's standalone builds) uses
    the raw system calls instead. The signal goes through the pidfd, never through `os.kill`."""
    from expcore_onedata import fault_relay

    if not kernel_has_pidfd(fault_relay):
        pytest.skip("this kernel has no pidfd_open: the fallback's tests run instead")
    if raw:
        monkeypatch.delattr(fault_relay.os, "pidfd_open", raising=False)
        monkeypatch.delattr(fault_relay.signal, "pidfd_send_signal", raising=False)
    start = fault_relay.proc_start(sleeper.pid)
    assert start is not None
    assert fault_relay.signal_if_started(sleeper.pid, str(int(start) + 1), fault_relay.signal.SIGTERM) is False
    assert sleeper.poll() is None
    assert fault_relay.signal_if_started(sleeper.pid, start, fault_relay.signal.SIGTERM) is True
    assert sleeper.wait(timeout=10) == -fault_relay.signal.SIGTERM
    assert [pid for pid, _ in kills if pid == sleeper.pid] == []


@pytest.mark.parametrize("python_has_it", [True, False])
def test_o58_where_the_kernel_has_no_pidfd_open_the_relay_is_signalled_through_its_start_tick(
    monkeypatch, kills, sleeper, python_has_it
):
    """O58: a login node whose kernel lacks `pidfd_open` answers ENOSYS (whether `os.pidfd_open` raises it or the raw
    call does): the stop then checks the process's start tick and sends the signal with `os.kill`, once, and the
    process ends. The first live `down` found the stop failing on the ENOSYS and leaving a running relay unverified."""
    from expcore_onedata import fault_relay

    without_pidfd_open(monkeypatch, fault_relay, python_has_it)
    start = fault_relay.proc_start(sleeper.pid)
    assert start is not None
    assert fault_relay.signal_if_started(sleeper.pid, start, fault_relay.signal.SIGTERM) is True
    assert sleeper.wait(timeout=10) == -fault_relay.signal.SIGTERM
    sent = [(pid, signum) for pid, signum in kills if pid == sleeper.pid]
    assert sent == [(sleeper.pid, fault_relay.signal.SIGTERM)]


@pytest.mark.parametrize("python_has_it", [True, False])
def test_o58_without_pidfd_open_a_pid_whose_start_tick_differs_is_never_signalled(
    monkeypatch, kills, sleeper, python_has_it
):
    """O58: on the fallback path too, a PID that shows another start tick than the recorded one (the relay died and the
    PID went to another process) is not signalled: nothing is sent and the process is untouched."""
    from expcore_onedata import fault_relay

    without_pidfd_open(monkeypatch, fault_relay, python_has_it)
    start = fault_relay.proc_start(sleeper.pid)
    assert start is not None
    assert fault_relay.signal_if_started(sleeper.pid, str(int(start) + 1), fault_relay.signal.SIGTERM) is False
    assert sleeper.poll() is None
    assert [pid for pid, _ in kills if pid == sleeper.pid] == []


def test_o58_without_pidfd_open_a_process_that_is_gone_is_reported_gone_and_nothing_is_sent(monkeypatch, kills):
    """O58: the fallback reads the start tick first: a process that is gone (no /proc entry) raises
    `ProcessLookupError`, as the pidfd path does, and no signal is sent."""
    from expcore_onedata import fault_relay

    without_pidfd_open(monkeypatch, fault_relay, False)
    child = subprocess.Popen(["sleep", "30"])
    start = fault_relay.proc_start(child.pid)
    assert start is not None
    child.kill()
    child.wait()
    kills.clear()  # the test's own kill above
    with pytest.raises(ProcessLookupError):
        fault_relay.signal_if_started(child.pid, start, fault_relay.signal.SIGTERM)
    assert kills == []


@pytest.mark.parametrize(
    ("pid", "start"),
    [(0, "1"), (-1, "1"), (1, "1"), ("ours", None), ("ours", ""), ("ours", 123)],
)
def test_o58_without_pidfd_open_no_pid_that_names_a_group_or_init_and_no_missing_tick_reaches_os_kill(
    monkeypatch, pid, start
):
    """O58: `os.kill` takes 0 and negative numbers for a process group or every process, and a missing start tick would
    equal an unreadable one: the fallback sends nothing for either, and nothing for PID 1 (pidfd_open refused them)."""
    from expcore_onedata import fault_relay

    without_pidfd_open(monkeypatch, fault_relay, True)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda target, signum: sent.append((target, signum)))  # never delegates
    target = os.getpid() if pid == "ours" else pid
    assert fault_relay.signal_if_started(target, start, fault_relay.signal.SIGTERM) is False
    assert sent == []


def test_o58_a_refusal_of_pidfd_open_other_than_no_such_call_is_raised_and_nothing_is_sent(monkeypatch, kills, sleeper):
    """O58: only ENOSYS (the call does not exist here) falls back to the start tick; any other failure of the pidfd
    (here too many open files) is raised, and nothing is sent by another way."""
    from expcore_onedata import fault_relay

    def full(*_args, **_kwargs):
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

    monkeypatch.setattr(fault_relay.os, "pidfd_open", full, raising=False)
    start = fault_relay.proc_start(sleeper.pid)
    assert start is not None
    with pytest.raises(OSError, match=os.strerror(errno.EMFILE)) as raised:
        fault_relay.signal_if_started(sleeper.pid, start, fault_relay.signal.SIGTERM)
    assert raised.value.errno == errno.EMFILE
    assert sleeper.poll() is None
    assert [pid for pid, _ in kills if pid == sleeper.pid] == []


def kernel_has_pidfd(fault_relay):
    try:
        os.close(fault_relay._open_pidfd(os.getpid()))
    except OSError as error:
        if error.errno == errno.ENOSYS:
            return False
        raise
    return True


@pytest.mark.parametrize("how", ["pidfd", "enosys_python", "enosys_raw"])
def test_o58_a_process_with_the_recorded_start_tick_is_signalled_only_when_its_command_line_is_the_relays(
    monkeypatch, kills, sleeper, how
):
    """O58: the heartbeat is a file; a start tick that matches does not make a process the relay. When the caller names
    the words of the relay's command line, a process that lacks one is never signalled, through the pidfd or by the
    start tick (ENOSYS), and one that has them all is."""
    from expcore_onedata import fault_relay

    if how == "pidfd" and not kernel_has_pidfd(fault_relay):
        pytest.skip("this kernel has no pidfd_open")
    if how != "pidfd":
        without_pidfd_open(monkeypatch, fault_relay, how == "enosys_python")
    start = fault_relay.proc_start(sleeper.pid)
    assert start is not None
    term = fault_relay.signal.SIGTERM
    assert fault_relay.signal_if_started(sleeper.pid, start, term, argv=("sleep", "fault_relay")) is False
    assert fault_relay.signal_if_started(sleeper.pid, start, term, argv=("fault_relay",)) is False
    assert sleeper.poll() is None
    assert [pid for pid, _ in kills if pid == sleeper.pid] == []
    assert fault_relay.signal_if_started(sleeper.pid, start, term, argv=("sleep", "30")) is True
    assert sleeper.wait(timeout=10) == -term


@pytest.mark.parametrize("how", ["pidfd", "enosys_raw"])
def test_o58_the_stop_never_signals_the_process_a_heartbeat_names_when_it_is_not_a_relay(
    tmp_path, monkeypatch, kills, sleeper, how
):
    """O58: a heartbeat that names another process of the user's and its real start tick (written by mistake, or left
    by a relay of another host that shares the root) is no relay: `fault_relay stop` signals nothing, fails, and leaves
    the heartbeat to the end of its client."""
    from expcore_onedata import fault_relay

    if how == "pidfd" and not kernel_has_pidfd(fault_relay):
        pytest.skip("this kernel has no pidfd_open")
    if how != "pidfd":
        without_pidfd_open(monkeypatch, fault_relay, False)
    root = root_of(tmp_path)
    relay_dir(root).mkdir(mode=0o700)
    start = fault_relay.proc_start(sleeper.pid)
    assert start is not None
    fault_relay._write(
        relay_dir(root) / "heartbeat.json",
        {"format": FORMAT, "nonce": uuid.uuid4().hex, "pid": sleeper.pid, "start": start, "wall": time.time()},
    )
    assert fault_relay._stop(root, timeout_s=0.5) == 1
    assert sleeper.poll() is None
    assert [pid for pid, _ in kills if pid == sleeper.pid] == []
    assert (relay_dir(root) / "heartbeat.json").exists()


def test_o58_the_stop_command_ends_a_running_relay_where_the_kernel_has_no_pidfd_open(tmp_path, monkeypatch):
    """O58: `fault_relay stop --root R` ends a real relay and exits 0 once its heartbeat is gone, on a kernel without
    `pidfd_open`, where it exited 1 on the ENOSYS and left `down` unable to call a running relay stopped."""
    from expcore_onedata import fault_relay

    root, log = root_of(tmp_path), tmp_path / "relay.log"
    with log.open("wb") as sink:
        relay = subprocess.Popen(
            [sys.executable, "-B", "-m", "expcore_onedata.fault_relay", "serve", "--root", str(root), "--", *PREFIX],
            env=clean_environment(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
        )
    try:
        deadline = time.monotonic() + 30
        while beat(root) is None:
            assert relay.poll() is None, f"the relay ended at its start: {log.read_text()}"
            assert time.monotonic() < deadline, f"the relay never beat: {log.read_text()}"
            time.sleep(0.1)
        without_pidfd_open(monkeypatch, fault_relay, False)
        assert fault_relay.main(["stop", "--root", str(root)]) == 0
        assert relay.wait(timeout=30) == 0
        assert not (relay_dir(root) / "heartbeat.json").exists()
    finally:
        if relay.poll() is None:
            relay.kill()
        relay.wait()
