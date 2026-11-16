"""The HPC bundle's kept stop (deploy/hpc-federation/bundle): teardown.sh --keep, the batch scripts' cleanup, the
sweep's keep-until mark, the retired Couchbase passwords, and the restart mode of wait_ready (O56).

Run in plain bash on this host with fake processes (tests/deploy/hpcnode.py): nothing here starts a container,
reaches Slurm or opens a connection.
"""

import importlib.util
import json
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hpcop_node_tests", HERE / "hpcnode.py")
node = sys.modules.get("hpcop_node_tests") or importlib.util.module_from_spec(spec)
sys.modules["hpcop_node_tests"] = node
spec.loader.exec_module(node)

STOP_LINE = re.compile(r'hpcop-stop \{"ended": "(exit|kill|none)", "seconds": (\d+\.\d{3})\}')


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


def stop_line(result):
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    match = STOP_LINE.fullmatch(lines[0])
    assert match, lines[0]
    return json.loads(lines[0].split(" ", 1)[1])


@node.needs_pidfd
def test_o56_a_kept_stop_ends_the_service_and_removes_nothing(holder, env, processes):
    """O56: --keep stops the recorded service gracefully and leaves the work directory, its secrets and its data."""
    work = node.make_work(holder)
    (work / "rootfs" / "data").write_text("bucket", encoding="utf-8")
    service = node.spawn(processes, node.GRACEFUL, str(work))
    node.record(work, service)
    result = node.script("teardown.sh", "op", "--workdir", str(work), "--keep", "--timeout", "30", env=env)
    assert result.returncode == 0, result.stderr
    assert stop_line(result)["ended"] == "exit"
    assert node.gone(service)
    assert (work / "rootfs" / "data").read_text(encoding="utf-8") == "bucket"
    assert (node.secrets_of(work) / "passphrase").exists()
    assert (node.secrets_of(work) / "registration-token").exists()


@node.needs_pidfd
def test_o56_the_keep_marks_are_written_before_the_sigterm(holder, env, processes):
    """O56: stop-requested's first line is `keep` and keep-until holds the epoch, 0600, when the signal arrives: the
    batch script's cleanup, which runs as the service ends, must find them."""
    work = node.make_work(holder)
    service = node.spawn(processes, node.GRACEFUL, str(work))
    node.record(work, service)
    until = int(time.time()) + 3600
    args = ("op", "--workdir", str(work), "--keep", "--keep-until", str(until), "--timeout", "30")
    assert node.script("teardown.sh", *args, env=env).returncode == 0
    assert (work / "witness").read_text(encoding="utf-8") == f"stop-requested=keep keep-until={until}"
    assert (work / "stop-requested").read_text(encoding="utf-8").splitlines()[0] == "keep"
    assert oct((work / "keep-until").stat().st_mode & 0o777) == "0o600"


@node.needs_pidfd
def test_o56_a_kept_stop_reports_a_graceful_end_with_the_seconds_it_took(holder, env, processes):
    """O56: stdout is exactly one `hpcop-stop` line: ended `exit`, and the seconds from the TERM to the process gone."""
    work = node.make_work(holder)
    service = node.spawn(processes, node.GRACEFUL, str(work))
    node.record(work, service)
    result = node.script("teardown.sh", "op", "--workdir", str(work), "--keep", "--timeout", "30", env=env)
    evidence = stop_line(result)
    assert evidence["ended"] == "exit"
    assert 0 <= evidence["seconds"] < 30


@node.needs_pidfd
def test_o56_a_service_that_ignores_the_term_is_killed_and_reported_as_kill(holder, env, processes):
    """O56: past --timeout the stop escalates as stop_service does (SIGUSR1, which supervise.py turns into SIGKILL),
    and the evidence says `kill`; the work directory is still kept."""
    work = node.make_work(holder)
    service = node.spawn(processes, node.STUBBORN)
    node.record(work, service)
    result = node.script("teardown.sh", "op", "--workdir", str(work), "--keep", "--timeout", "1", env=env)
    assert result.returncode == 0, result.stderr
    assert stop_line(result)["ended"] == "kill"
    assert node.gone(service)
    assert work.is_dir()
    assert (node.secrets_of(work) / "passphrase").exists()


