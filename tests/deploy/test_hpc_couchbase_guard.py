"""The HPC bundle's Couchbase guard (deploy/hpc-federation/bundle/hpcop-cb-init.py --watch), against the bundle's
loopback stand-in for Couchbase 4.5's REST (deploy/hpc-federation/tests/fake_couchbase.py).

Each test plays onepanel's Couchbase setup itself, as the image runs it: the memory quota, the services, the
administrator (POST /settings/web, which couchbase-cli's cluster-init skips for a password over its limit), then the
bucket. The guard must pass only when this launch's administrator answers GET /settings/web and an anonymous request
is refused, and fail at once, stopping the start script, otherwise. Everything stays on 127.0.0.1.
"""

import base64
import http.client
import os
import secrets
import socket
import string
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
GUARD = HPC / "bundle" / "hpcop-cb-init.py"
FAKE = HPC / "tests" / "fake_couchbase.py"
HOST = "127.0.0.1"
ALNUM = string.ascii_letters + string.digits
WAIT_S = 20


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def write_secret(path: Path, value: str) -> None:
    path.write_text(value, encoding="ascii")
    path.chmod(0o600)


@pytest.fixture
def launch(tmp_path):
    """This launch's administrator, in 0600 files as node-init.sh writes them: (secrets dir, user, password)."""
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    user = "cb" + secrets.token_hex(6)
    password = "".join(secrets.choice(ALNUM) for _ in range(24))
    write_secret(directory / "couchbase-user", user)
    write_secret(directory / "couchbase-password", password)
    return directory, user, password


@pytest.fixture
def processes():
    started: list[subprocess.Popen] = []
    yield started
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def start_fake(processes, port, *, lifetime=30.0, provisioned=None, anon_admin=False):
    """The stand-in, opening its port after 0.3 s; returns once it listens."""
    env = {**os.environ, "FAKE_CB_ANON_ADMIN": "1" if anon_admin else "0"}
    argv = [sys.executable, "-I", str(FAKE), str(port), "0.3", str(lifetime)]
    if provisioned is not None:
        argv.append(provisioned)
    fake = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, text=True)
    processes.append(fake)
    assert fake.stdout is not None
    assert fake.stdout.readline().strip() == "listening"
    return fake


def start_guard(processes, secrets_dir, port, *, timeout=WAIT_S):
    """The guard as hpcop-start.sh runs it, with START_PID 0: nothing to signal."""
    argv = [sys.executable, "-I", str(GUARD), "--watch", str(secrets_dir), str(timeout), str(port), "0"]
    guard = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    processes.append(guard)
    return guard


def post(port, path, form, auth=None) -> int:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if auth is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    conn = http.client.HTTPConnection(HOST, port, timeout=10)
    try:
        conn.request("POST", path, body=urllib.parse.urlencode(form), headers=headers)
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def onepanel_setup(port, user, password, *, create_administrator=True):
    """onepanel's steps: the quota, then cluster-init (services, administrator), then bucket-create."""
    auth = (user, password)
    assert post(port, "/pools/default", {"memoryQuota": "256"}, auth) == 200
    assert post(port, "/node/controller/setupServices", {"services": "kv"}) == 200
    if create_administrator:
        assert post(port, "/settings/web", {"username": user, "password": password, "port": "SAME"}) == 200
    assert post(port, "/pools/default/buckets/", {"name": "onedata", "ramQuotaMB": "256"}, auth) == 202


def verdict(guard) -> tuple[int, str]:
    out, _ = guard.communicate(timeout=WAIT_S + 10)
    return guard.returncode, out


def test_o48_the_guard_passes_when_this_launchs_administrator_answers_and_an_anonymous_request_is_refused(
    launch, processes
):
    """O48: the guard passes only on both requests: this launch's administrator gets 200 and is named, and an
    anonymous GET /settings/web gets 401."""
    secrets_dir, user, password = launch
    port = free_port()
    guard = start_guard(processes, secrets_dir, port)
    start_fake(processes, port)
    onepanel_setup(port, user, password)
    status, out = verdict(guard)
    assert status == 0, out
    assert "hpcop: Couchbase guard passed" in out
    assert "FAILED" not in out


