"""C48: a run on a lease the elastic ledger issued is judged by the lease's declared set, strictly: a reply with no
fingerprint is undeclared unless "none" is declared, "none" admits no fingerprint, and each undeclared value is kept in
the store for the worker. A set from models.toml keeps its lenient rule (B18)."""

import dataclasses
import json

from campaign_support import NOW, planned_store
from elastic_support import SITE, completion, join, make, plan
from elastic_support import leased as leased_to

from expcore.campaign.elastic import SiteReading, Wait
from expcore.campaign.identity import UNDECLARED_META, ServedIdentities
from expcore.config.models import ModelSpec
from expcore.models.served import DeclaredSet

MODEL = "kestrel"
SPEC = {MODEL: ModelSpec(key=MODEL, backend="stub", api_id=MODEL, status="frozen", accepted_fingerprints=("fp-a",))}


def judged(tmp_path, name, declared, *fingerprints):
    """The change a run with one reply per fingerprint is, and what the store kept undeclared."""
    store, _, _ = planned_store(tmp_path / name)
    leased = None if declared is None else {MODEL: declared}
    identities = ServedIdentities(store, lambda: NOW, SPEC, leased)
    turns = [{"served_model": MODEL, "system_fingerprint": fingerprint} for fingerprint in fingerprints]
    change = identities.check(MODEL, turns, NOW, run_key="run-1")
    kept = store.get_meta(UNDECLARED_META)
    return change, identities.undeclared(MODEL), None if kept is None else json.loads(kept)


def test_c48_a_reply_with_no_fingerprint_is_undeclared_unless_none_is_declared_and_none_admits_no_fingerprint(tmp_path):
    """C48: under the lease's set {fp-a} a reply with no fingerprint is a change, kept as null; once "none" is
    declared it is the model; "none" alone does not admit fp-a; and models.toml's own set still lets it through."""
    change, seen, kept = judged(tmp_path, "strict", DeclaredSet(("fp-a",), none=False), "fp-a", None)
    assert change == {"system_fingerprint": None, "declared_fingerprints": ["fp-a"], "none_declared": False}
    assert (seen, kept) == ([None], {MODEL: [None]})
    assert judged(tmp_path, "none", DeclaredSet(("fp-a",), none=True), "fp-a", None)[:2] == (None, [])
    change, seen, _ = judged(tmp_path, "only-none", DeclaredSet((), none=True), None, "fp-a | fp-b")
    assert change is not None
    assert seen == ["fp-a", "fp-b"]  # a mixed reply: each part, never the joined value
    assert judged(tmp_path, "models", None, "fp-a", None) == (None, [], None)


def test_c48_a_run_mixing_declared_values_is_the_model_and_an_undeclared_none_in_it_pauses_the_model(tmp_path):
    """C48: the fleet's front sends overflow elsewhere with no stickiness, so one run's replies come under the fleet's
    fingerprint, then none, then the fleet's again. With both declared the run is the model, judged reply by reply,
    and its mix is recorded; with "none" undeclared the same run is a change (infrastructure), the store keeps the
    reply with none, and a completion naming it pauses the model in the ledger."""
    mixed = ("fp-fleet", None, "fp-fleet")
    store, _, _ = planned_store(tmp_path / "both")
    leased = {MODEL: DeclaredSet(("fp-fleet",), none=True)}
    identities = ServedIdentities(store, lambda: NOW, SPEC, leased)
    turns = [{"served_model": MODEL, "system_fingerprint": fingerprint} for fingerprint in mixed]
    assert identities.check(MODEL, turns, NOW, run_key="run-1") is None
    assert identities.undeclared(MODEL) == []
    assert json.loads(identities.deployment_mix(MODEL, turns)) == [["fp-fleet", 2], [None, 1]]
    change, seen, _ = judged(tmp_path, "fleet-only", DeclaredSet(("fp-fleet",), none=False), *mixed)
    assert change == {"system_fingerprint": None, "declared_fingerprints": ["fp-fleet"], "none_declared": False}
    assert seen == [None]
    ledger, clock = make(
        tmp_path / "ledger", runs=plan(tasks=1, conditions=("api-none",)), declared={"m1": ["fp-fleet"]}
    )
    join(ledger, clock, "fed-1")
    lease = leased_to(ledger, "fed-1")
    found = dataclasses.replace(completion(lease, state="infra_fail", charged=1), undeclared=tuple(seen))
    ledger.observe_site(SITE, SiteReading(completions=(found,)))
    assert ledger.verify_archive(lease.lease_id, found.archive_sha256) == "counted"
    assert ledger.paused() == {"m1": (None,)}
    assert isinstance(ledger.issue("fed-1"), Wait)
