"""The throwaway federation of deploy/compose-federation, checked without starting it.

The Compose file runs on a shared Docker daemon, so what keeps it to itself is checked here, for every layout the
generator is given: one project name, only the two pinned images and never a pull, every published port on the host's
loopback, no privileged container, every bind source below the federation's root or the file's own directory, and a
janitor that deletes only in a root carrying the declared project's mark. The CA, the certificates, the secrets, the
root's checks and platform.toml's rendering are checked offline too.
"""

import json
import os
import shutil
import subprocess

import pytest
from federation_support import DEPLOY, EXAMPLE

from expcore_onedata.fault_settings import FaultSettings

FORBIDDEN_SERVICE_KEYS = {"privileged", "cap_add", "devices", "pid", "ipc", "build", "expose", "security_opt"}


def services(fed, spec) -> dict:
    return fed.compose.model(spec)["services"]


# -- the generated Compose file, for every layout ---------------------------------------------------------------------


def test_the_project_has_the_declared_name_and_exactly_the_declared_services(fed, any_spec):
    model = fed.compose.model(any_spec)
    assert model["name"] == any_spec.project
    assert set(model["services"]) == {s.name for s in any_spec.services} | {"janitor"}
    assert set(model) == {"name", "services", "networks"}


def test_every_published_port_is_bound_to_the_loopback_and_nothing_else_is_published(fed, any_spec):
    published = {}
    for name, service in services(fed, any_spec).items():
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1"
            assert port["target"] == 443
            published[name] = int(port["published"])
        assert service.get("network_mode") in (None, "none")  # never the host's network
    assert published == {s.name: s.port for s in any_spec.services}


def test_o11_the_fault_injectors_default_settle_timeout_covers_every_stop_grace_period_with_a_margin(fed, any_spec):
    """O11: a stop that hits the command timeout leaves the daemon stopping the container for up to the provider's
    grace period; the restore waits that long for it to show down, so the default settle timeout (the bound of that
    wait) exceeds the grace of every service the generator emits by at least 30 s."""
    graces = [
        float(s["stop_grace_period"].removesuffix("s"))
        for s in services(fed, any_spec).values()
        if "stop_grace_period" in s  # the janitor keeps Compose's default, and is no provider
    ]
    assert graces
    assert max(graces) + 30 <= FaultSettings.settle_timeout_s


def test_only_the_two_pinned_images_are_named_and_none_is_ever_pulled_or_built(fed, any_spec):
    for service in services(fed, any_spec).values():
        assert service["image"] in fed.layout.IMAGES
        assert service["pull_policy"] == "never"
        assert "build" not in service


def test_no_container_reaches_the_daemon_or_runs_privileged(fed, any_spec):
    text = fed.compose.render(any_spec)
    assert "docker.sock" not in text
    for service in services(fed, any_spec).values():
        assert not FORBIDDEN_SERVICE_KEYS & set(service)


def test_every_bind_source_is_below_the_root_or_beside_the_file_and_is_never_created(fed, any_spec):
    root = fed.compose.ROOT
    sources = []
    for service in services(fed, any_spec).values():
        for volume in service["volumes"]:
            assert volume["type"] == "bind"
            assert volume["bind"] == {"create_host_path": False}
            sources.append(volume["source"])
        for env_file in service.get("env_file", []):
            assert env_file["path"].startswith(root + "/secrets/")
    assert sources
    assert all(s.startswith(root + "/") or s == root or s == "./start.sh" for s in sources)


def test_every_service_has_its_declared_memory_limit_and_names_its_root(fed, any_spec):
    model = services(fed, any_spec)
    assert {name: model[name]["mem_limit"] for name in model if name != "janitor"} == {
        s.name: s.memory_limit for s in any_spec.services
    }
    assert all(s["labels"] == {fed.compose.ROOT_LABEL: fed.compose.ROOT} for s in model.values())


def test_the_projects_network_names_its_root_like_every_container(fed, any_spec):
    networks = fed.compose.model(any_spec)["networks"]
    assert networks == {"fed": {"driver": "bridge", "labels": {fed.compose.ROOT_LABEL: fed.compose.ROOT}}}


