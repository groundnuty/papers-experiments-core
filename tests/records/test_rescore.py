"""Re-scoring (R13): scorers run on saved records with no network, and the record keeps each scorer's version."""

import dataclasses
import json
import socket

import pytest
from replay_scenarios import SCENARIOS

from expcore.records.record import RunRecord, decode, load_record
from expcore.records.rescore import rescore, rescore_files, scorer_name, scorer_version
from expcore.records.schema import RecordError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Re-scoring needs no model and no platform: any attempt to open a connection fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("re-scoring opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class TurnCount:
    """A scorer that reads only the record."""

    name = "turns"
    version = "1.2"

    def score(self, record: RunRecord):
        return {
            "verdict": "fulfilled",
            "outcomes": {"turns": len(record.transcript), "rounds": record.totals["rounds"]},
        }


def untouched(record: RunRecord):
    """A scorer that is a plain function, with no declared version."""
    return {"verdict": "not_determined", "outcomes": {"bytes_moved": None}}


@pytest.fixture
def corpus(tmp_path):
    """Saved records, as a corpus to re-score: a finished attempt and one set aside."""
    root = tmp_path / "corpus"
    for name in ("tools", "infra_turn_three"):
        dataclasses.replace(SCENARIOS[name][0](), run_key=f"run-{name}").write(root)
    return root


def test_R13_a_record_is_re_scored_offline_and_the_scorers_version_is_recorded():
    """R13: the result and the version are in the record under the scorer's name, with no network."""
    record = SCENARIOS["tools"][0]()
    scored = rescore(record, [TurnCount()])
    assert scored.scores == {
        "turns": {"version": "1.2", "result": {"verdict": "fulfilled", "outcomes": {"turns": 6, "rounds": 6}}}
    }
    assert record.scores == {}
    assert RunRecord.from_json(decode(scored.dumps())) == scored


def test_R13_re_scoring_a_fixture_corpus_needs_no_network_and_records_the_version(corpus):
    """R13: every record of a saved corpus is re-scored in place; the infrastructure failure stays unscored."""
    written = rescore_files(corpus, [TurnCount()])
    assert len(written) == 2
    by_key = {r.run_key: r for r in (load_record(p) for p in written)}
    assert by_key["run-tools"].scores["turns"]["version"] == "1.2"
    assert by_key["run-infra_turn_three"].scores == {}


def test_R13_a_changed_scorer_re_scores_the_saved_records_and_the_new_version_replaces_the_old(corpus, tmp_path):
    """R13: scorer changes re-score saved records without model calls; the record names the version that scored it."""
    rescore_files(corpus, [TurnCount()])

    class Newer(TurnCount):
        version = "2.0"

        def score(self, record):
            return {"verdict": "violated", "outcomes": {"turns": None}}

    out = tmp_path / "rescored"
    paths = rescore_files(corpus, [Newer()], out_dir=out)
    scored = next(load_record(p) for p in paths if "tools" in load_record(p).run_key)
    assert scored.scores["turns"] == {"version": "2.0", "result": {"verdict": "violated", "outcomes": {"turns": None}}}
    stored = next(load_record(p) for p in sorted(corpus.glob("*.json")) if "tools" in load_record(p).run_key)
    assert stored.scores["turns"]["version"] == "1.2"


def test_R13_a_plain_function_is_versioned_by_a_hash_of_its_source():
    """R13: with no declared version (and no git to ask), the content hash of the scorer's file stands in."""
    version = scorer_version(untouched)
    assert version.startswith("sha256:")
    assert len(version) == len("sha256:") + 64
    assert scorer_name(untouched) == "untouched"
    scored = rescore(SCENARIOS["tools"][0](), [untouched])
    assert scored.scores["untouched"]["version"] == version
    assert scored.scores["untouched"]["result"]["outcomes"] == {"bytes_moved": None}
    assert json.loads(scored.dumps())["scores"]["untouched"]["result"]["outcomes"]["bytes_moved"] is None


def test_R13_the_entries_of_scorers_that_did_not_run_are_kept():
    first = rescore(SCENARIOS["tools"][0](), [TurnCount()])
    both = rescore(first, [untouched])
    assert sorted(both.scores) == ["turns", "untouched"]


def test_a_scorer_gets_a_copy_so_it_cannot_change_the_record():
    record = SCENARIOS["tools"][0]()
    before = record.dumps()

    def meddler(rec):
        rec.totals["rounds"] = 99
        rec.transcript.clear()
        return {"verdict": "fulfilled"}

    rescore(record, [meddler])
    assert record.dumps() == before


def test_R13_an_attempt_that_is_set_aside_is_never_scored():
    """R13: an infrastructure failure or a harness defect stays unscored, as the run left it."""
    for name in ("infra_turn_three", "tool_crash"):
        record = SCENARIOS[name][0]()
        assert rescore(record, [TurnCount()]) is record


def test_two_scorers_with_one_name_are_refused():
    with pytest.raises(RecordError, match="two scorers are named 'turns'"):
        rescore(SCENARIOS["tools"][0](), [TurnCount(), TurnCount()])


def test_a_scorer_without_a_name_is_refused():
    with pytest.raises(RecordError, match="has no name"):
        scorer_name(object())
