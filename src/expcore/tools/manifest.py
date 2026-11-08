# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/knowledge/manifest.py, blob aef43dbc07fcac6282fa0b5fc8cb40a9c5ef7f0e
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Tool manifest of the pinned server: read it from a running server, freeze it, compare it (#6).

`live_manifest()` starts the server exactly as the harness does (pin.server_env), asks it for its tools and returns a
canonical, hashable document: name, description, input schema, output schema, annotations. The frozen copy is
`protocol/knowledge/tools.json`. Equal manifests mean equal names, descriptions and schemas, byte for byte.

    uv run python -m dataset_76.knowledge.manifest freeze     # (re)write protocol/knowledge/tools.json
    uv run python -m dataset_76.knowledge.manifest check      # exit 1 if a live server differs from the frozen file
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import pin

PLACEHOLDER_HOST = "https://manifest.invalid"  # listing tools needs no federation call


def _g(o: Any, snake: str, camel: str) -> Any:
    v = getattr(o, snake, None)
    return v if v is not None else getattr(o, camel, None)


def canonical(tools: list[Any]) -> list[dict]:
    out = []
    for t in sorted(tools, key=lambda t: t.name):
        ann = getattr(t, "annotations", None)
        out.append(
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": _g(t, "input_schema", "inputSchema") or {},
                "output_schema": _g(t, "output_schema", "outputSchema"),
                "annotations": ann.model_dump(mode="json", exclude_none=True) if ann is not None else None,
            }
        )
    return out


def digest(manifest: list[dict]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def live_manifest(
    command: str | None = None, env: dict[str, str] | None = None, *, all_tools: bool = False
) -> dict:
    """Start the server and read its tool list. `all_tools=True` starts it WITHOUT the allow-list (used once, to
    record which tools exist and which were disabled); never used for a run."""
    base = dict(env or {})
    base.setdefault("ONEDATA_ONEZONE_HOST", PLACEHOLDER_HOST)
    base.setdefault("ONEDATA_ONEPROVIDER_HOST", PLACEHOLDER_HOST)
    if all_tools:
        e = {
            "PATH": base.get("PATH", ""),
            "FASTMCP_LOG_LEVEL": "WARNING",
            pin.UA_ENV: pin.user_agent(),
            **{k: v for k, v in base.items() if k != pin.TOOLS_ENV},
        }
    else:
        e = pin.server_env(base)
    cmd, args = (command, []) if command else pin.launch_spec()
    params = StdioServerParameters(command=cmd, args=args, env=e)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        tools = (await s.list_tools()).tools
    m = canonical(tools)
    info = _g(init, "server_info", "serverInfo")
    return {
        "server_name": info.name,
        "server_version": info.version,
        "protocol_version": _g(init, "protocol_version", "protocolVersion"),
        "tools": m,
        "sha256": digest(m),
    }


def frozen() -> dict:
    return json.loads(pin.TOOLS_FILE.read_text())


def diff(a: list[dict], b: list[dict]) -> list[str]:
    """Human-readable differences between two manifests (empty = identical)."""
    am, bm = {t["name"]: t for t in a}, {t["name"]: t for t in b}
    out = [f"only in A: {n}" for n in sorted(am.keys() - bm.keys())] + [
        f"only in B: {n}" for n in sorted(bm.keys() - am.keys())
    ]
    for n in sorted(am.keys() & bm.keys()):
        for k in am[n]:
            if am[n][k] != bm[n][k]:
                out.append(f"{n}.{k} differs")
    return out


def _freeze() -> int:
    p = pin.load_pin()
    pin.verify_pin()
    everything = asyncio.run(live_manifest(all_tools=True))
    exposed = asyncio.run(live_manifest())
    names_all = {t["name"] for t in everything["tools"]}
    classified = set(p["tools"]["allowed"]) | set(p["tools"]["disabled"])
    if names_all != classified:
        print(
            f"pin.json does not classify every server tool: unclassified={sorted(names_all - classified)} "
            f"unknown={sorted(classified - names_all)}",
            file=sys.stderr,
        )
        return 1
    if {t["name"] for t in exposed["tools"]} != set(p["tools"]["allowed"]):
        print("the server does not expose exactly the allowed tools", file=sys.stderr)
        return 1
    doc = {
        "server": p["server"]["repo"],
        "commit": p["server"]["commit"],
        "server_version": exposed["server_version"],
        "protocol_version": exposed["protocol_version"],
        "env": {pin.TOOLS_ENV: ",".join(p["tools"]["allowed"])},
        "sha256": exposed["sha256"],
        "tools": exposed["tools"],
    }
    pin.TOOLS_FILE.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"froze {len(doc['tools'])} tools, sha256 {doc['sha256']}")
    return 0


def _check() -> int:
    pin.verify_pin()
    live = asyncio.run(live_manifest())
    d = diff(frozen()["tools"], live["tools"])
    if d or live["sha256"] != frozen()["sha256"]:
        print("MISMATCH:", *d, sep="\n  ", file=sys.stderr)
        return 1
    print(f"live manifest equals the frozen one ({live['sha256']})")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit({"freeze": _freeze, "check": _check}.get(cmd, lambda: print(__doc__) or 2)())
