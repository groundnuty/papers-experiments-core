# Provenance: new
"""The operator's command for a slot held only because its tokens might still be used: revoke them (C19).

A run's launch revokes its slot's tokens when it ends (`sandbox/provider.py`). A slot quarantined by a run that ended
before that existed, or whose revocation failed, waits for its tokens' journalled expiry, which for a long run is a day.
This command runs the same revocation for one slot:

  python -m expcore_onedata.lease revoke --platform platform.toml --leases DIR --slot NAME

`--leases` is the directory of the campaign's lease files and token journal (`out/leases` for a study run by
`expcore`; each shard has its own). The command reads the slot by its name in `[slots]`, and then:

1. refuses a slot whose lock file is not in that directory (the wrong directory, or a slot never leased there);
2. takes the slot's lock when it is free, and holds it until the end, so no run can lease the slot meanwhile; a lock
   that is busy is accepted only when the pool's quarantine log shows the slot's lease quarantined, by the holder its
   lock note names, and not freed since (a campaign that runs keeps a quarantined lease's lock); otherwise a run holds
   the slot and it is refused;
3. refuses a slot whose sandbox is not shown gone (`SandboxProbe`: no container, network or mount of the lease);
4. revokes both users' temporary tokens, requires each generation to rise, and journals the revoked line with its
   settle end (`users.SlotTokens.revoke`); any failure journals no revoked line, so the expiry rule stands, and a line
   of the slot journalled after the revocation's start is never capped by it.

It never rewrites the lock's note. The campaign's pool frees the slot at its first look after the settle end: a
quarantine is asked again every minute, and a dead holder's key at the next sweep. The output names the slot by its
position (`slots[i]`), never by its name or its users (O12). Exit codes: 0 revoked, 1 refused or failed, 2 a usage
error.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import IO, Any

from expcore.campaign.lease import QUARANTINE_LOG
from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox.hostops import DockerHost, HostOps
from expcore_onedata.sandbox.provider import SandboxProbe
from expcore_onedata.tokens import RevocationUnconfirmed, TokenIssuer
from expcore_onedata.users import JOURNAL, SlotTokens, TokenJournal, load_slots


class Refused(Exception):
    """The command will not revoke the slot: its message says why, naming the slot by its position."""


def _note(lock: Path) -> dict[str, Any]:
    """The lock file's note, or an empty one when it cannot be read."""
    try:
        note = json.loads(lock.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return note if isinstance(note, dict) else {}


def quarantined(directory: Path, key: str, note: dict[str, Any]) -> bool:
    """Whether the pool's log shows the key's lease quarantined and not freed since, held by the holder the lock's
    note names: its last event is a quarantine, by that holder's process, which began no later than the quarantine."""
    try:
        text = (directory / QUARANTINE_LOG).read_text(encoding="utf-8")
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (OSError, ValueError):
        return False
    mine = [e for e in events if isinstance(e, dict) and e.get("key") == key]
    if not mine or mine[-1].get("ev") != "q":
        return False
    last, since = mine[-1], note.get("since")
    numbers = isinstance(since, int | float) and isinstance(last.get("start"), int | float)
    return note.get("pid") == last.get("pid") and numbers and since <= last["start"]


@contextlib.contextmanager
def _held(lock: Path, key: str, label: str) -> Iterator[None]:
    """Hold the key's lock for the body when it is free; let a busy one through only for a quarantine; else refuse."""
    handle: IO[str] = open(lock, "r+", encoding="utf-8")  # noqa: SIM115 -- held for the body, closed below
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if not quarantined(lock.parent, key, _note(lock)):
                raise Refused(f"refused: a run holds {label}; revoke it only once its run has ended") from None
        yield
    finally:
        handle.close()


def revoke(
    platform: str,
    leases: Path,
    name: str,
    *,
    client: OnezoneClient | None,
    host: HostOps,
    clock: Callable[[], float],
) -> str:
    """Revoke one slot's tokens as the module's docstring says; return what to print, or raise `Refused`."""
    config = load_platform(platform)
    if config.sandbox is None:
        raise Refused("refused: the platform file has no [sandbox] section, so no sandbox can be shown gone")
    slots = load_slots(platform, config)
    try:
        slot = slots.get(name)
    except KeyError:
        raise Refused("refused: the platform file declares no slot of that name") from None
    label = slots.label(slot.name)
    lock = leases / f"{slot.name}.lock"
    if not lock.is_file() or not (leases / JOURNAL).is_file():
        raise Refused(f"refused: {label} has no lock file or no token journal in that directory")
    with _held(lock, slot.name, label):
        if SandboxProbe(host, config.sandbox).resource_gone(slot.name, {}) is not True:
            raise Refused(f"refused: the sandbox of {label} is not shown gone")
        issuer = TokenIssuer(client if client is not None else OnezoneClient(config), config, clock=clock)
        minter = SlotTokens(issuer, config, slots, TokenJournal(leases), events=_no_event, clock=clock)
        try:
            done = minter.revoke(slot.name)
        except (PlatformError, RevocationUnconfirmed, OSError, ValueError) as error:
            shown = str(error) if isinstance(error, PlatformError | RevocationUnconfirmed) else type(error).__name__
            raise Refused(
                f"failed: the tokens of {label} were not revoked ({shown}); no revoked line was written"
            ) from None
    free = datetime.datetime.fromtimestamp(done.settle_until, tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    planes = ", ".join(f"{plane} {before}->{after}" for plane, (before, after) in done.generations.items())
    return f"revoked {label} (generations {planes}); its tokens count as gone from {free}"


def _no_event(kind: str, detail: str) -> None:
    """The revocation records no event: what it did is the command's output and the journal's line."""


def main(
    argv: Sequence[str] | None = None,
    *,
    client: OnezoneClient | None = None,
    host: HostOps | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    """The command line; `client`, `host` and `clock` stand in for the federation, the real host and time (tests)."""
    parser = argparse.ArgumentParser(prog="python -m expcore_onedata.lease", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    revoking = commands.add_parser("revoke", help="revoke one slot's tokens, so its lease frees after the settle")
    revoking.add_argument("--platform", required=True, help="the platform.toml whose [slots] declares the slot")
    revoking.add_argument("--leases", required=True, help="the campaign's directory of lease files and token journal")
    revoking.add_argument("--slot", required=True, help="the slot's name in [slots]")
    args = parser.parse_args(argv)
    try:
        print(
            revoke(args.platform, Path(args.leases), args.slot, client=client, host=host or DockerHost(), clock=clock)
        )
    except Refused as refused:
        print(refused)
        return 1
    except ConfigError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
