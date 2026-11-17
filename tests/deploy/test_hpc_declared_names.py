"""O73: the HPC federation's driver registers each service under the name its layout declares and reaches it by that
name: `up` writes a private map from each declared name to the node's address, the services' URLs, the registration's
inputs and the hosts file every service gets carry the declared names, the facts record what each service registered
and that the DNS check is advisory, and a `[sandbox.federation]` table that is not the registered names is refused.

Against the fake Slurm of hpc_fed_support, with invented names (`.test` for the layout's, `example.org` for the nodes').
"""

import json
import re
from types import SimpleNamespace

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path

from expcore_onedata.addresses import AddressMap
from expcore_onedata.config import load_platform

PROVIDERS = ["aspen", "larch", "rowan", "alder", "hazel"]
DOMAIN = "expcore-hpc-pilot.test"  # the pilot layout's `domain`
ZONE_NAME = f"zone.{DOMAIN}"
DECLARED = {"zone": ZONE_NAME, **{p: f"{p}.{DOMAIN}" for p in PROVIDERS}}
NODES = ["zone", *PROVIDERS]  # the services' roles, on node01 to node06 in order
FQDN = {role: f"node{i + 1:02d}.cluster.example.org" for i, role in enumerate(NODES)}
ADDRESS = {role: f"10.0.0.{i + 1}" for i, role in enumerate(NODES)}


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcnames

        modules.names = hpcnames
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

    def __init__(self, hpc, tmp_path, site):
        self.hpc = hpc
        self.fake = FakeSlurm()
        self.root = tmp_path / "fed" / "root"
        self.site = site
        self.driver = hpc.cli.make_driver(LAYOUT, str(self.root), runner=self.fake)
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        self.core_calls = []
        self.map_at_launch = []  # whether the map file existed when each service was launched
        spawn = self.fake.spawn

        def watching(argv, client_log):
            self.map_at_launch.append((self.root / "secrets" / "address-map.json").exists())
            return spawn(argv, client_log)

        self.fake.spawn = watching
        self.driver.core = self.core
        self.driver.relay_available = lambda: False

    def core(self, layout, owner, values, environ=None):
        self.core_calls.append((layout, owner, values))
        return "core: the link check passed"

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))


@pytest.fixture
def rig(hpc, tmp_path, site):
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    return rig


def test_o73_up_writes_the_address_map_into_the_root_private_before_any_service_starts(rig):
    """O73: from each declared name to the node's address and the node's own names, a 0600 file in the root's secrets,
    written once the nodes' facts are read and before the first service is launched, which the platform's clients and
    the tool server's names are built from."""
    path = rig.root / "secrets" / "address-map.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert rig.map_at_launch == [True] * 6
    found = AddressMap.load(path)
    for role, name in DECLARED.items():
        assert found.address_of(name) == ADDRESS[role]
        assert found.hosts[name].names == (FQDN[role], FQDN[role].split(".")[0])
    assert set(found.hosts) == set(DECLARED.values())  # the agent's node holds no service and no name


def test_o73_up_installs_the_resolver_shim_in_the_root_beside_the_map(rig, hpc):
    """O73: the shim a Python tool server resolves the declared names by is copied into the root (a directory of the
    driver's alone, the file a regular copy of the checkout's, replaced by a later `up`), before any service is
    launched, so a copy of the platform file beside a study names the root's own."""
    directory = rig.root / "toolshim"
    shim = directory / "sitecustomize.py"
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    assert shim.is_file()
    assert not shim.is_symlink()
    assert shim.read_bytes() == hpc.names.SHIM.read_bytes()
    assert rig.map_at_launch == [True] * 6
    shim.write_text("# an older copy\n", encoding="utf-8")
    assert hpc.names.install_toolshim(directory) == directory
    assert shim.read_bytes() == hpc.names.SHIM.read_bytes()
    shim.unlink()
    shim.symlink_to(hpc.names.SHIM)
    with pytest.raises(RuntimeError, match="link"):
        hpc.names.install_toolshim(directory)


