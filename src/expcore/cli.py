# Provenance: new
"""The command line: `python -m expcore run | check | analyze | report | package | package-check | example`.

A thin shell; the logic is in the packages.

    run STUDY       load the configuration, open or resume the campaign, run the scheduler
    check BATCH     the replay check over a batch of records (the study is found above the batch), with a copy of
                    the campaign store that the analysis reads instead of the live one
    analyze STUDY   refuse without a current replay check; then export, scores (derived from the records anew each time,
                    so this is also how a changed scorer is applied) and the preregistered analysis
    report STUDY    the same refusal; then write out/report.md
    package STUDY --out DIR
                    build the replication package the study's package.toml lists, as DIR/<name>.tar.gz
    package-check PACKAGE
                    what a package's `make check` runs: every file against SHA256SUMS, then the outputs derived anew
                    from the records and compared byte for byte
    example DEST    write the example study the core carries into DEST, a new directory, to run it from there

Exit status: 0 done, 1 refused (the message on stderr says why and what to do), 2 usage, 3 `run` ended with runs still
pending (held by a budget or a stop; the holds are printed).
"""

import argparse
import hashlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from expcore.campaign.attempts import ResumeRefused
from expcore.campaign.factors import PlanError
from expcore.campaign.scheduler import CampaignLocked
from expcore.config.loading import ConfigError
from expcore.package.build import build_package, verify_package
from expcore.package.spec import PackageError
from expcore.provenance.codestamp import CodeVersionUnknown
from expcore.replay.check import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, BatchNotVerified
from expcore.report import summary_lines
from expcore.study.example import ExampleError, write_example
from expcore.study.pipeline import (
    Refused,
    StudyAnalysis,
    analyse_study,
    check_batch,
    run_study,
    write_analysis,
    write_report,
)

EXIT_PENDING = 3
_REFUSALS = (
    Refused,
    ConfigError,
    CodeVersionUnknown,
    ResumeRefused,
    CampaignLocked,
    PlanError,
    BatchNotVerified,
    PackageError,
    ExampleError,
)


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
        if name != "run":
            command.add_argument(
                "--partial",
                action="store_true",
                help="accept a campaign with attempts still running, and leave out attempts no replayed record covers",
            )
        if name == "check":
            command.add_argument("--study", type=Path, help="the study directory (default: the one above the batch)")
    package = commands.add_parser("package", help="package STUDY: build the replication package package.toml lists")
    package.add_argument("study", type=Path, help="a study directory with a package.toml")
    package.add_argument("--out", type=Path, required=True, help="where the archive is written")
    package.add_argument("--hashes", type=Path, help="study-token hash file (default: the core's public one)")
    package.add_argument("--anonymise", type=Path, help="anonymisation list, kept outside the study and the output")
    package.add_argument(
        "--wheels", type=Path, help="a directory of wheels to vendor instead of packing installed ones"
    )
    package.add_argument(
        "--allow-uncommitted", action="store_true", help="let an anonymised package include files no commit holds"
    )
    verify = commands.add_parser("package-check", help="package-check PACKAGE: verify an unpacked package")
    verify.add_argument("package", type=Path, help="the directory of an unpacked package")
    example = commands.add_parser("example", help="example DEST: write the example study into a new directory")
    example.add_argument("dest", type=Path, help="a directory that does not exist yet, or is empty")
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
    check = check_batch(
        args.batch,
        study_root=args.study,
        allow_dirty=args.allow_dirty,
        code_root=args.code_root,
        partial=args.partial,
    )
    for failure in check["failures"]:
        print(f"FAIL {failure['file']} [{failure['stage']}] {failure['error']}")
    print(f"{check['n_ok']} of {check['n_records']} records passed")
    return EXIT_REFUSED if check["failures"] else EXIT_OK


def _analysis(args: argparse.Namespace) -> StudyAnalysis:
    return analyse_study(args.study, allow_dirty=args.allow_dirty, code_root=args.code_root, partial=args.partial)


def _analyze(args: argparse.Namespace) -> int:
    analysis = _analysis(args)
    for path in write_analysis(analysis):
        print(f"wrote {path}")
    print("\n".join(summary_lines(analysis.result)))
    return EXIT_OK


def _report(args: argparse.Namespace) -> int:
    analysis = _analysis(args)
    write_analysis(analysis)
    print(f"wrote {write_report(analysis)}")
    return EXIT_OK


def _package(args: argparse.Namespace) -> int:
    archive = build_package(
        args.study,
        args.out,
        hashes=args.hashes,
        anonymise=args.anonymise,
        wheels=args.wheels,
        allow_uncommitted=args.allow_uncommitted,
    )
    print(f"wrote {archive} (sha256 {hashlib.sha256(archive.read_bytes()).hexdigest()})")
    return EXIT_OK


def _package_check(args: argparse.Namespace) -> int:
    sys.dont_write_bytecode = True  # importing the study's plugin must not add files to the package
    problems = verify_package(args.package)
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return EXIT_REFUSED
    print("every file matches SHA256SUMS, and every output regenerates from the records byte for byte")
    return EXIT_OK


def _example(args: argparse.Namespace) -> int:
    written = write_example(args.dest)
    print(f"wrote the example study ({len(written)} files) to {args.dest}; run it: python -m expcore run {args.dest}")
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
        commands = {
            "check": _check,
            "analyze": _analyze,
            "report": _report,
            "package": _package,
            "package-check": _package_check,
            "example": _example,
        }
        return commands[args.command](args)
    except _REFUSALS as error:
        print(f"refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
