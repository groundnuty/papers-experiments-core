# Provenance: new
"""`python -m expcore.campaign.elastic`: an operator's (or an agent's) view and lever on an elastic campaign's ledger.

- `create LEDGER --study DIR --hard-term-s N [--prior-run-s N] [--max-usd N] [--cap MODEL=N]...
  [--topup-allowance MODEL=USD]...`: make a campaign's
  ledger from its study: one run per planned (task, model, condition) in the seeded order, the fingerprint a federation
  joins with, the study's attempt limit and one lease grant per model (`lease_grant_usd`), a lease's hard term (the
  study does not state the platform's reset and deferral bounds, so it is given: `hard_term_s`) and, with `--max-usd`,
  the dollar budget.
  `DIR` is the study as a federation runs it (its `platform.toml` beside `study.toml`, where the study has one). It
  refuses a ledger that exists; the ledger is closed when it is made, so a controller can open it. Each `--cap` is a
  model's starting cap on the leases it holds at once (a whole number from 1; a model not named has none), and the
  model must be one the study plans. Each `--topup-allowance` (C66) raises one model's lease grant by USD dollars (above
  0, no finer than a micro-dollar, which is what the ledger counts in) on top of its reservation and two admission
  probes. A lease's budget gate holds a lease whose spend and reservations pass its grant (a run pending again after an
  attempt that spent a reservation needs about two, and on a core that predates C64 so does a run in flight, counted as
  reserved and again as the next run); a grant of two reservations and the spend never holds one. It takes a model the
  study plans and prices, once, and needs `--max-usd` (without a budget a lease is granted nothing); the grant may not
  pass `--max-usd`. The allowance is recorded in a meta row of its own, nowhere in the fingerprint or the pinned
  configuration, and workers read the raised grant from the ledger. Each model's `accepted_fingerprints` in models.toml
  are its first declared fingerprints (C48), recorded with the actor `models.toml`; a model with none declares nothing
  yet.
- `status LEDGER`: the ceiling, what is committed, left and locked in the grants of unsettled leases (in flight,
  expired, lost), each model's own ceiling, and the history of raises; each model's cap in force and the history of
  its changes; each model's declared fingerprints, the models paused by an undeclared one seen, and the declaration
  log.
- `set-grant LEDGER MODEL=USD... --reason TEXT --actor NAME` (C66): raise models' lease grants to USD dollars each,
  on an existing ledger, for workers on a core that cannot be changed (they read the grant from the ledger). Only a
  raise (a grant not above the one in force is refused), only before any lease of the model has ever been issued (an
  issued lease's accounting never changes), and only if the caps still fit the budget (the sum of cap times grant over
  the capped models may not pass the campaign's ceiling). The raise is a meta row and an event of its own: the pinned
  configuration, the fingerprint and every earlier row stay as they were. It takes the dispatcher's lock, so it is
  refused while a controller holds the ledger; the controller that holds it next must run a core that reads the raise
  (this one or later). `status` shows the history.
- `raise-ceiling LEDGER --usd N --reason TEXT --actor NAME [--model M]`: raise the campaign's dollar ceiling (or model
  M's) to N dollars, rounded down to a micro-dollar. It is never lowered or set below what is committed.
- `set-cap LEDGER --model M --cap N --reason TEXT --actor NAME`: set model M's cap on its leases in flight or held
  back to N (a whole number from 1), up or down. A lower cap revokes no lease: it only stops new ones until the model
  is under it. M must have runs in the ledger.
- `declare LEDGER --model M (--fingerprint F | --none) --reason TEXT --actor NAME`: declare fingerprint F of model M,
  or "none" (M's replies that carry no fingerprint), in the append-only declaration log (C48). Nothing is ever removed
  or changed; a value already declared is refused, as is a model with no runs in the ledger. Each lease carries its
  model's declared set at its issue, and a model whose lease's run saw a value its set did not declare is paused (no
  new lease; leases out run to their end) until every value seen is declared. The campaign fingerprint never changes.

- `pause LEDGER --model M --reason TEXT --actor NAME`: stop new leases of model M at once (a provider's 429 or 5xx
  burst, a bad deployment, a key problem). Leases already out run on and settle as usual; M's pending runs wait, and no
  federation is sent ahead to a later set. Refused for a model with no runs in the ledger and for one an operator
  already paused. It is an append-only event (C55); nothing already in the ledger changes. Only a controller of this
  core honours it (an older one keeps leasing the model), so it is refused, writing nothing, while a live controller
  does not say it does (`awareness`), and written with a warning when no controller holds the ledger.
- `resume LEDGER --model M --reason TEXT --actor NAME`: lift an operator's pause of M. It lifts only that pause: a model
  that an undeclared fingerprint also pauses (C48) stays paused until `declare`, and `resume` says so. Refused for a
  model no operator paused. `status` shows each operator pause with its time, actor and reason, and the log of both.
- `drain LEDGER --label L --reason TEXT --actor NAME`: drain federation L (its label in the ledger, `<site>-<member>`)
  while the controller runs (C59): it takes no new lease from the dispatcher's next issue, a lease it holds runs to its
  end and counts, and the controller's next tick retires it once none is in flight. It is the ledger's own drain, with
  the reason and actor in its `drain` event. Refused for a federation the ledger does not hold, one already drained or
  proven ended, one past its recorded job end, and, writing nothing, while a live controller does not say it honours
  operator writes, as `pause` is.
- `reopen-run LEDGER --lease N --archive PATH --reason TEXT --actor NAME [--dry-run]`: put one run that a study-plugin
  defect ended as `harness_error` back to `pending`, with its evidence. An attempt of the run was set aside and charged
  nothing (an uncharged infrastructure failure: the model API refused the key, `key_rejected`, or the budget held the
  request, `budget_hold`); it ran again under the same attempt number, and the plugin's instrument failed at its start
  with a `LedgerError`, which the core treats as terminal. It is refused, writing nothing and naming the condition,
  unless: lease N is `counted`, the latest lease of its run, and charged nothing (its counted attempts and its
  completion's `charged` are 0; an earlier lease of the run may have charged); the run is `harness_error`; PATH (a
  plain `.tar`, or a `.tar.zst`, which is hashed as the tar it holds, read through the `zstd` program for at most ten
  minutes and 8 GiB, and must end as a tar does, with nothing but zero bytes after its last member) is a regular file,
  never a link, whose SHA-256 is the one lease N's completion names; its `out/state.sqlite` holds attempt rows of the
  run that are all uncharged, end with a `harness_error` whose detail starts with `LedgerError`, and hold before it an
  `infra_fail` of an uncharged kind with that last row's attempt number; no lease of the run is in flight or expired
  with its holder not proven ended; and the run has a charged attempt left (it is below `max_attempts`). The change is
  the run's state and one `operator_reopen` event (the run, the lease, the archive's hash, the attempt rows' summary,
  the reason and the actor): its charged count stays, so its next lease takes it as its `attempt_base`. The archive is
  read before the write lock is taken, and every ledger condition, the completion's hash and the controller's
  declaration are decided again under it. `--dry-run` decides every condition, again at its end, and writes nothing. A
  second call is refused (the run is no longer `harness_error`). Written as `pause` is, beside a running controller,
  and refused while one does not say it honours operator writes.
- `recover-run LEDGER --run RUN_KEY --charged N --source SEALED_FINAL --reason TEXT --actor NAME [--dry-run]`: put one
  lost run back to `pending` with the charged count N, on a working copy of a campaign's sealed final ledger (LEDGER;
  the final itself is never written), before the campaign's controller starts on the copy, so that it leases the run
  again and the lease's `attempt_base` is N. It is refused, writing nothing and naming the condition, unless: the run is
  in the ledger and is `infra_exhausted` or `harness_error`; no lease of it is in flight, `reported`, or expired with
  its holder not proven ended; every lease has a final count (it is counted, superseded, returned or cancelled; or it is
  lost and a settle gave its charged attempts, or the ledger has no budget; or it is expired with its holder proven
  ended and a settle gave its charged attempts: an overdue settle does not make a count final); N is a whole number from
  0 to below `max_attempts` and not above the run's charged count (the count is kept or lowered, never raised);
  SEALED_FINAL is a regular file, never a link, that is not LEDGER itself under any name (the same device and inode,
  decided before the ledger is opened), has no `-wal` with bytes in it beside it, is a ledger of the same schema with
  the same campaign fingerprint, and holds the run with the same state and charged count as LEDGER does now (its bytes
  are read once into a private temporary copy that is hashed and read, opened read-only and immutable, and its SHA-256
  is recorded; its path is looked at again under the write lock); and no controller holds LEDGER, a live one of any kind
  and a probe that cannot tell both refusing it. The change is the run's state, its charged count and its latest lease
  (set to NULL, so that a late completion of an earlier lease is superseded, not counted) and one `operator_recover`
  event (the run, its prior state, charged count and latest lease, N, the rule, its lease ids, the sealed final's hash,
  the reason and the actor): the rule is `declared` when N is the run's own count and `waiver` when N is below it. Every
  lease row stays as it was. `--dry-run` decides every condition, again at its end, and writes nothing. A second call is
  refused (the run is no longer lost).

`status`, `raise-ceiling`, `set-cap`, `declare`, `pause`, `resume`, `drain` and `reopen-run` work on the ledger file
under its own write lock and not the dispatcher's, so they run while a dispatcher holds the ledger, and the
dispatcher's next lease reads a raise, a cap, a declaration, a pause, a drain or a reopened run. `recover-run` writes
the same way but is refused while a controller holds the ledger: the controller that starts afterwards reads the
recovered run. Exit status: 0 done, 1 refused by the ledger (or, for `create`, by the study: not loadable, or code that
is not known), 2 a usage error or a file that is not a ledger.
"""

