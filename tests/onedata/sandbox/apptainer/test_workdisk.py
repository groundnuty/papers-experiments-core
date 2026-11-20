"""O71: `/work` as an ext3 image of `work_disk_bytes` on the Apptainer backend (`work_disk = "image"`): a write past
its size is refused with ENOSPC inside the cell, as on Docker, proven at the start; the outputs come out of the image
with every refusal of Docker's copy; the teardown and a reaper leave nothing holding it; the directory mode is
unchanged."""

import dataclasses
import errno
import json
import os
import sys
from pathlib import Path

import pytest
from apptainer_support import LIMITS, TOKEN, make_settings, make_spec, options, start
from fake_node import FakeNode, FakeProc
from locked_work import lock, locked_afterwards, needs_unprivileged, not_the_owner
from sandbox_support import SleptClock

from expcore.errors import InfraError
from expcore_onedata.sandbox import copyout
from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import facts, runs, workdisk
from expcore_onedata.sandbox.apptainer.node import Spawned
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.hostops import CmdResult, run_bounded

SIZE = workdisk.image_size(LIMITS.work_disk_bytes)


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, work_disk="image")


def _cell_execs(node: FakeNode):
    return [e for e in node.execs if e.command and e.command[0] == "timeout"]


# ------------------------------------------------------------------------------------------------ the image
def test_o71_every_cell_binds_a_directory_of_an_ext3_image_of_the_work_disk_size_at_work(node, settings):
    """O71: the image is made sparse at `work_disk_bytes` (rounded down to 2 MiB) by the node's mkfs.ext3 from a tree of
    one directory, and every cell binds that directory of it at /work, read/write."""
    box = start(node, settings)
    try:
        image = box.dir / "work" / workdisk.IMAGE_NAME
        made = [argv for argv in node.mkfs_calls if "-V" not in argv]
        assert made
        assert all(argv[:6] == ["mkfs.ext3", "-t", "ext3", "-q", "-F", "-d"] for argv in made)
        assert all(argv[-1] == str(image) for argv in made)
        assert image.stat().st_size == SIZE
        for done in _cell_execs(node):
            assert done.binds["/work"] == (str(image), True)
            assert done.images["/work"] == "/work"
        assert workdisk.bind(box.dir).arg() == f"{image}:/work:image-src=/work"
    finally:
        box.stop()


class _MkfsRecorder:
    """A node that only records what it is asked to run, so no mkfs of this host ever runs (the fake node answers just
    two program names; any other falls through to the host's own)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv, timeout_s, **kwargs) -> CmdResult:
        self.calls.append(list(argv))
        return CmdResult(0, "")


@pytest.mark.parametrize(
    "program",
    ["mkfs.ext3", "mke2fs", "mkfs.ext2", "mkfs.ext4", "/usr/sbin/mkfs.ext4", "/opt/site/e2fsprogs/sbin/mke2fs"],
)
def test_o71_the_image_is_ext3_whatever_the_program_is_called(tmp_path, program):
    """O71: e2fsprogs takes the file system's type from the program's NAME unless `-t` gives it (`mke2fs` makes ext2,
    `mkfs.ext4` ext4; checked with e2fsprogs 1.47: `-t ext3` makes ext3, `has_journal` and no `extent`, whatever the
    name). `work_disk_mkfs` is out of the fingerprint (C06), so the image's type must never depend on it: the argv
    carries `-t ext3` right after the program, then the arguments it always had."""
    (tmp_path / "work").mkdir()
    node = _MkfsRecorder()
    workdisk.make(node, tmp_path, SIZE, (program,))
    (argv,) = node.calls
    assert argv == [
        program, "-t", "ext3", "-q", "-F", "-d", str(tmp_path / "disk-tree"), str(workdisk.image_path(tmp_path))
    ]  # fmt: skip


def test_o71_the_version_probe_is_the_programs_own_and_carries_no_type(tmp_path):
    """O71: `-V` asks the program for its version alone: the type argument belongs to the image's making."""
    node = _MkfsRecorder()
    workdisk.mkfs_version(node, ("mke2fs",))
    assert node.calls == [["mke2fs", "-V"]]


