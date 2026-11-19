"""O93: the hop from an HPC job to the group's LLM fleet (deploy/hpc-federation/fleethop.py). A study's models.toml
names a base URL on the job's own loopback; the manifest's optional `fleet_hop` makes the step run, as a child of its
own, the one ssh command that forwards that port to the front's unix socket on the login node, before its dispatcher
starts, and kill it on every way out. A fake `ssh` on PATH opens a local listener, so nothing leaves this host. Every
host, path and port here is invented and carries "ZQ" (or is the port): none may ever appear in a log, a record or a
line's refusal."""

import argparse
import importlib.util
import itertools
import json
import os
import shlex
import signal
import socket
import stat
import sys
import time
from pathlib import Path

import pytest
from fingerprint_support import standin_python
from fleet_hop_support import KEY, KNOWN, LOGIN, SOCKET, Ssh, accepts, alive, keeper, settled

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_fleet_hop", DEPLOY / "shards.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_fleet_hop"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_fleet_hop", None)
        sys.modules.pop("hpc_fleethop", None)


@pytest.fixture(scope="module")
def hop_module(shards):
    """The module `shards.py` loads as its sibling, so the exception classes the step catches are the tests' own."""
    return shards._sibling("fleethop")


# -- the command and the port ----------------------------------------------------------------------------------------


def test_f03_the_hop_runs_exactly_the_ssh_command_and_returns_once_its_port_accepts(hop_module, tmp_path, monkeypatch):
    """O93: the command is the brief's, word for word, and `start` returns only when 127.0.0.1:<port> accepts a
    connection; `stop` leaves no ssh behind."""
    ssh = Ssh(tmp_path, monkeypatch)
    with keeper(hop_module, ssh.hop()) as hop:
        assert accepts(ssh.port)
        (first,) = ssh.started()
        assert first["argv"] == [
            "-F", "/dev/null", "-i", KEY, "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", f"UserKnownHostsFile={KNOWN}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes", "-o", "CheckHostIP=no", "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-N",
            "-L", f"127.0.0.1:{ssh.port}:{SOCKET}", LOGIN,
        ]  # fmt: skip
        assert hop.restarts == 0
    assert settled(lambda: not alive(first["pid"]))
    assert not accepts(ssh.port)


def test_f03_a_hop_whose_port_never_opens_is_refused_naming_fleet_hop_and_leaves_no_ssh(
    hop_module, tmp_path, monkeypatch
):
    """O93: a hop that runs but never opens its port is refused after the bound, the message names `fleet_hop` and no
    host, path or port, and the ssh it started is gone."""
    ssh = Ssh(tmp_path, monkeypatch, "nolisten")
    with pytest.raises(hop_module.HopRefused) as refused:
        keeper(hop_module, ssh.hop(), wait_s=1.0).start()
    text = str(refused.value)
    assert "fleet_hop" in text
    assert not any(secret in text for secret in (LOGIN, SOCKET, KEY, KNOWN, str(ssh.port)))
    (first,) = ssh.started()
    assert settled(lambda: not alive(first["pid"]))


def test_f03_a_port_something_else_holds_open_is_no_hop_while_its_own_ssh_has_exited(hop_module, tmp_path, monkeypatch):
    """O93: an ssh that exits at once (ExitOnForwardFailure on a port another listener holds, a refused key) is no hop
    even though the port accepts: the keeper wants its own child alive too."""
    ssh = Ssh(tmp_path, monkeypatch, "die")
    stranger = socket.socket()
    stranger.bind(("127.0.0.1", ssh.port))
    stranger.listen()
    try:
        with pytest.raises(hop_module.HopRefused, match="fleet_hop"):
            keeper(hop_module, ssh.hop(), wait_s=1.0, min_interval_s=5.0, max_interval_s=5.0).start()
    finally:
        stranger.close()


