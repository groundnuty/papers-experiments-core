"""O70: nothing a REST cell receives holds a node's name, a node's address or the nodes' public port, for a federation
whose services register and are reached by the names their layout declares.

The layout is a fake of the HPC pilot's shape with node-shaped names (a rack, a chassis and a blade in each node's name)
and addresses of the documentation range; the platform file is the one the driver renders (`addresses`, declared URLs),
loaded as a study loads it; the cell runs on the fake node, with real certificates made by openssl. Every file bound
into the cell, every environment and every command line is searched, and the certificates' names are read from them.
"""

import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from apptainer_support import LEVELS, TOKEN, make_settings, start
from fake_node import FakeNode

from expcore_onedata.addresses import FORMAT
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.hostops import DockerHost
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.sandbox.settings import FederationNetwork
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.tokens import Secret

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the certificates")

ZONE, LOCAL = "zone.fed-a.test", "alpha.fed-a.test"  # the layout's declared names
NODES = {  # declared name -> (address, the node's own names): realistic shapes, every one invented
    ZONE: ("198.51.100.21", ["cn-r3c07-n12.cluster.example.org", "cn-r3c07-n12"]),
    LOCAL: ("198.51.100.22", ["cn-r3c07-n13.cluster.example.org", "cn-r3c07-n13"]),
    "beta.fed-a.test": ("198.51.100.23", ["cn-r4c02-n03.cluster.example.org", "cn-r4c02-n03"]),
}
PORT = 8443


def forbidden() -> list[re.Pattern[str]]:
    """Each node's names and address, and the nodes' public port as a token of its own."""
    words = [w for address, names in NODES.values() for w in (address, *names)]
    return [re.compile(rf"(?<![\w.-]){re.escape(w)}(?![\w-])", re.IGNORECASE) for w in words] + [
        re.compile(rf"(?<![\w.-]){PORT}(?![\w-])")
    ]


@pytest.fixture(autouse=True)
def _admin_credential(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-token-0123456789")


@pytest.fixture
def config(tmp_path):
    """The platform the driver renders for the fake layout: declared URLs on the public port, the address map named."""
    (tmp_path / "address-map.json").write_text(
        json.dumps({"format": FORMAT, "hosts": {n: {"address": a, "nodes": ns} for n, (a, ns) in NODES.items()}}),
        encoding="utf-8",
    )
    (tmp_path / "address-map.json").chmod(0o600)

    def edit(data):
        data["zone"]["url"] = f"https://{ZONE}:{PORT}"
        data["providers"]["alpha"]["url"] = f"https://{LOCAL}:{PORT}"
        data["providers"]["beta"]["url"] = f"https://beta.fed-a.test:{PORT}"
        data["addresses"] = {"file": "address-map.json"}

    return load_platform(write_platform(tmp_path, edit))


def _spec(config):
    stub = SimpleNamespace(
        _s=SimpleNamespace(mode_of=lambda level: DataPathMode.REST, listing_of=lambda level: None), _config=config
    )
    return SandboxPlatform._spec(stub, "run-key-1", "via-gate", Secret(TOKEN), None)


def _certificate_names(path: Path) -> str:
    done = subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-text"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _everything_a_cell_receives(done) -> dict[str, str]:
    """Where a cell's inputs are: its command line and environment, and each file bound into it, whole directories
    included, by what it is bound as."""
    found = {"argv": " ".join(done.argv), "environment": json.dumps(done.env)}
    for target, (source, _) in done.binds.items():
        path = Path(source)
        for file in [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file()):
            found[f"{target} ({file.name})"] = file.read_bytes().decode("utf-8", "replace")
    return found


@pytest.mark.parametrize("table", [False, True], ids=["urls-are-the-names", "federation-table"])
def test_o70_no_cell_input_holds_a_node_name_an_address_or_the_public_port(config, tmp_path, table):
    """O70: the environment, the hosts file, the gate's file, the CA and every other file or argument a cell receives
    hold the declared names alone, with or without `[sandbox.federation]`; the gate's leaf certificates name them alone;
    what the run records (its metadata, its egress declaration and its files, but the gate's own directory) holds none
    either, and the gate, which the cell cannot read, is the one place the mapped addresses are."""
    node = FakeNode()
    node.make_ca = DockerHost().make_ca  # real certificates, whose names are read below
    (tmp_path / "sandbox").mkdir()
    settings = make_settings(tmp_path / "sandbox")
    if table:
        settings = dataclasses.replace(settings, federation=FederationNetwork(None, LOCAL, ZONE))
    box = start(node, settings, _spec(config))
    try:
        box.exec("print(1)", 30)
        assert node.execs
        patterns = forbidden()
        for done in node.execs:
            for where, text in _everything_a_cell_receives(done).items():
                for pattern in patterns:
                    assert not pattern.search(text), f"a cell input holds a node's name, address or port: {where}"
        seen = _everything_a_cell_receives(node.execs[-1])
        assert seen["environment"].count(ZONE) >= 1  # the declared name is what it was given instead
        assert ZONE in seen["/etc/hosts (hosts)"] if "/etc/hosts (hosts)" in seen else ZONE in " ".join(seen.values())
        leaves = sorted(p for p in (box.dir / "tls").glob("*.pem") if p.name != "ca.pem")
        assert sorted(p.stem for p in leaves) == sorted([ZONE, LOCAL])
        for leaf in leaves:
            text = _certificate_names(leaf)
            assert not [p for p in patterns if p.search(text)], "a certificate names a node or an address"
            assert f"DNS:{leaf.stem}" in text
        assert not [p for p in patterns if p.search(_certificate_names(box.dir / "tls" / "ca.pem"))]
        record = {
            "metadata": json.dumps(box.metadata(), default=str),
            "egress declaration": json.dumps(sorted(box.egress_declaration().hosts)),
        }
        for file in sorted(p for p in box.dir.rglob("*") if p.is_file() and "gate" not in p.relative_to(box.dir).parts):
            if file.suffix not in (".key", ".pem"):  # the certificates' names are read through openssl above
                record[f"the run's file {file.relative_to(box.dir)}"] = file.read_bytes().decode("utf-8", "replace")
        for where, text in record.items():  # what the run records and publishes, and its files but the gate's own
            assert not [p for p in patterns if p.search(text)], (
                f"the run's record holds a node's name or address: {where}"
            )
        dialled = node.gates[-1].config["hosts"]  # the gate's own configuration: the one place the addresses are
        assert {h["dial"] for h in dialled.values()} == {NODES[ZONE][0], NODES[LOCAL][0]}
    finally:
        box.stop()


def test_o70_the_levels_of_the_fake_are_the_apptainer_support_s(config):
    """O70: the scan above ran a REST level, which `apptainer_support` names `via-gate`."""
    assert LEVELS["via-gate"] is DataPathMode.REST
    assert _spec(config).zone.address == NODES[ZONE][0]


@pytest.mark.parametrize(
    "leak",
    [
        "host=cn-r3c07-n12.cluster.example.org",
        "198.51.100.22",
        "https://zone.fed-a.test:8443/api",
        "CN-R3C07-N13",
        '"port": 8443}',
    ],
)
def test_o70_the_search_finds_a_node_name_an_address_or_the_port_where_one_is(leak):
    """O70: the patterns the scan uses match each of the forms a leak takes, in any case, and not a declared name or a
    hash that holds the digits."""
    assert any(pattern.search(leak) for pattern in forbidden())
    clean = f"{ZONE} {LOCAL} 127.0.0.1 sha256:ab18443cd and port 40443"
    assert not [pattern for pattern in forbidden() if pattern.search(clean)]
