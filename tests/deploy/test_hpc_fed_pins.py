"""O63: the pilot's platform template declares the agent's Apptainer sandbox, and `up` fills its site values from
`site.env`: the SIF and the egress shim, each checked against the SHA-256 the site declares before the cluster is
touched (a pin is a declaration, checked, never a measurement taken instead), the sandbox's work root, and the bundle's
own collector. The rendered file's pins are in the campaign fingerprint, and no site path is. Slurm is the fake of
hpc_fed_support; the core's phase is recorded, or the renderer is run alone."""

import json
import os
import sys
from pathlib import Path

import pytest
from hpc_fed_support import (
    LAYOUT,
    PILOT_TEMPLATE,
    SITE,
    FakeSlurm,
    agent_site,
    hpc_on_path,
    manifest_values,
    services_only,
)

from expcore_onedata.config import load_platform

VARIABLES = (
    "HPC_AGENT_SIF",
    "HPC_AGENT_SIF_SHA256",
    "HPC_EGRESS_SHIM",
    "HPC_EGRESS_SHIM_SHA256",
    "HPC_ONECLIENT_BUNDLE",
    "HPC_ONECLIENT_BUNDLE_SHA256",
    "HPC_AGENT_WORK_ROOT",
)
FILES = ("HPC_AGENT_SIF", "HPC_EGRESS_SHIM", "HPC_ONECLIENT_BUNDLE")


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
    values |= pins.values(hpc.settings.site_from_environment(site)) | manifest_values(root)
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
    assert values["hpc_oneclient_bundle"] == site["HPC_ONECLIENT_BUNDLE"]
    assert values["hpc_oneclient_bundle_sha"] == site["HPC_ONECLIENT_BUNDLE_SHA256"]
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
    assert (str(app.oneclient_bundle.path), app.oneclient_bundle.sha256) == (
        site["HPC_ONECLIENT_BUNDLE"],
        site["HPC_ONECLIENT_BUNDLE_SHA256"],
    )  # the mount level's oneclient
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


@pytest.mark.parametrize("variable", FILES)
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


@pytest.mark.parametrize("variable", FILES)
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
    ("variable", "key"),
    [
        ("HPC_AGENT_SIF", "image_sha256"),
        ("HPC_EGRESS_SHIM", "egress_shim_sha256"),
        ("HPC_ONECLIENT_BUNDLE", "oneclient_bundle_sha256"),
    ],
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
    """O63: of everything site.env gives, only the declared SHA-256s reach the fingerprint, under the sandbox's
    pins, as the rendered file holds them: no path (SIF, shim, bundle, work root, SH, L), account or partition."""
    fingerprint = load_platform(render(hpc, pins, tmp_path / "root", site, ca)).fingerprint()
    text = json.dumps(fingerprint, sort_keys=True)
    pinned = {site["HPC_AGENT_SIF_SHA256"], site["HPC_EGRESS_SHIM_SHA256"], site["HPC_ONECLIENT_BUNDLE_SHA256"]}
    for variable, value in site.items():
        if value in pinned:
            assert text.count(value) == 1, variable
        elif variable not in ("HPC_STEP_PATH", "HPC_NODE_TMP"):  # "/usr/bin:/bin" and "/tmp": no site's own words
            assert value not in text, variable
    keys = ("image_sha256", "egress_shim_sha256", "oneclient_bundle_sha256")
    assert {fingerprint["sandbox_backend"][k] for k in keys} == pinned


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


# -- the storage kind of the providers (O57) ---------------------------------------------------------------------------

KIND_LINE = 'provider_storage = "@hpc_provider_storage@"'
PLAIN_SITE = {**SITE, "SH": "/invented/images", "L": "/invented/logs"}  # a tmpfs site, no agent files


