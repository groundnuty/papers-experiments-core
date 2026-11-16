"""The HPC federation driver's plan (deploy/hpc-federation, DESIGN.md): the layout's [hpc] table and the held job's
shape, the pilot-shape layout's ten link pairs, the state file, and how every Slurm command is formed and read back.
Nothing here reaches a cluster: Slurm is the fake of hpc_fed_support."""

import json
import math
import os
from dataclasses import fields, replace
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, FakeProcess, FakeSlurm, import_hpc, parse_srun

from expcore_onedata.config import load_platform
from expcore_onedata.fault_settings import FaultSettings, restore_token_margin_s
from expcore_onedata.linksettings import link_pairs


@pytest.fixture(scope="module")
def hpc():
    return import_hpc()


# -- the [hpc] table ------------------------------------------------------------------------------------------------


def test_a_layout_without_an_hpc_table_takes_the_sizes_of_the_live_peaks(hpc):
    """O57: every size is a layout setting, defaulting to the live peaks: the zone 4 cores and 8 GB, a provider 4 and
    10 GB, a control reserve of 1 core and 1 GB on every node, so each node is 5 cores and 11 GB."""
    settings = hpc.settings.load_settings(None)
    assert (settings.zone.cpus, settings.zone.memory_mib) == (4, 8192)
    assert (settings.provider.cpus, settings.provider.memory_mib) == (4, 10240)
    assert settings.node() == hpc.settings.Size(5, 11264)


def test_the_held_job_holds_one_node_per_service_and_one_for_the_agent_when_declared(hpc):
    """O57: one service per node: the zone first, the providers in the layout's order, the agent's node last."""
    plain = hpc.settings.load_settings({})
    assert [r.name for r in plain.roles(("a", "b"))] == ["zone", "a", "b"]
    with_agent = hpc.settings.load_settings({"sandbox": {"cpus": 8, "memory": "17g"}})
    roles = with_agent.roles(("a", "b"))
    assert [(r.name, r.kind) for r in roles] == [("zone", "oz"), ("a", "op"), ("b", "op"), ("sandbox", "sandbox")]
    assert with_agent.node() == hpc.settings.Size(9, 18432)  # the largest step sets the uniform shape


def test_billing_is_the_larger_of_cores_and_the_weight_times_the_memory_per_node(hpc):
    """O57: what describe prints: nodes x max(cores, weight x GB), from the layout's sizes."""
    settings = hpc.settings.load_settings(None)
    assert settings.billing_per_hour(6, 0.5) == 6 * 5.5
    assert settings.billing_per_hour(6, 0.25) == 6 * 5


def test_the_settle_time_exceeds_the_stops_whole_bound(hpc):
    """O57: a stop takes at most stop_seconds, the 30 s SIGKILL tail and 60 s for its step to leave: settle_timeout_s
    is above that, derived from the layout, never a constant."""
    assert hpc.settings.load_settings(None).settle_timeout_s == 750
    settings = hpc.settings.load_settings({"stop_seconds": 120})
    assert (settings.stop_bound_s, settings.settle_timeout_s) == (210, 270)


@pytest.mark.parametrize(
    ("table", "words"),
    [
        ({"zone": {"cpus": 0, "memory": "8g"}}, "hpc.zone.cpus"),
        ({"provider": {"cpus": 4, "memory": "10 GB"}}, "hpc.provider.memory"),
        ({"control": {"cpus": 1}}, "hpc.control.memory: is required"),
        ({"time": "2h"}, "hpc.time"),
        ({"time": "00:20:00"}, "must exceed twice the stop's bound"),
        ({"core": "elsewhere"}, "hpc.core"),
        ({"nodes": 3}, "hpc.nodes: is not a key"),
    ],
    ids=["no cores", "bad memory", "missing memory", "bad time", "too short", "bad core place", "unknown key"],
)
def test_a_bad_hpc_table_is_refused_naming_the_key(hpc, table, words):
    """O57: a bad hpc table is refused naming the key."""
    with pytest.raises(hpc.settings.SettingsError, match=words):
        hpc.settings.load_settings(table)