def test_f03_a_hop_that_dies_is_restarted_no_faster_than_the_interval_and_the_restarts_are_counted(
    hop_module, tmp_path, monkeypatch
):
    """O93: while the dispatcher runs the keeper starts the hop again when it exits, each restart at least
    `min_interval_s` after the start before it (and later after one that dies quickly again), counting them; the port
    opens again."""
    ssh = Ssh(tmp_path, monkeypatch, "listen_then_die", "listen_then_die", "stay", life=0.8)
    with keeper(hop_module, ssh.hop(), min_interval_s=0.5, max_interval_s=2.0) as hop:
        assert settled(lambda: hop.restarts == 2)
        assert settled(lambda: accepts(ssh.port))
        starts = ssh.started()
        gaps = [later["t"] - earlier["t"] for earlier, later in itertools.pairwise(starts)]
        assert len(starts) == 3
        # the ssh logs its own start, a little after the keeper's: the gaps are the interval within that jitter
        assert gaps[0] >= 0.45
        assert gaps[1] >= 0.9  # the second restart waited longer (1.0 s, not 0.5 s): the backoff doubled
        assert hop.status() == "fleet_hop: true (restarts: 2)"
    assert settled(lambda: not alive(starts[-1]["pid"]))


def test_f03_nothing_is_restarted_once_the_keeper_is_stopped(hop_module, tmp_path, monkeypatch):
    """O93: a hop that dies while the keeper is being stopped is not started again behind the cleanup."""
    ssh = Ssh(tmp_path, monkeypatch, "listen_then_die", life=0.8)
    one = keeper(hop_module, ssh.hop(), min_interval_s=0.2, max_interval_s=0.2)
    one.start()
    one.stop()
    one.stop()  # twice is a no-op
    time.sleep(1.5)
    assert len(ssh.started()) == 1
    assert not accepts(ssh.port)


# -- what the hop's values may and may not be ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"login": "-oProxyCommand=x"}, {"login": "a b"}, {"login": ""}, {"login": 7}, {"login": "a:b"},
        {"socket": "relative.sock"}, {"socket": "/a:b/s"}, {"socket": "/a/../s"}, {"socket": "/a//s"},
        {"socket": "/a/s\n"}, {"key": "k"}, {"key": 7}, {"known_hosts": "/a/./h"}, {"local_port": "30051"},
        {"local_port": True}, {"local_port": 80}, {"local_port": 70000}, {"extra": 1}, {"login": None},
    ],
)  # fmt: skip
def test_f03_a_hop_that_could_change_the_command_or_is_not_a_table_of_the_five_values_is_refused(hop_module, bad):
    """O93: no value may add an option to the ssh command (a login starting `-`, a socket with the `:` that separates
    the forward's parts), and every value is of its type; the reason names `fleet_hop`, never a value."""
    good = {"login": LOGIN, "socket": SOCKET, "key": KEY, "known_hosts": KNOWN, "local_port": 30051}
    assert hop_module.hop_problem(good) is None
    problem = hop_module.hop_problem({**good, **bad})
    assert problem
    assert not any(str(v) in problem for v in (LOGIN, SOCKET, KEY, KNOWN) if v)
    for missing in good:
        assert hop_module.hop_problem({k: v for k, v in good.items() if k != missing})
    for not_a_table in ([good], "x", None):
        assert hop_module.hop_problem(not_a_table)


# -- the step --------------------------------------------------------------------------------------------------------


