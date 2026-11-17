"""O70: on the Apptainer backend the gate and the host mount dial the address the platform's map holds for a declared
name, and nothing of a cell holds the address, the node's name or the node's port."""

import json
from pathlib import Path

import pytest
from apptainer_support import LEVELS, TOKEN, make_settings, pinned_bundle, start
from fake_mount_node import FakeMountNode
from fake_node import FakeNode
from sandbox_support import SleptClock

from expcore_onedata.addresses import build
from expcore_onedata.sandbox.api import Endpoint, SandboxSpec
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import gaterun
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.tokens import Secret

ZONE_NAME, PROVIDER_NAME = "zone.layout.test", "alpha.layout.test"  # declared names: the URLs' hosts
ZONE_ADDRESS, PROVIDER_ADDRESS = "192.0.2.10", "192.0.2.11"  # documentation addresses: the nodes
NODE_PORT = "8443"
MAP = build({ZONE_NAME: (ZONE_ADDRESS, ["node-a.cluster.example.org"]), PROVIDER_NAME: (PROVIDER_ADDRESS, [])})


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


def _spec(level: str) -> SandboxSpec:
    return SandboxSpec(
        run_key="run-key-1",
        mode=LEVELS[level],
        token=Secret(TOKEN),
        zone=Endpoint.from_url(f"https://{ZONE_NAME}:{NODE_PORT}", MAP),
        provider=Endpoint.from_url(f"https://{PROVIDER_NAME}:{NODE_PORT}", MAP),
    )


def _bound_text(cell) -> str:
    parts = []
    for source, _ in cell.binds.values():
        path = Path(source)
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        parts += [p.read_bytes().decode("utf-8", "replace") for p in files]
    return "\n".join(parts)


def test_o70_the_endpoint_of_a_url_carries_the_address_its_host_is_dialled_at():
    """O70: a mapped host keeps its name and gets its address; an unmapped one has none, and the address is no part of
    the endpoint's repr."""
    mapped = Endpoint.from_url(f"https://{ZONE_NAME}:{NODE_PORT}", MAP)
    assert (mapped.host, mapped.port, mapped.address, mapped.dial) == (ZONE_NAME, 8443, ZONE_ADDRESS, ZONE_ADDRESS)
    assert ZONE_ADDRESS not in repr(mapped)
    plain = Endpoint.from_url("https://other.layout.test", MAP)
    assert (plain.address, plain.dial) == (None, "other.layout.test")
    assert Endpoint.from_url(f"https://{ZONE_NAME}").address is None


def test_o70_the_gate_serves_the_declared_names_and_dials_their_addresses(node, tmp_path):
    """O70: the gate's configuration names each upstream by its declared name (`addr`, the handshake's and the
    certificate check's) and its mapped address (`dial`); a host with no map entry has no `dial` key."""
    box = start(node, make_settings(tmp_path), _spec("via-gate"))
    try:
        assert node.gates[-1].config["hosts"] == {
            ZONE_NAME: {"role": "onezone", "addr": ZONE_NAME, "port": 8443, "tls": True, "dial": ZONE_ADDRESS},
            PROVIDER_NAME: {
                "role": "oneprovider",
                "addr": PROVIDER_NAME,
                "port": 8443,
                "tls": True,
                "dial": PROVIDER_ADDRESS,
            },
        }
    finally:
        box.stop()
    unmapped = gaterun.hosts(Endpoint("zone.layout.test", 8443), Endpoint("alpha.layout.test", 8443))
    assert not [h for h in unmapped.values() if "dial" in h]


