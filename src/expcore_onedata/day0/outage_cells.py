# Provenance: new
"""What the outage check measures, one cell at a time: a read, a listing, a block map, a provider's state, a transfer.

Every cell is one bounded call or a bounded run of them, made as the agent's side makes it (through the agent's local
provider, in the space's own scope), and returns plain facts: what answered (`status`, Onedata's error id `code` and
`errno`, whether `transport` failed, a mount's `os_errno`), how long it took (`seconds`, against `bound_s`) and an
`outcome` from one vocabulary:

- `served`: the whole answer came and it is the file, the listing or the block map that was expected;
- `partial`: part of it came (bytes before the error, entries missing, a provider left out of a block map);
- `differs`: a whole answer that is not what was expected (another hash, other entries or sizes);
- `refused`: an answer with an error (its status and error id, or the mount's errno);
- `no answer`: nothing came back before the bound (a refused or reset connection);
- `blocked`: nothing came back until the bound: the call waited as long as it was allowed to.

Unknown is never one of them: a cell that cannot be read says `refused` or `no answer` with what it saw, and the check
decides what that means. Providers are named by their position (`providers[i]`) and a file by the role it plays, never
by an id or a name (O12).
"""

import errno as errnos
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from expcore_onedata.client import PlatformError, answered_absent
from expcore_onedata.config import PlatformConfig, StudySpace
from expcore_onedata.consistency import ENDED_STATES, transfer_state
from expcore_onedata.day0.registry import MountAccess
from expcore_onedata.placement import held_bytes, provider_blocks
from expcore_onedata.provision import LISTING_ATTRIBUTES, LISTING_LIMIT, Provisioner, listing_entry, next_page_token

BLOCKED_FRACTION = 0.9  # an unanswered call that took this much of its bound was blocked, not refused
ROLES = ("target", "mirror", "local")


@dataclass(frozen=True)
class Roles:
    """The three providers of a space in the outage: the one taken down, the one holding a replica, the agent's own."""

    target: str
    mirror: str
    local: str
    labels: Mapping[str, str]  # role -> `providers[i]`
    ids: Mapping[str, str]  # role -> the provider's id in Onezone

    @classmethod
    def of(cls, config: PlatformConfig, ids: Mapping[str, str], target: str, mirror: str, local: str) -> "Roles":
        """The roles of three declared providers, labelled by their positions in platform.toml."""
        names = {"target": target, "mirror": mirror, "local": local}
        return cls(
            target,
            mirror,
            local,
            {role: config.provider_label(name) for role, name in names.items()},
            {role: ids[name] for role, name in names.items()},
        )

    def name(self, role: str) -> str:
        """The provider name that plays a role."""
        return {"target": self.target, "mirror": self.mirror, "local": self.local}[role]


@dataclass(frozen=True)
class ScratchFile:
    """A file the check made: the pair it belongs to, the role it plays, what it holds and its id."""

    pair: str
    kind: str  # "replicated" (held by the target and the mirror) or "owner_only" (held by the target alone)
    path: str
    file_id: str
    data: bytes

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def key(self) -> str:
        """How the report names it: its pair and its kind."""
        return f"{self.pair}/{self.kind}"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def failure_facts(error: BaseException, seconds: float, bound_s: float) -> dict[str, Any]:
    """What a failed call shows: the answer's status, error id and errno, or the mount's errno, and its outcome."""
    facts: dict[str, Any] = {}
    answered = False
    if isinstance(error, TimeoutError):
        facts.update(status=None, transport=True)
    elif isinstance(error, OSError):  # a mount answering with an error
        facts.update(status=None, transport=False, os_errno=errnos.errorcode.get(error.errno or 0, "unknown"))
        answered = True
    elif isinstance(error, PlatformError):
        facts.update(status=error.status, code=error.code, errno=error.errno, transport=error.transport)
        answered = error.status is not None
    else:
        raise error
    if answered:
        facts["outcome"] = "refused"
    else:
        facts["outcome"] = "blocked" if seconds >= BLOCKED_FRACTION * bound_s else "no answer"
    return facts


