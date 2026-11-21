"""O97: the Apptainer sandbox's side of a restorable abort. A mount run's escalation is restorable with the mount's own
abort and the run's attribution. A cell whose survivor the abort freed, with the verdict `agent`, ends with its time
limit and a restore pending; the next cell is refused until `restore_mount()` has brought the mount back as
generation 2, and it then runs. Any other verdict, no attribution, an abort that frees nothing and a restore that
fails end the run as they always did (infrastructure), and a stop never runs beside a restore. The real `HostMount`
on the fake node; the diagnosis is a stub, since reading `/proc` is the diagnostics' own tests'."""

import dataclasses
import threading
import time

import pytest
from apptainer_support import make_settings, make_spec, options, pinned_bundle
from fake_mount_node import FUSE_DEV, fusectl_tree
from reader_node import ReaderNode

from expcore.errors import InfraError
from expcore_onedata.sandbox import cells as CELLS
from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox import fusectl as FC
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import cells as AC
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox

AGENT = {"verdict": "agent", "why": "blocked_on_held_down_holder", "target": "providers[1]",
         "space_holder": "providers[1]"}  # fmt: skip
HARNESS = {"verdict": "infrastructure", "why": "space_not_held_down", "target": "providers[1]", "space_holder": None}


@pytest.fixture
def node() -> ReaderNode:
    node = ReaderNode()
    node.cell_handler = lambda cell: node.default_cell(cell)
    return node


@pytest.fixture
def fusectl_root(tmp_path):
    root = tmp_path / "fusectl"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def stub_diagnosis(monkeypatch):
    """What the diagnostics read of the survivors, as a stub that covers every one the deadline listed."""

    def diagnose(self, left, timeout_s):
        return {"reading": [{"pid": p["pid"], "start": p["start"], "comm": "python3", "state": p.get("state")}
                            for p in left], "connection": {"fusectl": "writable"}}  # fmt: skip

    monkeypatch.setattr(AC._RunProcesses, "diagnose", diagnose, raising=False)


@pytest.fixture
def free_by_fusectl(monkeypatch, node):
    """The kernel's effect of the fusectl write: the reader is freed."""
    real = FC.write_abort

    def write(root, connection):
        real(root, connection)
        node.free("fusectl")

    monkeypatch.setattr(FC, "write_abort", write)


def make_box(
    node,
    tmp_path,
    fusectl_root,
    *,
    attribute=lambda diagnostics: dict(AGENT),
    level="mounted",
    mount_wrap=None,
    spec=None,
    **more,
):
    settings = make_settings(tmp_path, oneclient_bundle=pinned_bundle(tmp_path / "files"))
    factory = HM.host_mount_factory(mountinfo=node.mountinfo, fusectl_root=fusectl_root)
    if mount_wrap is not None:
        inner = factory

        def factory(*args):
            return mount_wrap(inner(*args))

    box_options = options(mount_helper=factory, attribute=attribute, **more)
    return ApptainerSandbox.start(node, settings, spec or make_spec(level), **box_options)


@pytest.fixture
def box(node, tmp_path, fusectl_root):
    fusectl_tree(fusectl_root, FUSE_DEV, waiting=1)
    sandbox = make_box(node, tmp_path, fusectl_root)
    yield sandbox
    node.free("teardown")
    sandbox.stop(force=True)


def stick(node, box) -> None:
    node.cell_handler = node.stuck


def deadline_of(box, cell: int = 1) -> dict:
    return next(e for e in box.metadata()["cell_deadlines"] if e["cell"] == cell)


# --------------------------------------------------------------------------------------------------- the escalation
def test_o97_a_mount_runs_escalation_is_restorable_with_the_mounts_abort_and_the_given_attribution(box):
    esc = box._cells._escalation
    assert esc.restorable is True
    assert esc.attribute is not None
    assert esc.attribute(None) == AGENT
    assert esc.abort == box._abort_mount


def test_o97_the_escalation_s_attribution_is_built_from_the_specs_held_down_set_and_holders(
    node, tmp_path, fusectl_root
):
    """O97: with no option given, the verdict is the spec's: `held_down` is read when it is asked for, never when the
    sandbox started, and each space's holder is the spec's."""
    held = set()
    spec = make_spec("mounted")
    spec = dataclasses.replace(spec, held_down=lambda: frozenset(held), space_holders={"orchard": "providers[1]"})
    sandbox = make_box(node, tmp_path, fusectl_root, attribute=None, spec=spec)
    try:
        attribute = sandbox._cells._escalation.attribute
        reading = {"reading": [{"pid": 7, "start": "7", "tasks": [{
            "state": "D", "syscall": {"nr": 0, "fd": 3},
            "blocking": {"via": "fd", "cut": False, "objects": [{"mount": "data", "space": "orchard", "path": "a"}]},
        }]}]}  # fmt: skip
        assert attribute(reading)["why"] == "no_injection_in_effect"  # nothing is held down yet
        held.add("providers[1]")  # the study takes the holder down after the sandbox started
        verdict = attribute(reading)
        assert verdict["verdict"] == "agent"
        assert verdict["target"] == "providers[1]"
    finally:
        sandbox.stop(force=True)


