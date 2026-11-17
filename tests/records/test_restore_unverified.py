"""O11: a record whose fault injector's reading says the restore was not verified is never scored.

The platform's injector writes `restore_verified` (with how the restore ended and what was checked) into its reading;
the core reads it as `restore_refusal`, which the record's `set_aside` and the campaign's post-run judgement both
use. The core imports no platform, so these tests hand the reading over as the platform writes it.
"""

import pytest
from record_support import hand_made

from expcore.errors import CORE_KINDS, POST_CHECK_UNVERIFIED, register_infra_kind
from expcore.records import injections
from expcore.records.injections import FAULT_NAMESPACE, INJECTOR_FAILURE, restore_refusal
from expcore.records.rescore import rescore

register_infra_kind(INJECTOR_FAILURE, "the fault injector could not inject, clear or verify its fault", treatable=False)

NOT_VERIFIED = {
    "ok": False,
    "target": "providers[1]",
    "restore_verified": False,
    "how": "not_serving",
    "identity_changed": True,
    "serving": False,
}


def test_o11_a_reading_that_records_the_restore_not_verified_is_refused_as_the_injectors_failure():
    """O11: the refusal has the shape of an instrument's refusal, the injector's own untreatable kind, and says which
    provider, how the restore ended and what was checked."""
    refusal = restore_refusal({FAULT_NAMESPACE: NOT_VERIFIED})
    assert refusal is not None
    assert (refusal["namespace"], refusal["infra_kind"]) == (FAULT_NAMESPACE, INJECTOR_FAILURE)
    assert "providers[1]" in refusal["detail"]
    assert "not_serving" in refusal["detail"]
    assert "changed" in refusal["detail"]
    assert "did not serve" in refusal["detail"]


def test_o11_a_process_that_does_not_know_the_injectors_kind_refuses_as_the_core_kind_for_an_unverified_result(
    monkeypatch,
):
    """O11: the platform that writes the reading registers `injector_failure`; a core-only process that has not
    registered it is refused as `post_check_unverified` (untreatable, a core kind), never as a kind its vocabulary
    does not hold, which would raise where the refusal is applied."""
    monkeypatch.setattr(injections, "infra_kinds", lambda: CORE_KINDS)
    refusal = restore_refusal({FAULT_NAMESPACE: NOT_VERIFIED})
    assert refusal is not None
    assert refusal["infra_kind"] == POST_CHECK_UNVERIFIED


@pytest.mark.parametrize(
    "instruments",
    [
        {},
        {FAULT_NAMESPACE: {"ok": True, "restore_verified": True}},
        {FAULT_NAMESPACE: {"ok": True}},  # a reading before the field: nothing to read, nothing refused
        {"other": "not an object", FAULT_NAMESPACE: {"restore_verified": True}},
    ],
    ids=["none", "verified", "absent", "not-an-object"],
)
def test_o11_a_verified_or_unrecorded_restore_is_not_refused(instruments):
    """O11: only a reading that says the restore was not verified is refused: a verified one, one that holds no such
    field and an instrument entry that is no object are not."""
    assert restore_refusal(instruments) is None


@pytest.mark.parametrize("value", [None, 0, "false", "no"], ids=["null", "zero", "text-false", "text-no"])
def test_o11_a_restore_verified_that_is_not_true_is_not_a_verified_restore(value):
    """O11: only `true` is a verified restore: a reading that carries the field as anything else is a malformed
    reading, and an unproven restore is refused rather than read as one."""
    assert restore_refusal({FAULT_NAMESPACE: {"restore_verified": value}}) is not None


def test_o11_the_field_is_read_whichever_namespace_the_reading_is_recorded_under():
    """O11: an executor that records the injector's snapshot under another namespace by hand does not hide the
    failure: any instrument's entry that carries the field is read, and the refusal names the namespace."""
    refusal = restore_refusal({"hand_filed": NOT_VERIFIED})
    assert refusal is not None
    assert refusal["namespace"] == "hand_filed"


def test_o11_the_detail_holds_only_a_label_a_word_and_two_answers():
    """O11: a reading is data from a file the record carries, so the detail is built from a position label, a plain
    word and two booleans, never from the reading's own text (O12): anything else in it is left out."""
    hostile = {
        **NOT_VERIFIED,
        "target": "provider-host.example.org",
        "how": "restored; SECRET=abc",
        "identity_changed": "yes",
        "serving": "sure",
    }
    refusal = restore_refusal({FAULT_NAMESPACE: hostile})
    assert refusal is not None
    assert "provider-host" not in refusal["detail"]
    assert "SECRET" not in refusal["detail"]
    assert "abc" not in refusal["detail"]
    assert "(failed)" in refusal["detail"]  # the word that stands for one that is not a plain word
    assert "did not change" in refusal["detail"]  # an answer that is not `true` is not a yes
    assert "did not serve" in refusal["detail"]


def test_o11_a_record_whose_reading_says_the_restore_was_not_verified_is_set_aside_and_never_rescored():
    """O11: the record is set aside whatever its outcome and holds no instrument refusal of its own, so a scorer is
    never run on it; a record whose restore was verified is not set aside."""

    class NeverRun:
        """A scorer that fails the test if it is called."""

        name = "never"
        version = "1"

        def score(self, record):
            raise AssertionError("a record whose restore was not verified was scored")

    refused = hand_made(instruments={FAULT_NAMESPACE: NOT_VERIFIED})
    assert refused.instrument_refusal is None
    assert refused.outcome["name"] == "finished"
    assert refused.set_aside is True
    assert rescore(refused, [NeverRun()]) is refused
    verified = hand_made(instruments={FAULT_NAMESPACE: {"ok": True, "restore_verified": True}})
    assert verified.set_aside is False
