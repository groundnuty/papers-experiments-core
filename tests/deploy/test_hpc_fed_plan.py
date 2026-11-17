"""The HPC federation driver's plan (deploy/hpc-federation, DESIGN.md): the layout's [hpc] table and the held job's
shape, the pilot-shape layout's ten link pairs, the state file, and how every Slurm command is formed and read back.
Nothing here reaches a cluster: Slurm is the fake of hpc_fed_support."""

import json
import math
import os
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, FakeProcess, FakeSlurm, hpc_on_path, manifest_values, parse_srun, pin_values

from expcore_onedata.config import load_platform
from expcore_onedata.fault_settings import FaultSettings, restore_token_margin_s
from expcore_onedata.linksettings import link_pairs


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


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
    assert with_agent.node() == hpc.settings.Size(5, 11264)  # the agent's node never sets the service nodes' shape
    assert with_agent.sandbox_node() == hpc.settings.Size(9, 18432)


def test_billing_is_the_larger_of_cores_and_the_weight_times_the_memory_per_node(hpc):
    """O57: what describe prints: nodes x max(cores, weight x GB), from the layout's sizes."""
    settings = hpc.settings.load_settings(None)
    assert settings.billing_per_hour(6, 0.5) == 6 * 5.5
    assert settings.billing_per_hour(6, 0.25) == 6 * 5


def test_the_settle_time_exceeds_the_stops_whole_bound(hpc):
    """O57: a stop takes at most stop_seconds, the 30 s SIGKILL tail and 60 s for its step to leave: settle_timeout_s
    is above that, derived from the layout, never a constant."""
    assert hpc.settings.load_settings(None).settle_timeout_s == 450
    settings = hpc.settings.load_settings({"stop_seconds": 120, "step_launch_wait_s": 100})
    assert (settings.stop_bound_s, settings.settle_timeout_s) == (210, 370)


LAYOUTS = sorted(LAYOUT.parent.glob("*.toml"))


@pytest.mark.parametrize("layout", LAYOUTS, ids=lambda path: path.name)
def test_every_committed_layout_stops_a_service_within_50_s_and_holds_the_job_for_down(hpc, layout):
    """O58: three provider outages on one HPC federation (17 Nov) ended a service 26.1 to 33.1 s after its SIGTERM, so
    every committed layout's graceful stop is 50 s (the largest plus 50 %), whether it declares `stop_seconds` or takes
    the default: the service's stop bound is 140 s, a control step may wait 240 s to start (a controller's
    message timeout, 200 s, plus 20 %), so settle_timeout_s is 450 s, and the held job's time still exceeds twice the
    stop verb's whole bound (380 s), which the loader requires for `down` to finish."""
    import tomllib

    assert LAYOUTS  # the glob found the committed layouts
    settings = hpc.settings.load_settings(tomllib.loads(layout.read_text(encoding="utf-8")).get("hpc"))
    assert settings.stop_seconds == 50
    assert (settings.stop_bound_s, settings.step_launch_wait_s, settings.settle_timeout_s) == (140, 240, 450)
    assert settings.stop_verb_bound_s == 380
    assert settings.time_s > 2 * settings.stop_verb_bound_s


MEASURED = {"stop_seconds": ("17 Nov", "33.1 s", "50 %"), "step_launch_wait_s": ("17 Nov", "103 s", "200 s", "20 %")}


@pytest.mark.parametrize("layout", LAYOUTS, ids=lambda path: path.name)
def test_a_layout_that_declares_a_measured_bound_says_where_the_figure_comes_from(layout):
    """O58: a declared `stop_seconds` or `step_launch_wait_s` has its measurement in the comment line above it, so a
    later change is made from a measurement and not from the number."""
    lines = layout.read_text(encoding="utf-8").splitlines()
    declared = [(n, key) for n, line in enumerate(lines) for key in MEASURED if line.startswith(f"{key} ")]
    for number, key in declared:
        assert lines[number - 1].startswith(f"# {key}:")
        assert all(words in lines[number - 1] for words in MEASURED[key])
    if "hpc" in tomllib.loads(layout.read_text(encoding="utf-8")):
        assert {key for _, key in declared} == set(MEASURED)


