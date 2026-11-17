"""O73: a Python tool server on an HPC node reaches a declared name that resolves nowhere, because the platform
starts it through the resolve launcher, and is given no `ONEDATA_MCP_NODE_NAMES`.

The server's command is the one `toolserver.server_argv` builds for a platform that declares `[addresses]` with a
`resolver`, its environment the one `server_environment` builds; a real child Python process, started with exactly
those, makes an async httpx call (as onedata-mcp does, over anyio) to a real TLS service on the loopback whose
certificate holds the declared name alone. Names are invented (`.test` resolves nowhere).
"""

import json
import re
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tls_peer import TlsPeer

from expcore.config.loading import ConfigError
from expcore_onedata.addresses import FORMAT, AddressMapError, launch_argv
from expcore_onedata.config import load_platform
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.toolserver import server_argv, server_environment

LAUNCHER = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "toolshim" / "resolve_launch.py"
ZONE = "zone.layout.test"
TOKEN = SimpleNamespace(reveal=lambda: "tok-0123456789")  # what `server_environment` asks of a token
CLIENT = """
import asyncio, os, sys, httpx
async def main():
    async with httpx.AsyncClient() as client:
        answer = await client.get(os.environ["ONEDATA_ONEZONE_HOST"] + "/api/v3/onezone/configuration")
        print(answer.status_code, sys.argv[1:])
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


def _config(tmp_path: Path, peer: TlsPeer, *, resolver: bool = True, server=("-m", "expcore_onedata.testing.x")):
    """A platform whose zone is the peer's declared name, mapped to the loopback; its tool server python + `server`."""
    _map(tmp_path / "map.json", {peer.name: "127.0.0.1"})

    def edit(data):
        data["zone"]["url"] = peer.url
        data["addresses"] = {"file": "map.json", **({"resolver": str(LAUNCHER)} if resolver else {})}
        data["toolserver"]["command"] = sys.executable
        data["toolserver"]["args"] = list(server)

    return load_platform(write_platform(tmp_path, edit))


def _run_server(config, peer: TlsPeer) -> subprocess.CompletedProcess:
    """A child with exactly the server's command line and environment, calling the zone as the server's client does."""
    command, args = server_argv(config)
    env = server_environment(config, config.condition("read"), TOKEN, ca_copy=peer.ca)
    return subprocess.run([command, *args], env=env, capture_output=True, text=True, timeout=60)


def test_o73_the_server_started_as_the_platform_says_reaches_a_declared_name_at_the_mapped_address(tmp_path):
    """O73: given declared-name URLs, a Python server started with the command and environment the platform builds
    reaches a fake service at the mapped address; the handshake asked for the declared name, the certificate (which
    holds it alone) verified, and `Host` is the declared name with the port. The server's own arguments reach it."""
    client = tmp_path / "client.py"
    client.write_text(CLIENT, encoding="utf-8")
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        done = _run_server(_config(tmp_path, peer, server=(str(client), "one", "two")), peer)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "200 ['one', 'two']"
        assert done.stderr == ""  # the server's stdout is its protocol: the launcher adds nothing
        assert peer.sni == [ZONE]
        assert peer.requests == [
            {"method": "GET", "path": "/api/v3/onezone/configuration", "host": f"{ZONE}:{peer.port}"}
        ]


def test_o73_without_the_launcher_the_same_server_cannot_resolve_the_declared_name(tmp_path):
    """O73: it is the launcher that resolves the name: a platform with the map and no `resolver` starts the server as
    declared, and the name fails to resolve."""
    client = tmp_path / "client.py"
    client.write_text(CLIENT, encoding="utf-8")
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        config = _config(tmp_path, peer, resolver=False, server=(str(client),))
        assert server_argv(config) == (sys.executable, [str(client)])
        assert _run_server(config, peer).returncode != 0
        assert peer.requests == []


def test_o73_the_certificate_is_still_checked_for_the_declared_name(tmp_path):
    """O73: the launcher moves the connection, never the identity: a name the certificate does not hold is refused
    though the map sends it to the same node."""
    client = tmp_path / "client.py"
    client.write_text(CLIENT, encoding="utf-8")
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        _map(tmp_path / "map.json", {"other.layout.test": "127.0.0.1"})

        def edit(data):
            data["zone"]["url"] = peer.url.replace(ZONE, "other.layout.test")
            data["addresses"] = {"file": "map.json", "resolver": str(LAUNCHER)}
            data["toolserver"].update(command=sys.executable, args=[str(client)])

        config = load_platform(write_platform(tmp_path, edit))
        assert _run_server(config, peer).returncode != 0
        assert peer.requests == []


