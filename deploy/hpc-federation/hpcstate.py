"""The HPC federation's state file, `hpc-federation.json` in the federation's root (DESIGN.md, section 4).

What `up` placed and launched, by the layout's service names: the held job, each service's node, step, work directory
and identity, and the site's values a later command needs. It holds no secret. It is written atomically (a draft in
the root, fsynced, renamed over the file, the directory fsynced) and before the action it enables, so a crash leaves
enough for `down`.
"""

import contextlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

STATE = "hpc-federation.json"
FORMAT = "expcore-hpc-federation/1"
PHASES = ("preflight", "submitted", "placed", "zone", "providers", "bootstrapped", "linked", "stopping", "down")
SERVICE_STATES = ("planned", "starting", "running", "stopping", "stopped", "failed", "removed")
WORKDIR_NAME = re.compile(r"hpcop-o[pz]\.[A-Za-z0-9]{6}")  # the bundle's mktemp -d name (lib.sh, make_workdir)
JOB_ID = re.compile(r"[0-9]{1,12}")
STEP_ID = re.compile(r"[0-9]{1,12}\.[0-9]{1,9}")


class StateError(RuntimeError):
    """A state file that is missing where one is needed, malformed, or of another federation."""


def path(root: Path) -> Path:
    return Path(root) / STATE


def checked_workdir(value: str, node_tmp: str, kind: str) -> str:
    """A service's work directory as its log names it, accepted only as the bundle makes one: directly under the
    node's tmpfs, `hpcop-<oz|op>.XXXXXX`, no other path. `down` removes what it names, so nothing else may stand
    here."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise StateError(f"a work directory of {kind} is not an absolute path")
    where = Path(value)
    if str(where.parent) != os.path.normpath(node_tmp) or not WORKDIR_NAME.fullmatch(where.name):
        raise StateError(f"a work directory of {kind} is not the bundle's: <node tmpfs>/hpcop-<oz|op>.XXXXXX")
    if not where.name.startswith(f"hpcop-{kind}."):
        raise StateError(f"a work directory of {kind} is another kind's")
    return value


def new_service(kind: str) -> Dict[str, Any]:
    """A role's record before up places it: a service (`oz`, `op`) with its public ports, or the agent's node
    (`sandbox`), which hosts no Onedata service and has none."""
    ports: Dict[str, Any] = {} if kind == "sandbox" else {"https": 8443, "epmd": None}
    if kind == "op":
        ports["rtransfer"] = 6665
    return {
        "kind": kind,
        "node": None,
        "fqdn": None,
        "address": None,
        "ports": ports,
        "workdir": None,
        "rootfs": None,
        "launch": 0,
        "step": None,
        "client": None,
        "log": None,
        "state": "planned",
    }


def load(root: Path) -> Optional[Dict[str, Any]]:
    """The state file, or None when there is none; StateError when it is not one of this format."""
    file = path(root)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        state = json.loads(text)
    except ValueError:
        raise StateError(f"{STATE} is not JSON") from None
    if not isinstance(state, dict) or state.get("format") != FORMAT:
        raise StateError(f"{STATE} is not of the format {FORMAT}")
    for key in ("project", "allocation", "services", "phase", "site"):
        if key not in state:
            raise StateError(f"{STATE} has no {key}")
    job = state["allocation"].get("job")
    if job is not None and not (isinstance(job, str) and JOB_ID.fullmatch(job)):
        raise StateError(f"{STATE}: allocation.job is not a Slurm job id")
    return state


def save(root: Path, state: Dict[str, Any]) -> None:
    """Write the state file atomically: a draft of this process's own in the root, fsynced, renamed over the file,
    then the root fsynced (best effort: the rename is done by then). Mode 0600: same uid, no secret, a private root."""
    file = path(root)
    draft = file.parent / f".{STATE}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, file)
    except BaseException:
        with contextlib.suppress(OSError):
            draft.unlink()
        raise
    with contextlib.suppress(OSError):
        directory = os.open(file.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
