"""The plugin of the HPC rehearsal's study (tests/e2e/test_hpc_rehearsal.py): the example study's tools, script and
scorer, each run in the Apptainer sandbox on a fake HPC node, with the egress and mount-guard instruments.

The rehearsal copies this file into its study as `hpc_plugin.py`, beside the example's `plugin.py`, which it imports
relatively. Its settings are `hpc_rehearsal.json` beside it, which the study's code lists: where the fakes are (the
dispatcher runs under `python -I`, so nothing of the test tree is on its path), the control directory, and the pinned
oneclient the mount helper starts.

**The platform** is read from `<EXPCORE_FED_ROOT>/platform.toml`, which the federation's `up` rendered, and only in the
executor, at the first run: `EXPCORE_FED_ROOT` is in the shard step's allowlist, and `check`, `analyze` and `report`
build this plugin on the login node, whose environment names no federation. Each run gets a fake node of its own
(`fake_mount_node.FakeMountNode`, a stand-in for `apptainer.node.LocalNode`), and the sandbox is the real
`ApptainerSandbox` with `[sandbox]` as the platform file has it, its data path the mount (`mounted`), its zone and
local provider the platform file's. The data-path token is invented: no token plane runs offline.

**Holds** (how the test controls a run in flight): the n-th attempt this process runs, when the control directory
holds `hold-<label>-<n>`, writes `held-<label>-<n>` in its first lookup, once that lookup's cell ran, and waits there
(the agent's clock paused) until `go-<label>-<n>` exists. `<label>` is the shard's, from the dispatcher's argv.
"""

import asyncio
import dataclasses
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from expcore.campaign.executor import RunRequest, record_run
from expcore.egress import EgressLog
from expcore.mountguard import MountGuard
from expcore.plugin import CallContext, ToolOutput
from expcore.prompt.templates import PromptTemplate
from expcore.records.instruments import finish_instruments, start_instruments
from expcore.records.record import RunRecord
from expcore.study.plugin import StudyContext, StudyPlugin
from expcore.tools.function_provider import FunctionProvider

from .plugin import HINTS, FactsServed, Shelf, script_for, submit, tool_specs
from .plugin import build as example_build

HERE = Path(__file__).resolve().parent
SETTINGS = json.loads((HERE / "hpc_rehearsal.json").read_text(encoding="utf-8"))
DATA_PATH = "mounted"  # the level of [sandbox.data_paths] every run takes: a mount, so the mount guard has a run
TOKEN = "an-invented-data-path-token"  # never a real one: no token plane runs offline
CELL_MARK = "# a rehearsal cell\n"  # the agent's cells, told apart from the harness's own (the canary, the probes)
HOLD_LIMIT_S = 240.0  # a hold never released fails its run rather than hang the dispatcher
WAIT = "toolserver_lifecycle"  # the kind the hold's wait is measured under: the agent's clock is paused


def shard_label() -> str:
    """The shard this dispatcher runs, from its argv (`run STUDY ... --shard-label L`); `whole` unsharded."""
    argv = sys.argv
    return argv[argv.index("--shard-label") + 1] if "--shard-label" in argv else "whole"


def hold(label: str, n: int, ctx: CallContext) -> None:
    """The test's hold of attempt `n` of this process, when it asked for one (the module's docstring)."""
    control = Path(SETTINGS["control"])
    if not (control / f"hold-{label}-{n}").exists():
        return
    with ctx.infrastructure_wait(WAIT):
        (control / f"held-{label}-{n}").write_text(f"{os.getpid()}\n", encoding="utf-8")
        deadline = time.monotonic() + HOLD_LIMIT_S
        while not (control / f"go-{label}-{n}").exists():
            if time.monotonic() > deadline:
                raise RuntimeError(f"the rehearsal's hold of attempt {n} was never released")
            time.sleep(0.02)


def cell_handler(node: Any) -> Callable[[Any], Any]:
    """The fake node's answer to a cell: the harness's own as the fake gives them, and the agent's after the mount
    watcher's proof that the cell is isolated (a real watcher proves every cell)."""
    from expcore_onedata.sandbox.hostops import CmdResult

    def handle(cell: Any) -> Any:
        if not cell.program.startswith(CELL_MARK):
            return node.default_cell(cell)
        node.cell()
        return CmdResult(0, "looked up\n")

    return handle


def dispatcher_node() -> Any:
    """A fake node (`fake_mount_node.FakeMountNode`) whose driver is this dispatcher process, as on a real node.

    The fake's own driver is one invented process, alive on every fake node, so a run record left by a dispatcher
    killed outright would name a driver that every later fake node shows alive, and the next start's reaper
    (`apptainer.runs.reap`) would never take it. Here the driver is this process by its pid and start time
    (`LocalNode.identity`), and a process the fake did not make is answered from the host's /proc (`LocalNode.alive`):
    the killed dispatcher is gone there, as it would be on the node."""
    from fake_mount_node import FakeMountNode
    from fake_node import OWN_NS, FakeProc

    from expcore_onedata.sandbox.apptainer.node import LocalNode

    class DispatcherNode(FakeMountNode):
        def __init__(self) -> None:
            super().__init__()
            self.host = LocalNode()
            self.owner = self.host.identity()
            self.procs[self.owner.pid] = FakeProc(self.owner.pid, self.owner.start, OWN_NS, "driver", role="driver")

        def alive(self, pid: int, start: str) -> bool | None:
            if pid in self.procs:
                return super().alive(pid, start)
            return self.host.alive(pid, start)

    return DispatcherNode()


