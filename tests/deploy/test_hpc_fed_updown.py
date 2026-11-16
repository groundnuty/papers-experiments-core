"""The HPC federation driver's up and down (deploy/hpc-federation/hpcfed.py, DESIGN.md sections 1 to 7), against the
fake Slurm of hpc_fed_support: the layout drives the services, their nodes, peers and steps; the state file records
what was placed; down finds and stops all of it, after a whole up or a partial one, cancelling only the held job's
id; the core's phase writes platform.toml and the link check's record in the root; the service verbs' hook."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, hpc_on_path

from expcore_onedata.linkcheck import LinkReport, PairResult
from expcore_onedata.linksettings import link_pairs

PROVIDERS = ["aspen", "larch", "rowan", "alder", "hazel"]


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, "SH": str(images), "L": str(logs)}


class Rig:
    """A driver over the fake Slurm, the core's phase recorded instead of run."""

    def __init__(self, hpc, tmp_path, site, layout=LAYOUT, fake=None):
        self.hpc = hpc
        self.fake = fake or FakeSlurm()
        self.root = tmp_path / "fed" / "root"
        self.site = site
        self.driver = hpc.cli.make_driver(layout, str(self.root), runner=self.fake)
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        self.core_calls = []
        self.driver.core = self.core
        self.driver.relay_available = lambda: False  # a test of the relay says so; M4's module may be importable

    def core(self, layout, owner, values, environ=None):
        self.core_calls.append((layout, owner, values))
        return "core: the link check passed"

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def state(self):
        return self.driver.store.read()


def test_up_places_the_zone_and_each_provider_on_its_own_node_in_the_layouts_order(hpc, tmp_path, site):
    """O57: one held job; the zone on its first node, each provider on the next, in the layout's order; every
    service's node, FQDN, address and work directory recorded in the state file, which holds no secret."""
    rig = Rig(hpc, tmp_path, site)
    assert rig.up().startswith("core: the link check passed")
    state = rig.state()
    assert state["allocation"]["job"] == "4242"
    assert state["phase"] == "linked"
    assert state["roles"] == ["zone", *PROVIDERS]
    for index, name in enumerate(["zone", *PROVIDERS]):
        record = state["services"][name]
        assert record["node"] == f"node{index + 1:02d}"
        assert record["state"] == "running"
        assert record["address"] == f"10.0.0.{index + 1}"
        assert record["fqdn"].startswith(record["node"])
        assert record["workdir"].startswith("/tmp/hpcop-o")
        assert record["rootfs"] == record["workdir"] + "/rootfs"
    text = (rig.root / "hpc-federation.json").read_text(encoding="utf-8")
    assert "A1b2C3d4E5f6G7h8I9j0K1l2" not in text  # the zone's passphrase is in secrets/, never in the state


def test_the_held_job_is_shaped_by_the_layout_and_read_back_before_anything_runs(hpc, tmp_path, site):
    """O57: the held job is shaped by the layout and read back before anything runs."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    asked = rig.fake.asked
    assert (asked["--nodes"], asked["--cpus-per-task"], asked["--mem"], asked["--time"]) == (
        "6",
        "5",
        "11264M",
        "02:00:00",
    )
    order = [c[0] if c[0] != "scontrol" else " ".join(c[:3]) for c in rig.fake.calls]
    assert order.index("sbatch") < order.index("scontrol show job") < order.index("srun")
    assert rig.state()["allocation"]["readback"]["Requeue"] == "0"


def test_every_service_gets_every_peer_address_before_it_starts(hpc, tmp_path, site):
    """O57: the shim reads its peers once, so the zone's step names every provider's address, and each provider's the
    zone's FQDN and address and every other provider's address."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    zone, *providers = rig.fake.spawned
    assert zone.command[1].endswith("bundle/zone.sbatch")
    assert zone.command[5:] == [f"10.0.0.{i}" for i in range(2, 7)]
    for index, step in enumerate(providers):
        assert step.command[1].endswith("bundle/provider.sbatch")
        assert step.command[4] == PROVIDERS[index]
        assert step.command[5:8] == ["node01.cluster.example.org", "admin@example.org", "10.0.0.1"]
        assert step.command[8:] == [f"10.0.0.{i}" for i in range(2, 7) if i != index + 2]


