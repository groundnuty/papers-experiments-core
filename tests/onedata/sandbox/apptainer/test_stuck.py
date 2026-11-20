"""The parsers of what a surviving process is blocked on (`expcore_onedata.sandbox.apptainer.stuck`): pure text."""

import pytest

from expcore_onedata.sandbox.apptainer import stuck as S

READ_OF_FD_THREE = "0 0x3 0x7f10 0x1000 0x0 0x0 0x0 0x7ffd1000 0x7f110000"
MOUNT_TABLE = (
    "36 35 8:1 /run/work /work rw,noatime master:1 - ext3 /dev/root rw\n"
    "40 35 0:99 / /data ro,nosuid shared:3 - fuse.oneclient oneclient ro,user_id=1000\n"
    "41 35 0:99 / /data/nested ro - fuse.oneclient oneclient ro\n"
    "42 35 8:2 / /with\\040space rw - ext3 /dev/x rw\n"
    "not a mount line\n"
)


def test_o97_a_read_syscall_gives_its_number_and_arguments():
    """O97: `syscall` reads `0 0x3 ...` as read(2) of descriptor 3."""
    call = S.parse_syscall(READ_OF_FD_THREE)
    assert call == S.Syscall(0, (3, 0x7F10, 0x1000, 0, 0, 0))
    assert S.fd_call(call) == ("read", 3)


def test_o97_a_task_in_no_syscall_reads_running():
    """O97: a task not in a syscall reads `running`, which names no call."""
    assert S.parse_syscall("running\n") == S.RUNNING


def test_o97_a_task_blocked_outside_a_syscall_reads_minus_one():
    """O97: `-1 sp pc` is a task blocked outside a syscall (a page fault), with no arguments and no file descriptor."""
    call = S.parse_syscall("-1 0x7ffd1000 0x7f110000")
    assert call == S.Syscall(-1, ())
    assert S.fd_call(call) is None
    assert S.path_call(call) is None


@pytest.mark.parametrize("text", ["", "garbage", "0 0x3", "0 zz 0 0 0 0 0 0 0", "1 2 3 4 5 6 7 8 9 10"])
def test_o97_text_that_is_no_syscall_reading_is_unreadable(text):
    """O97: anything that is neither a call, `running` nor `-1 sp pc` is unreadable, never a guessed call."""
    assert S.parse_syscall(text) == S.UNREADABLE


def test_o97_a_descriptor_argument_is_its_low_32_bits_signed():
    """O97: AT_FDCWD arrives as 0xffffffffffffff9c and reads -100; a descriptor argument's upper half is ignored."""
    assert S.signed32(0xFFFFFFFFFFFFFF9C) == S.AT_FDCWD
    assert S.signed32(0x1_0000_0003) == 3
    assert S.signed32(0x8000_0000) == -(2**31)


def test_o97_sendfile_names_its_descriptor_in_the_second_argument():
    """O97: `sendfile` waits on `in_fd`, its second argument."""
    call = S.Syscall(40, (9, 5, 0, 0, 0, 0))
    assert S.fd_call(call) == ("sendfile", 5)


def test_o97_an_unlisted_call_names_no_descriptor_and_no_path():
    """O97: a call the tables do not list (mmap, 9) is read by the holdings rule, not as a descriptor or a path."""
    call = S.Syscall(9, (0, 4096, 1, 1, 3, 0))
    assert S.fd_call(call) is None
    assert S.path_call(call) is None


def test_o97_an_openat_gives_its_directory_descriptor_and_its_path_pointer():
    """O97: `openat` has its directory descriptor first and the path pointer second."""
    call = S.Syscall(257, (0xFFFFFFFFFFFFFF9C, 0x7F20, 0, 0, 0, 0))
    assert S.path_call(call) == ("openat", S.AT_FDCWD, 0x7F20)


