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
```

The leases are the core's (`expcore.campaign.lease`): the plugin hands the scheduler `lease_group` (the constant `GROUP`)
and `lease_keys` (`Slots.keys_of`), and the scheduler takes, holds and releases the key. `Slots.users_of(lease)` then says
which two users the run has.

**A slot is free again only when its tokens have ended.** No token is ever revoked, so a token minted for a slot stays
good until its lifetime ends, and a run that died may have left a sandbox using it. `SlotTokens.mint` fixes the end of
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

**The provisional bound assumes the platform honours the requested time caveat.** Caveats only restrict a token, so a
token minted with `validUntil` cannot outlive it unless the server disregards it. Every successful mint checks this, and a
token that breaks it is journalled with the expiry it has, or, when that cannot be read or the request was sent but had no
usable answer, as indeterminate, which holds the slot until an operator confirms the token has ended and removes the
line. What the bound does not cover is a server that both disregards the caveat and answers unusably while this host
cannot write the indeterminate line; treating every unfinished mint as indeterminate would close that, at the price of an
operator clearing a slot after every crash mid-mint, and the trade was made for availability.

Users are created by the operator, not by this layer: there is no unscoped user creation or deletion here. The privileges
a slot's users hold are checked, direct and effective, before every token is minted (`tokens.py`).
"""

import json
import math
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.lease import Lease, process_gone
from expcore.config.loading import ConfigError, Section, load_toml
from expcore_onedata.config import NAME, PlatformConfig
from expcore_onedata.credentials import Principal
from expcore_onedata.tokens import DroppedToken, RunTokens, TokenIssuer, token_window

GROUP = "platform_users"
JOURNAL = "tokens.jsonl"
SAFETY_MARGIN_S = 60.0  # seconds past a journalled end before a slot counts as free: clock skew and whole-second expiry


@dataclass(frozen=True)
class Slot:
    """One lease key and the two platform users it gives a run."""

    name: str
    data_path: Principal
    tool_server: Principal


@dataclass(frozen=True)
class Slots:
    """The declared slots, in the order of the file."""

    slots: tuple[Slot, ...]

    def keys_of(self, group: str) -> list[str]:
        """Return the lease keys of the group; refuse a group this layer does not declare, so a typo is not an empty pool."""
        if group != GROUP:
            raise ValueError(f"the platform declares the lease group {GROUP!r}, not {group!r}")
        return [slot.name for slot in self.slots]

    def get(self, key: str) -> Slot:
        """Return the slot a lease key names."""
        for slot in self.slots:
            if slot.name == key:
                return slot
        raise KeyError(f"no slot is named by the lease key {key!r}")

    def users_of(self, lease: Lease) -> Slot:
        """Return the slot a held lease gives; refuse a lease of another group or one that has been released."""
        if lease.group != GROUP or not lease.held:
            raise ValueError("the lease is not a held lease of the platform users")
        return self.get(lease.key)


def _principal(section: Section, key: str) -> Principal:
    """Read one user of a slot."""
    user = section.section(key)
    user.reject_unknown(("username", "user_id"))
    try:
        return Principal(user.string("username"), user.string("user_id"))
    except ValueError as error:
        raise user.fail(None, str(error)) from None


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
        slot = Slot(name, _principal(section, "data_path"), _principal(section, "toolserver"))
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
        removes this line, having made sure the token has ended."""
        self._append({"slot": slot, "plane": "refused", "indeterminate": True})

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

        That is the latest of the slot's exact lines and of its provisional lines that no exact line settled.
        """
        if not self._path.exists():
            return None
        exact: list[float] = []
        open_lines: dict[str, float] = {}
        settled: set[str] = set()
        for text in self._path.read_text(encoding="utf-8").splitlines():
            if not text.strip():
                continue
            line = json.loads(text)
            if line["slot"] != slot:
                continue
            if line.get("indeterminate") is True:
                return math.inf  # a dropped token of unknown expiry: never free until an operator clears it
            expires_at = float(line["expires_at"])
            if line.get("provisional") is True:
                open_lines[line["mint"]] = expires_at  # a provisional line without its mint id is unreadable
                continue
            exact.append(expires_at)
            if "mint" in line:
                settled.add(line["mint"])
        times = exact + [t for mint, t in open_lines.items() if mint not in settled]
        return max(times) if times else None


class SlotProbe:
    """The `ResourceProbe` for slots: a slot's resource is gone once its tokens have ended and its sandbox is gone."""

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
        """Return True only when the clock is past every token's journalled end plus the margin, and the sandbox probe,
        if any, agrees."""
        try:
            expiry = self._journal.latest_expiry(key)
        except (OSError, ValueError, KeyError):
            return False  # an unreadable journal cannot show the tokens ended
        if expiry is not None and self._clock() <= expiry + self._margin_s:
            return False
        return self._sandbox_gone is None or self._sandbox_gone(key, note) is True


class SlotTokens:
    """Mints the tokens of both planes for a leased slot, and journals when they end once they are minted."""

    def __init__(
        self,
        issuer: TokenIssuer,
        config: PlatformConfig,
        slots: Slots,
        journal: TokenJournal,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._issuer, self._config, self._slots, self._journal, self._clock = issuer, config, slots, journal, clock

    def mint(
        self, lease: Lease, *, ceiling_s: float, condition: str | None = None, interface: str = "rest"
    ) -> RunTokens:
        """Mint the data path's token and, for a condition with a tool server, the tool server's, on the slot's users.

        A ceiling the window refuses (O06) mints and journals nothing. Otherwise the absolute expiry is computed once,
        journalled as a provisional line (durably), and both tokens are minted with exactly that expiry. A token whose
        examined expiry differs is refused and the expiry it has is journalled. When both pass, the exact line settles
        the provisional one before the tokens are returned. Any other failure leaves the provisional line.
        """
        slot = self._slots.users_of(lease)
        window = token_window(ceiling_s, self._config.tokens)
        mint = secrets.token_hex(8)
        valid_until = int(self._clock()) + window.mint_ttl_s
        self._journal.record(slot.name, "provisional", valid_until, mint=mint, provisional=True)
        try:
            tokens = self._issuer.mint_run_tokens(
                data=slot.data_path,
                ceiling_s=ceiling_s,
                interface=interface,
                tool_server=(slot.tool_server, condition) if condition is not None else None,
                valid_until=valid_until,
            )
        except DroppedToken as refused:  # a token exists, dropped but not revoked: the slot is held until it ends
            if refused.expires_at is None:
                self._journal.record_indeterminate(slot.name)
            else:
                self._journal.record(slot.name, "refused", refused.expires_at)
            raise
        latest = max((t for t in (tokens.data_path, tokens.tool_server) if t is not None), key=lambda t: t.expires_at)
        self._journal.record(slot.name, latest.plane.value, latest.expires_at, mint=mint)
        return tokens
