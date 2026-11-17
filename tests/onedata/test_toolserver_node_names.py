"""O77: the tool server of a federation reached by the names its layout declares is given `ONEDATA_MCP_NODE_NAMES`,
built from the platform's address map, so it names no node by its own name, its address or its port in what it
returns; a Compose federation, whose names are already the declared ones, is given none."""

import json
from types import SimpleNamespace

import pytest
from mcp_node_names import server_reads

from expcore_onedata.addresses import FORMAT
from expcore_onedata.config import load_platform
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.toolserver import ENV_NODE_NAMES, MANAGED_ENV, node_names, server_environment

ZONE, ALPHA, BETA = "zone.layout.test", "alpha.layout.test", "beta.layout.test"
NODES = {
    ZONE: ("192.0.2.10", ["node-a.cluster.example.org", "node-a"]),
    ALPHA: ("192.0.2.11", ["node-b.cluster.example.org", "node-b"]),
    BETA: ("2001:db8::12", []),
}
TOKEN = SimpleNamespace(reveal=lambda: "tok-0123456789")  # what `server_environment` asks of a token


@pytest.fixture(autouse=True)
def _admin_credential(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-token-0123456789")


def _config(tmp_path, *, mapped=True, ports=(8443, 8443, 9443)):
    zone, alpha, beta = ports
    (tmp_path / "map.json").write_text(
        json.dumps(
            {
                "format": FORMAT,
                "hosts": {n: {"address": a, "nodes": list(names)} for n, (a, names) in NODES.items()},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "map.json").chmod(0o600)

    def edit(data):
        data["zone"]["url"] = f"https://{ZONE}:{zone}"
        data["providers"]["alpha"]["url"] = f"https://{ALPHA}:{alpha}"
        data["providers"]["beta"]["url"] = f"https://{BETA}:{beta}"
        if mapped:
            data["addresses"] = {"file": "map.json"}

    return load_platform(write_platform(tmp_path, edit))


def test_o77_a_mapped_platform_gives_the_tool_server_every_real_name_under_its_declared_name(tmp_path):
    """O77: each node's names and address, bare and with the URLs' ports and 443, and each declared name under itself,
    valid by the server's own rules (a key a host, `host:port` or `[ipv6]:port`, a value a declared name, one host under
    one name)."""
    config = _config(tmp_path)
    env = server_environment(config, config.condition("read"), TOKEN)
    names = json.loads(env[ENV_NODE_NAMES])
    assert server_reads(env[ENV_NODE_NAMES])  # the server accepts it
    for key in ("192.0.2.10", "192.0.2.10:8443", "192.0.2.10:443", "node-a.cluster.example.org:8443", "node-a:443"):
        assert names[key] == ZONE
    assert names["node-b.cluster.example.org"] == names["192.0.2.11:9443"] == names["192.0.2.11:8443"] == ALPHA
    assert names["2001:db8::12"] == names["[2001:db8::12]:8443"] == names["[2001:db8::12]:9443"] == BETA
    for declared in (ZONE, ALPHA, BETA):
        assert names[declared] == names[f"{declared}:8443"] == declared  # a port after a declared name goes with it


def test_o77_the_value_is_built_from_the_same_map_the_clients_dial_through(tmp_path):
    """O77: every address the clients are given for a declared name is a key of the value."""
    config = _config(tmp_path)
    names = json.loads(node_names(config))
    for declared, node in config.addresses.hosts.items():
        assert names[node.address] == declared
        assert all(names[n] == declared for n in node.names)


def test_o77_a_platform_without_a_map_gives_the_tool_server_no_node_names(tmp_path):
    """O77: Compose stays unset: its names are the declared ones already."""
    config = _config(tmp_path, mapped=False)
    assert node_names(config) is None
    assert ENV_NODE_NAMES not in server_environment(config, config.condition("read"), TOKEN)


def test_o77_the_caller_cannot_set_the_node_names(tmp_path):
    """O77: the setting is the platform's, like every managed one: a caller's value is refused by name."""
    config = _config(tmp_path)
    assert ENV_NODE_NAMES in MANAGED_ENV
    with pytest.raises(ValueError, match=ENV_NODE_NAMES):
        server_environment(config, config.condition("read"), TOKEN, {ENV_NODE_NAMES: "{}"})
