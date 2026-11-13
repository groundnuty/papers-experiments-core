# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/slot_users.py, blob 6bd4dcbb3acbafbf3070773537c37b460f817ba9
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Leased platform users for the two token planes (C19, O09).

A run needs a platform user for its data path and another for its tool server, because the audit log must name one run
and the planes never share a principal (architecture.md, "Tokens and permissions"). Users are declared in platform.toml
as **slots**: a slot is one lease key, and holding it gives the run both of its users, so a run takes one lease and two
leases can never be taken in a bad order.

```toml
[slots.<name>]                 # the lease key: letters, digits, '_' and '-'
[slots.<name>.data_path]       # the user whose token reads the data
username = "..."               # the login name whose credential file is read ([credentials.users])
user_id = "..."                # the id Onezone gives the user
[slots.<name>.toolserver]      # the user whose token the tool server gets; never the data path's
username = "..."
user_id = "..."
rights = ["space_view", ...]    # required: the rights this user holds, exactly; a condition's vocabulary and rules
```

The leases are the core's (`expcore.campaign.lease`): the plugin hands the scheduler `lease_group` and `lease_keys`
(`Slots.keys_of`), and the scheduler takes, holds and releases the key. `Slots.users_of(lease)` then says which two users
the run has. The slots are the only pool of leased users: the sandbox platform (`sandbox/provider.py`) leases a slot for
every run that needs a data-path token or a tool server, mints both through `SlotTokens`, and its probe is a `SlotProbe`
whose `sandbox_gone` is the sandbox's own probe.

**A slot serves the conditions whose rights its tool-server user holds (O31).** That user must hold exactly the rights
of the condition its token is minted for (`tokens.py`), so each set of rights the slots declare is a lease group of its
own, `group_of(rights)`, holding the slots that declare it in the order of the file; conditions with the same rights
share it. A run with a tool server leases from its condition's group, so one campaign can interleave conditions of
different rights, each run on a slot whose user holds its condition's, and waits only for a slot of that group. A run
with a data path and no tool server needs only a data-path user, which every slot has with the baseline rights, so it
leases from `GROUP`, every slot. A condition whose rights no slot declares has no group: asking for it is refused
(`Slots.group_for`), which the platform does for every run of the plan when the campaign is built. `SlotTokens.mint`
refuses, before it journals or mints anything, a slot that declares other rights than the condition's.

**A slot is free again only when its tokens have ended:** its sandbox is shown gone, and its tokens are either past
their journalled expiry plus a margin, or revoked, confirmed and settled. A token minted for a slot stays good until its
lifetime ends unless its user revokes it, and a run that died may have left a sandbox using it. `SlotTokens.mint` fixes the end of
its tokens before it mints anything: `valid_until` is the clock plus the lifetime the window asks for, taken once. It is
written to a journal beside the leases as a **provisional** line (durably: fsynced, with its directory when it created
the file), and then both tokens are minted with exactly that absolute expiry as their time caveat, so no delay between
the journal line and the mint (the privilege reads take time) can push a token past it. After each token is examined
its expiry must still equal the journalled one; a token that differs is refused, dropped and recorded with the expiry it
does have. When both are minted, an **exact** line with the same mint id settles the provisional one.

`SlotProbe.resource_gone` answers True for a slot only once the clock is past the slot's journalled end plus a safety
margin (the clocks of this host and of Onezone are not the same) and, when the platform supplies one, its sandbox probe
also shows the run's resources gone. The slot's end is the latest of its exact lines and of its provisional lines that
no exact line settled, so a crash between a mint and its exact line leaves the provisional end, which is already the
tokens' own, and the slot is never freed early. A line that cannot be read keeps the slot quarantined (the core's rule:
only True frees a key). A mint that fails leaves its provisional line in place, since a token minted and dropped ends no
later than that line.

