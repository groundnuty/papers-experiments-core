"""The fleet's record, `fleet.json` in the fleet's directory (`fleet.py`; DESIGN.md, section 1, "A fleet").

What the fleet's `up` claimed and made, each federation's identity as its own `up` recorded it, and each shard's
assignment and launches. It is written whole (a draft, fsynced, renamed over the file) under its lock, and before the
action it enables, so `down` finds whatever `up` reached and nothing more.

The record is the only list `down`, `step` and `status` act on: no root is ever found by a search. A federation's
root is `<fleet>/<label>/root`, its slot `<fleet>/<label>` made by an exclusive mkdir that never adopts what is there,
and the record accepts no other form, so an edited record cannot point the fleet at a root it did not make.
"""

import contextlib
import fcntl
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

import hpcstate

RECORD = "fleet.json"
FORMAT = "expcore-hpc-fleet/1"
LOCK = ".fleet.lock"  # held around each change of the record
LIFECYCLE = ".fleet.lifecycle.lock"  # held for a whole up, add, plan, step or down, never waited for
MEMBER = re.compile(r"fed-[1-9][0-9]{0,3}")  # a federation's label: derived from its place, never a site value
SHARD = re.compile(r"shard-[1-9][0-9]{0,3}")  # a shard's label: its index in the campaign
# A member's status: its slot recorded and not yet made; its slot found taken (never adopted); made, up not yet run;
# up and linked; its up failed (it may hold a job until its down); down, verified; its down failed or was refused.
STATUSES = ("claimed", "taken", "made", "up", "failed", "down", "down-failed")
HOLDS_NOTHING = ("claimed", "taken", "down")  # no job of the fleet's can be held by a member in one of these
LINKED = "linked"  # a federation's phase once its up has finished (hpcstate.PHASES)


class FleetError(RuntimeError):
    """A fleet directory or record that cannot be used, or a command refused; the message names what."""


def slot_of(directory: Path, label: str) -> Path:
    return Path(directory) / label


def root_of(directory: Path, label: str) -> Path:
    return slot_of(directory, label) / "root"


def new_record(layout: Path, digest: str, count: int, maximum: int, parallel: int) -> Dict[str, Any]:
    """A fleet of `count` shards and no federation yet."""
    return {
        "format": FORMAT,
        "layout": {"path": str(layout), "digest": digest},
        "count": count,
        "maximum": maximum,
        "parallel": parallel,
        "members": [],
        "shards": [new_shard(n) for n in range(1, count + 1)],
        "reference": None,  # the platform fingerprint every assigned federation has (its first plan's)
        "template": None,  # the shard manifest's settings, as the first plan read them
    }


def new_shard(index: int) -> Dict[str, Any]:
    """Shard `index` of the campaign: its federation now, the one it last ran on, and each launch of its step."""
    return {"label": f"shard-{index}", "member": None, "last": None, "moved": False, "launches": []}


def new_member(directory: Path, label: str) -> Dict[str, Any]:
    return {
        "label": label,
        "root": str(root_of(directory, label)),
        "status": "claimed",
        "made": None,  # (st_dev, st_ino) of the root this fleet made, once made
        "name": None,  # the held job's name, with up's random part: the federation's identity
        "jobs": {},  # the ids its up's state recorded: "allocation", and "sandbox" (the agent's own held job)
        "phase": None,
        "shard": None,  # the shard it holds now
        "held": [],  # every shard it was ever given: a federation runs one shard in its life
        "said": None,  # the last error of its up or down, one line
    }