def test_o71_the_image_size_is_the_work_disk_rounded_down_so_its_allocation_never_reads_over_the_cap():
    """O71: the watch still walks the image file's allocated size against `work_disk_bytes`."""
    assert workdisk.image_size(4 << 20) == 4 << 20
    assert workdisk.image_size((4 << 20) + 4096) == 4 << 20
    assert workdisk.image_size((2 << 20) - 1) == 0


def test_o71_a_work_disk_too_small_for_an_image_refuses_the_start(node, tmp_path):
    """O71: below 2 MiB no image can be made of it; the start is refused before anything exists."""
    small = dataclasses.replace(LIMITS, work_disk_bytes=1 << 20)
    with pytest.raises(StartRefused, match="work_disk_bytes"):
        start(node, make_settings(tmp_path, limits=small, work_disk="image"))
    assert not node.mkfs_calls


def test_o71_an_image_mkfs_cannot_make_refuses_the_start_and_leaves_nothing(node, settings):
    """O71: the node's mkfs failing (missing, or without -d) fails the start; nothing of the run is left."""
    node.mkfs_fails = True
    with pytest.raises(SandboxError, match="image could not be made"):
        start(node, settings)
    assert not [p for p in settings.work_root.iterdir() if p.name != runs.RUNS]


# ------------------------------------------------------------------------------------------------ the proof
def test_o71_the_start_proves_a_write_past_the_size_is_refused_with_enospc_in_a_cell(node, settings):
    """O71: the probe runs as a cell (the harness's own), its ENOSPC recorded with what /work held; the image is then
    made again, and the run's own image is shown a file system of its own of at most its size."""
    box = start(node, settings)
    try:
        programs = [e.stdin for e in _cell_execs(node)]
        assert any(p.startswith(workdisk.PROBE_HEAD) for p in programs)
        assert any(p.startswith(workdisk.CHECK_HEAD) for p in programs)
        disk = box.metadata()["work_disk"]
        assert disk["mode"] == "image"
        assert disk["enospc_verified"] is True
        assert disk["size_bytes"] == SIZE
        assert SIZE // 2 < disk["held_bytes"] <= SIZE
        assert disk["remade"] is True
        assert disk["mkfs"].startswith("mke2fs")
        assert len([argv for argv in node.mkfs_calls if "-V" not in argv]) == 2
        image = node.images[str(box.dir / "work" / workdisk.IMAGE_NAME)]
        assert not (image.backing / "work" / workdisk.COPY_PROBE).exists()  # the agent's /work is the new image's
    finally:
        box.stop()


def test_o71_the_start_is_refused_when_a_file_cannot_be_copied_out_of_the_image(node, settings):
    """O71: the probe's file must come out through the collection's own path before the agent's first cell; a run whose
    outputs could not be collected is never started, and nothing is left."""
    node.copy_helper_result = CmdResult(1, "", "OSError (13)\n")
    with pytest.raises(SandboxError, match="copied out of the work disk"):
        start(node, settings)
    assert not [p for p in settings.work_root.iterdir() if p.name != runs.RUNS]


@pytest.mark.parametrize(
    "answer",
    [f"unbounded {SIZE + (1 << 20)}", f"deferred-{errno.ENOSPC} {SIZE}", f"errno-{errno.EACCES} 0",
     f"enospc {SIZE + 1}", "enospc 0", "enospc", ""],
)  # fmt: skip
def test_o71_the_start_is_refused_when_the_probe_does_not_see_enospc_at_the_size(node, settings, answer):
    """O71: no bound, a bound only at fsync, another error, ENOSPC past the size or before half of it, or no answer:
    nothing unproven runs; the start fails and leaves nothing."""
    node.disk_probe_answer = answer
    with pytest.raises(SandboxError, match="ENOSPC"):
        start(node, settings)
    assert not [p for p in settings.work_root.iterdir() if p.name != runs.RUNS]
    assert not list((settings.work_root / runs.RUNS).iterdir())


