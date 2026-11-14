"""The platform's settings that are in no fingerprint are recorded at a start and compared at a resume (C35).

A study that reads the platform's reset and placement bounds from its own file does not put them in the fingerprint,
since they change how the reset works and not what an agent sees. A resume after one changed still says so: the log,
an event of the store, and the export that the report reads.
"""

import json
import logging

import pytest
from campaign_support import LATER, NOW, make_fingerprint, make_manifest, make_schema, sha

from expcore.campaign.export import export_run_state
from expcore.campaign.reset_settings import CHANGED, EXPORT_KEY, META_KEY, UNKNOWN_BEFORE
from expcore.campaign.resume import FingerprintMismatch, open_campaign

LOGGER = "expcore.campaign.reset_settings"
SETTINGS = {"placement.qos_scan": "listing", "placement.poll_s": 2.0, "placement.concurrency": 16}
LATEST = "2026-01-01T00:20:00Z"


def start(tmp_path, reset_settings=None, *, now=NOW, **inputs):
    """Open the campaign store of a small plan; with `reset_settings` the platform supplies its settings."""
    schema = make_schema()
    manifest = make_manifest(schema, 7)
    options = {} if reset_settings is None else {"reset_settings": reset_settings}
    return open_campaign(
        tmp_path / "campaign.db",
        schema,
        manifest,
        make_fingerprint(schema, **inputs),
        now=now,
        max_attempts=3,
        **options,
    )


def events(store, *kinds):
    """The (kind, detail) of the store's events of the given kinds, in order."""
    marks = ",".join("?" * len(kinds))
    return [tuple(r) for r in store.q(f"SELECT kind, detail FROM events WHERE kind IN ({marks}) ORDER BY id", kinds)]


def stamped(store):
    """When the store's recorded settings were written."""
    return store.q("SELECT set_utc FROM meta WHERE key=?", (META_KEY,))[0]["set_utc"]


def warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING]


