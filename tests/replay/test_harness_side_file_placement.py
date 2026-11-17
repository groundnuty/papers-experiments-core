"""O91: placing a side file the HARNESS wrote (the read log) beside its record, as `stage_side_file` places the agent's.

The log never was in the sandbox's /work: the sandbox's collection left it on the harness's disk, and
`stage_harness_side_file` copies it from there with the same guarantees: the temporary dot-name, hashing as it copies,
fsync, and the rename only once the record names the hash.
"""

import dataclasses
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from record_support import CODE
from replay_scenarios import SCENARIOS

from expcore.records.record import RunRecord
from expcore.replay import side_files as side_module
from expcore.replay.check import verify_batch
from expcore.replay.side_file_placement import (
    PlacementError,
    UndeclaredSideFile,
    stage_harness_side_file,
    temporary_name,
)
from expcore.replay.side_files import SideFile, SideFileTooLarge

LOG = SideFile("read_log.jsonl", "instruments.read_log.side_sha256")
DECLARED = [LOG]
CONTENT = b'{"event":"start"}\n' * 50
ATTEMPT = 1


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_for(run_key: str, sha: str | None) -> RunRecord:
    record = dataclasses.replace(SCENARIOS["tools"][0](), run_key=run_key, attempt=ATTEMPT)
    return dataclasses.replace(record, instruments={**record.instruments, "read_log": {"ok": True, "side_sha256": sha}})


@pytest.fixture
def batch(tmp_path):
    return tmp_path / "runs"


@pytest.fixture
def request_of(batch):
    return SimpleNamespace(run_dir=batch / "bbbb" / "try1", identity=SimpleNamespace(run_key="run-1", attempt=ATTEMPT))


@pytest.fixture
def harness_file(tmp_path):
    """The log where the sandbox's collection left it: on the harness's disk, outside the batch."""
    path = tmp_path / "collected" / "read_log.jsonl"
    path.parent.mkdir()
    path.write_bytes(CONTENT)
    return path


def failures(batch):
    made = verify_batch(batch, code=CODE, side_files=DECLARED)
    return [(f["file"], f["error"]) for f in made["failures"]]


def test_o91_the_helper_stages_a_harness_file_under_a_dot_name_and_returns_its_sha256(request_of, harness_file, batch):
    staged = stage_harness_side_file(request_of, DECLARED, LOG.pattern, harness_file)
    assert staged is not None
    assert staged.sha256 == digest(CONTENT)
    assert staged.size == len(CONTENT)
    temporary = request_of.run_dir / temporary_name(LOG.pattern, ATTEMPT)
    assert temporary.read_bytes() == CONTENT
    assert not (request_of.run_dir / LOG.pattern).exists()
    assert harness_file.read_bytes() == CONTENT  # copied, not moved
    assert failures(batch) == []  # a dot-name is never read by the check


def test_o91_the_rename_waits_for_the_record_that_names_the_hash(request_of, harness_file, batch):
    """O91: commit before the record is refused, with another hash is refused, and after the record renames the file;
    then the check pairs it with the record."""
    staged = stage_harness_side_file(request_of, DECLARED, LOG.pattern, harness_file)
    assert staged is not None
    with pytest.raises(PlacementError, match="no record"):
        staged.commit()
    record_for("run-1", "0" * 64).write(request_of.run_dir)
    with pytest.raises(PlacementError, match="names"):
        staged.commit()
    record_for("run-1", staged.sha256).write(request_of.run_dir)
    assert staged.commit() == request_of.run_dir / LOG.pattern
    assert (request_of.run_dir / LOG.pattern).read_bytes() == CONTENT
    assert failures(batch) == []


def test_o91_a_path_with_no_file_is_no_side_file(request_of, tmp_path):
    """O91: no log (the collector never wrote one): the helper returns None and writes nothing; the record then holds
    no hash and the check passes it as a run that made no side file."""
    assert stage_harness_side_file(request_of, DECLARED, LOG.pattern, tmp_path / "absent.jsonl") is None
    assert not request_of.run_dir.exists() or list(request_of.run_dir.iterdir()) == []


def test_o91_a_name_no_declaration_holds_is_refused_before_the_disk_is_touched(request_of, tmp_path):
    with pytest.raises(UndeclaredSideFile, match="declares no side file named"):
        stage_harness_side_file(request_of, DECLARED, "other.jsonl", tmp_path / "never-looked-at")
    assert not request_of.run_dir.exists()


def test_o91_a_link_is_refused_and_so_is_a_directory(request_of, harness_file, tmp_path):
    """O91: only a regular file is copied; a link to one is not followed."""
    link = tmp_path / "link.jsonl"
    os.symlink(harness_file, link)
    with pytest.raises(PlacementError):
        stage_harness_side_file(request_of, DECLARED, LOG.pattern, link)
    with pytest.raises(PlacementError):
        stage_harness_side_file(request_of, DECLARED, LOG.pattern, tmp_path)
    assert not request_of.run_dir.exists() or list(request_of.run_dir.iterdir()) == []


def test_o91_a_file_over_the_cap_is_refused_and_leaves_nothing(request_of, harness_file, monkeypatch):
    monkeypatch.setattr(side_module, "MAX_SIDE_FILE_BYTES", len(CONTENT) - 1)
    with pytest.raises(SideFileTooLarge, match="MAX_SIDE_FILE_BYTES"):
        stage_harness_side_file(request_of, DECLARED, LOG.pattern, harness_file)
    assert not request_of.run_dir.exists() or list(request_of.run_dir.iterdir()) == []


def test_o91_the_path_may_be_a_string_or_a_path(request_of, harness_file):
    staged = stage_harness_side_file(request_of, DECLARED, LOG.pattern, str(harness_file))
    assert staged is not None
    assert staged.sha256 == digest(CONTENT)
    assert isinstance(staged.path, Path)