def edited_layout(folder: Path, edit) -> Path:
    """The pilot's layout with its template's text passed through `edit`, written into `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / LAYOUT.name).write_text(LAYOUT.read_text(encoding="utf-8"), encoding="utf-8")
    (folder / PILOT_TEMPLATE.name).write_text(edit(PILOT_TEMPLATE.read_text(encoding="utf-8")), encoding="utf-8")
    return folder / LAYOUT.name


def test_o57_the_pilot_template_declares_the_storage_kind_and_up_fills_it_from_the_site(pins, hpc, tmp_path, site, ca):
    """O57: where the providers keep their data is a declared setting of the rendered platform file, `provider_storage`
    in `[sandbox.apptainer]`: `shared_filesystem` for a site with a scratch root, `tmpfs` for one without, the word
    alone, so the root's path is in no value of the fingerprint."""
    assert any(line.split("#")[0].strip() == KIND_LINE for line in PILOT_TEMPLATE.read_text().splitlines())
    scratch = {**site, "HPC_STORAGE_ROOT": str(tmp_path / "invented-scratch-root")}
    for label, declared, kind in (("tmpfs", site, "tmpfs"), ("scratch", scratch, "shared_filesystem")):
        config = load_platform(render(hpc, pins, tmp_path / label, declared, ca))
        assert config.sandbox.apptainer.provider_storage == kind
        fingerprint = config.fingerprint()
        assert fingerprint["sandbox_backend"]["provider_storage"] == kind
        assert "invented-scratch-root" not in json.dumps(fingerprint)
    assert pins.values(hpc.settings.site_from_environment(site))["hpc_provider_storage"] == "tmpfs"
    assert pins.values(hpc.settings.site_from_environment(scratch))["hpc_provider_storage"] == "shared_filesystem"


def test_o57_the_driver_s_two_kinds_are_the_words_the_core_accepts(pins):
    """O57: the driver names the kinds by the same two words the platform layer validates; no other word is rendered."""
    from expcore_onedata.sandbox.apptainer.settings import PROVIDER_STORAGE_KINDS

    assert (pins.TMPFS, pins.SHARED_FILESYSTEM) == PROVIDER_STORAGE_KINDS


@pytest.mark.parametrize(
    "edit",
    [
        lambda text: text.replace(KIND_LINE, ""),  # left out
        lambda text: text.replace(KIND_LINE, "# " + KIND_LINE),  # named only in a comment
        lambda text: text.replace(KIND_LINE, 'provider_storage = "shared_filesystem"'),  # another kind than the site's
    ],
    ids=["left-out", "in-a-comment", "another-kind"],
)
def test_o57_up_refuses_a_template_that_declares_the_sandbox_and_not_the_storage_kind_before_it_submits(
    hpc, tmp_path, site, edit
):
    """O57: a template whose `[sandbox.apptainer]` leaves the kind out, names it only in a comment or hard-codes the
    other kind would give a scratch federation the fingerprint of a tmpfs one (the site here has no scratch root): `up`
    refuses it before anything is submitted, naming the key."""
    rig = Rig(hpc, tmp_path, layout=edited_layout(tmp_path / "layout", edit))
    with pytest.raises(RuntimeError, match="provider_storage"):
        rig.up(site)
    assert not [c for c in rig.fake.calls if c[0] == "sbatch"]
    assert not (rig.root / "hpc-federation.json").exists()


LITERAL_SANDBOX = f'[sandbox.apptainer]\nimage = "/invented/agent.sif"\nimage_sha256 = "{"a" * 64}"\n'


@pytest.mark.parametrize(
    "template",
    [
        LITERAL_SANDBOX,  # literal pins, no placeholder at all: the preflight still sees the sandbox
        LITERAL_SANDBOX + '# provider_storage = "@hpc_provider_storage@"\n',
        LITERAL_SANDBOX + 'provider_storage = "scratch"\n',
        LITERAL_SANDBOX + 'provider_storage = "shared_filesystem"\n',  # not this site's
        "[sandbox.apptainer.gate_options]\nkey = 1\n",  # a subtable declares the table
        'sandbox.apptainer.image = "/invented/agent.sif"\n',  # a dotted key too
        LITERAL_SANDBOX + '[metadata]\nprovider_storage = "tmpfs"\n',  # the key of another table declares nothing
    ],
    ids=["literal-pins", "comment-only", "unknown-word", "other-kind", "subtable", "dotted-key", "another-table"],
)
def test_o57_the_preflight_reads_the_template_s_declaration_not_its_placeholders(pins, hpc, template):
    """O57: `check` looks for the declaration itself: a template of literal pins is refused like one of placeholders."""
    tmpfs_site = hpc.settings.site_from_environment(PLAIN_SITE)
    with pytest.raises(RuntimeError, match="provider_storage"):
        pins.check(tmpfs_site, template)