def test_o73_the_address_map_is_in_nothing_but_the_root_s_private_file_and_the_state(rig):
    """O73: no other file of the root, but the state (which holds every node's name and address already, and is the
    driver's own), holds a node's address."""
    holders = []
    for path in sorted(rig.root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(address in text for address in ADDRESS.values()):
                holders.append(path.name)
    assert sorted(holders) == ["address-map.json", "hpc-federation.json"]


def test_o73_the_services_urls_carry_the_declared_names_on_the_public_port(rig):
    """O73: the layout the core's phase runs over has each service at its declared name, so every URL it builds,
    writes and records for the zone and the providers is the declared name, never a node's."""
    layout, _, _ = rig.core_calls[0]
    assert layout.spec.zone.url == f"https://{ZONE_NAME}:8443"
    for provider in PROVIDERS:
        assert layout.spec.provider(provider).url == f"https://{provider}.{DOMAIN}:8443"
        assert layout.spec.provider(provider).domain == DECLARED[provider]
    assert layout.spec.zone.domain == ZONE_NAME
    assert not [s.url for s in layout.spec.services if "cluster.example.org" in s.url or "10.0.0." in s.url]


def test_o73_the_zone_and_each_provider_register_the_declared_names_and_are_given_every_peer_s(rig):
    """O73: the zone's step names its own declared name and every service's, and each provider's the zone's declared
    name in the place the zone's FQDN had, its own declared name and every service's: with the address of each, so the
    hosts file of every cell resolves every peer. No node's FQDN is on any service's command line."""
    zone, *providers = rig.fake.spawned
    hosts = [f"--host={DECLARED[role]}={ADDRESS[role]}" for role in NODES]
    assert [w for w in zone.command if w.startswith("--")] == [f"--declared={ZONE_NAME}", *sorted(hosts)]
    for index, step in enumerate(providers):
        name = PROVIDERS[index]
        assert step.command[4] == name
        assert step.command[5:8] == [ZONE_NAME, "admin@example.org", ADDRESS["zone"]]  # the zone's registered name
        assert [w for w in step.command if w.startswith("--")] == [f"--declared={DECLARED[name]}", *sorted(hosts)]
    for step in rig.fake.spawned:
        assert not [w for w in step.command if "cluster.example.org" in w]


def test_o73_a_service_without_a_node_address_is_refused_before_anything_is_launched(hpc):
    """O73: the hosts file lists every peer, so a service the state has no address for stops the host arguments, naming
    the role and no address or node name."""
    services = {role: {"fqdn": FQDN[role], "address": ADDRESS[role]} for role in NODES}
    services["rowan"] = {"fqdn": FQDN["rowan"], "address": None}
    with pytest.raises(RuntimeError, match="rowan has no node address") as refused:
        hpc.names.address_map(DECLARED, services)
    assert not re.search(r"10\.0\.0|example\.org", str(refused.value))
    del services["rowan"]
    with pytest.raises(RuntimeError, match="rowan has no node address"):
        hpc.names.address_map(DECLARED, services)


def test_o73_two_services_on_one_node_are_refused(hpc):
    """O73: a node address belongs to one declared name only, as the tool server's names rule needs: two roles with one
    address fail, naming no value."""
    services = {role: {"fqdn": FQDN[role], "address": ADDRESS[role]} for role in NODES}
    services["larch"]["address"] = ADDRESS["aspen"]
    with pytest.raises(RuntimeError, match="one node's name or address") as refused:
        hpc.names.address_map(DECLARED, services)
    assert "10.0.0" not in str(refused.value)


def test_o73_the_state_records_each_services_declared_name(rig):
    """O73: `describe` and the later steps read the declared name from the state, beside the node's own."""
    state = json.loads((rig.root / "hpc-federation.json").read_text(encoding="utf-8"))
    for role, name in DECLARED.items():
        assert state["services"][role]["declared"] == name
        assert state["services"][role]["fqdn"] == FQDN[role]


# --- the core's phase: the clients, the platform file and the facts ---------------------------------------------------


@pytest.fixture
def phase(hpc, tmp_path):
    """The core's phase over a root with a map, the bootstrap and the REST client stood in."""
    import fedlayout
    import fedspec

    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")

    root = tmp_path / "root"
    (root / "secrets" / "users").mkdir(parents=True)
    (root / "ca").mkdir()
    spec = fedspec.load_spec(LAYOUT)
    hosts = {s.name: DECLARED[s.name] for s in spec.services}
    layout = fedlayout.Layout(root, tmp_path, hpc.boot.hpc_spec(spec, hosts))
    files = {"secrets/zone.passphrase": "A1b2C3d4E5f6G7h8", "secrets/admin.token": "an-admin-token"}
    files.update({f"secrets/users/{p.name}.pass": "a-password" for p in spec.principals})
    for name, value in files.items():
        (root / name).write_text(value, encoding="utf-8")
        (root / name).chmod(0o600)
    (root / "ca" / "federation-ca.pem").write_text(tls.make_federation_ca(tmp_path / "pki").ca.read_text("ascii"))
    hpc.names.install_toolshim(root / "toolshim")  # as `up` leaves the root, before the core's phase
    return SimpleNamespace(root=root, layout=layout, spec=spec, tmp=tmp_path)


def _run(hpc, phase, *, with_map, **more):
    """`core_phase` with every collaborator stood in; returns the arguments the REST client was built with."""
    from hpc_fed_support import data_stand_in, pin_values

    built = []
    if with_map:
        AddressMap.from_document(
            {
                "format": "expcore-address-map/1",
                "hosts": {DECLARED[r]: {"address": ADDRESS[r], "nodes": [FQDN[r]]} for r in NODES},
            }
        ).write(phase.layout.address_map)

    def rest(ca, **kwargs):
        built.append(kwargs)
        return SimpleNamespace(close=lambda: None)

    ids = SimpleNamespace(
        admin="uid-admin",
        providers={p.name: f"pid-{p.name}" for p in phase.spec.providers},
        spaces={s.name: f"sid-{s.name}" for s in phase.spec.spaces},
        users={p.name: f"uid-{p.name}" for p in phase.spec.principals},
        scans={},
    )
    registered = more.pop("registered", dict(DECLARED))

    def boot(client, layout, owner):
        return SimpleNamespace(layout=layout, ids=ids, spec=layout.spec, registered=registered, rest=client)

    values = {**hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, phase.root),
              **pin_values(phase.tmp / "agent")}  # fmt: skip
    out = hpc.boot.core_phase(
        phase.layout, (5001, 5001), values, rest=rest, bootstrap=boot, steps=lambda b: None,
        content=lambda b: SimpleNamespace(reset=lambda t: None), data=data_stand_in(),
        links=lambda layout, spaces: "link check: stood in", verify=lambda *a, **k: None, **more,
    )  # fmt: skip
    return built, out


