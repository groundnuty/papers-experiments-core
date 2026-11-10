"""The throwaway federation of deploy/compose-federation, checked without starting it.

The Compose file runs on a shared Docker daemon, so what keeps it to itself is checked here: one project name, only the
two pinned images and never a pull, every published port on the host's loopback, and every bind source below the
federation's root or the file's own directory. The CA, the certificates, the secrets and platform.toml's rendering are
checked offline too.
"""

import re
import shutil
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "compose-federation"
COMPOSE = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
ROOT_VARIABLE = "${EXPCORE_FED_ROOT:-/mnt/volume2/expcore-federation}"


@pytest.fixture(scope="module")
def fed():
    """The deploy scripts' modules, imported by their plain names as the scripts import one another."""
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(DEPLOY))
        import federation
        import fedlayout
        import fedpki

        yield type("Fed", (), {"layout": fedlayout, "pki": fedpki, "cli": federation})


def lines_after(key: str) -> list[str]:
    """The list items under every occurrence of `key:` in the Compose file."""
    items = []
    for block in re.finditer(rf"^(\s*){key}:\n((?:\1\s+-.*\n)+)", COMPOSE, re.MULTILINE):
        items += [line.strip()[2:].strip() for line in block.group(2).splitlines()]
    return items


def test_the_project_has_one_fixed_name(fed):
    assert re.findall(r"^name: (\S+)$", COMPOSE, re.MULTILINE) == [fed.layout.PROJECT]


def test_every_published_port_is_bound_to_the_loopback_and_nothing_else_is_published(fed):
    ports = lines_after("ports")
    assert sorted(ports) == sorted(f'"127.0.0.1:{s.port}:443"' for s in fed.layout.SERVICES)
    assert "network_mode: host" not in COMPOSE
    assert "expose:" not in COMPOSE


def test_only_the_two_pinned_images_are_named_and_none_is_ever_pulled(fed):
    images = set(re.findall(r"docker\.onedata\.org/\S+", COMPOSE))
    assert images == set(fed.layout.IMAGES)
    services = re.findall(r"^  (\w+):\n", COMPOSE.split("\nservices:\n")[1].split("\nnetworks:\n")[0], re.MULTILINE)
    assert set(services) == {s.name for s in fed.layout.SERVICES} | {"janitor"}
    assert len(re.findall(r"^\s+pull_policy: never$", COMPOSE, re.MULTILINE)) == 2  # the service anchor, the janitor
    assert COMPOSE.count("<<: *service") == len(fed.layout.SERVICES)
    assert "build:" not in COMPOSE


def test_no_container_reaches_the_daemon_or_runs_privileged():
    assert "docker.sock" not in COMPOSE
    assert "privileged" not in COMPOSE
    assert "cap_add" not in COMPOSE


def test_every_bind_source_is_below_the_root_or_beside_the_file_and_is_never_created():
    sources = re.findall(r"^\s+source: (\S+)$", COMPOSE, re.MULTILINE)
    assert sources
    assert all(s.startswith(ROOT_VARIABLE + "/") or s == ROOT_VARIABLE or s == "./start.sh" for s in sources)
    assert COMPOSE.count("type: bind") == COMPOSE.count("create_host_path: false") == len(sources)


def test_the_janitor_has_no_network_and_runs_only_when_asked():
    janitor = COMPOSE.split("\n  janitor:\n")[1].split("\nnetworks:\n")[0]
    assert "network_mode: none" in janitor
    assert 'profiles: ["janitor"]' in janitor
    assert "ports:" not in janitor


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the CA")


@needs_openssl
def test_a_service_certificate_chains_to_the_ca_for_its_domain_and_the_loopback(fed, tmp_path):
    pki, layout = fed.pki, fed.layout
    ca, ca_key, key, cert = tmp_path / "ca.pem", tmp_path / "ca.key", tmp_path / "svc.key", tmp_path / "svc.pem"
    pki.make_ca(ca, ca_key, "test CA")
    service = layout.PROVIDERS[0]
    pki.make_leaf(
        ca, ca_key, key, cert, service.domain, pki.subject_alt_names(service.domain, "node1", ["127.0.0.1"]), tmp_path
    )
    pki.verify_leaf(ca, cert, service.domain)
    pki.verify_leaf(ca, cert, "127.0.0.1")
    with pytest.raises(pki.PkiError):
        pki.verify_leaf(ca, cert, layout.ZONE.domain)  # another service's name is not in it
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
    render = fed.cli.render
    assert render('url = "@zone_url@"\nid = "@provider_id.pine@"\n', {"zone_url": "u", "provider_id.pine": "p"}) == (
        'url = "u"\nid = "p"\n'
    )
    with pytest.raises(RuntimeError, match="1 placeholder"):
        render('id = "@space_id.alpha-space@"\n', {})


def test_the_baseline_has_a_directory_of_more_than_1000_entries_and_repeatable_content(fed):
    layout = fed.layout
    files = layout.baseline(layout.STUDY_SPACE)
    listing = [f for f in files if f.path.startswith(layout.LISTING_DIR + "/")]
    assert len(listing) > 1000
    assert len({f.path for f in files}) == len(files)
    assert any("/" not in f.path and f.path == layout.CONTENT_FILE for f in files)
    first = files[1]
    assert first.content() == first.content()
    assert len(first.content()) == first.size


@needs_openssl
def test_the_committed_template_renders_into_a_platform_file_the_layer_loads(fed, tmp_path):
    """The template, filled from facts as up fills it, is a platform.toml the platform layer accepts: its CA bundle
    verified (ca_file, never insecure), its support set the three providers, the agent pinned to the data provider."""
    from fedboot import Bootstrap, Ids

    from expcore_onedata.config import load_platform

    layout = fed.layout
    boot = Bootstrap.__new__(Bootstrap)
    boot.ids = Ids(
        users={user: f"u{i}" for i, user in enumerate(layout.PRINCIPALS)},
        providers={p.name: f"p{i}" for i, p in enumerate(layout.PROVIDERS)},
        spaces={name: f"s{i}" for i, name in enumerate(layout.SPACES)},
    )
    platform = tmp_path / "platform.toml"
    template = layout.TEMPLATE.read_text(encoding="utf-8")
    platform.write_text(fed.cli.render(template, fed.cli.placeholders(fed.cli.facts_of(boot))), encoding="utf-8")
    (tmp_path / "ca").mkdir()
    fed.pki.make_ca(tmp_path / "ca" / "federation-ca.pem", tmp_path / "ca.key", "test CA")
    config = load_platform(platform, require_secrets=False)
    assert config.tls.record()["mode"] == "ca_file"
    assert config.support.providers == tuple(p.name for p in layout.PROVIDERS)
    assert config.support.local == layout.HOLDER
    assert config.space.name == layout.STUDY_SPACE
    assert config.space.id == boot.ids.spaces[layout.STUDY_SPACE]
    assert {name: p.url for name, p in config.providers.items()} == {p.name: p.url for p in layout.PROVIDERS}
    body = "\n".join(line for line in platform.read_text(encoding="utf-8").splitlines() if not line.startswith("#"))
    assert fed.cli.PLACEHOLDER.search(body) is None  # every placeholder filled
