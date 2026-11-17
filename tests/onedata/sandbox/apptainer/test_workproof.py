"""O71: the work disk is proved by the full fill once per node and driver (a shard step), at its first start; a later
start on that node checks the image's size and file-system type, a file system of its own seen from a cell, and the
copy out of the image, against a proof kept owner-only on the node where no cell can write. A proof that is missing,
unreadable, tampered with or of another configuration, or whose invariants differ, makes the full fill run again."""

import dataclasses
import json
import os
import stat
import struct

import pytest
from apptainer_support import LIMITS, make_settings, start
from fake_node import FakeNode
from sandbox_support import TOKEN

from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import workdisk, workproof
from expcore_onedata.sandbox.apptainer.node import LocalNode, Spawned
from expcore_onedata.sandbox.hostops import CmdResult

SIZE = workdisk.image_size(LIMITS.work_disk_bytes)


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, work_disk="image")


def _started(node, settings):
    """Start a sandbox, take what its start relied on and what its cells ran, and stop it."""
    seen = len(node.execs)
    box = start(node, settings)
    try:
        disk = box.metadata()["work_disk"]
        programs = [e.stdin or "" for e in node.execs[seen:] if e.command and e.command[0] == "timeout"]
        binds = [src for e in node.execs[seen:] for src, _ in e.binds.values()]
        keys = box.metadata()["infrastructure_facts"]["facts"]
    finally:
        box.stop()
    return disk, programs, binds, keys


def _filled(programs) -> bool:
    return any(p.startswith(workdisk.PROBE_HEAD) for p in programs)


def _proofs(settings):
    return sorted((settings.work_root / workproof.DIR).glob("*.json"))


# ------------------------------------------------------------------------------------------------ the two kinds
def test_o71_the_first_start_on_a_node_fills_and_keeps_its_proof_owner_only_where_no_cell_binds(node, settings):
    """O71: the first start runs the full fill, records it, and keeps the proof in a 0600 file of the driver's, in a
    0700 directory of the work root, outside every bind of every cell."""
    disk, programs, binds, keys = _started(node, settings)
    assert _filled(programs)
    assert disk["enospc_verified"] is True
    proof = disk["proof"]
    assert proof["kind"] == "full_fill"
    assert proof["full_fill_because"] == "missing"
    assert proof["kept"] is True
    assert SIZE // 2 < proof["held_bytes"] <= SIZE
    assert proof["size_bytes"] == SIZE
    (kept,) = _proofs(settings)
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert kept.stat().st_uid == os.getuid()
    assert stat.S_IMODE(kept.parent.stat().st_mode) == 0o700
    assert json.loads(kept.read_text())["id"] == proof["id"]
    assert not any(kept == p or kept.is_relative_to(p) for p in map(type(kept), binds))
    assert "work_disk_image" in keys


def test_o71_a_later_start_checks_the_invariants_and_records_the_first_start_s_proof(node, settings):
    """O71: the second start of the driver on the node fills nothing: it checks the invariants, its record names the
    first start's proof and carries its facts, and it says it did not show ENOSPC itself."""
    first, _, _, _ = _started(node, settings)
    disk, programs, _, keys = _started(node, settings)
    assert not _filled(programs)
    assert any(p.startswith(workdisk.MARK_HEAD) for p in programs)  # the copy probe's file, written without a fill
    assert any(p.startswith(workdisk.CHECK_HEAD) for p in programs)
    assert disk["enospc_verified"] is False
    proof = disk["proof"]
    assert proof["kind"] == "invariants"
    assert proof["id"] == first["proof"]["id"]
    assert proof["proved_utc"] == first["proof"]["proved_utc"]
    assert proof["held_bytes"] == first["proof"]["held_bytes"]
    assert "full_fill_because" not in proof
    assert disk["remade"] is True
    assert "work_disk_image" in keys
    assert len(_proofs(settings)) == 1


# ------------------------------------------------------------------------------------------------ never assumed
def _edit(path):
    document = json.loads(path.read_text())
    document["held_bytes"] += 4096
    path.write_text(json.dumps(document))


def _garbage(path):
    path.write_text("{not json")


def _open_to_others(path):
    path.chmod(0o644)


def _a_link(path):
    target = path.with_name("elsewhere.json")
    path.rename(target)
    path.symlink_to(target.name)