def test_c35_a_first_start_records_the_effective_settings_and_says_nothing(tmp_path, caplog):
    """C35: the first start records the settings it was given as canonical JSON, with no event and no log line."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        store, _ = start(tmp_path, SETTINGS)
    assert json.loads(store.get_meta(META_KEY)) == SETTINGS
    assert store.get_meta(META_KEY) == json.dumps(SETTINGS, sort_keys=True, separators=(",", ":"))
    assert events(store, CHANGED, UNKNOWN_BEFORE) == []
    assert [r for r in caplog.records if r.name == LOGGER] == []


def test_c35_an_unchanged_resume_is_quiet_and_writes_nothing(tmp_path, caplog):
    """C35: the same settings, in another order, change nothing: no event, no log line, the record is not rewritten."""
    first, _ = start(tmp_path, SETTINGS)
    first.close()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        store, _ = start(tmp_path, dict(reversed(list(SETTINGS.items()))), now=LATER)
    assert events(store, CHANGED, UNKNOWN_BEFORE) == []
    assert stamped(store) == NOW
    assert [r for r in caplog.records if r.name == LOGGER] == []


def test_c35_a_resume_after_qos_scan_changed_warns_names_both_values_records_the_change_and_the_new_value(
    tmp_path, caplog
):
    """C35: a changed key is named in one warning with its old and new value, an event holds both, and the record
    now holds the new settings, so the next resume under them is quiet."""
    first, _ = start(tmp_path, SETTINGS)
    first.close()
    changed = {**SETTINGS, "placement.qos_scan": "summaries"}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store, _ = start(tmp_path, changed, now=LATER)
    [warning] = warnings(caplog)
    text = warning.getMessage()
    assert 'placement.qos_scan: "listing" -> "summaries"' in text
    assert "placement.poll_s" not in text
    assert "placement.concurrency" not in text
    [(_kind, detail)] = events(store, CHANGED)
    assert json.loads(detail) == {"changes": [{"key": "placement.qos_scan", "old": "listing", "new": "summaries"}]}
    assert json.loads(store.get_meta(META_KEY)) == changed
    assert stamped(store) == LATER
    store.close()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        again, _ = start(tmp_path, changed, now=LATEST)
    assert warnings(caplog) == []
    assert len(events(again, CHANGED)) == 1


def test_c35_every_changed_key_is_named_in_key_order_and_a_key_that_appeared_or_went_is_absent_on_its_side(
    tmp_path, caplog
):
    """C35: two changed keys, one key a newer platform added and one it dropped, in one warning and one event."""
    first, _ = start(tmp_path, SETTINGS)
    first.close()
    changed = {
        "placement.qos_scan": "summaries",
        "placement.poll_s": 2.0,
        "placement.qos_fulfil_timeout_s": 120.0,  # new
        # placement.concurrency: dropped
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store, _ = start(tmp_path, changed, now=LATER)
    [warning] = warnings(caplog)
    lines = [line.strip() for line in warning.getMessage().splitlines() if "placement." in line]
    assert lines == [
        "placement.concurrency: 16 -> absent",
        "placement.qos_fulfil_timeout_s: absent -> 120.0",
        'placement.qos_scan: "listing" -> "summaries"',
    ]
    [(_, detail)] = events(store, CHANGED)
    assert json.loads(detail)["changes"] == [
        {"key": "placement.concurrency", "old": 16},
        {"key": "placement.qos_fulfil_timeout_s", "new": 120.0},
        {"key": "placement.qos_scan", "old": "listing", "new": "summaries"},
    ]


def test_c35_a_value_that_is_the_same_number_written_another_way_is_not_a_change(tmp_path, caplog):
    """C35: `poll_s = 2` and `poll_s = 2.0` are one setting, so a resume that writes it the other way is quiet."""
    first, _ = start(tmp_path, {"placement.poll_s": 2})
    first.close()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store, _ = start(tmp_path, {"placement.poll_s": 2.0}, now=LATER)
    assert warnings(caplog) == []
    assert events(store, CHANGED) == []


def test_c35_a_store_made_before_the_settings_were_recorded_records_them_at_the_resume_and_says_so_once(
    tmp_path, caplog
):
    """C35: a store with no record has earlier settings that are unknown: they are recorded now, with one event and one
    warning that says the earlier ones are unknown; the next resume under the same settings is quiet, and a later
    change is compared with what was recorded at this resume."""
    old, _ = start(tmp_path)  # a start of the earlier core, or of a platform that supplied none
    assert old.get_meta(META_KEY) is None
    old.close()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store, _ = start(tmp_path, SETTINGS, now=LATER)
    [warning] = warnings(caplog)
    assert "unknown" in warning.getMessage()
    assert "earlier" in warning.getMessage()
    [(_kind, detail)] = events(store, UNKNOWN_BEFORE)
    assert "unknown" in detail
    assert json.loads(store.get_meta(META_KEY)) == SETTINGS
    assert events(store, CHANGED) == []
    store.close()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        start(tmp_path, SETTINGS, now=LATEST)[0].close()
        assert warnings(caplog) == []
        final, _ = start(tmp_path, {**SETTINGS, "placement.qos_scan": "summaries"}, now=LATEST)
    [only] = warnings(caplog)
    assert 'placement.qos_scan: "listing" -> "summaries"' in only.getMessage()
    assert len(events(final, UNKNOWN_BEFORE)) == 1
    assert len(events(final, CHANGED)) == 1


def test_c35_a_platform_that_supplies_no_settings_leaves_the_store_as_it_was(tmp_path, caplog):
    """C35: a study without a platform, or with one that watches nothing, records nothing and compares nothing, at a
    first start and at a resume, so its store and its export are byte for byte what they were."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        first, _ = start(tmp_path)
        first.close()
        store, _ = start(tmp_path, {}, now=LATER)
    assert store.get_meta(META_KEY) is None
    assert events(store, CHANGED, UNKNOWN_BEFORE) == []
    assert [r for r in caplog.records if r.name == LOGGER] == []
    assert EXPORT_KEY not in export_run_state(store)


