"""A Compose site of the elastic fleet (`deploy/compose-federation/composesite.py`, `deploy/hpc-federation/
fleetcompose.py`): a host whose federations the operator brought up, its static members, driven over ssh.

The host is this one: every site command goes over the fake `ssh` (`fake_ssh_bin`) to the real `composesite.py`
behind a stub that swaps the elastic worker for a stand-in (a process that starts a "lease run" in a session of its own,
as the worker's `ProcessRunner` does) and shortens the launch's settle. The container engine is a fake on the host's
PATH that answers `ps` from a table of containers per project and logs every call. Nothing reaches Docker."""

import dataclasses
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from elastic_support import FINGERPRINT, FakeClock, config, leased, plan
from fake_ssh_bin import install_fake_ssh
from hpc_fleet_support import fleet_modules
from leased_support import killed_mid_lease, ran_for_real
from study_support import clean_environment

from expcore.campaign.elastic import Ledger, SiteReading
from expcore.campaign.elastic.control import ProveEnded, Retire, Settle

HOST = "zz-vm-5c1e.invalid"
SITE = "site-a"
REAL = Path(__file__).resolve().parents[2] / "deploy" / "compose-federation"
SECRET = "a-secret-value-0123456789"
__all__ = ["clean_environment"]  # a fixture the tests below use

ENGINE = """#!@PYTHON@
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "engine-calls.jsonl"), "a") as log:
    log.write(json.dumps({"engine": os.path.basename(sys.argv[0]), "argv": sys.argv[1:]}) + "\\n")
args = sys.argv[1:]
if args[:2] == ["ps", "--all"]:
    project = args[args.index("--filter") + 1].rsplit("=", 1)[1]
    table = json.load(open(os.path.join(here, "containers.json")))
    for row in table.get(project, []):
        print("\\t".join(row))
"""

# A worker that starts a "lease run" in a session of its own and waits, as the elastic worker does.
STAND_IN = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
with open(os.path.join(os.environ["TMPDIR"], "child.pid"), "w") as handle:
    handle.write(str(child.pid))
time.sleep(600)
"""

STUB = """
import sys
sys.path.insert(0, @REAL@)
import composesite
composesite.SETTLE_S = 0.5
composesite.worker_words = lambda federation, channel, study, work: [sys.executable, @STAND_IN@, federation]
sys.exit(composesite.main())
"""


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules():
        import composesite
        import fleetcompose
        import fleetcontrol
        import fleetexecute
        import fleetidentity

        yield SimpleNamespace(site=composesite, compose=fleetcompose, control=fleetcontrol, execute=fleetexecute,
                              identity=fleetidentity)  # fmt: skip


def make_root(root: Path, project: str, providers: tuple[str, ...] = ("pine", "birch")) -> None:
    """A Compose root as `federation.py up` leaves it, as far as the site reads it."""
    (root / "secrets").mkdir(parents=True, mode=0o700)
    (root / ".expcore-fed-root").write_text(project + "\n", encoding="utf-8")
    facts = {"layout": {"project": project}, "providers": {p: {"id": f"id-{project}-{p}"} for p in providers}}
    (root / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
    (root / "link-check.json").write_text(json.dumps({"ok": True, "pairs": []}), encoding="utf-8")
    (root / "platform.toml").write_text("# the federation's platform file\n", encoding="utf-8")
    (root / "secrets" / "admin.token").write_text(SECRET, encoding="utf-8")


class Vm:
    """The host: its fake engine, the operator's federations (two by default), the site's directory, channel, study
    and the workers' HOME, the stub deploy directory, and the site as the controller reads it from the sites file."""

    def __init__(self, tmp_path: Path, monkeypatch, deploy, engine: str = "docker", members: int = 2) -> None:
        self.deploy, self.tmp = deploy, tmp_path
        self.fake = install_fake_ssh(tmp_path / "bin", monkeypatch, HOST)
        self.base = tmp_path / "vm"
        self.bin = self.base / "enginebin"
        self.bin.mkdir(parents=True)
        for name in ("docker", "podman"):
            (self.bin / name).write_text(ENGINE.replace("@PYTHON@", sys.executable), encoding="utf-8")
            (self.bin / name).chmod(0o755)
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ['PATH']}")  # for the calls made in process
        self.scripts = self.base / "deploy"
        self.scripts.mkdir()
        (self.scripts / "stand_in.py").write_text(STAND_IN, encoding="utf-8")
        stub = STUB.replace("@REAL@", repr(str(REAL))).replace("@STAND_IN@", repr(str(self.scripts / "stand_in.py")))
        (self.scripts / "composesite.py").write_text(stub, encoding="utf-8")
        self.fleet, self.channel, self.home, self.study = (
            self.base / n for n in ("fleet", "channels", "home", "study")
        )
        for directory in (self.fleet, self.home, self.channel, self.study):
            directory.mkdir(mode=0o700)
        (self.study / "study.toml").write_text("# a study\n", encoding="utf-8")
        self.roots = {f"fed-{k}": self.base / "feds" / f"expcore-fed-{k}" for k in range(1, members + 1)}
        self.table: dict = {}
        for k, (member, root) in enumerate(self.roots.items(), start=1):
            make_root(root, f"expcore-fed-{k}")
            self.up(member)
        self.env_file = self.base / "site.env"
        self.env_file.write_text(f"PATH='{self.bin}{os.pathsep}{os.environ['PATH']}'\n", encoding="utf-8")
        self.engine = engine
        self.sites(dict(self.roots))
        self.transport = deploy.execute.SiteTransport(dict(os.environ))

    def sites(self, members: dict) -> None:
        """The sites file with these members (label: root), in this order, read as the controller reads it."""
        path = self.tmp / "sites.toml"
        members_line = ", ".join(f'{m} = "{r}"' for m, r in members.items())
        path.write_text("\n".join([
            "[controller]", f'evidence = "{self.tmp / "evidence"}"', f'archives = "{self.tmp / "archives"}"',
            "[[site]]", f'label = "{SITE}"', 'kind = "compose"', 'transport = "ssh"', f'host = "{HOST}"',
            f'interpreter = "{sys.executable}"', f'deploy = "{self.scripts}"', f'env_file = "{self.env_file}"',
            f'fleet = "{self.fleet}"', f'channel = "{self.channel}"', f'study = "{self.study}"',
            f'home = "{self.home}"', f'engine = "{self.engine}"', f"members = {{ {members_line} }}",
        ]) + "\n", encoding="utf-8")  # fmt: skip
        self.controller, (self.config,) = self.deploy.control.load_config(path)

    def up(self, member: str, services: tuple[str, ...] = ("zone", "pine", "birch"), state: str = "running") -> None:
        """The member's project's containers, each bound to its root, as the engine lists them."""
        root = self.roots[member]
        project = (root / ".expcore-fed-root").read_text(encoding="utf-8").strip()
        self.table[project] = [[f"{project}-{s}-1", s, state, str(root)] for s in services]
        (self.bin / "containers.json").write_text(json.dumps(self.table), encoding="utf-8")

    def engine_calls(self) -> list:
        path = self.bin / "engine-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def view(self):
        view = self.deploy.control.observe_site(self.transport, self.config, None)
        assert view.error is None, view.error
        return {m.label: m for m in view.members}

    def worker(self, member: str) -> dict:
        return json.loads((self.fleet / "members" / member / "worker.json").read_text(encoding="utf-8"))

    def executor(self, ledger, **hooks):
        self.log = self.deploy.execute.Log(None)
        return self.deploy.execute.Executor(ledger, self.tmp / "ledger.sqlite", {SITE: self.config}, self.transport,
                                            self.tmp / "evidence", self.log, **hooks)  # fmt: skip


