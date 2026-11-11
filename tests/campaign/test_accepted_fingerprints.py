"""A model whose endpoint serves it from several deployments the study declared the same model (B18): admission,
the check of every run, the deployment mix each attempt records, and the shares in the export and the report. A model
that declares no set is checked against its baseline as before (C16)."""

import json

from campaign_support import NOW, make_manifest, make_schema, planned_store
from loop_support import FakeProvider
from record_support import CODE, make_config
from scheduler_support import Factory, FakeExecutor, ReportingBackend, attempts_of, build, event_kinds

from expcore.campaign.executor import record_run
from expcore.campaign.export import export_run_state
from expcore.campaign.identity import ACCEPTED_META, ServedIdentities
from expcore.config.models import ModelSpec, ModelTable
from expcore.config.prices import Price
from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult

MODEL = "kestrel"
KESTREL, PLOVER, OSPREY = "deploy-kestrel", "deploy-plover", "deploy-osprey"
PLAN = {"tasks": ("task-a",), "seeds": (1,), "models": (MODEL,)}  # three runs, one per condition


class DeploymentBackend(ReportingBackend):
    """A stub whose probe reports a served identity through `info()`, as the core's OpenAI-compatible adapter's does,
    and each of whose replies can come from another deployment."""

    def __init__(self, *args, probe_served=(None, None), **kwargs):
        super().__init__(*args, **kwargs)
        self.probe_served, self.probed = probe_served, False
        self.per_turn: list[tuple[str | None, str | None]] = []

    async def probe(self):
        reply = await super().probe()
        self.probed = True
        return reply

    def info(self):
        info = super().info()
        if self.probed:
            model, fingerprint = self.probe_served
            info["served_model_ids"] = [model] if model else []
            info["system_fingerprints"] = [fingerprint] if fingerprint else []
        return info

    async def step(self):
        turn = await super().step()
        if self.per_turn:
            turn.served_model, turn.system_fingerprint = self.per_turn.pop(0)
        return turn


class DeploymentFactory(Factory):
    """Builds deployment backends whose probe reports `probe_served`, a (served model, fingerprint) pair."""

    def __init__(self, probe_served=(MODEL, KESTREL)):
        super().__init__()
        self.probe_served = probe_served

    def __call__(self, spec, *, seed, limits, base_url, time_left):
        self.calls.append((spec.key, seed, base_url))
        backend = DeploymentBackend(spec.key, seed, base_url, probe_served=self.probe_served)
        self.built.append(backend)
        return backend


def served(*fingerprints, model=MODEL):
    """A run of one reply per fingerprint, each served by the model under that fingerprint."""

    async def run(request):
        request.backend.per_turn = [(model, fingerprint) for fingerprint in fingerprints]
        request.backend.use([[("look", {})]] * (len(fingerprints) - 1))
        provider = FakeProvider({"look": lambda arguments: "ok"})
        config = make_config()
        return await record_run(
            request, [provider], "be brief", "do the task", config, code=dict(CODE), core_version="0.1.0"
        )

    return run


def models(accepted=(KESTREL, PLOVER)) -> ModelTable:
    spec = ModelSpec(
        key=MODEL, backend="stub", api_id=MODEL, status="frozen", price=Price(3.0, 15.0), accepted_fingerprints=accepted
    )
    return ModelTable({MODEL: spec})


def order():
    return [row.key.text for row in make_manifest(make_schema(), **PLAN).rows]


def campaign(tmp_path, behaviours, *, accepted=(KESTREL, PLOVER), probe_served=(MODEL, KESTREL)):
    return build(
        tmp_path,
        executor=FakeExecutor(behaviours),
        factory=DeploymentFactory(probe_served),
        models=models(accepted),
        plan=PLAN,
    )


def mix(store, key):
    rows = store.q("SELECT deployments FROM attempts WHERE run_key=? ORDER BY id", (key,))
    return [None if row["deployments"] is None else json.loads(row["deployments"]) for row in rows]