def test_the_compose_layout_reader_keeps_the_hpc_table_and_compose_ignores_it(hpc):
    """O57: the HPC driver reads the very layout file the Compose federation does: fedspec keeps [hpc] as written, and
    the Compose file it generates is the same with or without it."""
    import fedcompose
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    assert spec.hpc["provider"] == {"cpus": 4, "memory": "10g"}
    assert fedcompose.render(spec) == fedcompose.render(replace(spec, hpc=None))


def test_the_site_comes_from_the_environment_and_a_missing_value_is_named(hpc):
    """O57: the site comes from the environment and a missing value is named."""
    environ = {
        "HPC_ACCOUNT": "test-account",
        "HPC_PARTITION": "test-partition",
        "SH": "/site/images",
        "L": "/site/logs",
    }
    site = hpc.settings.site_from_environment(environ)
    assert (site.node_tmp, site.step_path) == ("/tmp", "/usr/local/bin:/usr/bin:/bin")
    assert hpc.settings.Site.from_json(site.to_json()) == site
    with pytest.raises(hpc.settings.SettingsError, match="HPC_PARTITION is not set"):
        hpc.settings.site_from_environment({**environ, "HPC_PARTITION": "<partition>"})
    with pytest.raises(hpc.settings.SettingsError, match="HPC_NODE_TMP"):
        hpc.settings.site_from_environment({**environ, "HPC_NODE_TMP": "tmp"})


# -- the pilot's shape: ten pairs -----------------------------------------------------------------------------------


def rendered_pilot(hpc, root: Path, make_ca) -> Path:
    """The pilot-shape platform.toml as up writes it, over invented ids, in a root holding the files it names."""
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    settings = hpc.settings.load_settings(spec.hpc)
    facts = {
        "zone": {"url": "https://zone.example.org:8443"},
        "study_space": spec.study_space,
        "providers": {
            p.name: {"url": f"https://{p.name}.example.org:8443", "id": f"pid-{p.name}"} for p in spec.providers
        },
        "spaces": {s.name: {"id": f"sid-{s.name}"} for s in spec.spaces},
        "principals": {p.name: {"user_id": f"uid-{p.name}"} for p in spec.principals},
    }
    (root / "secrets" / "users").mkdir(parents=True)
    for name, value in [("secrets/admin.token", "an-admin-token")] + [
        (f"secrets/users/{p.name}.pass", "a-password") for p in spec.principals
    ]:
        (root / name).write_text(value, encoding="utf-8")
        os.chmod(root / name, 0o600)
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(make_ca(root / "made").ca.read_bytes())
    (root / "fault-journal").mkdir()
    command = ["/venv/python", "/repo/hpcfed.py", "--layout", str(LAYOUT), "--root", str(root)]
    values = hpc.boot.hpc_values(command, settings.stop_bound_s, settings.settle_timeout_s)
    hpc.boot.write_platform(root / "platform.toml", spec.platform_template.read_text(encoding="utf-8"), facts, values)
    return root / "platform.toml"


@pytest.fixture
def make_ca():
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca


def test_the_pilot_shape_has_five_providers_four_data_spaces_and_ten_link_pairs(hpc, tmp_path, make_ca):
    """O57: the example layout matches the pilot's shape with invented names: five providers, four data spaces each
    owned by one and mirrored by another, the probe space on all five; its rendered platform.toml loads and declares the
    ten ordered pairs the pilot's link check reports."""
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    data = [s for s in spec.spaces if s.agent_visible]
    assert len(spec.providers) == 5
    assert len(data) == 4
    assert all(s.mirrors for s in data)
    assert set(spec.space(spec.probe_space).supporters) == {p.name for p in spec.providers}
    config = load_platform(rendered_pilot(hpc, tmp_path, make_ca))
    assert len(link_pairs(config.spaces, list(config.providers))) == 10