@pytest.fixture
def vm(deploy, tmp_path, monkeypatch):
    found = Vm(tmp_path, monkeypatch, deploy)
    yield found
    for path in (found.fleet / "members").glob("*/worker.json"):  # no stand-in outlives its test
        found.deploy.site.stop_tagged(json.loads(path.read_text(encoding="utf-8"))["tag"], 0.0)


def wait_gone(pid: int, wait_s: float = 10.0) -> None:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return
        except OSError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} did not end")


def tick(deploy, vm: Vm, ledger: Ledger, clock) -> list:
    """One tick of the controller over the site, its identity read stood in (one campaign fingerprint)."""
    executor = vm.executor(ledger, identity=lambda site, member: (FINGERPRINT, {"core": "abc"}), clock=clock)
    hooks = deploy.control.Hooks(runner=lambda: None, clock=clock, say=lambda line: None)
    decision = deploy.control.tick(ledger, vm.tmp / "ledger.sqlite", vm.controller, [vm.config], {}, vm.transport,
                                   executor, vm.log, hooks)  # fmt: skip
    executor.wait()
    return decision.actions


def new_ledger(tmp_path: Path, clock: FakeClock) -> Ledger:
    return Ledger.create(tmp_path / "out" / "ledger.sqlite", plan(tasks=1), config(), clock=clock)


# ------------------------------------------------------------------------------------------------ the sites file
def test_a_compose_site_is_an_ssh_site_whose_static_members_never_get_an_add_or_an_idle_drain(deploy, vm):
    """A Compose site names its members by root; its settings default to the members' count (maximum, warm pool),
    so no static member is drained for being idle (a label joins once), and its read reports no node: no add."""
    site = vm.config
    assert site.remote
    assert site.compose.members == tuple((m, str(r)) for m, r in vm.roots.items())
    assert (site.settings.max_federations, site.settings.warm_pool) == (2, 2)
    view = deploy.control.observe_site(vm.transport, site, None)
    assert view.nodes_per_federation == 0
    assert view.idle_nodes == 0
    assert HOST not in repr(site)


@pytest.mark.parametrize(
    ("change", "match"),
    [('transport = "ssh"', "an ssh site with a channel"), ('engine = "docker"', "engine must be one of"),
     ('kind = "compose"', "belong to a Compose site")],
)  # fmt: skip
def test_a_compose_site_that_is_not_whole_is_refused(deploy, vm, change, match):
    text = (vm.tmp / "sites.toml").read_text(encoding="utf-8")
    replaced = {"transport": 'transport = "local"', "engine": 'engine = "lxc"', "kind": 'kind = "hpc"'}
    (vm.tmp / "sites.toml").write_text(text.replace(change, replaced[change.split(" ")[0]]), encoding="utf-8")
    with pytest.raises(deploy.control.ConfigError, match=match) as error:
        deploy.control.load_config(vm.tmp / "sites.toml")
    assert HOST not in str(error.value)


