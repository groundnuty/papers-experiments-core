"""A lease's archive read as evidence stops where a tar stops and costs what a lease store costs: the tar must end as a
tar does (a damaged tail cannot hide a member from the reader), and a zstd stream is read for a bounded time and to a
bounded size. Each refusal changes one thing of an archive the success tests accept."""

import os
import tarfile
import time
from pathlib import Path

import pytest
from reopen_support import (
    DEFECT,
    REFUSED,
    Row,
    fake_zstd_archive,
    install_stand_in_zstd,
    install_zstd_script,
    make_archive,
    sha256_of,
)

from expcore.campaign.elastic import leasearchive as module
from expcore.campaign.elastic.leasearchive import STORE, WAL, read_evidence
from expcore.campaign.elastic.records import ReopenRefused

RUN = "task=task-0|model=m1|condition=api-none"
BLOCK = 512
CHARGED_IN_WAL = Row(2, "infra_fail", 1, "endpoint", "x")
NOT_A_TAR_END = "does not end as a tar does"


def _end_of(archive: Path, name: str) -> int:
    """The offset just past the data of member `name`, padded to a block: where the next header begins."""
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert names.index(WAL) > names.index(STORE), "the tests below hide the -wal behind the main file"
        info = tar.getmember(name)
    return info.offset_data + -(-info.size // BLOCK) * BLOCK


def _wal_behind_the_main_file(tmp_path: Path) -> tuple[bytes, int]:
    """The tar of a store whose main file passes the judgement of its rows alone and whose -wal holds a charged row, and
    the offset where the -wal's header begins."""
    archive, _ = make_archive(tmp_path, RUN, (REFUSED, DEFECT), wal_rows=(CHARGED_IN_WAL,))
    return archive.read_bytes(), _end_of(archive, STORE)


def _damaged(tmp_path: Path, name: str, data: bytes) -> tuple[Path, str]:
    path = tmp_path / name
    path.write_bytes(data)
    return path, sha256_of(path)


def _refused(path: Path, sha: str, phrase: str, number: int = 3) -> ReopenRefused:
    with pytest.raises(ReopenRefused) as refusal:
        read_evidence(path, RUN, sha)
    message = str(refusal.value)
    assert message.startswith(f"condition {number} ("), message
    assert phrase in message, message
    return refusal.value


def test_the_wal_is_what_refuses_the_archive_when_the_tar_is_whole(tmp_path):
    """The control of the cases below: the same store, read whole, is refused for the charged row its -wal holds."""
    archive, sha = make_archive(tmp_path, RUN, (REFUSED, DEFECT), wal_rows=(CHARGED_IN_WAL,))
    _refused(archive, sha, "uncharged", number=4)


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(b"\x01" * BLOCK, id="a header that does not parse"),
        pytest.param(b"ustar" * 100, id="text that is not a header"),
    ],
)
def test_a_header_that_does_not_parse_after_a_member_is_not_taken_for_the_end_and_the_wal_behind_it_is_not_hidden(
    tmp_path, tail
):
    """`tarfile` stops silently at a header it cannot parse after a good member, so the main file alone would be judged
    and the charged row in the -wal behind it never seen. The archive is refused, though its hash is the file's own."""
    data, cut = _wal_behind_the_main_file(tmp_path)
    path, sha = _damaged(tmp_path, "hidden.tar", data[:cut] + tail + data[cut:])
    _refused(path, sha, NOT_A_TAR_END)


@pytest.mark.parametrize(
    ("name", "end"),
    [
        pytest.param("none", b"", id="no end marker"),
        pytest.param("one-block", b"\0" * BLOCK, id="one block of zeros"),
        pytest.param("cut-header", b"\0" * 100, id="a header cut short"),
    ],
)
def test_a_tar_that_stops_after_a_member_without_the_two_zero_blocks_is_refused(tmp_path, name, end):
    data, cut = _wal_behind_the_main_file(tmp_path)
    path, sha = _damaged(tmp_path, f"{name}.tar", data[:cut] + end)
    _refused(path, sha, NOT_A_TAR_END)


