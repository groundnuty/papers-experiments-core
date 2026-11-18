# Provenance: new
"""`python -m expcore.campaign.elastic`: an operator's (or an agent's) view and lever on an elastic campaign's ledger.

- `create LEDGER --study DIR --hard-term-s N [--prior-run-s N] [--max-usd N]`: make a campaign's ledger from its study:
  one run per planned (task, model, condition) in the seeded order, the fingerprint a federation joins with, the
  study's attempt limit and one lease grant per model (`lease_grant_usd`), a lease's hard term (the study does not state
  the platform's reset and deferral bounds, so it is given: `hard_term_s`) and, with `--max-usd`, the dollar budget.
  `DIR` is the study as a federation runs it (its `platform.toml` beside `study.toml`, where the study has one). It
  refuses a ledger that exists; the ledger is closed when it is made, so a controller can open it.
- `status LEDGER`: the ceiling, what is committed, left and locked in the grants of unsettled leases (in flight,
  expired, lost), each model's own ceiling, and the history of raises.
- `raise-ceiling LEDGER --usd N --reason TEXT --actor NAME [--model M]`: raise the campaign's dollar ceiling (or model
  M's) to N dollars, rounded down to a micro-dollar. It is never lowered or set below what is committed.

`status` and `raise-ceiling` work on the ledger file under its own write lock and not the dispatcher's, so they run
while a dispatcher holds the ledger, and the dispatcher's next lease reads a raise. Exit status: 0 done, 1 refused by
the ledger (or, for `create`, by the study: not loadable, or code that is not known), 2 a usage error or a file that is
not a ledger.
"""

import argparse
import sqlite3
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from expcore.campaign.elastic.ceilings import raise_in_file
from expcore.campaign.elastic.config import MICRO, LedgerConfig, lease_grant_usd, ledger_runs, micro_down
from expcore.campaign.elastic.ledger import Ledger
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


def create_ledger(
    path: Path, study: Path, *, hard_term_s: float, prior_run_s: float | None = None, max_usd: float | None = None
) -> LedgerConfig:
    """Make the ledger of the study at `study` at `path` (the module's docstring); the configuration pinned in it.
    Raises `LedgerError` for a ledger that exists, and the study layer's own errors for a study that cannot be loaded.
    """
    from expcore.campaign.manifest import fingerprint_sha256
    from expcore.study.config import load_study
    from expcore.study.pipeline import campaign_fingerprint
    from expcore.study.selection_start import pricing_of

    if path.exists():
        raise LedgerError(f"{path} already holds a ledger: it is not made again")
    loaded = load_study(study)
    pricing = pricing_of(loaded)
    config = LedgerConfig(
        fingerprint=fingerprint_sha256(campaign_fingerprint(study)),
        hard_term_s=hard_term_s,
        prior_run_s=pricing.limits.time_cap_s / 2 if prior_run_s is None else prior_run_s,
        max_attempts=loaded.campaign.scheduler.max_attempts,
        max_usd=max_usd,
        grants_usd={model: lease_grant_usd(spec, pricing.limits) for model, spec in pricing.models.items()},
    )
    Ledger.create(path, ledger_runs(loaded.manifest, loaded.schema), config, clock=time.time).close()
    return config


def _create(args: argparse.Namespace) -> int:
    from expcore.config.loading import ConfigError
    from expcore.provenance.codestamp import CodeVersionUnknown

    max_usd = None if args.max_usd is None else float(_dollars(args.max_usd))
    try:
        config = create_ledger(
            args.ledger, args.study, hard_term_s=args.hard_term_s, prior_run_s=args.prior_run_s, max_usd=max_usd
        )
    except (ConfigError, CodeVersionUnknown) as error:
        raise LedgerError(f"the study cannot be planned: {error}") from error
    print(f"created {args.ledger}: fingerprint {config.fingerprint}, hard term {config.hard_term_s:g} s")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m expcore.campaign.elastic", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    creating = commands.add_parser("create", help="make a campaign's ledger from its study")
    creating.add_argument(
        "ledger", type=Path, help="the ledger file to make (out/ledger.sqlite); never one that exists"
    )
    creating.add_argument("--study", required=True, type=Path, help="the study directory, as a federation runs it")
    creating.add_argument("--hard-term-s", required=True, type=float, help="a lease's hard term in seconds")
    creating.add_argument(
        "--prior-run-s", type=float, help="a cell's expected lease time before any (default: half the run's time cap)"
    )
    creating.add_argument("--max-usd", help="the budget in dollars (default: none)")
    status = commands.add_parser("status", help="the money: ceiling, committed, remaining, locked, raises")
    status.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    raising = commands.add_parser("raise-ceiling", help="raise the campaign's or a model's dollar ceiling")
    raising.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    raising.add_argument("--usd", required=True, help="the new ceiling in dollars (above the current one)")
    raising.add_argument("--reason", required=True, help="why it is raised, recorded")
    raising.add_argument("--actor", required=True, help="who raises it (an operator or an agent), recorded")
    raising.add_argument("--model", help="raise this model's own ceiling instead of the campaign's")
    return parser


def _dollars(text: str) -> Decimal:
    """The operator's dollars exactly as typed (never through a float), finite and above zero."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise ValueError(f"--usd must be a number of dollars, got {text!r}") from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f"--usd must be a finite number of dollars above zero, got {text!r}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; print its result or its error, and return the exit status."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            return _create(args)
        if args.command == "status":
            reader = LedgerReader(args.ledger)
            try:
                print(format_status(reader.status()))
            finally:
                reader.close()
            return 0
        raised = raise_in_file(args.ledger, micro_down(_dollars(args.usd)), args.reason, args.actor, model=args.model)
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
