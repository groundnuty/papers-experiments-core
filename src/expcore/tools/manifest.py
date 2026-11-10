# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/knowledge/manifest.py, blob aef43dbc07fcac6282fa0b5fc8cb40a9c5ef7f0e
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The tool manifest of a server: read it from a running server, freeze it, compare it (L13).

`live_manifest()` starts the server exactly as the caller's launcher does, asks it for its tools and returns a
canonical, hashable document: name, description, input schema, output schema, annotations. The frozen copy is a JSON
file kept per condition. Equal manifests mean equal names, descriptions and schemas, byte for byte.

The server enforces its allow-list; the client only checks. Any tool the server shows beyond the frozen manifest, any
tool of the manifest it lacks, and any allow-list name it lacks fails the start. There is no client-side filter.

    python -m expcore.tools.manifest freeze OUT [--pass-env NAME ...] -- COMMAND [ARG ...]
    python -m expcore.tools.manifest check FROZEN [--pass-env NAME ...] -- COMMAND [ARG ...]
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import PaginatedRequestParams

PAGE_LIMIT = 100  # a server that keeps returning cursors is broken


class ManifestError(Exception):
    """A manifest file is unreadable, or a server's tools do not match the frozen manifest. `problems` lists why."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def canonical(tools: Sequence[Any]) -> list[dict]:
    """Return the tools in a canonical form: sorted by name, with every field a manifest compares."""
    out = []
    for t in sorted(tools, key=lambda t: t.name):
        ann = getattr(t, "annotations", None)
        out.append(
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema or {},
                "output_schema": t.output_schema,
                "annotations": ann.model_dump(mode="json", exclude_none=True) if ann is not None else None,
            }
        )
    return out


def digest(manifest: list[dict]) -> str:
    """Return the SHA-256 of the canonical JSON of a manifest."""
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def diff(a: list[dict], b: list[dict]) -> list[str]:
    """Human-readable differences between two manifests (empty = identical)."""
    am, bm = {t["name"]: t for t in a}, {t["name"]: t for t in b}
    out = [f"only in A: {n}" for n in sorted(am.keys() - bm.keys())] + [
        f"only in B: {n}" for n in sorted(bm.keys() - am.keys())
    ]
    for n in sorted(am.keys() & bm.keys()):
        for k in sorted(am[n].keys() | bm[n].keys()):
            if am[n].get(k) != bm[n].get(k):
                out.append(f"{n}.{k} differs")
    return out


@dataclass(frozen=True)
class FrozenManifest:
    """The tools a condition's server must show, with the server's own name and versions for the record."""

    tools: list[dict]
    sha256: str
    server_name: str | None = None
    server_version: str | None = None
    protocol_version: str | None = None

    @classmethod
    def from_live(cls, live: dict) -> "FrozenManifest":
        """Freeze what `live_manifest` read."""
        return cls(live["tools"], live["sha256"], live["server_name"], live["server_version"], live["protocol_version"])

    @classmethod
    def load(cls, path: Path) -> "FrozenManifest":
        """Read a frozen manifest; refuse a file that is not one, or whose stored hash is not its tools' hash."""
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ManifestError([f"{path}: cannot read the frozen manifest: {exc}"]) from exc
        tools = doc.get("tools") if isinstance(doc, dict) else None
        if not isinstance(tools, list) or not all(isinstance(t, dict) and "name" in t for t in tools):
            raise ManifestError([f"{path}: a frozen manifest is a JSON object with a 'tools' list of named tools"])
        if doc.get("sha256") != digest(tools):
            raise ManifestError([f"{path}: the stored sha256 is not the hash of the tools; the file was edited"])
        return cls(
            tools,
            doc["sha256"],
            doc.get("server_name"),
            doc.get("server_version"),
            doc.get("protocol_version"),
        )

    def dump(self) -> str:
        """Return the file text: sorted keys, one trailing newline."""
        doc = {
            "server_name": self.server_name,
            "server_version": self.server_version,
            "protocol_version": self.protocol_version,
            "sha256": self.sha256,
            "tools": self.tools,
        }
        return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def mismatches(live: list[dict], frozen: FrozenManifest, allowed: Iterable[str] = ()) -> list[str]:
    """Return every way the server's tools differ from the frozen manifest and the allow-list; empty when they fit.

    An extra or a missing tool, a changed description or schema, an allow-list name the server lacks, and a tool
    outside the allow-list that the server shows all count (L13).
    """
    names = {t["name"] for t in live}
    problems = []
    for line in diff(frozen.tools, live):
        problems.append(
            line.replace("only in A: ", "missing from the server: ").replace("only in B: ", "extra on the server: ")
        )
    wanted = set(allowed)
    if wanted:
        if lacking := sorted(wanted - names):
            problems.append(f"the allow-list names tools the server lacks: {lacking}")
        if outside := sorted(names - wanted):
            problems.append(f"the server shows tools outside the allow-list: {outside}")
    return problems


