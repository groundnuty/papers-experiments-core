"""O97: the Apptainer sandbox reads its cell's survivors for real and decides whose they are from its own spec. The
runner is given the diagnosis (the cell's fixed targets, the run's FUSE device as the live generation has it, the
connection's fusectl state), and the verdict is the spec's own: the providers its study holds down when the deadline
fires and the holder of each space. A fake `/proc` holds the survivor; nothing is stubbed between it and the verdict.
The same run is then restored and read whole: both generations' guard and I/O-mode readings."""

import dataclasses

import pytest
from apptainer_support import make_settings, make_spec, options, pinned_bundle
from fake_mount_node import FUSE_DEV, fusectl_tree
from fake_survivor_proc import DATA_DEVICE, DATA_MOUNT, MOUNTS, syscall_text
from reader_node import ReaderNode

from expcore.errors import InfraError
from expcore.records.cell_deadline import problem
from expcore_onedata.sandbox import calldeadline, directio
from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox import fusectl as FC
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox

HELD = "providers[1]"
HOLDERS = {"orchard": HELD, "meadow": "providers[2]"}
PROXY_LOG = (
    "I1116 10:00:00.000000  1 logging.cc:1] Forced direct IO: 0\n"
    "I1116 10:00:00.000000  1 logging.cc:1] Forced proxy IO: 1\n"
)


@pytest.fixture
def node() -> ReaderNode:
    node = ReaderNode()
    node.cell_handler = lambda cell: node.default_cell(cell)
    return node


@pytest.fixture
def fusectl_root(tmp_path):
    root = tmp_path / "fusectl"
    root.mkdir()
    fusectl_tree(root, FUSE_DEV, waiting=1)
    return root


@pytest.fixture
def free_by_fusectl(monkeypatch, node):
    real = FC.write_abort

    def write(root, connection):
        real(root, connection)
        node.free("fusectl")

    monkeypatch.setattr(FC, "write_abort", write)


def make_box(node, tmp_path, fusectl_root, *, held=(HELD,), level="mounted"):
    settings = make_settings(tmp_path, oneclient_bundle=pinned_bundle(tmp_path / "files"))
    factory = HM.host_mount_factory(mountinfo=node.mountinfo, fusectl_root=fusectl_root)
    spec = dataclasses.replace(make_spec(level), held_down=lambda: frozenset(held), space_holders=dict(HOLDERS))
    return ApptainerSandbox.start(node, settings, spec, **options(mount_helper=factory))


def blocked_reader(node, target="/data/orchard/a.bin"):
    """The cell's reader, as the node's `/proc` shows it: in D, in `read(2)` of fd 3, which is `target` on the run's
    FUSE mount; its mount table has the data mount on the run's device."""
    reader = node.reader
    table = MOUNTS.replace(DATA_DEVICE, FUSE_DEV)
    process = node.survivor_proc.process(reader.pid, start=reader.start, mounts=table)
    process.task(reader.pid, syscall=syscall_text(0, 3, 0x7F10, 0x1000), wchan="request_wait_answer")
    process.fd(3, target, mnt_id=DATA_MOUNT)


def stuck_cell(node, box, target="/data/orchard/a.bin"):
    original = node.stuck

    def stuck(cell):
        node.reader = cell.procs[1]
        blocked_reader(node, target)
        return original(cell)

    node.cell_handler = stuck
    return box.exec("read_a_stopped_providers_file()", 1.2)


def deadline_of(box):
    return box.metadata()["cell_deadlines"][0]


@pytest.fixture
def box(node, tmp_path, fusectl_root):
    sandbox = make_box(node, tmp_path, fusectl_root)
    yield sandbox
    node.free("teardown")
    sandbox.stop(force=True)


# ----------------------------------------------------------------------------------------------- the diagnosis given
def test_o97_the_runner_reads_survivors_with_the_cells_fixed_targets_and_the_runs_device(box):
    roles = box._cells.diagnosis.roles()
    assert roles.data == "/data"
    assert roles.device == FUSE_DEV
    assert roles.inputs == ()


def test_o97_the_runner_reports_the_connections_fusectl_state(box):
    assert box._cells.diagnosis.connection() == {"fusectl": "writable"}


def test_o97_a_node_with_no_fusectl_for_the_connection_reports_absent(node, tmp_path):
    sandbox = make_box(node, tmp_path, tmp_path / "no-fusectl")
    try:
        assert sandbox._cells.diagnosis.connection() == {"fusectl": "absent"}
    finally:
        sandbox.stop(force=True)


def test_o97_a_run_with_no_mount_is_given_no_diagnosis(node, tmp_path, fusectl_root):
    sandbox = make_box(node, tmp_path, fusectl_root, level="isolated")
    try:
        assert sandbox._cells.diagnosis is None
    finally:
        sandbox.stop(force=True)


