"""The survivors' diagnostics read from a fake `/proc` (`expcore_onedata.sandbox.apptainer.stuckread`)."""

import threading
import time

import pytest
from fake_survivor_proc import DATA_DEVICE, DATA_MOUNT, ROOT_MOUNT, WORK_MOUNT, FakeProcReader, syscall_text

from expcore_onedata.sandbox import attribution as A
from expcore_onedata.sandbox.apptainer import stuck as S
from expcore_onedata.sandbox.apptainer import stuckread as R
from expcore_onedata.sandbox.apptainer import stuckrecord as REC

ROLES = S.Roles("/data", device=DATA_DEVICE, inputs=("/opt/inputs",))
READ_FD3 = syscall_text(0, 3, 0x7F10, 0x1000)
OPENAT_CWD = syscall_text(257, 0xFFFFFFFFFFFFFF9C, 0x7F20)
MMAP = syscall_text(9, 0, 4096, 1, 1, 3, 0)  # mmap: a call no list names
ACCESS = syscall_text(21, 0x7F20, 4)  # access(path, mode): a path call the lists do not name
BLOCKED_OUTSIDE = "-1 0x7ffd1000 0x7f110000\n"


def survivors(*pids: int, start: str = "5000") -> list[dict]:
    return [{"pid": pid, "start": start, "comm": "python3", "state": "D"} for pid in pids]


def read(view: FakeProcReader, left: list[dict], **kwargs) -> dict:
    return R.diagnose(view, left, 5.0, ROLES, machine="x86_64", **kwargs)


def spaces(found: dict, process: int = 0, task: int = 0):
    """The (mount, space) of each object of one task's `blocking`; None when the task has none."""
    blocking = found["reading"][process]["tasks"][task]["blocking"]
    return None if blocking is None else [(o["mount"], o["space"]) for o in blocking["objects"]]


def blocking(found: dict, process: int = 0, task: int = 0):
    return found["reading"][process]["tasks"][task]["blocking"]


def reader_in_d(view: FakeProcReader, pid: int = 100, *, target: str = "/data/orchard/a.bin", mnt_id: int = DATA_MOUNT):
    """A process with one task in D, blocked in read(2) of descriptor 3."""
    return view.process(pid).task(pid, syscall=READ_FD3, wchan="request_wait_answer").fd(3, target, mnt_id=mnt_id)


def test_o97_a_reader_blocked_on_a_data_file_is_found_by_its_descriptor():
    """O97: a D task in read(3) on the data mount waits on that file, in its space, with its offset and flags."""
    view = FakeProcReader()
    reader_in_d(view)
    found = read(view, survivors(100))
    fd_object = {"via": "fd", "cut": False, "objects": [{"mount": "data", "space": "orchard", "path": "a.bin"}]}
    assert blocking(found) == fd_object
    survivor = found["survivors"][0]
    # the verdict read `blocking` (in the `reading`, above); the record's closed list of task keys holds none
    assert survivor["tasks"] == [{"state": "D", "wchan": "request_wait_answer", "syscall": {"nr": 0, "fd": 3}}]
    assert survivor["waits_on"] == [
        {
            "fd": 3,
            "mount": "data",
            "fstype": "fuse.oneclient",
            "space": "orchard",
            "path": "a.bin",
            "pos": 0,
            "flags": "0100000",
        }
    ]


def test_o97_a_wchan_of_zero_is_recorded_as_none():
    """O97: some kernels read `0` for a task's wait channel (H0 on the el8 nodes); it is recorded as None."""
    view = FakeProcReader()
    view.process(100).task(100, syscall=READ_FD3, wchan="0").fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    assert read(view, survivors(100))["survivors"][0]["tasks"][0]["wchan"] is None