def test_b18_a_run_served_by_two_accepted_fingerprints_is_accepted_and_its_mix_recorded(tmp_path):
    """B18: replies of one run that switch between two declared fingerprints are the same model: the run is scored,
    not set aside, and its attempt records how many turns each deployment served."""
    first, second, third = order()
    behaviours = {first: [served(KESTREL, PLOVER, KESTREL)], second: [served(PLOVER, PLOVER)], third: [served(KESTREL)]}
    with campaign(tmp_path, behaviours) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        assert [attempts_of(store, key) for key in (first, second, third)] == [[("done", 0, 1)]] * 3
        assert "served_identity_changed" not in event_kinds(store)
        assert mix(store, first) == [[[KESTREL, 2], [PLOVER, 1]]]
        assert mix(store, second) == [[[PLOVER, 2]]]


def test_b18_a_fingerprint_outside_the_set_is_refused_as_today(tmp_path):
    """B18: a reply served under a fingerprint the set does not list sets the run aside as infrastructure of the
    identity kind, and the run goes again; the event names the fingerprint and the set."""
    first, _, _ = order()
    behaviours = {first: [served(KESTREL, OSPREY), served(PLOVER)]}
    with campaign(tmp_path, behaviours) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        assert attempts_of(store, first) == [("infra_fail", 1, 1), ("done", 0, 2)]
        row = store.q("SELECT infra_kind, detail FROM attempts WHERE run_key=? ORDER BY id", (first,))[0]
        assert (row["infra_kind"], "served identity changed" in row["detail"]) == ("endpoint", True)
        (event,) = store.q("SELECT detail FROM events WHERE kind='served_identity_changed'")
        change = json.loads(event["detail"].split(": ", 1)[1])
        assert change == {"system_fingerprint": OSPREY, "accepted_fingerprints": [KESTREL, PLOVER]}


def test_b18_a_mixed_reply_counts_only_when_every_fingerprint_in_it_is_accepted(tmp_path):
    """B18: a reply that carries several fingerprints was served by those deployments: accepted when the set lists
    each of them, refused when it does not."""
    first, second, _ = order()
    behaviours = {first: [served(f"{KESTREL} | {PLOVER}")], second: [served(f"{KESTREL} | {OSPREY}"), served(KESTREL)]}
    with campaign(tmp_path, behaviours) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        assert attempts_of(scheduler.store, first) == [("done", 0, 1)]
        assert attempts_of(scheduler.store, second) == [("infra_fail", 1, 1), ("done", 0, 2)]


def test_c16_a_model_that_declares_no_set_is_checked_against_its_baseline_as_before(tmp_path):
    """C16, B18: without `accepted_fingerprints` a second fingerprint within a run is a change, as before the key
    existed; nothing of the set is kept, no attempt records a mix and the export has no deployment shares."""
    first, second, third = order()
    behaviours = {
        first: [served(KESTREL)],
        second: [served(KESTREL, PLOVER), served(KESTREL)],
        third: [served(KESTREL)],
    }
    with campaign(tmp_path, behaviours, accepted=()) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        assert attempts_of(store, second) == [("infra_fail", 1, 1), ("done", 0, 2)]
        assert store.get_meta(ACCEPTED_META) is None
        assert {row["deployments"] for row in store.q("SELECT deployments FROM attempts")} == {None}
        assert "deployment_shares" not in export_run_state(store)
        assert not [kind for kind in event_kinds(store) if kind.startswith("accepted_fingerprints")]


def test_b18_admission_refuses_a_probe_served_outside_the_set_or_by_another_model(tmp_path):
    """B18: the admission probe of a model that declares a set must be served by the model, under a fingerprint of the
    set; otherwise the declaration does not describe this endpoint and the model is not admitted."""
    with campaign(tmp_path / "a", {}, probe_served=(MODEL, OSPREY)) as scheduler:
        assert OSPREY in scheduler.not_admitted[MODEL]
        assert "model_not_admitted" in event_kinds(scheduler.store)
    with campaign(tmp_path / "b", {}, probe_served=("osprey-large", KESTREL)) as scheduler:
        assert "osprey-large" in scheduler.not_admitted[MODEL]