# ------------------------------------------------------------------------------------------------ join and lease
def test_two_static_members_of_one_site_join_and_each_takes_a_lease(deploy, vm, tmp_path):
    """C51: both members up and linked: one tick launches each one's worker on the host (detached, in a session of
    its own, with HOME the site's 0700 home and the member's own root and channel) and joins both, under one
    fingerprint; the ledger then issues each a lease. Their roots, projects and channels are their own."""
    clock = FakeClock(time.time())  # the host reads its own clock for a member's end
    ledger = new_ledger(tmp_path, clock)
    actions = tick(deploy, vm, ledger, clock)
    assert sorted(a.member for a in actions if type(a).__name__ == "Join") == ["fed-1", "fed-2"]
    joined = {f.label: f for f in ledger.federations()}
    assert sorted(joined) == ["site-a-fed-1", "site-a-fed-2"]
    assert joined["site-a-fed-1"].job_ids == ("compose-expcore-fed-1",)
    workers = {m: vm.worker(m) for m in vm.roots}
    assert {w["root"] for w in workers.values()} == {str(r) for r in vm.roots.values()}
    assert {w["channel"] for w in workers.values()} == {str(vm.channel / f"site-a-{m}") for m in vm.roots}
    for member, record in workers.items():
        environ = Path(f"/proc/{record['pid']}/environ").read_bytes().split(b"\0")
        assert f"HOME={vm.home}".encode() in environ
        assert f"EXPCORE_FED_ROOT={vm.roots[member]}".encode() in environ
        assert os.getsid(record["pid"]) == record["pid"]  # detached: a session of its own
    assert {leased(ledger, label).federation for label in joined} == set(joined)
    again = tick(deploy, vm, ledger, clock)  # joined: nothing launched or joined again
    assert not [a for a in again if type(a).__name__ == "Join"]
    assert {m: vm.worker(m)["pid"] for m in vm.roots} == {m: w["pid"] for m, w in workers.items()}
    assert HOST not in json.dumps(vm.log.lines)


def test_a_member_whose_project_is_not_up_does_not_join_and_its_worker_is_never_started(deploy, vm, tmp_path):
    clock = FakeClock(time.time())  # the host reads its own clock for a member's end
    ledger = new_ledger(tmp_path, clock)
    vm.up("fed-2", services=("zone", "pine"))  # birch is not running
    tick(deploy, vm, ledger, clock)
    assert [f.label for f in ledger.federations()] == ["site-a-fed-1"]
    assert not (vm.fleet / "members" / "fed-2" / "worker.json").exists()
    assert vm.view()["fed-2"].status == "down"


def test_the_engine_setting_names_the_engine_of_every_call_on_the_host(deploy, tmp_path, monkeypatch):
    """`engine = "podman"`: every engine call the site's commands make names podman, and docker is never called."""
    vm = Vm(tmp_path, monkeypatch, deploy, engine="podman")
    assert vm.view()["fed-1"].status == "up"
    calls = vm.engine_calls()
    assert calls
    assert {c["engine"] for c in calls} == {"podman"}


# ------------------------------------------------------------------------------------------------ proof of end
def test_a_worker_killed_is_proven_ended_only_once_no_process_of_it_is_left(deploy, vm, tmp_path):
    """C51: a worker killed with SIGKILL leaves its lease run behind, in a session of its own and still spending: the
    member is not proven ended while it lives. Once it is gone too, the read shows the member ended and the
    controller proves its end; the federation itself is untouched."""
    clock = FakeClock(time.time())  # the host reads its own clock for a member's end
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    record = vm.worker("fed-1")
    child = int((vm.fleet / "members" / "fed-1" / "run" / "tmp" / "child.pid").read_text())
    os.kill(record["pid"], signal.SIGKILL)
    wait_gone(record["pid"])
    assert vm.view()["fed-1"].state == "RUNNING"  # its run lives: no proof
    assert not [a for a in tick(deploy, vm, ledger, clock) if isinstance(a, ProveEnded)]
    os.kill(child, signal.SIGKILL)
    wait_gone(child)
    assert vm.view()["fed-1"].ended
    proofs = [a for a in tick(deploy, vm, ledger, clock) if isinstance(a, ProveEnded)]
    assert [(a.member, a.proof) for a in proofs] == [("fed-1", "terminal")]
    fed = {f.label: f for f in ledger.federations()}["site-a-fed-1"]
    assert fed.end_proof == "terminal"
    assert not [c for c in vm.engine_calls() if c["argv"][:1] != ["ps"]]  # only reads: nothing stopped or removed


def test_a_stopped_service_keeps_a_member_but_a_project_taken_down_under_its_worker_is_broken(deploy, vm, tmp_path):
    """A provider stopped (as a sanctioned outage stops its container) leaves a joined member as it was: never
    drained for it, since a label joins once. Its project taken down (no container left) while its worker runs is not
    proven ended (the worker may still spend): it is drained instead."""
    clock = FakeClock(time.time())
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    vm.up("fed-1", services=("zone", "pine"))
    vm.table["expcore-fed-1"].append(["expcore-fed-1-birch-1", "birch", "exited", str(vm.roots["fed-1"])])
    (vm.bin / "containers.json").write_text(json.dumps(vm.table), encoding="utf-8")
    assert vm.view()["fed-1"].state == "RUNNING"
    assert not [a for a in tick(deploy, vm, ledger, clock) if getattr(a, "member", None) == "fed-1"]
    vm.up("fed-1", services=())
    assert vm.view()["fed-1"].broken
    actions = tick(deploy, vm, ledger, clock)
    assert [type(a).__name__ for a in actions if getattr(a, "member", None) == "fed-1"] == ["Drain"]