@pytest.mark.parametrize("answer", [f"{SIZE + 4096} 1 ext3", f"{SIZE} 0 ext3", "0 1 ext3", "x 1 ext3", f"{SIZE} 1", ""])
def test_o71_the_start_is_refused_when_the_run_s_image_is_not_a_disk_of_its_own_of_at_most_its_size(
    node, settings, answer
):
    """O71: the image made again after the probe must be a file system of its own (not /tmp's) of at most its size."""
    node.disk_check_answer = answer
    with pytest.raises(SandboxError, match="file system of its own"):
        start(node, settings)


def test_o71_the_probe_s_verdict_is_read_strictly():
    """O71: only `enospc <bytes>` with an exit of 0, the bytes past half the size and at most the size, is a proof."""
    assert workdisk.judge(f"enospc {SIZE - 4096}\n", 0, SIZE) == SIZE - 4096
    assert workdisk.judge(f"enospc {SIZE - 4096}\n", 1, SIZE) is None
    assert workdisk.judge(f"enospc {SIZE // 2}\n", 0, SIZE) is None
    assert workdisk.judge(f"enospc {SIZE} extra\n", 0, SIZE) is None


def test_o71_the_probe_writes_at_most_one_chunk_past_the_size_and_names_enospc(tmp_path):
    """O71: run on a directory that has no bound, the probe stops one chunk past the size and says so; it removes its
    file either way."""
    work = tmp_path / "work"
    work.mkdir()
    source = workdisk.probe_source(1 << 20).replace("/work/", f"{work}/")
    done = run_bounded([sys.executable, "-I", "-c", source], 60)
    assert done.returncode == 0
    assert done.stdout.split() == ["unbounded", str((1 << 20) + workdisk.PROBE_CHUNK)]
    assert [p.name for p in work.iterdir()] == [workdisk.COPY_PROBE]  # its own file gone, the copy's left
    assert (work / workdisk.COPY_PROBE).read_bytes() == workdisk.COPY_MARK


# ------------------------------------------------------------------------------------------------ a cell's write
def test_o71_a_write_past_the_size_fails_in_the_cell_with_enospc_no_kill_and_the_run_goes_on(node, settings):
    """O71: as on Docker: the write is refused, nothing is killed, and the next cell runs."""
    box = start(node, settings)
    try:
        seen = []

        def filler(cell):
            seen.append(cell.write_work("big.bin", SIZE + 1))
            return CmdResult(1, "", "OSError: [Errno 28] No space left on device\n")

        node.cell_handler = filler
        result = box.exec("open('big.bin','wb').write(bytes(1 << 30))", 30)
        assert seen == [errno.ENOSPC]
        assert result.kill is None
        assert not result.timed_out
        node.cell_handler = lambda cell: CmdResult(0, "after\n")
        assert box.exec("print('after')", 30).stdout == "after\n"
        assert box.metadata()["watch"]["kills"] == []
    finally:
        box.stop()


# ------------------------------------------------------------------------------------------------ the collection
def _write(cell, files):
    work = cell.work_dir()
    for rel, make in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        make(path)
    return CmdResult(0, "")


def test_o71_a_named_output_is_copied_out_of_the_image_by_execs_of_the_image(node, settings, tmp_path):
    """O71: the copy helper runs in one-shot execs of the image that bind the run's disk at /work and carry a tag of
    their own; the output lands where Docker's collection puts it, the driver's."""
    box = start(node, settings)
    try:
        node.cell_handler = lambda cell: _write(cell, {"out/result.txt": lambda p: p.write_text("42\n")})
        box.exec("write()", 30)
        done = box.collect(tmp_path / "out", ["out"])
        assert done["outputs"] == {"out": True}
        assert (tmp_path / "out" / "work" / "out" / "result.txt").read_text() == "42\n"
        copies = [e for e in node.execs if e.command[:4] == [settings.apptainer.python, "-I", "-B", "-c"]]
        assert [e.command[-1] for e in copies] == ["list", "copy"] * 2  # the start's proof, then the collection
        for e in copies:
            assert e.images["/work"] == "/work"
            assert e.env == workdisk.copy_env(box.sid)
            assert AV.RUN_TAG not in e.env
        assert not (box.dir / "export").exists()
    finally:
        box.stop()