def test_o73_the_server_is_started_isolated_and_nothing_names_the_map_in_its_environment(tmp_path):
    """O73: the server's argv is the declared interpreter, `-I -B` (as every Python of the driver's), the launcher and
    the map as arguments; its environment has no `PYTHONPATH`, no `ONEDATA_MCP_NODE_NAMES` and no setting that names the
    map; and the declared name is what its URL holds."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        config = _config(tmp_path, peer, server=("-m", "onedata_mcp.main", "--quiet"))
        command, args = server_argv(config)
        assert command == sys.executable
        assert args == [
            "-I",
            "-B",
            str(LAUNCHER),
            str(tmp_path / "map.json"),
            "--",
            "-m",
            "onedata_mcp.main",
            "--quiet",
        ]
        env = server_environment(config, config.condition("read"), TOKEN, ca_copy=peer.ca)
        assert not {"PYTHONPATH", "ONEDATA_MCP_NODE_NAMES"} & set(env)
        assert not [k for k, v in env.items() if str(tmp_path / "map.json") in v or "EXPCORE_ADDRESS" in k]
        assert env["ONEDATA_ONEZONE_HOST"] == peer.url
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = load_platform(write_platform(plain_dir))
    assert server_argv(plain) == (plain.toolserver.command, list(plain.toolserver.args))  # a Compose one: as declared
    plain_env = server_environment(plain, plain.condition("read"), TOKEN)
    assert not {"PYTHONPATH", "ONEDATA_MCP_NODE_NAMES"} & set(plain_env)


def test_o73_the_module_form_runs_the_server_with_its_arguments():
    """O73: `-m <module> args` runs the module as `__main__` with `sys.argv` as python would give it, through the
    launcher, under `-I -B`."""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        map_file = _map(Path(directory) / "map.json", {ZONE: "192.0.2.10"})
        done = subprocess.run(
            [sys.executable, *launch_argv(sys.executable, ["-m", "calendar", "2026", "11"], LAUNCHER, map_file)[1:]],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert done.returncode == 0, done.stderr
    assert "November 2026" in done.stdout


@pytest.mark.parametrize(
    ("command", "args", "why"),
    [
        ("/usr/bin/uv", ["run", "onedata-mcp"], "Python interpreter"),
        (sys.executable, [], "start with -m"),
        (sys.executable, ["-X", "dev", "-m", "m"], "start with -m"),
        (sys.executable, ["-m"], "needs a module"),
        (sys.executable, ["-m", "-x"], "needs a module"),
    ],
)
def test_o73_a_server_the_launcher_cannot_start_is_refused_by_the_platform_naming_the_key(tmp_path, command, args, why):
    """O73: the launcher runs a Python server by `-m <module>` or a script, so a `[toolserver]` that is another program,
    has no arguments, another interpreter option or a bare `-m`, is refused when the platform is loaded, naming the
    table's key, never the map or its names."""
    _map(tmp_path / "map.json", {ZONE: "127.0.0.1"})

    def edit(data):
        data["addresses"] = {"file": "map.json", "resolver": str(LAUNCHER)}
        data["toolserver"].update(command=command, args=args)

    with pytest.raises(ConfigError, match=why) as refused:
        load_platform(write_platform(tmp_path, edit))
    assert "[toolserver]" in str(refused.value)
    assert "map.json" not in str(refused.value).split("[toolserver]", 1)[1]
    with pytest.raises(AddressMapError, match=why):
        launch_argv(command, args, LAUNCHER, tmp_path / "map.json")


@pytest.mark.parametrize("name", ["absent.py", "resolve_launch.py", "other.py"])
def test_o73_a_resolver_that_is_not_the_launcher_as_a_regular_file_is_refused_naming_the_key(tmp_path, name):
    """O73: `[addresses] resolver` is `resolve_launch.py` as a regular file, or the platform is refused naming the key
    and never the path."""
    _map(tmp_path / "map.json", {ZONE: "127.0.0.1"})
    if name == "other.py":
        (tmp_path / name).write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="resolver") as refused:
        load_platform(write_platform(tmp_path, lambda d: d.update(addresses={"file": "map.json", "resolver": name})))
    assert str(tmp_path) not in str(refused.value).split(": ", 1)[-1]


