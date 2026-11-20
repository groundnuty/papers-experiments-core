"""O97: a mount run built the way production builds it, through `SandboxPlatform.launch` on the fake node. The
platform's `_spec` supplies what the deadline's verdict needs (the holder of each space from `[[spaces]]`, and the
reader of what the run's injection holds down, taken inside the launch), the sandbox builds its escalation and its
runner's diagnosis from the spec, and the test passes the sandbox nothing: a hand-built sandbox could not tell a
missing wire from a working one. A survivor in D on the held space then drives the deadline to `mount_restored`; with
no hold it ends the run as `cell_unkillable`."""

import asyncio
import contextlib
import dataclasses

import pytest
from apptainer_support import make_settings, pinned_bundle
from apptainer_support import options as node_options
from fake_mount_node import FUSE_DEV, fusectl_tree
from fake_survivor_proc import DATA_DEVICE, DATA_MOUNT, MOUNTS, syscall_text
from reader_node import ReaderNode
from sandbox_support import add_sandbox

from expcore.errors import InfraError
from expcore_onedata import fault_owner
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox import fusectl as FC
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.testing.world import with_placement

RUN = "run-1"
CEILING_S = 600.0


class Context:
    """A call context that lets every wait through."""

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        yield


@pytest.fixture(autouse=True)
def empty_registry():
    fault_owner.reset_held()
    yield
    fault_owner.reset_held()


@pytest.fixture
def config(tmp_path, fake) -> PlatformConfig:
    """The platform document of this directory with the Apptainer backend's settings in place of its `[sandbox]`."""

    def edit(data):
        add_sandbox(data)
        with_placement(data, holder="beta")  # the one space's data provider, so the platform maps it to a label

    loaded = load_platform(write_platform(tmp_path, edit))
    (tmp_path / "node").mkdir()
    settings = make_settings(tmp_path / "node", oneclient_bundle=pinned_bundle(tmp_path / "node" / "files"))
    return dataclasses.replace(loaded, sandbox=settings)


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


@pytest.fixture(autouse=True)
def free_by_fusectl(monkeypatch, node):
    """The kernel's effect of the fusectl write: the reader is freed."""
    real = FC.write_abort

    def write(root, connection):
        real(root, connection)
        node.free("fusectl")

    monkeypatch.setattr(FC, "write_abort", write)


@pytest.fixture
def platform(config, issuer, node, fusectl_root):
    options = {k: v for k, v in node_options().items() if k != "registry"}
    options["mount_helper"] = HM.host_mount_factory(mountinfo=node.mountinfo, fusectl_root=fusectl_root)
    return SandboxPlatform(
        config, issuer, node, events=lambda kind, detail: None, registry=Registry(), sandbox_options=options,
        clock=lambda: NOW,
    )  # fmt: skip


def stick_on(node, space: str) -> None:
    """The cell's reader is in D in `read(2)` of a file of `space` on the run's FUSE mount, as the node's `/proc`
    shows it."""
    original = node.stuck

    def stuck(cell):
        node.reader = cell.procs[1]
        table = MOUNTS.replace(DATA_DEVICE, FUSE_DEV)
        process = node.survivor_proc.process(node.reader.pid, start=node.reader.start, mounts=table)
        process.task(node.reader.pid, syscall=syscall_text(0, 3, 0x7F10, 0x1000), wchan="request_wait_answer")
        process.fd(3, f"/data/{space}/a.bin", mnt_id=DATA_MOUNT)
        return original(cell)

    node.cell_handler = stuck


def launched(platform, body):
    async def go():
        async with platform.launch(Context(), run_key=RUN, level="mounted", ceiling_s=CEILING_S) as sandbox:
            body(sandbox)
            return sandbox

    return asyncio.run(go())


def held_space(platform) -> tuple[str, str]:
    """A space of the platform's document and the label of its holder, as the platform maps them."""
    holders = platform._config.space_holders()
    assert holders, "the test platform declares no holder for any space"
    return next(iter(holders.items()))


