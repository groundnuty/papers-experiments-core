# Provenance: new
"""The operator's command for a slot held only because its tokens might still be used: revoke them (C19).

A run's launch revokes its slot's tokens when it ends (`sandbox/provider.py`). A slot quarantined by a run that ended
before that existed, or whose revocation failed, waits for its tokens' journalled expiry, which for a long run is a day.
This command runs the same revocation for one slot:

  python -m expcore_onedata.lease revoke --platform platform.toml --leases DIR --slot NAME

`--leases` is the directory of the campaign's lease files and token journal (`out/leases` for a study run by
`expcore`; each shard has its own). The command reads the slot by its name in `[slots]`, and then:

1. refuses a slot whose lock file is not in that directory (the wrong directory, or a slot never leased there);
2. takes the slot's lock, the one the campaign's lease pool holds a lease by, and holds it from before the first check
   until after the revoked line: while it does, no pool can lease the slot, reap a dead holder's key or free a
   quarantine, since each of those needs that lock, and no mint of the slot can run, since a mint runs only under a
   held lease. A lock that is busy is refused, whoever holds it: a run under way, or a campaign that runs and keeps a
   quarantined lease's lock (a pool frees a quarantine only by letting the lock go, at a moment the command cannot
   know, and the next run could then mint tokens this revocation would revoke). Stop that campaign first: its lock
   goes with its process, and the next campaign only looks at the key a moment at a time, so try again if it was
   looking;
3. refuses a slot whose sandbox is not shown gone (`SandboxProbe`: no container, network or mount of the lease);
4. revokes both users' temporary tokens, requires each generation to rise, and journals the revoked line with its
   settle end (`users.SlotTokens.revoke`); any failure journals no revoked line, so the expiry rule stands, and a line
   of the slot journalled after the revocation's start is never capped by it.

It never rewrites the lock's note, which still names the holder that died, so the next campaign's pool asks the probe
and frees the slot at its first look after the settle end: a dead holder's key is asked again every 30 s, and at each
sweep. The output names the slot by its position (`slots[i]`), never by its name or its users (O12). Exit codes: 0
revoked, 1 refused or failed, 2 a usage error.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import IO

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox.hostops import DockerHost, HostOps
from expcore_onedata.sandbox.provider import SandboxProbe
from expcore_onedata.tokens import RevocationUnconfirmed, TokenIssuer
from expcore_onedata.users import JOURNAL, SlotTokens, TokenJournal, load_slots


class Refused(Exception):
    """The command will not revoke the slot: its message says why, naming the slot by its position."""


@contextlib.contextmanager
def _held(lock: Path, label: str) -> Iterator[None]:
    """Hold the slot's lease lock, exclusively, for the body; refuse when any other holder has it, whoever it is."""
    handle: IO[str] = open(lock, "r+", encoding="utf-8")  # noqa: SIM115 -- held for the body, closed below
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(
                f"refused: a live process holds the lock of {label}, a run under way or a running campaign that keeps "
                "it quarantined; the command needs the slot to itself, so let the run end or stop that campaign first"
            ) from None
        except OSError as error:
            raise Refused(f"refused: the lock of {label} could not be taken ({type(error).__name__})") from None
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
    with _held(lock, label):
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
