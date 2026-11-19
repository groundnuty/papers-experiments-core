"""O93: the hop in the elastic fleet (deploy/hpc-federation/fleetplan.py and fleetworker.py). The fleet's shard settings
take `fleet_hop` (a table: they were scalars), write it into the manifest the shard steps read, and the worker's line
carries it; on the node the worker starts the hop before it runs and kills it on every way out, as it removes the
operator's key files. A fake `ssh` on PATH opens a local listener; every value is invented."""

import json
import os
import shlex
import signal
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fleet_hop_support import KEY, KNOWN, LOGIN, SOCKET, Ssh, accepts, alive, keeper, settled
from hpc_fleet_support import fleet_modules

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
SPEC = {"login": LOGIN, "socket": SOCKET, "key": KEY, "known_hosts": KNOWN}
CANONICAL = json.dumps


def table(port: int) -> str:
    return (
        "fleet_hop = { " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in {**SPEC, "local_port": port}.items()) + " }"
    )


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetplan
        import fleetrecord
        import fleetworker

        shards = fleetworker._shards()
        refusal = shards.core_refusal
        shards.core_refusal = lambda report: None  # the suite's core is not what these tests ask about
        try:
            yield SimpleNamespace(
                found=found, fleetplan=fleetplan, fleetrecord=fleetrecord, fleetworker=fleetworker, shards=shards
            )
        finally:
            shards.core_refusal = refusal
            sys.modules.pop("hpc_shards", None)
            sys.modules.pop("hpc_fleethop", None)


def settings_file(tmp_path: Path, extra: str = "") -> Path:
    (tmp_path / "study").mkdir(exist_ok=True)
    lines = [f'study = "{tmp_path / "study"}"', 'python = "/usr/bin/python3"', f'core_src = "{tmp_path / "src"}"',
             f'results = "{tmp_path / "results"}"', f'tmpfs = "{tmp_path / "tmpfs"}"', 'step_path = "/usr/bin:/bin"',
             "cpus = 2", 'memory = "4g"', extra]  # fmt: skip
    path = tmp_path / "settings.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


# -- the fleet's settings and the manifest they make ------------------------------------------------------------------


def test_f03_the_fleets_shard_settings_take_a_fleet_hop_table_and_write_it_into_the_manifest(deploy, tmp_path):
    """O93: `read_settings` takes the table, `manifest_text` writes it as an inline table the shard steps' manifest
    reader reads back to the same values (the fleet compares the manifest it wrote with the one it would write now, so
    the text is the same every time), and `shards.load_plan` accepts it."""
    settings = deploy.fleetplan.read_settings(settings_file(tmp_path, table(30051)))
    assert settings["fleet_hop"] == {**SPEC, "local_port": 30051}
    text = deploy.fleetplan.manifest_text(settings, [("shard-1", "/fed/a"), ("shard-2", "/fed/b")])
    assert text == deploy.fleetplan.manifest_text(settings, [("shard-1", "/fed/a"), ("shard-2", "/fed/b")])
    assert tomllib.loads(text)["fleet_hop"] == {**SPEC, "local_port": 30051}
    (tmp_path / "manifest.toml").write_text(text)
    fed = {"study", "results", "tmpfs", "core_src"}
    assert fed <= set(tomllib.loads(text))
    plan = deploy.shards.load_plan(tmp_path / "manifest.toml")
    assert json.loads(plan.fleet_hop) == {**SPEC, "local_port": 30051}


def test_f03_settings_without_a_hop_write_the_manifest_they_always_did(deploy, tmp_path):
    settings = deploy.fleetplan.read_settings(settings_file(tmp_path))
    assert "fleet_hop" not in settings
    assert "fleet_hop" not in deploy.fleetplan.manifest_text(settings, [("shard-1", "/fed/a")])


@pytest.mark.parametrize("bad", ['"a string"', "7", "{ login = 'x' }", '{ login = "-x" }'])
def test_f03_settings_with_a_fleet_hop_that_cannot_be_meant_are_refused_naming_the_key_and_no_value(
    deploy, tmp_path, bad
):
    path = settings_file(tmp_path, f"fleet_hop = {bad}")
    with pytest.raises(deploy.fleetrecord.FleetError, match="fleet_hop") as refused:
        deploy.fleetplan.read_settings(path)
    assert "-x" not in str(refused.value)


# -- the worker's line and the worker on its node ---------------------------------------------------------------------


def worker_line(deploy, settings: dict) -> list[str]:
    return deploy.fleetworker.login_line(
        {"python": "/usr/bin/python3", "study": "/study", "tmpfs": "/tmp/x", "step_path": "/usr/bin", "cpus": 2,
         "memory": "4g", **settings},
        {"job": "5", "node": "n1", "name": "fed"}, Path("/fed/root"), "fed-worker", "site-fed-1", "/chan", 3600.0,
    )  # fmt: skip


