"""O60: a failed cell proof records which mounts failed it and what the process's root and mount namespace were, in
words that name no path, so a cell still being built is told from one that holds the host's `/proc`."""

import json

from fake_proc import FUSE_DEV, HOST_PROC, HOST_ROOT, HOST_TABLE, P, cell_process, cell_table, host_process, table
from mountproof_support import HOST_ROOT_MOUNT, WATCHER_MNT_NS, node, polled

from expcore_onedata.sandbox import mountdetail as MD
from expcore_onedata.sandbox import mountproof as MP

PROC_BIND = (HOST_PROC[0], "/", "/hostproc", "proc")  # the host's `/proc` bound into the cell at another point
RUN_BIND = ("0:30", "/run", "/x", "tmpfs")  # holds the protected directory `mountproof_support.GUARD` names


def test_a_failed_proof_carries_the_offending_mounts():
    """O60: each mount that made `host_proc_in_cell` or `protected_reachable`, by kind and never by path."""
    proc = node(cell_process(500, mountinfo=cell_table(PROC_BIND, RUN_BIND)))
    _, proofs = polled(proc)
    assert proofs[0][2] == [MP.HOST_PROC_IN_CELL, MP.PROTECTED_REACHABLE]
    detail = proofs.details[0]
    assert detail["offending_total"] == 2
    assert detail["offending"] == [
        {"problem": MP.HOST_PROC_IN_CELL, "dev": "0:20", "fstype": "proc", "root": "/", "point": "other",
         "host_copy": False},
        {"problem": MP.PROTECTED_REACHABLE, "dev": "0:30", "fstype": "tmpfs", "root": "/run", "point": "other",
         "host_copy": False, "protected": 0, "relation": "holds"},
    ]  # fmt: skip


def test_a_mount_under_a_protected_directory_is_told_from_one_that_holds_it():
    under = ("0:30", "/run/guard/token/a", "/x", "tmpfs")
    _, proofs = polled(node(cell_process(500, mountinfo=cell_table(under))))
    assert proofs.details[0]["offending"][0]["relation"] == "under"


def test_a_mount_the_host_also_has_is_told_from_a_bind_of_the_hosts_proc():
    """A cell whose table is a copy of the host's holds the host's own `/proc`: the same device, root and point."""
    entries = frozenset({(HOST_PROC[0], "/", "/proc")})
    copy = (HOST_PROC[0], "/", "/proc", "proc")
    _, proofs = polled(node(cell_process(500, mountinfo=cell_table(copy))), entries=entries)
    assert [(e["point"], e["host_copy"]) for e in proofs.details[0]["offending"]] == [("/proc", True)]


def test_the_offending_mounts_are_capped_and_all_counted():
    many = [(HOST_PROC[0], "/", f"/hostproc{n}", "proc") for n in range(MD.MAX_ENTRIES + 4)]
    _, proofs = polled(node(cell_process(500, mountinfo=cell_table(*many))))
    detail = proofs.details[0]
    assert len(detail["offending"]) == MD.MAX_ENTRIES
    assert detail["offending_total"] == MD.MAX_ENTRIES + 4


def test_a_passing_proof_carries_no_detail():
    _, proofs = polled(node(cell_process(500)))
    assert proofs.details == [None]


def test_the_detail_names_no_path_and_no_text_of_a_mount_table():
    """The log names no path: a mount's point and root are a system name or `other`, its device a number pair or `?` and
    its file system type a name from a list or `other`, whatever the table holds."""
    private = {"dev": "x:y", "root": "/srv/invented/data", "mountpoint": "/scratch/invented/run", "fstype": "e" * 40}
    found = [(MP.HOST_PROC_IN_CELL, private, None, None)]
    detail = MD.entries(found, frozenset())
    assert detail["offending"][0] == {"problem": MP.HOST_PROC_IN_CELL, "dev": "?", "fstype": "other", "root": "other",
                                      "point": "other", "host_copy": False}  # fmt: skip
    assert "invented" not in json.dumps(detail)


