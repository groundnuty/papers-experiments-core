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
from hpc_fed_support import (
    LAYOUT,
    PEM,
    SITE,
    FakeProcess,
    FakeSlurm,
    agent_site,
    data_stand_in,
    hpc_on_path,
    pin_values,
)

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
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


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
    assert state["roles"] == ["zone", *PROVIDERS, "sandbox"]  # the agent's node last (its template's [sandbox])
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
    homeless = tuple(f"{k}={v}" for k, v in hpc.slurm.home_env(hpc.launch.NO_HOME).items())
    assert {tuple(s.env) for s in rig.fake.sruns} == {("PATH=/usr/bin:/bin", "LC_ALL=C", "TMPDIR=/tmp", *homeless)}


HOME_NAMES = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "APPTAINER_CONFIGDIR",
              "APPTAINER_CACHEDIR", "UV_CACHE_DIR", "PIP_CACHE_DIR")  # fmt: skip


def test_o57_no_step_of_up_or_down_can_fall_back_to_the_accounts_home(hpc, tmp_path, site):
    """O57: with env -i and HOME unset, many tools take the passwd entry's home, which every job and agent of the
    account shares. Every step of up and down, control and service, names HOME, the XDG base directories, Apptainer's,
    uv's and pip's directories under a path nobody can make (below /dev/null, not a path of the shared tmpfs another
    user could make first), Python's user site off and no git configuration; nothing else joins the allowlist."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    rig.driver.down()
    assert rig.fake.spawned
    for srun in rig.fake.sruns:
        env = dict(word.split("=", 1) for word in srun.env)
        assert set(env) == {"PATH", "LC_ALL", "TMPDIR", *HOME_NAMES, "PYTHONNOUSERSITE", "GIT_CONFIG_GLOBAL",
                            "GIT_CONFIG_NOSYSTEM"}, srun.name  # fmt: skip
        assert all(env[name].startswith("/dev/null/") for name in HOME_NAMES), srun.name
        flags = [env[n] for n in ("PYTHONNOUSERSITE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")]
        assert flags == ["1", "/dev/null", "1"], srun.name


def scripts_of(sruns) -> set:
    """The bundle script (or helper) each step runs, by file name: a step's command is `<interpreter> <script> ...`."""
    return {Path(word).name for s in sruns for word in s.command[:4] if word.endswith((".sbatch", ".sh", ".py"))}


def test_o57_every_step_up_and_down_launch_names_the_sites_network_option_when_it_gives_one(hpc, tmp_path, site):
    """O57: HPC_STEP_NETWORK=<value> puts `--network=<value>` on every srun the driver launches, whatever the step: a
    service, a token's mint and delivery, the node facts, a teardown, the sweep of the agent's node, and a step in
    either component of the heterogeneous job (found live: a heterogeneous job's steps on component 0 failed with "Error
    configuring interconnect" on a site whose switch plugin gives each job a virtual network identifier)."""
    rig = Rig(hpc, tmp_path, {**site, "HPC_STEP_NETWORK": "no_vni"})
    rig.up()
    assert rig.state()["site"]["step_network"] == "no_vni"
    rig.driver.down()
    found = scripts_of(rig.fake.sruns)
    assert {"zone.sbatch", "provider.sbatch", "mint-registration-token.sh", "deliver-token.sh", "teardown.sh"} <= found
    assert {"nodefacts.py", "shards.py"} <= found  # the node facts, and down's sweep of the agent's node
    assert rig.fake.spawned
    assert {s.het_group for s in rig.fake.sruns} == {0, 1}  # the services' component and the agent's
    assert {s.network for s in rig.fake.sruns} == {"no_vni"}
    assert all(s.network == "no_vni" for s in rig.fake.spawned)


