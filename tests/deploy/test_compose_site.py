"""A Compose site of the elastic fleet (`deploy/compose-federation/composesite.py`, `deploy/hpc-federation/
fleetcompose.py`): a host whose federations the operator brought up, its static members, driven over ssh.

The host is this one: every site command goes over the fake `ssh` (`fake_ssh_bin`) to the real `composesite.py`
behind a stub that swaps the elastic worker for a stand-in (a process that starts a "lease run" in a session of its own,
as the worker's `ProcessRunner` does) and shortens the launch's settle. The container engine is a fake on the host's
PATH that answers `ps` from a table of containers per project and logs every call. Nothing reaches Docker."""

import dataclasses
import json
import os
import signal
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
        sites = self.tmp / "sites.toml"
        members_line = ", ".join(f'{m} = "{r}"' for m, r in self.roots.items())
        sites.write_text("\n".join([
            "[controller]", f'evidence = "{tmp_path / "evidence"}"', f'archives = "{tmp_path / "archives"}"',
            "[[site]]", f'label = "{SITE}"', 'kind = "compose"', 'transport = "ssh"', f'host = "{HOST}"',
            f'interpreter = "{sys.executable}"', f'deploy = "{self.scripts}"', f'env_file = "{self.env_file}"',
            f'fleet = "{self.fleet}"', f'channel = "{self.channel}"', f'study = "{self.study}"',
            f'home = "{self.home}"', f'engine = "{engine}"', f"members = {{ {members_line} }}",
        ]) + "\n", encoding="utf-8")  # fmt: skip
        self.controller, (self.config,) = deploy.control.load_config(sites)
        self.transport = deploy.execute.SiteTransport(dict(os.environ))

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
    for member in found.roots:  # no stand-in outlives its test
        path = found.fleet / "members" / member / "worker.json"
        if path.exists():
            found.deploy.site.stop_tagged(found.worker(member)["tag"], 0.0)


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
    """C49: both members up and linked: one tick launches each one's worker on the host (detached, in a session of
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
    """C49: a worker killed with SIGKILL leaves its lease run behind, in a session of its own and still spending: the
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


def test_a_live_worker_on_a_project_that_went_down_is_broken_never_ended(deploy, vm, tmp_path):
    """The project down while the worker runs: not proven (the worker may still spend), drained instead."""
    clock = FakeClock(time.time())  # the host reads its own clock for a member's end
    ledger = new_ledger(tmp_path, clock)
    tick(deploy, vm, ledger, clock)
    vm.up("fed-1", state="exited")
    assert vm.view()["fed-1"].broken
    actions = tick(deploy, vm, ledger, clock)
    assert [type(a).__name__ for a in actions if getattr(a, "member", None) == "fed-1"] == ["Drain"]


def test_c49_a_member_whose_worker_died_settles_its_lost_lease_from_the_channel_on_the_host(
    deploy, vm, tmp_path, clean_environment
):
    """C49: a member's worker dies mid-lease and is proven ended (no process of it left): the lost lease is settled
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
    """C49: a drained member with no lease in flight is retired: its worker stopped and shown gone, its root's evidence
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
def test_stop_ends_every_process_of_the_worker_and_removes_its_directory(deploy, vm, tmp_path):
    clock = FakeClock(time.time())  # the host reads its own clock for a member's end
    tick(deploy, vm, new_ledger(tmp_path, clock), clock)
    record = vm.worker("fed-2")
    code, _, err = vm.transport.run(vm.config, deploy.compose.stop_argv(vm.config, "fed-2"), 60.0)
    assert (code, err) == (0, "")
    assert deploy.site.worker_gone(record)
    assert not deploy.site.tagged(record["tag"])
    assert not (vm.fleet / "members" / "fed-2" / "run").exists()


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