class Cells:
    """The measurements of one space's outage, made through its local provider, each bounded by `bound_s`."""

    def __init__(
        self,
        client: Any,
        entry: StudySpace,
        provisioner: Provisioner,
        roles: Roles,
        *,
        bound_s: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        mount: MountAccess | None = None,
    ) -> None:
        self._client, self._entry, self._prov, self._roles = client, entry, provisioner, roles
        self.bound_s, self._clock, self._sleep, self._mount = bound_s, clock, sleep, mount
        self._transfers: dict[str, str] = {}  # a file's key -> the id of the replication scheduled for it

    def _timed(self, call: Callable[[], Any]) -> tuple[Any, BaseException | None, float]:
        """Run a call and return its value or its error, and the seconds it took on the check's clock."""
        began = self._clock()
        try:
            value, error = call(), None
        except (PlatformError, OSError) as failure:
            value, error = None, failure
        return value, error, round(self._clock() - began, 3)

    def _mount_access(self) -> MountAccess:
        if self._mount is None:
            raise LookupError("this run has no mount to read through")
        return self._mount

    def _base(self, seconds: float) -> dict[str, Any]:
        return {"seconds": seconds, "bound_s": self.bound_s}

    # -- reads of a file ------------------------------------------------------------------------------------------
    def read(self, file: ScratchFile, provider: str | None = None) -> dict[str, Any]:
        """Read a whole file through the REST data path of a provider (the local one by default), streamed, hashed."""
        received = 0

        def sink(piece: bytes) -> None:
            nonlocal received
            received += len(piece)

        digest, error, seconds = self._timed(
            lambda: self._client.read_content(
                f"/data/{file.file_id}/content",
                provider=provider or self._roles.local,
                size=len(file.data),
                chunk_bytes=self._prov.settings.chunk_bytes,
                deadline_s=self.bound_s,
                timeout_s=self.bound_s,
                sink=sink,
                clock=self._clock,
                space=self._entry.space,
            )
        )
        facts = {**self._base(seconds), "bytes": received, "expected_bytes": len(file.data)}
        return self._read_outcome(facts, file, error, digest.sha256 if error is None else None, received)

    def read_mount(self, file: ScratchFile) -> dict[str, Any]:
        """Read a whole file through the mount; the check passes a mount only when a run has one."""
        mount = self._mount_access()
        body, error, seconds = self._timed(lambda: mount.read(self._entry, file.path, timeout_s=self.bound_s))
        facts = {**self._base(seconds), "bytes": len(body) if error is None else 0, "expected_bytes": len(file.data)}
        sha = hashlib.sha256(body).hexdigest() if error is None else None
        return self._read_outcome(facts, file, error, sha, facts["bytes"])

    def _read_outcome(
        self, facts: dict[str, Any], file: ScratchFile, error: BaseException | None, sha: str | None, received: int
    ) -> dict[str, Any]:
        if error is not None:
            facts.update(failure_facts(error, facts["seconds"], self.bound_s))
            if received and facts["outcome"] != "blocked":
                facts["outcome"] = "partial"
            return facts
        facts["hash_matches"] = sha == file.sha256 and received == len(file.data)
        facts["outcome"] = "served" if facts["hash_matches"] else "differs"
        return facts

    # -- listings -------------------------------------------------------------------------------------------------
    def _names(self, directory_id: str) -> dict[str, int]:
        """Every entry of a directory through the local provider, name -> size, to the last page's marker (O26)."""
        entries: dict[str, int] = {}
        params: dict[str, Any] = {"limit": LISTING_LIMIT, "attribute": LISTING_ATTRIBUTES}
        while True:
            page = self._client.get(
                f"/data/{directory_id}/children", provider=self._roles.local, params=params, timeout_s=self.bound_s
            )
            token = next_page_token(page)
            for item in page["children"]:
                found = listing_entry(item)
                entries[found.name] = found.size
            if token is None:
                return entries
            params = {**params, "token": token}

    def listing(self, directory_id: str, expected: Mapping[str, int]) -> dict[str, Any]:
        """List a directory through the local provider and compare it with the entries it must hold."""
        found, error, seconds = self._timed(lambda: self._names(directory_id))
        facts = {**self._base(seconds), "expected_entries": len(expected)}
        if error is not None:
            return {**facts, **failure_facts(error, seconds, self.bound_s)}
        missing = [name for name in expected if name not in found]
        unexpected = [name for name in found if name not in expected]
        resized = [name for name in found if name in expected and found[name] != expected[name]]
        outcome = "served" if not (missing or unexpected or resized) else "differs"
        if missing and not (unexpected or resized):
            outcome = "partial"
        return {
            **facts,
            "outcome": outcome,
            "entries": len(found),
            "missing": len(missing),
            "unexpected": len(unexpected),
            "size_differs": len(resized),
        }

    def listing_mount(self, directory: str, expected: Mapping[str, int]) -> dict[str, Any]:
        """List a directory through the mount (names only: a mount's listing has no sizes here)."""
        mount = self._mount_access()
        names, error, seconds = self._timed(lambda: mount.listdir(self._entry, directory, timeout_s=self.bound_s))
        facts = {**self._base(seconds), "expected_entries": len(expected)}
        if error is not None:
            return {**facts, **failure_facts(error, seconds, self.bound_s)}
        missing = [name for name in expected if name not in names]
        unexpected = [name for name in names if name not in expected]
        outcome = "served" if not (missing or unexpected) else "partial" if not unexpected else "differs"
        return {
            **facts,
            "outcome": outcome,
            "entries": len(names),
            "missing": len(missing),
            "unexpected": len(unexpected),
        }

    # -- the knowledge plane: the REST calls the tools make ---------------------------------------------------------
    def distribution(self, file: ScratchFile) -> dict[str, Any]:
        """`get_file_distribution`: the file's block map through the local provider, who holds how much by role."""
        answer, error, seconds = self._timed(
            lambda: self._client.get(
                f"/data/{file.file_id}/distribution", provider=self._roles.local, timeout_s=self.bound_s
            )
        )
        facts = self._base(seconds)
        if error is not None:
            return {**facts, **failure_facts(error, seconds, self.bound_s)}
        rows: dict[str, Any] = {}
        for role in ROLES:
            try:
                rows[self._roles.labels[role]] = {
                    "listed": True,
                    "held_bytes": held_bytes(provider_blocks(answer, self._roles.ids[role])),
                }
            except ValueError:  # the provider is not in the answer, or its table is unreadable: unknown, never zero
                rows[self._roles.labels[role]] = {"listed": False, "held_bytes": None}
        listed = all(row["listed"] for row in rows.values())
        return {**facts, "outcome": "served" if listed else "partial", "providers": rows}

    def providers_online(self, *, until_target_offline: bool) -> dict[str, Any]:
        """`list_space_providers`: whether Onezone calls each provider of the space online, read as often as the bound
        allows until it calls the target offline (`until_target_offline`), and the seconds that took."""
        began, polls = self._clock(), 0
        online: dict[str, bool | None] = {}
        offline_after: float | None = None
        while True:
            polls += 1
            for role in ROLES:
                online[self._roles.labels[role]] = self._online(role)
            if online[self._roles.labels["target"]] is False and offline_after is None:
                offline_after = round(self._clock() - began, 3)
            elapsed = self._clock() - began
            if not until_target_offline or offline_after is not None or elapsed >= self.bound_s:
                break
            self._sleep(self._prov.settings.poll_s)
        facts: dict[str, Any] = {"online": online, "polls": polls, **self._base(round(self._clock() - began, 3))}
        if until_target_offline:
            facts["target_offline_after_s"] = offline_after
        return {**facts, "outcome": "served" if None not in online.values() else "partial"}

    def _online(self, role: str) -> bool | None:
        """Onezone's online flag of a provider; None when it could not be read."""
        try:
            flag = self._client.get(f"/providers/{self._roles.ids[role]}").get("online")
        except PlatformError:
            return None
        return flag if isinstance(flag, bool) else None

    def replicate_to_local(self, file: ScratchFile) -> dict[str, Any]:
        """`schedule_file_replication` to the local provider, then `get_transfer` read until it ends or the bound."""
        began = self._clock()
        sent, error, seconds = self._timed(
            lambda: self._prov.schedule_transfer("replication", file.file_id, self._roles.local)
        )
        if error is not None:
            refused = {**self._base(seconds), **failure_facts(error, seconds, self.bound_s)}
            return {"schedule_file_replication": refused, "get_transfer": None, "local_held_bytes": None}
        self._transfers[file.key] = sent
        return {
            "schedule_file_replication": {**self._base(seconds), "outcome": "served"},
            "get_transfer": self._follow(sent, began),
            "local_held_bytes": self._local_held(file),
        }

    def _local_held(self, file: ScratchFile) -> int | None:
        """What the local provider's block map says it holds of a file now; None when that could not be read."""
        facts = self.distribution(file)
        row = facts.get("providers", {}).get(self._roles.labels["local"])
        return row["held_bytes"] if row else None

    def _follow(self, transfer_id: str, began: float) -> dict[str, Any]:
        """Read a transfer's record every poll until it has ended or the bound, keeping the states it went through."""
        trail: list[list[Any]] = []
        record: Any = {}
        error: BaseException | None = None
        while True:
            record, error, _ = self._timed(
                lambda: self._client.get(
                    f"/transfers/{transfer_id}", provider=self._roles.local, timeout_s=self.bound_s
                )
            )
            if error is not None and not (isinstance(error, PlatformError) and answered_absent(error)):
                break
            state = "not shown yet" if error is not None else transfer_state(record)
            if not trail or trail[-1][1] != state:
                trail.append([round(self._clock() - began, 3), state])
            elapsed = self._clock() - began
            if state in ENDED_STATES or elapsed >= self.bound_s:
                break
            self._sleep(self._prov.settings.poll_s)
        seconds = round(self._clock() - began, 3)
        facts: dict[str, Any] = {
            **self._base(seconds),
            "states": trail,
            "ended": bool(trail) and trail[-1][1] in ENDED_STATES,
        }
        if error is not None and not facts["ended"]:
            return {**facts, **failure_facts(error, seconds, self.bound_s)}
        facts["outcome"] = "served" if facts["ended"] else "blocked"
        if isinstance(record, dict):
            facts.update({k: record[k] for k in ("bytesReplicated", "filesReplicated", "filesFailed") if k in record})
        return facts

    def transfer_now(self, file: ScratchFile) -> dict[str, Any] | None:
        """One read of the transfer scheduled for a file, after the restore; None when none was scheduled."""
        transfer_id = self._transfers.get(file.key)
        if transfer_id is None:
            return None
        record, error, seconds = self._timed(
            lambda: self._client.get(f"/transfers/{transfer_id}", provider=self._roles.local, timeout_s=self.bound_s)
        )
        if error is not None:
            return {**self._base(seconds), **failure_facts(error, seconds, self.bound_s)}
        return {**self._base(seconds), "outcome": "served", "state": transfer_state(record)}

    # -- the target ---------------------------------------------------------------------------------------------
    def target_answers(self, timeout_s: float) -> dict[str, Any]:
        """Ask the target for its configuration directly: whether it answers, as a down provider must not."""
        _, error, seconds = self._timed(
            lambda: self._client.get("/configuration", provider=self._roles.target, timeout_s=timeout_s)
        )
        if error is None:
            return {"answers": True, "seconds": seconds}
        return {"answers": False, "seconds": seconds, **failure_facts(error, seconds, timeout_s)}
