"""The plugin of the HPC rehearsal's study (tests/e2e/test_hpc_rehearsal.py): the example study's tools, script and
scorer, each run in the Apptainer sandbox on a fake HPC node, with the egress and mount-guard instruments.

The rehearsal copies this file into its study as `hpc_plugin.py`, beside the example's `plugin.py`, which it imports
relatively. Its settings are `hpc_rehearsal.json` beside it, which the study's code lists: where the fakes are (the
dispatcher runs under `python -I`, so nothing of the test tree is on its path) and the control directory. The mount
helper's oneclient is the platform's: the bundle its `[sandbox.apptainer]` pins, as `up` rendered it from the site.

**The platform** is the `platform.toml` beside `study.toml`: the shard step copies its federation's there (the file
the federation's `up` rendered, its paths absolute into the root), so no path of a federation is in the study's own
files. `check`, `analyze` and `report` build this plugin on the login node, over a study with no such file: none is
read there. Each run gets a fake node of its own
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
PLATFORM = HERE / "platform.toml"  # the federation's, which the shard step copies beside study.toml
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


class Sandboxes:
    """The platform file, loaded once, and a sandbox of the Apptainer backend on a fake node of its own per run."""

    def __init__(self) -> None:
        self._config: Any = None
        self._lock = threading.Lock()

    def config(self) -> Any:
        with self._lock:
            if self._config is None:
                from expcore_onedata.config import load_platform

                self._config = load_platform(PLATFORM)
            return self._config

    def start(self, run_key: str) -> Any:
        if SETTINGS["support"] not in sys.path:
            sys.path.insert(0, SETTINGS["support"])
        from fake_mount_node import FakeMountNode

        from expcore_onedata.sandbox.api import Endpoint, SandboxSpec
        from expcore_onedata.sandbox.apptainer.hostmount import host_mount_factory
        from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
        from expcore_onedata.tokens import Secret

        config = self.config()
        node = FakeMountNode(own_process=True)  # its driver this dispatcher, so a killed one's record is reaped
        node.cell_handler = cell_handler(node)
        spec = SandboxSpec(
            run_key=run_key,
            mode=config.sandbox.mode_of(DATA_PATH),
            token=Secret(TOKEN),
            zone=Endpoint.from_url(config.zone.url),
            provider=Endpoint.from_url(config.local_provider.url),
        )
        factory = host_mount_factory(mountinfo=node.mountinfo)
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
        request.declare_secrets()  # before the sandbox and the instruments start: a failed start's diagnostic is kept
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
    """The example's plugin with this executor, its scorer, hashes and hooks unchanged; in a shard step, with the
    platform's half of the campaign fingerprint (`PlatformConfig.fingerprint`): the Apptainer sandbox's pins, and each
    space by its declared identity and its data's content hash (`data_manifest`, which `up` rendered and checked), so
    two federations holding the same data are one campaign, and another image or another file of data is another. On
    the login node (`check`, `analyze`, `report`) the study has no platform file and no run is made: none is read."""
    executor = HpcExecutor(context)
    plugin = dataclasses.replace(example_build(context), executor=executor)
    if not PLATFORM.is_file():
        return plugin
    return dataclasses.replace(plugin, platform=executor.sandboxes.config().fingerprint())
