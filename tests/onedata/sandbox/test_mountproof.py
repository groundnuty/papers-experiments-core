"""O60: the proof from outside that each cell of a mount run is in a PID namespace of its own, with no host `/proc` and
no protected directory in its mounts, and that oneclient is in the host's namespace; read from a `/proc` in memory."""

import os

from fake_proc import (
    FUSE_DEV,
    HOST_MOUNTPOINT,
    HOST_PROC,
    HOST_TABLE,
    UID,
    FakeProc,
    P,
    cell_process,
    cell_table,
    host_process,
    table,
)
from mountproof_support import ONECLIENT, node, polled

from expcore_onedata.sandbox import mountproof as MP


def test_a_cell_in_its_own_namespace_with_the_runs_mount_and_nothing_protected_is_proved():
    """O60: nested in the watcher's PID namespace, the run's mount in its table, no host `/proc`, nothing private."""
    poll, proofs = polled(node(cell_process(500)))
    assert [(label, ok, problems) for label, ok, problems, _ in proofs] == [("cell", True, [])]
    assert proofs[0][3] == {"pid": 500, "start_time": 1500, "nspid_depth": 2}
    assert poll.failed == 0


def test_a_namespace_is_proved_once_whatever_number_of_processes_it_holds():
    _, proofs = polled(node(cell_process(500), cell_process(501, inner=7), cell_process(502, inner=9)))
    assert len(proofs) == 1


def test_a_hidden_init_does_not_hide_the_cell():
    """The namespace's init may be the setuid starter's, which /proc hides: its children are proved."""
    _, proofs = polled(node(cell_process(501, inner=7)))
    assert [(label, ok) for label, ok, _, _ in proofs] == [("cell", True)]


def test_without_the_namespace_link_each_process_is_proved_on_its_own():
    _, proofs = polled(node(cell_process(500, ns=None), cell_process(501, inner=7, ns=None)))
    assert len(proofs) == 2


def test_two_cells_in_two_namespaces_are_each_proved():
    _, proofs = polled(node(cell_process(500), cell_process(600, ns="pid:[4026532200]")))
    assert len(proofs) == 2


def test_a_process_is_looked_at_once_across_polls():
    proc = node(cell_process(500))
    poll, proofs = polled(proc)
    poll.poll()
    assert len(proofs) == 1


def test_a_nested_process_without_the_runs_mount_is_another_runs_and_not_proved():
    _, proofs = polled(node(cell_process(500, mountinfo=table(("0:70", "/", "/", "overlay")))))
    assert proofs == []


def test_a_process_of_another_uid_is_not_looked_at():
    _, proofs = polled(node(cell_process(500, uid=UID + 1)))
    assert proofs == []


def test_a_cell_that_holds_the_hosts_proc_fails():
    _, proofs = polled(node(cell_process(500, mountinfo=cell_table(("0:20", "/", "/hostproc", "proc")))))
    assert proofs[0][1:3] == (False, [MP.HOST_PROC_IN_CELL])


def test_a_cell_that_binds_a_directory_holding_a_protected_one_fails():
    _, proofs = polled(node(cell_process(500, mountinfo=cell_table(("0:30", "/run", "/x", "tmpfs")))))
    assert proofs[0][1:3] == (False, [MP.PROTECTED_REACHABLE])


def test_a_cell_that_binds_a_directory_under_a_protected_one_fails():
    _, proofs = polled(node(cell_process(500, mountinfo=cell_table(("0:30", "/run/guard/token/a", "/x", "tmpfs")))))
    assert proofs[0][2] == [MP.PROTECTED_REACHABLE]


def test_a_cell_binding_a_sibling_of_a_protected_directory_is_proved():
    _, proofs = polled(node(cell_process(500, mountinfo=cell_table(("0:30", "/run/guard/token2", "/x", "tmpfs")))))
    assert proofs[0][1] is True


def test_a_protected_directory_that_was_not_resolved_fails_every_proof():
    _, proofs = polled(node(cell_process(500)), protected=(MP.Protected(None, None),))
    assert MP.PROTECTED_UNRESOLVED in proofs[0][2]


def test_oneclient_not_in_the_hosts_namespace_fails():
    proc = FakeProc().add(cell_process(ONECLIENT, inner=3), cell_process(500))
    _, proofs = polled(proc)
    assert MP.ONECLIENT_NOT_HOST in [p for _, _, problems, _ in proofs for p in problems]


def test_oneclient_gone_fails():
    _, proofs = polled(FakeProc().add(cell_process(500)))
    assert proofs[0][2] == [MP.ONECLIENT_UNREAD]


def test_a_cell_in_the_hosts_namespace_holding_the_runs_mount_elsewhere_fails():
    """A cell started without a PID namespace of its own: in the node's, with the mount at the cell's path."""
    _, proofs = polled(node(host_process(500, mountinfo=cell_table())))
    assert [(label, ok, problems) for label, ok, problems, _ in proofs] == [
        ("host_process", False, ["same_pid_namespace"])
    ]