def test_service_steps_and_control_steps_are_exact_and_sized_by_the_layout(hpc, tmp_path, site):
    """O57: no step overlaps another's CPUs: services take their step's size, every control step the one control core
    and its memory, all behind the same allowlist."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    assert all(s.exact and not s.overlap for s in rig.fake.sruns)
    sizes = {(s.cpus, s.mem) for s in rig.fake.spawned[1:]}
    assert (rig.fake.spawned[0].cpus, rig.fake.spawned[0].mem) == (4, "8192M")
    assert sizes == {(4, "10240M")}
    controls = [s for s in rig.fake.sruns if s not in rig.fake.spawned]
    assert controls
    assert {(s.cpus, s.mem) for s in controls} == {(1, "1024M")}
    assert {tuple(s.env) for s in rig.fake.sruns} == {("PATH=/usr/bin:/bin", "LC_ALL=C", "TMPDIR=/tmp")}


def test_each_provider_is_registered_by_a_token_piped_from_the_zone_before_its_verdict(hpc, tmp_path, site):
    """O57: each provider is registered by a token piped from the zone before its verdict."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    assert rig.fake.delivered == PROVIDERS
    mints = [s for s in rig.fake.sruns if s.command and s.command[1].endswith("mint-registration-token.sh")]
    assert {s.node for s in mints} == {"node01"}
    assert all(s.command[s.command.index("--workdir") + 1] == "/tmp/hpcop-oz.abcdef" for s in mints)


def test_the_zones_passphrase_and_ca_reach_the_root_and_the_core_runs_over_the_real_addresses(hpc, tmp_path, site):
    """O57: the root gets the zone's passphrase (0600, as the Compose root holds it) and its CA; the core's phase
    runs over https://<node FQDN>:8443, with the job's uid as the storage's owner, this driver's argv prefix and the
    settle time derived from the layout."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    secret = rig.root / "secrets" / "zone.passphrase"
    assert oct(secret.stat().st_mode & 0o777) == "0o600"
    assert (rig.root / "ca" / "federation-ca.pem").read_text(encoding="utf-8").startswith("-----BEGIN CERTIFICATE")
    (layout, owner, values) = rig.core_calls[0]
    assert layout.spec.zone.url == "https://node01.cluster.example.org:8443"
    assert layout.spec.provider("hazel").url == "https://node06.cluster.example.org:8443"
    assert owner == (5001, 5001)
    assert (values["hpc_stop_bound_s"], values["hpc_settle_timeout_s"]) == ("690", "750")
    command = json.loads(values["hpc_command"])
    assert command[1] == "-B"
    assert command[2:] == [str(Path(rig.hpc.cli.__file__).resolve()), "--layout", str(LAYOUT), "--root", str(rig.root)]


def test_down_tears_down_the_providers_then_the_zone_checks_each_node_and_cancels_only_its_job(hpc, tmp_path, site):
    """O57: down finds everything up placed: teardown.sh on each service's own node, the providers first and the zone
    last, each node checked clean, then the held job cancelled by its id (never by user or name), the srun clients
    ended, and the root removed."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    rig.fake.sruns.clear()
    out = rig.driver.down()
    teardowns = [s for s in rig.fake.sruns if s.command[1:2] and s.command[1].endswith("teardown.sh")]
    assert [s.command[s.command.index("--workdir") + 1].split(".")[0] for s in teardowns][-1] == "/tmp/hpcop-oz"
    assert [s.node for s in teardowns] == [f"node{i:02d}" for i in (2, 3, 4, 5, 6, 1)]
    assert all(s.command[-2:] == ["--timeout", "600"] for s in teardowns)
    checks = [s for s in rig.fake.sruns if s.command[0] == "test"]
    assert len(checks) == 6
    assert all(s.command[1:3] == ["!", "-e"] for s in checks)
    assert rig.fake.cancelled == ["4242"]
    assert len(rig.fake.ended_clients) == 6
    assert not rig.root.exists()
    assert "is gone" in out


