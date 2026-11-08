# Provenance: new
"""The command line: `python -m expcore run | check | analyze | report`. A thin shell; the logic is in the packages.

    run STUDY       load the configuration, open or resume the campaign, run the scheduler
    check BATCH     the replay check over a batch of records (the study is found above the batch)
    analyze STUDY   refuse without a current replay check; then export, scores and the preregistered analysis
    report STUDY    the same refusal; then write out/report.md

Exit status: 0 done, 1 refused (the message on stderr says why and what to do), 2 usage, 3 `run` ended with runs still
pending (held by a budget or a stop; the holds are printed).
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from expcore.campaign.attempts import ResumeRefused
from expcore.campaign.factors import PlanError
from expcore.campaign.scheduler import CampaignLocked
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import CodeVersionUnknown
from expcore.replay.check import EXIT_OK, EXIT_REFUSED, EXIT_USAGE
from expcore.report import render_report
from expcore.study.pipeline import (
    REPORT_FILE,
    Refused,
    StudyAnalysis,
    analyse_study,
    check_batch,
    out_dir,
    run_study,
    write_analysis,
)

EXIT_PENDING = 3
_REFUSALS = (Refused, ConfigError, CodeVersionUnknown, ResumeRefused, CampaignLocked, PlanError)


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(prog="python -m expcore", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, target, text in (
        ("run", "study", "a study directory"),
        ("check", "batch", "a batch directory of records, normally STUDY/out/runs"),
        ("analyze", "study", "a study directory"),
        ("report", "study", "a study directory"),
    ):
        command = commands.add_parser(name, help=f"{name} {text}")
        command.add_argument(target, type=Path, help=text)
        command.add_argument("--allow-dirty", action="store_true", help="accept code with uncommitted changes")
        command.add_argument("--code-root", type=Path, help="where the code identity is read from (default: STUDY)")
        if name == "check":
            command.add_argument("--study", type=Path, help="the study directory (default: the one above the batch)")
    return parser


def _run(args: argparse.Namespace, crash_hook: Callable[[str, str], None] | None) -> int:
    summary = run_study(args.study, allow_dirty=args.allow_dirty, code_root=args.code_root, crash_hook=crash_hook)
    print(f"dispatched {summary.dispatched} runs; runs by status: {dict(sorted(summary.statuses.items()))}")
    pending = summary.statuses.get("pending", 0)
    for subject, hold in sorted(summary.holds.items()):
        print(f"hold on {subject}: {hold.reason}")
    if pending:
        print(f"{pending} runs are still pending; see the holds above", file=sys.stderr)
        return EXIT_PENDING
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    check = check_batch(args.batch, study_root=args.study, allow_dirty=args.allow_dirty, code_root=args.code_root)
    for failure in check["failures"]:
        print(f"FAIL {failure['file']} [{failure['stage']}] {failure['error']}")
    print(f"{check['n_ok']} of {check['n_records']} records passed")
    return EXIT_REFUSED if check["failures"] else EXIT_OK


def _analysis(args: argparse.Namespace) -> StudyAnalysis:
    return analyse_study(args.study, allow_dirty=args.allow_dirty, code_root=args.code_root)


def _analyze(args: argparse.Namespace) -> int:
    analysis = _analysis(args)
    for path in write_analysis(analysis):
        print(f"wrote {path}")
    for r in analysis.result.contrasts:
        print(f"{r.name}: estimate {r.estimate:.4g}, p {r.p_value:.4g}, {r.conclusion}")
    return EXIT_OK


def _report(args: argparse.Namespace) -> int:
    analysis = _analysis(args)
    write_analysis(analysis)
    text = render_report(
        analysis.result,
        analysis.export,
        study=analysis.study.name,
        code=analysis.code,
        core_version=analysis.core_version,
        interval_level=analysis.study.prereg.bootstrap.ci_level,
    )
    target = out_dir(analysis.study) / REPORT_FILE
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None, *, crash_hook: Callable[[str, str], None] | None = None) -> int:
    """Run one command; return its exit status. `crash_hook` is for tests that kill `run` at a chosen point."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:
        return EXIT_USAGE if stop.code else EXIT_OK
    try:
        if args.command == "run":
            return _run(args, crash_hook)
        return {"check": _check, "analyze": _analyze, "report": _report}[args.command](args)
    except _REFUSALS as error:
        print(f"refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
