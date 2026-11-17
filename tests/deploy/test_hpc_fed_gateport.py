"""O63: an HPC federation's REST arm. The committed REST layout (`layouts/pilot-shape-rest.toml`) is the pilot's with
the three things a REST level needs: `[sandbox] content_check_file`, a REST level beside the mount one, and the 443
mapper (`bundle/gateport.c`, built for the agent's SIF) as a pinned `[sandbox.apptainer] preload`. The mapper's file and
its SHA-256 come from `site.env` (`HPC_GATEPORT`, `HPC_GATEPORT_SHA256`) as the egress shim's do: `up` refuses a
template that names them without the keys, and a file that is missing or has another SHA-256, before it submits
anything; the rendered file carries the declared SHA-256, which the sandbox checks at every start. Slurm is the fake of
hpc_fed_support."""

import hashlib
import json
import os
import tomllib
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
)

from expcore_onedata.config import load_platform

REST_LAYOUT = LAYOUT.parent / "pilot-shape-rest.toml"
REST_TEMPLATE = LAYOUT.parent / "pilot-shape-rest.platform.toml.template"
REST_LEVEL = "rest"  # the data-path level the REST template adds beside the pilot's `mounted`
GATEPORT = b"an invented 443 mapper\n"
VARIABLES = ("HPC_GATEPORT", "HPC_GATEPORT_SHA256")


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
    """A site that declares the agent's files and the 443 mapper."""
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent", gateport=GATEPORT), "SH": str(images), "L": str(logs)}


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


class Rig:
    """A driver over the fake Slurm, the core's phase recorded with the values it is handed."""

    def __init__(self, hpc, tmp_path, layout=REST_LAYOUT):
        self.hpc, self.fake, self.root = hpc, FakeSlurm(), tmp_path / "fed" / "root"
        self.driver = hpc.cli.make_driver(layout, str(self.root), runner=self.fake)
        self.driver.sleep = self.driver.slurm.sleep = lambda s: None
        self.driver.poll_s = self.driver.slurm.poll_s = 0.0
        self.driver.relay_available = lambda: False
        self.values = []
        self.driver.core = lambda layout, owner, values, environ=None: self.values.append(values) or "core: linked"

    def up(self, site):
        return self.driver.up(self.hpc.settings.site_from_environment(site))

    def submitted(self) -> list:
        return [c for c in self.fake.calls if c[0] == "sbatch"]


def render(hpc, pins, root: Path, site: dict, ca: bytes, layout: Path = REST_LAYOUT) -> Path:
    """`layout`'s platform.toml as up renders it from this site, over invented ids, in a root holding its files."""
    import fedspec

    spec = fedspec.load_spec(layout)
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
    values |= pins.values(hpc.settings.site_from_environment(site)) | manifest_values(root, layout)
    template = spec.platform_template.read_text(encoding="utf-8")
    hpc.boot.write_platform(root / "platform.toml", template, facts, values)
    return root / "platform.toml"


def declared(pins, path: Path) -> dict:
    """A template as a TOML document, its placeholders filled in as the preflight reads it."""
    return tomllib.loads(pins.filled_in(path.read_text(encoding="utf-8"), "tmpfs"))


# -- the committed REST layout -------------------------------------------------------------------------------------


def test_o63_the_rest_layout_is_the_pilots_but_for_its_platform_template():
    """O63: `layouts/pilot-shape-rest.toml` is a copy of `pilot-shape.toml` that names its own platform template: the
    federation, its spaces, principals and `[hpc]` table are the pilot's, so the REST arm differs in the agent's
    sandbox alone."""
    rest, pilot = tomllib.loads(REST_LAYOUT.read_text("utf-8")), tomllib.loads(LAYOUT.read_text("utf-8"))
    assert rest["platform_template"] == REST_TEMPLATE.name
    assert pilot["platform_template"] == PILOT_TEMPLATE.name
    rest["platform_template"] = pilot["platform_template"]
    assert rest == pilot


def test_o63_the_rest_template_is_the_pilots_with_a_check_file_a_rest_level_and_the_mappers_preload(pins):
    """O63: read as documents, the two templates differ in exactly three declarations, the ones a REST arm adds:
    `[sandbox] content_check_file`, a REST level beside the mount level, and the preload of the 443 mapper. A change
    to one template that the other does not follow fails here (O68's rule for the separate layout)."""
    rest, pilot = declared(pins, REST_TEMPLATE), declared(pins, PILOT_TEMPLATE)
    assert rest["sandbox"].pop("content_check_file")
    assert rest["sandbox"]["data_paths"].pop(REST_LEVEL) == "rest"
    assert rest["sandbox"]["apptainer"].pop("preload") == [{"path": "0", "sha256": "0"}]  # both placeholders, as 0
    assert rest == pilot
    assert pilot["sandbox"]["data_paths"] == {"mounted": "mount"}


