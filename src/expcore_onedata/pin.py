# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/knowledge/pin.py, blob 5369fdcff1f7e4c53a3e175e58807e9190d1eea3
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The pinned knowledge-plane server (#6): which commit, which tools, how it is launched.

Everything here is read from `protocol/knowledge/pin.json` (frozen); nothing is tuned per option.
The server is the published groundnuty/onedata-mcp, unmodified. Tools are removed by the server's own
launch-time setting `ONEDATA_MCP_TOOLS` (an allow-list of tool names), never by patching its code.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO / "protocol" / "knowledge"
PIN_FILE = KNOWLEDGE_DIR / "pin.json"
TOOLS_FILE = KNOWLEDGE_DIR / "tools.json"
TOOLS_ENV = "ONEDATA_MCP_TOOLS"


def load_pin() -> dict:
    return json.loads(PIN_FILE.read_text())


def server_dir() -> Path:
    """Where the read-only clone of the server lives: $ONEDATA_MCP_DIR, else <work>/ref/onedata-mcp
    found by walking up from this repo (the worktrees sit under <work>/wt/)."""
    env = os.environ.get("ONEDATA_MCP_DIR")
    if env:
        return Path(env)
    for parent in REPO.parents:
        cand = parent / "ref" / "onedata-mcp"
        if cand.is_dir():
            return cand
    raise FileNotFoundError("onedata-mcp clone not found: set ONEDATA_MCP_DIR (see protocol/knowledge/README.md)")


def server_command(root: Path | None = None) -> str:
    return str((root or server_dir()) / ".venv" / "bin" / "onedata-mcp")


def server_python(root: Path | None = None) -> str:
    return str((root or server_dir()) / ".venv" / "bin" / "python")


LAUNCHER = Path(__file__).with_name("serve.py")
UA_ENV = "STUDY_VAR_62"


def user_agent() -> str:
    """The plane's fixed User-Agent: dataset-159/<first 7 of the pinned commit>."""
    return f"dataset-159/{load_pin()['server']['commit'][:7]}"


def launch_spec(root: Path | None = None) -> tuple[str, list[str]]:
    """(command, args) that start the server: the server venv's python running the launcher (serve.py), which then runs the
    server's own main. The same for every option."""
    return server_python(root), [str(LAUNCHER)]


def verify_pin(root: Path | None = None) -> str:
    """The clone is at the pinned commit and has no local change. Returns the commit; raises otherwise."""
    root = root or server_dir()
    want = load_pin()["server"]["commit"]

    def git(*a: str) -> str:
        return subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True).stdout.strip()

    head = git("rev-parse", "HEAD")
    if head != want:
        raise RuntimeError(f"onedata-mcp at {head}, pinned {want}")
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise RuntimeError(f"onedata-mcp clone has local changes: {dirty[:200]}")
    return head


def allowed_tools() -> list[str]:
    return list(load_pin()["tools"]["allowed"])


def server_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the server child: the allow-list (the same for every option) plus whatever the
    caller supplies (hosts and tokens). A caller-supplied allow-list is refused."""
    env = dict(base or {})
    if TOOLS_ENV in env and env[TOOLS_ENV] != ",".join(allowed_tools()):
        raise ValueError(f"{TOOLS_ENV} is fixed by protocol/knowledge/pin.json")
    env[TOOLS_ENV] = ",".join(allowed_tools())
    if UA_ENV in env and env[UA_ENV] != user_agent():
        raise ValueError(f"{UA_ENV} is fixed by the pin")
    env[UA_ENV] = user_agent()
    env.setdefault("FASTMCP_LOG_LEVEL", "WARNING")
    env.setdefault("PATH", os.environ.get("PATH", ""))
    return env
