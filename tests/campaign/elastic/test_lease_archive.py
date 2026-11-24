"""A lease's archive read as evidence for reopening its run: bound to the hash the completion names, one open file, only
the store copied, its rows judged. Each refusal changes one thing of an archive the success test shows is accepted."""

import io
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest
from reopen_support import (
    DEFECT,
    INCIDENT,
    LEDGER_ERROR,
    OTHER_RUN,
    REFUSED,
    ZSTD_MAGIC,
    Row,
    fake_zstd_archive,
    install_stand_in_zstd,
    make_archive,
    real_zstd_archive,
    sha256_of,
)

from expcore.campaign.elastic.leasearchive import AttemptRow, check_attempts, read_evidence
from expcore.campaign.elastic.records import ReopenRefused

RUN = "task=task-0|model=m1|condition=api-none"
needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="the zstd program is not installed")


def _refused(path: Path, sha: str, number: int, phrase: str, run_key: str = RUN) -> ReopenRefused:
    with pytest.raises(ReopenRefused) as refusal:
        read_evidence(path, run_key, sha)
    message = str(refusal.value)
    assert message.startswith(f"condition {number} ("), message
    assert phrase in message, message
    return refusal.value


def _tar_with(path: Path, members: list[tuple[str, bytes]]) -> str:
    """A tar of the named members (the first of them first), its SHA-256."""
    with tarfile.open(path, "w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return sha256_of(path)


def _store_bytes(tmp_path: Path, rows=INCIDENT) -> bytes:
    path = tmp_path / "plain.sqlite"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT, attempt INTEGER, state TEXT,"
        " charged INTEGER, infra_kind TEXT, detail TEXT)"
    )
    for row in rows:
        db.execute(
            "INSERT INTO attempts(run_key, attempt, state, charged, infra_kind, detail) VALUES(?,?,?,?,?,?)",
            (row.run_key or RUN, row.attempt, row.state, row.charged, row.infra_kind, row.detail),
        )
    db.commit()
    db.close()
    return path.read_bytes()


def test_a_plain_tar_the_worker_wrote_is_read_when_its_hash_is_the_completions(tmp_path):
    """The success the refusals below each change one thing of: the attempt rows come back in row order, two of one
    attempt number told apart by their row ids."""
    archive, sha = make_archive(tmp_path, RUN)
    found = read_evidence(archive, RUN, sha)
    assert (found.sha256, found.form) == (sha, "tar")
    assert [(row.row_id, row.attempt, row.state, row.infra_kind, row.charged) for row in found.attempts] == [
        (1, 1, "infra_fail", "key_rejected", 0),
        (2, 1, "harness_error", None, 0),
    ]
    assert found.attempts[1].detail == LEDGER_ERROR
    assert "detail" not in found.attempts[1].summary()