def test_the_platform_declares_the_hpc_backend_and_its_stop_bound_and_no_compose_keys(hpc, tmp_path, make_ca):
    """O57: up renders [faults] for the HPC backend (M4): backend "hpc", stop_bound_s from the layout (stop_seconds +
    90 s), the journal in the root and every provider as a service, with none of the Compose keys it refuses."""
    import tomllib

    faults = tomllib.loads(rendered_pilot(hpc, tmp_path, make_ca).read_text(encoding="utf-8"))["faults"]
    assert faults["backend"] == "hpc"
    assert faults["stop_bound_s"] == 690
    assert faults["journal"] == "fault-journal"
    assert set(faults["services"]) == {"aspen", "larch", "rowan", "alder", "hazel"}
    assert not {"compose_file", "project", "command", "docker"} & set(faults)


def test_the_token_margin_is_the_least_the_loader_of_this_core_accepts(hpc, tmp_path, make_ca, monkeypatch):
    """O57: [tokens] margin_s is derived by the platform layer's own loader from the file as written (never a
    constant), so a measured stop bound, or a term the loader adds, moves it."""
    import tomllib

    import expcore_onedata.fault_settings as fault_settings

    loaded = FaultSettings(
        file=Path("p"), sanctioned=False, journal=Path("j"), compose_file=Path("c"), services={},
        settle_timeout_s=750.0, poll_s=10.0, clear_attempts=5,
    )  # fmt: skip
    seen = []

    def load_faults(path, config):
        seen.append(path)
        return loaded

    monkeypatch.setattr(fault_settings, "load_faults", load_faults)
    document = tomllib.loads(rendered_pilot(hpc, tmp_path, make_ca).read_text(encoding="utf-8"))
    assert seen == [tmp_path / "platform.toml"]
    assert document["tokens"]["margin_s"] == math.ceil(restore_token_margin_s(loaded))


def test_a_core_without_the_hpc_backend_takes_the_restores_rule_with_the_hpc_settle_and_poll(hpc, tmp_path, make_ca):
    """O57: a core whose loader cannot read the HPC [faults] (no backend "hpc" yet) still gets a margin: the restore's
    rule over the keys it knows, with the HPC settle time (stop bound + 60 s) and poll (10 s)."""
    import tomllib

    document = tomllib.loads(rendered_pilot(hpc, tmp_path, make_ca).read_text(encoding="utf-8"))
    rule = FaultSettings(
        file=Path("p"), sanctioned=False, journal=Path("j"), compose_file=Path("c"), services={},
        settle_timeout_s=750.0, poll_s=10.0,
    )  # fmt: skip
    if "stop_bound_s" in {f.name for f in fields(FaultSettings)}:
        pytest.skip("this core's loader reads the HPC backend: the loader's own rule applies (the test above)")
    assert document["tokens"]["margin_s"] == math.ceil(restore_token_margin_s(rule))


# -- the state file ---------------------------------------------------------------------------------------------


def test_the_state_file_round_trips_and_holds_no_unknown_format(hpc, tmp_path):
    """O57: the state file is written atomically (no draft left) and read back whole; another format is refused."""
    state = {
        "format": hpc.state.FORMAT,
        "project": "p",
        "allocation": {"job": "12", "name": "hpcop-p-1"},
        "services": {"zone": hpc.state.new_service("oz")},
        "phase": "submitted",
        "site": {},
    }
    hpc.state.save(tmp_path, state)
    assert hpc.state.load(tmp_path) == state
    assert [p.name for p in tmp_path.iterdir()] == [hpc.state.STATE]
    assert oct((tmp_path / hpc.state.STATE).stat().st_mode & 0o777) == "0o600"
    (tmp_path / hpc.state.STATE).write_text(json.dumps({**state, "format": "other/1"}), encoding="utf-8")
    with pytest.raises(hpc.state.StateError, match="not of the format"):
        hpc.state.load(tmp_path)