def test_o73_the_core_s_rest_client_is_given_the_map_when_the_root_holds_one(hpc, phase):
    """O73: the bootstrap's client dials each declared name at its address; a root without a map (a Compose one) builds
    its client as it always has."""
    built, _ = _run(hpc, phase, with_map=True)
    (kwargs,) = built
    assert isinstance(kwargs["addresses"], AddressMap)
    assert kwargs["addresses"].address_of(ZONE_NAME) == ADDRESS["zone"]
    phase.layout.address_map.unlink()
    built, _ = _run(hpc, phase, with_map=False)
    assert built == [{}]


def test_o73_the_platform_file_names_the_map_and_loads_with_declared_urls(hpc, phase, monkeypatch):
    """O73: the rendered platform.toml carries `addresses` (the root's own file, by its absolute path) first, its URLs
    are the declared names, and the platform layer loads it with the map; none of its text holds a node's name or
    address."""
    from expcore_onedata.testing import ADMIN_ENV

    monkeypatch.setenv(ADMIN_ENV, "admin-token-0123456789")
    _run(hpc, phase, with_map=True)
    text = (phase.root / "platform.toml").read_text(encoding="utf-8")
    assert text.startswith('addresses = { file = "')
    assert f', resolver = "{phase.root}/toolshim" }}\n' in text.split("\n", 1)[0] + "\n"
    assert f'url = "https://{ZONE_NAME}:8443"' in text
    assert not re.search(r"cluster\.example\.org|10\.0\.0\.", text)
    config = load_platform(phase.root / "platform.toml", require_secrets=False)
    assert config.addresses.address_of(ZONE_NAME) == ADDRESS["zone"]
    assert config.addresses_resolver == phase.root / "toolshim"
    assert config.zone.url == f"https://{ZONE_NAME}:8443"


def test_o73_a_root_without_a_map_renders_the_platform_file_as_before(hpc, phase):
    """O73: no `addresses` line without a map."""
    _run(hpc, phase, with_map=False)
    assert "addresses" not in (phase.root / "platform.toml").read_text(encoding="utf-8")


def test_o73_the_facts_hold_what_each_service_registered_and_that_the_dns_check_is_advisory(hpc, phase):
    """O73: facts.json names, per service, the domain it registered under (read back from its `/configuration`), and
    the DNS check as advisory: Onepanel's own check cannot resolve a layout's name, which only the cells' hosts files
    know, so it is expected `unresolvable` and no step of `up` fails on it. No node's name or address is in the file."""
    _run(hpc, phase, with_map=True)
    facts = json.loads((phase.root / "facts.json").read_text(encoding="utf-8"))
    assert facts["zone"]["registered_domain"] == ZONE_NAME
    assert {p: v["registered_domain"] for p, v in facts["providers"].items()} == {p: DECLARED[p] for p in PROVIDERS}
    assert facts["dns_check"]["advisory"] is True
    assert facts["dns_check"]["expected"] == "unresolvable"
    assert facts["dns_check"]["measured"] is False
    assert not re.search(r"cluster\.example\.org|10\.0\.0\.", json.dumps(facts))


