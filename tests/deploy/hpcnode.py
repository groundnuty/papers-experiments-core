"""What the offline tests of the HPC bundle's node-side scripts share (test_hpc_keep.py, test_hpc_restart.py).

The scripts run in plain bash on this host, against temporary work directories, and signal fake processes (a Python
interpreter that records its start tick the way supervise.py does) instead of a service. Nothing here starts a
container, reaches Slurm or opens a network connection. Loaded by path, as test_hpc_run_steps.py loads its helpers.
"""

import contextlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle"
SECRETS = ("passphrase", "erlang-cookie", "couchbase-user", "couchbase-password", "config.yaml", "env")

# A service that ends when asked: SIGTERM is its graceful stop. It records what the work directory held when the
# signal arrived (argv[1], when given), so a test sees what was written before the SIGTERM.
GRACEFUL = """
import os, signal, sys, time
work = sys.argv[1] if len(sys.argv) > 1 else None
def on_term(*_):
    if work:
        seen = []
        for name in ("stop-requested", "keep-until"):
            path = os.path.join(work, name)
            seen.append(name + "=" + (open(path).read().strip() if os.path.exists(path) else "-"))
        open(os.path.join(work, "witness"), "w").write(" ".join(seen))
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
time.sleep(300)
"""

# A service that ignores SIGTERM and dies of SIGUSR1, as supervise.py's `run` turns that signal into SIGKILL.
STUBBORN = """
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGUSR1, lambda *_: os.kill(os.getpid(), signal.SIGKILL))
print("ready", flush=True)
time.sleep(300)
"""


def pidfd_python():
    """An interpreter the bundle's signals can use: supervise.py needs os.pidfd_open and signal.pidfd_send_signal,
    which some builds (a standalone CPython among them) lack. This one first, then the system's."""
    for name in (sys.executable, "python3", "python3.13", "python3.12", "python3.11", "python3.10", "python3.9"):
        path = shutil.which(name)
        if path is None:
            continue
        probe = subprocess.run(
            [path, "-I", str(BUNDLE / "supervise.py"), "check"], capture_output=True, timeout=30, check=False
        )
        if probe.returncode == 0:
            return path
    return None


PIDFD_PY = pidfd_python()
needs_pidfd = pytest.mark.skipif(PIDFD_PY is None, reason="no interpreter with pidfd_open on this host")


