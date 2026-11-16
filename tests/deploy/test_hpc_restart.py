"""The HPC bundle's start-again path (deploy/hpc-federation/bundle): status.sh, exec.sh, restart.sh and the identity
record a first start leaves (O58).

Run in plain bash on this host: fake processes stand for the service, and stand-ins for apptainer, curl and hostname
(tests/deploy/hpcnode.py) for what a node has. Nothing here starts a container, reaches Slurm or opens a connection.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hpcop_node_tests", HERE / "hpcnode.py")
node = sys.modules.get("hpcop_node_tests") or importlib.util.module_from_spec(spec)
sys.modules["hpcop_node_tests"] = node
spec.loader.exec_module(node)

DOMAIN = "provider-a.example.invalid"
CB_USER = "cb0123456789ab"
CB_PASSWORD = "Aa0" * 8


@pytest.fixture
def processes():
    started = []
    yield started
    node.kill_all(started)


@pytest.fixture
def env(tmp_path):
    holder = tmp_path / "tmpfs"
    holder.mkdir(mode=0o700)
    return node.node_env(holder)


@pytest.fixture
def holder(env):
    return Path(env["TMPDIR"])


def status(work, env):
    result = node.script("status.sh", "op", "--workdir", str(work), env=env)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 1, result.stdout
    return json.loads(result.stdout)


# --- status.sh ---


def test_o58_status_of_a_running_service_is_alive_with_its_record(holder, env, processes):
    """O58: alive, and the PID and start tick service.pid records; not ready until the marker is written."""
    work = node.make_work(holder)
    service = node.spawn(processes)
    node.record(work, service)
    assert status(work, env) == {
        "alive": True,
        "pid": service.pid,
        "start": node.start_tick(service.pid),
        "ready": False,
    }


def test_o58_status_is_ready_when_the_marker_holds_service_pid(holder, env, processes):
    """O58: ready is alive plus a service-verified that holds exactly the bytes of service.pid."""
    work = node.make_work(holder)
    service = node.spawn(processes)
    node.record(work, service)
    shutil.copyfile(work / "service.pid", work / "service-verified")
    assert status(work, env)["ready"] is True


def test_o58_a_marker_from_an_earlier_launch_is_not_ready(holder, env, processes):
    """O58: a stale service-verified (another PID or start tick) never reads as this launch's."""
    work = node.make_work(holder)
    service = node.spawn(processes)
    node.record(work, service)
    node.put(work / "service-verified", "12345 99\n")
    reading = status(work, env)
    assert reading["alive"] is True
    assert reading["ready"] is False


def test_o58_a_stopped_service_is_not_alive_and_not_ready_even_with_its_marker(holder, env, processes):
    """O58: a stopped service is alive false, ready false, exit 0, whatever the marker says."""
    work = node.make_work(holder)
    pid, tick = node.dead_record(processes)
    node.put(work / "service.pid", f"{pid} {tick}\n")
    shutil.copyfile(work / "service.pid", work / "service-verified")
    reading = status(work, env)
    assert reading["alive"] is False
    assert reading["ready"] is False


def test_o58_a_recycled_pid_is_not_the_service(holder, env, processes):
    """O58: the recorded PID under another start tick is another process: not alive."""
    work = node.make_work(holder)
    bystander = node.spawn(processes)
    node.put(work / "service.pid", f"{bystander.pid} 1\n")
    assert status(work, env)["alive"] is False


def test_o58_a_service_that_never_started_has_no_record(holder, env):
    """O58: no service.pid: alive false, pid and start null."""
    work = node.make_work(holder)
    assert status(work, env) == {"alive": False, "pid": None, "start": None, "ready": False}


@pytest.mark.parametrize("bad", ["not a pid\n", "12 x\n", "0 5\n", ""])
def test_o58_a_malformed_service_record_reads_as_no_record(bad, holder, env):
    """O58: anything but a PID and a start tick is no record."""
    work = node.make_work(holder)
    node.put(work / "service.pid", bad)
    assert status(work, env) == {"alive": False, "pid": None, "start": None, "ready": False}


