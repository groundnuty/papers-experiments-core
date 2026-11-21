"""The plugin of the O97 proof's minimal study: one task, a registered outage and its control, a stub that plays the
proof's script, and the platform's own launch and injector (written by tests/live/fuse_abort_apptainer.py).

`fuse_proof.json` beside this file says where the shared pieces are (`support`, put on the path here: the campaign runs
under `python -I`), which world the run is in (`live`: the platform's real objects over the federation's platform file;
`fake`: the dry run's `fuse_proof_fake.CURRENT`, in the same process), the target, the files' paths in the mount and
where the collection goes. The executor is the shape a platform study's executor has (`docs/study-guide.md`): the
sandbox through `launch` inside the attempt, the injector through `running` around the loop, the mount guard started
before and both finished after the collection. Nothing here is a fake.
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SETTINGS = json.loads((HERE / "fuse_proof.json").read_text(encoding="utf-8"))
if SETTINGS["support"] not in sys.path:
    sys.path.insert(0, SETTINGS["support"])

import fuse_proof as FP  # noqa: E402

from expcore.campaign.executor import RunRequest, record_run  # noqa: E402
from expcore.campaign.healthgate import Health  # noqa: E402
from expcore.mountguard import MountGuard  # noqa: E402
from expcore.prompt.templates import PromptTemplate  # noqa: E402
from expcore.records.instruments import finish_instruments, start_instruments  # noqa: E402
from expcore.records.record import RunRecord  # noqa: E402
from expcore.study.plugin import StudyContext, StudyPlugin, tools_sha256  # noqa: E402
from expcore_onedata.sandbox.codetool import CodeExecutionProvider  # noqa: E402

SYSTEM = "You run Python in a sandbox to read data files."
OUTAGE = "owner_down"


def world() -> Any:
    """The run's world: the platform's real objects (`live`), or the dry run's fake, made by the same process."""
    if SETTINGS["world"] == "fake":
        import fuse_proof_fake

        return fuse_proof_fake.CURRENT
    from fuse_proof_world import LiveWorld

    return LiveWorld(Path(SETTINGS["platform"]))


class ProofScorer:
    """Whether the run finished: the one outcome the contrast names. The proof reads records, never this."""

    name = "fuse-proof"
    version = "1"

    def score(self, record: RunRecord) -> dict[str, Any]:
        finished = record.outcome["name"] == "finished"
        return {
            "axes": {"agent": "finished" if finished else "other", "platform": "ran"},
            "verdict": "fulfilled" if finished else "violated",
            "outcomes": {"finished": 1.0 if finished else 0.0},
        }


class ProofExecutor:
    def __init__(self, context: StudyContext, here: Any) -> None:
        self.context, self.world = context, here
        self.template = PromptTemplate.from_file(HERE / "prompt.txt")

    async def __call__(self, request: RunRequest) -> RunRecord:
        study, factors = self.context.study, request.identity.factors
        outage = study.schema.condition_named(factors, request.run_key) == OUTAGE
        request.backend.use(
            FP.script(
                SETTINGS["owner_path"],
                SETTINGS["local_path"],
                SETTINGS["root_path"],
                second_cycle=outage and SETTINGS["second_cycle"],
            )
        )
        user = self.template.render({"TASK": request.identity.factors["task"]}, study_run=True)
        request.declare_secrets()
        identity = {"run_key": request.run_key, "factors": dict(factors), "attempt": request.identity.attempt}
        agent = study.campaign.agent
        dest = Path(SETTINGS["collected"]) / re.sub(r"[^A-Za-z0-9_.-]", "_", request.run_key)
        async with self.world.launch(request.run_key, agent.ceiling_s) as box:
            guard = MountGuard(box, box.mount_guard_nonce)
            await asyncio.to_thread(start_instruments, [guard], identity)
            finishing: list[Any] = [guard]
            providers = [CodeExecutionProvider(box)]
            code = dict(self.context.code)
            if outage:
                injector = self.world.injector(SETTINGS["target"])
                async with injector.running(identity):  # the hold is this attempt's: the campaign's own context
                    record = await record_run(request, providers, SYSTEM, user, agent, code=code)
                finishing.insert(0, injector)
            else:
                record = await record_run(request, providers, SYSTEM, user, agent, code=code)
            await asyncio.to_thread(box.collect, dest)
            record = await asyncio.to_thread(finish_instruments, record, finishing)
        record.write(request.run_dir)
        return record


class AlwaysHealthy:
    """The fake world's health: the dry run has no federation to check."""

    def __call__(self) -> Health:
        return Health(True, "the dry run's fake federation is up")


def build(context: StudyContext) -> StudyPlugin:
    here = world()
    template = PromptTemplate.from_file(HERE / "prompt.txt")
    prompt = template.render({"TASK": "read"}).rendered_sha256
    tools = tools_sha256(CodeExecutionProvider(None).tools())
    conditions = list(context.study.schema.conditions)
    live = SETTINGS["world"] == "live"
    return StudyPlugin(
        executor=ProofExecutor(context, here),
        scorers=[ProofScorer()],
        prompts=dict.fromkeys(conditions, prompt),
        tools=dict.fromkeys(conditions, tools),
        health=here.health() if live else AlwaysHealthy(),
        admission=here.admission(context.study.schema) if live else None,
        platform=dict(here.fingerprint()),
    )
