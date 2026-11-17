"""The served-model check judges a model's fingerprints against the set it declares, as the campaign does (B18).

A preflight asks `judge` whether the replies it saw came from the model it expects. A model that declares
`accepted_fingerprints` is served by several deployments the study decided are the same model, so a reply from any of
them is the model, and only a fingerprint outside the set is a change. The campaign's check (`ServedIdentities`) holds
the replies of a run to that set, and `judge` holds them to the same one, so a preflight and a campaign agree.
"""

from types import SimpleNamespace

import pytest
from campaign_support import NOW, planned_store

from expcore.campaign.identity import ServedIdentities, parts
from expcore.config.models import ModelSpec
from expcore.config.prices import Price
from expcore.models import served as served_module
from expcore.models.served import MIXED_FINGERPRINTS, Served, judge

MODEL = "kestrel"
KESTREL, PLOVER, OSPREY = "deploy-kestrel", "deploy-plover", "deploy-osprey"
DECLARED = (KESTREL, PLOVER)
BOTH = f"{KESTREL}{MIXED_FINGERPRINTS}{PLOVER}"
HALF_DECLARED = f"{KESTREL}{MIXED_FINGERPRINTS}{OSPREY}"


def reply(fingerprint, model=MODEL):
    return SimpleNamespace(model=model, system_fingerprint=fingerprint, model_extra={})


def judged(fingerprints, expected=MODEL, **kwargs):
    """What `judge` says of one run whose replies carried `fingerprints`, as a preflight calls it."""
    served = Served()
    for fingerprint in fingerprints:
        served.note(reply(fingerprint))
    info = served.info()
    return judge(
        info["served_model_ids"],
        info["served_settings"],
        expected,
        {},
        served_fingerprints=info["system_fingerprints"],
        **kwargs,
    )


def test_b18_replies_from_two_declared_deployments_are_the_model():
    """B18: the replies of one run from two of the deployments the model declares are a match."""
    result = judged([KESTREL, PLOVER, KESTREL], accepted_fingerprints=DECLARED)
    assert result["state"] == "match"
    assert result["reasons"] == []


def test_b18_the_same_replies_are_a_change_for_a_model_that_declares_nothing():
    """B18: without the set the two fingerprints are still different fingerprints in different replies."""
    result = judged([KESTREL, PLOVER])
    assert result["state"] == "mismatch"
    assert "different system fingerprints" in result["reasons"][0]


def test_b18_a_fingerprint_the_model_does_not_declare_is_a_mismatch_that_names_it_and_the_set():
    """B18: one reply from an undeclared deployment is a mismatch, whichever declared ones came with it."""
    result = judged([KESTREL, OSPREY, PLOVER], accepted_fingerprints=DECLARED)
    assert result["state"] == "mismatch"
    assert len(result["reasons"]) == 1
    assert OSPREY in result["reasons"][0]
    assert all(name in result["reasons"][0] for name in DECLARED)


def test_b18_a_single_undeclared_fingerprint_is_a_mismatch():
    """B18: a run served only by an undeclared deployment is no more the model than one that switched to it."""
    assert judged([OSPREY], accepted_fingerprints=DECLARED)["state"] == "mismatch"


def test_b18_a_model_without_a_declared_set_is_judged_as_before():
    """B18: an empty set, or none given, leaves every result of `judge` as it was, baseline included."""
    for fingerprints in ([KESTREL], [KESTREL, KESTREL], [KESTREL, PLOVER], [BOTH], [None]):
        assert judged(fingerprints, accepted_fingerprints=()) == judged(fingerprints)
    assert judged([KESTREL], expected_fingerprint=KESTREL, accepted_fingerprints=())["state"] == "match"
    assert judged([PLOVER], expected_fingerprint=KESTREL, accepted_fingerprints=())["state"] == "mismatch"


def test_b18_a_reply_that_carried_several_fingerprints_is_the_model_only_when_the_set_lists_each():
    """B18: as in the campaign, a mixed reply is accepted when every fingerprint in it is declared."""
    assert judged([BOTH], accepted_fingerprints=DECLARED)["state"] == "match"
    result = judged([HALF_DECLARED], accepted_fingerprints=DECLARED)
    assert result["state"] == "mismatch"
    assert HALF_DECLARED in result["reasons"][0]
    assert judged([BOTH])["state"] == "mismatch"  # a model with no set: a mixed reply is a change by itself


def test_b18_a_reply_without_a_fingerprint_is_no_change():
    """B18: the campaign sets nothing and flags nothing for a reply that carried none; so does `judge`."""
    assert judged([None, KESTREL, None], accepted_fingerprints=DECLARED)["state"] == "match"
    assert judged([None], accepted_fingerprints=DECLARED)["state"] == "match"