def start_tick(pid):
    """Field 22 of /proc/PID/stat, counted after the last ')' as supervise.py and lib.sh count it."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    return stat[stat.rfind(")") + 2 :].split()[19]


def node_env(holder, **extra):
    """The environment of a node-side script: TMPDIR and the shared tmp are `holder`, which holds the work
    directories; Python is one with pidfd (the bundle's default, python3.11, may not exist here); fast polling."""
    env = dict(os.environ)
    env.update(
        HPCOP_PY=PIDFD_PY or sys.executable,
        HPCOP_SHARED_TMP=str(holder),
        HPCOP_TEST_TRUSTED_UID=str(os.stat("/").st_uid),  # root shows as another uid in a user namespace
        HPCOP_POLL_S="0.1",
        HPCOP_SKIP_PORTCHECK="1",
        TMPDIR=str(holder),
        LC_ALL="C",
    )
    env.update(extra)
    return env


def make_work(holder, kind="op", suffix="Ab12Cd", secrets=True):
    """A work directory as node-init.sh and configure.sh leave it: 0700, an owner record for a process that ended, a
    complete rootfs, the six per-launch secrets (and the registration token of a provider), all 0600."""
    work = Path(holder) / f"hpcop-{kind}.{suffix}"
    work.mkdir(mode=0o700)
    work.chmod(0o700)
    (work / "tmp").mkdir(mode=0o700)
    (work / "rootfs" / "root" / ".hpcop").mkdir(parents=True)
    (work / "rootfs" / "root" / ".hpcop").chmod(0o700)
    (work / "rootfs.complete").touch()
    (work / "owner.pid").write_text("1999999999 1\n", encoding="ascii")
    if secrets:
        names = SECRETS + (("registration-token",) if kind == "op" else ())
        for name in names:
            put(work / "rootfs" / "root" / ".hpcop" / name, f"value-of-{name}\n")
    return work


def put(path, text, mode=0o600):
    Path(path).write_text(text, encoding="utf-8")
    Path(path).chmod(mode)


def secrets_of(work):
    return Path(work) / "rootfs" / "root" / ".hpcop"


def spawn(processes, code=GRACEFUL, *args):
    """A fake service, started in its own session once it has installed its handlers; the fixture ends it."""
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", code, *args],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    processes.append(proc)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


def record(work, proc):
    """service.pid as run_service writes it: the PID and the start tick, one line."""
    put(Path(work) / "service.pid", f"{proc.pid} {start_tick(proc.pid)}\n")


def gone(proc):
    """The process has ended (a zombie of ours counts: lib.sh's alive() does not)."""
    return proc.poll() is not None


def kill_all(processes):
    for proc in processes:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def dead_record(processes):
    """A (PID, start tick) of a process that has ended, as a service.pid or owner.pid left by a stop would hold."""
    proc = spawn(processes, GRACEFUL, "/nonexistent")
    pid, tick = proc.pid, start_tick(proc.pid)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    return pid, tick


def bash(script, *args, env, timeout=60, stdin=None):
    return subprocess.run(
        ["bash", "-c", script, "bash", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
        input=stdin,
    )


def script(name, *args, env, bundle=BUNDLE, timeout=90, stdin=None):
    return subprocess.run(
        ["bash", str(bundle / name), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
        input=stdin,
    )


def lib(snippet, *args, env, bundle=BUNDLE, timeout=60):
    """A snippet of bash after sourcing lib.sh; `$@` is `args`."""
    return bash(f'source "{bundle}/lib.sh"; {snippet}', *args, env=env, timeout=timeout)


def snapshot(root):
    """Every file and directory under `root` with its mode and content, to prove a refusal changed nothing."""
    found = {}
    for path in sorted(Path(root).rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            found[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            found[rel] = ("dir", oct(path.stat().st_mode & 0o777))
        else:
            found[rel] = ("file", oct(path.stat().st_mode & 0o777), path.read_bytes())
    return found


# --- stand-ins for what a node has and a test host has not: apptainer, curl (Couchbase and Onedata REST), hostname ---

FAKE_APPTAINER = r"""#!/bin/bash
# The service (exec --writable ... ROOTFS /root/hpcop-start.sh) logs which markers existed when it started, prints the
# guard's verdict, becomes ready and waits for SIGTERM; any other use is exec.sh's: log the arguments, count stdin.
if [[ " $* " == *" --writable "* ]]; then
  # FAKE_RUN_ARGS: record the service's argv, one word per line, and end at once (the bind tests, O57)
  if [[ -n ${FAKE_RUN_ARGS:-} ]]; then printf '%s\n' "$@" > "$FAKE_RUN_ARGS"; exit 0; fi
  R=${*: -2:1}; W=${R%/rootfs}
  if [[ -n ${FAKE_SERVICE_ARGS:-} ]]; then printf '%s\n' "$@" > "$FAKE_SERVICE_ARGS"; fi   # how the service was started
  for f in stop-requested service-verified rootfs/root/service-ready.lock; do
    if [[ -e $W/$f ]]; then echo "present $f" >> "$FAKE_LOG"; else echo "absent $f" >> "$FAKE_LOG"; fi
  done
  if [[ ${FAKE_GUARD:-pass} == fail ]]; then
    echo "hpcop: Couchbase guard FAILED: injected by the test"
  else
    echo "hpcop: Couchbase guard passed (stand-in)"
    touch "$R/root/service-ready.lock"
  fi
  trap 'exit 0' TERM
  while :; do sleep 0.2; done
fi
printf '%s\n' "$@" > "$FAKE_EXEC_ARGS"
echo "stdin: $(cat | wc -c) bytes" >> "$FAKE_EXEC_ARGS"
echo "output of the command"
echo "error of the command" >&2
exit "${FAKE_EXEC_RC:-0}"
"""

FAKE_HOSTNAME = """#!/bin/bash
if [[ ${1:-} == -f ]]; then echo provider-a.example.invalid; else echo provider-a; fi
"""

# Couchbase 4.5's two REST calls the bundle makes from the host, and the Onedata answers finish*.sh reads, from a
# state file the test owns: the administrator Couchbase holds now, and the identity the service reports.
FAKE_CURL = r"""#!PYTHON
import json, os, re, sys
args = sys.argv[1:]
url = args[-1]
out = method = cfg = data = None
i = 0
while i < len(args) - 1:
    flag = args[i]
    if flag in ("-o", "-X", "-K", "--data", "-w", "--connect-timeout", "--max-time", "--cacert", "--resolve", "-H"):
        value = args[i + 1]
        if flag == "-o": out = value
        elif flag == "-X": method = value
        elif flag == "-K": cfg = value
        elif flag == "--data": data = value
        i += 2
    else:
        i += 1
if os.environ.get("FAKE_CURL_LOG"):  # every call's arguments, for a test of what a script dialled
    open(os.environ["FAKE_CURL_LOG"], "a").write(" ".join(args) + "\n")
state = json.load(open(os.environ["FAKE_CURL_STATE"]))
creds = None
if cfg:
    m = re.search(r'user = "([^:]+):([^"]+)"', open(cfg).read())
    creds = [m.group(1), m.group(2)]
def reply(code, body):
    open(out, "w").write(json.dumps(body))
    sys.stdout.write(str(code))
    sys.exit(0)
admin = creds is not None and creds == state["creds"]
if url == "http://127.0.0.1:8091/settings/web":
    if method == "POST" and admin:
        form = open(data[1:]).read()
        state["creds"] = [re.search(r"username=([^&]*)", form).group(1), re.search(r"password=([^&]*)", form).group(1)]
        json.dump(state, open(os.environ["FAKE_CURL_STATE"], "w"))
        reply(200, {})
    if method == "GET" and admin:
        reply(200, {"port": 8091, "username": creds[0]})
    reply(401, {})
if url == "http://127.0.0.1:8091/pools/default":
    reply(200 if admin else 401, {})
if url.endswith(":8443/configuration"):
    # op_worker reads its domain over its Onezone connection: for the first `domain_early.reads` answers it says
    # `domain_early.value` (null or "") although REST already answers, as a start whose connection is not up yet does.
    # `domain_missing` leaves the field out. Every answer is counted in `config_reads`.
    seen = state.get("config_reads", 0) + 1
    state["config_reads"] = seen
    json.dump(state, open(os.environ["FAKE_CURL_STATE"], "w"))
    early = state.get("domain_early")
    body = {"providerId": state["providerId"], "domain": state["domain"]}
    if early and seen <= early["reads"]:
        body["domain"] = early["value"]
    if state.get("domain_missing"):
        del body["domain"]
    reply(200, body)
if url.endswith(":8443/api/v3/onezone/configuration"):
    reply(200, {"name": "zone-a", "domain": state["domain"]})
if url.endswith(":8443/api/v3/onezone/user"):
    reply(200 if creds and creds[0] == "admin" else 401, {"userId": state["adminUserId"]})
if url.endswith(":8443/api/v3/onezone/user/tokens/named") and method == "POST":
    reply(200 if creds and creds[0] == "admin" else 401, {"token": state.get("token", "minted-registration-token")})
if url.endswith(":9443/api/v3/onepanel/provider/storages") and method == "GET":
    reply(200, {"ids": ["storage-1"]})
reply(599, {"unexpected": url})
"""


def install_fakes(bin_dir):
    """apptainer, curl and hostname stand-ins in `bin_dir`, to put first on PATH."""
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(exist_ok=True)
    for name, text in (
        ("apptainer", FAKE_APPTAINER),
        ("hostname", FAKE_HOSTNAME),
        ("curl", FAKE_CURL.replace("#!PYTHON", f"#!{sys.executable}", 1)),
    ):
        (bin_dir / name).write_text(text, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    return bin_dir
