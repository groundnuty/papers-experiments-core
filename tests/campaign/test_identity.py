"""An endpoint whose served identity changed makes the run infrastructure (C16): the baseline and its comparison."""

import json

from campaign_support import NOW, planned_store

from expcore.campaign.identity import META, ServedIdentities
from expcore.models.served import MIXED_FINGERPRINTS


def turn(model="served-a", fingerprint="fp1"):
    return {"served_model": model, "system_fingerprint": fingerprint}


def identities(tmp_path):
    store, _, _ = planned_store(tmp_path)
    return ServedIdentities(store, lambda: NOW), store


def test_c16_the_first_reply_sets_the_baseline_and_a_later_run_with_the_same_identity_changes_nothing(tmp_path):
    """C16: the first identity a model is served with is its baseline; the same again is no change."""
    served, store = identities(tmp_path)
    assert served.baseline("m1") is None
    assert served.check("m1", [turn()], NOW) is None
    assert served.baseline("m1") == {"served_model": "served-a", "system_fingerprint": "fp1"}
    assert served.check("m1", [turn(), turn()], NOW) is None
    assert json.loads(store.get_meta(META))["m1"]["system_fingerprint"] == "fp1"


def test_c16_a_fingerprint_that_changed_between_runs_is_a_change(tmp_path):
    """C16: another fingerprint than the baseline's makes the run infrastructure."""
    served, _ = identities(tmp_path)
    served.check("m1", [turn()], NOW)
    change = served.check("m1", [turn(fingerprint="fp2")], NOW)
    assert change == {
        "first_system_fingerprint": "fp1",
        "system_fingerprint": "fp2",
    }
    assert served.baseline("m1")["system_fingerprint"] == "fp1"  # the baseline stays pinned


def test_c16_an_identity_that_changed_between_the_start_and_the_end_of_one_run_is_caught(tmp_path):
    """C16: the replies of a run are compared in order, so a change between start and end of a run is caught."""
    served, _ = identities(tmp_path)
    change = served.check("m1", [turn(), turn(), turn(model="served-b")], NOW)
    assert change["served_model"] == "served-b"
    assert change["first_served_model"] == "served-a"


def test_c16_a_mixed_fingerprint_is_a_change_by_itself_and_never_a_baseline(tmp_path):
    """C16: a reply spliced from servers that differ is a change whatever the baseline."""
    served, _ = identities(tmp_path)
    mixed = f"fp1{MIXED_FINGERPRINTS}fp2"
    assert served.check("m1", [turn(fingerprint=mixed)], NOW) is not None
    assert served.baseline("m1")["system_fingerprint"] is None


def test_c16_replies_that_name_no_identity_neither_change_nor_set_anything(tmp_path):
    """C16: a reply without a served model or fingerprint is no change and sets nothing."""
    served, _ = identities(tmp_path)
    assert served.check("m1", [turn(None, None), {}], NOW) is None
    assert served.baseline("m1") is None


def test_c16_models_have_baselines_of_their_own_and_they_survive_a_restart(tmp_path):
    """C16: one baseline per model, kept in the store."""
    served, store = identities(tmp_path)
    served.check("m1", [turn()], NOW)
    served.check("m2", [turn(model="served-z", fingerprint="fpz")], NOW)
    again = ServedIdentities(store, lambda: NOW)
    assert again.check("m1", [turn(fingerprint="fp9")], NOW) is not None
    assert again.check("m2", [turn(model="served-z", fingerprint="fpz")], NOW) is None


def test_c16_an_endpoint_moved_on_purpose_is_accepted_by_a_reset_that_is_on_the_record(tmp_path):
    """C16: the baseline is pinned until an operator resets it; the reset is an event."""
    served, store = identities(tmp_path)
    served.check("m1", [turn()], NOW)
    served.reset("m1", NOW)
    assert served.baseline("m1") is None
    assert served.check("m1", [turn(fingerprint="fp2")], NOW) is None
    assert served.baseline("m1")["system_fingerprint"] == "fp2"
    assert "served_baseline_reset" in [r["kind"] for r in store.q("SELECT kind FROM events")]


def test_c16_a_record_of_baselines_that_cannot_be_read_starts_afresh_and_says_so(tmp_path):
    """C16: an unreadable record is an event, not a crash."""
    _served, store = identities(tmp_path)
    store.put_meta(META, "{broken", NOW)
    again = ServedIdentities(store, lambda: NOW)
    assert again.baseline("m1") is None
    assert "served_baselines_unreadable" in [r["kind"] for r in store.q("SELECT kind FROM events")]
