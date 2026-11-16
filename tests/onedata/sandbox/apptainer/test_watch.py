"""O56: the watch finds a cell's processes by their PID namespace and keeps the memory and disk bounds of the run."""

import pytest
from fake_node import OWN_NS, FakeNode

from expcore_onedata.sandbox.apptainer.watch import Space, Watch, usage

TAG = "EXPCORE_SANDBOX=run-1-abcd1234"


def _watch(node, tmp_path, limit=100 << 20):
    spaces = (Space("work", tmp_path / "work", 1 << 20),)
    (tmp_path / "work").mkdir(exist_ok=True)
    return Watch(node, run_tag=TAG, own_pidns=OWN_NS, memory_bytes=limit, spaces=spaces, memory_poll_s=0.02,
                 disk_poll_s=0.0)  # fmt: skip


@pytest.fixture
def node() -> FakeNode:
    node = FakeNode()
    node.wait_for_watch = False
    return node


def test_a_namespace_is_the_run_s_once_one_of_its_processes_carries_the_tag(node, tmp_path):
    """O56: an untagged process in a tagged one's namespace is the cell's; one of the node's own namespace, or of
    another namespace with no tagged process, is not."""
    ns, other = node.new_namespace(), node.new_namespace()
    tagged = node._new_proc(ns, "timeout", environ=(TAG,))
    cleared = node._new_proc(ns, "python3")  # cleared its environment: still the cell's
    node._new_proc(other, "python3")
    node._new_proc(OWN_NS, "python3", environ=(TAG,))  # the node's own namespace is never a cell's
    watch = _watch(node, tmp_path)
    assert {p.pid for p in watch.members()} == {tagged.pid, cleared.pid}


def test_the_running_cell_s_nonce_names_its_namespace_too(node, tmp_path):
    ns = node.new_namespace()
    top = node._new_proc(ns, "timeout", cmdline=("timeout", "nonce-123"))
    watch = _watch(node, tmp_path)
    watch.begin_cell(1, "nonce-123")
    assert [p.pid for p in watch.members()] == [top.pid]
    assert watch.end_cell()["seen"] is True


def test_over_the_memory_limit_every_process_of_the_run_is_killed_and_the_kill_recorded(node, tmp_path):
    ns = node.new_namespace()
    procs = [node._new_proc(ns, "python3", environ=(TAG,), rss=60 << 20) for _ in range(2)]
    watch = _watch(node, tmp_path)
    watch.begin_cell(3, "n")
    watch.poll()
    assert not any(p.alive for p in procs)
    found = watch.end_cell()
    assert found["kills"][0]["reason"] == "memory_limit"
    assert found["kills"][0]["cell"] == 3
    assert found["kills"][0]["amount"] == 120 << 20
    assert watch.summary()["peak_bytes"] == 120 << 20


def test_under_the_limit_nothing_is_killed(node, tmp_path):
    ns = node.new_namespace()
    proc = node._new_proc(ns, "python3", environ=(TAG,), rss=10 << 20)
    watch = _watch(node, tmp_path)
    watch.poll()
    assert proc.alive
    assert watch.kills == []


def test_a_directory_past_its_size_kills_the_run_as_a_disk_limit(node, tmp_path):
    ns = node.new_namespace()
    proc = node._new_proc(ns, "python3", environ=(TAG,))
    watch = _watch(node, tmp_path)
    (tmp_path / "work" / "big").write_bytes(b"x" * (2 << 20))
    watch.poll()
    assert not proc.alive
    assert watch.kills[0]["reason"] == "disk_limit"
    assert watch.kills[0]["what"] == "work"


def test_a_proc_listing_that_fails_is_counted_never_taken_for_no_process(node, tmp_path):
    node.proc_unreadable = True
    watch = _watch(node, tmp_path)
    watch.poll()
    assert watch.unreadable_polls == 1
    assert watch.members() is None


def test_usage_never_follows_a_link_and_stops_past_its_entry_cap(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 8192)
    (tmp_path / "link").symlink_to("/")
    for n in range(10):
        (tmp_path / f"f{n}").touch()
    used, entries, whole = usage(tmp_path, 1000)
    assert used >= 8192
    assert entries == 12
    assert whole
    assert usage(tmp_path, 3)[1] == 4


def test_a_directory_the_walk_may_not_list_makes_it_incomplete(tmp_path):
    """O56: a directory its owner made 0000 hides what is under it: unknown, never nothing."""
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "big").write_bytes(b"x" * 8192)
    hidden.chmod(0)
    try:
        assert usage(tmp_path, 1000)[2] is False
    finally:
        hidden.chmod(0o700)


def test_a_space_the_watch_cannot_walk_kills_the_run_as_a_disk_limit(node, tmp_path):
    """O56: a cell that hides a directory from the walk (and writes on through a descriptor it holds) is taken as
    past its size."""
    ns = node.new_namespace()
    proc = node._new_proc(ns, "python3", environ=(TAG,))
    watch = _watch(node, tmp_path)
    hidden = tmp_path / "work" / "hidden"
    hidden.mkdir()
    hidden.chmod(0)
    try:
        watch.poll()
    finally:
        hidden.chmod(0o700)
    assert not proc.alive
    assert watch.kills[0]["what"] == "work_unwalkable"
