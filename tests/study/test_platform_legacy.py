"""`run_study` resumes a store that recorded the platform's half in its earlier form, and nothing else (C06).

A plugin may give `platform_legacy`, the earlier form of `platform` for the same settings (the Onedata layer's spaces by
their Onezone id, from before a space could declare its data). Every campaign here is held by a budget too small for
any run, so a start opens the store, checks and records its fingerprint and returns: what is under test is the start.
"""

import dataclasses
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from record_support import no_secrets_in_the_environment

from expcore.campaign.resume import FingerprintMismatch
from expcore.provenance.codestamp import write_stamp
from expcore.study import pipeline
from expcore.study.config import load_study
from expcore.study.pipeline import run_study

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]

EARLIER = {"tls": {"mode": "verify"}, "spaces": ["a" * 64, "b" * 64]}  # each space by its id-bearing digest
LATER = {"tls": {"mode": "verify"}, "spaces": ["c" * 64, "b" * 64]}  # the first declares its data


@pytest.fixture
def study(tmp_path) -> Path:
    """A stamped copy of the example whose budget holds every run."""
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    text = (copy / "campaign.toml").read_text(encoding="utf-8").replace("max_usd = 5.0", "max_usd = 0.00001")
    (copy / "campaign.toml").write_text(text, encoding="utf-8")
    write_stamp(copy, "a" * 40, load_study(copy).scope)
    return copy


def giving(monkeypatch, platform, legacy=None):
    """Make every plugin the pipeline loads give this platform half and, perhaps, its earlier form."""
    real = pipeline.load_plugin
    monkeypatch.setattr(
        pipeline,
        "load_plugin",
        lambda context: dataclasses.replace(real(context), platform=platform, platform_legacy=legacy),
    )


def recorded(study: Path) -> dict:
    connection = sqlite3.connect(study / "out" / "state.sqlite")
    try:
        return json.loads(connection.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()[0])
    finally:
        connection.close()


def test_c06_a_store_of_the_earlier_form_resumes_under_it_unchanged(study, monkeypatch, capsys):
    """C06: started before its spaces declared their data, the store recorded the earlier form; a start whose plugin
    gives the later form with that earlier one resumes it, says so, and leaves its fingerprint as it was."""
    with monkeypatch.context() as patch:
        giving(patch, EARLIER)
        run_study(study)
    before = recorded(study)
    assert before["platform"] == EARLIER
    giving(monkeypatch, LATER, EARLIER)
    run_study(study)
    assert recorded(study) == before
    assert "the store recorded its spaces in their earlier form" in capsys.readouterr().err


def test_c06_the_later_form_alone_or_another_earlier_one_is_refused(study, monkeypatch):
    """C06: the earlier form is accepted only as the plugin gives it and only when it is the store's whole platform
    half: the later form alone, or an earlier form that is not the store's, is another campaign."""
    with monkeypatch.context() as patch:
        giving(patch, EARLIER)
        run_study(study)
    for legacy in (None, {**EARLIER, "tls": {"mode": "insecure"}}):
        with monkeypatch.context() as patch:
            giving(patch, LATER, legacy)
            with pytest.raises(FingerprintMismatch, match=r"platform\.spaces"):
                run_study(study)
    assert recorded(study)["platform"] == EARLIER


def test_c06_a_store_of_the_later_form_resumes_as_any_store_does(study, monkeypatch, capsys):
    """C06: a store started with the later form resumes under it; the earlier form changes nothing and is not said."""
    giving(monkeypatch, LATER, EARLIER)
    run_study(study)
    run_study(study)
    assert recorded(study)["platform"] == LATER
    assert "earlier form" not in capsys.readouterr().err