def test_o48_a_bucket_without_an_administrator_fails_the_start(launch, processes):
    """O48: the third live run's incident. cluster-init skipped the administrator with exit status 0, bucket-create
    ran, and Couchbase served with none: the guard fails as soon as the bucket shows."""
    secrets_dir, user, password = launch
    port = free_port()
    guard = start_guard(processes, secrets_dir, port)
    start_fake(processes, port)
    onepanel_setup(port, user, password, create_administrator=False)
    status, out = verdict(guard)
    assert status == 2, out
    assert "hpcop: Couchbase guard FAILED" in out
    assert "has a bucket (onedata) but no administrator" in out
    assert "guard passed" not in out


def test_o48_an_anonymous_admin_request_that_is_answered_fails_the_start(launch, processes):
    """O48: our administrator answers, but so does an anonymous caller: anyone can act as the administrator."""
    secrets_dir, user, password = launch
    port = free_port()
    guard = start_guard(processes, secrets_dir, port)
    start_fake(processes, port, anon_admin=True)
    onepanel_setup(port, user, password)
    status, out = verdict(guard)
    assert status == 2, out
    assert "answers an anonymous GET /settings/web with HTTP 200" in out
    assert "guard passed" not in out


def test_o48_an_administrator_that_is_not_this_launchs_fails_the_start(launch, processes):
    """O48: Couchbase already has an administrator, someone else's: ours is refused (401) and the start fails."""
    secrets_dir, _, _ = launch
    port = free_port()
    other = "cbffffffffffff:" + "".join(secrets.choice(ALNUM) for _ in range(24))
    guard = start_guard(processes, secrets_dir, port)
    start_fake(processes, port, provisioned=other)
    status, out = verdict(guard)
    assert status == 2, out
    assert "has an administrator that is not this launch's" in out


def test_o48_couchbase_stopping_before_it_has_an_administrator_fails_the_start(launch, processes):
    """O48: our listener goes away while Couchbase still has no administrator: no verdict of 'passed' is possible."""
    secrets_dir, _, _ = launch
    port = free_port()
    guard = start_guard(processes, secrets_dir, port)
    start_fake(processes, port, lifetime=1.0)
    status, out = verdict(guard)
    assert status == 2, out
    assert "stopped listening" in out
    assert "before it had an administrator" in out


def test_o48_no_administrator_within_the_guards_time_fails_the_start(launch, processes):
    """O48: Couchbase listens but onepanel never runs its setup: the guard fails at its deadline, not later."""
    secrets_dir, _, _ = launch
    port = free_port()
    guard = start_guard(processes, secrets_dir, port, timeout=2)
    start_fake(processes, port)
    started = time.monotonic()
    status, out = verdict(guard)
    assert status == 2, out
    assert "still had no administrator" in out
    assert time.monotonic() - started < 10


def test_o48_an_error_of_the_guard_itself_is_a_failed_verdict(launch, processes):
    """O48: fail closed. An unreadable launch secret is an error of the guard, which is a FAILED verdict, never a
    silent pass."""
    secrets_dir, _, _ = launch
    write_secret(secrets_dir / "couchbase-password", "")
    guard = start_guard(processes, secrets_dir, free_port())
    status, out = verdict(guard)
    assert status == 2, out
    assert "the guard itself failed: ValueError" in out


def test_o48_a_failed_verdict_stops_the_start_script_at_once(launch, tmp_path):
    """O48: the guard sends SIGTERM to the start script that started it (its parent, START_PID), whose trap stops
    every component: a Couchbase with no administrator does not run on. Here the start script is a shell whose trap
    records the signal; the guard fails on its first look, as it does when its own secrets are unreadable."""
    secrets_dir, _, _ = launch
    write_secret(secrets_dir / "couchbase-password", "")
    marker = tmp_path / "stopped"
    script = (
        "trap 'echo TERM > \"$1\"; exit 143' TERM\n"
        '"$2" -I "$3" --watch "$4" 20 "$5" $$ &\n'
        "wait $!\n"
        "sleep 20\n"
        'echo "not stopped" > "$1"\n'
    )
    argv = ["bash", "-c", script, "start", str(marker), sys.executable, str(GUARD), str(secrets_dir), str(free_port())]
    shell = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    assert shell.returncode == 143, shell.stdout + shell.stderr
    assert marker.read_text(encoding="ascii").strip() == "TERM"
    assert "SIGTERM sent to the start script" in shell.stdout