def check(record: Any, directory: Path) -> Dict[str, Any]:
    """The record, refused unless it is this format and every member's root is exactly the form up makes."""
    if not isinstance(record, dict) or record.get("format") != FORMAT:
        raise FleetError(f"{RECORD} is not of the format {FORMAT}")
    members, shards = record.get("members"), record.get("shards")
    if not isinstance(members, list) or not isinstance(shards, list):
        raise FleetError(f"{RECORD} has no members or no shards")
    labels = []
    for member in members:
        label = member.get("label") if isinstance(member, dict) else None
        if not isinstance(label, str) or not MEMBER.fullmatch(label):
            raise FleetError(f"{RECORD}: a member's label is not fed-<n>")
        if member.get("root") != str(root_of(directory, label)) or member.get("status") not in STATUSES:
            raise FleetError(f"{RECORD}: member {label} is not a root this fleet makes (<fleet>/{label}/root)")
        jobs = member.get("jobs")
        if not isinstance(jobs, dict) or not all(k in ("allocation", "sandbox") and (v is None or (
                isinstance(v, str) and hpcstate.JOB_ID.fullmatch(v))) for k, v in jobs.items()):  # fmt: skip
            raise FleetError(f"{RECORD}: member {label}'s jobs are not Slurm job ids")
        made = member.get("made")
        if made is not None and not (isinstance(made, list) and len(made) == 2 and all(isinstance(v, int)
                                                                                         for v in made)):  # fmt: skip
            raise FleetError(f"{RECORD}: member {label}'s made is not a device and an inode")
        labels.append(label)
    if len(set(labels)) != len(labels):
        raise FleetError(f"{RECORD}: a member's label is given twice")
    for shard in shards:
        if not isinstance(shard, dict) or not SHARD.fullmatch(str(shard.get("label"))):
            raise FleetError(f"{RECORD}: a shard's label is not shard-<n>")
        if shard.get("member") is not None and shard["member"] not in labels:
            raise FleetError(f"{RECORD}: shard {shard['label']} is given to no member of this fleet")
    if record.get("count") != len(shards):
        raise FleetError(f"{RECORD}: the count of shards is not the shards'")
    return record


def member_of(record: Dict[str, Any], label: str) -> Dict[str, Any]:
    for member in record["members"]:
        if member["label"] == label:
            return member
    raise FleetError(f"no member {label} in this fleet")


def shard_of(record: Dict[str, Any], label: str) -> Dict[str, Any]:
    for shard in record["shards"]:
        if shard["label"] == label:
            return shard
    raise FleetError(f"no shard {label} in this fleet")


