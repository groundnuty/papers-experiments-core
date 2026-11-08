# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/route.py, blob 6fc01d6d6b93829a52d4730d35e150a31dc0b467
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The route to the data provider: one setting, one function (#34, science's rule of 2 Nov).

Traefik caps fr at 75.6% of direct, so every option reads fr **direct to op_worker** on a fixed NodePort. One setting,
`[network] provider_port` in config/harness.toml, decides it, and `apply_provider_route` is the one function that turns it into
(a) the gate's `upstream` entry for the provider (T, condition_a, D) and (b) oneclient's `-P <port>` (M, M+). Everything that
builds a DataConfig for a campaign run goes through `campaign_data_config`, which calls it, so a run cannot end up with the gate
on one path and oneclient on the other.

Why harness.toml and not `/etc/dataset-80/host_aliases.toml`: host_aliases are facts of one host (which LAN address a name has);
the port is a campaign-wide choice that every record and the config hash must show, and it must be the same on every host.
The key is required: a missing key raises, it never falls back to 443.

Fault calibration (#11) stays on Traefik (port 443), because its faults are injected against Traefik's behaviour: it asks for
`via="traefik"` explicitly.

The route a run really used is read back from what was passed (`observed_route`), not from the setting.
"""
from __future__ import annotations

import http.client
import os
import ssl
import tomllib
from pathlib import Path

TRAEFIK_PORT = 443
PATH_TRAEFIK, PATH_DIRECT = "traefik", "direct"


def config_dir() -> Path:
    return Path(os.environ.get("STUDY_VAR_56") or Path(__file__).resolve().parents[2] / "config")


def validate_port(port) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"[network] provider_port must be an integer in 1..65535, got {port!r}")
    return port


def path_name(port: int) -> str:
    """`traefik` for 443 (the ingress), `direct` for any other port (the NodePort on op_worker)."""
    return PATH_TRAEFIK if port == TRAEFIK_PORT else PATH_DIRECT


def configured_provider_port() -> int:
    with open(config_dir() / "harness.toml", "rb") as fh:
        net = tomllib.load(fh).get("network", {})
    if "provider_port" not in net:
        raise ValueError("config/harness.toml [network] provider_port is not set (no default: 443 would silently be Traefik)")
    return validate_port(net["provider_port"])


def fault_calibration_via(option) -> str | None:
    """A fault-calibration run's route. T's and D's stay on Traefik, whose access log is a record they are scored on. M's goes by the campaign's route, so its
    cache and prefetch calibration measures what campaign M does (science's rule, the lead, 2 Nov)."""
    return None if str(getattr(option, "value", option)) == "M" else PATH_TRAEFIK


def route_name(via: str | None) -> str:
    """The name of the route `via` selects, for a run's record."""
    return path_name(TRAEFIK_PORT if via == PATH_TRAEFIK else configured_provider_port())


def apply_provider_route(kw: dict, *, via: str | None = None) -> dict:
    """`kw` (the keyword arguments of a DataConfig) with the provider route applied; the one place it is done.

    `via=None` takes the configured port; `via="traefik"` is the explicit 443 of T's and D's fault calibration. Sets
    `upstream[<provider host>]` for every provider host named (provider_hosts, oneclient_provider_host) unless the caller
    already gave one that points elsewhere (a test's mock; one for the same host on another port is refused), and prepends `-P <port>` to `oneclient_extra_args` when the port is not 443 (oneclient's
    own default). A caller's own -P/--port is refused: there is one setting."""
    if via not in (None, PATH_TRAEFIK):
        raise ValueError(f"via must be None (the configured route) or {PATH_TRAEFIK!r}, got {via!r}")
    port = TRAEFIK_PORT if via == PATH_TRAEFIK else configured_provider_port()
    extra = list(kw.get("oneclient_extra_args") or [])
    if any(a in ("-P", "--port") or a.startswith(("--port=", "-P")) for a in extra):
        raise ValueError("the provider port is set by [network] provider_port only; do not pass -P/--port in oneclient_extra_args")
    hosts = [h for h in [*(kw.get("provider_hosts") or []), kw.get("oneclient_provider_host") or ""] if h]
    if not hosts:       # discovery at start would give the providers the sandbox's own 443 fallback: a campaign config names its host
        raise ValueError("a campaign DataConfig must name its provider host(s) (provider_hosts / oneclient_provider_host)")
    out = dict(kw)
    up = dict(kw.get("upstream") or {})
    for h in dict.fromkeys(hosts):
        mine = up.setdefault(h, {"addr": h, "port": port, "tls": True})
        if mine.get("addr") == h and mine.get("port") != port:      # an omitted port is a default (443, or 80 without TLS): not the route
            raise ValueError(f"upstream override for {h} says port {mine.get('port')}, but the provider route is {port}: one route only")
    out["upstream"] = up
    out["oneclient_extra_args"] = (["-P", str(port)] if port != TRAEFIK_PORT and hosts else []) + extra
    return out


def observed_route(gate_hosts: dict | None, oneclient_args: list[str] | None) -> dict:
    """The provider route as the sandbox actually used it: the gate's provider entries (REST options) and/or oneclient's
    `-H`/`-P` (M, M+). `path` is `direct`, `traefik`, `mixed` (the two disagree) or `none`."""
    seen: list[tuple[str, int]] = []
    for h in (gate_hosts or {}).values():
        if h.get("role") == "oneprovider":
            seen.append((h["addr"], int(h.get("port", 443))))
    if oneclient_args:
        a = list(oneclient_args)
        host = a[a.index("-H") + 1] if "-H" in a else None
        port = int(a[a.index("-P") + 1]) if "-P" in a else TRAEFIK_PORT
        if host:
            seen.append((host, port))
    ports = sorted({p for _, p in seen})
    hosts = sorted({h for h, _ in seen})
    if not seen:
        return {"hosts": [], "port": None, "path": "none"}
    if len(ports) > 1:
        return {"hosts": hosts, "ports": ports, "port": None, "path": "mixed"}
    return {"hosts": hosts, "port": ports[0], "path": path_name(ports[0])}


def check_provider_port(host: str, port: int, *, timeout: float = 15.0, context: ssl.SSLContext | None = None) -> dict:
    """A real request to the provider's open configuration endpoint on `port`, certificate verified against the system roots
    (or `context`). Returns {"ok", "host", "port", "path", "why"?, "status"?}; a failure names its stage: `connect`, `tls` or `http`."""
    rep: dict = {"ok": False, "host": host, "port": port, "path": path_name(port)}
    ctx = context or ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    try:
        try:
            conn.connect()
        except ssl.SSLError as e:      # SSLCertVerificationError is a subclass: the certificate is the one thing this check must see
            rep.update(stage="tls", why=f"TLS did not verify on {host}:{port}: {e}"[:300])
            return rep
        except OSError as e:
            rep.update(stage="connect", why=f"{host}:{port} does not answer: {type(e).__name__}: {e}"[:300])
            return rep
        conn.request("GET", "/api/v3/oneprovider/configuration")
        resp = conn.getresponse()
        resp.read(65536)
        rep["status"] = resp.status
        if resp.status != 200:
            rep.update(stage="http", why=f"GET /api/v3/oneprovider/configuration on {host}:{port} answered {resp.status}")
            return rep
        rep["ok"] = True
        return rep
    except (OSError, http.client.HTTPException) as e:
        rep.update(stage="http", why=f"request to {host}:{port} failed: {type(e).__name__}: {e}"[:300])
        return rep
    finally:
        conn.close()


if __name__ == "__main__":      # python -m dataset_76.sandbox.route [host]: the same check as `baseline --preflight`
    import json
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "host-5.example.org"
    r = check_provider_port(host, configured_provider_port())
    print(json.dumps(r))
    raise SystemExit(0 if r["ok"] else 1)
