# Provenance: new
"""`python -m expcore.campaign.elastic`: an operator's (or an agent's) view and lever on an elastic campaign's ledger.

- `status LEDGER`: the ceiling, what is committed, left and locked in the grants of unsettled leases (in flight,
  expired, lost), each model's own ceiling, and the history of raises.
- `raise-ceiling LEDGER --usd N --reason TEXT --actor NAME [--model M]`: raise the campaign's dollar ceiling (or model
  M's) to N dollars, rounded down to a micro-dollar. It is never lowered or set below what is committed.

Both work on the ledger file under its own write lock and not the dispatcher's, so they run while a dispatcher holds
the ledger, and the dispatcher's next lease reads a raise. Exit status: 0 done, 1 refused by the ledger, 2 a usage
error or a file that is not a ledger.
"""

import argparse
import math
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from expcore.campaign.elastic.ceilings import raise_in_file
from expcore.campaign.elastic.config import MICRO, micro_down
from expcore.campaign.elastic.records import LedgerError, Status
from expcore.campaign.elastic.views import LedgerReader


def usd(micro: int) -> str:
    """Whole micro-dollars as dollars, to the micro-dollar."""
    return f"{Decimal(micro) / MICRO:.6f} USD"


def format_status(status: Status) -> str:
    """The status as the lines `status` prints."""
    ceiling = "none (no budget)" if status.ceiling_micro is None else usd(status.ceiling_micro)
    lines = [f"ceiling: {ceiling}", f"committed: {usd(status.committed_micro)}"]
    if status.remaining_micro is not None:
        lines.append(f"remaining: {usd(status.remaining_micro)}")
    lines.append(f"locked in unsettled leases: {usd(status.locked_micro)}")
    lines += [f"  {state}: {usd(amount)}" for state, amount in sorted(status.locked_by_state.items())]
    lines += [f"  model {model}: {usd(amount)}" for model, amount in sorted(status.locked_by_model.items())]
    for model, model_ceiling in sorted(status.model_ceiling_micro.items()):
        left = usd(status.model_remaining_micro[model])
        lines.append(f"model {model}: ceiling {usd(model_ceiling)}, remaining {left}")
    if not status.ceilings:
        lines.append("ceiling history: no raise")
    else:
        lines.append("ceiling history:")
        for raised in status.ceilings:
            when = datetime.fromtimestamp(raised.at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            whose = "campaign" if raised.model is None else f"model {raised.model}"
            lines.append(
                f"  #{raised.ceiling_id} {when} {whose} {usd(raised.old_micro)[:-4]} -> {usd(raised.new_micro)}"
                f" by {raised.actor}: {raised.reason}"
            )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m expcore.campaign.elastic", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="the money: ceiling, committed, remaining, locked, raises")
    status.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    raising = commands.add_parser("raise-ceiling", help="raise the campaign's or a model's dollar ceiling")
    raising.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    raising.add_argument("--usd", type=float, required=True, help="the new ceiling in dollars (above the current one)")
    raising.add_argument("--reason", required=True, help="why it is raised, recorded")
    raising.add_argument("--actor", required=True, help="who raises it (an operator or an agent), recorded")
    raising.add_argument("--model", help="raise this model's own ceiling instead of the campaign's")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; print its result or its error, and return the exit status."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            reader = LedgerReader(args.ledger)
            try:
                print(format_status(reader.status()))
            finally:
                reader.close()
            return 0
        if not math.isfinite(args.usd) or args.usd <= 0:
            raise ValueError(f"--usd must be a finite number of dollars above zero, got {args.usd!r}")
        raised = raise_in_file(args.ledger, micro_down(args.usd), args.reason, args.actor, model=args.model)
    except LedgerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    whose = "the campaign's ceiling" if raised.model is None else f"model {raised.model}'s ceiling"
    print(f"raised {whose}: {usd(raised.old_micro)} -> {usd(raised.new_micro)} (raise #{raised.ceiling_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