def test_o57_down_in_a_shell_without_the_variable_launches_its_steps_with_the_recorded_option(hpc, tmp_path, site):
    """O57: the option is recorded in the state file with the rest of the site, so `down` and the service verbs, run
    where site.env was not sourced, launch their steps with it all the same."""
    rig = Rig(hpc, tmp_path, {**site, "HPC_STEP_NETWORK": "no_vni"})
    rig.up()
    rig.fake.sruns.clear()
    words = ["--layout", str(LAYOUT), "--root", str(rig.root)]
    assert hpc.cli.main([*words, "down"], {}, runner=rig.fake) == 0
    assert "teardown.sh" in scripts_of(rig.fake.sruns)
    assert {s.network for s in rig.fake.sruns} == {"no_vni"}


def test_o57_without_the_variable_no_step_names_a_network_option(hpc, tmp_path, site):
    """O57: a site that sets no HPC_STEP_NETWORK is unchanged: no srun of up or down carries `--network`."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    assert rig.state()["site"]["step_network"] == ""
    rig.driver.down()
    assert rig.fake.sruns
    assert {s.network for s in rig.fake.sruns} == {None}
    assert not any(word.startswith("--network") for call in rig.fake.calls for word in call)


COMPONENT_ZERO_STEPS_BEFORE_THE_FIRST_MINT = 12  # six nodes' facts and six services: the next step failed live


def test_o57_up_on_a_site_that_refuses_component_zero_steps_without_a_network_option_fails_without_one(
    hpc, tmp_path, site
):
    """O57: the site's switch plugin refused a heterogeneous job's steps on component 0 from a point on ("srun: error:
    task 0 launch failed: Error configuring interconnect"), after the zone and five providers had started, so the
    first token's mint and delivery were refused and `up` failed at a provider's registration; the fake does the same
    to every such step that names no --network, and a step on component 1 never."""
    fake = FakeSlurm(interconnect_after=COMPONENT_ZERO_STEPS_BEFORE_THE_FIRST_MINT)
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(RuntimeError, match="aspen's registration failed"):
        rig.up()
    refused = [s for s in fake.sruns if s.refused]
    assert refused
    assert all(s.het_group == 0 and s.name.endswith(("-mint", "-deliver")) for s in refused)
    assert len(fake.spawned) == 6  # the zone and five providers had started


def test_o57_up_on_a_site_that_refuses_component_zero_steps_finishes_with_the_network_option(hpc, tmp_path, site):
    """O57: the same site and the same fake, with HPC_STEP_NETWORK=no_vni: every step names it, none is refused, and
    `up` reaches the link check; the agent's component's steps, which the site never refused, carry it too."""
    fake = FakeSlurm(interconnect_after=COMPONENT_ZERO_STEPS_BEFORE_THE_FIRST_MINT)
    rig = Rig(hpc, tmp_path, {**site, "HPC_STEP_NETWORK": "no_vni"}, fake=fake)
    assert rig.up().startswith("core: the link check passed")
    assert rig.state()["phase"] == "linked"
    assert fake.delivered == PROVIDERS
    rig.driver.down()  # whose teardown steps on component 0 the site would have refused too
    assert not any(s.refused for s in fake.sruns)
    assert fake.component_zero_steps == 0  # every step of component 0 named the option
    assert {s.network for s in fake.sruns} == {"no_vni"}
    assert fake.cancelled == ["4242"]


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
    assert (values["hpc_step_launch_wait_s"], values["hpc_command_timeout_s"]) == ("240", "300")
    assert (values["hpc_stop_bound_s"], values["hpc_settle_timeout_s"]) == ("140", "440")
    command = json.loads(values["hpc_command"])
    assert command[1:3] == ["-I", "-B"]  # isolated (O58): the relay runs every verb through it
    assert command[3:] == [str(Path(rig.hpc.cli.__file__).resolve()), "--layout", str(LAYOUT), "--root", str(rig.root)]


KEY_BLOCK = "-----BEGIN RSA " + "PRIVATE KEY-----\nTUlJQg==\n-----END RSA " + "PRIVATE KEY-----\n"  # a stand-in


def test_the_root_keeps_only_the_certificate_of_a_ca_export_that_holds_its_key_first(hpc, tmp_path, site):
    """O57: the image's CA file holds the CA's private key before its certificate (seen live: the core's phase refused
    the root's ca_file, so every up failed before the link check); the root keeps the certificate alone."""
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(ca_pem=KEY_BLOCK + "\n" + PEM))
    rig.up()
    kept = (rig.root / "ca" / "federation-ca.pem").read_text(encoding="utf-8")
    assert kept == PEM
    assert "PRIVATE KEY" not in kept


def test_an_up_whose_ca_export_holds_no_certificate_fails_and_writes_no_ca(hpc, tmp_path, site):
    """O57: a CA export with a key and no certificate fails up, and the root holds no CA file."""
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(ca_pem=KEY_BLOCK))
    with pytest.raises(RuntimeError, match="holds no certificate"):
        rig.up()
    assert not (rig.root / "ca" / "federation-ca.pem").exists()
    assert rig.core_calls == []


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
    assert all(s.command[-2:] == ["--timeout", "50"] for s in teardowns)
    checks = [s for s in rig.fake.sruns if s.command[0] == "test"]
    assert len(checks) == 6
    assert all(s.command[1:3] == ["!", "-e"] for s in checks)
    assert rig.fake.cancelled == ["4242"]
    assert len(rig.fake.ended_clients) == 6
    assert not rig.root.exists()
    assert "is gone" in out


