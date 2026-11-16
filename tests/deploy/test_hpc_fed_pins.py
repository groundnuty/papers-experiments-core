"""O63: the pilot's platform template declares the agent's Apptainer sandbox, and `up` fills its site values from
`site.env`: the SIF and the egress shim, each checked against the SHA-256 the site declares before the cluster is
touched (a pin is a declaration, checked, never a measurement taken instead), the sandbox's work root, and the bundle's
own collector. The rendered file's pins are in the campaign fingerprint, and no site path is. Slurm is the fake of
hpc_fed_support; the core's phase is recorded, or the renderer is run alone."""

import json
import os
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, PILOT_TEMPLATE, SITE, FakeSlurm, agent_site, hpc_on_path, services_only

from expcore_onedata.config import load_platform

VARIABLES = (
    "HPC_AGENT_SIF",
    "HPC_AGENT_SIF_SHA256",
    "HPC_EGRESS_SHIM",
    "HPC_EGRESS_SHIM_SHA256",
    "HPC_AGENT_WORK_ROOT",
)


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture(scope="module")
def pins(hpc):
    import hpcpins  # on the path while the driver's modules are

    return hpcpins


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
    """A driver over the fake Slurm, the core's phase recorded with the values it is handed."""

    def __init__(self, hpc, tmp_path, layout=LAYOUT):
        self.hpc, self.fake, self.root = hpc, FakeSlurm(), tmp_path / "fed" / "root"
        self.driver = hpc.cli.make_driver(layout, str(self.root), runner=self.fake)
        self.driver.sleep = self.driver.slurm.sleep = lambda s: None
        self.driver.poll_s = self.driver.slurm.poll_s = 0.0
        self.driver.relay_available = lambda: False
        self.values = []
        self.driver.core = lambda layout, owner, values, environ=None: self.values.append(values) or "core: linked"

    def up(self, site):
        return self.driver.up(self.hpc.settings.site_from_environment(site))


def render(hpc, pins, root: Path, site: dict, ca: bytes) -> Path:
    """The pilot's platform.toml as up renders it from this site, over invented ids, in a root holding its files."""
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    facts = {
        "zone": {"url": "https://zone.example.org:8443"},
        "study_space": spec.study_space,
        "providers": {p.name: {"url": f"https://{p.name}.example.org:8443", "id": f"pid-{p.name}"}
                      for p in spec.providers},
        "spaces": {s.name: {"id": f"sid-{s.name}"} for s in spec.spaces},
        "principals": {p.name: {"user_id": f"uid-{p.name}"} for p in spec.principals},
    }  # fmt: skip
    (root / "secrets" / "users").mkdir(parents=True)
    for name in ["secrets/admin.token", *(f"secrets/users/{p.name}.pass" for p in spec.principals)]:
        (root / name).write_text("an-invented-secret", encoding="utf-8")
        os.chmod(root / name, 0o600)
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(ca)
    (root / "fault-journal").mkdir()
    settings = hpc.settings.load_settings(spec.hpc)
    values = hpc.boot.hpc_values(
        ["/venv/python", "/repo/hpcfed.py"], settings.stop_bound_s, settings.settle_timeout_s, root
    )
    values |= pins.values(hpc.settings.site_from_environment(site))
    hpc.boot.write_platform(root / "platform.toml", PILOT_TEMPLATE.read_text(encoding="utf-8"), facts, values)
    return root / "platform.toml"


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    """One CA for every render, so two renders differ only where their sites do."""
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


# -- what up hands the core's phase, and what the renderer makes of it ----------------------------------------------


def test_o63_up_hands_the_core_the_sites_declared_pins_paths_and_work_root_and_the_bundles_collector(
    hpc, pins, tmp_path, site
):
    """O63: the core's phase gets each placeholder of the template's [sandbox] as the site declared it, and the
    bundle's own egress collector with the SHA-256 of this checkout's copy."""
    rig = Rig(hpc, tmp_path)
    rig.up(site)
    (values,) = rig.values
    assert values["hpc_agent_sif"] == site["HPC_AGENT_SIF"]
    assert values["hpc_agent_sif_sha"] == site["HPC_AGENT_SIF_SHA256"]
    assert values["hpc_egress_shim"] == site["HPC_EGRESS_SHIM"]
    assert values["hpc_egress_shim_sha"] == site["HPC_EGRESS_SHIM_SHA256"]
    assert values["hpc_agent_work_root"] == site["HPC_AGENT_WORK_ROOT"]
    assert values["hpc_egress_collector"] == str(pins.COLLECTOR)
    assert values["hpc_egress_collector_sha"] == pins.sha256_of(pins.COLLECTOR)
    assert rig.driver.store.read()["sandbox"]["node"] == "node07"  # the template's limits held the agent's node