def test_c49_a_member_whose_worker_died_settles_its_lost_lease_from_the_channel_on_the_host(
    deploy, vm, tmp_path, clean_environment
):
    """C51: a member's worker dies mid-lease and is proven ended (no process of it left): the lost lease is settled
    at the spend its place's journal shows, read over ssh from the channel on the host's disk, not left charged its
    whole grant."""
    leased_run, kept = ran_for_real(tmp_path, clean_environment, "site-a-fed-1")
    killed_mid_lease(kept, tmp_path)
    ledger, now, lease = leased_run.ledger, leased_run.now, leased_run.lease
    config = dataclasses.replace(vm.config, channel=Path(leased_run.site.root))
    evidence = tmp_path / "evidence"
    spend = deploy.execute.dead_store_reader(deploy.identity.locator(evidence, vm.transport), leased_run.loaded)
    executor = deploy.execute.Executor(ledger, tmp_path / "ledger.sqlite", {SITE: config}, vm.transport, evidence,
                                       deploy.execute.Log(None), spend=spend, clock=lambda: now[0])  # fmt: skip
    now[0] += 4000.0
    ledger.observe_site(SITE, SiteReading())
    ledger.prove_ended("site-a-fed-1", "terminal")  # the read showed no process of its worker left
    grant = ledger.lease(lease.lease_id).charge_micro
    executor.in_ledger(Settle(SITE, "fed-1", "site-a-fed-1", lease.lease_id, None, "lost"))
    (done,) = [r for r in executor.log.lines if r.get("action") == "Settle"]
    assert done["ok"] is True
    assert done["source"] == "root"
    assert ledger.lease(lease.lease_id).charge_micro == done["spend_micro"] < grant


# ------------------------------------------------------------------------------------------------ the retirement
def test_a_retirement_stops_the_worker_collects_and_pulls_and_leaves_the_project_up(deploy, vm, tmp_path):
    """C51: a drained member with no lease in flight is retired: its worker stopped and shown gone, its root's evidence
    collected (no secret in it), pulled and verified here, the member recorded retired; no engine call but reads, the
    root and its containers stay, and the member reads `down` from then on (its end proven `cancelled`)."""
    clock = FakeClock(time.time())  # the host reads its own clock for a member's end
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    record = vm.worker("fed-1")
    ledger.drain("site-a-fed-1")
    retire = [a for a in tick(deploy, vm, ledger, clock) if isinstance(a, Retire)]
    assert [(a.member, a.collect) for a in retire] == [("fed-1", True)]
    results = [line for line in vm.log.lines if line.get("action") == "Retire"]
    assert [r["ok"] for r in results] == [True] * 6, results
    assert vm.deploy.site.worker_gone(record)
    copy = tmp_path / "evidence" / "site-a-fed-1"
    assert sorted(p.name for p in copy.iterdir()) == ["archives.sha256.json", "federation.root.json",
                                                      "federation.root.tar"]  # fmt: skip
    assert SECRET.encode() not in (copy / "federation.root.tar").read_bytes()
    assert vm.roots["fed-1"].is_dir()
    assert (vm.roots["fed-1"] / "facts.json").is_file()
    assert {c["argv"][0] for c in vm.engine_calls()} == {"ps"}  # never a down, a stop or a removal
    assert vm.view()["fed-1"].status == "down"
    proofs = [a for a in tick(deploy, vm, ledger, clock) if isinstance(a, ProveEnded)]
    assert [(a.member, a.proof) for a in proofs] == [("fed-1", "cancelled")]
    assert vm.view()["fed-2"].state == "RUNNING"  # the other member is untouched


def test_the_dry_run_prints_a_compose_retirement_with_no_down(deploy, vm):
    action = Retire(SITE, "fed-1", "site-a-fed-1", str(vm.roots["fed-1"]), True, "drained")
    rendered = deploy.execute.render(action, vm.config, Path("/ev"), Path("/ledger.sqlite"))
    entry = deploy.execute.described(action, rendered)
    assert entry["places"] == ["site", "site", "pull", "pull-places", "here", "site"]
    printed = deploy.execute.printed(entry)
    assert printed.index(" stop ") < printed.index(" collect ") < printed.index("verify-copy")
    assert printed.index("verify-copy") < printed.index(" retired ")
    assert "fleet.py" not in printed
    assert " down" not in printed
    assert HOST not in printed


# ------------------------------------------------------------------------------------------------ on the host
def test_stop_ends_every_process_of_the_worker_and_keeps_its_lease_stores(deploy, vm, tmp_path):
    clock = FakeClock(time.time())  # the host reads its own clock for a member's end
    tick(deploy, vm, new_ledger(tmp_path, clock), clock)
    record = vm.worker("fed-2")
    code, _, err = vm.transport.run(vm.config, deploy.compose.stop_argv(vm.config, "fed-2"), 60.0)
    assert (code, err) == (0, "")
    assert deploy.site.worker_gone(record)
    assert deploy.site.tagged(record["tag"], record["run"]) == []
    assert (vm.fleet / "members" / "fed-2" / "run" / "work").is_dir()  # a lease store there may be the only one


def test_a_container_of_the_project_bound_to_another_root_makes_the_site_dark(deploy, vm):
    """Another federation of the same project name: the read fails as a whole, never a member taken for it."""
    vm.table["expcore-fed-1"].append(["stranger", "pine", "running", "/elsewhere"])
    (vm.bin / "containers.json").write_text(json.dumps(vm.table), encoding="utf-8")
    view = deploy.control.observe_site(vm.transport, vm.config, None)
    assert view.error
    assert HOST not in view.error