def test_a_teardown_step_is_bounded_by_the_launch_wait_the_stop_the_kill_and_a_margin(hpc, tmp_path, site):
    """O58: down's teardown step is waited for from its start, so its bound holds the step's launch wait (240 s), the
    graceful stop (50 s), supervise.py's kill tail (30 s) and 90 s: 410 s, past a step that starts 200 s late (the
    controller's message timeout) and ends 33 s after its SIGTERM, and past the worst teardown after it (the whole
    graceful stop, then the kill)."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    asked, inner = {}, rig.driver.launcher.control

    def control(node, argv, *, name, timeout_s, job=None):
        asked[name] = timeout_s
        return inner(node, argv, name=name, timeout_s=timeout_s, job=job)

    rig.driver.launcher.control = control
    rig.driver.down()
    teardown = {bound for name, bound in asked.items() if "-teardown-" in name}
    assert teardown == {240 + 50 + 30 + 90}
    assert 200 + 33 < 410
    assert 200 + 50 + 30 < 410


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


NODE_INIT_FAILED = "ok: /proc hides other users' processes\nhpcop: ERROR: ss failed: cannot list the listeners\n"


def zone_died_in_node_init(fake, *, client_ends=True, listed=False):
    """The fake's zone step as the live one ran: its log holds node-init's error and no work directory, and its step
    ends at once (listed no more), its srun client too unless `client_ends` is False; `listed` keeps it listed."""
    real = fake.spawn

    def failing(argv, client_log):
        client = real(argv, client_log)
        srun = fake.sruns[-1]
        if srun.name.endswith("-zone-run-1"):
            Path(srun.output).write_text(NODE_INIT_FAILED, encoding="utf-8")
            if not listed:
                fake.steps.pop(srun.name, None)
            if client_ends:
                fake.dead_clients.append(client)
        return client

    fake.spawn = failing
    return fake


def leftover_checks(fake):
    return [s for s in fake.sruns if any(Path(word).name == "leftovers.py" for word in s.command)]


def test_down_after_a_zone_that_died_before_naming_its_work_directory_checks_its_node_and_removes_the_root(
    hpc, tmp_path, site
):
    """O57: a zone whose step ended before its log named a work directory (live: node-init failed before making
    one) is clean once its step is shown ended and a read-only check on its node counts no work directory of ours of
    its kind; down then removes the root instead of refusing it."""
    rig = Rig(hpc, tmp_path, site, fake=zone_died_in_node_init(FakeSlurm()))
    with pytest.raises(RuntimeError, match="zone failed to start"):
        rig.up()
    assert rig.state()["services"]["zone"]["workdir"] is None
    rig.fake.sruns.clear()
    assert "is gone, and so is the root" in rig.driver.down()
    (check,) = leftover_checks(rig.fake)
    assert (check.node, check.command[-2:]) == ("node01", ["oz", "/tmp"])
    assert check.command[:3] == ["python3.11", "-I", "-B"]
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_down_keeps_the_root_when_the_nodes_check_finds_a_work_directory_of_ours(hpc, tmp_path, site):
    """O57: a work directory of ours of the zone's kind on its node keeps the zone unverified and the root kept; the
    held job is cancelled all the same."""
    fake = zone_died_in_node_init(FakeSlurm(leftovers={"node01": 1}))
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(RuntimeError, match="zone failed to start"):
        rig.up()
    with pytest.raises(RuntimeError, match="zone could not be verified clean"):
        rig.driver.down()
    assert rig.fake.cancelled == ["4242"]
    assert rig.root.exists()


def test_a_step_whose_end_cannot_be_shown_stays_unverified_without_a_check(hpc, tmp_path, site):
    """O57: a step never seen listed whose srun client still runs may yet start: its end cannot be shown, so the zone
    stays unverified and no check of its node is taken as proof."""
    rig = Rig(hpc, tmp_path, site, fake=zone_died_in_node_init(FakeSlurm(), client_ends=False))
    with pytest.raises(RuntimeError, match="zone failed to start"):
        rig.up()
    rig.fake.sruns.clear()
    with pytest.raises(RuntimeError, match="zone could not be verified clean"):
        rig.driver.down()
    assert leftover_checks(rig.fake) == []
    assert rig.root.exists()


def test_a_nodes_check_that_fails_is_no_proof_that_it_is_clean(hpc, tmp_path, site):
    """O57: the node's check ending with a traceback, or printing no count, keeps the zone unverified: a check that
    could not read the tmpfs is never a count of 0."""
    from hpcslurm import Completed

    def broken(srun):
        if any(Path(word).name == "leftovers.py" for word in srun.command):
            return FakeProcess(Completed(1, "", "Traceback (most recent call last):\nPermissionError\n"))
        return None

    rig = Rig(hpc, tmp_path, site, fake=zone_died_in_node_init(FakeSlurm(on_control=broken)))
    with pytest.raises(RuntimeError, match="zone failed to start"):
        rig.up()
    with pytest.raises(RuntimeError, match="zone could not be verified clean"):
        rig.driver.down()
    assert rig.root.exists()


def test_a_zone_whose_log_names_a_directory_the_driver_refused_is_never_shown_clean(hpc, tmp_path, site):
    """O57: a zone whose log named a work directory the driver refused (not on the node's tmpfs) left that directory
    where the node's check does not look: its step ended all the same, it stays unverified and the root is kept."""
    fake = zone_died_in_node_init(FakeSlurm())
    real = fake.spawn

    def elsewhere(argv, client_log):
        client = real(argv, client_log)
        srun = fake.sruns[-1]
        if srun.name.endswith("-zone-run-1"):
            Path(srun.output).write_text("hpcop: work directory /scratch/hpcop-oz.abcdef\nhpcop: ERROR: x\n", "utf-8")
        return client

    fake.spawn = elsewhere
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(RuntimeError, match="zone failed to start"):
        rig.up()
    rig.fake.sruns.clear()
    with pytest.raises(RuntimeError, match="zone could not be verified clean"):
        rig.driver.down()
    assert leftover_checks(rig.fake) == []
    assert rig.fake.cancelled == ["4242"]
    assert rig.root.exists()


def test_a_step_still_listed_is_cancelled_by_its_id_and_its_node_checked_once_it_left(hpc, tmp_path, site):
    """O57: a zone step still listed whose log names no work directory is cancelled by its own id, recorded as seen;
    once every step has left, its node's check counts none and the root goes."""
    rig = Rig(hpc, tmp_path, site, fake=zone_died_in_node_init(FakeSlurm(), client_ends=False, listed=True))
    with pytest.raises(RuntimeError, match="zone failed to start"):
        rig.up()
    step = rig.fake.steps[f"{rig.state()['allocation']['name']}-zone-run-1"]
    rig.fake.sruns.clear()
    rig.driver.down()
    assert rig.fake.cancelled.index(step) < rig.fake.cancelled.index("4242")
    assert len(leftover_checks(rig.fake)) == 1
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


def test_a_node_facts_step_that_fails_is_named_with_its_status_and_the_end_of_its_stderr(hpc, tmp_path, site):
    """O57: a failed node facts step fails up with its status and the last of its stderr on one line, bounded, so a
    failure like the site's refusal of a het job's step with no group is diagnosable from the message alone."""
    from hpcslurm import Completed

    noise = "".join(f"line {i} of a long traceback\n" for i in range(200))
    said = noise + "srun: error: the step could not start\x07\n"

    def failing(srun):
        return FakeProcess(Completed(1, "", said)) if srun.name.endswith("-facts") else None

    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(on_control=failing))
    with pytest.raises(RuntimeError) as raised:
        rig.up()
    message = str(raised.value)
    assert message.startswith("the node facts of zone are not a name")
    assert "(the step's status 1; its stderr: " in message
    assert message.endswith("srun: error: the step could not start?)")
    assert "\n" not in message
    assert len(message) < 500
    assert "line 0 of" not in message