def test_o63_the_rendered_platform_file_runs_the_apptainer_sandbox_on_the_declared_pins(hpc, pins, tmp_path, site, ca):
    """O63: the rendered [sandbox] loads as the Apptainer backend with the site's work root and files, each pinned to
    its declared SHA-256, and the bundle's collector pinned to its own."""
    config = load_platform(render(hpc, pins, tmp_path / "root", site, ca))
    sandbox = config.sandbox
    assert (sandbox.backend, str(sandbox.work_root)) == ("apptainer", site["HPC_AGENT_WORK_ROOT"])
    app = sandbox.apptainer
    assert (str(app.image.path), app.image.sha256) == (site["HPC_AGENT_SIF"], site["HPC_AGENT_SIF_SHA256"])
    assert (str(app.egress_shim.path), app.egress_shim.sha256) == (
        site["HPC_EGRESS_SHIM"],
        site["HPC_EGRESS_SHIM_SHA256"],
    )
    assert app.egress_collector.sha256 == pins.sha256_of(pins.COLLECTOR)
    assert sandbox.mode_of("mounted").value == "mount"


# -- what up refuses, before the cluster is touched -------------------------------------------------------------------


@pytest.mark.parametrize("variable", VARIABLES)
def test_o63_up_refuses_a_site_that_leaves_a_placeholder_of_the_sandbox_unfilled(hpc, tmp_path, site, variable):
    """O63: each site variable the template's [sandbox] names is required; its absence fails up before any submit."""
    rig = Rig(hpc, tmp_path)
    del site[variable]
    with pytest.raises(RuntimeError, match=f"{variable} is not set"):
        rig.up(site)
    assert not [c for c in rig.fake.calls if c[0] == "sbatch"]
    assert not (rig.root / "hpc-federation.json").exists()


@pytest.mark.parametrize("variable", ["HPC_AGENT_SIF", "HPC_EGRESS_SHIM"])
def test_o63_up_refuses_a_declared_file_that_is_missing_naming_the_variable_never_the_path(
    hpc, tmp_path, site, variable
):
    """O63: a declared file that is not there fails up before any submit, naming the variable and never the path."""
    rig = Rig(hpc, tmp_path)
    Path(site[variable]).unlink()
    with pytest.raises(RuntimeError, match=f"^{variable}: the file it names is missing") as refused:
        rig.up(site)
    assert site[variable] not in str(refused.value)
    assert not [c for c in rig.fake.calls if c[0] == "sbatch"]


@pytest.mark.parametrize("variable", ["HPC_AGENT_SIF", "HPC_EGRESS_SHIM"])
def test_o63_up_refuses_a_file_whose_sha256_is_not_the_declared_one(hpc, tmp_path, site, variable):
    """O63: a pin is a declaration: a file changed since it was declared (one byte here) fails up, naming the pin."""
    rig = Rig(hpc, tmp_path)
    path = Path(site[variable])
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(RuntimeError, match=f"^{variable}_SHA256: the file {variable} names") as refused:
        rig.up(site)
    assert site[variable] not in str(refused.value)
    assert not [c for c in rig.fake.calls if c[0] == "sbatch"]


def test_o63_a_template_that_names_no_pin_needs_none_and_holds_no_agents_node(hpc, tmp_path, site):
    """O63: the pilot's layout with its template's [sandbox] taken out: the services alone, no site variable of the
    agent's sandbox read."""
    rig = Rig(hpc, tmp_path, layout=services_only(tmp_path / "layout"))
    rig.up({k: v for k, v in site.items() if k not in VARIABLES})
    assert rig.driver.store.read()["sandbox"] is None


