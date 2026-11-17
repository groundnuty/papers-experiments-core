"""The plugin of the faults-in-shards rehearsal (tests/study/test_faults_in_model_shards.py): the example study's tools,
script and scorer, with its `relay-down` condition's treatment a real provider outage, taken by the Onedata layer's
`FaultInjector` over the fake Compose services of `fault_support`.

The rehearsal copies this file into every copy of the study as `fault_plugin.py`, beside the example's `plugin.py`,
which it imports relatively. The file is the same in every copy, the whole campaign's, each shard's and the merge
target's: what differs between federations is the `platform.toml` beside `study.toml`, which no `[study] code` lists.

**The platform** is that `platform.toml` (`[faults]` included, its journal and its Compose file named in the
federation's own root). Without one (the merge target, `check`, `analyze` and `report`, which run no attempt) the
example's plugin is returned as it is. With one the plugin passes what the Onedata layer's own studies pass: the
platform's half of the campaign fingerprint (`PlatformConfig.fingerprint`), its run admission (`run_admission`, which
holds the federation exclusively for an outage run) and a health check that notes the federation's link epoch as the
fault gate does, which that admission needs.

**The outage** is the condition's declared treatment: `[conditions.relay-down] infrastructure_is_treatment` names
`provider_outage` on `providers[1]`. The executor starts the injector the way the fault injector's module documents:
`prepared()`, the probes' baseline, `inject()`, then the agent, then `finish_instruments` with the injector first in the
list, so the probes finish before the restore. Nothing is scripted: the stand-in `lookup` reads the fact from the
provider that holds it, in the federation's own services file, and raises `provider_outage` only when that provider is
not running. The fact of the task `lake` is held by `providers[1]` (beta), every other task's by `providers[0]`, so the
provider is down in every `relay-down` run and only the `lake` run's read meets it, the outcome shape the example has.
"""

import asyncio
import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from fault_support import SERVICES, FakeClock, FakeServices, injector_for

from expcore.campaign import prerun
from expcore.campaign.executor import RunRequest, record_run
from expcore.campaign.healthgate import Health
from expcore.plugin import CallContext, ToolOutput
from expcore.prompt.templates import PromptTemplate
from expcore.records.instruments import finish_instruments, start_instruments
from expcore.records.record import RunRecord
from expcore.study.plugin import StudyContext, StudyPlugin
from expcore.tools.function_provider import FunctionProvider
from expcore_onedata.admission import run_admission
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.fault_epoch import GATE_NOTE, LinkEpoch, noted_of
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.faults import PROVIDER_OUTAGE, provider_outage

from .plugin import HINTS, FactsServed, Shelf, script_for, submit, tool_specs
from .plugin import build as example_build

HERE = Path(__file__).resolve().parent
PLATFORM = HERE / "platform.toml"  # this federation's, which the rehearsal wrote beside study.toml
STATE_FILE = "services.json"  # the federation's Compose services, in its root beside the journal
HOLDER = {"lake": "beta"}  # the provider that holds the fact of a task; every other task's is alpha's
PLAIN = "no-special-run"  # a condition the example's script table does not know: its plain script, whatever the task


class FederationShelf(Shelf):
    """The shelf of one run, reading each fact from the provider that holds it: a provider that is not running does not
    answer, which is `provider_outage` naming it, as the platform's tools raise it."""

    def __init__(self, config: PlatformConfig, services: FakeServices) -> None:
        super().__init__()
        self.config, self.services = config, services

    def lookup(self, arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext) -> ToolOutput:
        holder = HOLDER.get(str(arguments.get("topic")), "alpha")
        if not self.services.running(SERVICES[holder]):
            raise provider_outage(self.config, holder, "the provider did not answer the read")
        return super().lookup(arguments, timeout_s=timeout_s, ctx=ctx)


def noting_health(journal: Path) -> Any:
    """A health check that passes and notes the federation's link epoch in the try's note, as the fault gate's pre-run
    check does; the platform's admission refuses a run whose check noted nothing."""

    def check() -> Health:
        prerun.note(GATE_NOTE, noted_of(LinkEpoch(journal).ensure()))
        return Health(True, "the federation is up")

    return check