**A run's end revokes its slot's tokens, so the slot frees minutes after the run and not when its tokens expire.**
`SlotTokens.revoke` (`revocation.py`) sends, for each of the slot's two users, that user's own revocation of all its
temporary tokens (HTTP Basic, no privilege; `load_slots` keeps every user to one slot and plane, so nothing of another
slot or of the fixed principals is touched), and requires the user's generation to have risen and every token the run
still holds to verify as `tokenRevoked`. Before the first bump it journals, durably, the revocation's **start** line;
only once every check has passed does it journal a **revoked** line with `settle_until`, the moment the revocation has
surely reached every provider (`[tokens] revocation_settle_s` after it was confirmed, 330 s by default, never less
than 315 s), never a token or a user. A revoked line caps, at `settle_until`, every line of its slot before its start
line, the indeterminate ones included, since a bump of the generation revokes a token whatever its expiry. A line
after the start line is never capped by it: it may be a mint that finished after the bump, with a token of the new
generation, so it keeps its own end whether or not anything is written after the cap. The lines before the start are
those of mints that had ended before it, since the caller keeps any mint off the slot (its run ended and no start
thread runs, or the command holds the slot's lease lock), so each of their tokens was minted at the old generation and
the bump revoked it. `SlotProbe.resource_gone` compares a settle end with the clock with no margin, because it records
an observation and not a prediction of another clock, and keeps the margin for every expiry. Anything that fails
journals no revoked line, so the expiry rule stands; a start line alone caps nothing. The sandbox platform revokes
when a run's launch ends (`sandbox/provider.py`), and `python -m expcore_onedata.lease revoke` does it for a slot
held from before (`lease.py`).

**The provisional bound assumes the platform honours the requested time caveat.** Caveats only restrict a token, so a
token minted with `validUntil` cannot outlive it unless the server disregards it. Every successful mint checks this, and a
token that breaks it is journalled with the expiry it has, or, when that cannot be read or the request was sent but had no
usable answer, as indeterminate, which holds the slot until an operator confirms the token has ended and removes the
line, or a confirmed revocation of the slot's tokens caps it. What the bound does not cover is a server that both disregards the caveat and answers unusably while this host
cannot write the indeterminate line; treating every unfinished mint as indeterminate would close that, at the price of an
operator clearing a slot after every crash mid-mint, and the trade was made for availability.

**That residual risk is recorded when it is seen.** A refused token whose expiry can be read and is later than the one it
was minted with (an `ExpiryMismatch` with a later expiry, or any other `DroppedToken` whose expiry is later) shows the
platform disregarding the requested time caveat. After its journal line, `SlotTokens` records a
`token_expiry_beyond_request` event through the platform's event sink (the one `toolserver.ToolServerLauncher` records
`tool_plane_rights_changed` through) with the slot by its position (`slots[i]`, never its configured name, O12) and both
expiries, never the token. A sink that fails does not hide the refusal, which is raised from it.

**`events` is a required argument (a change of interface).** `SlotTokens(..., events=...)` and, through it,
`sandbox.provider.SandboxPlatform(..., events=...)` take the platform's event sink and have no default: a silent
no-op would lose the event, so a caller that built either without one must now pass the sink it gives the tool-server
launcher.

Users are created by the operator, not by this layer: there is no unscoped user creation or deletion here. The privileges
a slot's users hold are checked, direct and effective, before every token is minted (`tokens.py`).