def _sparse_past_the_disk(path):
    with open(path, "wb") as handle:
        handle.truncate(LIMITS.work_disk_bytes + 1)


def test_o71_the_watch_never_takes_the_collection_s_copy_for_the_agent_s(node, settings, tmp_path):
    """O71: the agent's last cell left /tmp past its size; the copy execs carry a tag of their own, not the run's, so
    the watch kills none of them and the output is collected, never set aside."""
    box = start(node, settings)
    try:
        node.cell_handler = lambda cell: _write(cell, {"out/result.txt": lambda p: p.write_text("42\n")})
        box.exec("write()", 30)
        (box.dir / "tmp" / "left.bin").write_bytes(b"\0" * (2 * LIMITS.tmp_bytes))
        done = box.collect(tmp_path / "out", ["out"])
        assert done["outputs"] == {"out": True}
        assert [k for k in box.metadata()["watch"]["kills"] if k["cell"] is None] == []
    finally:
        box.stop()


def _outside_link(path):
    secret = path.parent.parent / "secret.txt"
    secret.write_text("not an output\n")
    os.link(secret, path)


@pytest.mark.parametrize(
    ("make", "what"),
    [
        (lambda p: os.symlink("/etc/passwd", p), "symbolic link"),
        (os.mkfifo, "FIFO"),
        (_outside_link, copyout.OUTSIDE_LINK),
        (_sparse_past_the_disk, copyout.TOO_BIG),
    ],
)  # fmt: skip
def test_o71_the_collection_out_of_the_image_keeps_every_refusal_of_docker_s_copy(node, settings, tmp_path, make, what):
    """O71, O19: the listing is the image's own, so a link, a special file, a hard link out of the output or more than
    the work disk holds is refused as on Docker, for the scorers, and nothing is copied."""
    box = start(node, settings)
    try:
        node.cell_handler = lambda cell: _write(cell, {"out/ok.txt": lambda p: p.write_text("ok\n"), "out/bad": make})
        box.exec("write()", 30)
        done = box.collect(tmp_path / "out", ["out"])
        assert done["outputs"] == {"out": False}
        assert done["refused_output"]["by_what"] == {what: 1}
        assert not (tmp_path / "out" / "work").exists()
    finally:
        box.stop()


def test_o71_the_copy_is_checked_against_the_listing_on_the_device_its_own_exec_mounted(tmp_path):
    """O71: each exec mounts the image anew, so the copy's device is not the listing's; the helper is given the
    listing's device and compares each object with the device the root has now, inode, kind and size unchanged."""
    root, dest = tmp_path / "root", tmp_path / "dest"
    (root / "out").mkdir(parents=True)
    (root / "out" / "a.txt").write_text("a\n")
    dest.mkdir()
    listed = run_bounded([sys.executable, "-I", "-c", workdisk.wrapper_source(), "list"], 60,
                         input=json.dumps({"root": str(root), "max_path_bytes": copyout.MAX_PATH_BYTES}))  # fmt: skip
    entries = copyout.parse_listing(listed.stdout)
    outputs, refused = copyout.check(entries, ["out"], max_bytes=None)
    assert not refused
    out, other = outputs["out"], entries[0].key[0] + 1  # the device a second mount would have had at the listing
    request = {
        "root": str(root), "parts": list(out.parts), "dest": str(dest / "out"), "uid": os.getuid(),
        "gid": os.getgid(), "ancestors": [[other, i] for _, i in out.ancestors],
        "expect": [[other, i, k, s, n] for (_, i, k, s), n in out.expect.items()],
    }  # fmt: skip
    as_listed = run_bounded([sys.executable, "-I", "-c", workdisk.wrapper_source(), "copy"], 60,
                            input=json.dumps(request))  # fmt: skip
    assert as_listed.returncode == 3  # without the listing's device, every object is another
    moved = run_bounded([sys.executable, "-I", "-c", workdisk.wrapper_source(), "copy"], 60,
                        input=json.dumps({**request, "listed_device": other}))  # fmt: skip
    assert moved.returncode == 0, moved.stderr
    assert (dest / "out" / "a.txt").read_text() == "a\n"