def test_o63_the_rest_template_names_the_mapper_and_its_pin_and_the_pilots_names_neither(pins):
    """O63: `@hpc_gateport@` and `@hpc_gateport_sha@` are in the REST template (so `up` reads the site's two keys for
    it) and in no template of a layout with no REST level, comments included (a placeholder in a comment counts)."""
    assert {"hpc_gateport", "hpc_gateport_sha"} <= set(pins.named(REST_TEMPLATE.read_text("utf-8")))
    for template in sorted(REST_TEMPLATE.parent.glob("*.platform.toml.template")):
        if template != REST_TEMPLATE:
            assert not {"hpc_gateport", "hpc_gateport_sha"} & set(pins.named(template.read_text("utf-8"))), template


def test_o63_the_rest_templates_check_file_is_one_the_layouts_own_baseline_provisions_in_its_first_space(pins, hpc):
    """O63: the pre-campaign check reads `content_check_file` under the first space the data path reaches, so it names a
    file of the study space's baseline (up writes it), and the study space is the template's first `[[spaces]]`."""
    import fedlayout
    import fedspec

    spec = fedspec.load_spec(REST_LAYOUT)
    document = declared(pins, REST_TEMPLATE)
    check_file = document["sandbox"]["content_check_file"]
    assert check_file in {f.path for f in fedlayout.baseline_plan(spec)[spec.study_space]}
    assert check_file == fedlayout.CONTENT_FILE
    assert document["spaces"][0]["name"] == spec.study_space


# -- the placeholders fill from site.env ------------------------------------------------------------------------------


def test_o63_the_mappers_placeholders_fill_from_site_env(hpc, pins, site):
    """O63: `hpc_gateport` is the file `HPC_GATEPORT` names and `hpc_gateport_sha` the SHA-256 `HPC_GATEPORT_SHA256`
    declares, as for every other pin."""
    values = pins.values(hpc.settings.site_from_environment(site))
    assert values["hpc_gateport"] == site["HPC_GATEPORT"]
    assert values["hpc_gateport_sha"] == site["HPC_GATEPORT_SHA256"] == hashlib.sha256(GATEPORT).hexdigest()


def test_o63_a_site_that_declares_no_mapper_has_no_value_for_it(hpc, pins, site):
    """O63: no `HPC_GATEPORT` is no value (the renderer then refuses a template that names the placeholder, never
    writes an empty path)."""
    values = pins.values(hpc.settings.site_from_environment({k: v for k, v in site.items() if k not in VARIABLES}))
    assert "hpc_gateport" not in values
    assert "hpc_gateport_sha" not in values


def test_o63_up_hands_the_core_the_mappers_path_and_pin(hpc, tmp_path, site):
    """O63: the REST layout's `up` passes the core's phase both placeholders as the site declared them."""
    rig = Rig(hpc, tmp_path)
    rig.up(site)
    (values,) = rig.values
    assert values["hpc_gateport"] == site["HPC_GATEPORT"]
    assert values["hpc_gateport_sha"] == site["HPC_GATEPORT_SHA256"]
    assert rig.submitted()


@pytest.mark.parametrize(
    ("variable", "value", "said"),
    [
        ("HPC_GATEPORT_SHA256", "ABC", "64 lower-case hexadecimal digits"),
        ("HPC_GATEPORT_SHA256", "0" * 63, "64 lower-case hexadecimal digits"),
        ("HPC_GATEPORT", "relative/gateport.so", "absolute path"),
        ("HPC_GATEPORT", "/shared/../gateport.so", "absolute path"),
    ],
)
def test_o63_a_mapper_value_that_is_no_pin_or_no_plain_absolute_path_is_refused(hpc, site, variable, value, said):
    """O63: the mapper's pin is 64 lower-case hexadecimal digits and its path absolute, plain and without '..', as the
    other pins' are; the message names the variable."""
    with pytest.raises(hpc.settings.SettingsError, match=f"^{variable}: .*{said}"):
        hpc.settings.site_from_environment({**site, variable: value})


def test_o63_the_state_file_keeps_the_mapper_and_an_older_state_without_it_still_loads(hpc, site):
    """O63: the site as the state file records it holds the mapper's two values, and a state written before they
    existed loads with none (the site is read back by `down` and the service verbs)."""
    declared_site = hpc.settings.site_from_environment(site)
    data = declared_site.to_json()
    assert (data["gateport"], data["gateport_sha256"]) == (site["HPC_GATEPORT"], site["HPC_GATEPORT_SHA256"])
    assert hpc.settings.Site.from_json(data) == declared_site
    older = {k: v for k, v in data.items() if k not in ("gateport", "gateport_sha256")}
    assert (hpc.settings.Site.from_json(older).gateport, hpc.settings.Site.from_json(older).gateport_sha256) == ("", "")