import argparse
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from expcore.campaign.elastic.allowances import allowances_micro, grants_usd, micro_of, model_allowance, model_dollars
from expcore.campaign.elastic.awareness import AWARE, NONE, OLDER, controller_of
from expcore.campaign.elastic.caps import set_in_file
from expcore.campaign.elastic.ceilings import raise_in_file
from expcore.campaign.elastic.config import MICRO, LedgerConfig, ledger_runs, micro_down
from expcore.campaign.elastic.declarations import declare_in_file
from expcore.campaign.elastic.drains import drain_in_file
from expcore.campaign.elastic.grants import set_in_file as set_grants_in_file
from expcore.campaign.elastic.ledger import Ledger
from expcore.campaign.elastic.pauses import pause_in_file, resume_in_file
from expcore.campaign.elastic.records import LedgerError, Status
from expcore.campaign.elastic.recoverrun import described as recovery_described
from expcore.campaign.elastic.recoverrun import recover_in_file
from expcore.campaign.elastic.reopenrun import described, reopen_in_file
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
    return "\n".join(lines + _declaration_lines(status) + _pause_lines(status) + _grant_lines(status))


def _value(fingerprint: str | None) -> str:
    return "none" if fingerprint is None else fingerprint


def _declaration_lines(status: Status) -> list[str]:
    """Each model's declared fingerprints, the paused models and the declaration log (C48); one line for a ledger made
    before the log."""
    if status.declared is None or status.declarations is None:
        return ["declarations: none kept (this ledger was made before the declaration log)"]
    lines = []
    for model, values in sorted(status.declared.items()):
        shown = ", ".join(_value(v) for v in values) if values else "nothing"
        lines.append(f"model {model}: declared {shown}")
    for model, values in sorted(status.paused.items()):
        lines.append(f"model {model}: paused: undeclared fingerprint(s) seen: {', '.join(_value(v) for v in values)}")
    if not status.declarations:
        return [*lines, "declaration history: none"]
    lines.append("declaration history:")
    for found in status.declarations:
        when = datetime.fromtimestamp(found.at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(
            f"  #{found.declaration_id} {when} model {found.model} {_value(found.fingerprint)} by {found.actor}: "
            f"{found.reason}"
        )
    return lines


def _pause_lines(status: Status) -> list[str]:
    """Each operator pause in force with its time, actor and reason, and the log of pauses and resumes (C55); nothing
    for a ledger no operator paused, so its status reads as it did."""
    lines = []
    for model, entry in sorted(status.operator_pauses.items()):
        lines.append(f"model {model}: paused by an operator since {_when(entry.at)} by {entry.actor}: {entry.reason}")
    if status.pause_log:
        lines.append("operator pause history:")
        lines += [
            f"  #{entry.event_seq} {_when(entry.at)} model {entry.model} {entry.kind} by {entry.actor}: {entry.reason}"
            for entry in status.pause_log
        ]
    return lines


def _grant_lines(status: Status) -> list[str]:
    """The log of raised lease grants (C66); nothing for a ledger no grant was raised on, so its status reads as
    it did."""
    if not status.grant_raises:
        return []
    return [
        "grant history:",
        *(
            f"  #{entry.event_seq} {_when(entry.at)} model {entry.model} {usd(entry.old_micro)} -> "
            f"{usd(entry.new_micro)} by {entry.actor}: {entry.reason}"
            for entry in status.grant_raises
        ),
    ]


def _when(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_ledger(
    path: Path,
    study: Path,
    *,
    hard_term_s: float,
    prior_run_s: float | None = None,
    max_usd: float | None = None,
    caps: Mapping[str, int] | None = None,
    topup_allowances: Mapping[str, Decimal] | None = None,
) -> LedgerConfig:
    """Make the ledger of the study at `study` at `path` (the module's docstring); the configuration pinned in it.
    Raises `LedgerError` for a ledger that exists, a cap or a top-up allowance on a model the study does not plan, or an
    allowance on a free one; the study layer's own errors for a study that cannot be loaded; and `CoreVersionUnknown`
    for a core whose commit is unknown or whose install is stale: the campaign fingerprint holds the core's identity,
    so a ledger made on it no run would serve. `topup_allowances` raise the named models' grants (C66; the
    module's docstring) and need `max_usd`."""
    from expcore.campaign.manifest import fingerprint_sha256
    from expcore.provenance.core_check import require_core
    from expcore.study.config import load_study
    from expcore.study.pipeline import campaign_fingerprint
    from expcore.study.selection_start import pricing_of

    if path.exists():
        raise LedgerError(f"{path} already holds a ledger: it is not made again")
    require_core(
        "elastic create", allow_dirty=True
    )  # an unknown core's fingerprint is no run's (a git checkout or a wheel)
    loaded = load_study(study)
    pricing = pricing_of(loaded)
    runs = ledger_runs(loaded.manifest, loaded.schema)
    unplanned = sorted(set(caps or {}) - {run.model for run in runs})
    if unplanned:
        raise LedgerError(f"--cap names {', '.join(unplanned)}, which the study does not plan: a cap would never apply")
    allowances = topup_allowances or {}
    unplanned = sorted(set(allowances) - {run.model for run in runs})
    if unplanned:
        raise LedgerError(
            f"--topup-allowance names {', '.join(unplanned)}, which the study does not plan: it would raise no grant"
        )
    config = LedgerConfig(
        fingerprint=fingerprint_sha256(campaign_fingerprint(study)),
        hard_term_s=hard_term_s,
        prior_run_s=pricing.limits.time_cap_s / 2 if prior_run_s is None else prior_run_s,
        max_attempts=loaded.campaign.scheduler.max_attempts,
        max_usd=max_usd,
        caps=caps or {},
        grants_usd=grants_usd(pricing.models, pricing.limits, allowances),
        platforms=loaded.campaign.platform_fingerprints,
    )
    declared = {model: spec.accepted_fingerprints for model, spec in pricing.models.items()}
    recorded = allowances_micro(allowances)
    Ledger.create(path, runs, config, clock=time.time, declared=declared, allowances_micro=recorded).close()
    return config


def _create(args: argparse.Namespace) -> int:
    from expcore.config.loading import ConfigError
    from expcore.provenance.codestamp import CodeVersionUnknown

    max_usd = None if args.max_usd is None else float(_dollars(args.max_usd))
    caps = dict(args.cap)
    if len(caps) != len(args.cap):
        raise ValueError("--cap names a model more than once")
    allowances = dict(args.topup_allowance)
    if len(allowances) != len(args.topup_allowance):
        raise ValueError("--topup-allowance names a model more than once")
    if allowances and max_usd is None:
        raise ValueError(
            "--topup-allowance needs --max-usd: without a budget a lease is granted nothing, so an allowance "
            "would never apply"
        )
    try:
        config = create_ledger(
            args.ledger,
            args.study,
            hard_term_s=args.hard_term_s,
            prior_run_s=args.prior_run_s,
            max_usd=max_usd,
            caps=caps,
            topup_allowances=allowances,
        )
    except (ConfigError, CodeVersionUnknown) as error:
        raise LedgerError(f"the study cannot be planned: {error}") from error
    capped = "".join(f", cap {model} {cap}" for model, cap in sorted(config.caps.items()))
    raised = "".join(
        f", top-up allowance {model} {usd(micro)}" for model, micro in allowances_micro(allowances).items()
    )
    print(
        f"created {args.ledger}: fingerprint {config.fingerprint}, hard term {config.hard_term_s:g} s{capped}{raised}"
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    reader = LedgerReader(args.ledger)
    try:
        status = reader.status()
        print(format_status(status))
        if status.operator_pauses and controller_of(args.ledger) == OLDER:
            print(
                "warning: the controller that holds this ledger does not say it honours operator pauses: it runs a "
                "core older than the pause and keeps leasing the paused model(s); restart it on this commit or later"
            )
        for platform, environment in sorted(reader.platform_environments().items()):  # C50: one pin per platform
            print(f"platform {platform}: environment {environment}")
    finally:
        reader.close()
    return 0


def _raise(args: argparse.Namespace) -> int:
    raised = raise_in_file(args.ledger, micro_down(_dollars(args.usd)), args.reason, args.actor, model=args.model)
    whose = "the campaign's ceiling" if raised.model is None else f"model {raised.model}'s ceiling"
    print(f"raised {whose}: {usd(raised.old_micro)} -> {usd(raised.new_micro)} (raise #{raised.ceiling_id})")
    return 0


def _set_grant(args: argparse.Namespace) -> int:
    models = [model for model, _ in args.grants]
    if len(set(models)) != len(models):
        raise ValueError("set-grant names a model more than once")
    wanted = {model: micro_of(dollars) for model, dollars in args.grants}
    for entry in set_grants_in_file(args.ledger, wanted, args.reason, args.actor):
        print(
            f"raised model {entry.model}'s grant: {usd(entry.old_micro)} -> {usd(entry.new_micro)} "
            f"(event #{entry.event_seq})"
        )
    print(
        "note: the controller that holds this ledger next must run this commit or later: a core that predates the "
        "raise ignores it and issues at the pinned grant"
    )
    return 0


def _set_cap(args: argparse.Namespace) -> int:
    changed = set_in_file(args.ledger, args.model, args.cap, args.reason, args.actor)
    before = "none" if changed.old is None else changed.old
    print(f"set model {changed.model}'s cap: {before} -> {changed.new} (cap change #{changed.cap_id})")
    return 0


def _declare(args: argparse.Namespace) -> int:
    fingerprint = None if args.none else args.fingerprint
    found, paused = declare_in_file(args.ledger, args.model, fingerprint, args.reason, args.actor)
    print(f"declared model {found.model}: {_value(found.fingerprint)} (declaration #{found.declaration_id})")
    if paused:
        print(f"model {found.model} stays paused: undeclared fingerprint(s) seen: {', '.join(map(_value, paused))}")
    try:  # the declaration is durable: a failed look for an operator's pause must not turn it into an error
        reader = LedgerReader(args.ledger)
        try:
            operator = reader.operator_pauses().get(found.model)
        finally:
            reader.close()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"note: the declaration is recorded; could not read whether an operator's pause also holds the model "
              f"({type(error).__name__})", file=sys.stderr)  # fmt: skip
        return 0
    if operator is not None:  # C55: declaring lifts only C48's pause
        print(f"model {found.model} stays paused by an operator: {operator.reason} (`resume` lifts it)")
    return 0


_NO_CONTROLLER = (
    "warning: no controller holds this ledger: the pause is written, but a controller older than this commit would "
    "ignore it, so whichever controller starts next must run this commit or later"
)
_UNKNOWN_CONTROLLER = (
    "warning: could not tell whether a controller holds this ledger: the pause is written, but a controller older "
    "than this commit ignores it and keeps leasing the model; restart any such controller on this commit or later"
)


def _pause(args: argparse.Namespace) -> int:
    entry, controller = pause_in_file(args.ledger, args.model, args.reason, args.actor)
    print(f"paused model {entry.model}: no new lease until an operator resumes it (event #{entry.event_seq})")
    if controller == AWARE:
        print("the controller that holds the ledger honours it from its next lease")
    else:
        print(_NO_CONTROLLER if controller == NONE else _UNKNOWN_CONTROLLER, file=sys.stderr)
    return 0


def _resume(args: argparse.Namespace) -> int:
    entry, undeclared = resume_in_file(args.ledger, args.model, args.reason, args.actor)
    print(f"resumed model {entry.model} (event #{entry.event_seq})")
    if undeclared:
        print(
            f"model {entry.model} stays paused: undeclared fingerprint(s) seen: {', '.join(map(_value, undeclared))}"
            " (declare them to lift it)"
        )
    return 0


def _drain(args: argparse.Namespace) -> int:
    event, controller = drain_in_file(args.ledger, args.label, args.reason, args.actor)
    print(f"drained federation {args.label}: no new lease; a lease it holds runs to its end, then the controller "
          f"retires it (event #{event})")  # fmt: skip
    if controller == AWARE:
        print("the controller that holds the ledger honours it from its next issue and tick")
    elif controller == NONE:
        print("no controller holds this ledger: the next one to open it reads the drain")
    else:
        print("warning: could not tell whether a controller holds this ledger: the drain is written; a controller "
              "older than the operator's pause would only read it at its next tick", file=sys.stderr)  # fmt: skip
    return 0


def _reopen_run(args: argparse.Namespace) -> int:
    done = reopen_in_file(args.ledger, args.lease, args.archive, args.reason, args.actor, dry_run=args.dry_run)
    for line in described(done):
        print(line)
    if done.event_seq is None:
        print("dry run: every condition holds, and nothing was written")
    elif done.controller == AWARE:
        print("the controller that holds the ledger leases the run from its next issue")
    elif done.controller == NONE:
        print("no controller holds this ledger: the next one to open it leases the run")
    else:
        print("warning: could not tell whether a controller holds this ledger: the run is pending; a controller "
              "older than the operator's pause would only read it at its next tick", file=sys.stderr)  # fmt: skip
    return 0


def _recover_run(args: argparse.Namespace) -> int:
    done = recover_in_file(
        args.ledger, args.run, args.charged, args.source, args.reason, args.actor, dry_run=args.dry_run
    )
    print(recovery_described(done))
    if done.event_seq is None:
        print("dry run: nothing written")
    return 0


def _lease_number(text: str) -> int:
    """A lease id as typed: a whole number from 1."""
    try:
        value = int(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"a lease is a whole number from 1, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"a lease is a whole number from 1, got {text!r}")
    return value


def _whole_number(text: str) -> int:
    """A charged count as typed: a whole number, of any sign (`recover-run` refuses what it may not set)."""
    try:
        return int(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"a charged count is a whole number, got {text!r}") from None


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


def _model_grant(text: str) -> tuple[str, Decimal]:
    """`MODEL=USD` of `set-grant` as typed."""
    return model_dollars("set-grant", text)


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
    creating.add_argument(
        "--topup-allowance",
        action="append",
        default=[],
        type=model_allowance,
        metavar="MODEL=USD",
        help="dollars added to one model's lease grant, on top of its reservation and two admission probes "
        "(repeatable, one per model; needs --max-usd; at most 6 decimals, the micro-dollar the ledger counts in; "
        "default: none)",
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
    granting = commands.add_parser("set-grant", help="raise models' lease grants before any lease of them is issued")
    granting.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    granting.add_argument(
        "grants",
        nargs="+",
        type=_model_grant,
        metavar="MODEL=USD",
        help="a model's new lease grant in dollars, above the one in force, at most 6 decimals (one per model)",
    )
    granting.add_argument("--reason", required=True, help="why it is raised, recorded")
    granting.add_argument("--actor", required=True, help="who raises it (an operator or an agent), recorded")
    declaring = commands.add_parser("declare", help="declare a model's fingerprint, or none, in the append-only log")
    declaring.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    declaring.add_argument("--model", required=True, help="the model (it must have runs in the ledger)")
    value = declaring.add_mutually_exclusive_group(required=True)
    value.add_argument("--fingerprint", help="the system fingerprint declared")
    value.add_argument("--none", action="store_true", help="declare the model's replies that carry no fingerprint")
    declaring.add_argument("--reason", required=True, help="why it is declared, recorded")
    declaring.add_argument("--actor", required=True, help="who declares it (an operator or an agent), recorded")
    for name, helps in (
        ("pause", "stop new leases of a model at once; leases out run on"),
        ("resume", "lift an operator's pause of a model (not C48's)"),
    ):
        pausing = commands.add_parser(name, help=helps)
        pausing.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
        pausing.add_argument("--model", required=True, help="the model (it must have runs in the ledger)")
        pausing.add_argument("--reason", required=True, help=f"why it is {name}d, recorded")
        pausing.add_argument("--actor", required=True, help=f"who {name}s it (an operator or an agent), recorded")
    draining = commands.add_parser("drain", help="drain one federation while the controller runs; its lease runs on")
    draining.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    draining.add_argument("--label", required=True, help="the federation's label in the ledger (<site>-<member>)")
    draining.add_argument("--reason", required=True, help="why it is drained, recorded")
    draining.add_argument("--actor", required=True, help="who drains it (an operator or an agent), recorded")
    reopening = commands.add_parser(
        "reopen-run", help="put a run a study-plugin defect ended as harness_error back to pending, with its evidence"
    )
    reopening.add_argument("ledger", type=Path, help="the ledger file (out/ledger.sqlite)")
    reopening.add_argument(
        "--lease", required=True, type=_lease_number, help="the run's latest lease, which is counted"
    )
    reopening.add_argument(
        "--archive",
        required=True,
        type=Path,
        help="that lease's archive: a .tar, or a .tar.zst (read as the tar it holds)",
    )
    reopening.add_argument("--reason", required=True, help="why it is reopened, recorded")
    reopening.add_argument("--actor", required=True, help="who reopens it (an operator or an agent), recorded")
    reopening.add_argument("--dry-run", action="store_true", help="decide every condition and write nothing")
    recovering = commands.add_parser(
        "recover-run",
        help="put a lost run (infra_exhausted or harness_error) back to pending with a charged count, on a working "
        "copy of a sealed final ledger",
    )
    recovering.add_argument("ledger", type=Path, help="the working copy of the ledger, never the sealed final")
    recovering.add_argument("--run", required=True, help="the run's key")
    recovering.add_argument(
        "--charged",
        required=True,
        type=_whole_number,
        help="the run's charged count afterwards (its next lease's attempt base): from 0 to below max_attempts, "
        "and not above its count now",
    )
    recovering.add_argument(
        "--source", required=True, type=Path, help="the sealed final ledger the working copy was made from (read-only)"
    )
    recovering.add_argument("--reason", required=True, help="why it is recovered, recorded")
    recovering.add_argument("--actor", required=True, help="who recovers it (an operator or an agent), recorded")
    recovering.add_argument("--dry-run", action="store_true", help="decide every condition and write nothing")
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
    commands = {
        "create": _create,
        "status": _status,
        "raise-ceiling": _raise,
        "set-cap": _set_cap,
        "set-grant": _set_grant,
        "declare": _declare,
        "pause": _pause,
        "resume": _resume,
        "drain": _drain,
        "reopen-run": _reopen_run,
        "recover-run": _recover_run,
    }
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
