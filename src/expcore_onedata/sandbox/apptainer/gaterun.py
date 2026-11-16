# Provenance: new
"""The data gate of a REST run on the node: its configuration and its token file (O56, O05, O23).

The gate is the core's own `gate.py` (with `gatepolicy.py` and `gateboundary.py`, which it loads from its directory),
run by the node's interpreter, isolated (`-I`), as a process of the driver's: it listens on 127.0.0.1 at a port that was
free (`listen_host`, `listen_port`), terminates TLS for the zone's and the local provider's names with a leaf of the
run's CA, and forwards to the federation's URLs, checking their certificate as the platform's `[tls]` says. It reads
the data-path token from a file in an owner-only directory of the run's that no cell binds, written here and removed by
the sandbox once the gate says it is ready, so the token is in the gate's memory alone (`token_held_by_gate`).

Hook (a): `[sandbox.apptainer.gate_options]` adds keys to the configuration (a peer-uid check, say); a key the sandbox
writes itself is refused when the settings load, and again here.
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
READY = "gate ready"
TOKEN_DIR = "gate-secret"
TOKEN_FILE = Path(TOKEN_DIR, "gate-token")


def hosts(zone: Endpoint, provider: Endpoint) -> dict[str, dict[str, Any]]:
    """The names the gate serves and dials: the zone's and the agent's pinned local provider's, as their URLs give
    them (O03, O05). On the node the cell resolves both to the loopback (`argv.hosts_text`)."""
    return {
        zone.host: {"role": "onezone", "addr": zone.host, "port": zone.port, "tls": zone.tls},
        provider.host: {"role": "oneprovider", "addr": provider.host, "port": provider.port, "tls": provider.tls},
    }


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