def test_the_temporary_copy_of_the_store_is_gone_after_a_read_and_after_a_refusal(tmp_path, monkeypatch):
    """Only a private temporary directory holds anything of the archive, and it is removed whatever came of the read."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    archive, sha = make_archive(tmp_path, RUN)
    read_evidence(archive, RUN, sha)
    assert list(scratch.iterdir()) == []
    _refused(archive, "0" * 64, 3, "SHA-256")
    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "out").exists()  # nothing was extracted where the command ran


def test_a_hash_that_is_not_the_completions_is_refused(tmp_path):
    archive, sha = make_archive(tmp_path, RUN)
    refusal = _refused(archive, "f" * 64, 3, "not the archive the ledger verified")
    assert sha in str(refusal)


def test_bytes_added_after_the_tars_end_marker_change_the_hash_and_are_refused(tmp_path):
    """The hash covers the file to its end: a tar reader stops at its end marker, so trailing bytes are no reason to
    trust the file."""
    archive, sha = make_archive(tmp_path, RUN)
    with archive.open("ab") as handle:
        handle.write(b"\0" * 4096 + b"trailing")
    _refused(archive, sha, 3, "not the archive the ledger verified")


def test_a_member_changed_inside_the_tar_is_refused_by_the_hash(tmp_path):
    _, sha = make_archive(tmp_path, RUN)
    forged, _ = make_archive(
        tmp_path, RUN, (REFUSED, Row(1, "harness_error", 0, None, "KeyError nothing")), name="f.tar"
    )
    _refused(forged, sha, 3, "not the archive the ledger verified")


def test_a_symbolic_link_is_not_followed(tmp_path):
    archive, sha = make_archive(tmp_path, RUN)
    link = tmp_path / "link.tar"
    link.symlink_to(archive)
    _refused(link, sha, 3, "is a symbolic link")


def test_a_path_that_is_not_a_regular_file_is_refused(tmp_path):
    _, sha = make_archive(tmp_path, RUN)
    directory = tmp_path / "a-directory.tar"
    directory.mkdir()
    _refused(directory, sha, 3, "not a regular file")
    fifo = tmp_path / "a-pipe.tar"
    os.mkfifo(fifo)
    _refused(fifo, sha, 3, "not a regular file")


def test_a_path_that_is_not_there_is_refused(tmp_path):
    _refused(tmp_path / "missing.tar", "0" * 64, 3, "cannot be opened")


def test_a_file_that_is_not_a_tar_is_refused(tmp_path):
    junk = tmp_path / "junk.tar"
    junk.write_bytes(b"not a tar at all" * 100)
    _refused(junk, sha256_of(junk), 3, "not a readable tar")


def test_a_tar_cut_short_is_refused(tmp_path):
    archive, sha = make_archive(tmp_path, RUN)
    archive.write_bytes(archive.read_bytes()[:3000])
    _refused(archive, sha, 3, "")


def test_an_archive_without_the_store_is_refused_even_with_the_right_hash(tmp_path):
    path = tmp_path / "other.tar"
    sha = _tar_with(path, [("out/runs/try1/record.json", b"{}")])
    _refused(path, sha, 4, "holds no out/state.sqlite")


def test_a_store_that_appears_twice_is_ambiguous_and_refused(tmp_path):
    path = tmp_path / "twice.tar"
    data = _store_bytes(tmp_path)
    sha = _tar_with(path, [("out/state.sqlite", data), ("out/state.sqlite", data)])
    _refused(path, sha, 4, "more than once")


def test_a_store_that_is_a_link_or_a_directory_in_the_tar_is_refused(tmp_path):
    path = tmp_path / "linked.tar"
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("out/state.sqlite")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    _refused(path, sha256_of(path), 4, "not a regular file")


def test_a_store_that_is_not_a_database_is_refused(tmp_path):
    path = tmp_path / "garbage.tar"
    sha = _tar_with(path, [("out/state.sqlite", b"this is not a database" * 50)])
    _refused(path, sha, 4, "cannot be read")


def test_a_store_without_an_attempts_table_is_refused(tmp_path):
    scratch = tmp_path / "empty.sqlite"
    db = sqlite3.connect(scratch)
    db.execute("CREATE TABLE other(x)")
    db.commit()
    db.close()
    path = tmp_path / "other.tar"
    sha = _tar_with(path, [("out/state.sqlite", scratch.read_bytes())])
    _refused(path, sha, 4, "cannot be read")


def test_names_are_matched_whole_and_a_look_alike_is_not_the_store(tmp_path):
    path = tmp_path / "lookalike.tar"
    sha = _tar_with(path, [("out/state.sqlite.bak", _store_bytes(tmp_path)), ("other/out/state.sqlite", b"x")])
    _refused(path, sha, 4, "holds no out/state.sqlite")


def test_a_store_with_no_row_of_the_run_is_refused(tmp_path):
    archive, sha = make_archive(tmp_path, RUN, (Row(1, "infra_fail", 0, "key_rejected", "x", run_key=OTHER_RUN),))
    _refused(archive, sha, 4, "no attempt of run")


@pytest.mark.parametrize(
    ("rows", "phrase"),
    [
        ((REFUSED, Row(1, "harness_error", 1, None, LEDGER_ERROR)), "uncharged"),
        ((Row(1, "infra_fail", 1, "key_rejected", "x"), DEFECT), "uncharged"),
        ((Row(1, "infra_fail", 0, "endpoint", "x"), DEFECT), "key_rejected"),
        ((DEFECT,), "key_rejected"),
        ((REFUSED, Row(1, "infra_fail", 0, "endpoint", "x")), "not harness_error"),
        ((DEFECT, REFUSED), "not harness_error"),
        ((REFUSED, Row(1, "harness_error", 0, None, None)), "does not start with LedgerError"),
        ((REFUSED, Row(1, "harness_error", 0, None, "ValueError 0123456789ab: boom")), "does not start with"),
        ((REFUSED, Row(1, "harness_error", 0, None, "LedgerErrorX 0123456789ab: boom")), "does not start with"),
        ((REFUSED, Row(1, "harness_error", 0, None, "the LedgerError it was")), "does not start with"),
    ],
)
def test_attempt_rows_that_are_not_what_a_plugin_defect_leaves_are_refused(tmp_path, rows, phrase):
    """Each case changes one thing of the rows the success test accepts: a charged row, no key_rejected row, a last row
    that is not a harness error, and a detail that is missing or is not the plugin's LedgerError."""
    archive, sha = make_archive(tmp_path, RUN, rows)
    _refused(archive, sha, 4, phrase)


def test_a_charged_row_that_lives_only_in_the_wal_is_seen_and_refused(tmp_path):
    """A store archived with a connection open keeps its newest rows in the -wal: a read of the main file alone would
    pass this archive."""
    archive, sha = make_archive(tmp_path, RUN, (REFUSED, DEFECT), wal_rows=(Row(2, "infra_fail", 1, "endpoint", "x"),))
    _refused(archive, sha, 4, "uncharged")