def make_study(tmp_path: Path) -> Path:
    study = tmp_path / "study"
    study.mkdir(parents=True, exist_ok=True)
    study.chmod(0o755)
    (study / "study.toml").write_text("[study]\n")
    (study / "campaign.toml").write_text("[scheduler]\nworkers = 1\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return study


def step_args(tmp_path: Path, hop: dict | None = None) -> argparse.Namespace:
    extra = {} if hop is None else {"fleet_hop": json.dumps(hop, sort_keys=True)}
    return argparse.Namespace(study=make_study(tmp_path), index=1, count=2, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=False, archive_left=False,
                              snapshot_every=3600.0, **extra)  # fmt: skip


def dispatcher(port: int):
    """The stand-in dispatcher: whether the hop's port accepted when it started, and what its step recorded."""
    seen = {}

    def run(argv, env, cwd):
        seen["port_open"] = accepts(port)
        seen["owner"] = json.loads((Path(cwd) / "step.json").read_text())
        seen["env"] = dict(env)
        copy = Path(argv[argv.index("run") + 1])
        (copy / "out" / "runs").mkdir(parents=True, exist_ok=True)
        (copy / "out" / "runs" / "r1.json").write_text("{}")
        (copy / "out" / "campaign.lock").write_text("")
        if seen.get("wait_for"):
            seen["wait_for"]()
        return 0

    return run, seen


def never_run(argv, env, cwd):
    raise AssertionError("a refused step must never start its dispatcher")


def run_step(shards, args, runner, hop_module, **timing):
    made = []

    def make(hop):
        made.append(keeper(hop_module, hop, **timing))
        return made[-1]

    code = shards.step(args, runner=runner, core_found=lambda path: True, hop_keeper=make)
    return code, made


def nothing_of_the_hop(root: Path, port: int, extra=()) -> None:
    """No file under `root` (names included) holds a login, a path of the hop's, or its port."""
    for path in [root, *root.rglob("*")]:
        text = str(path.relative_to(root)).encode() if path != root else b""
        body = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        for marker in (b"ZQ", str(port).encode(), *extra):
            assert marker not in text
            assert marker not in body


def test_f03_the_hop_is_open_before_the_dispatcher_starts_and_gone_after_the_step_with_nothing_of_it_recorded(
    shards, hop_module, tmp_path, monkeypatch, capsys
):
    """O93: the dispatcher starts with the port accepting; its step's record says `fleet_hop: true` and nothing of the
    host, the paths or the port; the ssh is gone when the step ends; what the step prints is the flag and the count."""
    ssh = Ssh(tmp_path, monkeypatch)
    run, seen = dispatcher(ssh.port)
    code, _ = run_step(shards, step_args(tmp_path, ssh.hop()), run, hop_module)
    assert code == 0
    assert seen["port_open"] is True
    assert seen["owner"]["fleet_hop"] is True
    assert not any(secret in json.dumps(seen["owner"]) for secret in (LOGIN, SOCKET, KEY, KNOWN, str(ssh.port)))
    (first,) = ssh.started()
    assert settled(lambda: not alive(first["pid"]))
    shown = capsys.readouterr()
    assert "fleet_hop: true (restarts: 0)" in shown.err
    assert not any("ZQ" in text or str(ssh.port) in text for text in (shown.out, shown.err))  # ssh's own words too
    nothing_of_the_hop(tmp_path / "results", ssh.port)
    assert not list((tmp_path / "tmpfs").iterdir())


def test_f03_a_step_whose_hop_never_opens_is_refused_before_the_dispatcher_with_its_own_exit_code(
    shards, hop_module, tmp_path, monkeypatch, capsys
):
    """O93: the step ends with `FLEET_HOP_FAILED`, a message naming `fleet_hop` only, no dispatcher, no ssh left and no
    directory of its own on the tmpfs."""
    ssh = Ssh(tmp_path, monkeypatch, "nolisten")
    code, _ = run_step(shards, step_args(tmp_path, ssh.hop()), never_run, hop_module, wait_s=1.0)
    assert code == shards.FLEET_HOP_FAILED == hop_module.FLEET_HOP_FAILED
    assert code not in (2, 71, 73, 74, 75, 97)
    shown = capsys.readouterr()
    assert "fleet_hop" in shown.err
    assert not any("ZQ" in text or str(ssh.port) in text for text in (shown.out, shown.err))
    (first,) = ssh.started()
    assert settled(lambda: not alive(first["pid"]))
    assert not list((tmp_path / "tmpfs").iterdir())


def test_f03_a_hop_that_dies_while_the_dispatcher_runs_is_restarted_and_the_step_says_how_often(
    shards, hop_module, tmp_path, monkeypatch, capsys
):
    """O93: the dispatcher's own wait sees the hop restarted (its port accepts again), and the step's last words give
    the count and nothing else."""
    ssh = Ssh(tmp_path, monkeypatch, "listen_then_die", "stay", life=0.9)
    run, seen = dispatcher(ssh.port)
    holder = []
    seen["wait_for"] = lambda: settled(lambda: holder[0].restarts == 1 and accepts(ssh.port))

    def make(hop):
        holder.append(keeper(hop_module, hop, min_interval_s=0.3, max_interval_s=1.0))
        return holder[0]

    code = shards.step(step_args(tmp_path, ssh.hop()), runner=run, core_found=lambda path: True, hop_keeper=make)
    assert code == 0
    assert holder[0].restarts == 1
    shown = capsys.readouterr().err
    assert "fleet_hop: true (restarts: 1)" in shown
    assert "ZQ" not in shown
    assert settled(lambda: not any(alive(entry["pid"]) for entry in ssh.started()))


def test_f03_a_term_while_the_dispatcher_runs_ends_the_step_through_its_cleanup_and_kills_the_hop(
    shards, hop_module, tmp_path, monkeypatch
):
    """O93: a stop signal ends the step with 128 + N through its `finally`, with the hop killed and the step's
    directory removed, as the operator's key files are."""
    ssh = Ssh(tmp_path, monkeypatch)
    before = signal.getsignal(signal.SIGTERM)

    def stopped(argv, env, cwd):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        raise AssertionError("the signal did not end the step")

    with pytest.raises(SystemExit) as ended:
        run_step(shards, step_args(tmp_path, ssh.hop()), stopped, hop_module)
    assert ended.value.code == 128 + signal.SIGTERM
    (first,) = ssh.started()
    assert settled(lambda: not alive(first["pid"]))
    assert not list((tmp_path / "tmpfs").iterdir())
    assert signal.getsignal(signal.SIGTERM) == before


def test_f03_a_step_that_raises_after_the_hop_started_still_kills_it(shards, hop_module, tmp_path, monkeypatch):
    ssh = Ssh(tmp_path, monkeypatch)

    def crash(argv, env, cwd):
        raise RuntimeError("the dispatcher's wrapper broke")

    with pytest.raises(RuntimeError, match="wrapper broke"):
        run_step(shards, step_args(tmp_path, ssh.hop()), crash, hop_module)
    (first,) = ssh.started()
    assert settled(lambda: not alive(first["pid"]))
    assert not list((tmp_path / "tmpfs").iterdir())


def test_f03_a_step_with_no_hop_starts_no_ssh_and_its_record_is_what_it_was(shards, hop_module, tmp_path, monkeypatch):
    """O93: unset, nothing changes: no ssh is run, the owner record has no `fleet_hop`."""
    ssh = Ssh(tmp_path, monkeypatch)
    run, seen = dispatcher(ssh.port)
    code, made = run_step(shards, step_args(tmp_path), run, hop_module)
    assert code == 0
    assert made == []
    assert ssh.started() == []
    assert "fleet_hop" not in seen["owner"]
    assert seen["port_open"] is False


# -- the manifest and the line ---------------------------------------------------------------------------------------


def manifest_file(tmp_path: Path, extra: str = "") -> Path:
    make_study(tmp_path)
    lines = [f'study = "{tmp_path / "study"}"', f'python = "{standin_python(tmp_path / "venv")}"',
             f'core_src = "{tmp_path / "core" / "src"}"', f'results = "{tmp_path / "results"}"',
             f'tmpfs = "{tmp_path / "tmpfs"}"', 'step_path = "/usr/bin:/bin"', "cpus = 6",
             'memory = "18g"']  # fmt: skip
    for n, name in enumerate(("a", "b"), 1):
        root = tmp_path / "fed" / name
        root.mkdir(parents=True, exist_ok=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": str(100 + n), "name": f"hpcop-x-{n}"},
                 "sandbox": {"placement": "het", "node": f"node-s{n}", "het_group": 1}, "phase": "linked",
                 "data": {"ingested": {}}}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        (root / "platform.toml").write_text('[space]\nname = "s"\ndata_manifest = "m.json"\n')
        (root / "facts.json").write_text("{}")
    if extra:
        lines.append(extra)
    for label, name in (("fed-a", "a"), ("fed-b", "b")):
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{tmp_path / "fed" / name}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def printed(shards, tmp_path, capsys, command, extra="") -> list[str]:
    assert shards.main([command, "--manifest", str(manifest_file(tmp_path, extra))]) == 0
    return capsys.readouterr().out.splitlines()


TABLE = (
    f'fleet_hop = {{ login = "{LOGIN}", socket = "{SOCKET}", key = "{KEY}", '
    f'known_hosts = "{KNOWN}", local_port = 30051 }}'
)


def test_f03_the_hop_rides_on_every_line_and_nothing_else_changes_and_the_plan_says_only_that_there_is_one(
    shards, tmp_path, capsys
):
    """O93: `lines` ends each line in `--fleet-hop <the table as JSON>` and is otherwise the line a manifest without the
    key prints; `plan` carries the same line and says `fleet_hop: true`, as it says `home_files: true`."""
    plain = printed(shards, tmp_path / "plain", capsys, "lines")
    lines = printed(shards, tmp_path / "with", capsys, "lines", TABLE)
    carried = json.dumps({"known_hosts": KNOWN, "key": KEY, "local_port": 30051, "login": LOGIN, "socket": SOCKET},
                         sort_keys=True, separators=(",", ":"))  # fmt: skip
    for line, base in zip(lines, plain, strict=True):
        words = shlex.split(line)
        assert words[-2:] == ["--fleet-hop", carried]
        stripped = shlex.join(words[:-2])
        assert stripped.replace(str(tmp_path / "with"), "ROOT") == base.replace(str(tmp_path / "plain"), "ROOT")
    plans = [json.loads(p) for p in printed(shards, tmp_path / "with", capsys, "plan", TABLE)]
    assert [p["fleet_hop"] for p in plans] == [True, True]
    assert all("fleet_hop" not in json.loads(p) for p in printed(shards, tmp_path / "plain", capsys, "plan"))


def hop_table(**changes) -> str:
    base = {"login": "h", "socket": "/s", "key": "/k", "known_hosts": "/h", "local_port": 30051, **changes}
    return "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in base.items()) + " }"


