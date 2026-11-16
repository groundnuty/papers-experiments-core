"""O58: the fault relay of an HPC federation (`expcore_onedata.fault_relay`): the dispatcher's requests, as files on the
federation's root, run by a relay on the login node, which runs only its own federation's fault verbs.

Each test runs the relay in this process: a client's wait for an answer serves the relay once (`serving`), so a request
is answered as the relay would answer it between two of the client's looks, and nothing runs but a fake runner.
"""

import json
import os
import stat
import subprocess
import threading
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


@pytest.mark.parametrize("raw", [False, True])
def test_o58_the_relay_is_stopped_only_while_its_pid_is_still_the_relay_and_without_the_python_pidfd_calls(
    tmp_path, monkeypatch, raw
):
    """O58: `down` stops the relay by its heartbeat's pid and start tick, through a pidfd held across the check, so a
    PID another process took is never signalled; a Python built without `os.pidfd_open` (uv's standalone builds) uses
    the raw system calls instead."""
    from expcore_onedata import fault_relay

    if raw:
        monkeypatch.delattr(fault_relay.os, "pidfd_open", raising=False)
        monkeypatch.delattr(fault_relay.signal, "pidfd_send_signal", raising=False)
    child = subprocess.Popen(["sleep", "30"])
    try:
        start = fault_relay.proc_start(child.pid)
        assert start is not None
        assert fault_relay.signal_if_started(child.pid, str(int(start) + 1), fault_relay.signal.SIGTERM) is False
        assert child.poll() is None
        assert fault_relay.signal_if_started(child.pid, start, fault_relay.signal.SIGTERM) is True
        assert child.wait(timeout=10) == -fault_relay.signal.SIGTERM
    finally:
        child.kill()
        child.wait()