def test_f03_the_workers_line_carries_the_hop_as_one_json_argument_and_a_line_without_one_has_none(deploy):
    hop = {**SPEC, "local_port": 30051}
    line = worker_line(deploy, {"fleet_hop": hop})
    assert line[line.index("--fleet-hop") + 1] == json.dumps(hop, sort_keys=True, separators=(",", ":"))
    assert "--fleet-hop" not in worker_line(deploy, {})
    assert shlex.split(shlex.join(line)) == line  # one argument: it survives a shell


def node_args(tmp_path: Path, hop: dict | None) -> SimpleNamespace:
    study, root = tmp_path / "study", tmp_path / "fed" / "root"
    study.mkdir(parents=True, exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    root.mkdir(parents=True, exist_ok=True)
    (root / "platform.toml").write_text('[space]\nname = "s"\n')
    (tmp_path / "tmpfs").mkdir(exist_ok=True)
    (tmp_path / "channel").mkdir(exist_ok=True)
    return SimpleNamespace(federation="site-a-fed-1", channel=str(tmp_path / "channel"), study=str(study),
                           tmpfs=str(tmp_path / "tmpfs"), root=str(root), home_files=None, pass_env=[],
                           fleet_hop=None if hop is None else json.dumps(hop))  # fmt: skip


def watcher(port: int):
    seen = {}

    def run(argv, env, cwd):
        seen["port_open"] = accepts(port)
        seen["owner"] = json.loads((Path(cwd) / "step.json").read_text())
        return 0

    return run, seen


def keepers(deploy, **timing):
    """The module under test, its keeper made with short waits, and the keepers it made."""
    made = []

    def make(hop):
        made.append(keeper(deploy.shards._sibling("fleethop"), hop, **timing))
        return made[-1]

    return make, made


def test_f03_the_worker_starts_with_the_hop_open_and_its_record_and_words_say_only_that_there_is_one(
    deploy, tmp_path, monkeypatch, capsys
):
    ssh = Ssh(tmp_path, monkeypatch)
    run, seen = watcher(ssh.port)
    make, _ = keepers(deploy)
    assert deploy.fleetworker.node(node_args(tmp_path, ssh.hop()), run, hop_keeper=make) == 0
    assert seen["port_open"] is True
    assert seen["owner"]["fleet_hop"] is True
    (first,) = ssh.started()
    assert settled(lambda: not alive(first["pid"]))
    shown = capsys.readouterr()
    assert "fleet_hop: true (restarts: 0)" in shown.err
    assert not any("ZQ" in text or str(ssh.port) in text for text in (shown.out, shown.err, json.dumps(seen["owner"])))
    assert not list((tmp_path / "tmpfs").iterdir())


def test_f03_a_worker_whose_hop_never_opens_is_not_started_and_leaves_nothing(deploy, tmp_path, monkeypatch, capsys):
    ssh = Ssh(tmp_path, monkeypatch, "nolisten")
    make, _ = keepers(deploy, wait_s=1.0)

    def never(argv, env, cwd):
        raise AssertionError("a worker whose hop did not open must never run")

    code = deploy.fleetworker.node(node_args(tmp_path, ssh.hop()), never, hop_keeper=make)
    assert code == deploy.shards.FLEET_HOP_FAILED
    shown = capsys.readouterr()
    assert "fleet_hop" in shown.err
    assert not any("ZQ" in text or str(ssh.port) in text for text in (shown.out, shown.err))
    (first,) = ssh.started()
    assert settled(lambda: not alive(first["pid"]))
    assert not list((tmp_path / "tmpfs").iterdir())


def test_f03_a_stop_signal_ends_the_worker_through_its_cleanup_and_kills_the_hop(deploy, tmp_path, monkeypatch):
    ssh = Ssh(tmp_path, monkeypatch)
    make, _ = keepers(deploy)
    before = signal.getsignal(signal.SIGTERM)

    def stopped(argv, env, cwd):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        raise AssertionError("the signal did not end the worker")

    with pytest.raises(SystemExit) as ended:
        deploy.fleetworker.node(node_args(tmp_path, ssh.hop()), stopped, hop_keeper=make)
    assert ended.value.code == 128 + signal.SIGTERM
    (first,) = ssh.started()
    assert settled(lambda: not alive(first["pid"]))
    assert not list((tmp_path / "tmpfs").iterdir())
    assert signal.getsignal(signal.SIGTERM) == before


def test_f03_a_worker_with_no_hop_starts_no_ssh(deploy, tmp_path, monkeypatch):
    ssh = Ssh(tmp_path, monkeypatch)
    run, seen = watcher(ssh.port)
    assert deploy.fleetworker.node(node_args(tmp_path, None), run) == 0
    assert ssh.started() == []
    assert "fleet_hop" not in seen["owner"]