@pytest.mark.parametrize(
    ("table", "words"),
    [
        ({"zone": {"cpus": 0, "memory": "8g"}}, "hpc.zone.cpus"),
        ({"provider": {"cpus": 4, "memory": "10 GB"}}, "hpc.provider.memory"),
        ({"control": {"cpus": 1}}, "hpc.control.memory: is required"),
        ({"time": "2h"}, "hpc.time"),
        ({"time": "00:20:00", "stop_seconds": 600}, "must exceed twice the stop's bound"),
        ({"time": "00:10:00"}, "must exceed twice the stop's bound"),
        ({"core": "elsewhere"}, "hpc.core"),
        ({"nodes": 3}, "hpc.nodes: is not a key"),
        ({"step_launch_wait_s": -1}, "hpc.step_launch_wait_s"),
        ({"step_launch_wait_s": 3601}, "hpc.step_launch_wait_s"),
        ({"step_launch_wait_s": 90.5}, "hpc.step_launch_wait_s"),
    ],
    ids=[
        "no cores",
        "bad memory",
        "missing memory",
        "bad time",
        "too short",
        "too short for the launch wait",
        "bad core place",
        "unknown key",
        "negative launch wait",
        "huge launch wait",
        "fractional launch wait",
    ],
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
    assert (site.node_tmp, site.step_path) == ("/tmp", "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin")
    assert hpc.settings.Site.from_json(site.to_json()) == site
    with pytest.raises(hpc.settings.SettingsError, match="HPC_PARTITION is not set"):
        hpc.settings.site_from_environment({**environ, "HPC_PARTITION": "<partition>"})
    with pytest.raises(hpc.settings.SettingsError, match="HPC_NODE_TMP"):
        hpc.settings.site_from_environment({**environ, "HPC_NODE_TMP": "tmp"})


@pytest.mark.parametrize("python", ["venv/bin/python", "./python3.11", "bin/python3"])
def test_o57_a_node_python_that_is_a_relative_path_is_refused(hpc, python):
    """O57: every step starts in `/` (`--chdir=/`), so a relative HPCOP_PY would name another interpreter than the
    one meant: it must be a command name, found on the step's PATH, or an absolute path."""
    environ = {"HPC_ACCOUNT": "a", "HPC_PARTITION": "p", "SH": "/site/images", "L": "/site/logs"}
    assert hpc.settings.site_from_environment({**environ, "HPCOP_PY": "python3.11"}).node_python == "python3.11"
    assert hpc.settings.site_from_environment({**environ, "HPCOP_PY": "/opt/py/bin/python3"}).node_python
    with pytest.raises(hpc.settings.SettingsError, match="HPCOP_PY: must be a command name or an absolute path"):
        hpc.settings.site_from_environment({**environ, "HPCOP_PY": python})


SITE_ENVIRON = {
    "HPC_ACCOUNT": "test-account",
    "HPC_PARTITION": "test-partition",
    "SH": "/site/images",
    "L": "/site/logs",
}


def test_the_site_may_name_a_network_option_for_every_step_and_by_default_names_none(hpc):
    """O57: HPC_STEP_NETWORK is optional and empty by default, so a site that sets nothing is unchanged; the value is
    kept as given, recorded in the state's site record, and a record written before the key existed still loads."""
    assert hpc.settings.site_from_environment(SITE_ENVIRON).step_network == ""
    assert hpc.settings.site_from_environment({**SITE_ENVIRON, "HPC_STEP_NETWORK": ""}).step_network == ""
    for value in ("no_vni", "single_node_vni,job_vni", "def_acs=100"):
        site = hpc.settings.site_from_environment({**SITE_ENVIRON, "HPC_STEP_NETWORK": value})
        assert site.step_network == value
        assert hpc.settings.Site.from_json(site.to_json()) == site
    old = hpc.settings.site_from_environment(SITE_ENVIRON).to_json()
    del old["step_network"]
    assert hpc.settings.Site.from_json(old).step_network == ""


@pytest.mark.parametrize(
    "value",
    ["no vni", "no_vni ", " no_vni", "no_vni;true", "$(id)", "`id`", "a|b", "a&b", "a>b", "a<b", "'x'", '"x"', "a\\b",
     "a\nb", "a\tb", "<network>", "--exec", "-x", "a" * 129, "no_vni\x00"],
    ids=lambda value: repr(value)[:20],
)  # fmt: skip
def test_a_step_network_with_a_space_or_a_shell_character_is_refused_naming_the_variable_not_the_value(hpc, value):
    """O57: the value reaches an srun argv, so it is a short option string of letters, digits and _ . , = : + - only;
    the message names HPC_STEP_NETWORK and never repeats what was given, and the state's record is held to the same."""
    with pytest.raises(hpc.settings.SettingsError, match="HPC_STEP_NETWORK") as refused:
        hpc.settings.site_from_environment({**SITE_ENVIRON, "HPC_STEP_NETWORK": value})
    assert value not in str(refused.value)
    recorded = hpc.settings.site_from_environment(SITE_ENVIRON).to_json()
    with pytest.raises(hpc.settings.SettingsError, match="HPC_STEP_NETWORK"):
        hpc.settings.Site.from_json({**recorded, "step_network": value})


@pytest.mark.parametrize("value", [None, False, True, 0, 5, [], ["no_vni"], {}, {"no_vni": 1}, b"no_vni"], ids=repr)
def test_a_step_network_in_the_state_that_is_not_a_string_is_refused_naming_the_variable(hpc, value):
    """O57: a state record is read back as data, so `step_network` must be a string: a false, a null, a number or a
    list is no option and no empty one, and is refused naming HPC_STEP_NETWORK (never read as none, never a TypeError);
    a step handed one refuses it too."""
    recorded = hpc.settings.site_from_environment(SITE_ENVIRON).to_json()
    with pytest.raises(hpc.settings.SettingsError, match="HPC_STEP_NETWORK"):
        hpc.settings.Site.from_json({**recorded, "step_network": value})
    step = hpc.slurm.Step("4242", "node01", "hpcop-x-zone-facts", 1, 1024, (), ("true",), het_group=0, network=value)
    with pytest.raises(hpc.slurm.SlurmError, match="HPC_STEP_NETWORK"):  # a falsy one is no "no option" to its guard
        step.argv()


def test_a_step_names_a_network_option_only_when_it_has_one(hpc):
    """O57: without one the step's whole srun line is what it was before the setting existed (an empty value is none);
    with one, that line plus `--network=<value>` after `--chdir=/`, so the prefix every test and tool slices
    (`--jobid`, `--het-group`, `--exact`) does not move; a value an argv must not carry is refused."""
    env = hpc.slurm.allowlist("/usr/bin:/bin", "/tmp")
    plain = hpc.slurm.Step("4242", "node01", "hpcop-x-zone-facts", 1, 1024, env, ("python3.11", "x.py"), het_group=0)
    wanted = ["srun", "--jobid=4242", "--het-group=0", "--exact", "-N1", "-n1", "-w", "node01", "-c", "1",
              "--mem=1024M", "-J", "hpcop-x-zone-facts", "--export=PATH", "--chdir=/", "/usr/bin/env", "-i", *env,
              "python3.11", "x.py"]  # fmt: skip
    assert plain.argv() == wanted
    assert replace(plain, network="").argv() == wanted
    command = wanted.index("/usr/bin/env")
    named = replace(plain, network="no_vni").argv()
    assert named == [*wanted[:command], "--network=no_vni", *wanted[command:]]
    assert parse_srun(named).network == "no_vni"
    assert parse_srun(wanted).network is None
    for bad in ("no vni", "a;b", "$x", "-x"):
        with pytest.raises(hpc.slurm.SlurmError, match="HPC_STEP_NETWORK"):
            replace(plain, network=bad).argv()


def test_the_default_step_path_holds_the_sbin_directories_where_the_sites_nodes_keep_ss(hpc):
    """O57: under env -i a step finds its tools on the allowlist's PATH alone; the site's nodes keep ss (the bundle's
    port checks) in /usr/sbin, and the first live zone step failed in its first second without it."""
    path = hpc.settings.Site("a", "p", "/i", "/l").step_path.split(":")
    assert path[:3] == ["/usr/local/bin", "/usr/bin", "/bin"]  # the bins first, as before
    assert {"/usr/local/sbin", "/usr/sbin"} <= set(path)
    env = dict(word.split("=", 1) for word in hpc.slurm.allowlist(":".join(path), "/tmp"))
    assert env["PATH"] == ":".join(path)


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
    values = {
        **hpc.boot.hpc_values(command, settings.stop_bound_s, settings.settle_timeout_s, root),
        **pin_values(root / "agent"),
        **manifest_values(root),
    }
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


def test_the_ca_kept_from_an_export_that_holds_its_key_first_loads_as_the_platforms_ca_file(hpc, tmp_path, make_ca):
    """O57: a real CA exported as the image's file holds it, its private key before its certificate: the core refuses
    the export whole as `ca_file` (its backstop), and loads the certificates `up` keeps of it."""
    import hpcca

    from expcore.config.loading import ConfigError

    path = rendered_pilot(hpc, tmp_path, make_ca)
    made, kept = tmp_path / "made", tmp_path / "ca" / "federation-ca.pem"
    exported = (made / "ca.key").read_text(encoding="utf-8") + (made / "ca.pem").read_text(encoding="utf-8")
    assert "PRIVATE KEY" in exported
    kept.write_text(exported, encoding="utf-8")
    with pytest.raises(ConfigError, match="private key"):
        load_platform(path)
    kept.write_text(hpcca.ca_certificates(exported), encoding="utf-8")
    assert load_platform(path).tls.ca_file == kept.resolve()


def test_the_platform_declares_the_hpc_backend_and_its_stop_bound_and_no_compose_keys(hpc, tmp_path, make_ca):
    """O57: up renders [faults] for the HPC backend (M4): backend "hpc", stop_bound_s from the layout (stop_seconds +
    90 s), the journal in the root and every provider as a service, with none of the Compose keys it refuses."""
    import tomllib

    faults = tomllib.loads(rendered_pilot(hpc, tmp_path, make_ca).read_text(encoding="utf-8"))["faults"]
    assert faults["backend"] == "hpc"
    assert (faults["stop_bound_s"], faults["step_launch_wait_s"], faults["command_timeout_s"]) == (140, 240, 300)
    assert faults["journal"] == str(tmp_path / "fault-journal")  # absolute: a copy beside a study names the root's
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
    monkeypatch.setattr(hpc.boot, "has_hpc_backend", lambda: True)
    document = tomllib.loads(rendered_pilot(hpc, tmp_path, make_ca).read_text(encoding="utf-8"))
    assert seen == [tmp_path / "platform.toml"]
    assert document["tokens"]["margin_s"] == math.ceil(restore_token_margin_s(loaded))


def test_with_this_cores_loader_the_pilot_shapes_least_margin_is_the_hpc_restores_rule(hpc, tmp_path, make_ca):
    """O58: with this core's [faults] loader (backend "hpc", M4), up's [tokens] margin_s is the loader's own rule over
    the file as written, `restore_token_margin_s` of what `load_faults` reads, with nothing stubbed: 5,370 s for the
    pilot shape's defaults (stop_seconds 50, step_launch_wait_s 240, command_timeout_s 300), the restore's bound, the
    slack, the drain and the start's own stop."""
    import tomllib

    from expcore_onedata.fault_settings import load_faults

    assert hpc.boot.has_hpc_backend() is True
    path = rendered_pilot(hpc, tmp_path, make_ca)
    settings = load_faults(path, load_platform(path))
    assert (settings.backend, settings.stop_bound_s, settings.settle_timeout_s) == ("hpc", 140.0, 450.0)
    margin = tomllib.loads(path.read_text(encoding="utf-8"))["tokens"]["margin_s"]
    assert margin == math.ceil(restore_token_margin_s(settings)) == 5370


def test_a_core_without_the_hpc_backend_takes_the_restores_rule_with_the_hpc_settle_and_poll(
    hpc, tmp_path, make_ca, monkeypatch
):
    """O57: a core whose loader cannot read the HPC [faults] (no backend "hpc" yet) still gets a margin: the restore's
    rule over the keys it knows, with the HPC settle time (stop bound + launch wait + 60 s) and poll (10 s), and the
    relay verb bound `up` declares (300 s, a key every core knows). Such a core's settings know none of the HPC
    backend's own keys (O58's `backend`, `stop_bound_s`, `relay_wait_s` and `step_launch_wait_s`), so they are left out
    here as that core leaves them out."""
    import tomllib

    monkeypatch.setattr(hpc.boot, "has_hpc_backend", lambda: False)
    monkeypatch.setattr(
        hpc.boot,
        "NOT_A_BOUND",
        hpc.boot.NOT_A_BOUND | {"backend", "stop_bound_s", "relay_wait_s", "step_launch_wait_s"},
    )
    document = tomllib.loads(rendered_pilot(hpc, tmp_path, make_ca).read_text(encoding="utf-8"))
    rule = FaultSettings(
        file=Path("p"), sanctioned=False, journal=Path("j"), compose_file=Path("c"), services={},
        settle_timeout_s=450.0, poll_s=10.0, command_timeout_s=300.0,
    )  # fmt: skip
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


def test_a_platform_the_hpc_loader_refuses_fails_up_instead_of_passing(hpc, tmp_path, make_ca, monkeypatch):
    """O57: with a core whose loader knows the HPC backend, its refusal of the rendered [faults] fails up: a
    platform.toml the campaign would refuse at load is never reported ready."""
    import expcore_onedata.fault_settings as fault_settings
    from expcore.config.loading import ConfigError

    def refuse(path, config):
        raise ConfigError("platform.toml: [faults]: stop_bound_s is too small")

    monkeypatch.setattr(fault_settings, "load_faults", refuse)
    monkeypatch.setattr(hpc.boot, "has_hpc_backend", lambda: True)
    with pytest.raises(ConfigError, match="stop_bound_s"):
        rendered_pilot(hpc, tmp_path, make_ca)


def test_a_step_a_signal_ended_never_reads_as_a_success(hpc):
    """O57: the accounting's ExitCode is status:signal; a step killed by a signal (0:9) reads as 128 + 9."""
    fake = FakeSlurm()
    rows = "4242.3|hpcop-x-core|0:9\n4242.4|hpcop-x-check|0:0\n4242.5|hpcop-x-mint|2:0\n"
    fake.run = lambda argv, timeout: hpc.slurm.Completed(0, rows, "") if argv[0] == "sacct" else None
    slurm = hpc.slurm.Slurm(fake)
    assert slurm.step_status("4242", "hpcop-x-core") == 137
    assert slurm.step_status("4242", "hpcop-x-check") == 0
    assert slurm.step_status("4242", "hpcop-x-mint") == 2


def test_a_pipe_whose_first_step_fails_still_ends_the_second(hpc):
    """O57: a token's mint that times out never leaves the delivery's client behind: both ends are finished, the
    second killed, and the first failure raised."""
    from hpcslurm import Completed

    fake = FakeSlurm()
    left = FakeProcess(Completed(None, "", ""), hangs=True)
    right = FakeProcess(Completed(None, "", ""), hangs=True)
    fake.pipe = lambda first, second: (left, right)
    clock = iter(range(0, 10**6, 10))
    slurm = hpc.slurm.Slurm(fake, poll_s=0.0, sleep=lambda s: None, clock=lambda: next(clock))
    slurm.steps = lambda job: {"hpcop-x-mint": "4242.7", "hpcop-x-deliver": "4242.8"}
    first = hpc.slurm.Step("4242", "node01", "hpcop-x-mint", 1, 1024, (), ("true",))
    second = hpc.slurm.Step("4242", "node02", "hpcop-x-deliver", 1, 1024, (), ("true",))
    with pytest.raises(TimeoutError):
        slurm.control_pipe(first, second, 0.0)
    assert left.killed
    assert right.killed
    assert fake.cancelled == ["4242.7", "4242.8"]


def test_the_real_runner_pipes_waits_kills_and_ends_a_detached_client(hpc, tmp_path):
    """O57: the login node's Runner, on local commands: a pipe carries its payload, a bound that passes leaves the
    process for a kill, a detached client is alive by pid and start tick, and its end is verified."""
    runner = hpc.slurm.Runner()
    left, right = runner.pipe(["printf", "x"], ["cat"])
    assert left.wait(10)
    assert right.wait(10)
    assert right.result().stdout == "x"
    slow = runner.start(["sleep", "30"])
    assert not slow.wait(0.1)
    slow.kill()
    assert slow.result().returncode is not None
    client = runner.spawn(["sleep", "30"], str(tmp_path / "client.log"))
    assert runner.client_alive(client)
    assert oct((tmp_path / "client.log").stat().st_mode & 0o777) == "0o600"
    assert runner.end_client(client, grace_s=5.0)
    assert not runner.client_alive(client)


def test_the_clients_of_a_job_are_this_users_srun_processes_naming_it(hpc, tmp_path):
    """O57: down finds an unrecorded client by its argv: an srun of our uid naming --jobid=<job>, nothing else."""
    proc = tmp_path / "proc"

    def process(pid, argv):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (proc / str(pid) / "stat").write_text(f"{pid} (x) S " + " ".join(["0"] * 18) + " 777 0\n", encoding="ascii")

    process(101, ["/usr/bin/srun", "--jobid=4242", "--exact", "-J", "hpcop-x-run-1"])
    process(102, ["/usr/bin/srun", "--jobid=4243", "--exact"])
    process(103, ["/usr/bin/sleep", "--jobid=4242"])
    found = hpc.slurm.clients_of("4242", str(proc))
    assert found == [hpc.slurm.Client(101, 777)]


def test_storages_are_found_by_name_and_each_maps_its_users(hpc):
    """O57: an owner of an imported space has two storages: its support of that space names the import storage, every
    other support finish.sh's posix storage, by name, never by place in a list."""
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    boot = hpc.boot.HpcBootstrap.__new__(hpc.boot.HpcBootstrap)
    boot.storages = lambda service: {"import-alpha-space": ("st-import", {}), "posix-local": ("st-posix", {})}
    owner = spec.provider("larch")
    plain = spec.space("alpha-space")
    imported = replace(plain, imported=object())
    assert boot.storage_for(owner, plain) == "st-posix"
    assert boot.storage_for(owner, imported) == "st-import"
    assert boot.storage_for(spec.provider("aspen"), imported) == "st-posix"
    boot.storages = lambda service: {"posix": ("st-other", {})}
    with pytest.raises(RuntimeError, match="no storage named posix-local"):
        boot.storage_for(owner, plain)