def test_o97_a_zombie_leader_with_a_thread_in_d_is_reported_by_its_thread():
    """O97: the leader is Z and holds nothing; the thread in D is what is blocked, and is the survivor's task."""
    view = FakeProcReader()
    process = view.process(100, state="Z")
    process.task(100, state="Z").task(101, syscall=READ_FD3, wchan="request_wait_answer")
    process.fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    found = read(view, survivors(100))
    assert found["reading"][0]["state"] == "Z"
    assert [t["state"] for t in found["reading"][0]["tasks"]] == ["D"]  # the zombie leader is not listed
    assert spaces(found) == [("data", "orchard")]
    assert [t["state"] for t in found["survivors"][0]["tasks"]] == ["D"]


def test_o97_a_task_that_is_not_in_d_has_no_object():
    """O97: only a task in D has a blocking object; a sleeping one does not, and the survivor shows it."""
    view = FakeProcReader()
    view.process(100, state="S").task(100, state="S", syscall=READ_FD3).fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    found = read(view, survivors(100))
    assert [t["state"] for t in found["reading"][0]["tasks"]] == ["S"]
    assert blocking(found) is None


def test_o97_a_read_on_the_work_directory_is_not_the_data_mount():
    """O97: a D task reading a file in /work waits on the work role, with no space."""
    view = FakeProcReader()
    reader_in_d(view, target="/work/out.bin", mnt_id=WORK_MOUNT)
    objects = spaces(read(view, survivors(100)))
    assert objects == [("work", None)]


def test_o97_a_descriptor_on_a_pipe_is_on_no_known_mount_so_it_is_other():
    """O97: a pipe's link names no path; it is placed by its mount id alone, here the root mount: `other`."""
    view = FakeProcReader()
    reader_in_d(view, target="pipe:[1234]", mnt_id=ROOT_MOUNT)
    objects = spaces(read(view, survivors(100)))
    assert objects == [("other", None)]


def test_o97_a_descriptor_whose_fdinfo_is_missing_is_unknown():
    """O97: with no mount id for the descriptor its object is unknown, never guessed from its path."""
    view = FakeProcReader()
    reader_in_d(view)
    view.processes[100].fds[3] = ("/data/orchard/a", "pos:\t0\n")
    assert spaces(read(view, survivors(100))) is None


def test_o97_an_unreadable_syscall_is_never_guessed_from_what_the_process_holds():
    """O97: a D task whose `syscall` cannot be read has no object, though the process holds a data file."""
    view = FakeProcReader()
    reader_in_d(view)
    del view.processes[100].tasks[100]["syscall"]
    found = read(view, survivors(100))
    assert spaces(found) is None
    assert found["survivors"][0]["tasks"][0]["syscall"] == "unreadable"


def test_o97_an_openat_is_placed_by_the_path_read_from_the_tasks_memory():
    """O97: the path argument is read at the syscall's pointer and placed on the data mount, in its space."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=OPENAT_CWD).mem_at(100, 0x7F20, "/data/orchard/dir/../b.txt\0")
    process.fd(3, "/work/x", mnt_id=WORK_MOUNT)
    found = read(view, survivors(100))
    assert spaces(found) == [("data", "orchard")]
    assert found["survivors"][0]["waits_on"] == [
        {"mount": "data", "fstype": "fuse.oneclient", "space": "orchard", "path": "b.txt"}
    ]


def test_o97_a_relative_path_is_placed_from_the_working_directory():
    """O97: a path relative to AT_FDCWD is placed from the task's working directory."""
    view = FakeProcReader()
    view.process(100, cwd="/data/orchard").task(100, syscall=OPENAT_CWD).mem_at(100, 0x7F20, "sub/f\0")
    assert spaces(read(view, survivors(100))) == [("data", "orchard")]


def test_o97_a_relative_path_is_placed_from_its_directory_descriptor():
    """O97: a path relative to a directory descriptor is placed from that descriptor's link."""
    view = FakeProcReader()
    process = view.process(100, cwd="/work").task(100, syscall=syscall_text(257, 5, 0x7F20))
    process.fd(5, "/data/meadow", mnt_id=DATA_MOUNT).mem_at(100, 0x7F20, "f\0")
    assert spaces(read(view, survivors(100))) == [("data", "meadow")]


