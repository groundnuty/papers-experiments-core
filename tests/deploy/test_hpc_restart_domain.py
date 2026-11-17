"""The provider's domain across a start (O58): finish.sh, finish-zone.sh and mint-registration-token.sh.

op_worker answers /configuration's `domain` from the provider record it reads over its Onezone connection, and null
while that connection is not up, although its REST listener already answers (live: a restart's identity check ran 1.9 s
before the connection and compared the word null with the recorded domain, a service that had not changed). So a start
waits, inside its readiness budget, for a domain that is neither null nor empty, then checks it as ever: a different
non-null domain still fails as a changed identity, and a domain that stays null fails as that, not as a change. The
word null is never recorded or compared as a value.

Run in plain bash on this host, over the stand-ins of tests/deploy/hpcnode.py: fake processes for the service, a curl
that answers null for a number of reads. Nothing here starts a container or opens a connection.
"""

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hpcop_restart_tests", HERE / "test_hpc_restart.py")
base = sys.modules.get("hpcop_restart_tests") or importlib.util.module_from_spec(spec)
sys.modules["hpcop_restart_tests"] = base
spec.loader.exec_module(base)
node, DOMAIN, Restart = base.node, base.DOMAIN, base.Restart

FOREVER = 10**6  # reads that answer null: more than any bound lets a start make
BOUND = "8"  # HPCOP_READY_SECONDS for a restart whose domain stays null: the service's own start fits well inside it
FIRST_START_BOUND = "3"  # a first start's service is already ready when finish.sh begins


@pytest.fixture
def processes():
    started = []
    yield started
    node.kill_all(started)


@pytest.fixture
def holder(tmp_path):
    path = tmp_path / "tmpfs"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def env(holder):
    return node.node_env(holder)


@pytest.fixture
def op(tmp_path, holder, env, processes):
    return Restart(tmp_path, holder, env, processes, "op")


@pytest.fixture
def oz(tmp_path, holder, env, processes):
    return Restart(tmp_path, holder, env, processes, "oz")


def config_reads(session):
    return json.loads(session.state.read_text(encoding="utf-8")).get("config_reads", 0)


def output(proc):
    return proc.stdout.read() if proc.stdout else ""


def first_start(session, processes, script="finish.sh"):
    """finish.sh (or finish-zone.sh) as a first start runs it: a fresh deployment, no identity recorded yet."""
    work = session.work
    for name in ("stop-requested", "keep-until", "identity.json", "service-verified"):
        (work / name).unlink(missing_ok=True)
    (work / "rootfs" / "root" / "service-ready.lock").touch()
    (work / "run.log").write_text("Congratulations! New deployment\nhpcop: Couchbase guard passed\n", encoding="utf-8")
    service = node.spawn(processes)
    return subprocess.run(
        ["bash", str(session.bundle / script), "--workdir", str(work), str(service.pid)],
        capture_output=True,
        text=True,
        env=session.env,
        timeout=90,
        check=False,
    )


def assert_nothing_rotated_and_kept(session, before):
    assert session.couchbase_password() == before
    assert not (node.secrets_of(session.work) / "couchbase-password.retired").exists()
    assert not (session.work / "service-verified").exists()
    assert (session.work / "stop-requested").read_text(encoding="utf-8").splitlines()[0] == "keep"
    status = node.script("status.sh", session.kind, "--workdir", str(session.work), env=session.env)
    assert json.loads(status.stdout)["alive"] is False


# --- a restart ---


@node.needs_pidfd
def test_o58_a_restart_waits_for_a_domain_that_is_null_while_op_worker_connects(op):
    """O58: /configuration answers domain null for the first reads, then the recorded domain: the restart waits, then
    the identity check passes and the restart completes (it failed as a changed identity before)."""
    op.set_state(domain_early={"reads": 3, "value": None})
    before = op.couchbase_password()
    proc = op.start()
    op.wait_ready(proc)
    assert config_reads(op) == 4
    assert op.stop().returncode == 0
    assert proc.wait(timeout=60) == 0, proc.stdout
    assert "identity unchanged since the first start" in output(proc)
    assert op.couchbase_password() != before  # the rest of the restart's checks ran after the wait


@node.needs_pidfd
def test_o58_a_restart_waits_for_an_empty_domain_too(op):
    """O58: an empty domain is no domain yet."""
    op.set_state(domain_early={"reads": 2, "value": ""})
    proc = op.start()
    op.wait_ready(proc)
    assert config_reads(op) == 3
    assert op.stop().returncode == 0
    assert proc.wait(timeout=60) == 0, proc.stdout


@node.needs_pidfd
def test_o58_a_domain_that_stays_null_fails_the_restart_as_null_and_not_as_changed(op):
    """O58: still null when the readiness budget ends (HPCOP_READY_SECONDS, 1800 by default): the restart fails
    saying the domain stayed null, after about the budget; nothing was rotated, the service is stopped, no marker, and
    `keep` is back in stop-requested."""
    op.env["HPCOP_READY_SECONDS"] = BOUND
    op.set_state(domain_early={"reads": FOREVER, "value": None})
    before = op.couchbase_password()
    began = time.monotonic()
    proc = op.start()
    assert proc.wait(timeout=90) != 0
    assert time.monotonic() - began < 60
    out = output(proc)
    assert "stayed null" in out
    assert "identity changed" not in out
    assert config_reads(op) > 3  # it polled, it did not give up at the first answer
    assert_nothing_rotated_and_kept(op, before)


