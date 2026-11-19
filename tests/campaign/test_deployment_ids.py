"""B20: each attempt keeps the set of deployments that served its turns, and the export says it per analysed attempt.

The fingerprint mix of B18 is kept only for a model that declares a set. The deployment a front names in a response
header is wanted for any model that sets `deployment_header`, so the analysis can use the deployment as a sensitivity
factor: the attempt keeps the distinct ids its turns carried, and its row of the export (`analysed_attempts`) has
them as `deployment_ids`, a sorted list, only where a turn carried one.
"""

import json

from campaign_support import make_manifest, make_schema
from loop_support import FakeProvider
from record_support import CODE, make_config
from scheduler_support import Factory, FakeExecutor, ReportingBackend, build

from expcore.campaign.executor import record_run
from expcore.campaign.export import export_run_state
from expcore.campaign.export_join import join_exports
from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult
from expcore.stats.rows import Row

PLAN = {"tasks": ("task-a",), "seeds": (1,), "models": ("m1",)}  # three runs, one per condition


class NamedBackend(ReportingBackend):
    """A stub whose replies each name a deployment, as the OpenAI-compatible adapter's do with `deployment_header`."""

    deployments: list[str | None]

    async def step(self):
        turn = await super().step()
        turn.deployment_id = self.deployments.pop(0)
        return turn


class NamedFactory(Factory):
    def __call__(self, spec, *, seed, limits, base_url, time_left):
        backend = NamedBackend(spec.key, seed, base_url)
        backend.deployments = []
        self.built.append(backend)
        return backend


def named(*deployments):
    """A run of one reply per entry, each naming that deployment (None: its reply named none)."""

    async def run(request):
        request.backend.deployments = list(deployments)
        request.backend.use([[("look", {})]] * (len(deployments) - 1))
        provider = FakeProvider({"look": lambda arguments: "ok"})
        return await record_run(
            request, [provider], "be brief", "do the task", make_config(), code=dict(CODE), core_version="0.1.0"
        )

    return run


def order():
    return [row.key.text for row in make_manifest(make_schema(), **PLAN).rows]


def test_f02_an_attempt_keeps_the_distinct_deployments_of_its_turns_and_the_export_says_so_per_attempt(tmp_path):
    first, second, third = order()
    behaviours = {first: [named("dep-b", "dep-a", "dep-b")], second: [named("dep-a", None)], third: [named(None)]}
    with build(tmp_path, executor=FakeExecutor(behaviours), factory=NamedFactory(), plan=PLAN) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        kept = [store.q("SELECT deployment_ids FROM attempts WHERE run_key=?", (key,))[0][0] for key in order()]
        assert [None if text is None else json.loads(text) for text in kept] == [["dep-a", "dep-b"], ["dep-a"], None]
        export = export_run_state(store)
    rows = {row["run_key"]: row for row in export["analysed_attempts"]}
    assert rows[first]["deployment_ids"] == ["dep-a", "dep-b"]
    assert rows[second]["deployment_ids"] == ["dep-a"]
    assert "deployment_ids" not in rows[third]  # absent where no turn named one, never an empty list or a null


def test_f02_a_campaign_whose_turns_name_no_deployment_exports_exactly_what_it_did(tmp_path):
    """B20: no `deployment_ids` key anywhere in the export of a campaign whose replies never named a deployment."""
    with build(tmp_path, plan=PLAN) as scheduler:
        scheduler.run()
        export = export_run_state(scheduler.store)
    assert export["analysed_attempts"]
    assert all("deployment_ids" not in row for row in export["analysed_attempts"])
    assert "deployment_ids" not in json.dumps(export)


def test_b20_a_row_with_deployment_ids_passes_through_the_join_the_analysis_row_and_the_report(tmp_path):
    """B20: the readers of an analysed attempt take the keys they need: a merged export keeps the ids on each entry, the
    analysis's row is built as before, and the report renders (a reader that refused the key would stop the first
    campaign's analysis)."""
    first, second, third = order()
    behaviours = {first: [named("dep-b", "dep-a")], second: [named("dep-a")], third: [named(None)]}
    with build(tmp_path, executor=FakeExecutor(behaviours), factory=NamedFactory(), plan=PLAN) as scheduler:
        scheduler.run()
        export = export_run_state(scheduler.store)
    seq = {key: index for index, key in enumerate(order())}
    joined = join_exports({1: export}, seq, [{"index": 1, "label": "shard-1"}])
    assert [row.get("deployment_ids") for row in joined["analysed_attempts"]] == [["dep-a", "dep-b"], ["dep-a"], None]
    entry = export["analysed_attempts"][0]
    assert Row.from_export(entry, {}).run_key == entry["run_key"]
    result = AnalysisResult((), (), 0.05, {"preregistration_sha256": "e" * 64})
    text = render_report(result, export, study="a-study", code={"commit": "c" * 40}, core_version="1.2.3")
    assert "analysed" in text