def test_o97_a_path_with_no_nul_in_its_read_is_unknown():
    """O97: a path argument with no NUL in the 4 KiB read is unknown, never a cut path."""
    view = FakeProcReader()
    view.process(100).task(100, syscall=OPENAT_CWD).mem_at(100, 0x7F20, "/data/orchard/" + "x" * 5000)
    assert spaces(read(view, survivors(100))) is None


def test_o97_a_path_in_memory_that_cannot_be_read_is_unknown():
    """O97: a task memory that cannot be read at the pointer leaves the path call's object unknown."""
    view = FakeProcReader()
    view.process(100).task(100, syscall=OPENAT_CWD)
    assert spaces(read(view, survivors(100))) is None


def test_o97_a_task_blocked_outside_a_syscall_is_read_by_what_the_process_holds():
    """O97: for `-1`, the holdings rule: the FUSE files the process holds, one entry for each space."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=BLOCKED_OUTSIDE)
    process.fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT).fd(4, "/data/meadow/b", mnt_id=DATA_MOUNT)
    process.fd(5, "/work/out", mnt_id=WORK_MOUNT)
    objects = spaces(read(view, survivors(100)))
    assert objects == [("data", "orchard"), ("data", "meadow")]


def test_o97_a_mapped_file_on_the_data_mount_is_a_holding():
    """O97: a file the process mapped from the data mount counts, as a page fault on it can be what is blocked."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=BLOCKED_OUTSIDE).maps("/data/orchard/lib.so", "/usr/lib/x.so")
    assert process is not None
    assert spaces(read(view, survivors(100))) == [("data", "orchard")]


def test_o97_a_working_directory_on_the_data_mount_is_a_holding():
    """O97: the working directory counts too, when it is on the data mount."""
    view = FakeProcReader()
    view.process(100, cwd="/data/meadow/run").task(100, syscall=BLOCKED_OUTSIDE)
    assert spaces(read(view, survivors(100))) == [("data", "meadow")]


def test_o97_a_process_that_holds_no_fuse_object_has_unknown_holdings():
    """O97: holdings rule with nothing held on a FUSE mount: unknown, since nothing says what it is blocked on."""
    view = FakeProcReader()
    view.process(100).task(100, syscall=BLOCKED_OUTSIDE).fd(3, "/work/out", mnt_id=WORK_MOUNT)
    assert spaces(read(view, survivors(100))) is None


def test_o97_a_process_with_a_descriptor_that_cannot_be_read_has_unknown_holdings():
    """O97: one descriptor whose link cannot be read makes the whole holdings set unknown, never a partial one."""
    view = FakeProcReader()
    process = (
        view.process(100)
        .task(100, syscall=BLOCKED_OUTSIDE)
        .fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
        .fd(4, "/x", mnt_id=1)
    )
    process.fds[4] = ("/x", "garbage")  # no mnt_id in its fdinfo
    assert spaces(read(view, survivors(100))) is None