**A slot's tool-server user must be a member of no space outside the tool-server plane's, hold no Onezone admin
privilege, and be a member of no cluster.** A tool-server token carries no data-access caveat, a read-only condition's
as a writing one's (`tokens.tool_server_spec`, O39), so what it can reach is what its user's memberships and admin
privileges give, and a user who is a member of a provider's cluster can act through that provider: with
`cluster_update` it can mint that provider's tokens and change its configuration, with `cluster_delete` delete it.
Setting the user up so is the operator's part of the slot; the harness reads all three (`tokenscope`) when it mints a
tool-server token and at the tool server's launch and teardown, and refuses a user that is set up otherwise. Onezone has no read of another user's clusters, so the harness
reads them as the token itself, `GET /user/effective_clusters`, which answers the clusters that token acts in.
"""

import json
import math
import os
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from expcore.campaign.lease import Lease, process_gone
from expcore.config.loading import ConfigError, Section, load_toml
from expcore_onedata.config import NAME, PlatformConfig
from expcore_onedata.credentials import Principal
from expcore_onedata.rights import rights_problem
from expcore_onedata.tokens import DroppedToken, RunTokens, TokenIssuer, token_window

if TYPE_CHECKING:
    from expcore_onedata.revocation import Revoked
    from expcore_onedata.toolserver import EventSink

GROUP = "platform_users"  # every slot: the group of a run with a data path and no tool server
EVENT_EXPIRY_BEYOND_REQUEST = "token_expiry_beyond_request"
JOURNAL = "tokens.jsonl"
SAFETY_MARGIN_S = 60.0  # seconds past a journalled end before a slot counts as free: clock skew and whole-second expiry
REVOKED = "revoked"  # the plane of a revocation line: the slot's tokens before its start end by its settle_until
REVOKING = "revoking"  # the plane of a revocation's start line, written before the first bump: what it caps ends here


def group_of(rights: Iterable[str]) -> str:
    """The lease group of the slots whose tool-server user holds these rights: `GROUP`, a colon and the rights, sorted
    and joined by '+'. The privilege vocabulary names no federation (O12), so the group can be logged as it is."""
    return f"{GROUP}:{'+'.join(sorted(rights))}"


@dataclass(frozen=True)
class Slot:
    """One lease key, the two platform users it gives a run, and the rights its tool-server user holds (sorted)."""

    name: str
    data_path: Principal
    tool_server: Principal
    rights: tuple[str, ...]


@dataclass(frozen=True)
class Slots:
    """The declared slots, in the order of the file."""

    slots: tuple[Slot, ...]

    @property
    def groups(self) -> tuple[str, ...]:
        """The lease groups the slots declare: `GROUP`, then one per set of rights, in the order the file first has it."""
        return (GROUP, *dict.fromkeys(group_of(slot.rights) for slot in self.slots))

    def keys_of(self, group: str) -> list[str]:
        """Return the lease keys of the group, in the order of the file: every slot for `GROUP`, and for a group of rights
        the slots whose tool-server user declares them. Refuse a group the slots do not declare, so a typo, or rights no
        slot holds, is never an empty pool."""
        if group == GROUP:
            return [slot.name for slot in self.slots]
        keys = [slot.name for slot in self.slots if group_of(slot.rights) == group]
        if not keys:
            raise ValueError(
                f"the slots declare the lease group {GROUP!r} and one per set of tool-server rights "
                f"({', '.join(map(repr, self.groups[1:]))}), not {group!r}"
            )
        return keys

    def group_for(self, rights: Iterable[str]) -> str:
        """Return the lease group of a condition with these rights; raise ValueError when no slot's tool-server user
        declares them, since its runs could lease no slot."""
        wanted = sorted(rights)
        group = group_of(wanted)
        if group not in self.groups:
            raise ValueError(f"no slot's tool-server user declares the rights {', '.join(wanted)}")
        return group

    def label(self, key: str) -> str:
        """Name a slot for a message or an event by its position in the file, `slots[i]`, never by its name (O12)."""
        return f"slots[{[slot.name for slot in self.slots].index(key)}]"

    def get(self, key: str) -> Slot:
        """Return the slot a lease key names."""
        for slot in self.slots:
            if slot.name == key:
                return slot
        raise KeyError(f"no slot is named by the lease key {key!r}")

    def users_of(self, lease: Lease) -> Slot:
        """Return the slot a held lease gives; refuse a lease that has been released or is of a group the slots do not
        declare (ValueError), whose key names no slot (KeyError), or whose slot is not of its group (ValueError)."""
        if not lease.held or lease.group not in self.groups:
            raise ValueError("the lease is not a held lease of the platform users")
        slot = self.get(lease.key)
        if slot.name not in self.keys_of(lease.group):
            raise ValueError("the lease's slot is not one of its lease group's")
        return slot


def _principal(section: Section, key: str, *, extra: tuple[str, ...] = ()) -> tuple[Principal, Section]:
    """Read one user of a slot, and return it with its table, which may hold the keys `extra` too."""
    user = section.section(key)
    user.reject_unknown(("username", "user_id", *extra))
    try:
        return Principal(user.string("username"), user.string("user_id")), user
    except ValueError as error:
        raise user.fail(None, str(error)) from None


def _rights(user: Section) -> tuple[str, ...]:
    """Read the rights a slot's tool-server user holds: required, by a condition's rules, kept sorted as a condition's."""
    if "rights" not in user.data:
        raise user.fail(
            "rights", "is required: the rights the user holds decide the conditions that may lease the slot"
        )
    rights = user.strings("rights")
    problem = rights_problem(rights)
    if problem is not None:
        raise user.fail("rights", problem)
    return tuple(sorted(rights))