def test_a_host_process_holding_the_mount_where_the_host_has_it_is_the_host():
    _, proofs = polled(node(host_process(500, mountinfo=HOST_TABLE)))
    assert proofs == []


def test_a_listing_of_proc_that_fails_is_a_failed_poll():
    proc = node(cell_process(500))
    proc.listing_fails = True
    poll, proofs = polled(proc)
    assert (poll.failed, proofs) == (1, [])


def test_a_nested_process_of_ours_whose_mounts_cannot_be_read_is_a_failed_poll_and_looked_at_again():
    proc = node(cell_process(500, unreadable_mounts=True))
    poll, proofs = polled(proc)
    assert (poll.failed, proofs) == (1, [])
    proc.procs[500].unreadable_mounts = False
    poll.poll()
    assert [ok for _, ok, _, _ in proofs] == [True]


def test_a_cell_still_alive_at_the_close_fails():
    proc = node(cell_process(500))
    poll, proofs = polled(proc)
    poll.close()
    assert [(label, ok, problems) for label, ok, problems, _ in proofs][-1] == ("close", False, ["cell_alive_at_close"])


def test_a_cell_gone_before_the_close_is_not_a_failure():
    proc = node(cell_process(500))
    poll, proofs = polled(proc)
    del proc.procs[500]
    poll.close()
    assert [label for label, *_ in proofs] == ["cell"]


def test_a_pid_taken_over_by_another_process_is_not_the_cell_alive():
    proc = node(cell_process(500))
    poll, proofs = polled(proc)
    proc.procs[500] = P(500, 9999, (500,))  # another process, started later, under the same pid
    poll.close()
    assert [label for label, *_ in proofs] == ["cell"]


def test_the_run_mount_and_proc_devices_come_from_the_hosts_table():
    assert MP.fuse_dev(HOST_TABLE, HOST_MOUNTPOINT) == FUSE_DEV
    assert MP.fuse_dev(HOST_TABLE, "/elsewhere") is None
    assert MP.proc_devs(HOST_TABLE) == {HOST_PROC[0]}


def test_a_protected_directory_resolves_to_its_device_and_its_path_in_its_file_system(tmp_path):
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        mountinfo = stream.read()
    found = MP.protected_at(str(tmp_path), mountinfo)
    info = os.stat(tmp_path)
    assert found.dev == f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
    assert found.fs_path is not None
    assert found.fs_path.endswith(tmp_path.name)


def test_a_directory_on_no_mount_of_the_table_is_unresolved(tmp_path):
    assert MP.protected_at(str(tmp_path), table(("9:9", "/", "/", "ext4"))) == MP.Protected(None, None)


def test_a_cell_first_seen_before_its_mounts_are_made_is_proved_once_they_are():
    """O60 (Codex, round 1): Apptainer may still be binding when a cell's process is first seen; a later poll finds the
    run's mount, and the host's `/proc` beside it, and fails the proof."""
    proc = node(cell_process(500, mountinfo=table(("0:70", "/", "/", "overlay"))))
    poll, proofs = polled(proc)
    assert proofs == []
    proc.procs[500].mountinfo = cell_table(("0:20", "/", "/hostproc", "proc"))
    poll.poll()
    assert [(label, ok, problems) for label, ok, problems, _ in proofs] == [("cell", False, [MP.HOST_PROC_IN_CELL])]


def test_a_problem_a_later_look_finds_in_a_proved_namespace_is_a_failed_proof():
    proc = node(cell_process(500))
    poll, proofs = polled(proc)
    proc.procs[500].mountinfo = cell_table(("0:30", "/run", "/x", "tmpfs"))
    poll.poll()
    poll.poll()  # written once, not at every look
    assert [(ok, problems) for _, ok, problems, _ in proofs] == [(True, []), (False, [MP.PROTECTED_REACHABLE])]


def test_a_process_of_ours_is_looked_at_again_at_every_poll_however_long_it_lives():
    """O60 (Codex, round 2): no count of polls shows Apptainer done with a cell's mounts; a change at any later poll is
    seen."""
    proc = node(cell_process(500, mountinfo=table(("0:70", "/", "/", "overlay"))))
    poll, proofs = polled(proc)
    for _ in range(300):
        poll.poll()
    proc.procs[500].mountinfo = cell_table(("0:20", "/", "/hostproc", "proc"))
    poll.poll()
    assert [(label, ok, problems) for label, ok, problems, _ in proofs] == [("cell", False, [MP.HOST_PROC_IN_CELL])]


def test_a_process_of_another_uid_that_becomes_ours_is_looked_at_within_a_second():
    """A setuid starter's process runs as root, then as the user, under one pid."""
    proc = node(cell_process(500, uid=0))
    poll, proofs = polled(proc)
    proc.procs[500].uid = UID
    for _ in range(MP.OTHER_EVERY):
        poll.poll()
    assert [(label, ok) for label, ok, _, _ in proofs] == [("cell", True)]