def test_o97_a_process_with_more_descriptors_than_are_read_has_unknown_holdings():
    """O97: past the number of descriptors read, the holdings are unknown: a partial set is never judged."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=BLOCKED_OUTSIDE)
    for number in range(R.FDS_READ + 1):
        process.fd(number, "/work/f", mnt_id=WORK_MOUNT)
    assert spaces(read(view, survivors(100))) is None


def test_o97_a_non_x86_machine_never_reads_a_syscall_number_as_a_call():
    """O97: the tables are x86_64's; on another machine a call's number says nothing, so no object is resolved (only a
    task blocked outside any syscall, which reads `-1` everywhere, has its holdings)."""
    view = FakeProcReader()
    reader_in_d(view)
    found = R.diagnose(view, survivors(100), 5.0, ROLES, machine="aarch64")
    assert found["survivors"][0]["tasks"][0]["syscall"] == {"nr": 0}
    assert blocking(found) is None
    other = FakeProcReader()
    other.process(100).task(100, syscall=BLOCKED_OUTSIDE).fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    assert spaces(R.diagnose(other, survivors(100), 5.0, ROLES, machine="aarch64")) == [("data", "orchard")]


def test_o97_a_survivor_whose_pid_was_taken_over_is_unknown():
    """O97: another process at the pid (another start time) is not the survivor: its tasks are unknown."""
    view = FakeProcReader()
    reader_in_d(view)
    found = read(view, survivors(100, start="9999"))
    assert found["reading"][0]["tasks"] is None
    assert (found["reading"][0]["pid"], found["reading"][0]["start"]) == (100, "9999")  # as the deadline listed it
    assert "tasks" not in found["survivors"][0]


def test_o97_a_survivor_that_cannot_be_read_is_unknown():
    """O97: a process `/proc` hides is unknown, not read as one that waits on nothing."""
    view = FakeProcReader()
    reader_in_d(view).hidden = True
    found = read(view, survivors(100))
    assert found["reading"][0]["tasks"] is None
    assert "tasks" not in found["survivors"][0]


def test_o97_the_record_keeps_eight_survivors_but_the_reading_has_every_one():
    """O97: the record's list is capped at 8 and says how many it cut; the `reading`, which the verdict is made on, is
    not."""
    view = FakeProcReader()
    for pid in range(100, 110):
        reader_in_d(view, pid)
    found = read(view, survivors(*range(100, 110)))
    assert len(found["survivors"]) == REC.SURVIVORS_KEPT
    assert found["survivors_cut"] == 2
    assert len(found["reading"]) == 10


def test_o97_the_record_keeps_four_tasks_with_the_blocked_ones_first():
    """O97: each survivor shows at most 4 tasks, those in D before the others; the reading lists every task."""
    view = FakeProcReader()
    process = view.process(100)
    for tid in range(100, 106):
        process.task(tid, state="S")
    process.task(105, state="D", syscall=READ_FD3).fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    found = read(view, survivors(100))
    assert found["survivors"][0]["tasks"][0]["state"] == "D"
    assert len(found["survivors"][0]["tasks"]) == REC.TASKS_KEPT
    assert len(found["reading"][0]["tasks"]) == 6
    assert found["reading"][0]["tasks_cut"] == 0
    assert found["survivors"][0]["tasks_cut"] == 2


def test_o97_a_data_file_in_a_second_space_gives_a_second_object_for_its_task():
    """O97: the objects of one task are listed by role and space, each once."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=BLOCKED_OUTSIDE)
    process.fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT).fd(4, "/data/orchard/b", mnt_id=DATA_MOUNT)
    assert spaces(read(view, survivors(100))) == [("data", "orchard")]


def test_o97_a_record_has_no_pid_no_command_line_and_no_node_path():
    """O97: what is recorded (`survivors`; the `reading` is never recorded) names no pid, no start, no command line, no
    environment and no path of the node."""
    view = FakeProcReader()
    reader_in_d(view)
    text = repr(read(view, survivors(100))["survivors"])
    assert "100" not in text.replace("0100000", "")  # the pid
    assert "5000" not in text  # the start
    assert "/proc" not in text
    assert "cmdline" not in text
    assert "environ" not in text


def test_o97_the_reader_opens_only_proc_status_files_never_a_file_the_process_waits_on():
    """O97: the names read are /proc's own (stat, task list, wchan, syscall, links, mountinfo, fdinfo, memory)."""
    view = FakeProcReader()
    reader_in_d(view)
    view.processes[100].task(101, syscall=OPENAT_CWD).mem_at(101, 0x7F20, "/data/orchard/x\0")
    read(view, survivors(100))
    tails = {name.split("/", 3)[-1].split("/")[0] for name in view.names}
    assert tails <= {
        "stat",
        "task",
        "100",
        "101",
        "wchan",
        "syscall",
        "mountinfo",
        "cwd",
        "root",
        "fd",
        "fdinfo",
        "mem",
        "maps",
    }
    assert not any(name.startswith("/") or "orchard" in name for name in view.names)  # never the target's own name


