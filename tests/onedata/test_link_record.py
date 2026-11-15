"""O35: the link-check record, `link-check.json`: the report of one all-pairs link check, written whole or not at all.

`up` has always written it (`deploy/compose-federation/fedlinks.py`); the restore's and the heal's link checks write it
through the same function now, so every writer produces one format and no reader of it (an operator's pre-check that
wants a check under an hour old) sees a torn file.
"""

import errno
import json
import os
import threading
from pathlib import Path

import pytest

from expcore_onedata import linkrecord
from expcore_onedata.linkcheck import REMEDY, LinkReport, PairResult
from expcore_onedata.linkrecord import RECORD, discard_record, record_text, write_record


def passing(pairs: int = 2, *, started: float = 10.0, ended: float = 12.5) -> LinkReport:
    results = tuple(PairResult(f"providers[{i}]", "providers[9]", True, 0.25, 65536) for i in range(pairs))
    return LinkReport(True, "every pair passed", results, started, ended)


def failing(pairs: int = 2, *, started: float = 20.0, ended: float = 55.0) -> LinkReport:
    results = tuple(
        PairResult(
            f"providers[{i}]",
            "providers[9]",
            False,
            30.0,
            reason=f"no transfer from providers[9] to providers[{i}] within 30 s",
            remedy=REMEDY.format(reader=f"providers[{i}]"),
        )
        for i in range(pairs)
    )
    return LinkReport(False, "a provider link is stuck or untested", results, started, ended)


def directory_names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_o35_the_record_is_named_for_the_file_up_writes():
    """O35: one name for every writer, the one `up`'s facts sit beside."""
    assert RECORD == "link-check.json"


def test_o35_the_record_text_is_the_reports_json_exactly_as_up_has_always_written_it():
    """O35: indent 2, sorted keys, a final newline: the bytes `up` wrote before the writer was shared, so a reader of an
    older record reads a new one."""
    report = passing()
    assert record_text(report) == json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n"
    assert record_text(report).startswith('{\n  "detail": "every pair passed",\n  "ok": true,\n')
    assert record_text(report).endswith("}\n")


def test_o35_a_written_record_is_the_report_whole_and_leaves_nothing_beside_it(tmp_path):
    """O35: the record holds the verdict, each pair, the transfers and the check's window on the host's epoch clock."""
    path = tmp_path / RECORD
    write_record(passing(started=10.0, ended=12.5), path)
    assert json.loads(path.read_text(encoding="utf-8")) == passing(started=10.0, ended=12.5).to_json()
    assert json.loads(path.read_text(encoding="utf-8"))["window"] == {"start_epoch_s": 10.0, "end_epoch_s": 12.5}
    assert directory_names(tmp_path) == [RECORD]


def test_o35_a_failing_report_is_written_as_the_failing_verdict_it_is(tmp_path):
    """O35: the record never says more than the check found: a failing report is written failing, naming the pair."""
    path = tmp_path / RECORD
    write_record(failing(), path)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["ok"] is False
    assert [pair["ok"] for pair in record["pairs"]] == [False, False]
    assert "no transfer from providers[9] to providers[0]" in record["pairs"][0]["reason"]


def test_o35_a_rewrite_replaces_the_old_record_in_one_rename_of_a_complete_draft(tmp_path, monkeypatch):
    """O35: the draft is a sibling of the record, complete and fsynced before it replaces the old one, and the old one
    is whole until then, so a reader sees the old record or the new, never a mixture."""
    path = tmp_path / RECORD
    write_record(passing(), path)
    seen: list[tuple[Path, Path]] = []
    synced: list[int] = []
    real_replace, real_fsync = os.replace, os.fsync

    def replace(source, destination):
        seen.append((Path(source), Path(destination)))
        assert json.loads(Path(source).read_text(encoding="utf-8")) == failing().to_json()  # the draft is complete
        assert json.loads(Path(destination).read_text(encoding="utf-8")) == passing().to_json()  # the old one, whole
        assert len(synced) >= 1  # the draft's data was flushed to disk before the rename
        real_replace(source, destination)

    def fsync(descriptor):
        synced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync)
    write_record(failing(), path)
    ((draft, destination),) = seen
    assert destination == path
    assert draft.parent == path.parent
    assert draft != path
    assert len(synced) >= 2  # the draft, then the directory that holds the rename
    assert json.loads(path.read_text(encoding="utf-8")) == failing().to_json()
    assert directory_names(tmp_path) == [RECORD]


def test_o35_a_concurrent_reader_never_reads_a_torn_record(tmp_path):
    """O35: a reader that reads the record in a loop while another writes it again and again always gets one complete
    report, passing or failing, never a prefix of one."""
    path = tmp_path / RECORD
    sizes = {True: 300, False: 200}
    reports = {True: passing(sizes[True]), False: failing(sizes[False])}
    write_record(reports[True], path)
    stop = threading.Event()
    problems: list[str] = []
    reads = [0]

    def read() -> None:
        while not stop.is_set():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                problems.append(f"{type(error).__name__}: {error}")
                return
            if len(record["pairs"]) != sizes[record["ok"]]:
                problems.append("a record whose verdict and pairs disagree")
                return
            reads[0] += 1

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for number in range(60):
            write_record(reports[number % 2 == 1], path)
    finally:
        stop.set()
        reader.join(30)
    assert problems == []
    assert reads[0] > 0
    assert directory_names(tmp_path) == [RECORD]


def test_o35_a_write_that_fails_leaves_the_old_record_whole_and_no_draft(tmp_path, monkeypatch):
    """O35: a disk that cannot take the draft raises, and what was on disk before is still the whole old record: no
    half-written file, no stray draft."""
    path = tmp_path / RECORD
    write_record(passing(), path)

    def full(descriptor):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError, match="no space"):
        write_record(failing(), path)
    monkeypatch.undo()
    assert json.loads(path.read_text(encoding="utf-8")) == passing().to_json()
    assert directory_names(tmp_path) == [RECORD]


def test_o35_two_writers_never_share_a_draft(tmp_path, monkeypatch):
    """O35: the draft's name is the writer's own, so a second process writing at the same time cannot overwrite the
    first one's draft before it is renamed."""
    path = tmp_path / RECORD
    names: list[str] = []
    real_replace = os.replace

    def replace(source, destination):
        names.append(Path(source).name)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    write_record(passing(), path)
    write_record(passing(), path)
    assert len(names) == 2
    assert len(set(names)) == 2


def test_o35_the_record_has_the_mode_a_plain_write_gives_it(tmp_path):
    """O35: `up`'s record was written with the process's umask, and is still: the draft is not owner-only, since the
    record holds only labels, seconds, bytes and transfer ids."""
    path = tmp_path / RECORD
    previous = os.umask(0o022)
    try:
        write_record(passing(), path)
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o644


def test_o35_a_record_is_discarded_so_that_no_older_verdict_stands(tmp_path):
    """O35: removing the record is how a check that cannot be recorded keeps a passing verdict that is older than it
    from standing on disk; discarding what is not there is no error."""
    path = tmp_path / RECORD
    write_record(passing(), path)
    discard_record(path)
    assert not path.exists()
    discard_record(path)
    assert directory_names(tmp_path) == []


def test_o35_the_module_names_its_three_functions_and_the_record():
    """O35: the writer's whole interface: what `fedlinks` and the platform's probe import."""
    assert set(linkrecord.__all__) == {"RECORD", "discard_record", "record_text", "write_record"}
