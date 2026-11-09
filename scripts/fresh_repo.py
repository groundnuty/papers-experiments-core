#!/usr/bin/env python3
"""The fresh-repository check: a new study repository depends on the core by commit and runs the example study.

    python3 scripts/fresh_repo.py --sha <40-character commit> [--python 3.12] [--workdir DIR]

It makes an empty git repository and a bare project, adds the core by commit with
`uv add git+https://github.com/groundnuty/papers-experiments-core@<sha>`, writes the example study the installed core
carries (`python -m expcore example study`), commits it, and runs it end to end: `run`, `check`, `analyze`, `report`,
then builds the replication package and runs the package's own `make check`. No file is copied from a checkout of
the core: everything the study runs comes from the installed package. A step that fails ends the check with its exit
status, after printing what it ran.

`--source` names another place to depend on (a local path or a file URL; with `--sha`, a commit of a local
repository); `--runner` and `--skip-install` run the study steps with an interpreter that already has the core; and
`--dry-run` prints the steps without running them. Those exist for the script's own tests, which cannot depend on an
unpushed commit. Standard library only, because it runs before any environment exists.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = "https://github.com/groundnuty/papers-experiments-core"
DISTRIBUTION = "papers-experiments-core"
STUDY = "study"
PACKAGE = "tiny-study-package"  # the name the example's package.toml gives its package
PROJECT_FILES = ("pyproject.toml", "uv.lock")
_REF = re.compile(r"[0-9a-f]{40}|v\d+\.\d+\.\d+")
# Variables that would point a command at another repository or environment than the fresh one.
_FOREIGN = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
)
_IDENTITY = ("-c", "user.name=fresh-study", "-c", "user.email=fresh-study@example.invalid")
_NO_SIGNING_OR_HOOKS = ("-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null")
_UNPACK = "import sys, tarfile; tarfile.open(sys.argv[1]).extractall(sys.argv[2], filter='data')"


@dataclass(frozen=True)
class Step:
    """One command of the check, run in the fresh repository."""

    what: str
    argv: tuple[str, ...]


def dependency(sha: str | None, source: str | None = None) -> str:
    """The requirement `uv add` gets: the core at a commit (or tag) of its repository, or at a local place for tests."""
    if sha is not None and not _REF.fullmatch(sha):
        raise ValueError(f"not a full commit id or a vX.Y.Z tag: {sha!r}")
    if source is None:
        if sha is None:
            raise ValueError("a commit is needed to depend on the core's repository")
        return f"{DISTRIBUTION} @ git+{REPOSITORY}@{sha}"
    url = source if "://" in source else Path(source).resolve().as_uri()
    return f"{DISTRIBUTION} @ git+{url}@{sha}" if sha else f"{DISTRIBUTION} @ {url}"


def install_steps(requirement: str, python: str) -> list[Step]:
    """Make the repository and its project, and add the core as its one dependency."""
    return [
        Step("make an empty git repository", ("git", "init", "--quiet")),
        Step(
            "make a bare project",
            ("uv", "init", "--bare", "--no-workspace", "--vcs", "none", "--name", "fresh-study", "--python", python),
        ),
        Step("depend on the core by commit", ("uv", "add", "--python", python, requirement)),
    ]


def study_steps(runner: Sequence[str], python_for_make: str, project: Sequence[str] = ()) -> list[Step]:
    """Write the example from the installed core, commit it, run it end to end, package it, check the package."""
    core = (*runner, "-m", "expcore")
    commit = ("git", *_IDENTITY, *_NO_SIGNING_OR_HOOKS, "commit", "--quiet", "-m", "the example study")
    return [
        Step("write the example study from the installed core", (*core, "example", STUDY)),
        Step("stage the study and the project", ("git", "add", "--", STUDY, *project)),
        Step("commit them, so the code identity is a clean commit", commit),
        Step("run the campaign", (*core, "run", STUDY)),
        Step("check the records", (*core, "check", f"{STUDY}/out/runs")),
        Step("analyse", (*core, "analyze", STUDY)),
        Step("report", (*core, "report", STUDY)),
        Step("build the replication package", (*core, "package", STUDY, "--out", "dist")),
        Step("unpack it", (*runner, "-c", _UNPACK, f"dist/{PACKAGE}.tar.gz", "unpacked")),
        Step(
            "run the package's make check", ("make", "-C", f"unpacked/{PACKAGE}", "check", f"PYTHON={python_for_make}")
        ),
    ]


def clean_environment(env: Mapping[str, str]) -> dict[str, str]:
    """The environment without what would point git or uv at another repository or environment."""
    return {key: value for key, value in env.items() if key not in _FOREIGN}


def run(steps: Sequence[Step], workdir: Path, env: Mapping[str, str]) -> int:
    """Run the steps in order in the work directory; return the first failing status, or 0."""
    for number, step in enumerate(steps, start=1):
        print(f"[{number}/{len(steps)}] {step.what}: {shlex.join(step.argv)}", flush=True)
        done = subprocess.run(step.argv, cwd=workdir, env=dict(env), check=False)
        if done.returncode != 0:
            print(f"failed: {step.what} (exit {done.returncode})", file=sys.stderr)
            return done.returncode
    report = workdir / STUDY / "out" / "report.md"
    if not report.is_file():
        print(f"failed: no report at {report}", file=sys.stderr)
        return 1
    print("the example study ran end to end from the installed core, and its package passed its own check")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sha", help="the commit (or vX.Y.Z tag) of the core to depend on")
    parser.add_argument("--source", help="depend on this local path or file URL instead of the core's repository")
    parser.add_argument("--python", default="3.12", help="the Python version of the fresh project")
    parser.add_argument("--workdir", type=Path, help="an empty directory to work in (default: a new temporary one)")
    parser.add_argument("--runner", help="run the study steps with this command instead of `uv run python`")
    parser.add_argument("--skip-install", action="store_true", help="make the git repository only; add no project")
    parser.add_argument("--dry-run", action="store_true", help="print the steps and run nothing")
    return parser


def plan(args: argparse.Namespace, workdir: Path) -> list[Step]:
    """The steps the arguments ask for, in order."""
    runner = shlex.split(args.runner) if args.runner else ["uv", "run", "python"]
    python_for_make = runner[0] if args.runner else str(workdir / ".venv" / "bin" / "python")
    if args.skip_install:
        return [Step("make an empty git repository", ("git", "init", "--quiet")), *study_steps(runner, python_for_make)]
    requirement = dependency(args.sha, args.source)
    return install_steps(requirement, args.python) + study_steps(runner, python_for_make, PROJECT_FILES)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.skip_install and not args.runner:
        print("--skip-install needs --runner: without the install no environment has the core", file=sys.stderr)
        return 2
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="fresh-study-"))
    try:
        steps = plan(args, workdir)
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    if args.dry_run:
        print("\n".join(shlex.join(step.argv) for step in steps))
        return 0
    workdir.mkdir(parents=True, exist_ok=True)
    if any(workdir.iterdir()):
        print(f"refused: {workdir} is not empty; the check needs a fresh repository", file=sys.stderr)
        return 2
    print(f"fresh study repository: {workdir}", flush=True)
    return run(steps, workdir, clean_environment(os.environ))


if __name__ == "__main__":
    sys.exit(main())