# -- what up refuses, before the cluster is touched ------------------------------------------------------------------


@pytest.mark.parametrize("variable", VARIABLES)
def test_o63_up_refuses_a_rest_template_whose_mapper_key_is_not_set_naming_it(hpc, tmp_path, site, variable):
    """O63: a template that names the mapper needs both keys; an absent one fails `up` before any submit, naming it."""
    rig = Rig(hpc, tmp_path)
    del site[variable]
    with pytest.raises(RuntimeError, match=f"{variable} is not set"):
        rig.up(site)
    assert not rig.submitted()
    assert not (rig.root / "hpc-federation.json").exists()


def test_o63_up_refuses_a_mapper_file_that_is_missing_naming_the_variable_never_the_path(hpc, tmp_path, site):
    """O63: the declared mapper that is not there fails `up` before any submit, naming the variable and no path."""
    rig = Rig(hpc, tmp_path)
    Path(site["HPC_GATEPORT"]).unlink()
    with pytest.raises(RuntimeError, match=r"^HPC_GATEPORT: the file it names is missing") as refused:
        rig.up(site)
    assert site["HPC_GATEPORT"] not in str(refused.value)
    assert not rig.submitted()


def test_o63_up_refuses_a_mapper_whose_sha256_is_not_the_declared_one_before_it_submits(hpc, tmp_path, site):
    """O63: a pin is a declaration: a mapper rebuilt since it was declared (one byte here) fails `up`, naming the pin
    and not the path; nothing is submitted and no state is written."""
    rig = Rig(hpc, tmp_path)
    path = Path(site["HPC_GATEPORT"])
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(RuntimeError, match=r"^HPC_GATEPORT_SHA256: the file HPC_GATEPORT names") as refused:
        rig.up(site)
    assert site["HPC_GATEPORT"] not in str(refused.value)
    assert not rig.submitted()
    assert not (rig.root / "hpc-federation.json").exists()


def test_o63_the_pilots_template_reads_no_mapper_though_the_site_declares_one(hpc, tmp_path, site):
    """O63: a template without a REST level stays as it was: a mapper the site declares (a corrupt one here) is never
    read for it, as any pin its template does not name, and `up` goes on."""
    rig = Rig(hpc, tmp_path, layout=LAYOUT)
    Path(site["HPC_GATEPORT"]).write_bytes(b"changed since it was declared")
    rig.up(site)
    assert rig.submitted()


# -- the rendered file ------------------------------------------------------------------------------------------------


def test_o63_the_rendered_rest_platform_loads_both_levels_and_the_mappers_preload_pin(hpc, pins, tmp_path, site, ca):
    """O63: `load_platform` reads the rendered REST file: the mount level and the REST level of `[sandbox.data_paths]`,
    `content_check_file`, and the mapper as the one preload, at the file `HPC_GATEPORT` names with the SHA-256
    `HPC_GATEPORT_SHA256` declares; the pilot's pins are as in the pilot's own file."""
    config = load_platform(render(hpc, pins, tmp_path / "root", site, ca))
    sandbox = config.sandbox
    assert sandbox.mode_of("mounted").value == "mount"
    assert sandbox.mode_of(REST_LEVEL).value == "rest"
    assert sandbox.content_check_file == "hello.txt"
    (preload,) = sandbox.apptainer.preload
    assert (str(preload.path), preload.sha256) == (site["HPC_GATEPORT"], site["HPC_GATEPORT_SHA256"])
    assert sandbox.apptainer.egress_shim.sha256 == site["HPC_EGRESS_SHIM_SHA256"]


def test_o63_two_renders_differing_only_in_the_mappers_pin_are_two_campaigns_by_that_pin_alone(
    hpc, pins, tmp_path, site, ca
):
    """O63: the fingerprint holds the preload's SHA-256 (`preload_sha256`), so shards run with two builds of the mapper
    are two campaigns, which the merge refuses; it holds no path of the mapper."""
    first = load_platform(render(hpc, pins, tmp_path / "one", site, ca)).fingerprint()
    other = agent_site(tmp_path / "other-build", gateport=GATEPORT + b"another build\n")
    changed = {**site, "HPC_GATEPORT": other["HPC_GATEPORT"], "HPC_GATEPORT_SHA256": other["HPC_GATEPORT_SHA256"]}
    second = load_platform(render(hpc, pins, tmp_path / "two", changed, ca)).fingerprint()
    assert first != second
    assert first["sandbox_backend"]["preload_sha256"] == [site["HPC_GATEPORT_SHA256"]]
    assert second["sandbox_backend"]["preload_sha256"] == [other["HPC_GATEPORT_SHA256"]]
    swapped = {**second["sandbox_backend"], "preload_sha256": first["sandbox_backend"]["preload_sha256"]}
    assert {**second, "sandbox_backend": swapped} == first
    assert "gateport.so" not in json.dumps(first)