@pytest.mark.parametrize(
    "value",
    ['"a string"', "7", "true", "{ login = 'x' }", "[1]", hop_table(login="-x"), hop_table(socket="s:/x"),
     hop_table(local_port="30051")],
)  # fmt: skip
def test_f03_a_fleet_hop_that_cannot_be_meant_refuses_the_plan_naming_the_key_and_no_value(
    shards, tmp_path, capsys, value
):
    path = manifest_file(tmp_path, f"fleet_hop = {value}")
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(path)]) == 2
        shown = capsys.readouterr()
        assert shown.out == ""
        assert "fleet_hop" in shown.err
        assert "30051" not in shown.err


def test_f03_the_plan_id_and_the_step_line_of_a_manifest_without_a_hop_are_what_they_were(shards, tmp_path, capsys):
    """O93: the plan's id hashes the manifest's study, results and shards, so the hop is not in it; a line without the
    key has no `--fleet-hop`."""
    one = shards.load_plan(manifest_file(tmp_path / "a"))
    two = shards.load_plan(manifest_file(tmp_path / "b", TABLE))
    assert one.fleet_hop is None
    assert json.loads(two.fleet_hop) == {
        "login": LOGIN,
        "socket": SOCKET,
        "key": KEY,
        "known_hosts": KNOWN,
        "local_port": 30051,
    }
    assert all("--fleet-hop" not in line for line in printed(shards, tmp_path / "c", capsys, "lines"))


