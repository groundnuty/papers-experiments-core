"""Helpers for tests that run the example study through the command line: a stamped copy, the commands, a campaign."""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from record_support import SECRET_NAME, clear_registry

from expcore.cli import main
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "tiny_study"
HEAD = "a" * 40  # the stamped commit: one value for every copy, so every campaign has the same code identity


@dataclass
class Campaign:
    """One copy of the example, and what the pipeline did to it."""

    root: Path
    codes: dict[str, int] = field(default_factory=dict)
    report: str = ""

    @property
    def runs(self) -> Path:
        return self.root / "out" / "runs"

    def sql(self, query: str, *args: Any) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.root / "out" / "state.sqlite")
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(query, args).fetchall()
        finally:
            connection.close()

    def read(self, name: str) -> Any:
        return json.loads((self.root / "out" / name).read_text(encoding="utf-8"))

    def planned(self) -> list[tuple[str, str, str, str]]:
        """The (run key, condition, task, model) of every planned run."""
        schema = load_study(self.root).schema
        found = []
        for row in self.sql("SELECT run_key, factors FROM runs"):
            factors = json.loads(row["factors"])
            found.append((row["run_key"], schema.condition_named(factors), factors["task"], factors["model"]))
        return found


@pytest.fixture(scope="module")
def clean_environment():
    """No variable named like a secret: a short value would redact ordinary numbers of a record, as it should."""
    with pytest.MonkeyPatch.context() as patch:
        for name in [n for n in os.environ if SECRET_NAME.search(n) and not n.startswith("GIT_")]:
            patch.delenv(name)
        clear_registry()
        yield dict(os.environ)
        clear_registry()


def make_copy(base: Path, name: str) -> Path:
    """Copy the example to a directory of its own and stamp it, as a snapshot made without .git is."""
    copy = base / name
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    write_stamp(copy, HEAD, load_study(copy).scope)
    return copy


def copy_campaign(source: Campaign, base: Path, name: str) -> Campaign:
    """Copy a study with everything its campaign wrote, to change the copy without touching the original."""
    return Campaign(Path(shutil.copytree(source.root, base / name)))


def cli(env: dict[str, str], *argv: str, process: bool = False) -> tuple[int, str]:
    """Run a command and return its exit status and stderr, in this process or as `python -m expcore`."""
    if process:
        done = subprocess.run([sys.executable, "-m", "expcore", *argv], capture_output=True, text=True, env=env)
        return done.returncode, done.stderr
    errors = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
        return main(list(argv)), errors.getvalue()


def finish(env: dict[str, str], campaign: Campaign, *, process: tuple[str, ...] = ()) -> None:
    """Check, analyze and report; remember each command's exit status and the report.

    The commands named in `process` run as `python -m expcore`, which costs a start of the interpreter and of the
    vendor libraries the core imports; the others run in this process.
    """
    steps = {"check": str(campaign.runs), "analyze": str(campaign.root), "report": str(campaign.root)}
    for name, target in steps.items():
        campaign.codes[name], _ = cli(env, name, target, process=name in process)
    campaign.report = (campaign.root / "out" / "report.md").read_text(encoding="utf-8")