def test_o97_the_reader_makes_no_read_of_memory_for_a_read_family_call():
    """O97: a read(2) is placed by its descriptor alone: the task's memory and `maps` are not opened."""
    view = FakeProcReader()
    reader_in_d(view)
    read(view, survivors(100))
    assert not any(name.endswith("/mem") or name.endswith("/maps") for name in view.names)


def test_o97_the_cheap_files_of_every_survivor_are_read_before_any_memory():
    """O97: `maps` and the task's memory come last, so one that blocks cannot starve another survivor's cheap reads."""
    view = FakeProcReader()
    view.process(100).task(100, syscall=OPENAT_CWD).mem_at(100, 0x7F20, "/data/orchard/x\0")
    reader_in_d(view, 200)
    read(view, survivors(100, 200))
    last_cheap = max(i for i, name in enumerate(view.names) if name.endswith(("/syscall", "/fdinfo/3", "/mountinfo")))
    assert min(i for i, name in enumerate(view.names) if name.endswith("/mem")) > last_cheap


def test_o97_a_memory_read_that_blocks_leaves_its_object_unknown_within_the_margin():
    """O97: a read that blocks (memory locks the address space) costs at most the margin, and the object is unknown."""
    view = FakeProcReader()
    view.process(100).task(100, syscall=OPENAT_CWD).mem_at(100, 0x7F20, "/data/orchard/x\0")
    gate = view.stall["100/task/100/mem"] = threading.Event()
    began = time.monotonic()
    found = R.diagnose(view, survivors(100), 0.3, ROLES, machine="x86_64")
    gate.set()
    assert time.monotonic() - began < 5
    assert blocking(found) is None


def test_o97_a_diagnosis_with_no_time_or_no_survivors_reads_nothing():
    """O97: no survivors, or no time left, gives None and opens nothing."""
    view = FakeProcReader()
    reader_in_d(view)
    assert R.diagnose(view, [], 5.0, ROLES) is None
    assert R.diagnose(view, survivors(100), 0.0, ROLES) is None
    assert view.names == []


def test_o97_the_connection_is_recorded_when_the_run_gives_one():
    """O97: the run's fusectl state is the record's `connection`."""
    view = FakeProcReader()
    reader_in_d(view)
    found = read(view, survivors(100), connection=lambda: {"fusectl": "writable", "device": DATA_DEVICE})
    assert found["connection"] == {"fusectl": "writable"}


@pytest.mark.parametrize("given", [None, {"fusectl": "perhaps"}, {}, "writable"])
def test_o97_a_connection_that_is_not_a_known_state_is_left_out(given):
    """O97: a fusectl state that is not one of the three is not recorded."""
    view = FakeProcReader()
    reader_in_d(view)
    assert "connection" not in read(view, survivors(100), connection=lambda: given)


def test_o97_a_connection_that_raises_costs_only_the_key():
    """O97: a failing connection reading leaves the survivors' diagnosis whole."""
    view = FakeProcReader()
    reader_in_d(view)

    def broken():
        raise RuntimeError("no")

    found = read(view, survivors(100), connection=broken)
    assert "connection" not in found
    assert spaces(found)


def test_o97_a_node_with_no_view_or_a_run_with_no_diagnosis_gives_none():
    """O97: `diagnose_node` reads only where the node has a `/proc` view and the run gave its targets."""
    view = FakeProcReader()
    reader_in_d(view)

    class Bare:
        pass

    class Viewing:
        def proc_reader(self):
            return view

    diagnosis = R.Diagnosis(roles=lambda: ROLES)
    assert R.diagnose_node(Bare(), diagnosis, survivors(100), 5.0) is None
    assert R.diagnose_node(Viewing(), None, survivors(100), 5.0) is None
    assert R.diagnose_node(Viewing(), diagnosis, survivors(100), 5.0)["reading"][0]["state"] == "D"