def test_o73_a_service_that_registered_another_name_fails_up_naming_its_declared_one_only(hpc, phase):
    """O73: the read-back of `/configuration` must give the declared name; a node's FQDN (an older registration) fails,
    and the message names the role and the declared name, never the name it registered."""
    wrong = {**DECLARED, "larch": FQDN["larch"]}
    with pytest.raises(RuntimeError, match="larch registered under a name other than its declared") as refused:
        _run(hpc, phase, with_map=True, registered=wrong)
    assert FQDN["larch"] not in str(refused.value)
    assert DECLARED["larch"] in str(refused.value)


def _table(zone, provider):
    return f'[sandbox.federation]\nzone = "{zone}"\nproviders.aspen = "{provider}"\n'


def test_o73_a_federation_table_of_the_registered_names_is_accepted_and_none_is_no_check(hpc):
    """O73: `[sandbox.federation]` names the zone's and the local provider's names; the registered ones pass, and a
    platform file without the table is not checked."""
    ok = hpc.names.check_federation_table(_table(ZONE_NAME, DECLARED["aspen"]), dict(DECLARED))
    assert ok is True
    assert hpc.names.check_federation_table("[sandbox]\nbackend = 'apptainer'\n", dict(DECLARED)) is False


@pytest.mark.parametrize(
    ("table", "why"),
    [
        (_table("zone.other.test", DECLARED["aspen"]), "zone"),
        (_table(ZONE_NAME, "aspen.other.test"), "aspen"),
    ],
)
def test_o73_a_federation_table_of_other_names_is_refused_naming_the_table_s_own(hpc, table, why):
    """O73: a table that names what no service registered under is refused, the message naming the table's own name
    and the role."""
    with pytest.raises(RuntimeError, match=why) as refused:
        hpc.names.check_federation_table(table, dict(DECLARED))
    assert "ordering" in str(refused.value)


def test_o73_a_federation_table_over_an_older_registration_is_refused_without_naming_the_nodes(hpc):
    """O73: services that registered their node's names (an `up` before the declared names) refuse the table of the
    declared names, and the message names the table's own names alone, never the name that was registered."""
    registered = {role: FQDN[role] for role in NODES}
    with pytest.raises(RuntimeError, match="zone") as refused:
        hpc.names.check_federation_table(_table(ZONE_NAME, DECLARED["aspen"]), registered)
    assert not re.search(r"cluster\.example\.org|10\.0\.0", str(refused.value))
    assert ZONE_NAME in str(refused.value)


def test_o73_a_federation_table_with_no_record_of_what_was_registered_is_refused(hpc):
    """O73: a layout-derived name proves nothing about registration: a table, with no record of the registered names, is
    refused."""
    with pytest.raises(RuntimeError, match="no record"):
        hpc.names.check_federation_table(_table(ZONE_NAME, DECLARED["aspen"]), {})


def test_o73_the_core_s_phase_refuses_a_platform_whose_table_is_not_the_registered_names(hpc, phase):
    """O73: the check runs as the platform file is rendered, from the facts' record of what each service registered,
    before the file is loaded and the link check runs: a template that declares the table with names the services did
    not register fails `up` there, naming the declared names alone."""
    import dataclasses

    import fedlayout

    table = phase.tmp / "table.toml"
    table.write_text(_table("zone.other.test", DECLARED["aspen"]), encoding="utf-8")
    spec = dataclasses.replace(phase.layout.spec, platform_template=table)
    phase.layout = fedlayout.Layout(phase.root, phase.tmp, spec)
    with pytest.raises(RuntimeError, match="zone") as refused:
        _run(hpc, phase, with_map=True)
    assert FQDN["zone"] not in str(refused.value)
    assert not (phase.root / "platform.toml").exists()


def test_o73_the_hpc_service_keeps_the_layouts_domain_and_is_no_domain_of_its_own_domain(hpc):
    """O73: `hpc_spec` builds each service from the layout's own domain (the declared name is `<service>.<domain>`),
    not from the service's declared name, which doubled the service's name in it."""
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    placed = hpc.boot.hpc_spec(spec, {s.name: DECLARED[s.name] for s in spec.services})
    assert [s.domain for s in placed.services] == [DECLARED[s.name] for s in spec.services]