@node.needs_pidfd
def test_o58_a_different_domain_after_the_wait_still_fails_as_a_changed_identity(op):
    """O58: the wait is for a domain, not for the recorded one: null twice, then another non-null domain, fails as a
    changed identity on the first non-null answer (three reads), and nothing is rotated."""
    op.set_state(domain="other.example.invalid", domain_early={"reads": 2, "value": None})
    before = op.couchbase_password()
    proc = op.start()
    assert proc.wait(timeout=90) != 0
    out = output(proc)
    assert "its domain is not the one recorded" in out
    assert "stayed null" not in out
    assert config_reads(op) == 3
    assert_nothing_rotated_and_kept(op, before)


@node.needs_pidfd
def test_o58_a_missing_domain_field_fails_at_once_and_is_not_waited_for(op):
    """O58: an answer without a domain field is not a connection still coming up: no polling, and not "stayed null"."""
    op.env["HPCOP_READY_SECONDS"] = "300"
    op.set_state(domain_missing=True)
    proc = op.start()
    assert proc.wait(timeout=90) != 0
    out = output(proc)
    assert "no readable domain" in out
    assert "stayed null" not in out
    assert config_reads(op) == 1


@pytest.mark.parametrize("bound", ["0", "-5", "1e3", "10 20", "0900000"])
def test_o58_a_readiness_budget_that_is_not_a_whole_number_of_seconds_is_refused(bound, op, processes):
    """O58: HPCOP_READY_SECONDS is validated before anything waits: no budget that could mean never, or nothing."""
    op.env["HPCOP_READY_SECONDS"] = bound
    done = first_start(op, processes)
    assert done.returncode != 0
    assert "HPCOP_READY_SECONDS" in done.stderr
    assert config_reads(op) == 0


# --- a first start ---


def test_o58_a_first_start_waits_for_the_domain_and_records_it_not_the_word_null(op, processes):
    """O58: the first start reads the same /configuration: a domain null for the first reads must not be recorded
    (identity.json held "null" for it, and every restart after it compared against that)."""
    op.set_state(domain_early={"reads": 2, "value": None})
    done = first_start(op, processes)
    assert done.returncode == 0, done.stderr
    record = json.loads((op.work / "identity.json").read_text(encoding="utf-8"))
    assert record == {"kind": "op", "providerId": "pid-1", "domain": DOMAIN}
    assert config_reads(op) == 3


def test_o58_a_first_start_whose_domain_stays_null_fails_and_records_no_identity(op, processes):
    """O58: the budget ends with the domain still null: the first start fails saying so, and writes no identity.json."""
    op.env["HPCOP_READY_SECONDS"] = FIRST_START_BOUND
    op.set_state(domain_early={"reads": FOREVER, "value": None})
    done = first_start(op, processes)
    assert done.returncode != 0
    assert "stayed null" in done.stderr
    assert not (op.work / "identity.json").exists()
    assert "check-defaults" not in " ".join(op.lines())


def test_o58_a_first_start_whose_provider_id_is_null_records_nothing(op, processes):
    """O58: a null provider id is no id: the start fails (finish.sh used to record the word null as the provider's)."""
    op.set_state(providerId=None)
    done = first_start(op, processes)
    assert done.returncode != 0
    assert "providerId" in done.stderr
    assert not (op.work / "identity.json").exists()


# --- a zone's first start, and the token it mints ---


def test_o58_a_zone_user_id_that_is_null_fails_the_first_start_and_records_nothing(oz, processes):
    """O58: finish-zone.sh took the admin's null user id for the id "null" and recorded it."""
    oz.set_state(adminUserId=None)
    done = first_start(oz, processes, "finish-zone.sh")
    assert done.returncode != 0
    assert "userId" in done.stderr
    assert not (oz.work / "identity.json").exists()


def test_o58_a_zone_with_no_domain_of_its_own_records_the_node_name(oz, processes):
    """O58: the zone's /configuration domain null is "reports none": the node's name stands in, never the word null."""
    oz.set_state(domain=None)
    done = first_start(oz, processes, "finish-zone.sh")
    assert done.returncode == 0, done.stderr
    record = json.loads((oz.work / "identity.json").read_text(encoding="utf-8"))
    assert record["domain"] == "provider-a.example.invalid"  # the stand-in hostname -f


def mint(session):
    return node.script("mint-registration-token.sh", "provider-a", "--workdir", str(session.work), env=session.env)


def test_o58_a_registration_token_the_zone_answers_with_is_printed(oz):
    """O58: the token goes to stdout, alone."""
    oz.set_state(token="token-1")
    done = mint(oz)
    assert (done.returncode, done.stdout) == (0, "token-1\n"), done.stderr


def test_o58_a_null_registration_token_is_not_printed_as_a_token(oz):
    """O58: a null token is no token: nothing on stdout for deliver-token.sh to hand to a provider, and a failure."""
    oz.set_state(token=None)
    done = mint(oz)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "hpcop: ERROR" in done.stderr
