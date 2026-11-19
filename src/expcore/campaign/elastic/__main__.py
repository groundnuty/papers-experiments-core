# Provenance: new
"""`python -m expcore.campaign.elastic`: an operator's (or an agent's) view and lever on an elastic campaign's ledger.

- `create LEDGER --study DIR --hard-term-s N [--prior-run-s N] [--max-usd N] [--cap MODEL=N]...`: make a campaign's
  ledger from its study: one run per planned (task, model, condition) in the seeded order, the fingerprint a federation
  joins with, the study's attempt limit and one lease grant per model (`lease_grant_usd`), a lease's hard term (the
  study does not state the platform's reset and deferral bounds, so it is given: `hard_term_s`) and, with `--max-usd`,
  the dollar budget.
  `DIR` is the study as a federation runs it (its `platform.toml` beside `study.toml`, where the study has one). It
  refuses a ledger that exists; the ledger is closed when it is made, so a controller can open it. Each `--cap` is a
  model's starting cap on the leases it holds at once (a whole number from 1; a model not named has none), and the
  model must be one the study plans.
- `status LEDGER`: the ceiling, what is committed, left and locked in the grants of unsettled leases (in flight,
  expired, lost), each model's own ceiling, and the history of raises; each model's cap in force and the history of
  its changes.
- `raise-ceiling LEDGER --usd N --reason TEXT --actor NAME [--model M]`: raise the campaign's dollar ceiling (or model
  M's) to N dollars, rounded down to a micro-dollar. It is never lowered or set below what is committed.
- `set-cap LEDGER --model M --cap N --reason TEXT --actor NAME`: set model M's cap on its leases in flight or held
  back to N (a whole number from 1), up or down. A lower cap revokes no lease: it only stops new ones until the model
  is under it. M must have runs in the ledger.

`status`, `raise-ceiling` and `set-cap` work on the ledger file under its own write lock and not the dispatcher's, so
they run while a dispatcher holds the ledger, and the dispatcher's next lease reads a raise or a cap. Exit status: 0
done, 1 refused by the ledger (or, for `create`, by the study: not loadable, or code that is not known), 2 a usage
error or a file that is not a ledger.
"""

