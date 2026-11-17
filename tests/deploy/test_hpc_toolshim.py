"""O73: a Python tool server on an HPC node reaches a declared name that resolves nowhere, through a resolver shim of
the root's, and is given no `ONEDATA_MCP_NODE_NAMES`.

The server's environment is the one `toolserver.server_environment` builds for a platform that declares `[addresses]`
with a `resolver`; a real child Python process, started with exactly that environment, makes an async httpx call (as
onedata-mcp does, over anyio) to a real TLS service on the loopback whose certificate holds the declared name alone.
Names are invented (`.test` resolves nowhere).
"""

import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tls_peer import TlsPeer

from expcore.config.loading import ConfigError
from expcore_onedata.addresses import FORMAT
from expcore_onedata.config import load_platform
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.toolserver import ENV_ADDRESS_MAP, MANAGED_ENV, server_environment

SHIM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "toolshim"
ZONE = "zone.layout.test"
TOKEN = SimpleNamespace(reveal=lambda: "tok-0123456789")  # what `server_environment` asks of a token
CLIENT = """
import asyncio, os, httpx
async def main():
    async with httpx.AsyncClient() as client:
        answer = await client.get(os.environ["ONEDATA_ONEZONE_HOST"] + "/api/v3/onezone/configuration")
        print(answer.status_code)
asyncio.run(main())
"""