def load_slots(
    path: str | Path, config: PlatformConfig, *, environ: Mapping[str, str] | None = None, require_secrets: bool = True
) -> Slots:
    """Read `[slots]` of a platform.toml against its loaded configuration; raise ConfigError naming the file and key.

    Every username and every user id is used once, across slots, planes and the fixed `[principals]`. With
    `require_secrets` each user must have a credential file that others cannot read, as the fixed principals must.
    """
    file = Path(path).resolve()
    root = Section(load_toml(file), file, "platform.toml")
    table = root.section("slots")
    if not table.data:
        raise table.fail(None, "must declare at least one slot")
    fixed = [p for p in (config.principals.data_path, config.principals.toolserver, config.principals.service) if p]
    names = {p.username for p in fixed}
    ids = {p.user_id for p in fixed}
    slots: list[Slot] = []
    for index, (name, raw) in enumerate(table.data.items(), start=1):
        where = f"slots[{index}]"  # a position, never the slot's name (O12)
        if not NAME.fullmatch(name):
            raise table.fail(None, f"{where} must be named with letters, digits, '_' and '-', starting with a letter")
        if not isinstance(raw, Mapping):
            raise table.fail(None, f"{where} must be a table")
        section = Section(raw, file, where)
        section.reject_unknown(("data_path", "toolserver"))
        data_path, _ = _principal(section, "data_path")
        tool_server, tool_table = _principal(section, "toolserver", extra=("rights",))
        slot = Slot(name, data_path, tool_server, _rights(tool_table))
        for plane, user in (("data_path", slot.data_path), ("toolserver", slot.tool_server)):
            if user.username in names or user.user_id in ids:
                raise section.fail(plane, "names a user that another slot, plane or fixed principal already uses")
            names.add(user.username)
            ids.add(user.user_id)
            if require_secrets:
                if config.users is None:
                    raise section.fail(plane, "needs [credentials.users], which platform.toml does not declare")
                try:
                    config.users.check(user.username, environ)
                except ConfigError as error:
                    raise section.fail(plane, f"its credential cannot be used: {error}") from None
        slots.append(slot)
    return Slots(tuple(slots))