class Sandboxes:
    """The platform file, loaded once, and a sandbox of the Apptainer backend on a fake node of its own per run."""

    def __init__(self) -> None:
        self._config: Any = None
        self._lock = threading.Lock()

    def config(self) -> Any:
        with self._lock:
            if self._config is None:
                from expcore_onedata.config import load_platform

                self._config = load_platform(Path(os.environ["EXPCORE_FED_ROOT"]) / "platform.toml")
            return self._config

    def start(self, run_key: str) -> Any:
        if SETTINGS["support"] not in sys.path:
            sys.path.insert(0, SETTINGS["support"])
        from expcore_onedata.sandbox.api import Endpoint, SandboxSpec
        from expcore_onedata.sandbox.apptainer.hostmount import host_mount_factory
        from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
        from expcore_onedata.sandbox.apptainer.settings import Pinned
        from expcore_onedata.tokens import Secret

        config = self.config()
        node = dispatcher_node()
        node.cell_handler = cell_handler(node)
        oneclient = Pinned(Path(SETTINGS["oneclient"]["path"]), SETTINGS["oneclient"]["sha256"])
        spec = SandboxSpec(
            run_key=run_key,
            mode=config.sandbox.mode_of(DATA_PATH),
            token=Secret(TOKEN),
            zone=Endpoint.from_url(config.zone.url),
            provider=Endpoint.from_url(config.local_provider.url),
        )
        factory = host_mount_factory(oneclient, mountinfo=node.mountinfo)
        return ApptainerSandbox.start(node, config.sandbox, spec, mount_helper=factory)


class HpcExecutor:
    """The example's executor, with every run in its own sandbox: the lookup runs a cell there, and the egress and
    mount-guard instruments read the sandbox's logs once its collection has stopped the collector and the watcher."""

    def __init__(self, context: StudyContext) -> None:
        self.context = context
        self.template = PromptTemplate.from_file(HERE / "prompt.txt")
        self.sandboxes = Sandboxes()
        self.label = shard_label()
        self.attempts = 0
        self.lock = threading.Lock()

    def provider(self, tools: str, shelf: Shelf, box: Any, n: int) -> FunctionProvider:
        held = threading.Event()

        def lookup(arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext) -> ToolOutput:
            result = shelf.lookup(arguments, timeout_s=timeout_s, ctx=ctx)
            box.exec(f"{CELL_MARK}print({json.dumps(str(arguments.get('topic')))})", max(1.0, min(timeout_s, 30.0)))
            if not held.is_set():
                held.set()
                hold(self.label, n, ctx)
            return result

        lookup_spec, submit_spec = tool_specs(tools)
        return FunctionProvider([(lookup_spec, lookup), (submit_spec, submit)])

    async def __call__(self, request: RunRequest) -> RunRecord:
        with self.lock:
            self.attempts += 1
            n = self.attempts
        study, factors = self.context.study, request.identity.factors
        condition = study.schema.condition_named(factors, request.run_key)
        request.backend.use(script_for(condition, factors["task"], factors["model"], factors["tools"]))
        task = study.tasks[factors["task"]]
        slots = {"TASK": task.id, "REGION": task.attributes["region"], "HINT": HINTS[factors["tools"]]}
        user = self.template.render(slots, study_run=True)
        shelf = Shelf()
        box = await asyncio.to_thread(self.sandboxes.start, request.run_key)
        try:
            instruments = [
                FactsServed(shelf),
                EgressLog(box, box.egress_declaration(), box.egress_nonce),
                MountGuard(box, box.mount_guard_nonce),
            ]
            identity = {"run_key": request.run_key, "factors": dict(factors), "attempt": request.identity.attempt}
            await asyncio.to_thread(start_instruments, instruments, identity)
            record = await record_run(
                request,
                [self.provider(factors["tools"], shelf, box, n)],
                "You are a careful assistant.",
                user,
                study.campaign.agent,
                code=dict(self.context.code),
            )
            # the collection stops the collector and the watcher, whose logs the instruments read; its files go
            # outside the run's directory, where every .json is a record to the replay check
            with tempfile.TemporaryDirectory(prefix="collected-") as collected:
                await asyncio.to_thread(box.collect, Path(collected))
                record = await asyncio.to_thread(finish_instruments, record, instruments)
            record.write(request.run_dir)
            return record
        finally:
            await asyncio.to_thread(box.stop)


def build(context: StudyContext) -> StudyPlugin:
    """The example's plugin with this executor: its scorer, hashes and platform hooks unchanged."""
    return dataclasses.replace(example_build(context), executor=HpcExecutor(context))