@pytest.fixture(autouse=True)
def _admin_credential(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-token-0123456789")


def _map(path: Path, hosts: dict[str, str], mode: int = 0o600) -> Path:
    path.write_text(
        json.dumps({"format": FORMAT, "hosts": {n: {"address": a, "nodes": []} for n, a in hosts.items()}}),
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def _config(tmp_path: Path, peer: TlsPeer, *, resolver: bool = True):
    _map(tmp_path / "map.json", {peer.name: "127.0.0.1"})

    def edit(data):
        data["zone"]["url"] = peer.url
        data["addresses"] = {"file": "map.json", **({"resolver": str(SHIM_DIR)} if resolver else {})}

    return load_platform(write_platform(tmp_path, edit))


def _run_server_client(config, peer: TlsPeer, extra=None) -> subprocess.CompletedProcess:
    """A child Python with exactly the server's environment, calling the zone as the server's client does."""
    env = server_environment(config, config.condition("read"), TOKEN, extra, ca_copy=peer.ca)
    return subprocess.run([sys.executable, "-c", CLIENT], env=env, capture_output=True, text=True, timeout=60)


def test_o73_the_servers_environment_reaches_a_declared_name_at_the_mapped_address_with_sni_and_host_of_the_name(
    tmp_path,
):
    """O73: given declared-name URLs, a Python server started with the environment the platform builds reaches a fake
    service at the mapped address; the handshake asked for the declared name, the certificate (which holds it alone)
    verified, and `Host` is the declared name with the port."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        done = _run_server_client(_config(tmp_path, peer), peer)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "200"
        assert peer.sni == [ZONE]
        assert peer.requests == [
            {"method": "GET", "path": "/api/v3/onezone/configuration", "host": f"{ZONE}:{peer.port}"}
        ]


def test_o73_without_the_resolver_the_same_server_cannot_resolve_the_declared_name(tmp_path):
    """O73: it is the shim that resolves the name: a platform with the map and no `resolver` leaves the server's
    environment as it was, and the name fails to resolve."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        config = _config(tmp_path, peer, resolver=False)
        env = server_environment(config, config.condition("read"), TOKEN, ca_copy=peer.ca)
        assert "PYTHONPATH" not in env
        assert ENV_ADDRESS_MAP not in env
        done = _run_server_client(config, peer)
        assert done.returncode != 0
        assert peer.requests == []


def test_o73_the_certificate_is_still_checked_for_the_declared_name(tmp_path):
    """O73: the shim moves the connection, never the identity: a name the certificate does not hold is refused even
    though the map sends it to the same node."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        _map(tmp_path / "map.json", {"other.layout.test": "127.0.0.1"})

        def edit(data):
            data["zone"]["url"] = peer.url.replace(ZONE, "other.layout.test")
            data["addresses"] = {"file": "map.json", "resolver": str(SHIM_DIR)}

        config = load_platform(write_platform(tmp_path, edit))
        done = _run_server_client(config, peer)
        assert done.returncode != 0
        assert peer.requests == []


def test_o73_the_hpc_tool_servers_environment_has_no_node_names_setting(tmp_path):
    """O73: onedata-mcp runs unset on both platforms, so its text is the same on both: once the services register the
    declared names there is nothing to map. The environment of a server on a federation of declared names, and of one
    with no map at all, holds no `ONEDATA_MCP_NODE_NAMES`, and a caller cannot add one through the managed names."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        config = _config(tmp_path, peer)
        env = server_environment(config, config.condition("read"), TOKEN, ca_copy=peer.ca)
        assert "ONEDATA_MCP_NODE_NAMES" not in env
        assert env["PYTHONPATH"] == str(SHIM_DIR)
        assert env[ENV_ADDRESS_MAP] == str(tmp_path / "map.json")
        assert env["ONEDATA_ONEZONE_HOST"] == peer.url  # the declared name, never a node's
    (tmp_path / "plain").mkdir()
    plain = load_platform(write_platform(tmp_path / "plain"))
    plain_env = server_environment(plain, plain.condition("read"), TOKEN)
    assert "ONEDATA_MCP_NODE_NAMES" not in plain_env
    assert not {"PYTHONPATH", ENV_ADDRESS_MAP} & set(plain_env)


def test_o73_the_callers_own_python_path_follows_the_shims_and_the_map_setting_is_the_platforms(tmp_path):
    """O73: the shim's directory comes first on the server's `PYTHONPATH`, the caller's after it; the map's setting is
    managed, so a caller cannot point the shim at another file."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        config = _config(tmp_path, peer)
        env = server_environment(config, config.condition("read"), TOKEN, {"PYTHONPATH": "/srv/study"}, ca_copy=peer.ca)
        assert env["PYTHONPATH"] == f"{SHIM_DIR}:/srv/study"
        assert ENV_ADDRESS_MAP in MANAGED_ENV
        with pytest.raises(ValueError, match=ENV_ADDRESS_MAP):
            server_environment(config, config.condition("read"), TOKEN, {ENV_ADDRESS_MAP: "/x"}, ca_copy=peer.ca)


@pytest.mark.parametrize(
    "edit",
    [
        lambda d, tmp: d.update(addresses={"file": "map.json", "resolver": "absent"}),
        lambda d, tmp: d.update(addresses={"file": "map.json", "resolver": "map.json"}),
        lambda d, tmp: (tmp / "empty").mkdir() or d.update(addresses={"file": "map.json", "resolver": "empty"}),
    ],
    ids=["absent", "a-file", "no-shim"],
)
def test_o73_a_resolver_directory_without_the_shim_is_refused_naming_the_key(tmp_path, edit):
    """O73: `[addresses] resolver` is a directory that holds `sitecustomize.py` as a regular file, or the platform is
    refused naming the key and never the path."""
    _map(tmp_path / "map.json", {ZONE: "127.0.0.1"})
    with pytest.raises(ConfigError, match="resolver") as refused:
        load_platform(write_platform(tmp_path, lambda d: edit(d, tmp_path)))
    assert str(tmp_path) not in str(refused.value).split(": ", 1)[-1]


# --- the shim itself: exactly the map's names, nothing else ---


def _load_shim(monkeypatch, map_path):
    """The shim imported by its path, `socket.getaddrinfo` stood in by a recorder, the map named by the environment."""
    calls = []

    def real(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    monkeypatch.setattr(socket, "getaddrinfo", real)
    monkeypatch.setenv(ENV_ADDRESS_MAP, str(map_path))
    spec = importlib.util.spec_from_file_location("shim_under_test", SHIM_DIR / "sitecustomize.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def test_o73_the_shim_resolves_exactly_the_names_of_the_map_and_no_other_host(tmp_path, monkeypatch):
    """O73: a declared name (in any case, as bytes too) is resolved to its address, an IPv6 address included; any other
    host, a node's own name among them, is resolved as it was, and nothing about the map is printed."""
    path = _map(tmp_path / "map.json", {ZONE: "192.0.2.10", "beta.layout.test": "2001:db8::2"})
    _, calls = _load_shim(monkeypatch, path)
    for host in (ZONE, "ZONE.layout.test", b"zone.layout.test", "beta.layout.test", "elsewhere.test", "127.0.0.1", ""):
        socket.getaddrinfo(host, 8443)
    assert calls == ["192.0.2.10", "192.0.2.10", "192.0.2.10", "2001:db8::2", "elsewhere.test", "127.0.0.1", ""]
    socket.getaddrinfo("zone.layout.test", 8443, 0, 1, 6, 0)  # the full signature reaches the real call
    assert calls[-1] == "192.0.2.10"


@pytest.mark.parametrize("why", ["mode", "link", "absent", "garbled", "format", "unset"])
def test_o73_a_map_the_shim_may_not_use_maps_no_name_and_raises_nothing(tmp_path, monkeypatch, why):
    """O73: a map readable by others, a link, a missing or garbled file, or another format is no map: the shim maps no
    name (the server then cannot resolve a declared name, which is the failure), and importing it never raises."""
    good = _map(tmp_path / "good.json", {ZONE: "192.0.2.10"})
    path = {
        "mode": lambda: _map(tmp_path / "m.json", {ZONE: "192.0.2.10"}, 0o640),
        "link": lambda: (tmp_path / "l.json").symlink_to(good) or tmp_path / "l.json",
        "absent": lambda: tmp_path / "absent.json",
        "garbled": lambda: _garbled(tmp_path),
        "format": lambda: _other_format(tmp_path),
        "unset": lambda: "",
    }[why]()
    module, calls = _load_shim(monkeypatch, path)
    assert module.MAP == {}
    socket.getaddrinfo(ZONE, 8443)
    assert calls == [ZONE]


def _garbled(tmp_path):
    (tmp_path / "g.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "g.json").chmod(0o600)
    return tmp_path / "g.json"


def _other_format(tmp_path):
    (tmp_path / "f.json").write_text(json.dumps({"format": "other/1", "hosts": {ZONE: {"address": "192.0.2.10"}}}))
    (tmp_path / "f.json").chmod(0o600)
    return tmp_path / "f.json"


def test_o73_the_shim_imports_nothing_of_the_core_and_prints_nothing():
    """O73: it runs inside the server's own interpreter, which does not hold the core: standard library only, and no
    print (the server's stdout is its protocol)."""
    import ast

    tree = ast.parse((SHIM_DIR / "sitecustomize.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"json", "os", "socket", "stat"}
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "print"]