def test_a_byte_that_is_not_zero_after_the_end_marker_is_refused_whatever_the_hash(tmp_path):
    """The whole store was read here: it is the tail, past the marker, that a tar reader never looks at."""
    archive, _ = make_archive(tmp_path, RUN, (REFUSED, DEFECT))
    path, sha = _damaged(tmp_path, "tail.tar", archive.read_bytes() + b"\0" * 2 * BLOCK + b"x")
    _refused(path, sha, NOT_A_TAR_END)


def test_zeros_past_the_end_marker_are_the_padding_of_a_tar_and_are_read(tmp_path):
    archive, _ = make_archive(tmp_path, RUN, (REFUSED, DEFECT))
    path, sha = _damaged(tmp_path, "padded.tar", archive.read_bytes() + b"\0" * 20 * 1024)
    assert [row.state for row in read_evidence(path, RUN, sha).attempts] == ["infra_fail", "harness_error"]


def test_the_hash_is_judged_before_the_end_of_the_tar(tmp_path):
    """A damaged tar that is not the lease's archive is refused for that, as a wrong file with a bad member is."""
    data, cut = _wal_behind_the_main_file(tmp_path)
    path, _ = _damaged(tmp_path, "hidden.tar", data[:cut] + b"\x01" * BLOCK + data[cut:])
    _refused(path, "0" * 64, "not the archive the ledger verified")


def test_a_damaged_tail_is_refused_in_a_zstd_archive_too(tmp_path, monkeypatch):
    install_stand_in_zstd(tmp_path / "bin", monkeypatch)
    data, cut = _wal_behind_the_main_file(tmp_path)
    plain, sha = _damaged(tmp_path, "hidden.tar", data[:cut] + b"\x01" * BLOCK + data[cut:])
    _refused(fake_zstd_archive(plain), sha, NOT_A_TAR_END)


def _zstd_that(tmp_path: Path, monkeypatch, body: str) -> Path:
    """A `zstd` that records its process id in a file, then does `body`; the file's path."""
    pid_file = tmp_path / "zstd.pid"
    monkeypatch.setenv("ZSTD_PID_FILE", str(pid_file))
    install_zstd_script(tmp_path / "bin", monkeypatch, f'echo $$ > "$ZSTD_PID_FILE"\n{body}')
    return pid_file


def _is_gone(pid_file: Path) -> bool:
    try:
        os.kill(int(pid_file.read_text()), 0)
    except ProcessLookupError:
        return True
    return False


def test_a_zstd_stream_that_decompresses_past_the_cap_is_refused_and_the_program_killed(tmp_path, monkeypatch):
    """The tar comes through whole and then the stream does not end (a decompression bomb, or a program gone wrong)."""
    pid_file = _zstd_that(tmp_path, monkeypatch, "tail -c +5\nexec cat /dev/zero\n")
    monkeypatch.setattr(module, "MAX_DECOMPRESSED_BYTES", 1 << 20)
    archive, sha = make_archive(tmp_path, RUN)
    assert archive.stat().st_size < 1 << 20
    _refused(fake_zstd_archive(archive), sha, f"more than {1 << 20} bytes")
    assert _is_gone(pid_file)


def test_a_stream_the_size_of_the_cap_is_read(tmp_path, monkeypatch):
    archive, sha = make_archive(tmp_path, RUN)
    install_stand_in_zstd(tmp_path / "bin", monkeypatch)
    monkeypatch.setattr(module, "MAX_DECOMPRESSED_BYTES", archive.stat().st_size)
    assert read_evidence(fake_zstd_archive(archive), RUN, sha).form == "tar.zst"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("exec sleep 30\n", id="no output at all"),
        pytest.param("tail -c +5\nexec sleep 30\n", id="the tar and then no end"),
    ],
)
def test_a_zstd_program_that_does_not_end_is_killed_at_the_deadline_and_the_archive_refused(
    tmp_path, monkeypatch, body
):
    pid_file = _zstd_that(tmp_path, monkeypatch, body)
    monkeypatch.setattr(module, "ZSTD_DEADLINE_S", 0.5)
    archive, sha = make_archive(tmp_path, RUN)
    started = time.monotonic()
    _refused(fake_zstd_archive(archive), sha, "zstd did not finish within 0.5 s")
    assert time.monotonic() - started < 15
    assert _is_gone(pid_file)