def test_o97_an_open_has_its_path_pointer_first_and_no_directory_descriptor():
    """O97: `open`, `stat` and `lstat` name the path in the first argument, relative to the working directory."""
    assert S.path_call(S.Syscall(2, (0x7F20, 0, 0, 0, 0, 0))) == ("open", None, 0x7F20)
    assert S.path_call(S.Syscall(4, (0x7F30, 0, 0, 0, 0, 0))) == ("stat", None, 0x7F30)


def test_o97_a_path_is_the_text_up_to_its_first_nul():
    """O97: the path argument is read up to the first NUL, and what follows it is never part of it."""
    assert S.cstring(b"/data/orchard/f.txt\0trailing") == "/data/orchard/f.txt"


def test_o97_a_read_with_no_nul_is_unknown_not_a_cut_path():
    """O97: a path with no NUL in what was read (too long, or a short read) is unknown: never a truncated path."""
    assert S.cstring(b"/data/orchard/" + b"x" * 100) is None
    assert S.cstring(None) is None


def test_o97_a_path_is_normalised_without_asking_a_file_system():
    """O97: `.`, `..` and repeated slashes go lexically, and `..` at the root stays there."""
    assert S.normalize("/data//orchard/./a/../b") == "/data/orchard/b"
    assert S.normalize("/../../x") == "/x"


def test_o97_a_link_names_a_path_only_when_it_is_one():
    """O97: `pipe:[1]` and `anon_inode:...` name no path; ` (deleted)` is dropped from one that does."""
    assert S.link_path("pipe:[1234]") is None
    assert S.link_path("anon_inode:[eventfd]") is None
    assert S.link_path(None) is None
    assert S.link_path("/data/orchard/f (deleted)") == "/data/orchard/f"


def test_o97_fdinfo_gives_the_offset_the_flags_and_the_mount():
    """O97: `fdinfo` has the file's offset, its flags in octal and the id of the mount it is on."""
    info = S.parse_fdinfo("pos:\t4096\nflags:\t0100002\nmnt_id:\t40\nino:\t7\n")
    assert info == S.FdInfo(4096, "0100002", 40)


def test_o97_a_damaged_fdinfo_gives_unknown_fields():
    """O97: a field that is missing or not a number is None, so a descriptor with no mount id is never placed."""
    assert S.parse_fdinfo("pos:\tx\nflags:\t9\n") == S.FdInfo(None, None, None)


def test_o97_mountinfo_gives_each_mount_and_skips_a_line_that_is_not_whole():
    """O97: every whole line is a mount with its id, device, point and file system; the damaged one is left out."""
    mounts = S.parse_mountinfo(MOUNT_TABLE)
    assert [(m.ident, m.device, m.point, m.fstype) for m in mounts] == [
        (36, "8:1", "/work", "ext3"),
        (40, "0:99", "/data", "fuse.oneclient"),
        (41, "0:99", "/data/nested", "fuse.oneclient"),
        (42, "8:2", "/with space", "ext3"),
    ]
    assert mounts[1].fuse
    assert not mounts[0].fuse


def test_o97_a_path_is_on_the_mount_with_the_longest_point_above_it():
    """O97: `/data/nested/x` is on `/data/nested`, `/data/orchard/x` on `/data`, and `/etc/x` on neither."""
    mounts = S.parse_mountinfo(MOUNT_TABLE)
    assert S.mount_of_path(mounts, "/data/nested/x").ident == 41
    assert S.mount_of_path(mounts, "/data/orchard/x").ident == 40
    assert S.mount_of_path(mounts, "/etc/x") is None
    assert S.mount_of_path(mounts, "/database/x") is None  # a name that merely starts alike is no path below it


def test_o97_a_mount_is_data_only_when_it_is_the_fuse_mount_at_the_data_path():
    """O97: the FUSE mount at the data path is `data`; the same point with another file system is `other`."""
    roles = S.Roles("/data")
    fuse, ext3 = S.MountEntry(40, "0:99", "/data", "fuse.oneclient"), S.MountEntry(41, "8:1", "/data", "ext3")
    assert roles.of(fuse) == S.DATA
    assert roles.of(ext3) == S.OTHER