def test_o58_status_fails_only_for_a_directory_that_is_not_a_work_directory(holder, env, tmp_path):
    """O58: non-zero, and nothing on stdout, for a path that is no private hpcop work directory of the kind."""
    other = tmp_path / "plain"
    other.mkdir(mode=0o700)
    assert node.script("status.sh", "op", "--workdir", str(other), env=env).stdout == ""
    assert node.script("status.sh", "op", "--workdir", str(other), env=env).returncode != 0
    open_dir = node.make_work(holder, suffix="Open12")
    open_dir.chmod(0o755)
    assert node.script("status.sh", "op", "--workdir", str(open_dir), env=env).returncode != 0
    zone = node.make_work(holder, kind="oz", suffix="Zone12")
    assert node.script("status.sh", "op", "--workdir", str(zone), env=env).returncode != 0


def test_o58_status_changes_nothing(holder, env, processes):
    """O58: it is read-only."""
    work = node.make_work(holder)
    service = node.spawn(processes)
    node.record(work, service)
    before = node.snapshot(work)
    status(work, env)
    assert node.snapshot(work) == before


# --- exec.sh ---


@pytest.fixture
def fakes(tmp_path, env):
    bin_dir = node.install_fakes(tmp_path / "bin")
    log = tmp_path / "exec-args"
    return {
        **env,
        "PATH": f"{bin_dir}:{env['PATH']}",
        "FAKE_EXEC_ARGS": str(log),
        "FAKE_LOG": str(tmp_path / "fake-log"),
    }


def test_o58_exec_refuses_when_no_service_runs(holder, fakes, processes):
    """O58: no recorded process alive: refused, and apptainer is never started."""
    work = node.make_work(holder)
    pid, tick = node.dead_record(processes)
    node.put(work / "service.pid", f"{pid} {tick}\n")
    result = node.script("exec.sh", "op", "--workdir", str(work), "--", "true", env=fakes)
    assert result.returncode != 0
    assert "no running service" in result.stderr
    assert not Path(fakes["FAKE_EXEC_ARGS"]).exists()
    assert not list(work.glob("exec.*"))


def test_o58_exec_refuses_without_a_command(holder, fakes, processes):
    """O58: nothing after `--` is a usage error, not an empty exec."""
    work = node.make_work(holder)
    service = node.spawn(processes)
    node.record(work, service)
    result = node.script("exec.sh", "op", "--workdir", str(work), "--", env=fakes)
    assert result.returncode != 0
    assert not Path(fakes["FAKE_EXEC_ARGS"]).exists()


def test_o58_exec_runs_the_command_in_a_new_container_on_the_same_rootfs(holder, fakes, processes):
    """O58: apptainer exec with run_service's flags but not --writable, a per-call --workdir under the work directory,
    env HOME=/root and the argv, stdin /dev/null; the per-call directory is removed afterwards."""
    work = node.make_work(holder)
    service = node.spawn(processes)
    node.record(work, service)
    argv = ["/usr/sbin/op_worker", "ping", "two words"]
    result = node.script("exec.sh", "op", "--workdir", str(work), "--", *argv, env=fakes, stdin="payload")
    assert result.returncode == 0, result.stderr
    lines = Path(fakes["FAKE_EXEC_ARGS"]).read_text(encoding="utf-8").splitlines()
    call_dir = lines[lines.index("--workdir") + 1]
    assert Path(call_dir).parent == work
    assert Path(call_dir).name.startswith("exec.")
    assert lines[: lines.index("--workdir")] == ["exec", "--containall", "--no-home", "--pwd", "/root"]
    assert lines[lines.index("--workdir") + 2 :] == [
        f"{work}/rootfs",
        "/usr/bin/env",
        "HOME=/root",
        *argv,
        "stdin: 0 bytes",
    ]
    assert "--writable" not in lines
    assert not Path(call_dir).exists()


@pytest.mark.parametrize("rc", [0, 3, 41])
def test_o58_exec_passes_output_through_and_exits_with_the_commands_status(rc, holder, fakes, processes):
    """O58: stdout and stderr unchanged, the exit status the command's."""
    work = node.make_work(holder)
    service = node.spawn(processes)
    node.record(work, service)
    fakes = {**fakes, "FAKE_EXEC_RC": str(rc)}
    result = node.script("exec.sh", "op", "--workdir", str(work), "--", "cmd", env=fakes)
    assert result.returncode == rc
    assert result.stdout == "output of the command\n"
    assert result.stderr == "error of the command\n"


# --- the identity record and the ready marker ---