@pytest.fixture
def unlocked_afterwards(tmp_path):
    yield from locked_afterwards(lambda: tmp_path)


def _lock_in_work(cell, *, root: bool = False):
    """What an agent can do in /work: leave a file and a directory (with a file in it) with every bit cleared, and,
    with `root`, clear the bits of /work itself."""
    work = cell.work_dir()
    (work / "out" / "locked").mkdir(parents=True)
    (work / "out" / "ok.txt").write_text("ok\n")
    (work / "out" / "secret.txt").write_text("secret\n")
    (work / "out" / "locked" / "inner.txt").write_text("inner\n")
    lock(work / "out" / "secret.txt")
    lock(work / "out" / "locked")
    if root:
        lock(work)
    return CmdResult(0, "")


@needs_unprivileged
@pytest.mark.parametrize("root", [False, True])
def test_o71_a_mode_000_file_and_directory_the_agent_locked_are_collected_out_of_the_image_never_set_aside(
    node, settings, tmp_path, unlocked_afterwards, root
):
    """O71: the copy helper runs as the agent's own uid, so an entry the agent locked (`chmod 000` of a file, of a
    directory, of /work itself) made the collection an infrastructure failure, set aside instead of scored. It is
    collected with what it holds."""
    box = start(node, settings)
    try:
        node.cell_handler = lambda cell: _lock_in_work(cell, root=root)
        box.exec("lock()", 30)
        done = box.collect(tmp_path / "out", ["out"])  # no InfraError
        assert done["outputs"] == {"out": True}
        assert "refused_output" not in done
        got = tmp_path / "out" / "work" / "out"
        assert (got / "secret.txt").read_text() == "secret\n"
        assert (got / "locked" / "inner.txt").read_text() == "inner\n"
        assert (got / "ok.txt").read_text() == "ok\n"
    finally:
        box.stop()


@needs_unprivileged
def test_o71_an_entry_of_the_image_the_helper_cannot_read_is_the_agents_refusal_never_a_set_aside(
    node, settings, tmp_path, monkeypatch, unlocked_afterwards
):
    """O71, O19: where the owner's bits cannot be given back, the entry is named for the scorers (`refused_output`) as
    every other object the agent made that cannot be copied; none of the outputs is copied, and nothing is set aside."""
    box = start(node, settings)
    try:
        node.cell_handler = lambda cell: _lock_in_work(cell)
        box.exec("lock()", 30)
        source = workdisk.wrapper_source()
        monkeypatch.setattr(workdisk, "wrapper_source", lambda: not_the_owner(source))
        done = box.collect(tmp_path / "out", ["out"])  # no InfraError
        assert done["outputs"] == {"out": False}
        assert done["refused_output"]["by_what"] == {copyout.UNREADABLE: 2}
        assert sorted(r["path"] for r in done["refused_output"]["refused"]) == ["out/locked", "out/secret.txt"]
        assert not (tmp_path / "out" / "work").exists()
    finally:
        box.stop()


# ------------------------------------------------------------------------------------------------ what holds it
def _holder(node: FakeNode, image: Path, **kw) -> FakeProc:
    """A process of the uid whose command line names the image: a cell's apptainer left behind, a FUSE server."""
    return node._new_proc("pid:[4026531836]", "fuse2fs", cmdline=("fuse2fs", str(image), "/mnt"), role="holder", **kw)