def test_the_janitor_has_no_network_runs_only_when_asked_and_deletes_only_under_the_declared_mark(fed, any_spec):
    """A Compose command run by hand, outside the checked entry point, may bind another directory as the root; the
    janitor deletes only after reading the mark up writes into a root it made or found empty, holding the project."""
    janitor = services(fed, any_spec)["janitor"]
    assert janitor["network_mode"] == "none"
    assert janitor["profiles"] == ["janitor"]
    assert "ports" not in janitor
    script = janitor["entrypoint"][-1]
    assert f"/fed/{fed.layout.ROOT_MARKER}" in script
    assert f"= {any_spec.project} ||" in script
    assert script.index(fed.layout.ROOT_MARKER) < script.index("-delete")


@pytest.mark.parametrize(
    ("layout", "committed"),
    [(None, DEPLOY / "compose.yaml"), (EXAMPLE, EXAMPLE.with_suffix(".compose.yaml"))],
)
def test_the_committed_compose_files_are_what_the_generator_makes(fed, layout, committed):
    """compose.yaml and the example's file are generated, never edited: a change to the generator shows in them."""
    assert committed.read_text(encoding="utf-8") == fed.compose.render(fed.spec.load_spec(layout))


needs_compose = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode,
    reason="the docker compose command resolves the file",
)