def _fsync_directory(directory: Path) -> None:
    """Make a directory's entries durable: a file created in it survives a crash only once this has returned."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class TokenJournal:
    """Append-only record of when each slot's tokens end: the slot, the plane and the expiry, never a token.

    A line is exact unless it is marked provisional. A provisional line carries the id of its mint, and an exact line
    with the same id settles it; one that nothing settled still counts, as the worst case of a mint that did not finish.
    """

    def __init__(self, directory: Path | str) -> None:
        self._path = Path(directory) / JOURNAL

    def record(
        self, slot: str, plane: str, expires_at: float, *, mint: str | None = None, provisional: bool = False
    ) -> None:
        """Append one line and make it durable before returning: write, flush and fsync the file, and fsync the
        directory when the line created the file (and that directory's parent when it created the directory), so
        neither a crash nor a power cut after this call can lose the expiry."""
        line: dict[str, Any] = {"slot": slot, "plane": plane, "expires_at": expires_at}
        if mint is not None:
            line["mint"] = mint
        if provisional:
            line["provisional"] = True
        self._append(line)

    def record_indeterminate(self, slot: str) -> None:
        """Record that a slot holds a dropped token whose expiry is unknown: the slot is held until an operator
        removes this line, having made sure the token has ended, or a later revocation caps it."""
        self._append({"slot": slot, "plane": "refused", "indeterminate": True})

    def record_revoking(self, slot: str, *, revocation: str, started_at: float) -> None:
        """Append, durably and before the first bump of a generation, the start of the revocation named `revocation`:
        its revoked line caps only the slot's lines before this one. A start line with no revoked line caps nothing.
        It carries `expires_at` equal to `started_at`, so a reader of the earlier journal takes it as an exact line that
        has already ended."""
        self._append(
            {
                "slot": slot,
                "plane": REVOKING,
                "expires_at": started_at,
                "revocation": revocation,
                "started_at": started_at,
            }
        )

    def record_revoked(
        self,
        slot: str,
        *,
        revocation: str,
        revoked_at: float,
        settle_until: float,
        generations: Mapping[str, tuple[int, int]],
        verified: int,
    ) -> None:
        """Append, durably, that the slot's tokens were revoked and confirmed: every line of the slot before the
        revocation's start line (`record_revoking`, with the same `revocation`) ends at `settle_until` at the latest,
        and no line after it is touched. `generations` are each plane's user's generation before and after, by plane,
        never by user. It carries `expires_at` equal to `settle_until` as well, so a reader of the earlier journal,
        which knows no revocation, takes it as an exact line and holds the slot as before."""
        line = {
            "slot": slot,
            "plane": REVOKED,
            "expires_at": settle_until,
            "revocation": revocation,
            "revoked_at": revoked_at,
            "settle_until": settle_until,
            "generations": {plane: list(pair) for plane, pair in generations.items()},
            "verified": verified,
        }
        self._append(line)

    def _lines(self) -> list[dict[str, Any]]:
        """Every line, in order; raise if the journal cannot be read or a line is not an object with a slot."""
        if not self._path.exists():
            return []
        lines = [json.loads(text) for text in self._path.read_text(encoding="utf-8").splitlines() if text.strip()]
        if not all(isinstance(line, dict) and "slot" in line for line in lines):
            raise ValueError("a line of the token journal is not a slot's")
        return lines

    def _append(self, line: Mapping[str, Any]) -> None:
        """Append one line durably: the file, and every directory this append created and the one holding it."""
        directory = self._path.parent
        created: list[Path] = []
        missing = directory
        while not missing.exists():
            created.append(missing)
            missing = missing.parent
        new_file = not self._path.exists()
        directory.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if new_file:
            _fsync_directory(directory)
        for made in created:  # deepest first: each new directory's entry lives in its parent
            _fsync_directory(made.parent)

    def latest_expiry(self, slot: str) -> float | None:
        """Return the latest end journalled for a slot, or None when none was; raise if the journal is unreadable.

        That is the latest of the slot's exact lines and of its provisional lines that no exact line settled, each
        line before a revocation's start capped at that revocation's `settle_until` (`held_until` with no margin).
        """
        return self.held_until(slot, 0.0)

    def held_until(self, slot: str, margin_s: float) -> float | None:
        """Return when the slot's tokens surely no longer work, or None when it has no line; raise if unreadable.

        Each expiry counts with `margin_s` added (another clock's prediction), or as infinity when it is indeterminate.
        A revocation caps, at its `settle_until` and with no margin (an observation), only the lines before its start
        line, which it wrote before the first bump; a line after the start, a mint that may have ended after the bump
        with a token of the new generation, is never capped by it, whatever was or was not written after the cap. A
        revocation with no line of the slot before its start counts its own settle end. A revoked line with no start
        line before it, or with no finite settle end, makes the journal unreadable, which holds the slot.
        """
        lines = [line for line in self._lines() if line["slot"] == slot]
        revocations = _revocations(lines)
        settled = {
            line["mint"] for line in lines if _expiry_line(line) and not line.get("provisional") and "mint" in line
        }
        held: float | None = None
        for index, line in enumerate(lines):
            if line.get("plane") == REVOKING:
                continue
            if line.get("plane") == REVOKED:
                start, end = revocations[index]
                if any(earlier.get("plane") != REVOKING for earlier in lines[:start]):
                    continue  # it caps the lines before its start, and extends nothing
            elif line.get("indeterminate") is True:  # a dropped token of unknown expiry: held until cleared or capped
                end = math.inf
            elif line.get("provisional") is True and line["mint"] in settled:  # no mint id: unreadable
                continue
            else:
                end = float(line["expires_at"]) + margin_s
            caps = [settle for start, settle in revocations.values() if start > index]
            held = _later(held, min([end, *caps]))
        return held


def _expiry_line(line: Mapping[str, Any]) -> bool:
    """Whether a journal line records an expiry, not the start or the end of a revocation."""
    return line.get("plane") not in (REVOKING, REVOKED)


def _revocations(lines: list[dict[str, Any]]) -> dict[int, tuple[int, float]]:
    """Map the index of each of a slot's revoked lines to the index of its start line and its settle end; raise
    ValueError for a revoked line with no start line of the same name before it or with no finite settle end."""
    starts: dict[str, int] = {}
    found: dict[int, tuple[int, float]] = {}
    for index, line in enumerate(lines):
        if line.get("plane") == REVOKING:
            starts[str(line["revocation"])] = index
        elif line.get("plane") == REVOKED:
            settle = float(line["settle_until"])
            if not math.isfinite(settle):
                raise ValueError("a revocation line's settle end is not a finite time")
            start = starts.get(str(line.get("revocation")))
            if start is None:
                raise ValueError("a revocation line has no start line before it, so what it caps is unknown")
            found[index] = (start, settle)
    return found


def _later(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else max(a, b)


class SlotProbe:
    """The `ResourceProbe` for slots: a slot's resource is gone once its sandbox is gone and its tokens have ended, by
    their expiry plus the margin or by a confirmed revocation once it has settled."""

    def __init__(
        self,
        journal: TokenJournal,
        *,
        sandbox_gone: Callable[[str, Mapping[str, Any]], bool] | None = None,
        clock: Callable[[], float] = time.time,
        margin_s: float = SAFETY_MARGIN_S,
    ) -> None:
        if isinstance(margin_s, bool) or not isinstance(margin_s, int | float) or not 0 <= margin_s < float("inf"):
            raise ValueError("the safety margin must be a finite number of seconds, zero or more")
        self._journal, self._sandbox_gone, self._clock, self._margin_s = journal, sandbox_gone, clock, margin_s

    def holder_gone(self, owner: str) -> bool | None:
        """Tell whether the process that held a slot is gone, as the core tells it."""
        return process_gone(owner)

    def resource_gone(self, key: str, note: Mapping[str, Any]) -> bool:
        """Return True only when the clock is past every token's journalled end plus the margin, or past the settle end
        of a revocation that caps it, and the sandbox probe, if any, agrees."""
        try:
            held = self._journal.held_until(key, self._margin_s)
        except (OSError, ValueError, KeyError, TypeError):
            return False  # an unreadable journal cannot show the tokens ended
        if held is not None and self._clock() <= held:
            return False
        return self._sandbox_gone is None or self._sandbox_gone(key, note) is True


class SlotTokens:
    """Mints the tokens of both planes for a leased slot, and journals when they end once they are minted.

    `events` is the platform's event sink; it receives `token_expiry_beyond_request` when a refused token's expiry is
    later than the one it was minted with.
    """

    def __init__(
        self,
        issuer: TokenIssuer,
        config: PlatformConfig,
        slots: Slots,
        journal: TokenJournal,
        *,
        events: "EventSink",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._issuer, self._config, self._slots, self._journal, self._clock = issuer, config, slots, journal, clock
        self._events = events

    def mint(
        self, lease: Lease, *, ceiling_s: float, condition: str | None = None, interface: str | None = "rest"
    ) -> RunTokens:
        """Mint the data path's token (none when `interface` is None: a run without a data path) and, for a condition
        with a tool server, the tool server's, on the slot's users; a run that needs neither takes no slot.

        A ceiling the window refuses (O06) mints and journals nothing. Otherwise the absolute expiry is computed once,
        journalled as a provisional line (durably), and both tokens are minted with exactly that expiry. A token whose
        examined expiry differs is refused and the expiry it has is journalled. When both pass, the exact line settles
        the provisional one before the tokens are returned. Any other failure leaves the provisional line.

        A slot whose tool-server user declares other rights than the condition's is refused before anything is
        journalled or minted (O31): its user would fail the privilege check, and the run leased the wrong group.
        """
        slot = self._slots.users_of(lease)
        if interface is None and condition is None:
            raise ValueError("a run with neither a data path nor a tool server takes no slot and mints no token")
        if condition is not None and slot.rights != tuple(sorted(self._config.condition(condition).rights)):
            raise ValueError(
                f"{self._slots.label(slot.name)}: its tool-server user declares other rights than "
                f"[toolserver] {self._config.condition_label(condition)}; lease a slot of the condition's group"
            )
        window = token_window(ceiling_s, self._config.tokens)
        mint = secrets.token_hex(8)
        valid_until = int(self._clock()) + window.mint_ttl_s
        self._journal.record(slot.name, "provisional", valid_until, mint=mint, provisional=True)
        try:
            tokens = self._issuer.mint_run_tokens(
                data=slot.data_path if interface is not None else None,
                ceiling_s=ceiling_s,
                interface=interface or "rest",
                tool_server=(slot.tool_server, condition) if condition is not None else None,
                valid_until=valid_until,
            )
        except DroppedToken as refused:  # a token exists, dropped: the slot is held until it ends or is revoked
            if refused.expires_at is None:
                self._journal.record_indeterminate(slot.name)
            else:
                self._journal.record(slot.name, "refused", refused.expires_at)
                if refused.expires_at > valid_until:
                    self._beyond_request(self._slots.label(slot.name), valid_until, refused)
            raise
        latest = max((t for t in (tokens.data_path, tokens.tool_server) if t is not None), key=lambda t: t.expires_at)
        self._journal.record(slot.name, latest.plane.value, latest.expires_at, mint=mint)
        return tokens

    def revoke(self, key: str, tokens: RunTokens | None = None, *, settle_s: float | None = None) -> "Revoked":
        """Revoke every temporary token of the slot's two users, confirm it, and journal the settle end (C19).

        `tokens` are the values the run still holds, each of which must verify as revoked; without them the rise of
        both users' generations alone confirms it. `settle_s` is `[tokens] revocation_settle_s` unless given. Call it
        only when no mint of the slot is under way: its run has ended and no start thread of it runs. Raises, and
        journals no revoked line, when any step fails (`revocation.revoke_slot`).
        """
        from expcore_onedata.revocation import revoke_slot  # its own module; it needs this one's journal

        wait = self._config.tokens.revocation_settle_s if settle_s is None else settle_s
        return revoke_slot(self._issuer, self._slots.get(key), self._journal, tokens, settle_s=wait, clock=self._clock)

    def _beyond_request(self, label: str, requested: int, refused: DroppedToken) -> None:
        """Record that the platform gave a token a later expiry than it was minted with: the slot by its position label
        and both expiries, never the slot's name or the token. A sink that fails raises the refusal from its error, so
        neither is lost."""
        examined = refused.expires_at
        shown = int(examined) if examined is not None and float(examined).is_integer() else examined
        detail = (
            f"{label}: a token minted to expire at {requested} examined as expiring at {shown}, later than asked;"
            " the platform disregarded the requested time caveat"
        )
        try:
            self._events(EVENT_EXPIRY_BEYOND_REQUEST, detail)
        except Exception as sink_error:  # the refusal stands whether or not the event could be written
            raise refused from sink_error