def test_o71_the_teardown_kills_a_process_left_holding_the_image_and_removes_the_run(node, settings):
    """O71: nothing that serves or holds the run's image outlives the teardown."""
    box = start(node, settings)
    holder = _holder(node, box.dir / "work" / workdisk.IMAGE_NAME)
    box.stop()
    assert not holder.alive
    assert not box.dir.exists()
    assert not runs.record_path(settings.work_root, box.sid).exists()


def test_o71_the_teardown_kills_a_copy_of_the_collection_left_running(node, settings):
    """O71: a process carrying the run's copy tag (an exec of the collection that was given up on) is killed too."""
    box = start(node, settings)
    left = node._new_proc(node.new_namespace(), "python3", environ=(f"{workdisk.COPY_TAG}={box.sid}",), role="holder")
    box.stop()
    assert not left.alive
    assert not box.dir.exists()


def test_o71_a_holder_started_while_the_sweep_kills_is_found_by_a_fresh_listing(node, tmp_path):
    """O71: one holder starts another as it is killed (a launcher still running): the sweep lists again until a fresh
    listing shows none, so the second is killed too; a zombie, which holds nothing, never keeps it waiting."""
    image = tmp_path / "work" / workdisk.IMAGE_NAME
    first, later = _holder(node, image), []
    zombie = _holder(node, image, state="Z", unkillable=True)
    signal = node.signal

    def forking(pid, start, sig):
        sent = signal(pid, start, sig)
        if pid == first.pid and not later:
            later.append(_holder(node, image))
        return sent

    node.signal = forking
    clock = SleptClock()
    assert workdisk.end_holders(node, image, "sid-00000000", sleep=clock.sleep, clock=clock) == 2
    assert not first.alive
    assert later
    assert not later[0].alive
    assert zombie.state == "Z"


def test_o71_a_holder_once_found_is_waited_for_even_when_its_command_line_cannot_be_read_again(node, tmp_path):
    """O71: a holder whose kill could not be told sent, and whose command line the next listing cannot read, is still
    the one found: the sweep waits for it to be shown gone and fails at its deadline, never passing over it."""
    image = tmp_path / "work" / workdisk.IMAGE_NAME
    holder = _holder(node, image)

    def unreadable(pid, start, sig):
        holder.cmdline = ()  # its tags cannot be read any more
        return None

    node.signal = unreadable
    clock = SleptClock()
    with pytest.raises(SandboxError, match="holds the run's work disk"):
        workdisk.end_holders(node, image, "sid-00000000", sleep=clock.sleep, clock=clock)
    assert holder.alive


def test_o71_a_holder_that_cannot_be_shown_gone_fails_the_teardown_and_keeps_the_record(node, settings):
    """O71: one that no kill ends is never passed over: the directory and the record stay for a next try."""
    box = start(node, settings)
    _holder(node, box.dir / "work" / workdisk.IMAGE_NAME, unkillable=True)
    with pytest.raises(SandboxError, match="holds the run's work disk"):
        box.stop()
    assert box.dir.exists()
    assert runs.record_path(settings.work_root, box.sid).exists()


def test_o71_the_reaper_kills_a_dead_driver_s_image_holder_and_removes_the_image(node, settings):
    """O71: a run whose driver was killed outright: what holds its image is killed before its directory goes."""
    old = start(node, settings)
    holder = _holder(node, old.dir / "work" / workdisk.IMAGE_NAME)
    node.procs[node.owner.pid].alive = False
    node.owner = Spawned(1001, "5151")
    node.procs[1001] = FakeProc(1001, "5151", "pid:[4026531836]", "driver", role="driver")
    fresh = start(node, settings)
    try:
        assert fresh.metadata()["reaped_orphans"]["left"] == 0
        assert not holder.alive
        assert not old.dir.exists()
    finally:
        fresh.stop()