async def list_all_tools(session: ClientSession) -> list[Any]:
    """Read every page of the server's tool list."""
    tools: list[Any] = []
    cursor = None
    for _ in range(PAGE_LIMIT):
        params = PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools
    raise ManifestError([f"the server returned more than {PAGE_LIMIT} pages of tools"])


async def live_manifest(params: StdioServerParameters, *, timeout_s: float = 120.0) -> dict:
    """Start the server as the caller's launcher does and read its tool list, within `timeout_s`."""
    async with asyncio.timeout(timeout_s), stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        tools = await list_all_tools(s)
    m = canonical(tools)
    return {
        "server_name": init.server_info.name,
        "server_version": init.server_info.version,
        "protocol_version": init.protocol_version,
        "tools": m,
        "sha256": digest(m),
    }


def _params(command: Sequence[str], pass_env: Sequence[str]) -> StdioServerParameters:
    """Build the launch parameters; environment variables are copied by name so no value is on the command line."""
    env = {name: os.environ[name] for name in pass_env if name in os.environ}
    return StdioServerParameters(command=command[0], args=list(command[1:]), env=env or None)


def _freeze(out: Path, params: StdioServerParameters, timeout_s: float) -> int:
    """Write the frozen manifest of the server the parameters launch."""
    frozen = FrozenManifest.from_live(asyncio.run(live_manifest(params, timeout_s=timeout_s)))
    out.write_text(frozen.dump(), encoding="utf-8")
    print(f"froze {len(frozen.tools)} tools, sha256 {frozen.sha256}")
    return 0


def _check(path: Path, params: StdioServerParameters, timeout_s: float) -> int:
    """Compare a live server with a frozen manifest; exit 1 and list the differences when they are not equal."""
    frozen = FrozenManifest.load(path)
    live = asyncio.run(live_manifest(params, timeout_s=timeout_s))
    problems = mismatches(live["tools"], frozen)
    if problems:
        print("MISMATCH:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"live manifest equals the frozen one ({live['sha256']})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return 0 on success, 1 on a mismatch, 2 on an error.

    Everything after the first `--` is the server's command, passed as it is; the options before it may come before or
    after the action and the file, as the usage shows.
    """
    words = list(sys.argv[1:] if argv is None else argv)
    ours, command = (words[: words.index("--")], words[words.index("--") + 1 :]) if "--" in words else (words, [])
    parser = argparse.ArgumentParser(
        prog="python -m expcore.tools.manifest",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="%(prog)s {freeze,check} FILE [--pass-env NAME ...] [--timeout S] -- COMMAND [ARG ...]",
    )
    parser.add_argument("action", choices=["freeze", "check"])
    parser.add_argument("file", type=Path)
    parser.add_argument(
        "--pass-env", action="append", default=[], metavar="NAME", help="copy this variable to the server"
    )
    parser.add_argument("--timeout", type=float, default=120.0, metavar="S", help="seconds to wait for the server")
    args = parser.parse_args(ours)
    if not command:
        print("error: give the server command after --", file=sys.stderr)
        return 2
    params = _params(command, args.pass_env)
    try:
        run = _freeze if args.action == "freeze" else _check
        return run(args.file, params, args.timeout)
    except (ManifestError, OSError, TimeoutError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