@node.needs_pidfd
def test_o56_a_kept_stop_of_a_stopped_service_signals_nothing_and_succeeds_again(holder, env, processes):
    """O56: `none` when no service of this launch runs (its process ended, its PID may be another process now): exit
    0, no signal, and a repeat gives the same answer; the keep mark is still there for restart.sh."""
    work = node.make_work(holder)
    pid, tick = node.dead_record(processes)
    node.put(work / "service.pid", f"{pid} {tick}\n")
    bystander = node.spawn(processes, node.GRACEFUL, str(work))
    for _ in range(2):
        result = node.script("teardown.sh", "op", "--workdir", str(work), "--keep", "--timeout", "5", env=env)
        assert result.returncode == 0, result.stderr
        assert stop_line(result) == {"ended": "none", "seconds": 0.0}
    assert not node.gone(bystander)
    assert (work / "stop-requested").read_text(encoding="utf-8").splitlines()[0] == "keep"


@node.needs_pidfd
def test_o56_a_pid_that_is_another_process_now_is_not_signalled_by_a_kept_stop(holder, env, processes):
    """O56: the recorded PID with another start tick (a reused PID) is not our service: not signalled, `none`."""
    work = node.make_work(holder)
    bystander = node.spawn(processes, node.GRACEFUL, str(work))
    node.put(work / "service.pid", f"{bystander.pid} 1\n")
    result = node.script("teardown.sh", "op", "--workdir", str(work), "--keep", "--timeout", "5", env=env)
    assert result.returncode == 0, result.stderr
    assert stop_line(result)["ended"] == "none"
    assert not node.gone(bystander)