def test_a_worker_never_starts_without_a_private_home(deploy, vm, tmp_path):
    vm.home.chmod(0o755)
    clock = FakeClock(time.time())  # the host reads its own clock for a member's end
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    assert ledger.federations() == []  # the launch failed: the join waits
    assert not (vm.fleet / "members" / "fed-1" / "worker.json").exists()
    assert [r["ok"] for r in vm.log.lines if r.get("action") == "Worker"] == [False, False]


# ------------------------------------------------------------------------------------------------ rejoin
def retire_dead_worker(deploy, vm: Vm, ledger: Ledger, clock, member: str = "fed-1") -> None:
    """The member's worker and its run killed: its end is proven, and the member retired."""
    record = vm.worker(member)
    child = int((vm.fleet / "members" / member / "run" / "tmp" / "child.pid").read_text())
    for pid in (record["pid"], child):
        os.kill(pid, signal.SIGKILL)
        wait_gone(pid)
    assert [a.proof for a in tick(deploy, vm, ledger, clock) if isinstance(a, ProveEnded)] == ["terminal"]
    assert [a.member for a in tick(deploy, vm, ledger, clock) if isinstance(a, Retire)] == [member]


def test_c51_a_member_whose_worker_died_joins_again_as_its_next_generation(deploy, vm, tmp_path):
    """C51: on a dedicated host a dead worker never loses the federation for the rest of the campaign: once the member
    is retired and its root reads live and linked again, it joins as `<member>-g2`, a new ledger label with its own
    worker, channel and copy."""
    clock = FakeClock(time.time())
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    retire_dead_worker(deploy, vm, ledger, clock)
    joins = [a for a in tick(deploy, vm, ledger, clock) if type(a).__name__ == "Join"]
    assert [a.label for a in joins] == ["site-a-fed-1-g2"]
    assert vm.worker("fed-1-g2")["channel"] == str(vm.channel / "site-a-fed-1-g2")
    assert vm.worker("fed-1-g2")["root"] == str(vm.roots["fed-1"])
    feds = {f.label: f for f in ledger.federations()}
    assert feds["site-a-fed-1"].end_proof == "terminal"
    assert feds["site-a-fed-1-g2"].ended_at is None
    assert {m: v.status for m, v in vm.view().items()} == {"fed-1": "down", "fed-1-g2": "up", "fed-2": "up"}


def test_c51_a_new_member_naming_a_retired_members_root_joins_cleanly(deploy, vm, tmp_path):
    """C51: the operator's path: a new member key naming the same root, listed after the first, reads down while the
    first holds the root, and joins as its own ledger label (its own channel and collect directory) once the first is
    retired; the first, no longer the root's last, starts no generation of its own."""
    clock = FakeClock(time.time())
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    vm.sites({"fed-1": vm.roots["fed-1"], "fed-1b": vm.roots["fed-1"], "fed-2": vm.roots["fed-2"]})
    assert vm.view()["fed-1b"].status == "down"
    assert not [a for a in tick(deploy, vm, ledger, clock) if getattr(a, "member", None) == "fed-1b"]
    retire_dead_worker(deploy, vm, ledger, clock)
    joins = [a for a in tick(deploy, vm, ledger, clock) if type(a).__name__ == "Join"]
    assert [a.label for a in joins] == ["site-a-fed-1b"]
    assert vm.worker("fed-1b")["channel"] == str(vm.channel / "site-a-fed-1b")
    assert "fed-1-g2" not in vm.view()
    assert (vm.fleet / "collected" / "fed-1").is_dir()
    assert not (vm.fleet / "collected" / "fed-1b").exists()


@pytest.mark.parametrize(
    ("label", "members", "match"),
    [(SITE, {"fed-1": "/r/1", "fed-1-g2": "/r/2"}, "later generation"),
     ("s" * 30, {"f" * 35: "/r/1"}, "64 characters")],
)  # fmt: skip
def test_member_labels_that_meet_a_generation_or_the_ledger_limit_are_refused(deploy, label, members, match):
    with pytest.raises(ValueError, match=match):
        deploy.compose.compose_site(label, {"members": members, "study": "/s", "home": "/h"})


# ------------------------------------------------------------------------------------------------ fail closed
HIDDEN = """
import ctypes, sys, time
ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: its environment is no longer readable to this user
time.sleep(60)
"""


def test_a_lease_run_whose_environment_cannot_be_read_is_still_found_by_its_run_directory(deploy, tmp_path):
    """A process of the worker's whose environment cannot be read (not dumpable), or was replaced, is still the
    worker's by its command line, which names the worker's run directory: no proof of the end while it lives."""
    run = tmp_path / "members" / "fed-1" / "run"
    hidden = subprocess.Popen([sys.executable, "-c", HIDDEN, str(run / "work" / "lease-000000000001" / "study")],
                              env={"PATH": os.environ["PATH"]})  # fmt: skip
    reader = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", f"{run}/work/lease-1.log"],
        env={"PATH": os.environ["PATH"]},
    )  # fmt: skip  (a reader of a file below the run)
    try:
        record = {"tag": "its-tag", "pid": None, "start": None, "run": str(run)}
        deadline = time.monotonic() + 10
        while hidden.pid not in deploy.site.tagged("its-tag", str(run)) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hidden.pid in deploy.site.tagged("its-tag", str(run))
        assert not deploy.site.worker_gone(record)
        assert hidden.pid not in deploy.site.tagged("its-tag", str(tmp_path / "another-run"))
        assert reader.pid not in deploy.site.tagged("its-tag", str(run))  # never stopped as the worker's
    finally:
        for process in (hidden, reader):
            process.kill()
            process.wait()
    assert deploy.site.worker_gone(record)


