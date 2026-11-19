"""Several federations of one layout on one host (`federation.py --instance K`), the host's shared CA (`federation.py
ca`, `EXPCORE_FED_CA_DIR`), the template's project placeholders, the engine setting, and the render identity, checked
without starting anything.

The CA's SHA-256 is in the campaign fingerprint (the TLS setting's record) and the elastic ledger refuses a federation
whose fingerprint is not the campaign's, so instances that each made their own CA could never take leases of one
campaign: one template rendered for two instances on one shared CA has one fingerprint, and on two CAs it has two."""

import json
import os
import subprocess
import sys

import pytest
from federation_support import MINIMAL
from slots_support import ids_of, layout_with_template, make_root

from expcore.campaign.manifest import fingerprint_sha256
from expcore_onedata.config import load_platform

FLEET_FRONT = 29440  # the group's LLM fleet front on a VM's loopback: no instance may publish it

TEMPLATE = """\
zone = { name = "zone", url = "@zone_url@" }
providers.oak = { url = "@provider_url.oak@", id = "@provider_id.oak@" }
providers.elm = { url = "@provider_url.elm@", id = "@provider_id.elm@" }
space = { name = "@space_name@", id = "@space_id.alpha-space@", prefixes = ["alpha-"] }
support = { providers = ["oak", "elm"], local = "oak" }
credentials.admin = { file = "@hpc_root@/secrets/admin.token" }
credentials.users = { directory = "@hpc_root@/secrets/users", pattern = "{user}.pass" }
principals.data_path = { username = "alpha-reader", user_id = "@user_id.alpha-reader@" }
placement = { holder = "oak" }
tls = { ca_file = "@hpc_root@/ca/federation-ca.pem" }
# what one template names per instance, from its project
# network @compose_network@ zone @zone_domain@ local @provider_domain.oak@ project @compose_project@
"""


def instances(fed, tmp_path, count: int = 3):
    spec = fed.spec.load_spec(layout_with_template(TEMPLATE, tmp_path / "layout", MINIMAL))
    return [fed.spec.instance(spec, k) for k in range(1, count + 1)]


def test_instances_of_one_layout_have_their_own_project_network_domain_ports_and_root(fed, tmp_path):
    """C49: three instances on one host: no two share a project (so a network, `<project>_fed`), a domain or a published
    port, none publishes the fleet front's port, and each one's default root is its own, beside the others below the
    parent. The layout file, its digest and its template are the same for all."""
    found = instances(fed, tmp_path)
    models = [fed.compose.model(spec) for spec in found]
    assert len({m["name"] for m in models}) == 3
    assert len({spec.domain for spec in found}) == 3
    ports = [int(p["published"]) for m in models for s in m["services"].values() for p in s.get("ports", [])]
    assert len(ports) == len(set(ports)) == 9
    assert FLEET_FRONT not in ports
    assert {(spec.digest, spec.platform_template) for spec in found} == {(found[0].digest, found[0].platform_template)}
    parent = tmp_path / "parent"
    parent.mkdir()
    environ = {"EXPCORE_FED_ROOT_PARENT": str(parent)}
    roots = [fed.layout.Layout.from_environment(environ, spec, instance=True).root for spec in found]
    assert roots == [parent / spec.project for spec in found]
    assert fed.layout.Layout.from_environment({**environ, "EXPCORE_FED_ROOT": str(parent / "x")}, found[0],
                                              instance=True).root == parent / "x"  # fmt: skip


def test_an_instance_whose_ports_would_meet_another_is_refused(fed, tmp_path):
    wide = MINIMAL.replace("port = 29645", "port = 29653")  # a span of 10: instance 2's zone would take elm's port
    spec = fed.spec.load_spec(layout_with_template(TEMPLATE, tmp_path / "layout", wide))
    with pytest.raises(fed.spec.LayoutError, match="span"):
        fed.spec.instance(spec, 2)
    with pytest.raises(fed.spec.LayoutError, match="from 1 to"):
        fed.spec.instance(fed.spec.load_spec(layout_with_template(TEMPLATE, tmp_path / "l2", MINIMAL)), 0)


def test_the_engine_setting_is_the_one_command_every_engine_call_names(fed, tmp_path, monkeypatch):
    """`EXPCORE_FED_ENGINE`: the deploy's engine calls name it (a fake podman on PATH answers and logs), and a value
    that is no engine is refused before anything runs."""
    log = tmp_path / "calls.jsonl"
    engine = tmp_path / "podman"
    engine.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + "
                      "'\\n')\nprint('c-1\\tpine\\trunning\\t/r')\n", encoding="utf-8")  # fmt: skip
    engine.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("EXPCORE_FED_ENGINE", "podman")
    (container,) = fed.docker.project_containers("expcore-test-1")
    assert (container.service, container.state, container.root) == ("pine", "running", "/r")
    assert json.loads(log.read_text())[:2] == ["ps", "--all"]
    monkeypatch.setenv("EXPCORE_FED_ENGINE", "lxc")
    with pytest.raises(RuntimeError, match="EXPCORE_FED_ENGINE"):
        fed.docker.engine()
    assert fed.cli.main(["status"]) == 2