def test_a_node_facts_step_that_says_nothing_is_named_so(hpc, tmp_path, site):
    """O57: a node facts step that printed nothing usable and nothing on stderr says so in up's message."""
    from hpcslurm import Completed

    def silent(srun):
        return FakeProcess(Completed(0, "not json\n", "")) if srun.name.endswith("-facts") else None

    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(on_control=silent))
    with pytest.raises(RuntimeError, match=r"\(the step's status 0; nothing on its stderr\)$"):
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
    """O57: [hpc] sandbox makes the agent's node one more role, placed last, as the held job's own component (its
    shape never enters the service nodes'); it gets no service step and no teardown, and has no ports of a service.
    O63: down's one step there is the sweep of the shard steps' directories on its tmpfs."""
    control = 'control = { cpus = 1, memory = "1g" }'
    layout = edited_layout(tmp_path / "layout", control, control + '\nsandbox = { cpus = 8, memory = "17g" }')
    rig = Rig(hpc, tmp_path, site, layout=layout)
    rig.up()
    assert (rig.fake.asked["--nodes"], rig.fake.asked["--cpus-per-task"]) == ("6", "5")
    assert rig.fake.asked["--mem"] == "11264M"
    sandbox = rig.state()["services"]["sandbox"]
    assert sandbox["node"] == "node07"
    assert sandbox["ports"] == {}
    assert all(s.node != "node07" for s in rig.fake.spawned)
    rig.fake.sruns.clear()
    rig.driver.down()
    assert [s for s in rig.fake.sruns if s.node == "node07"] == rig.fake.sweeps
    assert [s.name.rsplit("-", 2)[-2:] for s in rig.fake.sweeps] == [["sandbox", "sweep"]]


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
        layout, (5001, 5001), {**hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, root),
                               **pin_values(tmp_path / "agent")},
        rest=lambda ca: SimpleNamespace(close=lambda: None),
        bootstrap=lambda rest, layout, owner: SimpleNamespace(layout=layout, ids=ids, spec=layout.spec),
        steps=lambda boot: None,
        content=lambda boot: SimpleNamespace(reset=lambda timeout: None),
        data=data_stand_in(),
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
    assert keywords["stop_seconds"] == 50
    assert keywords["step_launch_wait_s"] == 240
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
    assert argv[:cut] == [*environment, sys.executable, "-I", "-B"]
    command = rig.state()["command"]
    assert argv[cut:] == ["-m", "expcore_onedata.fault_relay", "serve", "--root", str(rig.root), "--", *command]
    assert rig.fake.relay_calls[:2] == ["serve", "alive"]
    assert rig.fake.relay_cwds == [str(rig.root)]  # it works in the root, never where up was launched (O58)
    assert rig.state()["relay"]["pid"] == 9999