def test_a_row_that_lives_only_in_the_wal_counts_when_it_is_the_defect(tmp_path):
    archive, sha = make_archive(tmp_path, RUN, (REFUSED,), wal_rows=(DEFECT,))
    found = read_evidence(archive, RUN, sha)
    assert [row.state for row in found.attempts] == ["infra_fail", "harness_error"]


@pytest.mark.parametrize(
    ("rows", "phrase"),
    [
        ([], "no attempt"),
        ([AttemptRow(1, 1, "harness_error", None, 0, LEDGER_ERROR)], "key_rejected"),
        (
            [AttemptRow(1, 1, "infra_fail", "key_rejected", None, "x"), AttemptRow(2, 1, "harness_error", None, 0, "")],
            "charged None",
        ),
        (
            [AttemptRow(1, 1, "infra_fail", "key_rejected", 0, None), AttemptRow(2, 1, "harness_error", None, 0, "")],
            "start",
        ),
    ],
)
def test_the_judgement_of_rows_names_what_fails_without_echoing_a_detail(rows, phrase):
    with pytest.raises(ReopenRefused) as refusal:
        check_attempts(rows, RUN)
    assert phrase in str(refusal.value)
    assert LEDGER_ERROR not in str(refusal.value)


def test_a_zstd_archive_is_hashed_as_the_tar_it_holds_and_read(tmp_path, monkeypatch):
    """The ledger names the hash of the plain tar the worker wrote, so a compressed copy cannot match it as a file: it
    is the decompressed stream that is hashed."""
    install_stand_in_zstd(tmp_path / "bin", monkeypatch)
    archive, sha = make_archive(tmp_path, RUN)
    packed = fake_zstd_archive(archive)
    assert sha256_of(packed) != sha
    found = read_evidence(packed, RUN, sha)
    assert (found.sha256, found.form) == (sha, "tar.zst")
    assert len(found.attempts) == 2


def test_a_zstd_archive_of_another_tar_is_refused_by_the_hash(tmp_path, monkeypatch):
    install_stand_in_zstd(tmp_path / "bin", monkeypatch)
    archive, _ = make_archive(tmp_path, RUN)
    other, other_sha = make_archive(
        tmp_path, RUN, (REFUSED, Row(1, "harness_error", 0, None, "KeyError x")), name="o.tar"
    )
    _refused(fake_zstd_archive(other), sha256_of(archive), 3, "hashed as the tar it holds")
    assert other_sha != sha256_of(archive)


def test_a_zstd_archive_is_refused_when_no_zstd_program_is_found(tmp_path, monkeypatch):
    archive, sha = make_archive(tmp_path, RUN)
    packed = fake_zstd_archive(archive)
    monkeypatch.setenv("PATH", str(tmp_path / "no-programs-here"))
    _refused(packed, sha, 3, "no zstd program")


def test_a_zstd_stream_the_program_cannot_decompress_is_refused_with_its_exit_status(tmp_path, monkeypatch):
    install_stand_in_zstd(tmp_path / "bin", monkeypatch, exit_status=1)
    archive, sha = make_archive(tmp_path, RUN)
    _refused(fake_zstd_archive(archive), sha, 3, "")


def test_zstd_is_chosen_by_what_the_file_holds_and_not_by_its_name(tmp_path, monkeypatch):
    """A compressed archive under a plain name, and a plain tar under a compressed name, are each read as they are."""
    install_stand_in_zstd(tmp_path / "bin", monkeypatch)
    archive, sha = make_archive(tmp_path, RUN)
    assert read_evidence(fake_zstd_archive(archive, "looks-plain.tar"), RUN, sha).form == "tar.zst"
    misnamed = tmp_path / "plain.tar.zst"
    shutil.copy(archive, misnamed)
    assert read_evidence(misnamed, RUN, sha).form == "tar"
    assert not archive.read_bytes().startswith(ZSTD_MAGIC)


@needs_zstd
def test_a_tar_compressed_by_the_real_zstd_is_read(tmp_path):
    archive, sha = make_archive(tmp_path, RUN)
    found = read_evidence(real_zstd_archive(archive), RUN, sha)
    assert (found.sha256, found.form, len(found.attempts)) == (sha, "tar.zst", 2)


@needs_zstd
def test_a_truncated_real_zstd_stream_is_refused(tmp_path):
    archive, sha = make_archive(tmp_path, RUN)
    packed = real_zstd_archive(archive)
    packed.write_bytes(packed.read_bytes()[: packed.stat().st_size // 2])
    with pytest.raises(ReopenRefused) as refusal:
        read_evidence(packed, RUN, sha)
    assert str(refusal.value).startswith("condition 3 (archive)")