def test_o97_the_local_view_reads_a_real_directory(tmp_path):
    """O97: the node's own view lists, reads and links by names under its root, and gives None for what is not there."""
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "stat").write_text("x")
    (tmp_path / "7" / "mem").write_bytes(b"0123456789")
    (tmp_path / "7" / "link").symlink_to("/data/orchard/a")
    view = R.LocalProcReader(str(tmp_path))
    assert view.listdir("7") is not None
    assert sorted(view.listdir("7")) == ["link", "mem", "stat"]
    assert view.read("7/stat", 10) == b"x"
    assert view.readlink("7/link") == "/data/orchard/a"
    assert view.read_at("7/mem", 3, 4) == b"3456"
    assert view.read("8/stat", 10) is None
    assert view.readlink("8/x") is None
    assert view.read_at("8/mem", 0, 1) is None
    assert view.read_at("7/mem", 2**70, 1) is None


def test_o97_a_chrooted_process_is_placed_by_its_own_view_of_the_paths():
    """O97: a cell chrooted at a directory shows its descriptors, mappings and working directory with that prefix; the
    reader takes it off, so the file is in its space and not on an unknown mount."""
    view = FakeProcReader()
    process = view.process(100, cwd="/run/cell/work").task(100, syscall=BLOCKED_OUTSIDE)
    process.root = "/run/cell"
    process.fd(3, "/run/cell/data/orchard/a", mnt_id=DATA_MOUNT).maps("/run/cell/data/orchard/lib.so")
    found = read(view, survivors(100))
    assert spaces(found) == [("data", "orchard")]
    assert found["survivors"][0]["waits_on"][0]["path"] == "a"
    assert found["survivors"][0]["cwd_mount"] == "work"


def test_o97_a_descriptor_whose_path_is_not_under_the_data_mount_has_the_data_mount_and_no_space():
    """O97: the signature of a chroot the reader did not know (to hand to whoever reads the live record): the mount id
    says `data` while the path is not under the mount point, so the space is None and the attribution fails closed."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=READ_FD3)
    process.fd(3, "/run/cell/data/orchard/a", mnt_id=DATA_MOUNT)
    assert spaces(read(view, survivors(100))) == [("data", None)]


def test_o97_a_read_that_blocks_returns_before_the_time_given_so_the_deadline_still_gets_what_was_read():
    """O97: a memory read that never returns is given up on at 0.8 of the time given, short of the deadline's own join,
    which would otherwise expire first and throw away the reading."""
    view = FakeProcReader()
    view.process(100).task(100, syscall=OPENAT_CWD).mem_at(100, 0x7F20, "/data/orchard/x\0")
    gate = view.stall["100/task/100/mem"] = threading.Event()
    began = time.monotonic()
    found = R.diagnose(view, survivors(100), 2.0, ROLES, machine="x86_64")
    took = time.monotonic() - began
    gate.set()
    assert 1.5 < took < 2.0
    assert found["reading"][0]["tasks"][0]["blocking"] is None


def test_o97_the_time_the_deadline_gives_is_the_time_the_diagnosis_honours():
    """O97: the bound is the one the deadline passes, less a margin that is a fraction of it, never a constant of this
    module's own."""
    assert 0 < R.DIAGNOSE_SHARE < 1
    assert not hasattr(R, "DIAGNOSE_MAX_S")


