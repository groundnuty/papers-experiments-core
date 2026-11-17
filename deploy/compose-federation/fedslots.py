"""The slots and the tool server a layout declares (O83), read once for both platforms.

A **slot** pairs two principals of the layout: the user whose token reads the run's data and the user whose token the
tool server gets. They are ordinary principals (`[principals.<name>]`), so everything that provisions principals,
on Compose and on HPC alike, provisions them: the zone user and its 0600 password file, the membership of each space
with exactly the declared privileges, and on HPC the storage mapping that lets the user reach the files. The slot only
says which two of them belong together, and the checks below are the ones the platform layer would make of the same
`[slots]` (`expcore_onedata.users.load_slots`, `tokens`), made when the layout is read.

```toml
[slots.<name>]                        # a lease key: letters, digits, '_' and '-'
data_path = "<principal>"             # exactly the baseline rights (space_view, space_read_data)
toolserver = "<principal>"            # the rights its conditions declare, which are its privileges

[toolserver]                          # optional, and only with slots
command = "@study_artifacts@/venv/bin/python"   # a Python interpreter (the launcher of O73 starts the server by it)
args = ["-m", "<module>"]             # -m <module>, then its arguments
clone = "@study_artifacts@/<clone>"   # the pinned checkout, and its commit
commit = "<forty hex digits>"
start_timeout_s = 120                 # optional

[toolserver.conditions.<name>]
allow = ["<tool>", ...]               # the server-side allow-list of the condition
rights = ["space_view", ...]          # the privileges a slot's tool-server user holds exactly
manifest = "@study_artifacts@/manifests/<file>.json"   # the condition's frozen manifest
```

Every path names the study's own artifacts directory, `@study_artifacts@`, so a layout (a public file) holds no path of
a host: each platform fills it from where its own site keeps the study's files (`fedtools`). Nothing is a secret.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore_onedata.addresses import AddressMapError, launch_argv
from expcore_onedata.rights import BASELINE_RIGHTS, rights_problem

STUDY_ARTIFACTS = "@study_artifacts@"  # the placeholder of the study's own directory (site.env on HPC)
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")  # the platform layer's own rule for a slot's and a condition's name
TOOL = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
COMMIT = re.compile(r"[0-9a-f]{40}")
# the placeholder, then plain names: no step up, no dotfile, no absolute path
ARTIFACT_PATH = re.compile(rf"{re.escape(STUDY_ARTIFACTS)}(?:/[A-Za-z0-9_][A-Za-z0-9_.-]{{0,63}}){{1,8}}")
SLOT_KEYS = {"data_path", "toolserver"}
TOOLSERVER_KEYS = {"command", "args", "clone", "commit", "start_timeout_s", "conditions"}
CONDITION_KEYS = {"allow", "rights", "manifest"}


@dataclass(frozen=True)
class Slot:
    """One lease key: the principals whose users a run leases for its data path and for its tool server."""

    name: str
    data_path: str
    toolserver: str


@dataclass(frozen=True)
class Condition:
    """A tool-server condition: its allow-list, the rights (sorted) a slot's tool-server user must hold exactly, and the
    declared place of its frozen manifest (under `@study_artifacts@`)."""

    name: str
    allow: tuple[str, ...]
    rights: tuple[str, ...]
    manifest: str


@dataclass(frozen=True)
class ToolServer:
    """The tool server's command and arguments, the checkout it runs from and its commit, and the conditions it serves.
    Each path is as declared: under `@study_artifacts@`."""

    command: str
    args: tuple[str, ...]
    clone: str
    commit: str
    start_timeout_s: float
    conditions: tuple[Condition, ...]

    def named(self) -> tuple[str, ...]:
        """Every declared path, in the order the file gives them: the command, the clone, each condition's manifest."""
        return (self.command, self.clone, *(c.manifest for c in self.conditions))


