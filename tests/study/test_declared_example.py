"""C40: the example study declares its secrets before it starts its instrument, so a failed start keeps its diagnostic.

A diagnostic is kept only when the executor told the core its secrets first (`RunRequest.declare_secrets`); one that
never declares has the diagnostic of its failed start withheld. The example is the pattern a study copies, so its
executor makes the call, with no argument: it mints and reads no secret beyond the environment's.

The test makes a copy of the example whose instrument fails to start on a run's first attempt, quoting a secret of the
environment, and runs a cut of it (one model): the stored attempt keeps the instrument's diagnostic with the secret
redacted, and the run goes through on its second attempt.
"""

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest
from record_support import no_secrets_in_the_environment
from study_support import HEAD, make_copy

from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.pipeline import run_built_study

pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]  # a fixture the tests below use, imported from the support module

SECRET = "example-token-value-0123456789abcdef"
START = "        self.before = self.shelf.served\n"
FAILING_START = f"""\
        if identity["attempt"] == 1:  # the first attempt of a run only: the second goes through
            raise InfraError(
                "sandbox",
                "the shelf would not open",
                diagnostic={{"checked": "the shelf", "seen": "the platform answered with {SECRET}"}},
            )
{START}"""


def failing_copy(base: Path) -> Path:
    """A stamped copy of the example whose instrument fails to start on a run's first attempt, quoting a secret."""
    copy = make_copy(base, "failing")
    plugin = copy / "plugin.py"
    text = plugin.read_text(encoding="utf-8")
    assert text.count(START) == 1, "the instrument's start is the one this test replaces"
    plugin.write_text(text.replace(START, FAILING_START), encoding="utf-8")
    write_stamp(copy, HEAD, load_study(copy).scope)  # plugin.py is in the code scope: the stamp names the edited file
    return copy


def first_attempts(root: Path) -> list[sqlite3.Row]:
    """Every attempt of the cut that was set aside (a failed start), in the order they ran."""
    connection = sqlite3.connect(root / "out" / "state.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute("SELECT * FROM attempts WHERE state = 'infra_fail' ORDER BY id").fetchall()
    finally:
        connection.close()


def test_c40_the_example_keeps_the_redacted_diagnostic_of_a_failed_instrument_start(tmp_path, monkeypatch):
    """C40: the example's executor declares before it starts its instrument, so the failed start's diagnostic is
    kept on the attempt's row with the instrument's name and the phase, and the secret the instrument quoted (a
    variable of the environment named like a secret) is redacted from it. The run is not lost: its second attempt
    goes through."""
    monkeypatch.setenv("EXAMPLE_API_TOKEN", SECRET)
    root = failing_copy(tmp_path)
    loaded = load_study(root)
    cut = dataclasses.replace(loaded, campaign=dataclasses.replace(loaded.campaign, models=("alpha-stub",)))
    summary = run_built_study(cut)
    assert summary.statuses.get("done", 0) > 0, summary.statuses
    rows = first_attempts(root)
    assert rows, "no attempt was set aside: the instrument's start never failed"
    for row in rows:
        kept = json.loads(row["diagnostic"])
        assert kept.get("error") != "Withheld", f"the example's executor never declared its secrets: {kept}"
        assert (kept["phase"], kept["instrument"]) == ("start", "facts"), kept
        assert kept["diagnostic"]["checked"] == "the shelf"
        assert "[redacted]" in kept["diagnostic"]["seen"]
        assert SECRET not in row["diagnostic"]
        assert (row["infra_kind"], row["charged"]) == ("sandbox", 1)
