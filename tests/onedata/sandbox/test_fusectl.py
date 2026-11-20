"""O97: a FUSE connection as both backends read it: its fusectl name from a device, the device of a mount point from a
mount table alone, the state of its control files for this uid, and the one write that aborts it."""

import os
import stat

import pytest
from fake_mount_node import fusectl_tree

from expcore_onedata.sandbox import fusectl as FC
from expcore_onedata.sandbox import teardown as TD

TABLE = (
    "1 0 8:1 / / rw shared:1 - ext4 /dev/invented rw\n"
    "2 1 0:20 / /proc rw shared:2 - proc proc rw\n"
    "3 1 0:57 / /run/mnt ro,nosuid shared:3 - fuse.oneclient oneclient rw,user_id=1000,group_id=1000\n"
    "4 1 0:58 / /run/mnt ro,nosuid shared:4 - fuse.oneclient oneclient rw,user_id=1000,group_id=1000\n"
    "5 1 0:59 / /run/other rw shared:5 - tmpfs tmpfs rw\n"
)


def test_o97_the_connection_of_a_fuse_device_is_its_minor():
    assert FC.conn("0:57") == "57"


def test_o97_a_device_with_a_major_is_no_fuse_connection():
    assert FC.conn("8:1") is None
    assert FC.conn("0:abc") is None
    assert FC.conn("garbage") is None


def test_o97_the_docker_teardown_reads_the_same_connection_name():
    """O97: one parse for both backends, not two that can drift."""
    assert TD._conn("0:57") == FC.conn("0:57")
    assert TD._conn("8:1") == FC.conn("8:1")
    assert TD.FUSECTL == FC.FUSECTL


def test_o97_the_device_of_a_mount_point_is_the_last_fuse_mount_there():
    assert FC.fuse_device(TABLE, "/run/mnt") == "0:58"


def test_o97_a_mount_point_with_no_fuse_mount_has_no_device():
    assert FC.fuse_device(TABLE, "/run/other") is None
    assert FC.fuse_device(TABLE, "/nowhere") is None


def test_o97_a_table_that_cannot_be_read_gives_no_device():
    assert FC.fuse_device("not a mount table\n", "/run/mnt") is None


def test_o97_a_table_cut_off_before_its_root_mount_gives_no_device_even_if_it_shows_the_mount():
    """O97: a cut-off table is unknown, never a mount to write an abort for."""
    assert FC.fuse_device("".join(TABLE.splitlines(keepends=True)[1:]), "/run/mnt") is None


def test_o97_a_connection_whose_control_files_this_uid_may_use_is_writable(tmp_path):
    fusectl_tree(tmp_path, "0:57")
    assert FC.state(tmp_path, "57") == FC.WRITABLE


def test_o97_a_connection_with_no_directory_is_absent(tmp_path):
    assert FC.state(tmp_path, "57") == FC.ABSENT


def test_o97_an_unknown_connection_is_absent(tmp_path):
    assert FC.state(tmp_path, None) == FC.ABSENT


def test_o97_a_connection_with_no_abort_file_is_not_writable(tmp_path):
    fusectl_tree(tmp_path, "0:57", abort=False)
    assert FC.state(tmp_path, "57") == FC.NOT_WRITABLE


@pytest.mark.skipif(os.geteuid() == 0, reason="root may write any file: the mode proves nothing")
def test_o97_a_connection_whose_abort_this_uid_may_not_write_is_not_writable(tmp_path):
    directory = fusectl_tree(tmp_path, "0:57")
    (directory / "abort").chmod(stat.S_IRUSR)
    assert FC.state(tmp_path, "57") == FC.NOT_WRITABLE


def test_o97_the_waiting_count_is_read_as_a_number(tmp_path):
    fusectl_tree(tmp_path, "0:57", waiting=3)
    assert FC.waiting(tmp_path, "57") == 3


def test_o97_a_waiting_count_that_cannot_be_read_is_unknown_never_zero(tmp_path):
    assert FC.waiting(tmp_path, "57") is None
    directory = fusectl_tree(tmp_path, "0:58")
    (directory / "waiting").write_text("many\n")
    assert FC.waiting(tmp_path, "58") is None


def test_o97_the_abort_writes_one_to_the_connections_abort_file(tmp_path):
    directory = fusectl_tree(tmp_path, "0:57")
    FC.write_abort(tmp_path, "57")
    assert (directory / "abort").read_bytes() == b"1"


def test_o97_the_abort_never_creates_a_file_for_a_connection_that_is_gone(tmp_path):
    (tmp_path / "57").mkdir()
    with pytest.raises(FileNotFoundError):
        FC.write_abort(tmp_path, "57")
    assert not (tmp_path / "57" / "abort").exists()