def test_a_held_job_recorded_otherwise_is_cancelled_and_down_after_it_removes_the_root(hpc, tmp_path, site):
    """O57: a partial up: the read-back refuses the job and cancels it; down finds the job gone and removes the root
    without a second cancel."""
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(recorded={"MinMemoryNode": "64G"}))
    with pytest.raises(hpc.slurm.SlurmError, match="MinMemoryNode"):
        rig.up()
    assert rig.fake.cancelled == ["4242"]
    rig.driver.down()
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_a_job_whose_id_never_reached_the_state_is_found_by_its_allocation_name(hpc, tmp_path, site):
    """O57: up records the allocation's name (holding a random part) before sbatch; when the id never reached the
    state, down finds the job by that exact name among this user's jobs and cancels that id."""
    rig = Rig(hpc, tmp_path, site)
    real = rig.fake.run

    def garbled(argv, timeout):
        done = real(argv, timeout)
        return done._replace(stdout="Submitted.\n") if argv[0] == "sbatch" else done

    rig.fake.run = garbled
    with pytest.raises(hpc.slurm.SlurmError, match="no job id"):
        rig.up()
    assert rig.state()["allocation"]["job"] is None
    rig.driver.down()
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()
    squeue = next(c for c in rig.fake.calls if c[0] == "squeue" and "-n" in c)
    assert squeue[squeue.index("-n") + 1].startswith("hpcop-expcore-hpc-pilot-")
    assert "-u" in squeue


def test_down_after_a_zone_that_failed_finds_its_work_directory_in_its_log(hpc, tmp_path, site):
    """O57: up failed at the zone's verdict, before its work directory was recorded; down reads it from the zone's log
    and tears the zone down on its node; the providers, never launched, need nothing."""
    fake = FakeSlurm()
    real = fake.spawn

    def failing(argv, client_log):
        client = real(argv, client_log)
        log = Path(fake.sruns[-1].output)
        log.write_text("hpcop: work directory /tmp/hpcop-oz.abcdef\nFINISH FAILED: service stopped\n", encoding="utf-8")
        return client

    fake.spawn = failing
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(RuntimeError, match="zone failed to start"):
        rig.up()
    assert rig.state()["services"]["zone"]["workdir"] is None
    rig.fake.sruns.clear()
    rig.driver.down()
    teardowns = [s for s in rig.fake.sruns if s.command[1:2] and s.command[1].endswith("teardown.sh")]
    assert [(s.node, s.command[2]) for s in teardowns] == [("node01", "oz")]
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_a_teardown_that_fails_keeps_the_root_and_its_state_and_still_cancels_the_job(hpc, tmp_path, site):
    """O57: what down cannot verify it says, keeping the root and its state for a second down; the job is cancelled
    all the same, so nothing keeps billing."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    rig.fake.teardown_rc = 1
    with pytest.raises(RuntimeError, match="could not be verified clean"):
        rig.driver.down()
    assert rig.fake.cancelled[-1] == "4242"
    assert rig.root.exists()
    rig.fake.state = "GONE"
    rig.driver.down()
    assert not rig.root.exists()


def test_up_refuses_a_root_that_holds_a_state_file(hpc, tmp_path, site):
    """O57: up refuses a root that holds a state file."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    with pytest.raises(RuntimeError, match="run down first"):
        rig.up()


def test_a_step_on_a_node_outside_the_held_job_is_refused(hpc, tmp_path, site):
    """O57: the launcher runs steps of the held job, on its own nodes only."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    with pytest.raises(hpc.state.StateError, match="not one of the held job's"):
        rig.driver.launcher.control("node99", ["true"], name="hpcop-x-check", timeout_s=10)


# -- the core's phase -------------------------------------------------------------------------------------------


def edited_layout(folder: Path, old: str, new: str) -> Path:
    """The pilot-shape layout with one edit, beside a copy of its platform template."""
    folder.mkdir()
    text = LAYOUT.read_text(encoding="utf-8")
    assert old in text
    (folder / LAYOUT.name).write_text(text.replace(old, new), encoding="utf-8")
    template = LAYOUT.parent / "pilot-shape.platform.toml.template"
    (folder / template.name).write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return folder / LAYOUT.name


@pytest.fixture
def step_layout(tmp_path):
    return edited_layout(tmp_path / "layout", 'core = "login"', 'core = "step"')


def test_the_agents_node_is_held_and_placed_last_and_nothing_runs_or_is_torn_down_there(hpc, tmp_path, site):
    """O57: [hpc] sandbox makes the agent's node one more role, the held job's last node; it enters the uniform shape,
    gets no service step and no teardown, and has no ports of a service."""
    control = 'control = { cpus = 1, memory = "1g" }'
    layout = edited_layout(tmp_path / "layout", control, control + '\nsandbox = { cpus = 8, memory = "17g" }')
    rig = Rig(hpc, tmp_path, site, layout=layout)
    rig.up()
    assert (rig.fake.asked["--nodes"], rig.fake.asked["--cpus-per-task"]) == ("7", "9")
    assert rig.fake.asked["--mem"] == "18432M"
    sandbox = rig.state()["services"]["sandbox"]
    assert sandbox["node"] == "node07"
    assert sandbox["ports"] == {}
    assert all(s.node != "node07" for s in rig.fake.spawned)
    rig.fake.sruns.clear()
    rig.driver.down()
    assert [s for s in rig.fake.sruns if s.node == "node07"] == []


def test_with_core_step_the_core_runs_as_a_control_step_on_the_zones_node(hpc, tmp_path, site, step_layout):
    """O57: [hpc] core = "step" (the fallback when the login node cannot reach the services) runs the core's phase in
    a control step on the zone's node, with the core's Python in isolated mode and this driver's own arguments."""
    rig = Rig(hpc, tmp_path, {**site, "HPC_CORE_PYTHON": "/shared/venv/bin/python"}, layout=step_layout)
    assert rig.up().startswith("core: the link check passed")
    core = next(s for s in rig.fake.sruns if "hpcfed.py" in " ".join(s.command))
    assert core.node == "node01"
    assert core.command[:3] == ["/shared/venv/bin/python", "-I", "-B"]  # isolated, and no bytecode written
    assert core.command[4:] == ["--layout", str(step_layout.resolve()), "--root", str(rig.root), "core"]
    assert rig.core_calls == []


