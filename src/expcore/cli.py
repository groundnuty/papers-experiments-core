# Provenance: new
"""The command line: `python -m expcore run | check | analyze | report | package | package-check | example`.

A thin shell; the logic is in the packages.

    run STUDY       load the configuration, open or resume the campaign, run the scheduler; with `--shard i/N
                    [--shard-label L]` run shard i of N of the campaign on this host (one campaign on several hosts)
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

Exit status: 0 done, 1 refused (the message on stderr names the command, says why and what to do), 2 usage, 3 `run`
ended with runs still pending (held by a budget or a stop; the holds are printed). `run` counts what it dispatched in
attempts it began, of the campaign's runs: a run set aside and run again is one run of two attempts, and a run that got
no lease began none.
"""

import argparse
import hashlib
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from expcore.campaign.attempts import ResumeRefused
from expcore.campaign.factors import PlanError
from expcore.campaign.scheduler import CampaignLocked
from expcore.campaign.shard import ShardError, ShardSpec
from expcore.config.loading import ConfigError
from expcore.package.build import build_package, default_hashes, private_hashes, verify_package
from expcore.package.spec import PackageError
from expcore.provenance.codestamp import CodeVersionUnknown
from expcore.provenance.redact import QuietParser, Redactor, stopped
from expcore.provenance.redact import failure as shown_failure
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
    ShardError,
)
_GUARDED = frozenset({"package", "package-check"})  # commands whose every message goes through the redactor


_SHARD_TEXT = re.compile(r"\d+/\d+")


def _shard_text(text: str) -> str:
    """An `i/N` for `--shard`; anything else is a usage error that does not repeat what was typed."""
    if _SHARD_TEXT.fullmatch(text.strip()) is None:
        raise argparse.ArgumentTypeError("--shard takes i/N, such as 2/4")
    return text


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = QuietParser(prog="python -m expcore", description=__doc__.split("\n\n")[0])
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
        if name == "run":
            command.add_argument(
                "--shard",
                type=_shard_text,
                metavar="i/N",
                help="run shard i of N of the campaign on this host: its own store and out/, a fixed part of the plan "
                "chosen by pair, and its share of the caps and the budget (N at least 2)",
            )
            command.add_argument(
                "--shard-label",
                metavar="L",
                help="the shard's name in its export and in a merge: lower-case letters, digits, '.', '_' or '-', "
                "chosen by you and never a host name (default: shard-i)",
            )
        else:
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
    package.add_argument(
        "--hashes",
        type=Path,
        help="a study-token hash file to use in place of the core's public one: it replaces that list, it does not add "
        "to it (make one with: python -m expcore.provenance.study_tokens hash PLAIN)",
    )
    package.add_argument("--anonymise", type=Path, help="anonymisation list, kept outside the study and the output")
    package.add_argument(
        "--wheels", type=Path, help="a directory of wheels to vendor instead of packing installed ones"
    )
    package.add_argument(
        "--allow-uncommitted", action="store_true", help="let an anonymised package include files no commit holds"
    )
    package.add_argument(
        "--show-private-paths",
        action="store_true",
        help="after the build, print the path each hidden name stands for; for your own terminal only, never a log",
    )
    verify = commands.add_parser("package-check", help="package-check PACKAGE: verify an unpacked package")
    verify.add_argument("package", type=Path, help="the directory of an unpacked package")
    example = commands.add_parser("example", help="example DEST: write the example study into a new directory")
    example.add_argument("dest", type=Path, help="a directory that does not exist yet, or is empty")
    return parser


def _run(args: argparse.Namespace, crash_hook: Callable[[str, str], None] | None) -> int:
    shard = None if args.shard is None else ShardSpec.parse(args.shard, args.shard_label)
    summary = run_study(
        args.study, allow_dirty=args.allow_dirty, code_root=args.code_root, crash_hook=crash_hook, shard=shard
    )
    runs = sum(summary.statuses.values())
    statuses = dict(sorted(summary.statuses.items()))
    print(f"dispatched {summary.attempts} attempts of {runs} runs; runs by status: {statuses}")
    pending = summary.statuses.get("pending", 0)
    for subject, hold in sorted(summary.holds.items()):
        print(f"hold on {subject}: {hold.reason}")
    stopped = any(hold.kind == "harness_errors" for hold in summary.holds.values())
    if stopped and summary.harness_log is not None:
        print(f"dispatch stopped on harness errors; their tracebacks, redacted, are in {summary.harness_log}")
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
    hidden: dict[str, str] = {}
    archive = build_package(
        args.study,
        args.out,
        hashes=args.hashes,
        anonymise=args.anonymise,
        wheels=args.wheels,
        allow_uncommitted=args.allow_uncommitted,
        hidden=hidden,
    )
    print(f"wrote {archive.name} (sha256 {hashlib.sha256(archive.read_bytes()).hexdigest()}) in the --out directory")
    if args.show_private_paths:
        _show_private_paths(hidden)
    return EXIT_OK


def _package_check(args: argparse.Namespace) -> int:
    """Verify an unpacked package. Every problem, and any exception, goes through a redactor of the core's public
    study-token list, the only list a reader has; without it the command refuses before reading anything."""
    sys.dont_write_bytecode = True  # importing the study's plugin must not add files to the package
    redactor = Redactor(private_hashes(default_hashes(), given=False))
    try:
        problems = verify_package(args.package, redactor=redactor)
    except (KeyboardInterrupt, SystemExit) as stop:  # a study's plugin, run during the regeneration, can raise either
        return stopped(stop, redactor, prefix=f"{args.command} refused: ")
    except Exception as error:
        print(f"{args.command} refused: {shown_failure(error, redactor, expected=_REFUSALS)}", file=sys.stderr)
        return EXIT_REFUSED
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return EXIT_REFUSED
    print("every file matches SHA256SUMS, and every output regenerates from the records byte for byte")
    return EXIT_OK


def _example(args: argparse.Namespace) -> int:
    written = write_example(args.dest)
    print(
        f"wrote the example study ({len(written)} files) to {args.dest}; "
        f"commit it, then run it: python -m expcore run {args.dest}"
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None, *, crash_hook: Callable[[str, str], None] | None = None) -> int:
    """Run one command; return its exit status. `crash_hook` is for tests that kill `run` at a chosen point."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "run" and args.shard is None and args.shard_label is not None:
            parser.error("--shard-label needs --shard")
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
        print(f"{args.command} refused: {error}", file=sys.stderr)
        if getattr(args, "show_private_paths", False):
            _show_private_paths(getattr(error, "private", {}))
        return EXIT_REFUSED
    except (KeyboardInterrupt, SystemExit) as stop:  # the build turns an exit with a message into a refusal
        if args.command not in _GUARDED:
            raise
        return stopped(stop, None, prefix=f"{args.command} refused: ")
    except Exception as error:  # the package commands' last guard
        if args.command not in _GUARDED:
            raise
        # The build and the check turn everything after their lists load into a refusal that went through the
        # redactor; what reaches here came before, when nothing could hide a name in its message.
        print(f"{args.command} refused: {shown_failure(error, None)}", file=sys.stderr)
        return EXIT_REFUSED


def _show_private_paths(private: Mapping[str, str]) -> None:
    """Print the path each hidden name stands for. Only `--show-private-paths` asks for it: whether standard error is
    a terminal says nothing about who reads it (a recorded session reports a terminal), so the author decides."""
    if not private:
        return
    print("the paths the hidden names above stand for; for your own terminal only, never a log:", file=sys.stderr)
    for name, path in private.items():
        print(f"  {name} is {path}", file=sys.stderr)