@pytest.mark.parametrize(
    "template",
    [
        LITERAL_SANDBOX + 'provider_storage = "tmpfs"  # this site\'s own\n',
        LITERAL_SANDBOX + 'provider_storage = "@hpc_provider_storage@"\n',
        'zone = { name = "zone" }\n# [sandbox.apptainer] is in a comment here\n',  # no sandbox declared
        LITERAL_SANDBOX + "provider_storage = 'tmpfs'\n",  # a literal string is TOML's too
        'sandbox.apptainer.image = "x"\nsandbox.apptainer.provider_storage = "tmpfs"\n',  # dotted keys at the root
        'sandbox = { apptainer = { image = "x", provider_storage = "tmpfs" } }\n',  # inline tables
    ],
    ids=["its-own-word", "placeholder", "no-sandbox", "literal-string", "dotted-keys", "inline-tables"],
)
def test_o57_the_preflight_accepts_the_site_s_own_kind_and_a_template_with_no_sandbox(pins, hpc, template):
    """O57: the placeholder and the site's own word declare the kind; a template that declares no Apptainer sandbox is
    not asked for one."""
    pins.check(hpc.settings.site_from_environment(PLAIN_SITE), template)


def test_o57_a_template_with_no_sandbox_declares_no_kind_and_needs_none(hpc, tmp_path, site):
    """O57: the services alone (no `[sandbox]`, so no run to fingerprint) are not asked for a kind."""
    rig = Rig(hpc, tmp_path, layout=services_only(tmp_path / "layout"))
    rig.up({k: v for k, v in site.items() if k not in VARIABLES})
    assert rig.driver.store.read()["sandbox"] is None


@pytest.mark.parametrize("declared", ['"scratch"', '"shared_filesystem"'])
def test_o57_a_rendered_file_that_declares_another_kind_than_the_site_s_is_refused(
    hpc, pins, tmp_path, site, ca, monkeypatch, declared
):
    """O57: the renderer reads the file it renders back: a template that hard-codes a kind the site does not have (the
    site here has no scratch root, so its kind is `tmpfs`) never reaches the campaign, whatever the word."""
    folder = tmp_path / "template"
    folder.mkdir()
    hard_coded = folder / PILOT_TEMPLATE.name
    hard_coded.write_text(PILOT_TEMPLATE.read_text(encoding="utf-8").replace('"@hpc_provider_storage@"', declared))
    monkeypatch.setattr(sys.modules[__name__], "PILOT_TEMPLATE", hard_coded)
    with pytest.raises(RuntimeError, match="provider_storage"):
        render(hpc, pins, tmp_path / "root", site, ca)
    assert not (tmp_path / "root" / "platform.toml").exists()  # nothing a campaign could read was left behind


def test_o57_a_template_that_is_no_toml_once_filled_in_is_left_to_the_renderer(pins, hpc):
    """O57: the preflight cannot read a template whose placeholder stands where no value does (`key = @x@ @y@`): it
    raises nothing, and the renderer's read-back of the file it writes is the check that stands."""
    pins.check(hpc.settings.site_from_environment(PLAIN_SITE), "[sandbox.apptainer]\nkey = @x@ @y@\n")


@pytest.mark.parametrize("kind", ["tmpfs", "shared_filesystem"])
def test_o57_every_shipped_template_is_toml_once_filled_in_and_the_pilot_s_declares_the_kind(pins, kind):
    """O57: the preflight really reads the templates this repository ships (none is silently skipped as no TOML), and
    the pilot's, filled in for a site, declares that site's kind in `[sandbox.apptainer]`."""
    import tomllib

    for template in sorted(PILOT_TEMPLATE.parent.glob("*.platform.toml.template")):
        document = tomllib.loads(pins.filled_in(template.read_text(encoding="utf-8"), kind))
        declared = document.get("sandbox", {}).get("apptainer", {}).get("provider_storage")
        assert declared == (kind if template == PILOT_TEMPLATE else None), template.name