def test_b18_a_fingerprint_is_confirmed_by_the_probe_or_the_first_run_that_shows_it_and_else_stays_unconfirmed(
    tmp_path,
):
    """B18: a declared fingerprint counts as observed once the admission probe or a run's reply showed it under the
    model's own id; until then the export marks it unconfirmed, with no turn and no run."""
    first, _, _ = order()
    with campaign(tmp_path / "a", {}) as scheduler:
        (shares,) = export_run_state(scheduler.store)["deployment_shares"]
        confirmed = {f["fingerprint"]: (f["confirmed_by"], f["confirming_run"]) for f in shares["fingerprints"]}
        assert confirmed == {KESTREL: ("admission_probe", None), PLOVER: (None, None)}
        assert "accepted_fingerprints_unconfirmed" in event_kinds(scheduler.store)
    with campaign(tmp_path / "b", {first: [served(PLOVER)]}, probe_served=(MODEL, None)) as scheduler:
        scheduler.run()
        (shares,) = export_run_state(scheduler.store)["deployment_shares"]
        confirmed = {f["fingerprint"]: (f["confirmed_by"], f["confirming_run"]) for f in shares["fingerprints"]}
        assert confirmed[PLOVER] == ("run", first)


def test_b18_confirmations_survive_a_restart_and_a_record_that_cannot_be_read_starts_afresh_and_says_so(tmp_path):
    """B18: what confirmed a declared fingerprint is kept in the store, so a restart keeps it; a record of the sets that
    cannot be read is an event, and then no fingerprint counts as observed until a reply shows it again."""
    store, _, _ = planned_store(tmp_path)
    declared = models().models
    served = ServedIdentities(store, lambda: NOW, declared)
    served.check(MODEL, [{"served_model": MODEL, "system_fingerprint": PLOVER}], NOW, run_key="first-run")
    ServedIdentities(store, lambda: NOW, declared)
    confirmed = json.loads(store.get_meta(ACCEPTED_META))[MODEL]["confirmed"]
    assert confirmed == {PLOVER: {"by": "run", "run": "first-run", "at": NOW}}
    store.put_meta(ACCEPTED_META, "{broken", NOW)
    ServedIdentities(store, lambda: NOW, declared)
    assert json.loads(store.get_meta(ACCEPTED_META))[MODEL] == {"accepted": [KESTREL, PLOVER], "confirmed": {}}
    assert "accepted_fingerprints_unreadable" in event_kinds(store)


def test_b18_the_export_and_the_report_give_each_deployments_share_of_turns_and_of_runs(tmp_path):
    """B18: per model that declares a set, the export counts the analysed turns and runs each fingerprint served, with
    their fractions and the runs served by more than one; the report shows the same numbers."""
    first, second, third = order()
    behaviours = {first: [served(KESTREL, PLOVER, KESTREL)], second: [served(PLOVER, PLOVER)], third: [served(KESTREL)]}
    with campaign(tmp_path, behaviours) as scheduler:
        scheduler.run()
        export = export_run_state(scheduler.store)
    assert export["deployment_shares"] == [
        {
            "model": MODEL,
            "accepted_fingerprints": [KESTREL, PLOVER],
            "analysed_runs": 3,
            "runs_without_mix": 0,
            "mixed_runs": 1,
            "turns": 6,
            "fingerprints": [
                {
                    "fingerprint": KESTREL,
                    "accepted": True,
                    "confirmed_by": "admission_probe",
                    "confirming_run": None,
                    "turns": 3,
                    "turn_share": 0.5,
                    "runs": 2,
                    "run_share": 2 / 3,
                },
                {
                    "fingerprint": PLOVER,
                    "accepted": True,
                    "confirmed_by": "run",
                    "confirming_run": first,
                    "turns": 3,
                    "turn_share": 0.5,
                    "runs": 2,
                    "run_share": 2 / 3,
                },
            ],
        }
    ]
    result = AnalysisResult((), (), 0.05, {"preregistration_sha256": "e" * 64})
    text = render_report(result, export, study="a-study", code={"commit": "c" * 40}, core_version="1.2.3")
    assert "## Deployments" in text
    assert f"| {MODEL} | {KESTREL} | yes | admission probe | 3 | 50% | 2 | 66.67% |" in text
    cell = first.replace("|", "\\|")  # a run key's separators are escaped in a table cell
    assert f"| {MODEL} | {PLOVER} | yes | run {cell} | 3 | 50% | 2 | 66.67% |" in text
    assert f"{MODEL}: 3 analysed run(s), 1 served by more than one deployment, 0 without a recorded mix" in text
    del export["deployment_shares"]
    plain = render_report(result, export, study="a-study", code={"commit": "c" * 40}, core_version="1.2.3")
    assert "Deployments" not in plain