def test_o97_a_process_with_a_root_that_cannot_be_read_is_taken_as_rooted_at_slash():
    """O97: with no root link, the paths are read as they are."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=READ_FD3).fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    process.root = ""
    assert spaces(read(view, survivors(100))) == [("data", "orchard")]


def test_o97_each_resolution_says_how_it_was_made():
    """O97: a read-family call is `via` fd, a path call `via` path, and a task outside any syscall `via` holdings."""
    view = FakeProcReader()
    reader_in_d(view, 100)
    view.process(200, start="6000").task(200, syscall=OPENAT_CWD).mem_at(200, 0x7F20, "/data/orchard/x\0")
    process = view.process(300, start="7000").task(300, syscall=BLOCKED_OUTSIDE)
    process.fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    left = [*survivors(100), *survivors(200, start="6000"), *survivors(300, start="7000")]
    found = read(view, left)
    assert [blocking(found, i)["via"] for i in range(3)] == ["fd", "path", "holdings"]


def test_o97_a_call_the_lists_do_not_name_is_never_read_by_what_the_process_holds():
    """O97: access(2), mmap(2) and the like may wait on a path the process does not hold: blocking is None, though the
    process holds a data file."""
    for call in (ACCESS, MMAP):
        view = FakeProcReader()
        process = view.process(100).task(100, syscall=call)
        process.fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT).mem_at(100, 0x7F20, "/data/meadow/x\0")
        assert blocking(read(view, survivors(100))) is None


def test_o97_the_holdings_name_each_space_once_and_carry_no_path():
    """O97: many files of one space are one object, and a holding has no path of its own."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=BLOCKED_OUTSIDE)
    process.fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT).fd(4, "/data/orchard/b", mnt_id=DATA_MOUNT)
    process.maps("/data/orchard/lib.so")
    assert blocking(read(view, survivors(100)))["objects"] == [{"mount": "data", "space": "orchard", "path": None}]


def test_o97_holdings_past_the_cap_are_flagged_cut_so_they_are_never_believed():
    """O97: more distinct objects than are kept make `cut` true, which the attribution reads as unknown."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=BLOCKED_OUTSIDE)
    for number in range(REC.OBJECTS_KEPT + 1):
        process.fd(number + 3, f"/data/space-{number}/a", mnt_id=DATA_MOUNT)
    found = read(view, survivors(100))
    assert blocking(found)["cut"] is True
    assert len(blocking(found)["objects"]) == REC.OBJECTS_KEPT


def test_o97_the_reading_has_the_pid_and_start_exactly_as_the_deadline_listed_them_for_every_survivor():
    """O97: one entry for each process listed, in order, whether or not it could be read."""
    view = FakeProcReader()
    reader_in_d(view, 100)
    left = [*survivors(100), {"pid": 555, "start": "123", "comm": "gone", "state": "D"}]
    found = read(view, left)
    assert [(e["pid"], e["start"]) for e in found["reading"]] == [(100, "5000"), (555, "123")]
    assert found["reading"][1]["tasks"] is None


def test_o97_a_survivor_not_reached_before_the_diagnosis_was_given_up_is_still_listed_unknown():
    """O97: a read that blocks leaves the survivors after it unread, and each is in the reading with no tasks."""
    view = FakeProcReader()
    view.process(100).task(100, syscall=OPENAT_CWD).mem_at(100, 0x7F20, "/data/orchard/x\0")
    reader_in_d(view, 200)
    gate = view.stall["100/task/100/mem"] = threading.Event()
    found = R.diagnose(view, survivors(100, 200), 0.3, ROLES, machine="x86_64")
    gate.set()
    assert [(e["pid"], e["start"]) for e in found["reading"]] == [(100, "5000"), (200, "5000")]
    assert found["reading"][0]["tasks"][0]["blocking"] is None


def test_o97_the_readers_call_tables_are_the_attributions():
    """O97: the calls the reader resolves are exactly the calls the attribution believes, so neither side can disagree
    about a syscall number and fail every run closed."""
    from expcore_onedata.sandbox import attribution

    assert set(S.FD_CALLS) == attribution.FD_CALLS
    assert set(S.PATH_CALLS) == attribution.PATH_CALLS
    assert S.OUTSIDE_A_SYSCALL == attribution.OUTSIDE_A_SYSCALL


def test_o97_a_reader_blocked_past_the_join_returns_what_it_read_before_the_time_given():
    """O97: one survivor is read whole and another waits on a task-memory read that never returns: the diagnosis is
    back before `timeout_s`, so the deadline's own join (the same bound) has not discarded it, with the first
    survivor's object resolved and the second's unknown."""
    view = FakeProcReader()
    reader_in_d(view, 100)
    view.process(200, start="6000").task(200, syscall=OPENAT_CWD).mem_at(200, 0x7F20, "/data/orchard/x\0")
    gate = view.stall["200/task/200/mem"] = threading.Event()
    began = time.monotonic()
    found = R.diagnose(view, [*survivors(100), *survivors(200, start="6000")], 2.0, ROLES, machine="x86_64")
    took = time.monotonic() - began
    gate.set()
    assert took < 2.0
    assert spaces(found, process=0) == [("data", "orchard")]
    assert blocking(found, process=1) is None
    assert [(e["pid"], e["start"]) for e in found["reading"]] == [(100, "5000"), (200, "6000")]


def test_o97_the_record_keeps_no_blocking_object_of_a_task_and_the_reading_keeps_it():
    """O97: the closed list of a recorded task's keys (`records.cell_deadline.TASK_KEYS`) has no `blocking`: it is what
    the attribution reads, in the uncapped `reading`, and never what the record keeps, though it passes the schema's
    check as the deadline's record."""
    view = FakeProcReader()
    reader_in_d(view)
    found = read(view, survivors(100))
    assert found["reading"][0]["tasks"][0]["blocking"] is not None
    assert all("blocking" not in task for task in found["survivors"][0]["tasks"])


