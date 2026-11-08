# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/knowledge/pin.py, blob 5369fdcff1f7e4c53a3e175e58807e9190d1eea3
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The pinned tool-server checkout: which commit it runs from, and that it has no local change.

The server is a published program that runs unmodified. Tools are removed by the server's own launch-time allow-list
(toolserver.py), never by patching its code, so the only thing to pin is which code runs. `[toolserver] clone` and
`commit` in platform.toml name the checkout and the commit; when they are declared, a launch verifies them first and
refuses on any difference. Where the checkout is, how it was built and which commit it holds all come from the
configuration; nothing here searches the file system for a clone.
"""

import subprocess
from pathlib import Path

from expcore_onedata.config import ToolServer

GIT_TIMEOUT_S = 30.0


class PinError(RuntimeError):
    """The tool server's checkout is not the pinned one."""


def _git(clone: Path, *args: str) -> str:
    """Run git in the checkout and return its stdout; raise PinError, without the output, when it fails."""
    try:
        done = subprocess.run(
            ["git", "-C", str(clone), *args], check=True, capture_output=True, text=True, timeout=GIT_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PinError(f"git {args[0]} failed in the tool-server checkout ({type(error).__name__})") from None
    return done.stdout.strip()


def verify_pin(clone: Path, commit: str) -> str:
    """Require the checkout to be at the pinned commit with no local change to tracked files; return the commit."""
    head = _git(clone, "rev-parse", "HEAD")
    if head != commit:
        raise PinError("the tool-server checkout is not at the pinned commit")
    if _git(clone, "status", "--porcelain", "--untracked-files=no"):
        raise PinError("the tool-server checkout has local changes")
    return head


def verify_configured(server: ToolServer) -> str | None:
    """Verify the checkout the configuration pins, if it pins one; return the commit, or None when it pins none."""
    if server.clone is None or server.commit is None:
        return None
    return verify_pin(server.clone, server.commit)
