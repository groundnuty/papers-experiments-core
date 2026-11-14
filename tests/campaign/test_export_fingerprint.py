"""The export carries the campaign fingerprint the store records, and its hash (#243, C35)."""

import hashlib
import json

import pytest
from campaign_support import NOW, make_fingerprint, make_manifest, make_schema, planned_store

from expcore.campaign.export import (
    EXPORT_FINGERPRINT,
    NOT_RECORDED,
    REQUIRED_KEYS,
    ExportError,
    export_run_state,
    read_export,
    stored_fingerprint_sha256,
    verify_export_fingerprint,
    write_export,
)
from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.resume import FINGERPRINT_KEY, open_campaign
from expcore.campaign.snapshot import SNAPSHOT_FILE, StoreSnapshot, take_snapshot

OTHER = "f" * 64


def started(tmp_path, **changes):
    """A store started as a campaign starts, so that it records its fingerprint; return it and the fingerprint."""
    schema = make_schema()
    fingerprint = make_fingerprint(schema, **changes)
    store, _ = open_campaign(
        tmp_path / "campaign.db", schema, make_manifest(schema), fingerprint, now=NOW, max_attempts=3
    )
    return store, fingerprint


def rewrite_fingerprint(store, text: str) -> None:
    """Put another text in the store's fingerprint row, as only a person with a shell could."""
    store.transact(lambda: store.db.execute("UPDATE meta SET value=? WHERE key=?", (text, FINGERPRINT_KEY)))


def test_c35_the_export_carries_the_fingerprint_the_store_records_and_its_hash(tmp_path):
    """C35: `campaign_fingerprint` holds the fingerprint as the store keeps it and the SHA-256 of the text the store
    keeps, which is the hash merge.json names for a merged batch."""
    store, fingerprint = started(tmp_path)
    held = store.get_meta(FINGERPRINT_KEY)
    entry = export_run_state(store)[EXPORT_FINGERPRINT]
    assert entry == {"sha256": hashlib.sha256(held.encode("utf-8")).hexdigest(), "fingerprint": json.loads(held)}
    assert entry["fingerprint"] == fingerprint
    assert entry["sha256"] == fingerprint_sha256(fingerprint)
    assert stored_fingerprint_sha256(store) == entry["sha256"]


def test_c35_the_key_is_additive_and_comes_last_in_either_form_of_the_export(tmp_path):
    """C35: no reader's required key changes, so an export without it still reads; the key follows every other key."""
    store, _ = started(tmp_path)
    assert EXPORT_FINGERPRINT not in REQUIRED_KEYS
    assert list(export_run_state(store))[-1] == EXPORT_FINGERPRINT
    partial = export_run_state(store, excluded={})
    assert list(partial)[-3:-1] == ["running_attempts", "excluded_attempts"]
    assert list(partial)[-1] == EXPORT_FINGERPRINT


def test_c35_the_export_holds_what_the_store_holds_never_a_recomputed_fingerprint(tmp_path):
    """C35: the entry is read from the store's row; a row no start would have written is exported as it is."""
    store, _ = started(tmp_path)
    odd = {"written_by_another_core": [1, 2], "seeds": [9]}
    text = json.dumps(odd, sort_keys=True, separators=(",", ":"))
    rewrite_fingerprint(store, text)
    entry = export_run_state(store)[EXPORT_FINGERPRINT]
    assert entry == {"sha256": fingerprint_sha256(odd), "fingerprint": odd}


def test_c35_a_snapshot_exports_the_fingerprint_of_the_store_it_copies(tmp_path):
    """C35: the analysis reads the snapshot, so the snapshot's export carries what the store's does."""
    store, _ = started(tmp_path)
    take_snapshot(store.path, tmp_path / SNAPSHOT_FILE)
    snapshot = StoreSnapshot((tmp_path / SNAPSHOT_FILE).read_bytes(), make_schema())
    assert export_run_state(snapshot)[EXPORT_FINGERPRINT] == export_run_state(store)[EXPORT_FINGERPRINT]
    assert stored_fingerprint_sha256(snapshot) == stored_fingerprint_sha256(store)


def test_c35_a_store_that_records_no_fingerprint_exports_none_and_is_otherwise_unchanged(tmp_path):
    """C35: a store never started as a campaign has no fingerprint to carry: its export has no key, nothing else
    differs, and its hash is None."""
    store, _, _ = planned_store(tmp_path)
    assert EXPORT_FINGERPRINT not in export_run_state(store)
    assert stored_fingerprint_sha256(store) is None


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"a string"'])
def test_c35_a_fingerprint_row_that_is_not_a_json_object_is_refused_not_passed_over(tmp_path, text):
    """C35: a row that exists but cannot be read is a damaged store, never 'not recorded'."""
    store, _ = started(tmp_path)
    rewrite_fingerprint(store, text)
    with pytest.raises(ExportError, match="campaign fingerprint"):
        export_run_state(store)
    with pytest.raises(ExportError, match="campaign fingerprint"):
        stored_fingerprint_sha256(store)