@pytest.mark.parametrize(
    ("variable", "value", "said"),
    [
        ("HPC_AGENT_SIF_SHA256", "ABC", "64 lower-case hexadecimal digits"),
        ("HPC_EGRESS_SHIM_SHA256", "0" * 63, "64 lower-case hexadecimal digits"),
        ("HPC_AGENT_SIF", "relative/agent.sif", "absolute path"),
        ("HPC_AGENT_WORK_ROOT", "/tmp/../agent", "absolute path"),
    ],
)
def test_o63_a_site_value_of_the_sandbox_that_is_no_pin_or_no_plain_absolute_path_is_refused(
    hpc, site, variable, value, said
):
    """O63: a pin is 64 lower-case hexadecimal digits, a path absolute, plain and without '..'."""
    with pytest.raises(hpc.settings.SettingsError, match=f"^{variable}: .*{said}"):
        hpc.settings.site_from_environment({**site, variable: value})


# -- the fingerprint ---------------------------------------------------------------------------------------------


def other_file(site: dict, variable: str, directory: Path) -> dict:
    """The site with the file a variable names replaced by another one (one byte differs) and its pin declared."""
    import hashlib

    content = Path(site[variable]).read_bytes() + b"another build\n"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / Path(site[variable]).name
    path.write_bytes(content)
    return {**site, variable: str(path), f"{variable}_SHA256": hashlib.sha256(content).hexdigest()}


@pytest.mark.parametrize(
    ("variable", "key"), [("HPC_AGENT_SIF", "image_sha256"), ("HPC_EGRESS_SHIM", "egress_shim_sha256")]
)
def test_o63_two_renders_differing_only_in_a_pin_are_two_campaigns_by_that_pin_alone(
    hpc, pins, tmp_path, site, ca, variable, key
):
    """O63: the platform's half of the campaign fingerprint (`PlatformConfig.fingerprint`, which a study's plugin
    hands the core) holds each pin, so shards run on two builds of the agent's image (or of its shim) are two
    campaigns, which the merge refuses (the rehearsal shows it end to end); the files' paths differ too, and the
    fingerprints differ in the pin alone."""
    first = load_platform(render(hpc, pins, tmp_path / "one", site, ca)).fingerprint()
    changed = other_file(site, variable, tmp_path / "other-build")
    second = load_platform(render(hpc, pins, tmp_path / "two", changed, ca)).fingerprint()
    assert first != second
    assert first["sandbox_backend"][key] == site[f"{variable}_SHA256"]
    assert second["sandbox_backend"][key] == changed[f"{variable}_SHA256"]
    assert {**second, "sandbox_backend": {**second["sandbox_backend"], key: first["sandbox_backend"][key]}} == first


def test_o63_the_fingerprint_holds_no_site_value_but_the_pins_of_the_rendered_file(hpc, pins, tmp_path, site, ca):
    """O63: of everything site.env gives, only the two declared SHA-256s reach the fingerprint, under the sandbox's
    pins, as the rendered file holds them: no path (SIF, shim, work root, SH, L), account or partition."""
    fingerprint = load_platform(render(hpc, pins, tmp_path / "root", site, ca)).fingerprint()
    text = json.dumps(fingerprint, sort_keys=True)
    pinned = {site["HPC_AGENT_SIF_SHA256"], site["HPC_EGRESS_SHIM_SHA256"]}
    for variable, value in site.items():
        if value in pinned:
            assert text.count(value) == 1, variable
        elif variable not in ("HPC_STEP_PATH", "HPC_NODE_TMP"):  # "/usr/bin:/bin" and "/tmp": no site's own words
            assert value not in text, variable
    assert {fingerprint["sandbox_backend"][k] for k in ("image_sha256", "egress_shim_sha256")} == pinned


def test_o63_a_copy_of_the_rendered_file_beside_a_study_reads_the_roots_secrets_ca_and_journal(
    hpc, pins, tmp_path, site, ca
):
    """O63: every path up renders into the root is absolute, so the copy the shard step places beside a study's
    study.toml loads with the root's own admin token, users' directory, CA and fault journal, and gives the platform's
    half of the campaign fingerprint the root's own file gives."""
    from expcore_onedata.fault_settings import load_faults

    root = tmp_path / "root"
    rendered = render(hpc, pins, root, site, ca)
    copy = tmp_path / "node" / "study" / "platform.toml"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(rendered.read_bytes())
    config = load_platform(copy)
    assert (config.admin.kind, config.admin.ref) == ("file", str(root / "secrets" / "admin.token"))
    assert (config.users.kind, config.users.ref) == ("dir", str(root / "secrets" / "users"))
    assert config.tls.ca_file == root / "ca" / "federation-ca.pem"
    assert load_faults(copy, config).journal == root / "fault-journal"
    assert config.fingerprint() == load_platform(rendered).fingerprint()
