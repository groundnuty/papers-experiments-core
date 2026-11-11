"""Re-scoring (R13): scorers run on saved records with no network, and the record keeps each scorer's version; a scorer
that takes `side_files` gets the record's side files as verified bytes, never a path (R14)."""

import dataclasses
import hashlib
import json
import socket

import pytest
from replay_scenarios import SCENARIOS

from expcore.records import rescore as rescore_module
from expcore.records.record import RunRecord, decode, load_record
from expcore.records.rescore import rescore, rescore_files, scorer_name, scorer_version
from expcore.records.schema import RecordError
from expcore.replay import side_files as side_module
from expcore.replay.side_files import SideFile


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


def written_files(root):
    return {p.name: p.read_bytes() for p in sorted(root.iterdir())}


def test_R13_only_run_records_are_re_scored_a_check_and_a_manifest_are_left_alone(corpus):
    """R13: a file without the record schema marker is not a run record, whatever it is named."""
    (corpus / "replay-check.json").write_text(json.dumps({"schema": "replay-check/1", "inputs": {}}), encoding="utf-8")
    (corpus / "manifest.json").write_text(json.dumps({"cells": 3}), encoding="utf-8")
    (corpus / "list.json").write_text("[1, 2]", encoding="utf-8")
    (corpus / "notes.json").write_text("not json at all", encoding="utf-8")
    before = written_files(corpus)
    written = rescore_files(corpus, [TurnCount()])
    after = written_files(corpus)
    assert len(written) == 2
    for name in ("replay-check.json", "manifest.json", "list.json", "notes.json"):
        assert after[name] == before[name]


def test_R13_a_file_named_like_a_record_that_is_not_one_stops_the_run(corpus):
    """R13: a corrupt record or one without the marker is an error, not a file to skip."""
    name = "a" * 64 + ".1.json"
    (corpus / name).write_text("{truncated", encoding="utf-8")
    with pytest.raises(RecordError, match=name):
        rescore_files(corpus, [TurnCount()])
    (corpus / name).write_text("{}", encoding="utf-8")
    with pytest.raises(RecordError, match="holds no record schema marker"):
        rescore_files(corpus, [TurnCount()])


def test_R13_every_record_is_validated_and_scored_before_any_is_rewritten(corpus):
    """R13: two phases; a record that cannot be loaded leaves every file as it was."""
    path = next(p for p in corpus.glob("*.json") if json.loads(p.read_text(encoding="utf-8"))["run_key"] == "run-tools")
    broken = json.loads(path.read_text(encoding="utf-8"))
    broken["run_key"] = "run-broken"
    broken["outcome"]["name"] = "exploded"
    (corpus / ("b" * 64 + ".1.json")).write_text(json.dumps(broken), encoding="utf-8")
    before = written_files(corpus)
    with pytest.raises(RecordError, match="unknown outcome"):
        rescore_files(corpus, [TurnCount()])
    assert written_files(corpus) == before


def test_R13_a_scorer_that_fails_on_the_last_record_leaves_every_file_as_it_was(corpus):
    """R13: the failure comes before the first write, however late in the corpus the bad record is."""
    for name in ("malformed", "turn_cap"):
        dataclasses.replace(SCENARIOS[name][0](), run_key=f"run-{name}").write(corpus)
    before = written_files(corpus)

    def fussy(record):
        if record.run_key == "run-turn_cap":
            raise ValueError("cannot score this one")
        return {"verdict": "fulfilled"}

    with pytest.raises(ValueError, match="cannot score"):
        rescore_files(corpus, [fussy])
    assert written_files(corpus) == before


def test_R13_a_result_that_cannot_be_written_stops_the_run_before_any_write(corpus):
    """R13: a score of NaN cannot be written as JSON; it is found while the records are still unchanged."""
    before = written_files(corpus)

    def nan_scorer(record):
        return {"verdict": "fulfilled", "outcomes": {"share": float("nan")}}

    with pytest.raises(RecordError, match="cannot be written as JSON"):
        rescore_files(corpus, [nan_scorer])
    assert written_files(corpus) == before