def test_a_core_step_that_imports_another_core_fails_up(hpc, tmp_path, site, step_layout):
    """O57: the core step's self-check exits 97 when its interpreter imports another core; up fails on it."""
    from hpc_fed_support import FakeProcess
    from hpcslurm import Completed

    fake = FakeSlurm()
    fake.on_control = lambda s: FakeProcess(Completed(97, "", "")) if "hpcfed.py" in " ".join(s.command) else None
    rig = Rig(hpc, tmp_path, {**site, "HPC_CORE_PYTHON": "/shared/venv/bin/python"}, layout=step_layout, fake=fake)
    with pytest.raises(hpc.driver.CoreMismatch):
        rig.up()


def test_the_self_check_names_the_core_this_interpreter_imports(hpc):
    """O57: the self-check passes for this checkout's core and records where each module came from."""
    paths = hpc.cli.self_check()
    assert paths["expcore_onedata"].endswith("src/expcore_onedata")
    assert paths["hpcfed"].endswith("hpcfed.py")


@pytest.fixture
def make_ca():
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca


def test_the_core_phase_writes_platform_toml_and_the_ten_pair_link_record_in_the_root(
    hpc, tmp_path, make_ca, monkeypatch
):
    """O57: the core's phase renders platform.toml from the layout's template and runs the platform layer's link check
    through it (fedlinks.check_after_up); the record of all ten pairs is written in the root as link-check.json,
    through expcore_onedata.linkrecord's writer."""
    import fedlayout
    import fedlinks
    import fedspec

    root = tmp_path / "root"
    (root / "secrets" / "users").mkdir(parents=True)
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(make_ca(tmp_path / "ca").ca.read_bytes())
    spec = fedspec.load_spec(LAYOUT)
    files = {"secrets/zone.passphrase": "A1b2C3d4E5f6G7h8", "secrets/admin.token": "an-admin-token"}
    files.update({f"secrets/users/{p.name}.pass": "a-password" for p in spec.principals})
    for name, value in files.items():
        (root / name).write_text(value, encoding="utf-8")
        os.chmod(root / name, 0o600)
    hosts = {s.name: f"{s.name}.cluster.example.org" for s in spec.services}
    layout = fedlayout.Layout(root, tmp_path, hpc.boot.hpc_spec(spec, hosts))
    ids = SimpleNamespace(
        admin="uid-admin",
        providers={p.name: f"pid-{p.name}" for p in spec.providers},
        spaces={s.name: f"sid-{s.name}" for s in spec.spaces},
        users={p.name: f"uid-{p.name}" for p in spec.principals},
        scans={},
    )
    seen = []

    def check(config, client):
        pairs = link_pairs(config.spaces, list(config.providers))
        seen.append(pairs)
        rows = tuple(
            PairResult(f"providers[{i}]", f"providers[{j}]", True, 0.5, 65536) for i, j in enumerate(range(len(pairs)))
        )
        return LinkReport(True, "ok", rows, 1.0, 2.0)

    monkeypatch.setattr(fedlinks, "check_links", check)
    out = hpc.boot.core_phase(
        layout, (5001, 5001), hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750),
        rest=lambda ca: SimpleNamespace(close=lambda: None),
        bootstrap=lambda rest, layout, owner: SimpleNamespace(layout=layout, ids=ids, spec=layout.spec),
        steps=lambda boot: None,
        content=lambda boot: SimpleNamespace(reset=lambda timeout: None),
    )  # fmt: skip
    assert len(seen[0]) == 10
    record = json.loads((root / "link-check.json").read_text(encoding="utf-8"))
    assert len(record["pairs"]) == 10
    assert record["ok"] is True
    platform = (root / "platform.toml").read_text(encoding="utf-8")
    assert 'url = "https://zone.cluster.example.org:8443"' in platform
    assert "facts.json and platform.toml written" in out