# -- the standalone probe --------------------------------------------------------------------------------------------


def test_f03_the_probe_prints_the_status_of_a_keyless_request_through_the_hop_and_nothing_else(
    hop_module, tmp_path, monkeypatch, capsys
):
    """O93: `fleethop.py probe` runs the hop through the same keeper, makes one keyless GET through it and prints the
    HTTP status alone; the ssh is gone after."""
    ssh = Ssh(tmp_path, monkeypatch, "http401")
    assert hop_module.main(["probe", "--fleet-hop", json.dumps(ssh.hop())]) == 0
    shown = capsys.readouterr()
    assert shown.out == "401\n"
    assert shown.err == ""
    (first,) = ssh.started()
    assert settled(lambda: not alive(first["pid"]))


def test_f03_the_probe_of_a_hop_that_never_opens_says_so_without_a_host_a_path_or_a_port(
    hop_module, tmp_path, monkeypatch, capsys
):
    ssh = Ssh(tmp_path, monkeypatch, "nolisten")
    monkeypatch.setattr(hop_module, "WAIT_S", 1.0)
    assert hop_module.main(["probe", "--fleet-hop", json.dumps(ssh.hop())]) == hop_module.FLEET_HOP_FAILED
    shown = capsys.readouterr()
    assert shown.out == ""
    assert "fleet_hop" in shown.err
    assert not any(secret in shown.err for secret in (LOGIN, SOCKET, KEY, KNOWN, str(ssh.port)))


def test_f03_the_fake_ssh_is_the_only_ssh_these_tests_can_reach(tmp_path, monkeypatch):
    """The tests' own guard: PATH starts with the fake, so a real ssh is never run from here."""
    ssh = Ssh(tmp_path, monkeypatch)
    import shutil

    assert Path(shutil.which("ssh")) == ssh.directory / "ssh"
    assert stat.S_IMODE((ssh.directory / "ssh").stat().st_mode) == 0o755