def test_o97_an_attribute_option_given_to_the_sandbox_is_the_escalations_attribution(node, tmp_path, fusectl_root):
    """O97: the constructor swallows unknown keyword arguments, so the name of the option is pinned here."""
    given = lambda diagnostics: dict(HARNESS)  # noqa: E731
    sandbox = make_box(node, tmp_path, fusectl_root, attribute=given)
    try:
        assert sandbox._cells._escalation.attribute is given
    finally:
        sandbox.stop(force=True)


def test_o97_a_run_with_no_mount_keeps_the_no_mount_abort_and_is_not_restorable(node, tmp_path, fusectl_root):
    sandbox = make_box(node, tmp_path, fusectl_root, level="isolated")
    try:
        esc = sandbox._cells._escalation
        assert esc.restorable is False
        assert esc.attribute is None
        assert esc.abort(5.0)["ok"] is None
    finally:
        sandbox.stop(force=True)


def test_o97_a_mount_helper_that_cannot_abort_and_restore_keeps_the_no_mount_abort(node, tmp_path, fusectl_root):
    class Plain:
        """A mount helper of a platform that has none of the two: everything but them is the real one's."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name in ("abort", "restore"):
                raise AttributeError(name)
            return getattr(self._inner, name)

    sandbox = make_box(node, tmp_path, fusectl_root, mount_wrap=Plain)
    try:
        esc = sandbox._cells._escalation
        assert esc.restorable is False
        assert esc.abort(5.0)["ok"] is None
    finally:
        sandbox.stop(force=True)


# --------------------------------------------------------------------------------------------- the agent's survivor
def test_o97_a_survivor_freed_by_the_abort_ends_the_cell_with_its_time_limit_and_a_restore_pending(
    node, box, free_by_fusectl
):
    stick(node, box)
    result = box.exec("read_a_stopped_providers_file()", 1.2)
    assert result.timed_out
    assert result.kill["reason"] == "time_limit"
    assert result.kill["category"] == "agent"
    assert result.restore_pending is True
    record = deadline_of(box)
    assert record["outcome"] == DL.MOUNT_ABORTED
    assert record["restore"] == DL.RESTORE_PENDING
    assert record["attribution"]["verdict"] == "agent"
    assert box._cells.ended is None
    assert node.freed_by == ["fusectl"]


def test_o97_the_next_cell_is_refused_until_the_mount_is_restored(node, box, free_by_fusectl):
    stick(node, box)
    box.exec("read_a_stopped_providers_file()", 1.2)
    with pytest.raises(SandboxError, match="not been restored"):
        box.exec("print('next')", 30)


def test_o97_a_restore_brings_the_mount_back_as_generation_two_and_the_next_cell_runs(node, box, free_by_fusectl):
    stick(node, box)
    box.exec("read_a_stopped_providers_file()", 1.2)
    node.cell_handler = node.default_cell
    answer = box.restore_mount()
    assert answer["ok"] is True
    assert answer["generation"] == 2
    record = deadline_of(box)
    assert record["outcome"] == DL.MOUNT_RESTORED
    assert record["restore"] == DL.RESTORE_DONE
    assert record["steps"][-1]["generation"] == 2
    assert box._cells.ended is None
    after = box.exec("print('next')", 30)
    assert after.exit_code == 0
    assert after.kill is None


def test_o97_a_cell_after_the_restore_binds_the_same_mount_point_at_the_same_path(node, box, free_by_fusectl):
    stick(node, box)
    box.exec("read_a_stopped_providers_file()", 1.2)
    node.cell_handler = node.default_cell
    box.restore_mount()
    box.exec("print('next')", 30)
    first, last = node.execs[1], node.execs[-1]  # the start's canary cells are before the agent's
    assert last.binds["/data"] == first.binds["/data"]


def test_o97_the_restore_runs_under_the_sandboxs_restore_lock(node, box, free_by_fusectl, monkeypatch):
    seen = []
    real = box._mount.restore

    def restore(**kwargs):
        seen.append(box._restore_lock.locked())
        return real(**kwargs)

    stick(node, box)
    box.exec("read_a_stopped_providers_file()", 1.2)
    monkeypatch.setattr(box._mount, "restore", restore)
    box.restore_mount()
    assert seen == [True]
    assert not box._restore_lock.locked()


def test_o97_a_survivor_freed_by_oneclients_death_when_the_node_has_no_fusectl_is_restored_the_same(
    node, tmp_path, fusectl_root
):
    sandbox = make_box(node, tmp_path, fusectl_root)  # no fusectl directory for the connection: the kill path
    try:
        node.cell_handler = node.stuck
        result = sandbox.exec("read_a_stopped_providers_file()", 1.2)
        assert result.restore_pending is True
        assert node.freed_by == ["daemon_death"]
        node.cell_handler = node.default_cell
        assert sandbox.restore_mount()["ok"] is True
        assert sandbox.metadata()["mount"]["generations"][0]["abort"]["method"] == "daemon_kill"
    finally:
        node.free("teardown")
        sandbox.stop(force=True)


def test_o97_after_a_restore_the_run_collects_with_both_generations_in_its_metadata_and_logs(
    node, box, free_by_fusectl, tmp_path
):
    stick(node, box)
    box.exec("read_a_stopped_providers_file()", 1.2)
    node.cell_handler = node.default_cell
    box.restore_mount()
    node.cell()  # a proof in the new generation's window
    box.collect(tmp_path / "out")
    mount = box.metadata()["mount"]
    assert mount["remounts"] == 1
    assert [g["abort"] is not None for g in mount["generations"]] == [True, False]
    assert len(box.mount_watch_logs()) == 2


# ----------------------------------------------------------------------------------- everything else stays as it was
def test_o97_a_survivor_that_is_the_harness_s_ends_the_run_as_cell_unkillable_and_nothing_is_restored(
    node, tmp_path, fusectl_root, free_by_fusectl
):
    sandbox = make_box(node, tmp_path, fusectl_root, attribute=lambda diagnostics: dict(HARNESS))
    try:
        node.cell_handler = node.stuck
        result = sandbox.exec("read_a_stopped_providers_file()", 1.2)
        assert result.kill["reason"] == "kill_survived"
        assert result.restore_pending is False
        assert deadline_of(sandbox)["outcome"] == DL.MOUNT_ABORTED
        assert "restore" not in deadline_of(sandbox)
        with pytest.raises(InfraError) as refused:
            sandbox.exec("print('next')", 30)
        assert refused.value.kind == "cell_unkillable"
        assert sandbox.restore_mount()["ok"] is None
        assert node.mount_count == 1
    finally:
        sandbox.stop(force=True)


def test_o97_when_no_provider_is_held_down_a_freed_survivor_ends_the_run_as_it_always_did(
    node, tmp_path, fusectl_root, free_by_fusectl
):
    sandbox = make_box(node, tmp_path, fusectl_root, attribute=None)
    try:
        node.cell_handler = node.stuck
        result = sandbox.exec("read_a_stopped_providers_file()", 1.2)
        assert result.kill["reason"] == "kill_survived"
        assert result.restore_pending is False
        with pytest.raises(InfraError) as refused:
            sandbox.exec("print('next')", 30)
        assert refused.value.kind == "cell_unkillable"
    finally:
        sandbox.stop(force=True)


def test_o97_an_abort_that_frees_nothing_orders_the_stop_and_ends_the_run_as_it_always_did(node, box):
    stick(node, box)  # no hook frees the reader: the write changes nothing
    result = box.exec("read_a_stopped_providers_file()", 1.2)
    assert result.kill["reason"] == "kill_survived"
    assert deadline_of(box)["outcome"] in (DL.SANDBOX_STOPPED, DL.SURVIVED)
    with pytest.raises(InfraError) as refused:
        box.exec("print('next')", 30)
    assert refused.value.kind == "cell_unkillable"


def test_o97_a_restore_that_fails_ends_the_run_as_kind_sandbox_and_leaves_no_token_file(node, box, free_by_fusectl):
    stick(node, box)
    result = box.exec("read_a_stopped_providers_file()", 1.2)
    assert result.restore_pending is True
    node.oneclient_fails = True
    answer = box.restore_mount()
    assert answer["ok"] is False
    assert "ended before" in answer["why"]
    assert result.kill["category"] == "agent"  # the cell keeps its own result
    with pytest.raises(InfraError) as refused:
        box.exec("print('next')", 30)
    assert refused.value.kind == "sandbox"
    assert not box.token_files_left()


# ---------------------------------------------------------------------------------------------- stop and a restore
def test_o97_a_stop_that_comes_while_a_restore_runs_waits_for_it_and_the_restore_leaves_no_token_file(
    node, tmp_path, fusectl_root, free_by_fusectl, monkeypatch
):
    """O97: the restore is still waiting for its mount when the stop is asked for. L1's settle gives up on it after its
    bound and ends the run; the stop tells the restore to end, waits for it, and only then runs the teardown, so nothing
    writes the token file or starts a process beside it."""
    fusectl_tree(fusectl_root, FUSE_DEV, waiting=1)
    sandbox = make_box(node, tmp_path, fusectl_root)
    monkeypatch.setattr(CELLS, "RESTORE_WAIT_S", 0.3)
    order, answers = [], []
    real = sandbox.teardown.run
    sandbox.teardown.run = lambda force: (order.append(("teardown", restorer.is_alive())), real(force))[1]
    node.cell_handler = node.stuck
    sandbox.exec("read_a_stopped_providers_file()", 1.2)
    node.cell_handler = node.default_cell
    node.mount_never = True  # the new oneclient's mount never appears: the restore waits for it
    restorer = threading.Thread(target=lambda: answers.append(sandbox.restore_mount()))
    restorer.start()
    until = time.monotonic() + 10
    while len(node.oneclient_tokens) < 2 and time.monotonic() < until:
        time.sleep(0.01)
    assert len(node.oneclient_tokens) == 2  # the second oneclient was started on the rewritten token file
    sandbox.stop(force=True)
    restorer.join(10)
    assert not restorer.is_alive()
    assert order == [("teardown", False)]  # the restore had ended when the teardown began
    assert answers[0]["ok"] is False
    assert "being stopped" in answers[0]["why"]
    assert not sandbox.token_files_left()
    assert node.mounted_at is None
    assert sandbox.stopped


# -------------------------------------------------------------------------------- the run ends with a restore pending
def stuck_cell_with_restore_pending(node, box):
    node.cell_handler = node.stuck
    result = box.exec("read_a_stopped_providers_file()", 1.2)
    assert result.restore_pending is True
    node.cell_handler = node.default_cell
    return result


def test_o97_a_run_whose_last_cell_was_aborted_collects_with_no_restore_and_stops_with_the_mount_unmounted(
    node, box, free_by_fusectl, tmp_path
):
    """O97: the blocked read was the agent's last cell: the body ends with the restore pending, which is then
    `not_needed`; the collection reads the one generation's log as it always did, and the stop unmounts the dead
    mount."""
    stuck_cell_with_restore_pending(node, box)
    node.cell()  # a proof in the window
    box.collect(tmp_path / "out")
    record = deadline_of(box)
    assert record["restore"] == DL.RESTORE_NOT_NEEDED
    assert record["outcome"] == DL.MOUNT_ABORTED
    assert box._cells.ended is None
    logs = box.mount_watch_logs()
    assert len(logs) == 1
    assert box.mount_watch_log() == logs[0]
    meta = box.metadata()["mount"]
    assert meta["guard"]["verdict"] == "clean"
    assert meta["remounts"] == 0
    assert box.infra_check()["deaths"] == []
    box.stop()
    assert node.mounted_at is None
    assert node.mount_count == 1


def test_o97_the_same_when_oneclient_ended_with_the_kill_it_is_the_harness_s_end_not_a_death(
    node, tmp_path, fusectl_root, monkeypatch
):
    sandbox = make_box(node, tmp_path, fusectl_root)  # no fusectl directory: the abort kills oneclient
    try:
        stuck_cell_with_restore_pending(node, sandbox)
        node.cell()
        sandbox.collect(tmp_path / "out")
        assert deadline_of(sandbox)["restore"] == DL.RESTORE_NOT_NEEDED
        assert node.freed_by == ["daemon_death"]
        check = sandbox.infra_check()
        assert check["deaths"] == []
        assert check["containers"]["mount"]["stopped_by_harness"] is True
        assert sandbox.metadata()["mount"]["guard"]["verdict"] == "clean"
        sandbox.stop()
        assert node.mounted_at is None
    finally:
        node.free("teardown")
        sandbox.stop(force=True)


def test_o97_the_restore_is_given_a_total_deadline_below_the_bound_the_runner_waits_for_it(
    node, box, free_by_fusectl, monkeypatch
):
    """O97: past `cells.RESTORE_WAIT_S` the runner gives up on a restore and the teardown goes on, so the restore must
    end by itself before that: it is given its own total deadline, a margin shorter."""
    from expcore_onedata.sandbox.apptainer import sandbox as SB

    given = []
    real = box._mount.restore

    def restore(**kwargs):
        given.append(kwargs)
        return real(**kwargs)

    stick(node, box)
    box.exec("read_a_stopped_providers_file()", 1.2)
    monkeypatch.setattr(box._mount, "restore", restore)
    box.restore_mount()
    assert given == [{"timeout_s": SB.RESTORE_BUDGET_S}]
    assert SB.RESTORE_BUDGET_S < CELLS.RESTORE_WAIT_S