# --- the launcher itself: exactly the map's names, nothing else, and no start with a map it may not use ---


def _launch(map_path, server, *, tmp_path):
    """The launcher run as the platform runs it (`-I -B`), the server's output captured."""
    return subprocess.run(
        [sys.executable, "-I", "-B", str(LAUNCHER), str(map_path), "--", *server],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )


PROBE = """
import socket, sys
calls = []
real = socket.getaddrinfo
for host in sys.argv[1:]:
    try:
        calls.append((host, socket.getaddrinfo(host, 8443, 0, socket.SOCK_STREAM)[0][4][0]))
    except OSError as error:
        calls.append((host, "unresolved"))
print(calls)
"""


def test_o73_the_launcher_resolves_exactly_the_names_of_the_map_and_no_other_host(tmp_path):
    """O73: a declared name (in any case, an IPv6 address included) resolves to its mapped address; an address literal
    resolves as it did, and a name the map does not hold is not resolved by the launcher (unresolved here: `.test`)."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    path = _map(tmp_path / "map.json", {ZONE: "192.0.2.10", "beta.layout.test": "2001:db8::2"})
    done = _launch(
        path,
        [str(probe), ZONE, "ZONE.layout.test", "beta.layout.test", "127.0.0.1", "gamma.layout.test"],
        tmp_path=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen[ZONE] == "192.0.2.10"
    assert seen["ZONE.layout.test"] == "192.0.2.10"
    assert seen["beta.layout.test"] == "2001:db8::2"
    assert seen["127.0.0.1"] == "127.0.0.1"
    assert seen["gamma.layout.test"] == "unresolved"


@pytest.mark.parametrize("why", ["mode", "link", "absent", "garbled", "format", "empty"])
def test_o73_a_map_the_launcher_may_not_use_stops_the_start_naming_nothing(tmp_path, why):
    """O73: a map readable by others, a link, a missing or garbled file, another format or no host stops the start with
    exit 78 and a message that names no path, name or address; the server never runs with a name unresolved by
    accident, and nothing is written to its stdout."""
    good = _map(tmp_path / "good.json", {ZONE: "192.0.2.10"})
    other = tmp_path / "f.json"
    (tmp_path / "g.json").write_text("{not json", encoding="utf-8")
    other.write_text(json.dumps({"format": "other/1", "hosts": {ZONE: {"address": "192.0.2.10"}}}), encoding="utf-8")
    empty = tmp_path / "e.json"
    empty.write_text(json.dumps({"format": FORMAT, "hosts": {}}), encoding="utf-8")
    link = tmp_path / "l.json"
    link.symlink_to(good)
    paths = {
        "mode": _map(tmp_path / "m.json", {ZONE: "192.0.2.10"}, 0o640),
        "link": link,
        "absent": tmp_path / "absent.json",
        "garbled": tmp_path / "g.json",
        "format": other,
        "empty": empty,
    }
    for path in (other, empty, tmp_path / "g.json"):
        path.chmod(0o600)
    ran = tmp_path / "ran"
    server = tmp_path / "server.py"
    server.write_text(f"open({str(ran)!r}, 'w').close()\n", encoding="utf-8")
    done = _launch(paths[why], [str(server)], tmp_path=tmp_path)
    assert done.returncode == 78
    assert done.stdout == ""
    assert not ran.exists()
    assert not re.search(r"layout\.test|192\.0\.2|\.json|/tmp|" + re.escape(str(tmp_path)), done.stderr)


@pytest.mark.parametrize("server", [[], ["-X", "dev"], ["-m"], ["-m", ""], ["-c", "print(1)"]])
def test_o73_a_server_the_launcher_cannot_run_is_refused(tmp_path, server):
    """O73: the launcher runs `-m <module>` or a script and nothing else of an interpreter's command line."""
    done = _launch(_map(tmp_path / "map.json", {ZONE: "192.0.2.10"}), server, tmp_path=tmp_path)
    assert done.returncode == 78
    assert done.stdout == ""


def test_o73_the_launcher_imports_nothing_of_the_core_and_prints_nothing_of_its_own():
    """O73: it runs inside the server's own interpreter, which does not hold the core: standard library only, and no
    print (the server's stdout is its protocol)."""
    import ast

    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"json", "os", "runpy", "socket", "stat", "sys"}
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "print"]
    assert socket  # the host's own `socket` is what the child imports: nothing of this module's