class FaultedExecutor:
    """The example's executor, with the real injector around every run whose condition declares an outage."""

    def __init__(self, context: StudyContext, config: PlatformConfig) -> None:
        self.context, self.config = context, config
        self.settings = load_faults(PLATFORM, config)
        self.services = FakeServices(path=self.settings.journal.parent / STATE_FILE)
        self.template = PromptTemplate.from_file(HERE / "prompt.txt")

    def script(self, condition: str, task: str, model: str, tools: str) -> list[Any]:
        """The example's script, but for a `relay-down` run, whose failure is the provider's outage and not a scripted
        one: the run is the plain one, and the lookup of the task whose fact the down provider holds fails by itself."""
        return script_for(PLAIN if condition == "relay-down" and task == "lake" else condition, task, model, tools)

    def baseline_then_inject(self, injector: Any, instruments: list[Any], identity: dict[str, Any]) -> None:
        """The injector's start in one worker thread: its admission and recovery, the probes' baseline, the stop."""
        with injector.prepared(identity) as prepared:
            start_instruments(instruments, identity)
            prepared.inject()

    async def __call__(self, request: RunRequest) -> RunRecord:
        study, factors = self.context.study, request.identity.factors
        condition = study.schema.condition_named(factors, request.run_key)
        request.backend.use(self.script(condition, factors["task"], factors["model"], factors["tools"]))
        task = study.tasks[factors["task"]]
        slots = {"TASK": task.id, "REGION": task.attributes["region"], "HINT": HINTS[factors["tools"]]}
        user = self.template.render(slots, study_run=True)
        shelf = FederationShelf(self.config, self.services)
        instruments: list[Any] = [FactsServed(shelf)]
        identity = {"run_key": request.run_key, "factors": dict(factors), "attempt": request.identity.attempt}
        target = request.treatment_targets.get(PROVIDER_OUTAGE) if PROVIDER_OUTAGE in request.treatment_kinds else None
        injector = None
        if target is None:
            await asyncio.to_thread(start_instruments, instruments, identity)
        else:  # a fake clock, so its settle and ready waits cost no time here
            clock = FakeClock()
            injector = injector_for(PLATFORM, self.services, target, clock)
            await asyncio.to_thread(self.baseline_then_inject, injector, instruments, identity)
        try:
            lookup_spec, submit_spec = tool_specs(factors["tools"])
            provider = FunctionProvider([(lookup_spec, shelf.lookup), (submit_spec, submit)])
            record = await record_run(
                request,
                [provider],
                "You are a careful assistant.",
                user,
                study.campaign.agent,
                code=dict(self.context.code),
            )
        except BaseException:
            if injector is not None:  # no record carries the restore's failure, so the failure is raised from here
                await asyncio.to_thread(injector.clear)
            raise
        finishing = instruments if injector is None else [injector, *instruments]  # probes finish first
        record = await asyncio.to_thread(finish_instruments, record, finishing)
        record.write(request.run_dir)
        return record


def environment_of(platform: Any) -> Any:
    """What the shard ran against, for a merge to compare: the platform's fingerprint by its SHA-256, which holds
    nothing of one federation (its journal, its Compose project and its services' state are not in it)."""

    def identity() -> dict[str, str]:
        text = json.dumps(platform, sort_keys=True, separators=(",", ":"))
        return {"platform": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    return identity


def build(context: StudyContext) -> StudyPlugin:
    """The example's plugin; with a platform file beside study.toml, the executor above and the platform's admission,
    health check and fingerprint."""
    plugin = example_build(context)
    if not PLATFORM.is_file():
        return plugin
    config = load_platform(PLATFORM, require_secrets=False)
    settings = load_faults(PLATFORM, config)
    fingerprint = config.fingerprint()
    return dataclasses.replace(
        plugin,
        executor=FaultedExecutor(context, config),
        health=noting_health(settings.journal),
        admission=run_admission(config, context.study.schema),
        platform=fingerprint,
        environment_identity=environment_of(fingerprint),
    )