def test_c35_a_resume_that_no_longer_supplies_settings_the_store_recorded_says_each_is_gone(tmp_path, caplog):
    """C35: dropping the settings is a change like another: each recorded key is named, new side absent."""
    first, _ = start(tmp_path, {"placement.qos_scan": "listing"})
    first.close()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store, _ = start(tmp_path, {}, now=LATER)
    [warning] = warnings(caplog)
    assert 'placement.qos_scan: "listing" -> absent' in warning.getMessage()
    assert json.loads(store.get_meta(META_KEY)) == {}


def test_c35_a_refused_start_records_nothing_and_says_nothing(tmp_path, caplog):
    """C35: the comparison is in the start's transaction after the fingerprint check, so a start that is refused for
    another experiment leaves the record, the events and the log as they were."""
    first, _ = start(tmp_path, SETTINGS)
    first.close()
    changed = {**SETTINGS, "placement.qos_scan": "summaries"}
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(FingerprintMismatch):
        start(tmp_path, changed, now=LATER, scorer_digest=sha("another scorer"))
    store, _ = start(tmp_path, SETTINGS, now=LATEST)
    assert json.loads(store.get_meta(META_KEY)) == SETTINGS
    assert stamped(store) == NOW
    assert events(store, CHANGED, UNKNOWN_BEFORE) == []
    assert warnings(caplog) == []


@pytest.mark.parametrize(
    ("given", "fragment"),
    [
        ({"placement.x": {"nested": 1}}, "placement.x"),
        ({"placement.x": [1, 2]}, "placement.x"),
        ({"placement.x": float("nan")}, "placement.x"),
        ({"placement.x": float("inf")}, "placement.x"),
        ({"": 1}, "empty"),
        ({1: "a"}, "text"),
    ],
)
def test_c35_settings_that_are_not_plain_flat_data_are_refused_before_the_store_is_opened(tmp_path, given, fragment):
    """C35: names mapped to text, numbers, booleans or None, and no more, so a record can always be compared and shown;
    the refusal names the key and comes before any file is made."""
    with pytest.raises(ValueError, match=fragment):
        start(tmp_path, given)
    assert not (tmp_path / "campaign.db").exists()


def test_c35_the_export_lists_each_change_and_each_unknown_earlier_start_for_the_report(tmp_path):
    """C35: `reset_settings` of the export, last before the partial-analysis keys: each change with when it was seen
    and both values (a value that was absent has no entry), and each start whose earlier settings were unknown."""
    old, _ = start(tmp_path)
    old.close()
    start(tmp_path, SETTINGS, now=LATER)[0].close()
    store, _ = start(tmp_path, {**SETTINGS, "placement.qos_scan": "summaries", "placement.new": 1}, now=LATEST)
    exported = export_run_state(store)
    assert exported[EXPORT_KEY] == {
        "changes": [
            {"at": LATEST, "key": "placement.new", "new": 1},
            {"at": LATEST, "key": "placement.qos_scan", "old": "listing", "new": "summaries"},
        ],
        "unknown_before": [{"at": LATER}],
    }
    assert list(exported)[-1] == EXPORT_KEY
    assert list(exported).index(EXPORT_KEY) == list(exported).index("kinds") + 1


def test_c35_an_export_of_settings_that_never_changed_says_so_with_empty_lists(tmp_path):
    """C35: a store that recorded settings has the key, with nothing in it, so a report can say that no change was
    recorded; a store that recorded none has no key."""
    store, _ = start(tmp_path, SETTINGS)
    assert export_run_state(store)[EXPORT_KEY] == {"changes": [], "unknown_before": []}