def test_o58_the_ready_marker_is_a_copy_of_service_pid_0600(holder, env):
    """O58: mark_verified writes exactly the bytes of service.pid, mode 0600, and replaces an older marker."""
    work = node.make_work(holder)
    node.put(work / "service.pid", "4242 777\n")
    node.put(work / "service-verified", "old\n")
    assert node.lib('mark_verified "$1"', str(work), env=env).returncode == 0
    assert (work / "service-verified").read_bytes() == b"4242 777\n"
    assert oct((work / "service-verified").stat().st_mode & 0o777) == "0o600"


def test_o58_the_ready_marker_needs_a_service_record(holder, env):
    """O58: no service.pid, no marker."""
    work = node.make_work(holder)
    assert node.lib('mark_verified "$1"', str(work), env=env).returncode != 0
    assert not (work / "service-verified").exists()


def test_o58_the_identity_is_recorded_as_json_0600(holder, env):
    """O58: write_identity records the kind and the named values, valid JSON, 0600, and check_identity reads it."""
    work = node.make_work(holder)
    done = node.lib('write_identity "$1" op providerId abc123 domain "$2"', str(work), DOMAIN, env=env)
    assert done.returncode == 0, done.stderr
    assert json.loads((work / "identity.json").read_text(encoding="utf-8")) == {
        "kind": "op",
        "providerId": "abc123",
        "domain": DOMAIN,
    }
    assert oct((work / "identity.json").stat().st_mode & 0o777) == "0o600"
    same = node.lib('check_identity "$1" kind op providerId abc123 domain "$2"', str(work), DOMAIN, env=env)
    assert same.returncode == 0, same.stderr
    other = node.lib('check_identity "$1" kind op providerId other domain "$2"', str(work), DOMAIN, env=env)
    assert other.returncode != 0
    assert "identity changed" in other.stderr


@pytest.mark.parametrize("value", ['a"b', "a b", "", "x\ny", "a\\b", "caf\u00e9", "tab\there", "x" * 254])
def test_o58_an_identity_value_that_could_break_the_json_is_refused(value, holder, env):
    """O58: the file is always valid JSON: a quote, a backslash, a space or control character, anything but printable
    ASCII, or a value too long, is refused, and nothing is written."""
    work = node.make_work(holder)
    done = node.lib('write_identity "$1" op providerId "$2" domain d', str(work), value, env=env)
    assert done.returncode != 0
    assert not (work / "identity.json").exists()


@pytest.mark.parametrize("value", ["0a1b2c", "a/b+c=", "user@host:8443", "Zone-1_x.y", "%7B~!#$&*()[]{};,<>?|^`'"])
def test_o58_an_identity_value_of_any_printable_ascii_is_recorded_as_valid_json(value, holder, env):
    """O58: no first start fails on an id format nobody has seen: whatever is printable ASCII without a space, a quote
    or a backslash is written, and the file reads back as the same value."""
    work = node.make_work(holder)
    done = node.lib('write_identity "$1" op providerId "$2" domain d', str(work), value, env=env)
    assert done.returncode == 0, done.stderr
    assert json.loads((work / "identity.json").read_text(encoding="utf-8"))["providerId"] == value


# --- restart.sh: what it refuses ---


def kept_work(holder, kind="op", **kwargs):
    """A work directory a kept stop left: the keep marks, the identity, no running service."""
    work = node.make_work(holder, kind=kind, **kwargs)
    node.put(work / "stop-requested", "keep\n")
    node.put(work / "keep-until", f"{int(time.time()) + 3600}\n")
    if kind == "op":
        node.put(work / "identity.json", json.dumps({"kind": "op", "providerId": "pid-1", "domain": DOMAIN}) + "\n")
    else:
        node.put(work / "identity.json", json.dumps({"kind": "oz", "adminUserId": "admin-1", "domain": DOMAIN}) + "\n")
    return work


def break_not_kept(work, _processes):
    (work / "stop-requested").unlink()


def break_empty_stop(work, _processes):
    node.put(work / "stop-requested", "")


def break_no_identity(work, _processes):
    (work / "identity.json").unlink()


def break_no_secret(work, _processes):
    (node.secrets_of(work) / "passphrase").unlink()


def break_loose_secret(work, _processes):
    (node.secrets_of(work) / "erlang-cookie").chmod(0o644)