def test_b18_the_served_model_id_is_judged_whatever_the_set_says():
    """B18: a declared fingerprint served by another model id is still another model."""
    served = Served()
    served.note(reply(KESTREL, model="plover-large"))
    info = served.info()
    result = judge(
        info["served_model_ids"],
        info["served_settings"],
        MODEL,
        {},
        served_fingerprints=info["system_fingerprints"],
        accepted_fingerprints=DECLARED,
    )
    assert result["state"] == "mismatch"
    assert "plover-large" in result["reasons"][0]


def test_b18_a_declared_set_replaces_a_baseline_fingerprint():
    """B18: the campaign never consults the baseline fingerprint of a model that declares a set, so `judge` does not."""
    result = judged([PLOVER], expected_fingerprint=KESTREL, accepted_fingerprints=DECLARED)
    assert result["state"] == "match"
    assert judged([OSPREY], expected_fingerprint=KESTREL, accepted_fingerprints=DECLARED)["state"] == "mismatch"


def test_b18_a_baseline_taken_before_the_set_was_declared_changes_neither_check(tmp_path):
    """B18: a model first served with no set keeps its baseline once the set is declared, and neither the campaign's
    check nor `judge` holds a reply to it: the second deployment is the model to both."""
    store, _, _ = planned_store(tmp_path)
    before = [{"served_model": MODEL, "system_fingerprint": KESTREL}]
    assert ServedIdentities(store, lambda: NOW).check(MODEL, before, NOW) is None
    declared = ServedIdentities(store, lambda: NOW, {MODEL: model_spec(DECLARED)})
    baseline = declared.baseline(MODEL)["system_fingerprint"]
    assert baseline == KESTREL
    later = [{"served_model": MODEL, "system_fingerprint": PLOVER}]
    assert declared.check(MODEL, later, NOW) is None
    assert judged([PLOVER], expected_fingerprint=baseline, accepted_fingerprints=DECLARED)["state"] == "match"


def test_b18_the_set_is_a_collection_of_fingerprints_not_one_string():
    """B18: a bare string would be read as its characters, so it is refused."""
    with pytest.raises(TypeError, match="not one string"):
        judged([KESTREL], accepted_fingerprints=KESTREL)


def test_b18_the_set_may_be_any_iterable_and_is_read_once():
    """B18: a generator of the declared fingerprints judges every reply, not only the first."""
    result = judged([KESTREL, PLOVER], accepted_fingerprints=(name for name in DECLARED))
    assert result["state"] == "match"


def test_b18_the_split_of_a_mixed_reply_is_the_one_the_campaign_uses():
    """B18: `parts` of the campaign's check is the served module's own split, joined with `MIXED_FINGERPRINTS`."""
    assert parts is served_module.fingerprint_parts
    assert served_module.fingerprint_parts(BOTH) == [KESTREL, PLOVER]
    assert served_module.fingerprint_parts(KESTREL) == [KESTREL]


def model_spec(declared):
    """The model's entry, declaring `declared` (none when empty)."""
    return ModelSpec(
        key=MODEL, backend="stub", api_id=MODEL, status="frozen", price=Price(3.0, 15.0), accepted_fingerprints=declared
    )


def campaign_change(tmp_path, fingerprints, declared):
    """What the campaign's check says of the same run: the change it finds, or None."""
    store, _, _ = planned_store(tmp_path)
    identities = ServedIdentities(store, lambda: NOW, {MODEL: model_spec(declared)})
    transcript = [{"served_model": MODEL, "system_fingerprint": fingerprint} for fingerprint in fingerprints]
    return identities.check(MODEL, transcript, NOW)


AGREEMENT = [
    pytest.param(DECLARED, [KESTREL], id="one-declared"),
    pytest.param(DECLARED, [KESTREL, PLOVER], id="two-declared"),
    pytest.param(DECLARED, [PLOVER, KESTREL, PLOVER], id="declared-switching"),
    pytest.param(DECLARED, [KESTREL, PLOVER, OSPREY], id="two-declared-and-one-not"),
    pytest.param(DECLARED, [OSPREY], id="only-undeclared"),
    pytest.param(DECLARED, [BOTH], id="mixed-all-declared"),
    pytest.param(DECLARED, [HALF_DECLARED], id="mixed-one-undeclared"),
    pytest.param(DECLARED, [None, KESTREL, None], id="some-without-a-fingerprint"),
    pytest.param(DECLARED, [None], id="no-fingerprint-at-all"),
    pytest.param((), [KESTREL], id="no-set-one"),
    pytest.param((), [KESTREL, KESTREL], id="no-set-the-same-twice"),
    pytest.param((), [KESTREL, PLOVER], id="no-set-two"),
    pytest.param((), [BOTH], id="no-set-mixed"),
]


@pytest.mark.parametrize(("declared", "fingerprints"), AGREEMENT)
def test_b18_a_preflight_and_the_campaign_agree_on_the_same_replies(tmp_path, declared, fingerprints):
    """B18: the replies the campaign sets aside as a change are the ones `judge` calls a mismatch, and no others."""
    change = campaign_change(tmp_path, fingerprints, declared)
    state = judged(fingerprints, accepted_fingerprints=declared)["state"]
    assert (change is None) == (state == "match"), (change, state)