def test_a_fuse_subtype_is_never_written_as_the_mounting_programs_own_text():
    """A `fuse.<subtype>` is a name the mounting program chose, and may hold a user's or a host's: it is `fuse` unless
    it is one the core's own run names."""
    assert [MD.fstype(name) for name in ("fuse.alice", "fuse.oneclient", "fuse", "proc", "alice", "")] == [
        "fuse", "fuse.oneclient", "fuse", "proc", "other", "other",
    ]  # fmt: skip


def test_the_process_view_records_the_root_and_whether_the_mount_namespace_is_the_watchers():
    cell = cell_process(500, mountinfo=cell_table(PROC_BIND), mnt_ns="mnt:[4026532400]", root="/")
    _, proofs = polled(node(cell), mnt_ns=WATCHER_MNT_NS)
    assert proofs.details[0]["process"] == {"root": "/", "mnt_ns": "different", "root_mount": "?"}


def test_a_root_that_is_not_slash_is_recorded_as_other_never_as_its_path():
    cell = cell_process(500, mountinfo=cell_table(PROC_BIND), root="/some/where")
    _, proofs = polled(node(cell))
    assert proofs.details[0]["process"]["root"] == "other"
    assert "where" not in json.dumps(proofs.details[0])


def test_links_that_cannot_be_read_are_unread_and_never_a_failed_poll():
    """A readlink is gated by ptrace access; the detail says `unread` and the proof is written as before."""
    cell = cell_process(500, mountinfo=cell_table(PROC_BIND))  # no `root` and no `ns/mnt` link readable
    poll, proofs = polled(node(cell), mnt_ns=WATCHER_MNT_NS)
    assert proofs.details[0]["process"] == {"root": "unread", "mnt_ns": "unread", "root_mount": "?"}
    assert poll.failed == 0


def test_without_the_watchers_own_namespace_the_comparison_is_unread():
    cell = cell_process(500, mountinfo=cell_table(PROC_BIND), mnt_ns="mnt:[4026532400]")
    _, proofs = polled(node(cell))
    assert proofs.details[0]["process"]["mnt_ns"] == "unread"


def test_a_root_mount_of_the_processs_own_is_told_from_the_hosts():
    _, proofs = polled(node(cell_process(500, mountinfo=cell_table(PROC_BIND))), root_mount=HOST_ROOT_MOUNT)
    assert proofs.details[0]["process"]["root_mount"] == "own"


def test_a_process_whose_root_mount_is_the_hosts_says_so():
    """In the node's namespace with the host's `/` and the run's mount elsewhere: a cell started without a namespace."""
    mounts = table(HOST_ROOT, (FUSE_DEV, "/", "/cell/path", "fuse.oneclient"))
    _, proofs = polled(node(host_process(500, mountinfo=mounts)), root_mount=HOST_ROOT_MOUNT)
    assert proofs.details[0]["process"]["root_mount"] == "host"


def test_a_cell_failed_only_for_oneclient_has_no_offending_mounts():
    proc = node(cell_process(500))
    del proc.procs[99]  # oneclient is gone
    _, proofs = polled(proc)
    assert proofs.details[0]["offending"] == []
    assert proofs.details[0]["offending_total"] == 0


def test_a_cell_in_the_nodes_namespace_failed_as_a_host_process_carries_its_process_view():
    proc = node(P(500, 1500, (500,), mountinfo=cell_table(), root="/", mnt_ns="mnt:[4026532400]"))
    _, proofs = polled(proc, mnt_ns=WATCHER_MNT_NS)
    assert proofs[0][:3] == ("host_process", False, ["same_pid_namespace"])
    assert proofs.details[0] == {"process": {"root": "/", "mnt_ns": "different", "root_mount": "?"}}


def test_the_hosts_root_and_mounts_come_from_its_table():
    root, entries = MP.host_mounts(HOST_TABLE)
    assert root == ("8:1", "/")
    assert (HOST_PROC[0], "/", "/proc") in entries
    assert MP.host_mounts("") == (None, frozenset())
