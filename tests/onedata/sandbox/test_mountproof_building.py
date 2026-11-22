"""O60: a cell Apptainer is still building is not judged. Until a nested process pivots into its own root its mount
table is the host's (or a copy of it), with the host's `/proc` and protected directories; the first look at a new cell,
a fraction of a second after its launch, failed a proof on that. The process is looked at again; one never seen
otherwise was never proved, which is a failed poll."""

from fake_proc import FUSE_DEV, HOST_PROC, HOST_ROOT, HOST_TABLE, cell_process, cell_table, host_process, table
from mountproof_support import HOST_ROOT_MOUNT, WATCHER_MNT_NS, host, node, polled

from expcore_onedata.sandbox import mountproof as MP

SEEN = {"root_mount": HOST_ROOT_MOUNT, "mnt_ns": WATCHER_MNT_NS}  # what the watcher learned of the host at its start
COPY_NS = "mnt:[4026532400]"  # a mount namespace of the cell's own, made by an unshare that has copied the host's table
OTHER_PID_NS = "pid:[4026532200]"


def building(pid: int, **kw):
    """A nested process whose mount namespace is its own but whose table is still the host's copy: the pivot is not
    done."""
    return cell_process(pid, mountinfo=HOST_TABLE, mnt_ns=COPY_NS, **kw)


def pivoted(proc, pid: int, *extra) -> None:
    proc.procs[pid].mountinfo = cell_table(*extra)


def test_a_process_that_is_still_a_copy_of_the_host_is_not_judged_and_is_proved_once_it_pivots():
    """O60: the live case, a first look 0.17 s after the launch found the host's `/proc` and protected directories."""
    proc = node(building(500))
    poll, proofs = polled(proc, **SEEN)
    assert proofs == []
    pivoted(proc, 500)
    poll.poll()
    assert [(label, ok, problems) for label, ok, problems, _ in proofs] == [("cell", True, [])]
    assert poll.failed == 0


def test_a_process_in_the_watchers_own_mount_namespace_is_not_judged_whatever_its_table():
    """Before it unshares a mount namespace of its own, its table is the watcher's: not a cell's."""
    proc = node(cell_process(500, mnt_ns=WATCHER_MNT_NS))
    poll, proofs = polled(proc, **SEEN)
    assert proofs == []
    proc.procs[500].mnt_ns = COPY_NS
    poll.poll()
    assert [ok for _, ok, _, _ in proofs] == [True]


def test_a_cell_that_has_the_hosts_proc_after_the_pivot_still_fails():
    """Only a cell still being built is passed over: its own root with a bind of the host's `/proc` is the failure."""
    proc = node(building(500))
    poll, proofs = polled(proc, **SEEN)
    pivoted(proc, 500, (HOST_PROC[0], "/", "/hostproc", "proc"))
    poll.poll()
    assert [(label, ok, problems) for label, ok, problems, _ in proofs] == [("cell", False, [MP.HOST_PROC_IN_CELL])]
    assert [e["point"] for e in proofs.details[0]["offending"]] == ["other"]


def test_a_problem_found_after_the_pivot_in_a_proved_namespace_is_still_a_failed_proof():
    proc = node(building(500))
    poll, proofs = polled(proc, **SEEN)
    pivoted(proc, 500)
    poll.poll()
    pivoted(proc, 500, ("0:30", "/run", "/x", "tmpfs"))
    poll.poll()
    assert [(ok, problems) for _, ok, problems, _ in proofs] == [(True, []), (False, [MP.PROTECTED_REACHABLE])]


def test_a_namespace_seen_only_while_being_built_is_a_failed_poll_when_it_ends_and_no_failed_proof():
    proc = node(building(500))
    poll, proofs = polled(proc, **SEEN)
    assert poll.failed == 0
    del proc.procs[500]
    poll.poll()
    assert (poll.failed, proofs) == (1, [])


def test_a_cell_being_built_is_no_failed_poll_at_every_look():
    proc = node(building(500))
    poll, _ = polled(proc, **SEEN)
    for _ in range(50):
        poll.poll()
    assert poll.failed == 0


