# Provenance: new
"""The data gate of a REST run on the node: its configuration and its token file (O56, O05, O23).

The gate is the core's own `gate.py` (with `gatepolicy.py` and `gateboundary.py`, which it loads from its directory),
run by the node's interpreter, isolated (`-I`), as a process of the driver's: it listens on 127.0.0.1 at a port that was
free (`listen_host`, `listen_port`), terminates TLS for the zone's and the local provider's names with a leaf of the
run's CA, and forwards to the federation's URLs, checking their certificate as the platform's `[tls]` says. It reads
the data-path token from a file in an owner-only directory of the run's that no cell binds, written here and removed by
the sandbox once the gate says it is ready, so the token is in the gate's memory alone (`token_held_by_gate`).

Hook (a): `[sandbox.apptainer.gate_options]` adds keys to the configuration; a key the sandbox writes itself is refused
when the settings load, and again here. The gate's peer-uid check (`peer_uid_check`, #317) is on by default whenever the
gate's directory holds its `gatepeer.py`, so the loopback the node's other users share reaches the gate only from this
uid; `gate_options` may set it false.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.api import Endpoint, SandboxError
from expcore_onedata.sandbox.apptainer.argv import LOOPBACK
from expcore_onedata.sandbox.apptainer.settings import GATE_OWN_KEYS
from expcore_onedata.tls import TlsSettings

GATE_PY = Path(__file__).resolve().parent.parent / "gate.py"  # the core's gate, beside its policy and boundary
# the gate's peer-uid check (#317, O61), beside it once that lands: on by default on this backend whenever the gate has
# it, so a co-located user's connection to the loopback is refused; `gate_options` may still turn it off
PEER_CHECK, PEER_CHECK_KEY = GATE_PY.parent / "gatepeer.py", "peer_uid_check"
READY = "gate ready"
TOKEN_DIR = "gate-secret"
TOKEN_FILE = Path(TOKEN_DIR, "gate-token")


def hosts(zone: Endpoint, provider: Endpoint, names: tuple[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """The names the gate serves, each with the host and port it dials: the zone's and the agent's pinned local
    provider's (O03, O05). The cell resolves every name to the loopback (`argv.hosts_text`) and dials it on 443.

    `names` are `[sandbox.federation]`'s, the zone's and the provider's (O69): the gate then serves and certifies those
    and dials the URLs' hosts and ports behind them, so the agent sees the names the layout declares and never a
    node's name or port. Without them it serves the URLs' hosts themselves, as it always has. An endpoint with an
    address (O73) is dialled at it, the name it is served under unchanged."""
    zone_name, provider_name = names if names is not None else (zone.host, provider.host)
    if names is not None and zone_name.lower() == provider_name.lower():
        raise SandboxError("[sandbox.federation] serves the zone and the provider by one name: a name is one host's")
    return {
        zone_name: _upstream("onezone", zone),
        provider_name: _upstream("oneprovider", provider),
    }


def _upstream(role: str, endpoint: Endpoint) -> dict[str, Any]:
    """One served name's upstream: the URL's host (`addr`, the name its handshake asks for and its certificate is
    checked against), its port and TLS, and `dial`, the address to connect to, when the platform's `[addresses]` map
    holds the host (O73): the host is then a declared name that resolves nowhere. Without a map entry there is no `dial`
    key, and the gate dials `addr`, as it always has."""
    out: dict[str, Any] = {"role": role, "addr": endpoint.host, "port": endpoint.port, "tls": endpoint.tls}
    if endpoint.address is not None:
        out["dial"] = endpoint.address
    return out


def config(
    *,
    run_key: str,
    tls: TlsSettings,
    served: Mapping[str, Mapping[str, Any]],
    listing: str,
    directory: Path,
    tls_dir: Path,
    token_file: Path,
    port: int,
    upstream_ca: Path | None,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """The gate's configuration: no secret (the token is in its file), the hook's options added to the sandbox's."""
    clash = sorted(set(options) & GATE_OWN_KEYS)
    if clash:
        raise SandboxError(f"the gate's options may not set {clash[0]!r}: the sandbox writes it itself")
    out: dict[str, Any] = dict(options)
    if PEER_CHECK.is_file():  # never set for a gate that would not read it: a check claimed is a check made
        out.setdefault(PEER_CHECK_KEY, True)
    elif out.get(PEER_CHECK_KEY) is True:  # asked of a gate that would ignore it: refused, never claimed
        raise SandboxError("gate_options asks for the gate's peer-uid check, and this core's gate does not have it")
    out.update(
        run_key=run_key,
        verify_ssl=not tls.insecure,
        log=str(directory / "requests.jsonl"),
        tls_dir=str(tls_dir),
        hosts={name: dict(value) for name, value in served.items()},
        listing=listing,
        token_file=str(token_file),
        listen_host=LOOPBACK,
        listen_port=port,
    )
    if upstream_ca is not None:
        out["upstream_ca_file"] = str(upstream_ca)
    return out


def write_config(path: Path, document: Mapping[str, Any]) -> None:
    """The configuration, owner-only (it holds no secret, but the run's directory is the driver's alone)."""
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(document), sort_keys=True))


def write_token(path: Path, token: str) -> None:
    """The token's file: a new owner-only file (0400 once written) in an owner-only directory of the run's (made here,
    or the one a gate tried before this one left), never through a link."""
    path.parent.mkdir(mode=0o700, exist_ok=True)
    info = os.lstat(path.parent)
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise SandboxError("the directory of the run's token file is not an owner-only directory of the driver's")
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(token)
    path.chmod(0o400)


def argv(python: str, config_path: Path) -> list[str]:
    """The gate's command line: the node's interpreter, isolated, the core's gate and its configuration's path."""
    return [python, "-I", str(GATE_PY), str(config_path)]


__all__ = ["GATE_PY", "READY", "TOKEN_FILE", "argv", "config", "hosts", "write_config", "write_token"]