@pytest.mark.parametrize(
    "value",
    ["/tmp/hpcop-op.abcdef/..", "/scratch/hpcop-op.abcdef", "/tmp/x/hpcop-op.abcdef", "/tmp/hpcop-oz.abcdef", "tmp"],
    ids=["dots", "not tmpfs", "nested", "other kind", "relative"],
)
def test_a_work_directory_is_accepted_only_as_the_bundle_makes_one(hpc, value):
    """O57: down removes what a recorded work directory names, so only <node tmpfs>/hpcop-<kind>.XXXXXX passes."""
    assert hpc.state.checked_workdir("/tmp/hpcop-op.abcdef", "/tmp", "op") == "/tmp/hpcop-op.abcdef"
    with pytest.raises(hpc.state.StateError):
        hpc.state.checked_workdir(value, "/tmp", "op")


def test_a_state_update_is_a_locked_read_modify_write(hpc, tmp_path):
    """O57: the store's update reads, changes and writes under its lock; a change that returns no state is refused and
    the file stays as it was."""
    store = hpc.launch.StateStore(tmp_path)
    store.create({"format": hpc.state.FORMAT, "project": "p", "allocation": {"job": None}, "services": {},
                  "phase": "preflight", "site": {}})  # fmt: skip
    store.update(lambda s: {**s, "phase": "submitted"})
    assert store.read()["phase"] == "submitted"
    with pytest.raises(hpc.state.StateError):
        store.update(lambda s: None)
    assert store.read()["phase"] == "submitted"
    with pytest.raises(hpc.state.StateError, match="run down first"):
        store.create(store.read())


# -- Slurm --------------------------------------------------------------------------------------------------------


def allocation(hpc, **change):
    values = {"name": "hpcop-p-abcd", "account": "acct", "partition": "part", "nodes": 6, "cpus_per_node": 5,
              "memory_mib": 11264, "time": "02:00:00", "time_s": 7200}  # fmt: skip
    return hpc.slurm.Allocation(**{**values, **change})


def test_the_held_job_is_submitted_unrequeued_timed_and_with_no_environment(hpc):
    """O57: the held job: --no-requeue, a --time, one task per node, --export=NONE, held by exec sleep."""
    argv = allocation(hpc).argv()
    for word in ("--no-requeue", "--time=02:00:00", "--ntasks-per-node=1", "--export=NONE", "--wrap=exec sleep 7200"):
        assert word in argv
    assert "--mem=11264M" in argv
    assert "--cpus-per-task=5" in argv
    assert "--nodes=6" in argv


@pytest.mark.parametrize(
    ("recorded", "field"),
    [
        ({"TimeLimit": "04:00:00"}, "TimeLimit"),
        ({"Requeue": "1"}, "Requeue"),
        ({"Partition": "other"}, "Partition"),
        ({"NumCPUs": "36"}, "NumCPUs"),
        ({"MinMemoryNode": "12G"}, "MinMemoryNode"),
        ({"Account": "another"}, "Account"),
    ],
    ids=["time", "requeue", "partition", "cpus", "memory", "account"],
)
def test_a_held_job_recorded_otherwise_than_asked_is_cancelled_by_its_own_id(hpc, recorded, field):
    """O57: the submit reads back what Slurm recorded; any field otherwise than asked cancels that one job, by id."""
    fake = FakeSlurm(recorded=recorded)
    slurm = hpc.slurm.Slurm(fake, sleep=lambda s: None)
    asked = allocation(hpc)
    job = slurm.submit(asked)
    with pytest.raises(hpc.slurm.SlurmError, match=field):
        slurm.readback(job, asked)
    assert fake.cancelled == [job]
    assert not any(c[0] == "scancel" and c[1:] != [job] for c in fake.calls)


def test_a_held_job_recorded_as_asked_passes_the_read_back(hpc):
    """O57: a held job recorded as asked passes the read back."""
    fake = FakeSlurm()
    slurm = hpc.slurm.Slurm(fake, sleep=lambda s: None)
    asked = allocation(hpc)
    readback = slurm.readback(slurm.submit(asked), asked)
    assert readback["MinMemoryNode"] == "11264M"
    assert fake.cancelled == []