def test_R13_a_treatment_attempt_is_scored_like_any_other_scored_attempt():
    """R13: adds that an infrastructure outcome of a declared kind is not set aside, so the scorers run on it."""
    record = SCENARIOS["infra_treatment"][0]()
    assert record.set_aside is False
    scored = rescore(record, [TurnCount()])
    assert scored is not record
    assert scored.scores["turns"]["result"]["outcomes"]["turns"] == len(record.transcript)
    assert scored.outcome == record.outcome


# --- the side files of a record --------------------------------------------------------------------------------------

LOGGED = SideFile("ranges_log.gz", "instruments.ranges.side_sha256")
CONTENT = b"\x1f\x8b an invented log of byte ranges"


class RangeBytes:
    """A scorer that takes the side files of the record: a measure computed from a side file's content."""

    name = "ranges"
    version = "1"

    def __init__(self) -> None:
        self.handed: list[dict[str, bytes]] = []

    def score(self, record: RunRecord, side_files):
        self.handed.append(dict(side_files))
        data = side_files.get(LOGGED.pattern)
        return {"outcomes": {"logged": None if data is None else float(len(data))}, "names": sorted(side_files)}


def logged_length(record, *, side_files):
    """A scorer that is a plain function and takes the side files as a keyword."""
    return {"outcomes": {"logged": float(sum(len(data) for data in side_files.values()))}}


def test_R13_a_scorer_that_takes_only_the_record_is_called_as_before_when_side_files_are_handed_over():
    """R13: a scorer without a `side_files` parameter is called with the record alone, whatever is handed over, and
    scores exactly as it did."""
    record = SCENARIOS["tools"][0]()
    before = rescore(record, [TurnCount(), untouched])
    after = rescore(record, [TurnCount(), untouched], side_files={LOGGED.pattern: CONTENT})
    assert after.scores == before.scores
    assert after.dumps() == before.dumps()


def test_R14_a_scorer_that_takes_side_files_gets_the_bytes_handed_over_keyed_by_pattern():
    """R14: the scorer gets each side file of the record by its declared pattern, the bytes as handed over, in a method
    or a plain function, positional or keyword-only."""
    scorer = RangeBytes()
    scored = rescore(SCENARIOS["tools"][0](), [scorer, logged_length], side_files={LOGGED.pattern: CONTENT})
    assert scorer.handed == [{LOGGED.pattern: CONTENT}]
    assert scored.scores["ranges"]["result"] == {"outcomes": {"logged": float(len(CONTENT))}, "names": [LOGGED.pattern]}
    assert scored.scores["logged_length"]["result"] == {"outcomes": {"logged": float(len(CONTENT))}}


def test_R14_a_record_without_a_side_file_gives_the_scorer_an_empty_mapping():
    """R14: a record that has no side file, or a study that declares none, hands the scorer an empty mapping."""
    scorer = RangeBytes()
    scored = rescore(SCENARIOS["tools"][0](), [scorer], side_files={})
    assert scorer.handed == [{}]
    assert scored.scores["ranges"]["result"] == {"outcomes": {"logged": None}, "names": []}


def test_R14_a_scorer_that_takes_side_files_is_refused_when_none_are_handed_over():
    """R14: a caller that hands over no side files at all leaves a scorer that takes them unrun, named, rather than
    giving it an empty mapping it would take for a record without one."""
    with pytest.raises(RecordError, match="scorer 'ranges' takes the side files of the record"):
        rescore(SCENARIOS["tools"][0](), [RangeBytes()])


def test_R14_the_side_files_a_scorer_gets_are_read_only_and_its_own():
    """R14: a scorer cannot change the side files the next scorer gets, nor the caller's."""
    handed = {LOGGED.pattern: CONTENT}

    def meddler(record, side_files):
        side_files[LOGGED.pattern] = b"rewritten"
        return {}

    with pytest.raises(TypeError, match="does not support item assignment"):
        rescore(SCENARIOS["tools"][0](), [meddler], side_files=handed)
    assert handed == {LOGGED.pattern: CONTENT}


@pytest.fixture
def attempt(tmp_path):
    """An attempt directory: one record, which names the side file beside it by its SHA-256."""
    root = tmp_path / "try1"
    record = dataclasses.replace(SCENARIOS["tools"][0](), run_key="run-sided")
    instruments = {**record.instruments, "ranges": {"ok": True, "side_sha256": hashlib.sha256(CONTENT).hexdigest()}}
    dataclasses.replace(record, instruments=instruments).write(root, environ={})
    (root / LOGGED.pattern).write_bytes(CONTENT)
    return root