def test_c35_the_written_file_has_it_and_names_no_path_of_the_machine(tmp_path):
    """C35: the entry adds the fingerprint, which names no path, secret or host (O12), and nothing of where the store
    lies."""
    store, fingerprint = started(tmp_path)
    path = tmp_path / "out" / "export.json"
    write_export(store, path)
    text = path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert read_export(path)[EXPORT_FINGERPRINT]["fingerprint"] == fingerprint


def test_c35_the_reader_refuses_an_entry_whose_hash_is_not_that_of_the_fingerprint_it_holds(tmp_path):
    """C35: an export edited after it was written (or a hash copied from another campaign) is refused on reading."""
    store, _ = started(tmp_path)
    path = tmp_path / "export.json"
    write_export(store, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data[EXPORT_FINGERPRINT]["fingerprint"]["seeds"] = [99]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ExportError, match="not the SHA-256 of the fingerprint it holds"):
        read_export(path)


@pytest.mark.parametrize(
    "entry",
    [
        "a string",
        {"sha256": OTHER},
        {"fingerprint": {}},
        {"sha256": "short", "fingerprint": {}},
        {"sha256": OTHER, "fingerprint": []},
        {"sha256": OTHER, "fingerprint": {}, "extra": 1},
    ],
)
def test_c35_the_reader_refuses_an_entry_of_another_form(tmp_path, entry):
    """C35: the entry is `{sha256, fingerprint}` and nothing else."""
    store, _ = started(tmp_path)
    path = tmp_path / "export.json"
    write_export(store, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data[EXPORT_FINGERPRINT] = entry
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ExportError, match=EXPORT_FINGERPRINT):
        read_export(path)


def test_c35_an_export_from_before_the_key_still_reads_and_says_its_fingerprint_is_not_recorded(tmp_path):
    """C35: an old export reads as it did; checking it against a campaign says 'fingerprint not recorded' and does
    not fail."""
    store, _ = started(tmp_path)
    path = tmp_path / "export.json"
    write_export(store, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data[EXPORT_FINGERPRINT]
    path.write_text(json.dumps(data), encoding="utf-8")
    old = read_export(path)
    assert EXPORT_FINGERPRINT not in old
    assert NOT_RECORDED == "fingerprint not recorded"
    assert verify_export_fingerprint(old, stored_fingerprint_sha256(store)) == NOT_RECORDED
    assert verify_export_fingerprint(old) == NOT_RECORDED


def test_c35_an_export_is_verified_against_the_hash_of_the_store_or_snapshot_it_is_read_with(tmp_path):
    """C35: the verifier says what it found for an export of this campaign, and refuses one of another, naming both
    hashes."""
    store, fingerprint = started(tmp_path)
    take_snapshot(store.path, tmp_path / SNAPSHOT_FILE)
    snapshot = StoreSnapshot((tmp_path / SNAPSHOT_FILE).read_bytes(), make_schema())
    data = export_run_state(store)
    expected = fingerprint_sha256(fingerprint)
    for held in (store, snapshot):
        assert expected in verify_export_fingerprint(data, stored_fingerprint_sha256(held))
    assert expected in verify_export_fingerprint(data)  # without one to compare with, it says what the export holds
    with pytest.raises(ExportError, match=rf"{expected}.*{OTHER}|{OTHER}.*{expected}"):
        verify_export_fingerprint(data, OTHER)


def test_c35_an_export_of_another_campaign_is_refused_whatever_else_agrees(tmp_path):
    """C35: two campaigns over the same plan whose settings differ in one value have different hashes, so the export
    of one is not accepted for the other."""
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    one, _ = started(tmp_path / "one")
    two, _ = started(tmp_path / "two", caps={"attempt_limit": 3, "time_cap_s": 601})
    assert stored_fingerprint_sha256(one) != stored_fingerprint_sha256(two)
    with pytest.raises(ExportError, match="another campaign"):
        verify_export_fingerprint(export_run_state(one), stored_fingerprint_sha256(two))


def test_c35_an_entry_that_is_not_the_hash_of_its_fingerprint_is_refused_by_the_verifier_too(tmp_path):
    """C35: an export built in memory is held to the reader's rule: the verifier checks the entry's own consistency
    before it compares anything."""
    store, _ = started(tmp_path)
    data = export_run_state(store)
    data[EXPORT_FINGERPRINT] = {**data[EXPORT_FINGERPRINT], "sha256": OTHER}
    with pytest.raises(ExportError, match="not the SHA-256 of the fingerprint it holds"):
        verify_export_fingerprint(data, OTHER)