def test_a_key_home_inside_the_study_is_refused_and_nothing_is_copied(deploy, vm, tmp_path):
    inside = vm.study / "keys"
    inside.mkdir(mode=0o700)
    vm.config = dataclasses.replace(vm.config, compose=dataclasses.replace(vm.config.compose, home=str(inside)))
    clock = FakeClock(time.time())
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    assert ledger.federations() == []
    assert not (vm.fleet / "members" / "fed-1" / "run").exists()
    code, _, err = vm.transport.run(vm.config, deploy.compose.identity_argv(vm.config, "fed-1"), 60.0)
    assert code != 0
    assert "overlaps" in err


def test_a_retry_of_collect_never_takes_another_roots_copy_for_this_ones(deploy, vm):
    into = vm.fleet / "collected" / "fed-1"
    argv = deploy.compose.collect_argv(vm.config, str(vm.roots["fed-1"]), str(into))
    code, out, err = vm.transport.run(vm.config, argv, 60.0)
    assert (code, err) == (0, "")
    assert vm.transport.run(vm.config, argv, 60.0)[1] == out  # a retry: the same copy, the same hash
    other = deploy.compose.collect_argv(vm.config, str(vm.roots["fed-2"]), str(into))
    code, _, err = vm.transport.run(vm.config, other, 60.0)
    assert code != 0
    assert "not this root's whole copy" in err


def test_a_member_never_launches_on_a_root_whose_other_members_worker_still_runs(deploy, vm, tmp_path):
    """One worker per federation, whatever the sites file names now: a sites file rewritten with only a new member
    for a root whose earlier member's worker runs gets no second worker there (the join waits)."""
    clock = FakeClock(time.time())
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    vm.sites({"fed-1b": vm.roots["fed-1"], "fed-2": vm.roots["fed-2"]})
    tick(deploy, vm, ledger, clock)
    assert "site-a-fed-1b" not in {f.label for f in ledger.federations()}
    assert not (vm.fleet / "members" / "fed-1b" / "worker.json").exists()
    assert [r["ok"] for r in vm.log.lines if r.get("action") == "Worker"] == [False]


def test_site_labels_one_of_which_begins_with_another_and_a_dash_are_refused(deploy, vm):
    """`a-b`'s member `c` and `a`'s member `b-c` would be one ledger label: such sites never share a sites file."""
    text = (vm.tmp / "sites.toml").read_text(encoding="utf-8")
    second = text[text.index("[[site]]") :].replace(f'label = "{SITE}"', f'label = "{SITE}-x"')
    second = second.replace(str(vm.fleet), str(vm.fleet) + "-x")
    (vm.tmp / "sites.toml").write_text(text + second, encoding="utf-8")
    with pytest.raises(deploy.control.ConfigError, match="begin with another site's label"):
        deploy.control.load_config(vm.tmp / "sites.toml")


# ------------------------------------------------------------------------------------------------ a site's ssh config
def test_a_sites_own_ssh_config_file_is_passed_with_host_key_checking_forced_after_it(deploy, vm, tmp_path):
    """`ssh_config` (a private file of this host's: the site's alias, key, known hosts, a jump host): every call goes
    `ssh -F <file> -o StrictHostKeyChecking=yes -o BatchMode=yes ...`, so no file can weaken host-key checking."""
    from expcore.campaign.elastic.transport import SSH_OPTIONS, ssh_argv

    config = tmp_path / "ssh.config"
    config.write_text(f"Host {HOST}\n  IdentitiesOnly yes\n", encoding="utf-8")
    config.chmod(0o600)
    text = (vm.tmp / "sites.toml").read_text(encoding="utf-8")
    (vm.tmp / "sites.toml").write_text(text + f'ssh_config = "{config}"\n', encoding="utf-8")
    _, (site,) = deploy.control.load_config(vm.tmp / "sites.toml")
    argv = ssh_argv(HOST, site.ssh_config)
    assert argv == ("ssh", "-F", str(config), "-o", "StrictHostKeyChecking=yes", *SSH_OPTIONS, "--", HOST)
    assert deploy.control.observe_site(vm.transport, site, None).error is None
    (call,) = [c for c in vm.fake.calls() if "observe" in c["command"][0]]
    assert call["config"] == str(config)
    assert call["options"][:2] == ["StrictHostKeyChecking=yes", "BatchMode=yes"]