def test_o58_an_up_whose_root_a_link_reaches_is_refused_before_anything_is_submitted(hpc, tmp_path, site):
    """O58: the relay works in the root and never starts in one a symbolic link reaches (the root's own name or a
    directory above it), so up refuses such a root in its preflight, before any job is submitted, rather than once
    the federation is up."""
    (tmp_path / "real" / "fed").mkdir(parents=True)  # the root's parent is no link: one above it is
    (tmp_path / "linked").symlink_to(tmp_path / "real")
    rig = Rig(hpc, tmp_path, site)
    rig.driver = hpc.cli.make_driver(LAYOUT, str(tmp_path / "linked" / "fed" / "root"), runner=rig.fake)
    rig.driver.relay_available = lambda: True
    with pytest.raises(RuntimeError, match="reached through a symbolic link"):
        rig.up()
    assert rig.fake.submitted == {}
    assert not (tmp_path / "real" / "fed" / "root").exists()


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
    assert first[1:6] == ["-I", "-B", "-m", "expcore_onedata.fault_relay", "stop"]
    assert rig.fake.cancelled == ["4242"]


class RelayWhoseStopFails(FakeSlurm):
    """A relay whose own stop fails (as on a login node whose kernel has no pidfd_open: the stop's signal could not be
    sent), and which only the end of its recorded client brings down: its stop fails, by exit status 1 or by an error
    (`raises`), until that client is ended, and shows no heartbeat after."""

    raises = False

    def run(self, argv, timeout):
        argv = list(argv)
        if argv[1:6] == ["-I", "-B", "-m", "expcore_onedata.fault_relay", "stop"]:
            ended = any(client.pid == 9999 for client in self.ended_clients)
            if not ended and self.raises:
                self.relay_calls.append("stop")
                raise OSError("the stop could not be started")
            self.relay_rc["stop"] = 0 if ended else 1
        return super().run(argv, timeout)


