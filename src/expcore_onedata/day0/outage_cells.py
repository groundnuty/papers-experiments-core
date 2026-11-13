# Provenance: new
"""What the outage check measures over REST, one cell at a time: a read, a listing, and whether the target answers.

The mount's cells are `outage_mount` (through a real sandbox) and the knowledge plane's `outage_tools` (the agent's
tools, through MCP); all three share the outcome vocabulary below. Every cell here is one bounded call or a bounded run
of them, made as the agent's side makes it (through the agent's local provider, in the space's own scope), and returns
plain facts: what answered (`status`, Onedata's error id `code` and `errno`, whether `transport` failed), how long it
took (`seconds`, against `bound_s`) and an `outcome`:

- `served`: the whole answer came and it is the file or the listing that was expected;
- `partial`: part of it came (bytes before the error, entries missing, a listing cut after some pages);
- `differs`: a whole answer that is not what was expected (another hash, other entries or sizes);
- `refused`: an answer with an error (its status and error id, or the mount's errno, or the tool's wording);
- `no answer`: nothing came back before the bound (a refused or reset connection, a tool server gone);
- `blocked`: nothing came back until the bound: the call waited as long as it was allowed to.

Unknown is never one of them: a cell that cannot be read says `refused` or `no answer` with what it saw, and the check
decides what that means. Providers are named by their position (`providers[i]`) and a file by the role it plays, never
by an id or a name (O12).
"""

import errno as errnos
import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from expcore_onedata.client import PlatformError
from expcore_onedata.config import PlatformConfig, StudySpace
from expcore_onedata.provision import LISTING_ATTRIBUTES, LISTING_LIMIT, Provisioner, listing_entry, next_page_token
from expcore_onedata.readretry import ONE_ATTEMPT

BLOCKED_FRACTION = 0.9  # an unanswered call that took this much of its bound was blocked, not refused
ROLES = ("target", "mirror", "local")
KINDS = ("replicated", "owner_only")  # the files of a pair: held by the target and the mirror, or by the target alone
WORD = re.compile(r"[A-Za-z][A-Za-z_]{0,63}")  # an error id or a class name: letters, never an id, host or address


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


def word(value: Any, pattern: re.Pattern[str] = WORD) -> str | None:
    """A word an answer names (an error id, an errno's name, an exception's class) as it may be recorded (O12): the
    word when it is one of the shape `pattern` allows, else `other`; None stays None."""
    if value is None:
        return None
    return value if isinstance(value, str) and pattern.fullmatch(value) else "other"


def unanswered(seconds: float, bound_s: float) -> str:
    """The outcome of a call that got no answer: `blocked` when it took the bound's blocked fraction, else `no
    answer`. A timeout is one, whether a transport's or a mount's ETIMEDOUT (Python's `TimeoutError`)."""
    return "blocked" if seconds >= BLOCKED_FRACTION * bound_s else "no answer"


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
        facts.update(status=error.status, code=word(error.code), errno=word(error.errno), transport=error.transport)
        answered = error.status is not None
    else:
        raise error
    facts["outcome"] = "refused" if answered else unanswered(seconds, bound_s)
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
    ) -> None:
        self._client, self._entry, self._prov, self._roles = client, entry, provisioner, roles
        self.bound_s, self._clock, self._sleep = bound_s, clock, sleep

    def _timed(self, call: Callable[[], Any]) -> tuple[Any, BaseException | None, float]:
        """Run a call and return its value or its error, and the seconds it took on the check's clock."""
        began = self._clock()
        try:
            value, error = call(), None
        except (PlatformError, OSError) as failure:
            value, error = None, failure
        return value, error, round(self._clock() - began, 3)

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
                gate_retry=ONE_ATTEMPT,  # a measurement: a stalled gate read is the cell's recorded outcome (O29)
            )
        )
        facts = {**self._base(seconds), "bytes": received, "expected_bytes": len(file.data)}
        return self._read_outcome(facts, file, error, digest.sha256 if error is None else None, received)

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
    def _names(self, directory_id: str, found: dict[str, int]) -> None:
        """Every entry of a directory through the local provider, name -> size, to the last page's marker (O26), put
        into `found` as each page comes (it is the point: a page that fails leaves what came before it there)."""
        params: dict[str, Any] = {"limit": LISTING_LIMIT, "attribute": LISTING_ATTRIBUTES}
        while True:
            page = self._client.get(
                f"/data/{directory_id}/children",
                provider=self._roles.local,
                params=params,
                timeout_s=self.bound_s,
                retry=ONE_ATTEMPT,  # a measurement: a page that stalls is what the cell records (O29)
            )
            token = next_page_token(page)
            for item in page["children"]:
                entry = listing_entry(item)
                found[entry.name] = entry.size
            if token is None:
                return
            params = {**params, "token": token}

    def listing(self, directory_id: str, expected: Mapping[str, int]) -> dict[str, Any]:
        """List a directory through the local provider and compare it with the entries it must hold. A listing that
        fails after some pages is `partial`, with the entries it got and the error that cut it."""
        found: dict[str, int] = {}
        _, error, seconds = self._timed(lambda: self._names(directory_id, found))
        facts = {**self._base(seconds), "expected_entries": len(expected)}
        if error is not None and not found:
            return {**facts, **failure_facts(error, seconds, self.bound_s)}
        missing = [name for name in expected if name not in found]
        unexpected = [name for name in found if name not in expected]
        resized = [name for name in found if name in expected and found[name] != expected[name]]
        outcome = "served" if not (missing or unexpected or resized) else "differs"
        if missing and not (unexpected or resized):
            outcome = "partial"
        counts = {"entries": len(found), "missing": len(missing), "unexpected": len(unexpected)}
        if error is not None:  # cut partway: what came is kept, with the error, and is never the whole listing
            return {**facts, **failure_facts(error, seconds, self.bound_s), **counts, "outcome": "partial"}
        return {**facts, "outcome": outcome, **counts, "size_differs": len(resized)}

    # -- the target ---------------------------------------------------------------------------------------------
    def target_answers(self, timeout_s: float) -> dict[str, Any]:
        """Ask the target for its configuration directly: whether it answers, as a down provider must not."""
        _, error, seconds = self._timed(
            lambda: self._client.get(
                "/configuration", provider=self._roles.target, timeout_s=timeout_s, retry=ONE_ATTEMPT
            )  # the answer, or the lack of one, is the measurement (O29)
        )
        if error is None:
            return {"answers": True, "seconds": seconds}
        return {"answers": False, "seconds": seconds, **failure_facts(error, seconds, timeout_s)}