def test_o97_after_a_restore_the_diagnosis_names_the_new_generations_device(node, box, free_by_fusectl, fusectl_root):
    """O97: the targets are asked when the deadline fires, so a second deadline reads the new mount's device."""
    major, _, minor = FUSE_DEV.partition(":")
    second = f"{major}:{int(minor) + 1}"
    fusectl_tree(fusectl_root, second)
    stuck_cell(node, box)
    node.cell_handler = node.default_cell
    box.restore_mount()
    assert box._cells.diagnosis.roles().device == second
    assert box._cells.diagnosis.connection() == {"fusectl": "writable"}


# ---------------------------------------------------------------------------------- the reading to the verdict, whole
def test_o97_a_reader_blocked_on_a_held_down_providers_file_is_the_agents_and_the_run_goes_on(
    node, box, free_by_fusectl
):
    """O97: read from the node's `/proc`, judged by the spec's own attribution: agent, a restore pending, then
    restored."""
    result = stuck_cell(node, box)
    assert result.restore_pending is True
    assert result.kill["category"] == "agent"
    record = deadline_of(box)
    assert record["attribution"] == {
        "verdict": "agent", "why": "blocked_on_held_down_holder", "target": HELD, "space_holder": HELD,
    }  # fmt: skip
    assert record["connection"] == {"fusectl": "writable"}
    survivor = record["survivors"][0]
    assert survivor["waits_on"][0]["mount"] == "data"
    assert survivor["waits_on"][0]["space"] == "orchard"
    node.cell_handler = node.default_cell
    assert box.restore_mount()["ok"] is True
    assert box.exec("print('next')", 30).exit_code == 0
    assert problem(calldeadline.cell_deadline_of(deadline_of(box))) is None  # what the call carries validates


def test_o97_the_same_read_in_a_space_whose_holder_is_up_ends_the_run_as_infrastructure(node, box, free_by_fusectl):
    result = stuck_cell(node, box, target="/data/meadow/a.bin")
    assert result.restore_pending is False
    assert result.kill["reason"] == "kill_survived"
    record = deadline_of(box)
    assert record["attribution"]["verdict"] == "infrastructure"
    assert record["attribution"]["why"] == "space_not_held_down"
    with pytest.raises(InfraError) as refused:
        box.exec("print('next')", 30)
    assert refused.value.kind == "cell_unkillable"


def test_o97_the_same_read_with_no_provider_held_down_is_infrastructure(node, tmp_path, fusectl_root, free_by_fusectl):
    sandbox = make_box(node, tmp_path, fusectl_root, held=())
    try:
        result = stuck_cell(node, sandbox)
        assert result.restore_pending is False
        assert deadline_of(sandbox)["attribution"]["why"] == "no_injection_in_effect"
    finally:
        node.free("teardown")
        sandbox.stop(force=True)


# ----------------------------------------------------------------------------------- a restored run, read whole
def test_o97_a_restored_run_reads_agreeing_in_both_generations_and_clean_through_the_sandbox(
    node, box, free_by_fusectl, tmp_path
):
    """O97: the integrated reading: two generations, each with its own watcher log and its own oneclient log. The I/O
    mode agrees with the declared one in both, and the guard is not unknown: it is the union of the proofs."""
    log = box._mount.where.log_dir / directio.LOG_FILE
    log.write_text(PROXY_LOG)
    node.cell()  # generation 1 proved a cell
    stuck_cell(node, box)
    node.cell_handler = node.default_cell
    box.restore_mount()
    log.write_text(PROXY_LOG)  # glog's link names the second generation's log now
    box.collect(tmp_path / "out")
    mount = box.metadata()["mount"]
    assert mount["remounts"] == 1
    assert len(box.mount_watch_logs()) == 2
    assert mount["guard"]["verdict"] == "clean"  # no cell after the restore: generation 1's proof stands
    reading = box.mount_io_mode()
    assert reading["agree"] is True
    assert reading["effective"] == "proxy"
    assert sorted(path.name for path in (tmp_path / "out").iterdir() if path.name.startswith("mount-watch")) == [
        "mount-watch-2.jsonl", "mount-watch.jsonl",
    ]  # fmt: skip


def test_o97_a_restored_run_whose_second_generation_contradicts_the_declared_mode_does_not_agree(
    node, box, free_by_fusectl, tmp_path
):
    log = box._mount.where.log_dir / directio.LOG_FILE
    log.write_text(PROXY_LOG)
    node.cell()
    stuck_cell(node, box)
    node.cell_handler = node.default_cell
    box.restore_mount()
    log.write_text("I1116 10:00:00.000000  1 logging.cc:1] Forced direct IO: 1\n")
    box.collect(tmp_path / "out")
    assert box.mount_io_mode()["agree"] is False


def test_o97_the_deadline_record_names_a_restore_and_a_generation(node, box, free_by_fusectl):
    stuck_cell(node, box)
    node.cell_handler = node.default_cell
    box.restore_mount()
    record = deadline_of(box)
    assert record["outcome"] == DL.MOUNT_RESTORED
    assert record["steps"][-1] == {"step": "restored", "s": record["steps"][-1]["s"], "generation": 2}