@pytest.mark.parametrize("raises", [False, True])
def test_o58_down_checks_the_fault_relay_again_once_its_client_is_ended(hpc, tmp_path, site, raises):
    """O58: a relay whose own stop failed (it exited 1 or could not be run) is ended by `down` as a recorded client, and
    `down` then checks it again: with no heartbeat beating it is clean, and `down` finishes and removes the root in
    the one run, where it concluded the relay was not verified before it had ended it and failed every time."""
    fake = RelayWhoseStopFails()
    fake.raises = raises
    rig = Rig(hpc, tmp_path, site, fake=fake)
    rig.driver.relay_available = lambda: True
    rig.up()
    rig.fake.relay_calls.clear()
    assert "the held job 4242 is gone, and so is the root" in rig.driver.down()
    assert not rig.root.exists()
    assert rig.fake.relay_calls == ["stop", "stop"]
    assert [c.pid for c in rig.fake.ended_clients if c.pid == 9999] == [9999]
    assert rig.fake.cancelled == ["4242"]


def test_o58_down_still_reports_a_fault_relay_that_beats_after_its_client_was_ended(hpc, tmp_path, site):
    """O58: the second check is not a way round the first: a relay whose stop fails again after its client was ended is
    unverified, the job is cancelled all the same, and the root and its state are kept for the next `down`."""
    rig = Rig(hpc, tmp_path, site)
    rig.driver.relay_available = lambda: True
    rig.up()
    rig.fake.relay_calls.clear()
    rig.fake.relay_rc["stop"] = 1
    with pytest.raises(RuntimeError, match="the fault relay could not be verified clean"):
        rig.driver.down()
    assert rig.fake.relay_calls == ["stop", "stop"]
    assert rig.fake.cancelled == ["4242"]
    assert rig.root.exists()
    assert rig.state()["phase"] == "down"


def test_o58_down_checks_the_fault_relay_once_when_its_stop_succeeded(hpc, tmp_path, site):
    """O58: a relay that stopped at the first call is verified by it: no second stop is made."""
    rig = Rig(hpc, tmp_path, site)
    rig.driver.relay_available = lambda: True
    rig.up()
    rig.fake.relay_calls.clear()
    rig.driver.down()
    assert rig.fake.relay_calls == ["stop"]


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