def test_every_step_is_exact_named_sized_and_sees_only_its_allowlist(hpc, monkeypatch):
    """O57: a step is srun --exact (never --overlap) with its name, cores and memory, and env -i with PATH, LC_ALL and
    TMPDIR on the node's tmpfs only: nothing of the login environment reaches it."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/a/socket")
    monkeypatch.setenv("HPC_SECRET_LIKE", "x")
    env = hpc.slurm.allowlist("/usr/bin:/bin", "/tmp")
    step = hpc.slurm.Step("12", "node01", "hpcop-p-zone-facts", 1, 1024, env, ("python3.11", "-I", "x.py"))
    srun = parse_srun(step.argv())
    assert srun.exact
    assert not srun.overlap
    assert "--overlap" not in step.argv()
    assert (srun.node, srun.name, srun.cpus, srun.mem) == ("node01", "hpcop-p-zone-facts", 1, "1024M")
    assert srun.env == ["PATH=/usr/bin:/bin", "LC_ALL=C", "TMPDIR=/tmp"]
    assert srun.command == ["python3.11", "-I", "x.py"]
    assert not any("SSH_AUTH_SOCK" in w or "HPC_SECRET_LIKE" in w for w in step.argv())
    with pytest.raises(hpc.slurm.SlurmError):
        hpc.slurm.allowlist("/usr/bin;rm", "/tmp")


def test_a_hung_client_is_ended_only_once_its_step_has_been_gone_three_polls(hpc):
    """O57: a finished step's srun client can hang on the login node: past its bound it is killed only once its step
    is absent from squeue -s for three polls in a row, and the step's own status comes from the accounting."""
    from hpcslurm import Completed

    fake = FakeSlurm()
    listings = iter([True, False, True, False, False, False])  # absent, then back, then absent three times

    def steps(job):
        return {"hpcop-x-mint": "4242.3"} if next(listings) else {}

    slurm = hpc.slurm.Slurm(fake, poll_s=0.0, sleep=lambda s: None)
    slurm.steps = steps
    slurm.step_status = lambda job, name: 0
    process = FakeProcess(Completed(None, "", ""), hangs=True)
    step = hpc.slurm.Step("4242", "node01", "hpcop-x-mint", 1, 1024, (), ("true",))
    done = slurm._finish(process, step, 0.0)
    assert process.killed
    assert done.returncode == 0


def test_a_control_step_still_listed_at_its_bound_is_cancelled_by_its_id_and_times_out(hpc):
    """O57: a step still running at its bound is cancelled by its own step id, never more, and the call raises a
    TimeoutError (M4's launcher contract)."""
    from hpcslurm import Completed

    fake = FakeSlurm()
    clock = iter(range(0, 10000, 10))
    slurm = hpc.slurm.Slurm(fake, poll_s=0.0, sleep=lambda s: None, clock=lambda: next(clock))
    slurm.steps = lambda job: {"hpcop-x-status": "4242.9"}
    process = FakeProcess(Completed(None, "", ""), hangs=True)
    step = hpc.slurm.Step("4242", "node01", "hpcop-x-status", 1, 1024, (), ("true",))
    with pytest.raises(TimeoutError):
        slurm._finish(process, step, 0.0)
    assert fake.cancelled == ["4242.9"]
    assert process.killed


def test_a_cancel_takes_one_job_id_and_nothing_else(hpc):
    """O57: never scancel -u or by name: a cancel is one job id, or one step id."""
    slurm = hpc.slurm.Slurm(FakeSlurm())
    for bad in ("-u", "me", "12,13", "12 13", ""):
        with pytest.raises(hpc.slurm.SlurmError):
            slurm.cancel(bad)
    with pytest.raises(hpc.slurm.SlurmError):
        slurm.cancel_step("12")