def test_o97_the_platform_gives_a_mount_run_the_holders_and_a_reader_of_what_its_injection_holds(platform):
    """O97: the spec a mount run is launched with has both inputs, and the sandbox built from it a restorable
    escalation with an attribution and a runner with a diagnosis."""
    space, label = held_space(platform)
    seen = {}

    def body(sandbox):
        seen["spec"] = sandbox.spec
        seen["escalation"] = sandbox._cells._escalation
        seen["diagnosis"] = sandbox._cells.diagnosis

    sandbox = launched(platform, body)
    assert isinstance(sandbox, ApptainerSandbox)
    assert seen["spec"].space_holders[space] == label
    assert seen["spec"].held_down() == frozenset()
    assert seen["escalation"].restorable is True
    assert seen["escalation"].attribute is not None
    assert seen["diagnosis"] is not None
    assert seen["diagnosis"].roles().device == FUSE_DEV


def test_o97_a_survivor_in_d_on_the_held_space_drives_the_deadline_to_mount_restored_and_the_run_goes_on(
    platform, node
):
    """O97: the hold is the run's injection's, registered for the run; the reader is blocked on a file of the space
    whose holder it holds down: the verdict is the agent's, the mount is restored, and the next cell runs."""
    space, label = held_space(platform)
    fault_owner.hold(RUN, label, attempt=fault_owner.attempt_key())
    seen = {}

    def body(sandbox):
        stick_on(node, space)  # after the start: its own canary cell is no read of the mount
        first = sandbox.exec("read_a_stopped_providers_file()", 1.2)
        seen["pending"] = (first.restore_pending, first.kill["reason"], first.kill["category"])
        seen["verdict"] = sandbox.metadata()["cell_deadlines"][0]["attribution"]
        node.cell_handler = node.default_cell
        seen["restored"] = sandbox.restore_mount()
        seen["outcome"] = sandbox.metadata()["cell_deadlines"][0]["outcome"]
        seen["next"] = sandbox.exec("print('next')", 30).exit_code

    launched(platform, body)
    assert seen["pending"] == (True, "time_limit", "agent")
    assert seen["verdict"]["verdict"] == "agent"
    assert seen["verdict"]["why"] == "blocked_on_held_down_holder"
    assert seen["verdict"]["target"] == label
    assert seen["restored"]["ok"] is True
    assert seen["restored"]["generation"] == 2
    assert seen["outcome"] == DL.MOUNT_RESTORED
    assert seen["next"] == 0


def test_o97_the_same_survivor_with_no_hold_ends_the_run_as_cell_unkillable(platform, node):
    """O97: nothing is held down for this run: no injection in effect, so the block is the harness's."""
    space, _ = held_space(platform)
    seen = {}

    def body(sandbox):
        stick_on(node, space)
        first = sandbox.exec("read_a_stopped_providers_file()", 1.2)
        seen["restore_pending"] = first.restore_pending
        seen["reason"] = first.kill["reason"]
        seen["verdict"] = sandbox.metadata()["cell_deadlines"][0]["attribution"]
        with pytest.raises(InfraError) as refused:
            sandbox.exec("print('next')", 30)
        seen["kind"] = refused.value.kind

    with pytest.raises(InfraError) as ended:
        launched(platform, body)  # the launch ends the run on leaving, as it does for any run a deadline ended
    assert ended.value.kind == "cell_unkillable"
    assert seen["restore_pending"] is False
    assert seen["reason"] == "kill_survived"
    assert seen["verdict"]["why"] == "no_injection_in_effect"
    assert seen["kind"] == "cell_unkillable"


def test_o97_a_hold_of_another_run_is_not_this_runs(platform, node):
    """O97: the registry is keyed by the run, so another run's hold never makes this one's block the agent's."""
    space, label = held_space(platform)
    fault_owner.hold("another-run", label, attempt=fault_owner.attempt_key())
    seen = {}

    def body(sandbox):
        stick_on(node, space)
        seen["restore_pending"] = sandbox.exec("read_a_stopped_providers_file()", 1.2).restore_pending

    with pytest.raises(InfraError) as ended:
        launched(platform, body)
    assert ended.value.kind == "cell_unkillable"
    assert seen["restore_pending"] is False