def break_no_token(work, _processes):
    (node.secrets_of(work) / "registration-token").unlink()


def break_no_rootfs(work, _processes):
    (work / "rootfs.complete").unlink()


def break_service_runs(work, processes):
    node.record(work, node.spawn(processes))


def break_owner_runs(work, processes):
    owner = node.spawn(processes)
    node.put(work / "owner.pid", f"{owner.pid} {node.start_tick(owner.pid)}\n")


@pytest.mark.parametrize(
    "broken",
    [
        break_not_kept,
        break_empty_stop,
        break_no_identity,
        break_no_secret,
        break_loose_secret,
        break_no_token,
        break_no_rootfs,
        break_service_runs,
        break_owner_runs,
    ],
    ids=lambda f: f.__name__.removeprefix("break_"),
)
def test_o58_restart_refuses_and_changes_nothing(broken, holder, fakes, processes):
    """O58: a directory that is not a kept stop with its identity and secrets, or whose service or owner still runs, is
    refused before anything is touched: no marker removed, no owner record rewritten, nothing started."""
    work = kept_work(holder)
    node.put(work / "service-verified", "stale\n")
    (work / "rootfs" / "root" / "service-ready.lock").touch()
    broken(work, processes)
    before = node.snapshot(work)
    result = node.script("restart.sh", "op", "--workdir", str(work), env=fakes)
    assert result.returncode != 0
    assert node.snapshot(work) == before
    assert not Path(fakes["FAKE_LOG"]).exists()


def test_o58_restart_refuses_a_path_that_is_not_a_work_directory(tmp_path, fakes):
    """O58: only an hpcop work directory of the kind, 0700 and ours."""
    other = tmp_path / "plain"
    other.mkdir(mode=0o700)
    assert node.script("restart.sh", "op", "--workdir", str(other), env=fakes).returncode != 0


# --- restart.sh: the whole restart, on stand-ins ---