@needs_compose
def test_docker_compose_reads_the_generated_file_as_the_generator_meant(fed, any_spec, tmp_path):
    """Compose itself, offline, resolves the file: loopback ports, binds below the root and start.sh beside the file."""
    file = tmp_path / "compose.yaml"
    file.write_text(fed.compose.render(any_spec), encoding="utf-8")
    root = "/mnt/volume2/expcore-federation/check"
    env = {**os.environ, "EXPCORE_FED_ROOT": root, "DOCKER_CONFIG": str(tmp_path)}
    command = ["docker", "compose", "--file", str(file), "--project-directory", str(DEPLOY), "--profile", "janitor"]
    done = subprocess.run([*command, "config", "--format", "json"], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    model = json.loads(done.stdout)
    assert model["name"] == any_spec.project
    assert model["networks"]["fed"]["labels"][fed.compose.ROOT_LABEL] == root
    for name, service in model["services"].items():
        assert all(p["host_ip"] == "127.0.0.1" for p in service.get("ports", []))
        assert service["labels"][fed.compose.ROOT_LABEL] == root
        for volume in service["volumes"]:
            assert volume["source"].startswith(root) or volume["source"] == str(DEPLOY / "start.sh"), name


def test_a_compose_file_run_without_the_root_set_binds_nothing(fed):
    """The generated file has no default root: a hand-run command without EXPCORE_FED_ROOT fails instead of binding
    some directory by accident."""
    assert fed.compose.ROOT.startswith("${EXPCORE_FED_ROOT:?")


# -- certificates, secrets and platform.toml --------------------------------------------------------------------------

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the CA")


@needs_openssl
def test_a_service_certificate_chains_to_the_ca_for_its_domain_and_the_loopback(fed, tmp_path):
    pki, spec = fed.pki, fed.spec.load_spec()
    ca, ca_key, key, cert = tmp_path / "ca.pem", tmp_path / "ca.key", tmp_path / "svc.key", tmp_path / "svc.pem"
    pki.make_ca(ca, ca_key, "test CA")
    service = spec.providers[0]
    pki.make_leaf(
        ca, ca_key, key, cert, service.domain, pki.subject_alt_names(service.domain, "node1", ["127.0.0.1"]), tmp_path
    )
    pki.verify_leaf(ca, cert, service.domain)
    pki.verify_leaf(ca, cert, "127.0.0.1")
    with pytest.raises(pki.PkiError):
        pki.verify_leaf(ca, cert, spec.zone.domain)  # another service's name is not in it
    for private in (ca_key, key):
        assert private.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE KEY" not in ca.read_text(encoding="utf-8")


def test_a_secret_is_written_private_and_never_replaced(fed, tmp_path):
    pki = fed.pki
    path = pki.write_secret(tmp_path / "one.pass", "first")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(pki.PkiError):
        pki.write_secret(path, "second")
    assert pki.ensure_secret(path) == "first"
    path.chmod(0o640)
    with pytest.raises(pki.PkiError):
        pki.ensure_secret(path)


def test_rendering_fills_every_placeholder_and_refuses_one_without_a_value(fed):
    render = fed.facts.render
    assert render('url = "@zone_url@"\nid = "@provider_id.pine@"\n', {"zone_url": "u", "provider_id.pine": "p"}) == (
        'url = "u"\nid = "p"\n'
    )
    with pytest.raises(RuntimeError, match="1 placeholder"):
        render('id = "@space_id.alpha-space@"\n', {})


def test_the_baseline_has_a_directory_of_more_than_1000_entries_and_repeatable_content(fed):
    layout = fed.layout
    files = layout.baseline("alpha-space", "alpha-space")
    listing = [f for f in files if f.path.startswith(layout.LISTING_DIR + "/")]
    assert len(listing) > 1000
    assert len({f.path for f in files}) == len(files)
    assert any("/" not in f.path and f.path == layout.CONTENT_FILE for f in files)
    first = files[1]
    assert first.content() == first.content()
    assert len(first.content()) == first.size
    assert [f.path for f in layout.baseline("beta-space", "alpha-space")] == ["beta.txt"]


def fake_boot(fed, layout):
    """A bootstrap with made-up ids, as up holds them once the federation answered."""
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout = layout
    spec = layout.spec
    boot.ids = fed.boot.Ids(
        users={p.name: f"u{i}" for i, p in enumerate(spec.principals)},
        providers={p.name: f"p{i}" for i, p in enumerate(spec.providers)},
        spaces={s.name: f"s{i}" for i, s in enumerate(spec.spaces)},
    )
    return boot


@needs_openssl
@pytest.mark.parametrize("path", [None, EXAMPLE], ids=["default", "five-providers"])
def test_each_committed_template_renders_into_a_platform_file_the_layer_loads(fed, path, tmp_path):
    """A layout's template, filled from facts as up fills it, is a platform.toml the platform layer accepts: its CA
    bundle verified (ca_file, never insecure), its support set the study space's owner and mirrors, the agent pinned
    to the data provider."""
    from expcore_onedata.config import load_platform

    spec = fed.spec.load_spec(path)
    layout = fed.layout.Layout(tmp_path, spec=spec)
    boot = fake_boot(fed, layout)
    facts = fed.facts.facts_of(boot)
    assert facts["root_parent"] == {"path": fed.layout.DEFAULT_ROOT, "set_by": "default"}
    assert facts["layout"]["project"] == spec.project
    assert facts["layout"]["memory_limits"] == {s.name: s.memory_limit for s in spec.services}
    platform = tmp_path / "platform.toml"
    assert fed.facts.write_facts(layout, boot) == "facts.json and platform.toml written"
    (tmp_path / "ca").mkdir()
    fed.pki.make_ca(tmp_path / "ca" / "federation-ca.pem", tmp_path / "ca.key", "test CA")
    config = load_platform(platform, require_secrets=False)
    study = spec.space(spec.study_space)
    assert config.tls.record()["mode"] == "ca_file"
    assert config.support.providers == study.supporters
    assert config.support.local == spec.holder
    assert config.space.name == spec.study_space
    assert config.space.id == boot.ids.spaces[spec.study_space]
    assert {name: p.url for name, p in config.providers.items()} == {p.name: p.url for p in spec.providers}
    body = "\n".join(line for line in platform.read_text(encoding="utf-8").splitlines() if not line.startswith("#"))
    assert fed.facts.PLACEHOLDER.search(body) is None  # every placeholder filled


def test_platform_toml_is_written_private_even_over_an_open_one(fed, tmp_path):
    """platform.toml names where the credentials are and is rendered from a template: it is written 0600, and a
    rewrite at reset tightens one left open."""
    layout = fed.layout.Layout(tmp_path)
    boot = fake_boot(fed, layout)
    layout.platform.write_text("stale-content-sentinel\n", encoding="utf-8")
    layout.platform.chmod(0o644)
    fed.facts.write_facts(layout, boot)
    assert layout.platform.stat().st_mode & 0o777 == 0o600
    assert "stale-content-sentinel" not in layout.platform.read_text(encoding="utf-8")


@needs_openssl
def test_o03_the_mount_live_test_puts_the_sidecar_on_the_federation_network_dialling_the_data_providers_domain(
    fed, tmp_path
):
    """O03: the live mount-path test runs with a copy of the federation's platform.toml and a `[sandbox]` table: the
    sidecar joins the project's network and dials the local provider by its domain there, the one its certificate
    names, never the loopback address the host reaches it at. The copy lives in a directory of its own, with every path
    of the original made absolute, and loads with its secrets and its CA where the federation keeps them."""
    from mount_live import LEVEL, derive_platform, federation_network, sandbox_table

    from expcore_onedata.config import load_platform
    from expcore_onedata.sandbox.api import DataPathMode

    root, own = tmp_path / "root", tmp_path / "own"
    root.mkdir()
    own.mkdir()
    layout = fed.layout.Layout(root)
    fed.facts.write_facts(layout, fake_boot(fed, layout))
    (root / "ca").mkdir()
    fed.pki.make_ca(root / "ca" / "federation-ca.pem", tmp_path / "ca.key", "test CA")
    facts = json.loads(layout.facts.read_text(encoding="utf-8"))
    local = layout.spec.holder
    table = sandbox_table(
        work_root=own / "work",
        images={"agent": ("agent:1", "sha256:" + "a" * 64), "oneclient": ("mounter:1", "sha256:" + "c" * 64)},
        pool="10.77.0.0/24",
        network=federation_network(facts),
        local=local,
        local_address=facts["providers"][local]["domain_on_project_network"],
    )
    before = sorted(p.name for p in root.iterdir())
    config = load_platform(derive_platform(layout.platform, own, table), require_secrets=False)
    assert "fed" in fed.compose.model(layout.spec)["networks"]  # the project's one network
    assert config.sandbox.federation.network == f"{layout.spec.project}_fed"
    assert config.sandbox.federation.local_provider == layout.spec.provider(local).domain
    assert config.sandbox.mode_of(LEVEL) is DataPathMode.MOUNT
    assert config.tls.ca_file == (root / "ca" / "federation-ca.pem").resolve()
    assert config.admin.ref == str((root / "secrets" / "admin.token").resolve())
    assert list(own.iterdir()) == [own / "platform.toml"]
    assert sorted(p.name for p in root.iterdir()) == before  # nothing written beside the original
    with pytest.raises(ValueError, match="already has a"):
        derive_platform(own / "platform.toml", tmp_path, table)


@needs_openssl
def test_o40_a_direct_mount_table_for_the_federation_maps_the_local_providers_storage_and_loads(fed, tmp_path):
    """O40: the comparison harness's direct pass derives the platform as the proxy one does, with the local provider's
    storage (the Compose layout's directory of it) mapped and a check file named; the copy loads, and the federation's
    root gets nothing written beside the original."""
    from measure_direct_vs_proxy import mount_keys, storage_of
    from mount_live import derive_platform, federation_network, sandbox_table

    from expcore_onedata.config import load_platform

    root, own = tmp_path / "root", tmp_path / "own"
    root.mkdir()
    own.mkdir()
    layout = fed.layout.Layout(root)
    fed.facts.write_facts(layout, fake_boot(fed, layout))
    (root / "ca").mkdir()
    fed.pki.make_ca(root / "ca" / "federation-ca.pem", tmp_path / "ca.key", "test CA")
    facts = json.loads(layout.facts.read_text(encoding="utf-8"))
    local = layout.spec.holder
    storage = storage_of("0a1b2c3d", None, layout.platform, local)
    assert storage == ("0a1b2c3d", str(layout.storage(local).resolve()))
    table = sandbox_table(
        work_root=own / "work",
        images={"agent": ("agent:1", "sha256:" + "a" * 64), "oneclient": ("mounter:1", "sha256:" + "c" * 64)},
        pool="10.77.0.0/24",
        network=federation_network(facts),
        local=local,
        local_address=facts["providers"][local]["domain_on_project_network"],
        **mount_keys("direct", storage, "space-x/run/check.bin"),
    )
    before = sorted(p.name for p in root.iterdir())
    mount = load_platform(derive_platform(layout.platform, own, table), require_secrets=False).sandbox.mount
    assert mount.io_mode == "direct"
    assert mount.storages == (storage,)
    assert mount.direct_check_file == "space-x/run/check.bin"
    assert sorted(p.name for p in root.iterdir()) == before


@needs_openssl
def test_o03_the_rest_live_test_puts_the_gate_on_the_federation_network_dialling_names_the_certificates_hold(
    fed, tmp_path
):
    """O30 (#203): the live REST-path test runs with a copy of the federation's platform.toml and a `[sandbox]` table
    of one REST level, whose `[sandbox.federation]` names the project's network, the zone's domain and the local
    provider's. The gate then serves and dials those domains on port 443, never the loopback ports the platform file's
    URLs give, and each is a name the certificate the federation issues that service holds, so the gate's check of it
    against the federation's CA passes."""
    from mount_live import derive_platform, federation_network
    from rest_live import REST_LEVEL, rest_sandbox_table

    from expcore_onedata.config import load_platform
    from expcore_onedata.sandbox.api import DataPathMode, Endpoint
    from expcore_onedata.sandbox.containers import gate_network, gate_upstreams

    root, own = tmp_path / "root", tmp_path / "own"
    root.mkdir()
    own.mkdir()
    layout = fed.layout.Layout(root)
    fed.facts.write_facts(layout, fake_boot(fed, layout))
    ca, ca_key = root / "ca" / "federation-ca.pem", tmp_path / "ca.key"
    ca.parent.mkdir()
    fed.pki.make_ca(ca, ca_key, "test CA")
    facts = json.loads(layout.facts.read_text(encoding="utf-8"))
    local = layout.spec.holder
    table = rest_sandbox_table(
        work_root=own / "work",
        images={"agent": ("agent:1", "sha256:" + "a" * 64), "gate": ("gate:1", "sha256:" + "b" * 64)},
        pool="10.77.0.0/24",
        network=federation_network(facts),
        zone_address=facts["zone"]["domain_on_project_network"],
        local=local,
        local_address=facts["providers"][local]["domain_on_project_network"],
        content_check_file="hello.txt",
    )
    config = load_platform(derive_platform(layout.platform, own, table), require_secrets=False)
    s = config.sandbox
    assert s.mode_of(REST_LEVEL) is DataPathMode.REST
    assert gate_network(s) == f"{layout.spec.project}_fed"
    zone, provider = Endpoint.from_url(config.zone.url), Endpoint.from_url(config.local_provider.url)
    assert (zone.host, provider.host) == ("127.0.0.1", "127.0.0.1")  # what the host reaches; no container can
    upstreams = gate_upstreams(s, zone, provider)
    services = {"onezone": layout.spec.zone, "oneprovider": layout.spec.provider(local)}
    assert {h["role"]: (name, h["addr"], h["port"]) for name, h in upstreams.items()} == {
        role: (service.domain, service.domain, 443) for role, service in services.items()
    }
    for role, service in services.items():
        key, cert = tmp_path / f"{role}.key", tmp_path / f"{role}.pem"
        sans = fed.pki.subject_alt_names(service.domain, "node1", ["127.0.0.1"])  # as up issues them
        fed.pki.make_leaf(ca, ca_key, key, cert, service.domain, sans, tmp_path)
        fed.pki.verify_leaf(ca, cert, service.domain)


def test_a_layout_without_a_template_writes_facts_and_says_there_is_no_platform_file(fed, minimal_layout, tmp_path):
    layout = fed.layout.Layout(tmp_path, spec=fed.spec.load_spec(minimal_layout))
    assert "no platform.toml" in fed.facts.write_facts(layout, fake_boot(fed, layout))
    assert layout.facts.is_file()
    assert not layout.platform.exists()