def test_o70_nothing_a_cell_is_given_holds_an_address_a_node_name_or_the_node_port(node, tmp_path):
    """O70: no exec's argv, environment or bound file, and no file the cell's start binds, holds a mapped address; the
    environment, the hosts file and the gate's file hold neither the port nor any name but the declared ones."""
    box = start(node, make_settings(tmp_path), _spec("via-gate"))
    try:
        box.exec("print(1)", 30)
        assert node.execs
        for done in node.execs:
            seen = "\n".join([" ".join(done.argv), json.dumps(done.env), _bound_text(done)])
            assert ZONE_ADDRESS not in seen
            assert PROVIDER_ADDRESS not in seen
            assert "node-a.cluster.example.org" not in seen
            assert NODE_PORT not in json.dumps(done.env)
            for target in ("/etc/hosts", AV.GATE_INFO):
                assert target not in done.binds or NODE_PORT not in Path(done.binds[target][0]).read_text()
    finally:
        box.stop()


def test_o70_the_host_mount_dials_the_mapped_address(tmp_path):
    """O70: oneclient is started with the provider's address, which it dials without checking the name, and its port;
    the declared name is not on its command line."""
    node = FakeMountNode()
    settings = make_settings(tmp_path, oneclient_bundle=pinned_bundle(tmp_path / "files"))
    run = settings.work_root / "run-1"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    mount = HM.HostMount(
        node, settings, _spec("mounted"), run, mountinfo=node.mountinfo, sleep=clock.sleep, clock=clock
    )
    try:
        mount.start()
        argv = node.oneclient_argv
        assert argv[argv.index("-H") + 1] == PROVIDER_ADDRESS
        assert PROVIDER_NAME not in " ".join(argv)
        assert argv[argv.index("--port") + 1] == NODE_PORT
    finally:
        mount.stop()


def _mapped_config(tmp_path, monkeypatch):
    """A platform whose zone and local provider are declared names, with the map beside it."""
    from expcore_onedata.config import load_platform
    from expcore_onedata.testing import ADMIN_ENV, write_platform

    monkeypatch.setenv(ADMIN_ENV, "admin-token-0123456789")
    (tmp_path / "address-map.json").write_text(json.dumps(MAP.to_document()), encoding="utf-8")
    (tmp_path / "address-map.json").chmod(0o600)

    def edit(data):
        data["zone"]["url"] = f"https://{ZONE_NAME}:{NODE_PORT}"
        data["providers"]["alpha"]["url"] = f"https://{PROVIDER_NAME}:{NODE_PORT}"
        data["addresses"] = {"file": "address-map.json"}

    return load_platform(write_platform(tmp_path, edit))


def test_o70_the_platform_builds_the_run_s_endpoints_with_its_map(tmp_path, monkeypatch):
    """O70: the spec a run's sandbox is started with holds the zone's and the local provider's endpoints with the
    addresses the platform's map gives their declared names."""
    from types import SimpleNamespace

    from expcore_onedata.sandbox.api import DataPathMode
    from expcore_onedata.sandbox.provider import SandboxPlatform

    config = _mapped_config(tmp_path, monkeypatch)
    stub = SimpleNamespace(
        _s=SimpleNamespace(mode_of=lambda level: DataPathMode.REST, listing_of=lambda level: None), _config=config
    )
    spec = SandboxPlatform._spec(stub, "run-key-1", "via-gate", Secret(TOKEN), None)
    assert (spec.zone.host, spec.zone.address) == (ZONE_NAME, ZONE_ADDRESS)
    assert (spec.provider.host, spec.provider.address) == (PROVIDER_NAME, PROVIDER_ADDRESS)


def test_o70_the_day_zero_reachability_check_connects_where_the_map_says(tmp_path, monkeypatch):
    """O70: the check's TCP samples go to the mapped address of the provider's declared name, never to the name."""
    from types import SimpleNamespace

    from expcore_onedata.day0 import checks

    config = _mapped_config(tmp_path, monkeypatch)
    dialled = []
    ctx = SimpleNamespace(
        config=config,
        samples=2,
        sleep=lambda seconds: None,
        tcp_connect=lambda host, port, timeout: dialled.append((host, port)) or 1.0,
    )
    checks._tcp(ctx, "alpha")
    assert dialled == [(PROVIDER_ADDRESS, 8443)] * 2
