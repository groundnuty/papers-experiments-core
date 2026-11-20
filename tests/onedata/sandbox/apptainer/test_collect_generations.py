"""The collection of a mount run whose mount was restored copies each generation's watcher log, the first under the name
it always had, and scans every copy for the data-path token (O97)."""

import pytest
from apptainer_support import TOKEN, make_settings, make_spec, options, pinned_bundle
from fake_mount_node import FakeMountNode
from mountguard_logs import log

from expcore.errors import InfraError
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.collect import MOUNT_WATCH_COPY, mount_watch_copy
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox


@pytest.fixture
def node() -> FakeMountNode:
    return FakeMountNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, oneclient_bundle=pinned_bundle(tmp_path / "files"))


def restored_box(node, settings, logs):
    """A mount run on the fake node whose helper says it had one watcher log for each of `logs`."""
    factory = HM.host_mount_factory(mountinfo=node.mountinfo)
    box = ApptainerSandbox.start(node, settings, make_spec("mounted"), **options(mount_helper=factory))
    box._mount.mount_watch_log = lambda: logs
    return box


def test_o97_the_first_generations_copy_keeps_the_name_it_always_had_and_the_rest_are_numbered():
    """O97: `mount-watch.jsonl`, then `mount-watch-2.jsonl`, `mount-watch-3.jsonl`."""
    assert [mount_watch_copy(g) for g in (1, 2, 3)] == [MOUNT_WATCH_COPY, "mount-watch-2.jsonl", "mount-watch-3.jsonl"]


def test_o97_every_generations_log_is_copied_under_its_own_name(node, settings, tmp_path):
    """O97: a run with two generations leaves two copies, byte for byte, and names them in the result."""
    first, second = log(), log([{"event": "tracer", "attached": False}])
    box = restored_box(node, settings, [first, second])
    try:
        node.cell()
        result = box.collect(tmp_path / "out")
        assert result["mount_watch_log"] == "mount-watch.jsonl"
        assert result["mount_watch_logs"] == ["mount-watch.jsonl", "mount-watch-2.jsonl"]
        assert (tmp_path / "out" / "mount-watch.jsonl").read_bytes() == first
        assert (tmp_path / "out" / "mount-watch-2.jsonl").read_bytes() == second
    finally:
        box.stop()


def test_o97_a_missing_generation_log_has_no_copy_and_no_name(node, settings, tmp_path):
    """O97: a generation whose log is missing is None in the list of names and has no file."""
    box = restored_box(node, settings, [log(), None, log()])
    try:
        node.cell()
        result = box.collect(tmp_path / "out")
        assert result["mount_watch_logs"] == ["mount-watch.jsonl", None, "mount-watch-3.jsonl"]
        assert not (tmp_path / "out" / "mount-watch-2.jsonl").exists()
    finally:
        box.stop()


def test_o97_a_run_with_one_generation_collects_exactly_as_before(node, settings, tmp_path):
    """O97: one log gives one copy and no `mount_watch_logs` key."""
    box = restored_box(node, settings, log())
    try:
        node.cell()
        result = box.collect(tmp_path / "out")
        assert result["mount_watch_log"] == "mount-watch.jsonl"
        assert "mount_watch_logs" not in result
        assert [p.name for p in (tmp_path / "out").glob("mount-watch*")] == ["mount-watch.jsonl"]
    finally:
        box.stop()


def test_o97_a_token_in_a_later_generations_log_fails_the_collection_and_removes_the_copy(node, settings, tmp_path):
    """O97: every generation's copy is scanned for the data-path token: one that holds it is removed and the
    collection fails, since a secret is never evidence."""
    box = restored_box(node, settings, [log(), log() + TOKEN.encode()])
    try:
        node.cell()
        with pytest.raises(InfraError):
            box.collect(tmp_path / "out")
        assert not (tmp_path / "out" / "mount-watch-2.jsonl").exists()
    finally:
        box.stop()