def test_R14_rescore_files_hands_each_scorer_the_side_file_beside_the_record_read_once_and_paired(attempt):
    """R14: with the study's declaration, the side file beside the record is read, paired with the hash the record
    names, and handed to the scorer, which gets what it gets through `analyze`."""
    scorer = RangeBytes()
    [written] = rescore_files(attempt, [scorer], side_files=[LOGGED])
    assert scorer.handed == [{LOGGED.pattern: CONTENT}]
    assert load_record(written).scores["ranges"]["result"]["outcomes"] == {"logged": float(len(CONTENT))}


def test_R14_rescore_files_hands_an_empty_mapping_for_a_record_that_names_no_side_file(tmp_path):
    """R14: a record with neither the hash nor the file beside it gets an empty mapping."""
    root = tmp_path / "try1"
    dataclasses.replace(SCENARIOS["tools"][0](), run_key="run-plain").write(root)
    scorer = RangeBytes()
    rescore_files(root, [scorer], side_files=[LOGGED])
    assert scorer.handed == [{}]


@pytest.mark.parametrize("change", ["changed", "removed", "too-large"])
def test_R14_rescore_files_refuses_a_side_file_that_does_not_pair_before_any_scorer_runs(attempt, monkeypatch, change):
    """R14: a side file changed, removed while its record names it, or over the cap is refused, naming it, before any
    scorer runs and before any file is written."""
    side = attempt / LOGGED.pattern
    if change == "changed":
        side.write_bytes(CONTENT + b" edited")
        problem = "does not have the SHA-256 the record names"
    elif change == "removed":
        side.unlink()
        problem = "no such file lies beside it"
    else:
        monkeypatch.setattr(side_module, "MAX_SIDE_FILE_BYTES", 8)
        problem = "more than 8 bytes"
    before = written_files(attempt)
    scorer = RangeBytes()
    with pytest.raises(RecordError, match=problem):
        rescore_files(attempt, [scorer], side_files=[LOGGED])
    assert scorer.handed == []
    assert written_files(attempt) == before


def test_R14_rescore_files_reads_a_side_file_named_like_json_only_as_a_side_file(tmp_path, monkeypatch):
    """R14: a declared side file whose name ends in `.json` is never loaded as a record, even when its bytes hold one,
    so it is read only through the bounded reader and handed to the scorer."""
    named = SideFile("ranges_log.json", "instruments.ranges.side_sha256")
    content = dataclasses.replace(SCENARIOS["tools"][0](), run_key="run-inside").dumps(environ={}).encode("utf-8")
    root = tmp_path / "try1"
    record = dataclasses.replace(SCENARIOS["tools"][0](), run_key="run-sided")
    instruments = {**record.instruments, "ranges": {"ok": True, "side_sha256": hashlib.sha256(content).hexdigest()}}
    dataclasses.replace(record, instruments=instruments).write(root, environ={})
    (root / named.pattern).write_bytes(content)
    handed: list[dict[str, bytes]] = []

    def logged(record, side_files):
        handed.append(dict(side_files))
        return {"outcomes": {"logged": float(len(side_files[named.pattern]))}}

    reads: list[str] = []
    real = side_module.read_side_file
    monkeypatch.setattr(rescore_module, "read_side_file", lambda path: reads.append(path.name) or real(path))
    [written] = rescore_files(root, [logged], side_files=[named])
    assert load_record(written).run_key == "run-sided"
    assert handed == [{named.pattern: content}]
    assert reads == [named.pattern]


def test_R14_rescore_files_without_the_declaration_refuses_a_scorer_that_takes_side_files(attempt):
    """R14: without the study's declaration nothing is read beside the records, so a scorer that takes side files is
    refused before any file is written; a scorer of the record alone needs no declaration."""
    before = written_files(attempt)
    with pytest.raises(RecordError, match="takes the side files of the record"):
        rescore_files(attempt, [RangeBytes()])
    assert written_files(attempt) == before
    assert len(rescore_files(attempt, [TurnCount()])) == 1