@node.needs_pidfd
def test_o56_a_kept_stop_fails_when_the_process_survives_the_kill(holder, env, processes, tmp_path):
    """O56: non-zero, and no evidence line, when the process is still there after the KILL (here supervise.py is a
    stand-in whose signals reach nothing); the directory stays kept."""
    bundle = tmp_path / "bundle"
    shutil.copytree(node.BUNDLE, bundle)
    (bundle / "supervise.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    work = node.make_work(holder)
    service = node.spawn(processes, node.GRACEFUL, str(work))
    node.record(work, service)
    env = {**env, "HPCOP_KILL_WAIT_S": "1"}
    result = node.script(
        "teardown.sh", "op", "--workdir", str(work), "--keep", "--timeout", "1", env=env, bundle=bundle
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "still running" in result.stderr
    assert not node.gone(service)
    assert (work / "stop-requested").read_text(encoding="utf-8").splitlines()[0] == "keep"


@pytest.mark.parametrize(
    "extra",
    [["--keep-until", "1999999999"], ["--keep", "--keep-until", "soon"], ["--keep", "--keep-until", "1 2"]],
)
def test_o56_keep_until_takes_one_epoch_and_only_with_keep(holder, env, extra):
    """O56: a keep-until without --keep, or that is not one integer, is refused before anything is written."""
    work = node.make_work(holder)
    result = node.script("teardown.sh", "op", "--workdir", str(work), *extra, env=env)
    assert result.returncode != 0
    assert not (work / "keep-until").exists()
    assert not (work / "stop-requested").exists()


@node.needs_pidfd
def test_o56_a_stop_without_keep_still_removes_the_work_directory(holder, env, processes):
    """O56: the path without --keep is as before: an empty stop-requested, the SIGTERM, the work directory removed."""
    work = node.make_work(holder)
    service = node.spawn(processes, node.GRACEFUL, str(work))
    node.record(work, service)
    result = node.script("teardown.sh", "op", "--workdir", str(work), "--timeout", "3", env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert node.gone(service)
    assert not work.exists()


# --- the batch scripts' cleanup ---


def cleanup_text(job):
    text = (node.BUNDLE / job).read_text(encoding="utf-8")
    start = text.index("cleanup() {")
    return text[start : text.index("\n}\n", start) + 3]


def run_cleanup(job, work, env, *, svc="", svc_start=""):
    body = cleanup_text(job)
    return node.lib(f'W="{work}"; svc="{svc}"; svc_start="{svc_start}"\n{body}\ncleanup', env=env)


@pytest.mark.parametrize("job", ["provider.sbatch", "zone.sbatch"])
def test_o56_the_batch_cleanup_keeps_a_work_directory_whose_stop_kept_it(job, holder, env):
    """O56: stop-requested beginning with `keep`: the cleanup removes nothing, neither the secrets nor the directory."""
    work = node.make_work(holder, kind="op" if job.startswith("provider") else "oz")
    node.put(work / "stop-requested", "keep\n")
    result = run_cleanup(job, work, env)
    assert result.returncode == 0, result.stderr
    assert (work / "rootfs" / "root" / ".hpcop" / "passphrase").exists()
    assert (work / "stop-requested").exists()


@pytest.mark.parametrize("job", ["provider.sbatch", "zone.sbatch"])
@node.needs_pidfd
def test_o56_the_batch_cleanup_stops_a_service_that_still_runs_before_keeping(job, holder, env, processes):
    """O56: a kept stop whose service is somehow still running: it is stopped, and still nothing is removed."""
    work = node.make_work(holder)
    node.put(work / "stop-requested", "keep\n")
    service = node.spawn(processes, node.GRACEFUL, str(work))
    env = {**env, "HPCOP_STOP_SECONDS": "30"}
    result = run_cleanup(job, work, env, svc=str(service.pid), svc_start=node.start_tick(service.pid))
    assert result.returncode == 0, result.stderr
    assert node.gone(service)
    assert (node.secrets_of(work) / "passphrase").exists()


@pytest.mark.parametrize("marker", [None, ""])
@pytest.mark.parametrize("job", ["provider.sbatch", "zone.sbatch"])
def test_o56_the_batch_cleanup_still_removes_everything_when_no_stop_kept_it(job, marker, holder, env):
    """O56: no stop-requested, or an empty one (teardown.sh without --keep): the secrets and the directory go."""
    work = node.make_work(holder, kind="op" if job.startswith("provider") else "oz")
    if marker is not None:
        node.put(work / "stop-requested", marker)
    result = run_cleanup(job, work, env)
    assert result.returncode == 0, result.stderr
    assert not work.exists()


@pytest.mark.parametrize("job", ["provider.sbatch", "zone.sbatch"])
def test_o56_a_service_that_ran_to_the_end_leaves_the_ready_marker(job):
    """O56: the batch script writes the ready marker right after finish*.sh succeeded, before it holds the service."""
    text = (node.BUNDLE / job).read_text(encoding="utf-8")
    assert text.index('mark_verified "$W"') < text.index('status=0; wait "$svc"')
    finish = "finish.sh" if job.startswith("provider") else "finish-zone.sh"
    assert text.index(f'bash "$B/{finish}"') < text.index('mark_verified "$W"')


# --- the sweep ---


def sweep(env, holder):
    """sweep_leftovers over `holder`, with the ancestry check (every parent ours or root's) left out: that policy
    depends on where the test's directory lives and has nothing to do with the keep mark."""
    return node.lib("safe_parents() { return 0; }; sweep_leftovers", env=env)


def leftover(holder, processes, until=None, suffix="Zz9Zz9"):
    work = node.make_work(holder, suffix=suffix)
    pid, tick = node.dead_record(processes)
    node.put(work / "owner.pid", f"{pid} {tick}\n")
    if until is not None:
        node.put(work / "keep-until", until)
    return work


def test_o56_the_sweep_removes_a_leftover_whose_owner_has_ended(holder, env, processes):
    """O56: as before the keep mark: a work directory whose owner process is gone is removed."""
    work = leftover(holder, processes)
    assert sweep(env, holder).returncode == 0
    assert not work.exists()


def test_o56_the_sweep_keeps_a_directory_whose_keep_until_is_in_the_future(holder, env, processes):
    """O56: a stopped service's directory has no owner, and must outlive the next job's sweep until the allocation
    ends."""
    work = leftover(holder, processes, until=f"{int(time.time()) + 3600}\n")
    result = sweep(env, holder)
    assert result.returncode == 0, result.stderr
    assert work.is_dir()
    assert (node.secrets_of(work) / "passphrase").exists()


def test_o56_the_sweep_removes_a_kept_directory_once_its_keep_until_has_passed(holder, env, processes):
    """O56: kept until that time, and no longer."""
    work = leftover(holder, processes, until=f"{int(time.time()) - 5}\n")
    assert sweep(env, holder).returncode == 0
    assert not work.exists()


@pytest.mark.parametrize(
    "mark", ["soon\n", "99999999999\n99999999999\n", "-5\n", "", "99999999999 x\n", "9" * 20 + "\n"]
)
def test_o56_a_keep_until_that_is_not_one_integer_keeps_nothing(mark, holder, env, processes):
    """O56: only a file holding one integer protects a directory; anything else is no mark."""
    work = leftover(holder, processes, until=mark)
    assert sweep(env, holder).returncode == 0
    assert not work.exists()


def test_o56_a_keep_until_that_is_a_link_keeps_nothing(holder, env, processes, tmp_path):
    """O56: a link is no mark (it could point anywhere), even to a file holding a future epoch."""
    work = leftover(holder, processes)
    target = tmp_path / "epoch"
    target.write_text(f"{int(time.time()) + 3600}\n", encoding="utf-8")
    (work / "keep-until").symlink_to(target)
    assert sweep(env, holder).returncode == 0
    assert not work.exists()


def test_o56_the_sweep_keeps_a_directory_whose_owner_still_runs(holder, env, processes):
    """O56: the owner check is unchanged: a running owner keeps its directory with or without a mark."""
    work = node.make_work(holder)
    owner = node.spawn(processes, node.GRACEFUL, str(work))
    node.put(work / "owner.pid", f"{owner.pid} {node.start_tick(owner.pid)}\n")
    assert sweep(env, holder).returncode == 0
    assert work.is_dir()


# --- the retired Couchbase passwords ---


def test_o56_secret_values_lists_every_retired_password_and_the_others(holder, env):
    """O56: scrub and redaction read secret_values_file: every line of the retired list, not the first only."""
    work = node.make_work(holder)
    secrets = node.secrets_of(work)
    node.put(secrets / "couchbase-password.retired", "retiredOne\nretiredTwo\nretiredThree\n")
    out = work / "values"
    result = node.lib('secret_values_file "$1" "$2"', str(work), str(out), env=env)
    assert result.returncode == 0, result.stderr
    values = out.read_text(encoding="utf-8").splitlines()
    for expected in ("retiredOne", "retiredTwo", "retiredThree", "value-of-passphrase", "value-of-couchbase-password"):
        assert expected in values
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_o56_a_retired_password_is_appended_never_overwritten(holder, env):
    """O56: each rotation adds a line; the earlier ones stay; the file stays 0600."""
    work = node.make_work(holder)
    secrets = node.secrets_of(work)
    for old in ("first", "second", "third"):
        assert node.lib('append_retired "$1" "$2"', str(secrets), old, env=env).returncode == 0
    path = secrets / "couchbase-password.retired"
    assert path.read_text(encoding="utf-8").splitlines() == ["first", "second", "third"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_o56_a_retired_list_with_the_wrong_mode_is_refused_and_left_alone(holder, env):
    """O56: a list that is not 0600 is not trusted, and the refusal does not rewrite it."""
    work = node.make_work(holder)
    secrets = node.secrets_of(work)
    path = secrets / "couchbase-password.retired"
    node.put(path, "first\n", mode=0o644)
    assert node.lib('append_retired "$1" "$2"', str(secrets), "second", env=env).returncode != 0
    assert path.read_text(encoding="utf-8") == "first\n"


FAKE_COUCHBASE = r"""
CB_STATE="$1"   # the administrator password Couchbase holds now
cb_status() { [[ $3 == $(<"$CB_STATE") ]] && printf 200 || printf 401; }       # <tmpdir> <user> <password>
http() {  # the one POST of the rotation: take the new password from its form
  local form pw
  while (( $# )); do if [[ $1 == --data ]]; then form=$(<"${2#@}"); fi; shift; done
  pw=${form#*password=}; pw=${pw%%&*}
  printf '%s' "$pw" > "$CB_STATE"
  printf 200
}
cb_guard() { :; }
write_cb_overlay() { :; }
"""


def test_o56_every_rotation_retires_one_more_password(holder, env):
    """O56: cb_rotate on a deployment, and again after a restart: Couchbase holds the newest password, and the retired
    list holds the deploy-time one and the first rotation's."""
    work = node.make_work(holder)
    secrets = node.secrets_of(work)
    deploy_time = "D" * 24
    node.put(secrets / "couchbase-user", "cb0123456789ab\n")
    node.put(secrets / "couchbase-password", deploy_time + "\n")
    state = holder / "couchbase-state"
    state.write_text(deploy_time, encoding="utf-8")
    snippet = FAKE_COUCHBASE + 'cb_rotate "$2" "$3"'
    for _ in range(2):
        done = node.lib(snippet, str(state), str(work), str(work / "tmp"), env=env)
        assert done.returncode == 0, done.stderr
    current = (secrets / "couchbase-password").read_text(encoding="utf-8").strip()
    retired = (secrets / "couchbase-password.retired").read_text(encoding="utf-8").splitlines()
    assert state.read_text(encoding="utf-8") == current
    assert len(retired) == 2
    assert retired[0] == deploy_time
    assert current not in retired
    assert len(set(retired)) == 2


def test_o56_the_live_default_check_refuses_every_retired_password():
    """O56: check-defaults.sh --live asks Couchbase about each line of the retired list (the live part runs only on a
    node: this reads the script)."""
    text = (node.BUNDLE / "check-defaults.sh").read_text(encoding="utf-8")
    assert 'retired_passwords "$S"' in text
    assert 'for OLD in "${RETIRED[@]}"' in text
    assert 'cb_is 401 "$CB_USER" "$OLD"' in text
    assert "couchbase-password.retired" not in text.split("--live ]]; then", 1)[1]


# --- wait_ready's restart mode ---

GUARD_PASSED = "hpcop: Couchbase guard passed"


def ready_dir(holder, log, lock=True):
    work = node.make_work(holder)
    (work / "run.log").write_text(log, encoding="utf-8")
    if lock:
        (work / "rootfs" / "root" / "service-ready.lock").touch()
    return work


def wait_ready(env, work, mode):
    """wait_ready with this bash as the service (alive) and a few seconds to spare."""
    return node.lib('wait_ready "$1" "$$" 5 "$2"', str(work), mode, env=env)


def test_o56_a_restart_is_ready_without_a_fresh_deployment_line(holder, env):
    """O56: a restart over an existing deployment prints no `Congratulations! New`: the lock and the guard's passed
    line are enough in restart mode."""
    work = ready_dir(holder, f"starting\n{GUARD_PASSED}\n")
    assert wait_ready(env, work, "restart").returncode == 0


def test_o56_a_fresh_start_still_needs_the_fresh_deployment_line(holder, env):
    """O56: the default mode is unchanged: the same log is refused as a fresh start."""
    work = ready_dir(holder, f"starting\n{GUARD_PASSED}\n")
    result = wait_ready(env, work, "fresh")
    assert result.returncode != 0
    assert "without a fresh deployment" in result.stderr
    assert node.lib('wait_ready "$1" "$$" 5', str(work), env=env).returncode != 0


def test_o56_a_fresh_start_with_the_line_passes_in_both_modes(holder, env):
    """O56: a log with the fresh deployment line and the guard's line is ready in either mode."""
    work = ready_dir(holder, f"Congratulations! New deployment\n{GUARD_PASSED}\n")
    assert wait_ready(env, work, "fresh").returncode == 0
    assert wait_ready(env, work, "restart").returncode == 0


def test_o56_a_restart_without_the_guards_passed_line_is_not_ready(holder, env):
    """O56: the lock alone is not enough: the guard must have confirmed this launch's administrator."""
    work = ready_dir(holder, "starting\n")
    result = wait_ready(env, work, "restart")
    assert result.returncode != 0
    assert "guard never confirmed" in result.stderr


def test_o56_a_restart_fails_on_the_guards_failed_line(holder, env):
    """O56: the guard's FAILED line ends a restart at once, with or without the lock."""
    work = ready_dir(holder, "starting\nhpcop: Couchbase guard FAILED: no administrator\n", lock=False)
    result = wait_ready(env, work, "restart")
    assert result.returncode != 0
    assert "guard FAILED" in result.stderr


def test_o56_a_restart_fails_on_the_launchers_start_error(holder, env):
    """O56: the launcher's own error ends a restart, as it ends a fresh start."""
    work = ready_dir(holder, "ERROR: Starting the container failed: boom\n", lock=False)
    result = wait_ready(env, work, "restart")
    assert result.returncode != 0
    assert "deployment failed" in result.stderr


def test_o56_a_restart_fails_when_the_service_exits_before_it_is_ready(holder, env, processes):
    """O56: a service that ended without the lock is a failed restart, not a wait until the timeout."""
    work = ready_dir(holder, "starting\n", lock=False)
    pid, _ = node.dead_record(processes)
    result = node.lib('wait_ready "$1" "$2" 30 restart', str(work), str(pid), env=env)
    assert result.returncode != 0
    assert "exited before it was ready" in result.stderr


def test_o56_an_unknown_wait_mode_is_refused(holder, env):
    """O56: only fresh and restart exist."""
    work = ready_dir(holder, f"Congratulations! New\n{GUARD_PASSED}\n")
    assert wait_ready(env, work, "resume").returncode != 0