# -- the service verbs' hook ------------------------------------------------------------------------------------


def test_the_service_verbs_go_through_the_stable_hook_with_the_launcher_and_the_state_store(hpc, tmp_path, monkeypatch):
    """O57: stop, start, status and exec are hpcfed_control.run(verb, service, argv, *, launcher, state, stop_seconds)
    (M4's module); what follows -- is exec's argv, and the stop bound is the layout's."""
    calls = []
    module = SimpleNamespace(VERBS=("stop", "start", "status", "exec"), run=lambda *a, **k: calls.append((a, k)) or 3)
    monkeypatch.setitem(sys.modules, "hpcfed_control", module)
    (tmp_path / "fed").mkdir()
    root = str(tmp_path / "fed" / "root")
    assert hpc.cli.main(["--layout", str(LAYOUT), "--root", root, "exec", "aspen", "--", "op_worker", "ping"], {}) == 3
    assert hpc.cli.main(["--layout", str(LAYOUT), "--root", root, "status", "aspen"], {}) == 3
    (verb, service, argv), keywords = calls[0]
    assert (verb, service, argv) == ("exec", "aspen", ["op_worker", "ping"])
    assert calls[1][0] == ("status", "aspen", [])
    assert keywords["stop_seconds"] == 600
    assert isinstance(keywords["launcher"], hpc.launch.Launcher)
    assert isinstance(keywords["state"], hpc.launch.StateStore)


# -- the login node's fault relay (M4) ----------------------------------------------------------------------------


def test_up_starts_the_fault_relay_last_with_no_environment_but_its_own_and_waits_for_its_heartbeat(
    hpc, tmp_path, site
):
    """O57: once the federation is linked, up starts the login node's fault relay detached, in an environment of
    PATH, HOME and LC_ALL alone (it refuses a secret-like name), serving this root with this driver's argv prefix, and
    waits for its heartbeat; it is recorded in the state file."""
    rig = Rig(hpc, tmp_path, site)
    rig.driver.relay_available = lambda: True
    rig.driver.environ = {"PATH": "/usr/bin:/bin", "HOME": "/home/someone", "SSH_AUTH_SOCK": "/s", "A_TOKEN": "t"}
    assert rig.up().endswith("fault relay: serving")
    (argv,) = rig.fake.relays
    cut = argv.index("-m")
    environment = ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "HOME=/home/someone", "LC_ALL=C"]
    assert argv[:cut] == [*environment, sys.executable, "-B"]
    command = rig.state()["command"]
    assert argv[cut:] == ["-m", "expcore_onedata.fault_relay", "serve", "--root", str(rig.root), "--", *command]
    assert rig.fake.relay_calls[:2] == ["serve", "alive"]
    assert rig.state()["relay"]["pid"] == 9999


def test_an_up_whose_relay_never_beats_fails_and_ends_the_relay(hpc, tmp_path, site):
    """O57: a relay whose heartbeat never comes fails up, and its process is ended."""
    rig = Rig(hpc, tmp_path, site)
    rig.driver.relay_available = lambda: True
    rig.fake.relay_rc["alive"] = 1
    clock = iter(range(0, 10**6, 10))
    rig.driver.clock = lambda: next(clock)
    with pytest.raises(RuntimeError, match="did not beat"):
        rig.up()
    assert [c.pid for c in rig.fake.ended_clients] == [9999]
    assert rig.state()["relay"]["pid"] == 9999  # recorded at once: down ends it if this end failed