def verdict_of(found: dict) -> str:
    """The attribution's reason for a reading, with the one space `orchard` held down."""
    return A.attribute(found, {"providers[0]"}, {"orchard": "providers[0]"})["why"]


def test_o97_a_task_whose_state_cannot_be_read_makes_the_verdict_unknown_not_agent():
    """O97 (Codex, final review): a listed task whose `stat` cannot be read may be one in D, so it is never dropped
    from the reading: the survivor says it left a task out, and the verdict is `blocking_object_unknown` even when
    every task that was read waits on the held-down space."""
    view = FakeProcReader()
    process = reader_in_d(view)
    process.tasks[101] = {"wchan": "0", "syscall": READ_FD3}  # listed, with no `stat` to read
    found = read(view, survivors(100))
    assert found["reading"][0]["tasks_cut"] == 1
    assert verdict_of(found) == A.UNKNOWN
    assert found["survivors"][0]["tasks_cut"] == 1  # the record says one task was left out


def test_o97_a_maps_file_longer_than_the_read_limit_makes_the_holdings_unknown(monkeypatch):
    """O97 (Codex, final review): a `maps` read that hit its limit is a partial set, never judged: a mapping past
    the cut in another space could be the one a page fault waits on."""
    monkeypatch.setattr(R, "MAPS_LIMIT", 400)
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=BLOCKED_OUTSIDE).fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    process.maps(*[f"/usr/lib/lib{n}.so" for n in range(12)], "/data/meadow/late.so")
    assert spaces(read(view, survivors(100))) is None


def test_o97_a_maps_file_that_just_fits_the_read_limit_is_read_whole(monkeypatch):
    """O97: the limit is on what is read past it, so a file of exactly the limit is whole, not cut."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=BLOCKED_OUTSIDE).fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    process.maps("/data/orchard/lib.so")
    size = len(process.maps_text())
    monkeypatch.setattr(R, "MAPS_LIMIT", size)
    assert spaces(read(view, survivors(100))) == [("data", "orchard")]


def test_o97_a_working_directory_that_cannot_be_read_makes_the_holdings_unknown():
    """O97 (Codex, final review): the working directory is one of the holdings; an unreadable one is not left out of the
    set, it makes the set unknown: it could be in another space."""
    view = FakeProcReader()
    process = view.process(100).task(100, syscall=BLOCKED_OUTSIDE).fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT)
    process.cwd = None  # its link cannot be read
    found = read(view, survivors(100))
    assert spaces(found) is None
    assert verdict_of(found) == A.UNKNOWN
