# Provenance: new
"""The mount-guard instrument: whether the agent touched the mount's token or oneclient's process, from the host
watcher's log (namespace `mount_guard`).

`MountGuard(source, nonce)` is an `Instrument` a platform's executor builds for each run whose mount cannot hide the
token from the agent the way a separate container does (an HPC batch node, where oneclient and the agent's cell run as
one uid). `source.mount_watch_log()` returns the bytes of the watcher's log, or None when there is none; `nonce` is the
value the host gave the run's watcher, so a log of another run is never read as this one's. The executor closes the
window (`disarmed`) after the agent's cell has stopped and stops the watcher before `finish_instruments`: a watcher
still running has written no end line, and its log reads as unknown (`not_closed`).

The snapshot, under `record.instruments["mount_guard"]`, holds counts and reasons, never a pid, a path or a name:

    {"version": 1, "log": "intact" | "unknown", "unknown": [<reasons>],
     "isolation": "proven" | "unproven", "unproven": [<reasons>],
     "accesses": {"token_file": 0, "token_dir": 0}, "traced": 0, "proofs": 2, "ptrace_scope": 1}

`accesses` are the inotify events on the token file and on its directory inside the agent's window (named so that the
record's redaction, which takes a key `token` or `..._token` for a secret's, leaves the counts as they are), `traced`
the tracers attached to oneclient inside it, `proofs` the isolation proofs read in it, and `ptrace_scope` the
kernel's Yama setting (None without Yama). When the log is unknown the counts are what its readable part shows, a
lower bound. The reasons are `expcore.mountguard.log`'s. **A missing or damaged log is `"log": "unknown"`, never
intact with no access; a log without a proof, or with a failed one, is `"isolation": "unproven"`.**

**A run whose mount was restored** (O97: a cell's deadline aborted the mount and the survivors were the agent's) has one
watcher for each generation of its oneclient, each with its own complete log (its canary, its window and its end line),
all with the run's nonce. `source.mount_watch_log()` then returns the logs in order, one entry for each generation
started (None for one that is missing, which reads as unknown); a run with one generation returns its one log, as it
always did, and its snapshot is exactly what it was. The snapshot of several generations is `combine`d: the verdict is
the worst (`log` is intact only if every generation's is, the isolation proven only if no generation has a failed proof
or another reason, and the run has a proof at least: a generation that held no cell has none of its own, which the
others' satisfy), the accesses and tracers are summed and the proofs are the union, the reasons are every
generation's, and the key
`generations` lists each generation's own snapshot, which `flags.judge` reads the verdict from, never the top level
alone.

**A run with no mount** (a REST run, a run with no data path) has nothing for the guard to watch: its exposure to the
mount's token is nil, so the guard does not apply to it. `mount_guard_for(sandbox)` builds the instrument of any run, a
`MountGuard` for a mount run and `NoMount` for the others, so a study may list it for every run of a campaign that
mixes data paths. `NoMount` records the statement `{"version": 1, "mount": false, "data_path": "rest" | "none"}`, and
`judge` reads it as `not_applicable`: the run stays in the mount guard's rerun. **The sandbox's own data path decides,
never a missing watcher or a missing snapshot:** a mount run whose helper keeps no watcher has no nonce and is
refused, a mount run whose record lacks the snapshot is `unknown`, and a statement that is not exactly the one above is
`unknown` too.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from expcore.mountguard.log import NO_PROOF, Reading, read_log

NAMESPACE = "mount_guard"
VERSION = 1
GENERATIONS = "generations"  # the optional key of a snapshot of a run whose mount was restored: one snapshot per log
INTACT, UNKNOWN = "intact", "unknown"
PROVEN, UNPROVEN = "proven", "unproven"
MOUNT_PATH = "mount"  # the data path that has a mount
NO_MOUNT_PATHS = ("rest", "none")  # the data paths that have none (`expcore_onedata.sandbox.api.DataPathMode`)
NOT_APPLICABLE_KEYS = {"version", "mount", "data_path"}


class MountWatchSource(Protocol):
    """Where the run's watcher log is: its bytes, or None when there is none; for a run whose mount was restored, one
    entry for each generation, in order (the module's text)."""

    def mount_watch_log(self) -> Sequence[bytes | None] | bytes | None: ...


def snapshot(reading: Reading) -> dict[str, Any]:
    """The instrument's snapshot of one log reading (see the module's text)."""
    return {
        "version": VERSION,
        "log": INTACT if reading.intact else UNKNOWN,
        "unknown": list(reading.problems),
        "isolation": UNPROVEN if reading.isolation_problems else PROVEN,
        "unproven": list(reading.isolation_problems),
        "accesses": dict(reading.accesses),
        "traced": reading.traced,
        "proofs": reading.proofs,
        "ptrace_scope": reading.ptrace_scope,
    }


def logs_of(found: Sequence[bytes | None] | bytes | None) -> list[bytes | None]:
    """The logs a source gave, in order: one for bytes or None, one for each entry of a sequence. An empty sequence is
    one missing log, never no log: a run whose mount had no watcher is not one the guard reads as clean."""
    if found is None or isinstance(found, bytes | bytearray):
        return [None if found is None else bytes(found)]
    return list(found) or [None]


def combine(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The snapshot of a run from each generation's snapshot (the module's text). One generation is its own snapshot,
    unchanged and with no `generations` key; several are the worst of them, with each listed under `generations`."""
    if len(parts) == 1:
        return dict(parts[0])

    proofs = sum(part["proofs"] for part in parts)

    def reasons(key: str) -> list[str]:
        return list(dict.fromkeys(reason for part in parts for reason in part[key]))

    # A generation whose log is intact and that held no cell has no proof of its own, which the others' satisfy (the
    # proofs are the union): its `no_proof` counts only when no generation has one. A log that is not intact keeps it.
    unproven = list(
        dict.fromkeys(
            reason
            for part in parts
            for reason in part["unproven"]
            if not (reason == NO_PROOF and proofs and part["log"] == INTACT)
        )
    )
    scopes = [part["ptrace_scope"] for part in parts if part["ptrace_scope"] is not None]
    return {
        "version": VERSION,
        "log": INTACT if all(part["log"] == INTACT for part in parts) else UNKNOWN,
        "unknown": reasons("unknown"),
        "isolation": UNPROVEN if unproven else PROVEN,
        "unproven": unproven,
        "accesses": {key: sum(part["accesses"][key] for part in parts) for key in parts[0]["accesses"]},
        "traced": sum(part["traced"] for part in parts),
        "proofs": proofs,
        "ptrace_scope": max(scopes) if scopes else None,
        GENERATIONS: [dict(part) for part in parts],
    }


def aggregate(logs: Sequence[bytes | None], nonce: str) -> dict[str, Any]:
    """The snapshot of a run's watcher logs, one for each generation of its mount, read with the run's nonce: for one
    log, exactly `snapshot(read_log(log, nonce))`."""
    return combine([snapshot(read_log(data, nonce)) for data in (list(logs) or [None])])


class MountRunSource(MountWatchSource, Protocol):
    """A sandbox as `mount_guard_for` reads it: the data path it was started with, which only its own spec says, and
    the nonce of its mount's watcher (None without one)."""

    @property
    def data_path_kind(self) -> str: ...

    @property
    def mount_guard_nonce(self) -> str | None: ...


def not_applicable_snapshot(data_path: str) -> dict[str, Any]:
    """The statement of a run whose data path has no mount (see the module's text)."""
    return {"version": VERSION, "mount": False, "data_path": data_path}


class NoMount:
    """The `Instrument` of a run whose data path has no mount: it records that the guard does not apply, and reads
    nothing. Built through `mount_guard_for`, which takes the data path from the sandbox."""

    namespace = NAMESPACE

    def __init__(self, data_path: str) -> None:
        if data_path not in NO_MOUNT_PATHS:
            raise ValueError(f"a run with no mount has a data path of {NO_MOUNT_PATHS}, not {data_path!r}")
        self._data_path = data_path

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read."""

    def finish(self) -> dict[str, Any]:
        return not_applicable_snapshot(self._data_path)


def mount_guard_for(sandbox: MountRunSource) -> "MountGuard | NoMount":
    """The mount-guard instrument of one run, from its sandbox: a `MountGuard` over the watcher's log for a mount run
    (`ValueError` when its helper has no nonce: an unguarded mount is not a run the guard does not apply to), `NoMount`
    for a run whose data path is REST or none. The data path is the sandbox's, never inferred from a missing nonce or
    log."""
    kind = sandbox.data_path_kind
    if kind == MOUNT_PATH:
        return MountGuard(sandbox, sandbox.mount_guard_nonce)  # a missing nonce raises there
    if kind in NO_MOUNT_PATHS:
        return NoMount(kind)
    raise ValueError(f"the run's data path is {kind!r}, not one of {(MOUNT_PATH, *NO_MOUNT_PATHS)}")


class MountGuard:
    """The `Instrument` of one run's mount guard: one per run, holding the source of its watcher's log."""

    namespace = NAMESPACE

    def __init__(self, source: MountWatchSource, nonce: str | None) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("the run's watcher nonce is a non-empty text")
        self._source, self._nonce = source, nonce

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: the watcher's log begins before the agent's cell."""

    def finish(self) -> dict[str, Any]:
        """The snapshot of the watcher's log (of each generation's, for a restored mount), read once, after the watcher
        has stopped."""
        return aggregate(logs_of(self._source.mount_watch_log()), self._nonce)