def test_down_stops_the_fault_relay_before_anything_else(hpc, tmp_path, site):
    """O57: down stops the relay first, so no service verb launches a step while the federation goes down; a relay
    that will not stop is reported, and the job is cancelled all the same."""
    rig = Rig(hpc, tmp_path, site)
    rig.driver.relay_available = lambda: True
    rig.up()
    rig.fake.calls.clear()
    rig.fake.relay_rc["stop"] = 1
    with pytest.raises(RuntimeError, match="the fault relay"):
        rig.driver.down()
    first = rig.fake.calls[0]
    assert first[1:5] == ["-B", "-m", "expcore_onedata.fault_relay", "stop"]
    assert rig.fake.cancelled == ["4242"]


def test_a_core_without_the_relay_starts_and_stops_none(hpc, tmp_path, site):
    """O57: a core without M4's relay module says so at up, and down does not call it."""
    rig = Rig(hpc, tmp_path, site)
    rig.driver.relay_available = lambda: False
    assert "fault relay: none" in rig.up()
    rig.driver.down()
    assert rig.fake.relay_calls == []


# -- what down must survive (Codex round 1) ----------------------------------------------------------------------


def test_down_never_touches_a_job_whose_id_now_names_another_job(hpc, tmp_path, site):
    """O57: Slurm reuses job ids: a recorded id whose job now carries another name is not ours, so down tears nothing
    down and cancels nothing, and removes the root (ours ended long ago)."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    rig.fake.recorded["JobName"] = "someone-elses-job"
    rig.fake.sruns.clear()
    rig.driver.down()
    assert rig.fake.cancelled == []
    assert rig.fake.sruns == []
    assert not rig.root.exists()


def test_down_cancels_the_job_even_when_stopping_the_services_fails(hpc, tmp_path, site):
    """O57: a zone whose log names a work directory the driver refuses (not under the node's tmpfs) fails up; down
    still cancels the held job by its id and reports the zone unverified, keeping the root."""
    fake = FakeSlurm()
    real = fake.spawn

    def elsewhere(argv, client_log):
        client = real(argv, client_log)
        log = Path(fake.sruns[-1].output)
        log.write_text("hpcop: work directory /scratch/hpcop-oz.abcdef\nhpcop: OK zone\n", encoding="utf-8")
        return client

    fake.spawn = elsewhere
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.state.StateError):
        rig.up()

    def broken(argv, timeout):
        if argv[:2] == ["squeue", "-s"]:
            raise hpc.slurm.SlurmError("squeue failed")
        return FakeSlurm.run(fake, argv, timeout)

    fake.run = broken
    with pytest.raises(RuntimeError, match="could not be verified clean"):
        rig.driver.down()
    assert fake.cancelled[-1] == "4242"
    assert rig.root.exists()


def test_a_running_job_larger_than_asked_is_cancelled_before_any_step(hpc, tmp_path, site):
    """O57: the read-back runs again once the job runs: a scheduler that allocated more cores than the pending
    request showed is caught, and the job cancelled by its id before any step bills on it."""
    fake = FakeSlurm(running_recorded={"NumCPUs": "36"})
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError, match="NumCPUs"):
        rig.up()
    assert fake.cancelled == ["4242"]
    assert fake.sruns == []


def test_down_ends_a_client_that_was_never_recorded_and_reports_one_that_survives(hpc, tmp_path, site):
    """O57: down ends every srun client of the job on this host, recorded or not (one spawned just before a crash),
    each verified gone; one that survives is reported and the root kept."""
    from hpcslurm import Client

    rig = Rig(hpc, tmp_path, site)
    rig.up()
    stray = Client(31337, 5)
    rig.fake.stray_clients = [stray]
    rig.fake.unkillable = [stray]
    with pytest.raises(RuntimeError, match="the client 31337"):
        rig.driver.down()
    assert stray in rig.fake.ended_clients
    assert rig.fake.cancelled == ["4242"]


def test_down_is_refused_while_an_up_of_the_root_runs(hpc, tmp_path, site):
    """O57: up holds the root's lifecycle lock from its state to its end, so a down between up's state and its sbatch
    (which would find no job and remove the root) is refused instead."""
    rig = Rig(hpc, tmp_path, site)
    rig.driver.prepare_root()
    with rig.driver.store.lifecycle("up"):
        other = Rig(hpc, tmp_path, site, fake=rig.fake)
        with pytest.raises(hpc.state.StateError, match="another up or down"):
            other.driver.down()
    assert rig.root.exists()