def test_o71_a_kept_run_drops_the_image_it_cannot_show_free_of_the_token(node, settings, tmp_path):
    """O71, O05: a raw image cannot be scanned for the token (a file's blocks need not be contiguous), so a run kept as
    evidence drops it and says so; the rest is kept as before."""
    box = start(node, settings)
    with open(box.dir / "gate" / "requests.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "leak", "value": TOKEN}) + "\n")
    with pytest.raises(InfraError):
        box.collect(tmp_path / "out")
    box.stop()
    kept = settings.work_root / "_kept_evidence" / box.sid
    assert kept.is_dir()
    assert not (kept / "work" / workdisk.IMAGE_NAME).exists()
    assert box.metadata()["evidence_kept"]["work_disk_image_dropped"] is True


def test_o71_a_kept_directory_run_keeps_an_agent_s_file_named_as_the_image(node, tmp_path):
    """O71, O05: with the directory, `/work/disk.img` is the agent's own file: a kept run keeps it (the token scan reads
    it), and nothing says an image was dropped."""
    settings = make_settings(tmp_path)
    box = start(node, settings)
    (box.dir / "work" / workdisk.IMAGE_NAME).write_bytes(b"the agent's own file\n")
    with open(box.dir / "gate" / "requests.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "leak", "value": TOKEN}) + "\n")
    with pytest.raises(InfraError):
        box.collect(tmp_path / "out")
    box.stop()
    kept = settings.work_root / "_kept_evidence" / box.sid
    assert (kept / "work" / workdisk.IMAGE_NAME).read_bytes() == b"the agent's own file\n"
    assert "work_disk_image_dropped" not in box.metadata()["evidence_kept"]


# ------------------------------------------------------------------------------------------------ the record
def test_o71_an_image_run_s_facts_close_work_and_keep_tmp_and_shm_declared(node, settings):
    """O71: with the proof held, `/work` is no departure (work_disk_image); /tmp and /dev/shm stay the watch's."""
    box = start(node, settings)
    try:
        keys = box.metadata()["infrastructure_facts"]["facts"]
        assert {"work_disk_image", "disk_watch_tmp"} <= set(keys)
        assert not {"disk_watch", "work_disk_unverified"} & set(keys)
        assert box.metadata()["backend"]["work_disk"] == "image"
    finally:
        box.stop()


def test_o71_an_image_run_s_facts_say_unverified_until_the_probe_holds(node, settings):
    """O71: before the proof, the record never says /work is closed."""
    keys = facts.record(work_disk="image_unverified", mount_refused=True)["facts"]
    assert "work_disk_unverified" in keys
    assert not {"work_disk_image", "disk_watch"} & set(keys)
    box = ApptainerSandbox(node, settings, make_spec(), **options())
    assert box.metadata()["infrastructure_facts"]["facts"] == keys


def test_o71_the_directory_mode_is_unchanged(node, tmp_path):
    """O71: by default /work is the run's directory, bound as before; no mkfs, no probe, the facts and the backend's
    record as they were."""
    settings = make_settings(tmp_path)
    assert settings.apptainer.work_disk == "directory"
    assert "work_disk" not in settings.apptainer.record()
    image_keys = {"disk_watch_tmp", "work_disk_image", "work_disk_unverified"}
    for refused in (True, False):  # the keys every run had before the image existed, `mount_refused` as it was
        before = sorted(k for k in facts.FACTS if k not in image_keys and (refused or k != "mount_refused"))
        assert facts.record(mount_refused=refused)["facts"] == before
        assert facts.record("directory", mount_refused=refused) == facts.record(mount_refused=refused)
    assert "disk_watch" in facts.record(mount_refused=True)["facts"]
    box = start(node, settings, make_spec("isolated"))
    try:
        assert not node.mkfs_calls
        for done in _cell_execs(node):
            assert done.binds["/work"] == (str(box.dir / "work"), True)
            assert "/work" not in done.images
            assert not done.stdin.startswith(workdisk.PROBE_HEAD)
        assert "work_disk" not in box.metadata()
    finally:
        box.stop()
