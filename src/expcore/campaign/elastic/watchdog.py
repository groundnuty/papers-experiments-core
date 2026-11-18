# Provenance: new
"""The login watchdog of a site (#462 step 3, C44): when the orchestrator stops reaching the site, drain it.

Each dispatcher tick writes `contact.json` in the site's channel root with the login node's own clock
(`elastic.exchange`). `python -m expcore.campaign.elastic.watchdog --root DIR --until EPOCH` runs on the login node
and reads it every `CHECK_S`, on that same clock, after a listing of the root. Once no contact is newer than `STALE_S`
(30 minutes; counted from the watchdog's own start when there is none), it acts once and exits:

1. it writes `inbox/drain.json` in every federation's channel under the root, so each worker finishes the lease it
   runs, returns any lease not yet taken, writes `exited.json` and stops (`elastic.worker`);
2. it runs the command given with `--on-out`, once, with the federations' labels as its last arguments, within
   `--on-out-timeout-s`. The design note's step after the drain (collect each federation's evidence and take it down)
   is that command, which step 6 supplies; the watchdog cancels nothing and takes nothing down by itself.

It is bounded: it exits at `--until` (the last job's end plus KillWait: nothing of the campaign runs after it),
whether it acted or not. Exit 0: no action was needed; 3: it acted (a failure of its command is printed, still exit
3).

Live-unproven: that a process on the login node survives a closure of the login route has not been measured, and the
watchdog has run against a temp-dir site only.
"""

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from expcore.campaign.elastic import channel as ch

STALE_S = 1800.0
CHECK_S = 60.0
ON_OUT_TIMEOUT_S = 3600.0
EXIT_QUIET, EXIT_ACTED = 0, 3


class Watchdog:
    """One site's watchdog over its channel root."""

    def __init__(
        self,
        root: Path,
        clock: Callable[[], float] = time.time,
        stale_s: float = STALE_S,
        on_out: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.root = root
        self.clock = clock
        self.stale_s = stale_s
        self.on_out = on_out
        self.started = clock()
        self.acted = False

    def last_contact(self) -> float:
        """When the orchestrator last reached the site, on this host's clock; the watchdog's start when it never did."""
        try:
            record = ch.read_listed(self.root, ch.listing(self.root), ch.CONTACT)
        except OSError:
            record = None
        at = None if record is None or record.get("format") != ch.CONTACT_FORMAT else record.get("at")
        if isinstance(at, int | float) and not isinstance(at, bool):
            return max(float(at), self.started)
        return self.started

    def federations(self) -> list[str]:
        """The labels of the channels under the root."""
        try:
            names = ch.listing(self.root)
        except OSError:
            return []
        return sorted(name for name in names if ch.LABEL.fullmatch(name) and (self.root / name).is_dir())

    def check(self) -> bool:
        """Act once when the contact is stale; True once it has acted."""
        if self.acted:
            return True
        now = self.clock()
        quiet = now - self.last_contact()
        if quiet < self.stale_s:
            return False
        labels = self.federations()
        for label in labels:
            inbox = ch.federation_dir(self.root, label) / ch.INBOX
            inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
            ch.create_json(
                inbox / ch.DRAIN,
                {"format": ch.DRAIN_FORMAT, "reason": f"no contact from the orchestrator for {quiet:.0f} s", "at": now},
            )
        self.acted = True
        print(
            f"watchdog: no contact for {quiet:.0f} s: drained {', '.join(labels) or 'no federation'}", file=sys.stderr
        )
        if self.on_out is not None:
            self.on_out(labels)
        return True

    def serve(self, until: float, sleep: Callable[[float], None] = time.sleep, check_s: float = CHECK_S) -> int:
        while self.clock() < until:
            if self.check():
                return EXIT_ACTED
            sleep(check_s)
        return EXIT_QUIET


def command(argv: Sequence[str], timeout_s: float) -> Callable[[list[str]], None]:
    """The action after a drain: `argv` with the labels appended, run once within `timeout_s`."""

    def run(labels: list[str]) -> None:
        try:
            done = subprocess.run([*argv, *labels], check=False, timeout=timeout_s, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"watchdog: the command after the drain failed: {type(error).__name__}", file=sys.stderr)
            return
        if done.returncode != 0:
            print(f"watchdog: the command after the drain exited {done.returncode}", file=sys.stderr)

    return run


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m expcore.campaign.elastic.watchdog")
    parser.add_argument("--root", required=True, type=Path, help="the site's channel root")
    parser.add_argument("--until", required=True, type=float, help="seconds since the epoch: exit then, whatever")
    parser.add_argument("--stale-s", type=float, default=STALE_S, help="act after this long without a contact")
    parser.add_argument("--check-s", type=float, default=CHECK_S, help="seconds between two readings")
    parser.add_argument("--on-out-timeout-s", type=float, default=ON_OUT_TIMEOUT_S)
    parser.add_argument("--on-out", nargs=argparse.REMAINDER, default=[], help="the command run once after the drain")
    args = parser.parse_args(argv)
    action = command(args.on_out, args.on_out_timeout_s) if args.on_out else None
    return Watchdog(args.root, stale_s=args.stale_s, on_out=action).serve(args.until, check_s=args.check_s)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["STALE_S", "Watchdog", "command", "main"]