def test_o97_with_the_runs_device_only_that_connection_is_the_data_mount():
    """O97: where the host knows the run's FUSE device, a FUSE mount on another device at the point is `other`."""
    entry = S.MountEntry(40, "0:99", "/data", "fuse.oneclient")
    assert S.Roles("/data", device="0:99").of(entry) == S.DATA
    assert S.Roles("/data", device="0:100").of(entry) == S.OTHER


def test_o97_the_cells_fixed_targets_have_their_own_roles():
    """O97: /work, /tmp, /dev/shm and a read-only input are named by their points; anything else is `other`."""
    roles = S.Roles("/data", inputs=("/opt/inputs",))
    roles_of = {
        point: roles.of(S.MountEntry(1, "8:1", point, "ext3"))
        for point in ("/work", "/tmp", "/dev/shm", "/opt/inputs", "/proc")
    }
    assert roles_of == {"/work": S.WORK, "/tmp": S.TMP, "/dev/shm": S.SHM, "/opt/inputs": S.INPUT, "/proc": S.OTHER}


def test_o97_a_file_on_the_data_mount_has_its_space_and_its_path_under_the_space():
    """O97: the first component under the mount is the space; the rest is the path under it."""
    roles, entry = S.Roles("/data"), S.MountEntry(40, "0:99", "/data", "fuse.oneclient")
    place = roles.place(entry, "/data/orchard/dir/f.txt")
    assert (place.mount, place.space, place.rest) == (S.DATA, "orchard", "dir/f.txt")


def test_o97_the_mount_point_itself_has_no_space():
    """O97: a wait on the data mount's own root names no space, so no space's holder can be blamed for it."""
    roles, entry = S.Roles("/data"), S.MountEntry(40, "0:99", "/data", "fuse.oneclient")
    assert roles.place(entry, "/data").space is None


def test_o97_a_path_that_is_not_below_the_mount_point_has_no_space():
    """O97: a link whose path is not under the data mount's point (a moved mount) leaves the space unknown."""
    roles, entry = S.Roles("/data"), S.MountEntry(40, "0:99", "/data", "fuse.oneclient")
    place = roles.place(entry, "/elsewhere/x")
    assert place.mount == S.DATA
    assert place.space is None
    assert place.rest is None


def test_o97_the_rest_of_a_path_is_capped():
    """O97: a record keeps at most 256 characters of the path under the space."""
    roles, entry = S.Roles("/data"), S.MountEntry(40, "0:99", "/data", "fuse.oneclient")
    assert len(roles.place(entry, "/data/orchard/" + "x" * 900).rest) == S.REST_KEPT


def test_o97_a_file_off_the_data_mount_has_no_space_whatever_its_path():
    """O97: space and path are only for the data mount: a file in /work has neither."""
    roles, entry = S.Roles("/data"), S.MountEntry(30, "8:2", "/work", "ext3")
    place = roles.place(entry, "/work/out/f")
    assert (place.mount, place.space, place.rest) == (S.WORK, None, None)


def test_o97_a_chroots_prefix_is_taken_off_a_path_the_kernel_showed_from_the_readers_side():
    """O97: a process chrooted at /run/cell sees /data/x where the reader's link says /run/cell/data/x."""
    assert S.within_root("/run/cell/data/orchard/a", "/run/cell") == "/data/orchard/a"
    assert S.within_root("/run/cell", "/run/cell") == "/"


def test_o97_a_path_not_under_the_root_or_with_no_root_to_go_by_is_as_it_was():
    """O97: `/` and an unknown root strip nothing, and a name that merely starts alike is not under the root."""
    assert S.within_root("/data/a", "/") == "/data/a"
    assert S.within_root("/data/a", "") == "/data/a"
    assert S.within_root("/run/cellar/x", "/run/cell") == "/run/cellar/x"
    assert S.within_root(None, "/run/cell") is None