class Record:
    """The fleet's record, read whole and changed only under its lock."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    @property
    def path(self) -> Path:
        return self.directory / RECORD

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> Dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise FleetError(f"{self.directory} holds no {RECORD}: no fleet was brought up there") from None
        try:
            return check(json.loads(text), self.directory)
        except ValueError:
            raise FleetError(f"{RECORD} is not JSON") from None

    def save(self, record: Dict[str, Any]) -> None:
        """Written whole: a draft of this process's own, fsynced, renamed over the file, then the directory fsynced."""
        check(record, self.directory)
        draft = self.directory / f".{RECORD}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(draft, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                draft.unlink()
            raise
        with contextlib.suppress(OSError):
            folder = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)

    @contextlib.contextmanager
    def _flock(self, name: str, blocking: bool, what: str = "") -> Iterator[None]:
        descriptor = os.open(self.directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise FleetError(f"another command of this fleet is running; {what} refused") from None
            yield
        finally:
            os.close(descriptor)

    def update(self, change: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
        """Change the record under its lock; `change` edits the dict in place."""
        with self._flock(LOCK, True):
            record = self.read()
            change(record)
            self.save(record)
            return record

    def lifecycle(self, what: str):
        """Hold the fleet's lifecycle lock for a whole command that acts on its federations, never waiting for it."""
        return self._flock(LIFECYCLE, False, what)


def jobs_of(state: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """The held jobs a state names: its allocation's, and the agent's own job's under `separate` (a `het` agent's node
    is the allocation's component, never a job of its own)."""
    sandbox = state.get("sandbox") or {}
    own = sandbox.get("job") if sandbox.get("placement") == "separate" else None
    return {"allocation": (state.get("allocation") or {}).get("job"), "sandbox": own}


def held_count(member: Mapping[str, Any], facts: Mapping[str, Any]) -> int:
    """How many held jobs a member may bill for: the distinct ids its state file and the fleet's record name, none for a
    member in a status that holds nothing (`HOLDS_NOTHING`) or whose state file says its down went as far as the jobs
    (phase `down`: `hpcdown` writes it only once the held jobs are cancelled and every client ended, and keeps the root
    for a second down). Both sources are read, so a record that kept an id its state lost still counts."""
    if member["status"] in HOLDS_NOTHING or facts.get("phase") == "down":
        return 0
    found = {job for jobs in (facts.get("jobs"), member.get("jobs")) for job in (jobs or {}).values() if job}
    return len(found)


def member_state(member: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    """One federation's state in plain words, from the fleet's record and what its root's state file says now (`facts`:
    its phase, its jobs and its fault relay as `fleet.state_facts` reads them; empty when the root has no readable
    state): up and ready, up with no relay, failed (or down-failed, or never up) and still HOLDING jobs, by count, with
    the command that releases them, or down. "Ready" needs the state file to show the federation linked and its relay
    serving; the label is the fleet's (`fed-k`), never a host."""
    label, status = member["label"], member["status"]
    holding = held_count(member, facts)
    held = f"still HOLDING {holding} job(s): it bills until `fleet.py down --member {label}`"
    if status == "up":
        if not facts:
            return "up, but its state file is missing or unreadable" + (f"; {held}" if holding else "")
        if facts.get("phase") != LINKED:
            return f"up, but not linked (phase {facts.get('phase')})" + (f"; {held}" if holding else "")
        relay = facts.get("relay")
        if relay == "serving":
            return "up and ready"
        return "up with no relay" + ("" if relay == "none" else " (its state file has no relay record)")
    word = {"failed": "failed", "down-failed": "down FAILED", "made": "made, its up never finished"}.get(status, status)
    if holding:
        return f"{word}, {held}"
    if status in HOLDS_NOTHING:
        return word
    if facts.get("phase") == "down":
        return f"{word}, its jobs are gone and its root is kept: `fleet.py down --member {label}` again finishes it"
    return f"{word}, no job on record"


def sandbox_name(name: str, placement: Optional[str]) -> str:
    """The agent's node's job name as up derives it from the allocation's (`hpcdriver.sandbox_record`)."""
    return name if placement == "het" else f"{name}-sandbox"


def identity(directory: Path, member: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Whether a member's root is the one this fleet made and holds the federation its up made: (why not, None), or
    (None, the held job's name its state file records, None when it has none). The root must be the directory this
    fleet's mkdir made (its device and inode), never a link; a state file in it must be that federation's: written by
    an up of this root (its command names it), the name up recorded (with up's random part), the agent's node's record
    named as up derives it from that name, and every job id the fleet recorded at its up the same."""
    label, root = member["label"], root_of(directory, member["label"])
    if member.get("made") is None:
        return f"{label}: its root was never made by this fleet", None
    if slot_of(directory, label).is_symlink() or root.is_symlink():
        return f"{label}: its root or slot is a link", None
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return None, None  # gone: nothing of it is left to touch
    if [info.st_dev, info.st_ino] != list(member["made"]):
        return f"{label}: its root is not the directory this fleet made", None
    try:
        state = hpcstate.load(root)
    except hpcstate.StateError as error:
        return f"{label}: {error}", None
    if state is None:
        return None, None
    name = (state.get("allocation") or {}).get("name")
    command = state.get("command") or []
    if not isinstance(name, str) or not name:
        return f"{label}: its state file names no held job", None
    if _root_in(command) != str(root):
        return f"{label}: its state file was not written by an up of this root", None
    if member.get("name") is not None and name != member["name"]:
        return f"{label}: its state file is of another federation than the one this fleet's up made", None
    sandbox = state.get("sandbox")
    if sandbox is not None and sandbox.get("name") != sandbox_name(name, sandbox.get("placement")):
        return f"{label}: its state file's agent's node is not the one its up held", None
    found, recorded = jobs_of(state), member.get("jobs") or {}
    if any(recorded.get(key) is not None and found.get(key) != recorded[key] for key in ("allocation", "sandbox")):
        return f"{label}: its state file names other jobs than the ones this fleet's up recorded", None
    return None, name


def _root_in(command: List[Any]) -> Optional[str]:
    words = [str(word) for word in command]
    return words[words.index("--root") + 1] if "--root" in words[:-1] else None