@pytest.mark.skipif(shutil.which("ssh") is None, reason="no ssh client to read its effective configuration")
def test_the_command_lines_host_key_checking_wins_over_a_config_file_that_weakens_it(tmp_path):
    """ssh's own resolution (`ssh -G`, which connects nowhere): a file saying `StrictHostKeyChecking no` and
    `BatchMode no` still gives yes and yes, since the command line's options are read first."""
    from expcore.campaign.elastic.transport import ssh_argv

    config = tmp_path / "weak.config"
    config.write_text("Host *\n  StrictHostKeyChecking no\n  BatchMode no\n", encoding="utf-8")
    config.chmod(0o600)
    argv = list(ssh_argv("vm-alias.invalid", str(config)))
    done = subprocess.run([argv[0], "-G", *argv[1:]], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    effective = dict(line.split(" ", 1) for line in done.stdout.splitlines() if " " in line)
    assert (effective["stricthostkeychecking"], effective["batchmode"]) == ("true", "yes")


@pytest.mark.parametrize("trouble", ["missing", "world-readable", "link"])
def test_an_ssh_config_that_is_not_a_private_file_is_refused(deploy, vm, tmp_path, trouble):
    config = tmp_path / "ssh.config"
    if trouble != "missing":
        real = tmp_path / "real.config"
        real.write_text("Host x\n", encoding="utf-8")
        real.chmod(0o644 if trouble == "world-readable" else 0o600)
        config = real if trouble == "world-readable" else config
        if trouble == "link":
            config.symlink_to(real)
    text = (vm.tmp / "sites.toml").read_text(encoding="utf-8")
    (vm.tmp / "sites.toml").write_text(text + f'ssh_config = "{config}"\n', encoding="utf-8")
    with pytest.raises(deploy.control.ConfigError, match="ssh_config"):
        deploy.control.load_config(vm.tmp / "sites.toml")


def test_c51_a_drained_member_stays_out_until_its_operator_undrains_it(deploy, vm, tmp_path):
    """C51: a member drained (an operator taking a VM federation out for maintenance, say) is retired and stays out:
    no later generation joins by itself, though its root reads live and linked. The operator's `undrain` brings it
    back, as its next generation (a member whose worker died rejoins with no such step: see above)."""
    clock = FakeClock(time.time())
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    ledger.drain("site-a-fed-1")
    assert [a.drained for a in tick(deploy, vm, ledger, clock) if isinstance(a, Retire)] == [True]
    for _ in range(2):
        assert not [a for a in tick(deploy, vm, ledger, clock) if type(a).__name__ == "Join"]
    assert set(vm.view()) == {"fed-1", "fed-2"}
    assert vm.view()["fed-1"].status == "down"
    undrain = deploy.compose.script(vm.config, "undrain", "--fleet", str(vm.fleet), "--member", "fed-1")
    code, _, err = vm.transport.run(vm.config, undrain, 60.0)
    assert (code, err) == (0, "")
    joins = [a for a in tick(deploy, vm, ledger, clock) if type(a).__name__ == "Join"]
    assert [a.label for a in joins] == ["site-a-fed-1-g2"]


def good_marker(manifest: Path) -> dict:
    """The marker `verify-data` writes for the declared manifest as it is now: format /2, both digests."""
    from expcore_onedata.importmanifest import load_manifest

    entry = {"space_id": "sid-alpha", "manifest_sha256": load_manifest(manifest).digest(),
             "manifest_file_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}  # fmt: skip
    return {"format": "expcore-compose-data-verified/2", "spaces": {"alpha": entry}}


def test_c51_a_member_whose_platform_declares_data_is_verified_only_by_a_marker_of_that_manifest(deploy, vm, tmp_path):
    """C51: verified is never just linked when the member's platform file declares a space's data (a data manifest),
    or a federation with missing or other data would join and score runs. Without a marker it reads unverified and
    never joins; a marker naming the space's id, its manifest's core digest and its file's bytes' digest as it is now
    verifies it, exactly as an HPC member's verify-data marker."""
    root = vm.roots["fed-1"]
    manifest = declare_data(vm)
    clock = FakeClock(time.time())
    ledger = new_ledger(tmp_path, clock)
    assert not vm.view()["fed-1"].verified
    tick(deploy, vm, ledger, clock)
    assert [f.label for f in ledger.federations()] == ["site-a-fed-2"]  # fed-1 waits: its data is not shown
    (root / "data-verified.json").write_text(json.dumps(good_marker(manifest)), encoding="utf-8")
    assert vm.view()["fed-1"].verified
    tick(deploy, vm, ledger, clock)
    assert "site-a-fed-1" in {f.label for f in ledger.federations()}


@pytest.mark.parametrize(
    "wrong",
    ["format /1", "manifest_sha256", "manifest_file_sha256", "space_id", "the file rewritten in another layout"],
)
def test_c52_a_marker_is_no_marker_when_any_field_is_not_the_files_now(deploy, vm, wrong):
    """C52: each field the marker holds must be the declared manifest's as it is now: a marker of the earlier format
    (/1), another core digest, another file digest, another space id, and a manifest file rewritten with the same
    entries in another byte layout (its core digest the same, its file digest not) each read unverified."""
    root = vm.roots["fed-1"]
    manifest = declare_data(vm)
    marker = good_marker(manifest)
    entry = marker["spaces"]["alpha"]
    if wrong == "format /1":
        marker["format"] = "expcore-compose-data-verified/1"
    elif wrong == "the file rewritten in another layout":
        manifest.write_text(json.dumps(json.loads(manifest.read_text(encoding="utf-8")), indent=4), encoding="utf-8")
        assert good_marker(manifest)["spaces"]["alpha"]["manifest_sha256"] == entry["manifest_sha256"]
    else:
        entry[wrong] = "0" * 64 if wrong != "space_id" else "sid-other"
    (root / "data-verified.json").write_text(json.dumps(marker), encoding="utf-8")
    assert not deploy.site.data_verified(root)


# ------------------------------------------------------------------------------------------------ declared data
def declare_data(vm: Vm, member: str = "fed-1") -> Path:
    """The member's platform file declares its study space's data: a manifest of one file, `data.bin`."""
    from expcore_onedata.importmanifest import Manifest, ManifestFile, write_manifest

    root = vm.roots[member]
    manifest = root / "data-manifests" / "alpha.json"
    manifest.parent.mkdir(exist_ok=True)
    write_manifest(Manifest((ManifestFile("data.bin", 4, hashlib.sha256(b"data").hexdigest()),), ()), manifest)
    (root / "platform.toml").write_text(
        f'[[spaces]]\nname = "alpha"\nid = "sid-alpha"\ndata_manifest = "{manifest}"\n', encoding="utf-8"
    )
    facts = json.loads((root / "facts.json").read_text(encoding="utf-8"))
    facts["spaces"] = {"alpha": {"id": "sid-alpha"}}
    (root / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
    return manifest


def test_c52_verify_data_writes_the_marker_only_for_data_that_is_exactly_its_manifest(deploy, vm):
    """C52: `verify-data` reads every declared space back (here a stand-in for the owner's REST reader) and writes the
    marker the member joins on only when each is exactly its manifest; a space with a file of other bytes refuses,
    naming the space, and removes the marker an earlier check wrote, so a re-ingest that went wrong never keeps it."""
    import fedverify

    manifest = declare_data(vm)
    layout = SimpleNamespace(root=vm.roots["fed-1"])
    whole = ({"data.bin": (4, hashlib.sha256(b"data").hexdigest())}, set())
    assert "1 space(s) hold exactly their manifests" in fedverify.verify_data(layout, read=lambda space: whole)
    written = json.loads((vm.roots["fed-1"] / "data-verified.json").read_text(encoding="utf-8"))
    assert written == good_marker(manifest)  # format /2: the core digest and the file's bytes' digest
    assert deploy.site.data_verified(vm.roots["fed-1"])
    other = ({"data.bin": (4, hashlib.sha256(b"dat!").hexdigest())}, set())
    with pytest.raises(RuntimeError, match="alpha does not hold the data its manifest declares"):
        fedverify.verify_data(layout, read=lambda space: other)
    assert not (vm.roots["fed-1"] / "data-verified.json").exists()
    assert not deploy.site.data_verified(vm.roots["fed-1"])
    fedverify.verify_data(layout, read=lambda space: whole)
    fedverify.invalidate(vm.roots["fed-1"])  # what reset --clean does before it empties the spaces
    assert not deploy.site.data_verified(vm.roots["fed-1"])


def test_c52_a_manifest_named_by_a_relative_path_is_never_verified(deploy, vm):
    """C52: a relative data_manifest resolves against the root for the check and against the study beside the
    platform file's copy for a run, so the two could be two manifests: it is refused, and the member never verified."""
    import fedverify

    manifest = declare_data(vm)
    root = vm.roots["fed-1"]
    platform = (root / "platform.toml").read_text(encoding="utf-8").replace(str(manifest), "data-manifests/alpha.json")
    (root / "platform.toml").write_text(platform, encoding="utf-8")
    whole = ({"data.bin": (4, hashlib.sha256(b"data").hexdigest())}, set())
    with pytest.raises(RuntimeError, match="relative path"):
        fedverify.verify_data(SimpleNamespace(root=root), read=lambda space: whole)
    assert not deploy.site.data_verified(root)


PREPARE = """
import hashlib, json, sys
from expcore_onedata.importmanifest import load_manifest
root = sys.argv[1]
path = root + "/data-manifests/alpha.json"
marker = {"format": "expcore-compose-data-verified/2", "spaces": {"alpha": {"space_id": "sid-alpha",
          "manifest_sha256": load_manifest(path).digest(),
          "manifest_file_sha256": hashlib.sha256(open(path, "rb").read()).hexdigest()}}}
open(root + "/data-verified.json", "w").write(json.dumps(marker))
open(root + "/prepare-saw-layout", "w").write(sys.argv[2])
"""


def test_c52_a_compose_member_with_declared_data_joins_after_its_prepare_verifies_it(deploy, vm, tmp_path):
    """C52: a Compose site's `prepare` (#494's hook) runs once, on the host, for a member linked with its declared data
    unverified, with `{root}` its root and `{layout}` the site's layout file; the member joins only once the marker its
    command leaves (the study's ingest, then `composesite.py verify-data`; here a stand-in) verifies it."""
    declare_data(vm)
    real = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "fleetprepare.py"
    (vm.base / "hpc-federation").mkdir()
    (vm.base / "hpc-federation" / "fleetprepare.py").write_text(
        f"import runpy, sys\nsys.argv[0] = {str(real)!r}\nrunpy.run_path({str(real)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    (vm.base / "prepare.py").write_text(PREPARE, encoding="utf-8")
    layout = vm.base / "layout.toml"
    layout.write_text("# the members' layout\n", encoding="utf-8")
    text = (vm.tmp / "sites.toml").read_text(encoding="utf-8")
    command = f'prepare = ["{sys.executable}", "{vm.base / "prepare.py"}", "{{root}}", "{{layout}}"]'
    (vm.tmp / "sites.toml").write_text(text + f'{command}\nprepare_timeout_s = 120\nlayout = "{layout}"\n', "utf-8")
    vm.controller, (vm.config,) = deploy.control.load_config(vm.tmp / "sites.toml")
    clock = FakeClock(time.time())
    ledger = new_ledger(tmp_path, clock)
    actions = tick(deploy, vm, ledger, clock)
    assert [a.member for a in actions if type(a).__name__ == "Prepare"] == ["fed-1"]
    assert [f.label for f in ledger.federations()] == ["site-a-fed-2"]  # fed-1 waits for its data
    assert (vm.roots["fed-1"] / "prepare-saw-layout").read_text(encoding="utf-8") == str(layout)
    assert vm.view()["fed-1"].verified
    tick(deploy, vm, ledger, clock)
    assert "site-a-fed-1" in {f.label for f in ledger.federations()}
    assert not [a for a in tick(deploy, vm, ledger, clock) if type(a).__name__ == "Prepare"]  # once
