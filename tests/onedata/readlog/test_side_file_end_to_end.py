"""O91: the log as the study's declared side file, from the instrument's snapshot to the replay check.

Two byte sources must agree: the hash the instrument puts in its snapshot (of the bytes the sandbox read once) and the
hash `stage_harness_side_file` computes of the file it copies. The record names the first; `commit` compares it with
the second; the check pairs the file with the record. A missing log is the same chain with no file and a null hash.
"""

import dataclasses
from types import SimpleNamespace

import pytest
from readlog_logs import NONCE, PEER_UID, log, read
from record_support import CODE
from replay_scenarios import SCENARIOS

from expcore.replay.check import verify_batch
from expcore.replay.side_file_placement import PlacementError, stage_harness_side_file
from expcore.replay.side_files import SideFile
from expcore_onedata.readlog import paths
from expcore_onedata.readlog.instrument import ReadLog

DECLARED = [SideFile(paths.SIDE_FILE_NAME, paths.SIDE_HASH_FIELD)]
READS = [read("file-1"), read("file-2", bytes=10)]


class Source:
    def __init__(self, data):
        self.data = data

    def read_log(self):
        return self.data


@pytest.fixture
def batch(tmp_path):
    return tmp_path / "runs"


@pytest.fixture
def request_of(batch):
    return SimpleNamespace(run_dir=batch / "aaaa" / "try1", identity=SimpleNamespace(run_key="run-1", attempt=1))


def failures(batch):
    made = verify_batch(batch, code=CODE, side_files=DECLARED)
    return [(f["file"], f["error"]) for f in made["failures"]]


def written_record(request_of, snapshot):
    record = dataclasses.replace(SCENARIOS["tools"][0](), run_key="run-1", attempt=1)
    record = dataclasses.replace(record, instruments={**record.instruments, paths.NAMESPACE: snapshot})
    return record.write(request_of.run_dir)


def collect_to_disk(tmp_path, data):
    """What the sandbox's collection does: put the bytes it read once on the harness's disk."""
    target = tmp_path / "collected.jsonl"
    target.write_bytes(data)
    return target


@pytest.mark.parametrize("closed", [True, False], ids=["intact", "killed"])
def test_o91_the_snapshots_hash_is_the_staged_files_and_the_check_pairs_it(request_of, batch, tmp_path, closed):
    """O91: instrument, stage, record, commit, check: no failure, whether the log is intact or unknown (an unknown log
    is staged too, so that what was read before the kill can be looked at)."""
    data = log(READS, end=closed)
    snapshot = ReadLog(Source(data), NONCE, PEER_UID).finish()
    staged = stage_harness_side_file(request_of, DECLARED, paths.SIDE_FILE_NAME, collect_to_disk(tmp_path, data))
    assert staged is not None
    assert staged.sha256 == snapshot["side_sha256"]
    written_record(request_of, snapshot)
    staged.commit()
    assert (request_of.run_dir / paths.SIDE_FILE_NAME).read_bytes() == data
    assert failures(batch) == []
    assert snapshot["log"] == ("intact" if closed else "unknown")


def test_o91_a_missing_log_is_a_null_hash_and_no_file_and_the_check_passes(request_of, batch, tmp_path):
    """O91: no log is `side_sha256: null` and no side file: the check reads it as a run that made no side file, and the
    snapshot says the log is unknown, so a scorer never reads the missing file as zero reads."""
    snapshot = ReadLog(Source(None), NONCE, PEER_UID).finish()
    assert snapshot["side_sha256"] is None
    assert stage_harness_side_file(request_of, DECLARED, paths.SIDE_FILE_NAME, tmp_path / "none.jsonl") is None
    written_record(request_of, snapshot)
    assert failures(batch) == []
    assert snapshot["log"] == "unknown"


def test_o91_a_log_that_changed_between_the_read_and_the_copy_is_refused_at_the_rename(request_of, tmp_path):
    """O91: the two byte sources disagree: the record names the snapshot's hash, the staged file hashes otherwise, and
    commit refuses rather than renaming a file the record does not name."""
    data = log(READS)
    snapshot = ReadLog(Source(data), NONCE, PEER_UID).finish()
    staged = stage_harness_side_file(
        request_of, DECLARED, paths.SIDE_FILE_NAME, collect_to_disk(tmp_path, log([*READS, read("sneaked-in")]))
    )
    assert staged is not None
    written_record(request_of, snapshot)
    with pytest.raises(PlacementError, match="names"):
        staged.commit()
    assert not (request_of.run_dir / paths.SIDE_FILE_NAME).exists()
