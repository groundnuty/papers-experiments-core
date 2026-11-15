"""`run_study` records the settings the plugin gives, and a resume under other ones says so (C35).

Every campaign here is held by a budget too small for any run, so a start opens the store, records and returns at
once: what is under test is what the start does, and no model is run.
"""

import dataclasses
import json
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest
from record_support import no_secrets_in_the_environment

from expcore.campaign.reset_settings import CHANGED, META_KEY
from expcore.provenance.codestamp import write_stamp
from expcore.study import pipeline
from expcore.study.config import load_study
from expcore.study.pipeline import run_study

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
LOGGER = "expcore.campaign.reset_settings"
pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]

SETTINGS = {"placement.qos_scan": "listing", "placement.concurrency": 16}


@pytest.fixture
def study(tmp_path) -> Path:
    """A stamped copy of the example whose budget holds every run."""
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    text = (copy / "campaign.toml").read_text(encoding="utf-8").replace("max_usd = 5.0", "max_usd = 0.00001")
    (copy / "campaign.toml").write_text(text, encoding="utf-8")
    write_stamp(copy, "a" * 40, load_study(copy).scope)
    return copy


def supplying(monkeypatch, settings):
    """Make every plugin the pipeline loads give `settings` as its reset settings."""
    real = pipeline.load_plugin
    monkeypatch.setattr(
        pipeline, "load_plugin", lambda context: dataclasses.replace(real(context), reset_settings=settings)
    )


def rows(study: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(study / "out" / "state.sqlite")
    try:
        return [tuple(row) for row in connection.execute(sql)]
    finally:
        connection.close()


def test_c35_a_study_whose_plugin_gives_settings_has_them_recorded_and_a_resume_under_other_ones_warns(
    study, monkeypatch, caplog
):
    """C35: the plugin's `reset_settings` reach the store through the scheduler at the first start; the resume of the
    same study under a changed one is not refused, and says what changed."""
    supplying(monkeypatch, SETTINGS)
    run_study(study)
    [(text,)] = rows(study, f"SELECT value FROM meta WHERE key='{META_KEY}'")
    assert json.loads(text) == SETTINGS
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        run_study(study)
    assert [r for r in caplog.records if r.name == LOGGER] == []  # unchanged: quiet
    supplying(monkeypatch, {**SETTINGS, "placement.qos_scan": "summaries"})
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        summary = run_study(study)
    assert summary.statuses  # not refused: the campaign opened and ran as far as its budget let it
    [warning] = [r for r in caplog.records if r.name == LOGGER]
    assert 'placement.qos_scan: "listing" -> "summaries"' in warning.getMessage()
    [(detail,)] = rows(study, f"SELECT detail FROM events WHERE kind='{CHANGED}'")
    assert json.loads(detail)["changes"] == [{"key": "placement.qos_scan", "old": "listing", "new": "summaries"}]


def test_c35_a_study_whose_plugin_gives_none_has_a_store_without_a_record(study):
    """C35: the example's plugin gives none, and its store holds no record and no event of the kind."""
    run_study(study)
    assert rows(study, f"SELECT key FROM meta WHERE key='{META_KEY}'") == []
    assert rows(study, "SELECT kind FROM events WHERE kind LIKE 'reset_settings%'") == []