def test_a_failed_poll_is_counted_once_for_a_namespace_whose_every_process_ends():
    proc = node(building(500), building(501, inner=7))
    poll, _ = polled(proc, **SEEN)
    del proc.procs[500], proc.procs[501]
    poll.poll()
    poll.poll()
    assert poll.failed == 1


def test_a_namespace_still_being_built_at_the_close_is_a_failed_poll_and_a_cell_alive():
    proc = node(building(500))
    poll, proofs = polled(proc, **SEEN)
    poll.close()
    assert poll.failed == 1
    assert [(label, problems) for label, _, problems, _ in proofs] == [("close", [MP.CELL_ALIVE_AT_CLOSE])]


def test_a_setup_helper_that_ends_in_a_namespace_that_was_proved_is_no_failed_poll():
    """A process of the cell's namespace seen only before its pivot, which ends: the namespace itself was proved."""
    proc = node(cell_process(500), building(501, inner=7))
    poll, proofs = polled(proc, **SEEN)
    del proc.procs[501]
    poll.poll()
    assert (poll.failed, [ok for _, ok, _, _ in proofs]) == (0, [True])


def test_a_helper_seen_before_the_namespace_is_proved_is_no_failed_poll_once_it_is():
    proc = node(building(501, inner=7), building(500))
    poll, proofs = polled(proc, **SEEN)
    pivoted(proc, 500)
    poll.poll()
    del proc.procs[501]
    poll.poll()
    assert (poll.failed, [ok for _, ok, _, _ in proofs]) == (0, [True])


def test_a_cell_whose_namespace_link_is_readable_only_after_the_pivot_is_no_failed_poll_when_it_ends():
    """The `ns/pid` link is gated like `ns/mnt`: seen without it while being built, a cell is pending as a process, and
    proved under its namespace once it can be read; the first entry must not outlive the proof."""
    proc = node(building(500, ns=None))
    poll, proofs = polled(proc, **SEEN)
    proc.procs[500].ns = "pid:[4026532100]"
    pivoted(proc, 500)
    poll.poll()
    del proc.procs[500]
    poll.poll()
    assert (poll.failed, [ok for _, ok, _, _ in proofs]) == (0, [True])


def test_proving_one_namespace_does_not_prove_another_that_was_only_being_built():
    proc = node(building(500), cell_process(600, ns=OTHER_PID_NS))
    poll, proofs = polled(proc, **SEEN)
    del proc.procs[500]
    poll.poll()
    assert (poll.failed, [ok for _, ok, _, _ in proofs]) == (1, [True])


def test_without_the_hosts_root_and_namespace_a_copy_is_judged_as_it_was():
    """Nothing says the cell is still being built when the watcher could not read the host's own root and namespace."""
    _, proofs = polled(node(building(500)))
    assert proofs[0][1:3] == (False, [MP.HOST_PROC_IN_CELL, MP.PROTECTED_REACHABLE])  # the live failure's two problems


def test_a_cell_in_the_nodes_pid_namespace_is_still_a_failed_host_process_proof():
    """The run's mount where the host has none, whatever its root mount: a cell with no PID namespace of its own."""
    mounts = table(HOST_ROOT, (FUSE_DEV, "/", "/cell/path", "fuse.oneclient"))
    _, proofs = polled(node(host_process(500, mountinfo=mounts)), **SEEN)
    assert [(label, ok, problems) for label, ok, problems, _ in proofs] == [
        ("host_process", False, [MP.SAME_PID_NAMESPACE])
    ]


def test_the_judgment_waits_on_the_hosts_own_root_mount_and_not_on_a_namespace_link_it_cannot_read():
    """The watcher's `ns/mnt` link of a process cannot be read (ptrace): the root mount decides."""
    proc = node(cell_process(500, mountinfo=HOST_TABLE))
    poll, proofs = polled(proc, root_mount=HOST_ROOT_MOUNT, mnt_ns=WATCHER_MNT_NS)
    assert proofs == []
    pivoted(proc, 500)
    poll.poll()
    assert [ok for _, ok, _, _ in proofs] == [True]


def test_the_watchers_host_is_a_plain_value_whose_new_fields_default_to_unknown():
    base = host()
    assert (base.root_mount, base.mnt_ns, base.entries) == (None, None, frozenset())