@pytest.mark.parametrize("tamper", [_edit, _garbage, _open_to_others, _a_link])
def test_o71_a_tampered_proof_makes_the_full_fill_run_again(node, settings, tamper):
    """O71: an edited value (its checksum no longer holds), a file that does not parse, one open to others, or a link:
    never assumed; the full fill runs again and keeps a new proof."""
    first, _, _, _ = _started(node, settings)
    (kept,) = _proofs(settings)
    tamper(kept)
    disk, programs, _, _ = _started(node, settings)
    assert _filled(programs)
    assert disk["proof"]["kind"] == "full_fill"
    assert disk["proof"]["full_fill_because"] == "unreadable"
    assert disk["proof"]["id"] != first["proof"]["id"]


def test_o71_a_missing_proof_makes_the_full_fill_run_again(node, settings):
    """O71: the proof removed between two starts: the second fills again."""
    _started(node, settings)
    for path in _proofs(settings):
        path.unlink()
    disk, programs, _, _ = _started(node, settings)
    assert _filled(programs)
    assert disk["proof"]["full_fill_because"] == "missing"


def test_o71_a_changed_size_forces_a_full_fill(node, tmp_path):
    """O71: another work disk size is another configuration: its first start fills, whatever the other size proved."""
    _started(node, make_settings(tmp_path, work_disk="image"))
    bigger = dataclasses.replace(LIMITS, work_disk_bytes=LIMITS.work_disk_bytes + workdisk.ALIGN)
    disk, programs, _, _ = _started(node, make_settings(tmp_path, limits=bigger, work_disk="image"))
    assert _filled(programs)
    assert disk["proof"]["full_fill_because"] == "missing"
    assert disk["proof"]["size_bytes"] == SIZE + workdisk.ALIGN


def test_o71_another_driver_or_another_boot_of_the_node_forces_a_full_fill(node, settings):
    """O71: the proof is the shard step's (its driver, by pid and start time) on this boot of the node."""
    _started(node, settings)
    node.owner = Spawned(1001, "5151")
    node.procs[1001] = type(node.procs[999])(1001, "5151", "pid:[4026531836]", "driver", role="driver")
    _, programs, _, _ = _started(node, settings)
    assert _filled(programs)
    node.boot = "00000000-0000-4000-8000-000000000002"
    _, programs, _, _ = _started(node, settings)
    assert _filled(programs)


def test_o71_a_node_that_cannot_be_identified_never_keeps_or_reuses_a_proof(node, settings):
    """O71: a key with an unknown part (the node's boot id here) is never written nor matched: every start fills."""
    node.boot = None
    first, _, _, _ = _started(node, settings)
    assert first["proof"]["kept"] is False
    assert first["proof"]["full_fill_because"] == "unknown_configuration"
    _, programs, _, _ = _started(node, settings)
    assert _filled(programs)
    assert not (settings.work_root / workproof.DIR).exists()


def test_o71_a_mount_type_that_differs_from_the_proof_forces_a_full_fill_and_a_new_proof(node, settings):
    """O71: the mount seen from a cell is not the one the proof was made on (the site's Apptainer now mounts the image
    another way): the invariants fail, the full fill runs again, and its proof replaces the old one."""
    first, _, _, _ = _started(node, settings)
    node.disk_check_answer = f"{SIZE * 9 // 10} 1 fuse.fuse2fs"
    disk, programs, _, _ = _started(node, settings)
    assert _filled(programs)
    assert disk["proof"]["full_fill_because"] == "invariants_differ"
    assert disk["fstype"] == "fuse.fuse2fs"
    (kept,) = _proofs(settings)
    assert json.loads(kept.read_text())["id"] == disk["proof"]["id"] != first["proof"]["id"]


def test_o71_an_unknown_mount_type_is_never_kept_as_a_proof(node, settings):
    """O71: a start whose cell could not name /work's mount type fills and runs, but keeps no proof to reuse."""
    node.disk_check_answer = f"{SIZE * 9 // 10} 1 ?"
    first, _, _, _ = _started(node, settings)
    assert first["proof"]["kept"] is False
    assert first["fstype"] is None
    assert not (settings.work_root / workproof.DIR).exists()


def test_o71_a_copy_probe_that_fails_on_the_invariants_path_still_refuses_the_start(node, settings):
    """O71: a proof never excuses the copy out of the image: it is checked at every start."""
    _started(node, settings)
    node.copy_helper_result = CmdResult(1, "", "OSError (13)\n")
    with pytest.raises(SandboxError, match="copied out of the work disk"):
        start(node, settings)