import argparse
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from expcore.campaign.elastic.caps import set_in_file
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
    lines += [f"model {model}: cap {cap}" for model, cap in sorted(status.caps.items())]
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
    if not status.cap_changes:
        lines.append("cap history: no change")
    else:
        lines.append("cap history:")
        for changed in status.cap_changes:
            when = datetime.fromtimestamp(changed.at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            before = "none" if changed.old is None else changed.old
            lines.append(
                f"  #{changed.cap_id} {when} model {changed.model} {before} -> {changed.new}"
                f" by {changed.actor}: {changed.reason}"
            )
    return "\n".join(lines)


def create_ledger(
    path: Path,
    study: Path,
    *,
    hard_term_s: float,
    prior_run_s: float | None = None,
    max_usd: float | None = None,
    caps: Mapping[str, int] | None = None,
) -> LedgerConfig:
    """Make the ledger of the study at `study` at `path` (the module's docstring); the configuration pinned in it.
    Raises `LedgerError` for a ledger that exists or a cap on a model the study does not plan, and the study layer's
    own errors for a study that cannot be loaded."""
    from expcore.campaign.manifest import fingerprint_sha256
    from expcore.study.config import load_study
    from expcore.study.pipeline import campaign_fingerprint
    from expcore.study.selection_start import pricing_of

    if path.exists():
        raise LedgerError(f"{path} already holds a ledger: it is not made again")
    loaded = load_study(study)
    pricing = pricing_of(loaded)
    runs = ledger_runs(loaded.manifest, loaded.schema)
    unplanned = sorted(set(caps or {}) - {run.model for run in runs})
    if unplanned:
        raise LedgerError(f"--cap names {', '.join(unplanned)}, which the study does not plan: a cap would never apply")
    config = LedgerConfig(
        fingerprint=fingerprint_sha256(campaign_fingerprint(study)),
        hard_term_s=hard_term_s,
        prior_run_s=pricing.limits.time_cap_s / 2 if prior_run_s is None else prior_run_s,
        max_attempts=loaded.campaign.scheduler.max_attempts,
        max_usd=max_usd,
        caps=caps or {},
        grants_usd={model: lease_grant_usd(spec, pricing.limits) for model, spec in pricing.models.items()},
    )
    Ledger.create(path, runs, config, clock=time.time).close()
    return config


def _create(args: argparse.Namespace) -> int:
    from expcore.config.loading import ConfigError
    from expcore.provenance.codestamp import CodeVersionUnknown

    max_usd = None if args.max_usd is None else float(_dollars(args.max_usd))
    caps = dict(args.cap)
    if len(caps) != len(args.cap):
        raise ValueError("--cap names a model more than once")
    try:
        config = create_ledger(
            args.ledger,
            args.study,
            hard_term_s=args.hard_term_s,
            prior_run_s=args.prior_run_s,
            max_usd=max_usd,
            caps=caps,
        )
    except (ConfigError, CodeVersionUnknown) as error:
        raise LedgerError(f"the study cannot be planned: {error}") from error
    capped = "".join(f", cap {model} {cap}" for model, cap in sorted(config.caps.items()))
    print(f"created {args.ledger}: fingerprint {config.fingerprint}, hard term {config.hard_term_s:g} s{capped}")
    return 0


def _status(args: argparse.Namespace) -> int:
    reader = LedgerReader(args.ledger)
    try:
        print(format_status(reader.status()))
    finally:
        reader.close()
    return 0


def _raise(args: argparse.Namespace) -> int:
    raised = raise_in_file(args.ledger, micro_down(_dollars(args.usd)), args.reason, args.actor, model=args.model)
    whose = "the campaign's ceiling" if raised.model is None else f"model {raised.model}'s ceiling"
    print(f"raised {whose}: {usd(raised.old_micro)} -> {usd(raised.new_micro)} (raise #{raised.ceiling_id})")
    return 0


def _set_cap(args: argparse.Namespace) -> int:
    changed = set_in_file(args.ledger, args.model, args.cap, args.reason, args.actor)
    before = "none" if changed.old is None else changed.old
    print(f"set model {changed.model}'s cap: {before} -> {changed.new} (cap change #{changed.cap_id})")
    return 0


def _cap_of(text: str) -> int:
    """A cap as typed: a whole number from 1."""
    try:
        value = int(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"a cap must be a whole number from 1, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"a cap must be a whole number from 1, got {text!r}")
    return value


def _model_cap(text: str) -> tuple[str, int]:
    """`MODEL=N` as typed."""
    model, _, number = text.rpartition("=")
    if not model:
        raise argparse.ArgumentTypeError(f"--cap is MODEL=N, got {text!r}")
    return model, _cap_of(number)


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
    creating.add_argument(
        "--cap",
        action="append",
        default=[],
        type=_model_cap,
        metavar="MODEL=N",
        help="a model's starting cap on the leases it holds at once (repeatable; default: no cap)",
    )
    status = commands.add_parser("status", help="the money: ceiling, committed, remaining, locked, raises")
    status.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    raising = commands.add_parser("raise-ceiling", help="raise the campaign's or a model's dollar ceiling")
    raising.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    raising.add_argument("--usd", required=True, help="the new ceiling in dollars (above the current one)")
    raising.add_argument("--reason", required=True, help="why it is raised, recorded")
    raising.add_argument("--actor", required=True, help="who raises it (an operator or an agent), recorded")
    raising.add_argument("--model", help="raise this model's own ceiling instead of the campaign's")
    capping = commands.add_parser("set-cap", help="set a model's cap on the leases it holds at once, up or down")
    capping.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    capping.add_argument("--model", required=True, help="the model whose cap is set (it must have runs in the ledger)")
    capping.add_argument("--cap", required=True, type=_cap_of, help="the new cap, a whole number from 1")
    capping.add_argument("--reason", required=True, help="why it is set, recorded")
    capping.add_argument("--actor", required=True, help="who sets it (an operator or an agent), recorded")
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
    commands = {"create": _create, "status": _status, "raise-ceiling": _raise, "set-cap": _set_cap}
    try:
        return commands[args.command](args)
    except LedgerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