class Restart:
    """A kept work directory of one kind, a copy of the bundle whose live default check is a stand-in that logs its
    arguments, the stand-in tools, and the state of the Couchbase the stand-in curl answers for."""

    def __init__(self, tmp_path, holder, env, processes, kind):
        self.kind, self.processes, self.holder = kind, processes, holder
        self.work = kept_work(holder, kind=kind)
        self.bundle = tmp_path / "bundle"
        shutil.copytree(node.BUNDLE, self.bundle)
        log = tmp_path / "fake-log"
        (self.bundle / "check-defaults.sh").write_text(
            '#!/bin/bash\necho "check-defaults $*" >> "$FAKE_LOG"\n', encoding="utf-8"
        )
        bin_dir = node.install_fakes(tmp_path / "bin")
        self.state = tmp_path / "curl-state"
        self.set_state(providerId="pid-1", domain=DOMAIN, adminUserId="admin-1")
        self.env = {
            **env,
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_LOG": str(log),
            "FAKE_CURL_STATE": str(self.state),
            "FAKE_EXEC_ARGS": str(tmp_path / "exec-args"),
            "HPCOP_STOP_SECONDS": "30",
        }
        self.log = log
        secrets = node.secrets_of(self.work)
        node.put(secrets / "couchbase-user", CB_USER + "\n")
        node.put(secrets / "couchbase-password", CB_PASSWORD + "\n")
        node.put(secrets / "passphrase", "Pass1234" * 6 + "\n")
        panel = f"{kind}_panel"
        rootfs = self.work / "rootfs"
        (rootfs / "etc").mkdir()
        (rootfs / "etc" / panel).symlink_to(f"/volumes/persistence/etc/{panel}")
        (rootfs / "volumes" / "persistence_backup" / "etc" / panel).mkdir(parents=True)
        (rootfs / "volumes" / "persistence_backup" / "etc" / panel / "OnedataTestWebServerCa.pem").write_text("ca\n")
        (rootfs / "etc" / "hpcop-erlang-env").write_text("ERL_EPMD_PORT=21500\n", encoding="utf-8")
        node.put(self.work / "service-verified", "stale\n")
        (rootfs / "root" / "service-ready.lock").touch()

    def set_state(self, **fields):
        current = (
            json.loads(self.state.read_text(encoding="utf-8"))
            if self.state.exists()
            else {"creds": [CB_USER, CB_PASSWORD]}
        )
        self.state.write_text(json.dumps({**current, **fields}), encoding="utf-8")

    def couchbase_password(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["creds"][1]

    def start(self):
        proc = subprocess.Popen(
            ["bash", str(self.bundle / "restart.sh"), self.kind, "--workdir", str(self.work)],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.processes.append(proc)
        return proc

    def wait_ready(self, proc, seconds=60):
        """Until service-verified is the marker of this launch: it holds the bytes of service.pid (the stale one the
        fixture left holds other bytes)."""
        deadline = time.monotonic() + seconds
        marker, record = self.work / "service-verified", self.work / "service.pid"
        while not (record.exists() and marker.exists() and marker.read_bytes() == record.read_bytes()):
            assert proc.poll() is None, proc.communicate()[0]
            assert time.monotonic() < deadline, "timed out waiting for the ready marker"
            time.sleep(0.1)

    def stop(self):
        argv = [str(self.bundle / "teardown.sh"), self.kind, "--workdir", str(self.work), "--keep", "--timeout", "30"]
        return subprocess.run(["bash", *argv], capture_output=True, text=True, env=self.env, timeout=90, check=False)

    def lines(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@pytest.fixture(params=["op", "oz"])
def restart(request, tmp_path, holder, env, processes):
    return Restart(tmp_path, holder, env, processes, request.param)


@node.needs_pidfd
def test_o58_a_restart_clears_the_old_markers_before_starting_and_checks_before_it_is_ready(restart):
    """O58: stop-requested, service-verified and the stale ready lock are gone before the service starts; the guard,
    the identity, the rotation and the live check pass; only then is service-verified written, as service.pid."""
    proc = restart.start()
    restart.wait_ready(proc)
    assert (restart.work / "service-verified").read_bytes() == (restart.work / "service.pid").read_bytes()
    assert restart.lines()[:3] == [
        "absent stop-requested",
        "absent service-verified",
        "absent rootfs/root/service-ready.lock",
    ]
    assert not (restart.work / "stop-requested").exists()
    assert f"check-defaults {restart.kind} --workdir {restart.work} --live" in restart.lines()
    owner = (restart.work / "owner.pid").read_text(encoding="utf-8").split()
    assert owner == [str(proc.pid), node.start_tick(proc.pid)]
    assert restart.stop().returncode == 0
    assert proc.wait(timeout=60) == 0, proc.stdout


@node.needs_pidfd
def test_o58_a_restart_rotates_the_couchbase_password_and_retires_the_old_one(restart):
    """O58: the restart rotates (Couchbase holds a new password, and the files agree), and the old one joins the
    retired list, which grows by one on each restart."""
    secrets = node.secrets_of(restart.work)
    retired = []
    for _ in range(2):
        before = restart.couchbase_password()
        retired.append(before)
        (restart.work / "service-verified").unlink(missing_ok=True)
        proc = restart.start()
        restart.wait_ready(proc)
        assert restart.couchbase_password() != before
        assert (secrets / "couchbase-password").read_text(encoding="utf-8").strip() == restart.couchbase_password()
        assert restart.stop().returncode == 0
        assert proc.wait(timeout=60) == 0, proc.stdout
    assert (secrets / "couchbase-password.retired").read_text(encoding="utf-8").splitlines() == retired


@node.needs_pidfd
def test_o58_after_a_restart_the_service_is_alive_and_ready_and_after_a_stop_neither(restart):
    """O58: status.sh reads the restarted service as alive and ready, and the stopped one as neither; the work
    directory and its secrets are kept by the stop."""
    proc = restart.start()
    restart.wait_ready(proc)
    up = node.script("status.sh", restart.kind, "--workdir", str(restart.work), env=restart.env)
    assert json.loads(up.stdout)["alive"] is True
    assert json.loads(up.stdout)["ready"] is True
    done = restart.stop()
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.split(" ", 1)[1])["ended"] == "exit"
    assert proc.wait(timeout=60) == 0, proc.stdout
    down = json.loads(node.script("status.sh", restart.kind, "--workdir", str(restart.work), env=restart.env).stdout)
    assert down["alive"] is False
    assert down["ready"] is False
    assert (node.secrets_of(restart.work) / "passphrase").exists()
    assert (restart.work / "stop-requested").read_text(encoding="utf-8").splitlines()[0] == "keep"


@node.needs_pidfd
def test_o58_a_changed_identity_fails_the_restart_before_the_rotation_and_keeps_the_directory(restart):
    """O58: the service reports another provider id or user id: the restart fails, nothing was rotated, the service is
    stopped, no ready marker is written, and `keep` is back in stop-requested so the directory stays."""
    restart.set_state(providerId="pid-2", adminUserId="admin-2")
    before = restart.couchbase_password()
    proc = restart.start()
    assert proc.wait(timeout=90) != 0
    out = proc.stdout.read() if proc.stdout else ""
    assert "identity changed" in out
    assert restart.couchbase_password() == before
    assert not (node.secrets_of(restart.work) / "couchbase-password.retired").exists()
    assert not (restart.work / "service-verified").exists()
    assert (restart.work / "stop-requested").read_text(encoding="utf-8").splitlines()[0] == "keep"
    assert (node.secrets_of(restart.work) / "passphrase").exists()
    assert (
        json.loads(node.script("status.sh", restart.kind, "--workdir", str(restart.work), env=restart.env).stdout)[
            "alive"
        ]
        is False
    )


@node.needs_pidfd
def test_o58_a_failed_guard_fails_the_restart_stops_the_service_and_keeps_the_directory(restart):
    """O58: the guard's FAILED line fails the restart: the service is stopped, no marker, the directory is kept."""
    restart.env["FAKE_GUARD"] = "fail"
    proc = restart.start()
    assert proc.wait(timeout=90) != 0
    out = proc.stdout.read() if proc.stdout else ""
    assert "guard FAILED" in out
    assert not (restart.work / "service-verified").exists()
    assert (restart.work / "stop-requested").read_text(encoding="utf-8").splitlines()[0] == "keep"
    assert (
        json.loads(node.script("status.sh", restart.kind, "--workdir", str(restart.work), env=restart.env).stdout)[
            "alive"
        ]
        is False
    )
    assert restart.work.is_dir()


@node.needs_pidfd
def test_o58_a_stop_without_keep_during_a_restarted_service_removes_the_work_directory(restart):
    """O58: down's teardown.sh (no --keep) on a restarted service still removes everything, as for a first start."""
    proc = restart.start()
    restart.wait_ready(proc)
    done = subprocess.run(
        ["bash", str(restart.bundle / "teardown.sh"), restart.kind, "--workdir", str(restart.work), "--timeout", "10"],
        capture_output=True,
        text=True,
        env=restart.env,
        timeout=90,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert proc.wait(timeout=60) == 0, proc.stdout
    assert not restart.work.exists()


# --- a first start records the identity ---


@pytest.mark.parametrize("kind", ["op", "oz"])
def test_o58_a_first_start_records_the_identity_it_verified(kind, tmp_path, holder, env, processes):
    """O58: finish.sh (provider: id and domain) and finish-zone.sh (zone: admin user id and domain) write
    identity.json once every check has passed, and not before: the live check stand-in sees none yet."""
    session = Restart(tmp_path, holder, env, processes, kind)
    work = session.work
    for name in ("stop-requested", "keep-until", "identity.json", "service-verified"):
        (work / name).unlink(missing_ok=True)
    (work / "rootfs" / "root" / "service-ready.lock").touch()
    (work / "run.log").write_text("Congratulations! New deployment\nhpcop: Couchbase guard passed\n", encoding="utf-8")
    (session.bundle / "check-defaults.sh").write_text(
        f'#!/bin/bash\n[ -e "{work}/identity.json" ] && echo "identity early" >> "$FAKE_LOG"\nexit 0\n',
        encoding="utf-8",
    )
    service = node.spawn(processes)
    script = "finish.sh" if kind == "op" else "finish-zone.sh"
    done = subprocess.run(
        ["bash", str(session.bundle / script), "--workdir", str(work), str(service.pid)],
        capture_output=True,
        text=True,
        env=session.env,
        timeout=90,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    expected = (
        {"kind": "op", "providerId": "pid-1", "domain": DOMAIN}
        if kind == "op"
        else {"kind": "oz", "adminUserId": "admin-1", "domain": DOMAIN}
    )
    assert json.loads((work / "identity.json").read_text(encoding="utf-8")) == expected
    assert oct((work / "identity.json").stat().st_mode & 0o777) == "0o600"
    assert "identity early" not in session.lines()