def test_o71_an_image_whose_superblock_is_not_the_one_made_refuses_the_start(node, settings):
    """O71: the image's file-system type is read from its superblock on the host: a file that is no ext file system of
    at most its size is refused, whatever the cells said."""
    node.mkfs_superblock = False
    with pytest.raises(SandboxError, match="superblock"):
        start(node, settings)


# ------------------------------------------------------------------------------------------------ the parts
def test_o71_the_superblock_is_read_as_dumpe2fs_shows_it(tmp_path):
    """O71: block count, block size, magic and features at their offsets (checked against a real mkfs.ext3 image and
    dumpe2fs -h on e2fsprogs 1.47.0: 2048 blocks of 4096, has_journal, no extents)."""
    image = tmp_path / "disk.img"
    raw = bytearray(4096)
    struct.pack_into("<I", raw, 1024 + 4, 2048)
    struct.pack_into("<I", raw, 1024 + 24, 2)
    struct.pack_into("<H", raw, 1024 + 56, 0xEF53)
    struct.pack_into("<III", raw, 1024 + 92, 0x3C, 0x2, 0x3)
    image.write_bytes(bytes(raw))
    assert workdisk.superblock(image) == {"block_bytes": 4096, "bytes": 2048 * 4096, "features": [0x3C, 0x2, 0x3]}
    image.write_bytes(bytes(4096))
    assert workdisk.superblock(image) is None
    image.write_bytes(b"short")
    assert workdisk.superblock(image) is None


def test_o71_the_check_reads_the_mount_type_of_work_or_none():
    """O71: `<bytes> <own> <type>`, the type a word of the mount table or None when the cell could not name it."""
    assert workdisk.judge_check(f"{SIZE} 1 ext3\n", 0, SIZE) == (SIZE, "ext3")
    assert workdisk.judge_check(f"{SIZE} 1 ?\n", 0, SIZE) == (SIZE, None)
    assert workdisk.judge_check(f"{SIZE} 1\n", 0, SIZE) is None
    assert workdisk.judge_check(f"{SIZE} 1 a/b\n", 0, SIZE) is None


def test_o71_the_node_s_boot_id_is_read_from_proc_or_is_none():
    """O71: `LocalNode.boot_id` is the kernel's per-boot UUID, never a name of the node; None when it cannot be read."""
    found = LocalNode().boot_id()
    assert found is None or (len(found) == 36 and found.count("-") == 4)


def test_o71_a_kept_run_s_token_scan_never_reads_the_proof(node, settings):
    """O71, O05: the proof is no file of a run's: it holds no token and stays when a run is kept or removed."""
    _started(node, settings)
    (kept,) = _proofs(settings)
    assert TOKEN.encode() not in kept.read_bytes()


def _proof_file(tmp_path, text):
    """A proof file as `load` takes one (the driver's, 0600, one link, within the size cap) holding `text`."""
    root = tmp_path / "work"
    (root / workproof.DIR).mkdir(parents=True, mode=0o700, exist_ok=True)
    path = workproof.path_of(root, "k")
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return root, path


def test_o71_a_proof_nested_past_the_parser_s_recursion_limit_is_unreadable_never_raised(tmp_path):
    """O71: `load` never raises. 30,000 nested brackets (60,001 bytes, under the size cap) made `json.loads` raise
    `RecursionError` on CPython 3.12 and 3.13: the start would have failed on a file a cell can never write."""
    root, path = _proof_file(tmp_path, "[" * 30000 + "0" + "]" * 30000)
    assert path.stat().st_size <= 1 << 16
    assert workproof.load(root, "k", {}) == (None, "unreadable")


@pytest.mark.parametrize("error", [RecursionError, MemoryError])
def test_o71_a_proof_the_parser_cannot_hold_is_unreadable_never_raised(tmp_path, monkeypatch, error):
    """O71: whatever interpreter runs it, a parser that gives up (recursion, memory) is a proof that is no proof."""
    root, _ = _proof_file(tmp_path, "{}")

    def give_up(*args, **kwargs):
        raise error

    with monkeypatch.context() as patched:
        patched.setattr(workproof.json, "loads", give_up)
        assert workproof.load(root, "k", {}) == (None, "unreadable")