def read_slots(read: Any, value: Any, principals: Mapping[str, Any]) -> tuple[Slot, ...]:
    """The `[slots]` table, checked against the layout's principals (`name -> Principal`). `read` is the layout reader
    (`fedspec._Reader`). Each refusal names the file and the key."""
    tables = read.named_tables(value, "slots", NAME, "slot")
    slots, used = [], {}
    for name, table in tables.items():
        key = f"slots.{name}"
        fields = read.table(table, key, SLOT_KEYS, required=SLOT_KEYS)
        users = {}
        for plane in ("data_path", "toolserver"):
            user = read.name(fields[plane], f"{key}.{plane}", set(principals), "principal")
            if user in used:
                raise read.fail(
                    f"{key}.{plane}", f"{user} is already a user of {used[user]}: a user belongs to one slot"
                )
            used[user] = f"{key}.{plane}"
            if not principals[user].agent:
                raise read.fail(f"{key}.{plane}", f"{user} is declared agent = false, which no leased user may be")
            users[plane] = user
        if users["data_path"] == users["toolserver"]:
            raise read.fail(f"{key}.toolserver", "is the data path's user: the planes never share a principal")
        if set(principals[users["data_path"]].privileges) != set(BASELINE_RIGHTS):
            raise read.fail(
                f"{key}.data_path",
                f"{users['data_path']} must hold exactly {' and '.join(BASELINE_RIGHTS)}: the data path's token is "
                "checked against those rights alone",
            )
        problem = rights_problem(list(principals[users["toolserver"]].privileges))
        if problem is not None:
            raise read.fail(f"{key}.toolserver", f"{users['toolserver']}'s privileges {problem}")
        slots.append(Slot(name, users["data_path"], users["toolserver"]))
    reference = frozenset(principals[slots[0].data_path].spaces)
    for slot in slots:
        for plane in ("data_path", "toolserver"):
            if frozenset(principals[getattr(slot, plane)].spaces) != reference:
                raise read.fail(
                    f"slots.{slot.name}.{plane}",
                    "is a member of other spaces than the first slot's data-path user: a slot's users must be "
                    "members of exactly the plane's spaces, and any slot may serve any run",
                )
    return tuple(slots)


def _path(read: Any, value: Any, key: str, what: str) -> str:
    if not isinstance(value, str) or not ARTIFACT_PATH.fullmatch(value):
        raise read.fail(
            key,
            f"must be {what} under {STUDY_ARTIFACTS}, of plain names (letters, digits, _ . -; no step up, no dotfile): "
            "a layout holds no path of a host",
        )
    return value


def read_toolserver(read: Any, value: Any, slots: Sequence[Slot], principals: Mapping[str, Any]) -> ToolServer:
    """The `[toolserver]` table: the launcher's rule for the command and arguments (`launch_argv`, so the layout never
    declares a server the platform file would be refused for), the pinned checkout, and each condition's rights, which
    some slot's tool-server user must hold exactly. `slots` must not be empty."""
    if not slots:
        raise read.fail("toolserver", "needs [slots]: the users the tool server runs as are the slots'")
    table = read.table(
        value, "toolserver", TOOLSERVER_KEYS, required={"command", "args", "clone", "commit", "conditions"}
    )
    command = _path(read, table["command"], "toolserver.command", "a Python interpreter")
    args = table["args"]
    if not isinstance(args, list) or not all(
        isinstance(a, str) and a and a.isprintable() and "@" not in a for a in args
    ):
        raise read.fail("toolserver.args", "must be a list of printable strings that name no placeholder")
    if len(args) < 2 or args[0] != "-m" or not MODULE.fullmatch(args[1]):
        raise read.fail("toolserver.args", "must start with -m <module>: the launcher runs the server as a module")
    try:
        launch_argv(command, args, Path("resolve_launch.py"), Path("address-map.json"))
    except AddressMapError as error:
        raise read.fail("toolserver.command", str(error)) from None
    clone = _path(read, table["clone"], "toolserver.clone", "the pinned checkout")
    commit = table["commit"]
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise read.fail("toolserver.commit", "must be the pinned commit: forty lower-case hexadecimal digits")
    timeout = read.integer(table.get("start_timeout_s", 120), "toolserver.start_timeout_s", 1, 3600)
    held = {tuple(sorted(principals[slot.toolserver].privileges)) for slot in slots}
    conditions = []
    for name, entry in read.named_tables(table["conditions"], "toolserver.conditions", NAME, "condition").items():
        key = f"toolserver.conditions.{name}"
        fields = read.table(entry, key, CONDITION_KEYS, required=CONDITION_KEYS)
        allow = fields["allow"]
        if (
            not isinstance(allow, list)
            or not allow
            or not all(isinstance(t, str) and TOOL.fullmatch(t) for t in allow)
            or len(set(allow)) != len(allow)
        ):
            raise read.fail(f"{key}.allow", f"must list at least one tool, each once (pattern {TOOL.pattern})")
        rights = fields["rights"]
        if not isinstance(rights, list) or not all(isinstance(r, str) for r in rights):
            raise read.fail(f"{key}.rights", "must be a list of rights")
        problem = rights_problem(rights)
        if problem is not None:
            raise read.fail(f"{key}.rights", problem)
        if tuple(sorted(rights)) not in held:
            raise read.fail(
                f"{key}.rights",
                "are the privileges of no slot's tool-server user: its runs could lease no slot (a user must hold "
                "exactly a condition's rights)",
            )
        manifest = _path(read, fields["manifest"], f"{key}.manifest", "a frozen manifest")
        conditions.append(Condition(name, tuple(allow), tuple(sorted(rights)), manifest))
    return ToolServer(command, tuple(args), clone, commit, float(timeout), tuple(conditions))