def staged(fed, tmp_path, spec, k: int, ca_dir):
    """Instance `k`'s root, with its secrets, the CA (the shared one, or its own) and the ids Onezone gave it."""
    root = tmp_path / "parent" / spec.project
    root.mkdir(parents=True)
    layout = fed.layout.Layout(root, tmp_path / "parent", spec, ca_dir)
    layout.marker.write_text(spec.project + "\n", encoding="utf-8")
    make_root(layout)
    layout.ca_pem.parent.mkdir()
    if ca_dir is None:
        fed.pki.make_ca(layout.ca_pem, layout.ca_key, f"{spec.project} test CA")
    else:
        fed.cli.use_shared_ca(layout)
    ids = {kind: {name: f"{k}-{value}" for name, value in found.items()} for kind, found in ids_of(spec).items()}
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout, boot.ids = layout, fed.boot.Ids(admin=f"uid-admin-{k}", **ids)
    fed.facts.write_facts(layout, boot)
    return layout


@pytest.mark.skipif(subprocess.run(["which", "openssl"], capture_output=True).returncode != 0, reason="no openssl")
@pytest.mark.parametrize("shared", [True, False])
def test_one_template_on_one_shared_ca_renders_one_fingerprint_for_every_instance(fed, tmp_path, shared):
    """C49: two instances (other ids, urls, domains and roots) rendered from one template: on the host's shared CA their
    platform files have one campaign fingerprint, and one render identity; on a CA each made, two. The shared CA's
    key stays in its directory: no root holds it. The template's project placeholders name each instance's own."""
    specs = instances(fed, tmp_path, 2)
    ca_dir = None
    if shared:
        ca_dir = tmp_path / "shared-ca"
        said = fed.cli.make_shared_ca({"EXPCORE_FED_CA_DIR": str(ca_dir)})
        assert said.startswith("ca: made")
        assert fed.cli.make_shared_ca({"EXPCORE_FED_CA_DIR": str(ca_dir)}).startswith("ca: already there")
    layouts = [staged(fed, tmp_path, spec, k, ca_dir) for k, spec in enumerate(specs, start=1)]
    prints = {fingerprint_sha256(load_platform(layout.platform).fingerprint()) for layout in layouts}
    assert len(prints) == (1 if shared else 2)
    for layout in layouts:
        text = layout.platform.read_text(encoding="utf-8")
        project = layout.spec.project
        assert f"network {project}_fed zone zone.{layout.spec.domain} local oak.{layout.spec.domain}" in text
        assert f"project {project}" in text
    if shared:
        assert not any(layout.secrets.joinpath("ca.key").exists() for layout in layouts)
        assert {layout.ca_pem.read_bytes() for layout in layouts} == {(ca_dir / "federation-ca.pem").read_bytes()}
        import fedidentity

        records = [fedidentity.record_of(layout.spec.platform_template, layout.platform, None) for layout in layouts]
        assert records[0] == records[1]
        assert set(records[0]) == {"version", "template_sha256", "platform_fingerprint_sha256"}  # no HPC pin


@pytest.mark.skipif(subprocess.run(["which", "openssl"], capture_output=True).returncode != 0, reason="no openssl")
def test_a_root_holding_another_ca_than_the_shared_one_is_refused(fed, tmp_path):
    (spec,) = instances(fed, tmp_path, 1)
    ca_dir = tmp_path / "shared-ca"
    fed.cli.make_shared_ca({"EXPCORE_FED_CA_DIR": str(ca_dir)})
    root = tmp_path / "parent" / spec.project
    (root / "ca").mkdir(parents=True)
    (root / "ca" / "federation-ca.pem").write_text("another CA\n", encoding="utf-8")
    layout = fed.layout.Layout(root, tmp_path / "parent", spec, ca_dir)
    with pytest.raises(RuntimeError, match="another CA"):
        fed.cli.use_shared_ca(layout)
    with pytest.raises(ValueError, match="outside the federation's root"):
        fed.layout.checked_ca_dir(str(root / "ca"), root)


@pytest.mark.skipif(subprocess.run(["which", "openssl"], capture_output=True).returncode != 0, reason="no openssl")
def test_an_up_without_the_shared_ca_never_makes_a_new_ca_over_the_roots_shared_one(fed, tmp_path):
    """A root brought up on the host's shared CA holds its certificate and no key: an up run without the setting is
    refused, so it never writes a CA of its own over the one the root's certificates chain to."""
    (spec,) = instances(fed, tmp_path, 1)
    ca_dir = tmp_path / "shared-ca"
    fed.cli.make_shared_ca({"EXPCORE_FED_CA_DIR": str(ca_dir)})
    layout = staged(fed, tmp_path, spec, 1, ca_dir)
    before = layout.ca_pem.read_bytes()
    unset = fed.layout.Layout(layout.root, layout.parent, spec, None)
    with pytest.raises(RuntimeError, match="EXPCORE_FED_CA_DIR"):
        fed.cli.prepare(unset)
    assert layout.ca_pem.read_bytes() == before
    assert not unset.ca_key.exists()
