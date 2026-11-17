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
PLACEMENTS = ("het", "separate")  # where the agent's node is held (hpcsettings.SANDBOX_PLACEMENTS)
SHA256 = re.compile(r"[0-9a-f]{64}")
RELAYS = ("serving", "none")  # the login node's fault relay as `up` ended: its heartbeat seen, or this core has none
UNRECORDED = "unrecorded"  # `relay_of`'s word for a state that says neither: an older up, or one that never got there


class StateError(RuntimeError):
    """A state file that is missing where one is needed, malformed, or of another federation."""


def path(root: Path) -> Path:
    return Path(root) / STATE


def relay_of(state: Dict[str, Any]) -> str:
    """The fault relay's state as `up` recorded it once the relay beat (`serving`) or found none in its core (`none`);
    `unrecorded` when the state has neither, as one written by an older up, or by an up that failed before the relay's
    heartbeat, has none. The `relay` record (the client's pid, kept so `down` can end it) is no answer: it is written at
    the relay's spawn, before its heartbeat."""
    found = state.get("fault_relay")
    return found if found in RELAYS else UNRECORDED


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
        "declared": None,  # the name the service registers under and is reached by (O73); fqdn is the node's own
        "address": None,
        "ports": ports,
        "workdir": None,
        "rootfs": None,
        "launch": 0,
        "launch_retries": [],  # each launch `up` repeated, with the proofs that nothing of it ran (hpclaunchproof)
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
    excluded = state["allocation"].get("excluded_nodes")  # a count (O57): none in a state an older up wrote
    if excluded is not None and not (isinstance(excluded, int) and not isinstance(excluded, bool) and excluded >= 0):
        raise StateError(f"{STATE}: allocation.excluded_nodes is not a count")
    if "fault_relay" in state and state["fault_relay"] not in RELAYS:
        raise StateError(f"{STATE}: fault_relay is not one of {', '.join(RELAYS)}")
    sandbox = state.get("sandbox")
    if sandbox is not None:
        checked_sandbox(sandbox)
    if "data" in state:
        checked_data(state["data"])
    checked_shard_nodes(state.get("shard_nodes", []))
    return state


def checked_data(data: Any) -> None:
    """The record of the spaces the study ingests into (`data.ingested`, by space name): the digest of the manifest each
    is verified against, whether the layout declares it, and `verified`, null until `hpcfed.py verify-data` has read
    the space back and found exactly that manifest, then the digest it was verified against and the counts.
    `shards.py plan` reads it, so a shape other than the driver's is a state that is not this one's."""
    where = f"{STATE}: data"
    if not isinstance(data, dict) or not isinstance(data.get("ingested"), dict):
        raise StateError(f"{where}.ingested is not a table of spaces")
    for record in data["ingested"].values():
        if not isinstance(record, dict) or not isinstance(record.get("declared"), bool):
            raise StateError(f"{where}.ingested: a space's record is not the driver's")
        if not (isinstance(record.get("manifest"), str) and SHA256.fullmatch(record["manifest"])):
            raise StateError(f"{where}.ingested: a space's manifest is not a SHA-256")
        verified = record.get("verified")
        if verified is None:
            continue
        sound = isinstance(verified, dict) and isinstance(verified.get("manifest"), str)
        if not (
            sound
            and SHA256.fullmatch(verified["manifest"])
            and all(
                isinstance(verified.get(key), int) and not isinstance(verified.get(key), bool) and verified[key] >= 0
                for key in ("files", "bytes")
            )
        ):
            raise StateError(f"{where}.ingested: a space's verified marker is not the driver's")


NODE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")  # as `hpcslurm.Slurm.hostnames` accepts one


def checked_shard_nodes(entries: Any) -> None:
    """The nodes a shard step of this federation was resumed on, outside its own jobs (`shards.py resume-line`
    records each, W1): down steps into the job each names, on its node, so each must be what Slurm gives."""
    if not isinstance(entries, list):
        raise StateError(f"{STATE}: shard_nodes is not a list")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"job", "node", "label", "name"}:
            raise StateError(f"{STATE}: a shard_nodes entry is not {{job, node, label, name}}")
        if not (isinstance(entry["job"], str) and JOB_ID.fullmatch(entry["job"])):
            raise StateError(f"{STATE}: a shard_nodes entry's job is not a Slurm job id")
        if not (isinstance(entry["node"], str) and NODE_NAME.fullmatch(entry["node"])):
            raise StateError(f"{STATE}: a shard_nodes entry's node is not a node name")
        if not all(isinstance(entry[key], str) and entry[key] for key in ("label", "name")):
            raise StateError(f"{STATE}: a shard_nodes entry has no label or job name")


def checked_sandbox(sandbox: Any) -> None:
    """The agent's node's record: down cancels the job it names and steps take its het group, so each must be what
    Slurm gives (DESIGN.md, section 4)."""
    if not isinstance(sandbox, dict) or sandbox.get("placement") not in PLACEMENTS:
        raise StateError(f"{STATE}: sandbox is not the record of the agent's node")
    for key in ("job", "component_job"):
        value = sandbox.get(key)
        if value is not None and not (isinstance(value, str) and JOB_ID.fullmatch(value)):
            raise StateError(f"{STATE}: sandbox.{key} is not a Slurm job id")
    if sandbox.get("het_group") != (1 if sandbox["placement"] == "het" else None):
        raise StateError(f"{STATE}: sandbox.het_group is not the agent's component")
    if not isinstance(sandbox.get("name"), str) or not sandbox["name"]:
        raise StateError(f"{STATE}: sandbox.name is not a job name")


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
