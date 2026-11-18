"""The lease a run is taken on (C41, `expcore-shard/5`, step 2 of #462): its file, its refusals and its expiry."""

import datetime as dt
import json
from pathlib import Path

import pytest

from expcore.campaign.run_lease import LEASE_SCHEMA, RULE_LEASE, LeaseError, RunLease, read_lease
from expcore.campaign.shard import ShardError

KEY = "task=lake|access=direct|tools=basic|model=alpha-stub|seed=1"
SHA = "ab" * 32
FILE = {
    "schema": LEASE_SCHEMA,
    "run_key": KEY,
    "lease_id": 7,
    "attempt_base": 2,
    "grant_usd": 1.25,
    "campaign_sha256": SHA,
    "expires_utc": "2026-11-18T12:30:00Z",
}


def written(tmp_path: Path, document: object, *, text: str | None = None) -> Path:
    path = tmp_path / "lease.json"
    path.write_text(text if text is not None else json.dumps(document), encoding="utf-8")
    return path


def test_c41_a_lease_file_reads_into_the_lease_and_the_pinned_json_reads_back_to_it(tmp_path):
    """C41: the shape the ledger produces (run key, lease id, attempt base, grant, campaign sha256, expiry), and the
    spec a store pins, which names the rule and drops the file's schema tag."""
    lease = read_lease(written(tmp_path, FILE))
    assert (lease.run_key, lease.lease_id, lease.attempt_base, lease.grant_usd) == (KEY, 7, 2, 1.25)
    assert (lease.campaign_sha256, lease.expires_utc, lease.rule) == (SHA, "2026-11-18T12:30:00Z", RULE_LEASE)
    pinned = lease.to_json()
    assert set(pinned) == {"rule", *(set(FILE) - {"schema"})}
    assert RunLease.from_json(pinned) == lease
    assert lease.name == f"lease 7 of run {KEY}"


def test_c41_a_whole_number_grant_is_kept_as_dollars_so_the_pinned_text_is_one_text(tmp_path):
    """C41: a grant of 5 and of 5.0 pin the same text."""
    assert read_lease(written(tmp_path, {**FILE, "grant_usd": 5})).to_json() == {
        **read_lease(written(tmp_path, {**FILE, "grant_usd": 5.0})).to_json()
    }
    assert isinstance(read_lease(written(tmp_path, {**FILE, "grant_usd": 5})).grant_usd, float)


@pytest.mark.parametrize(
    "change",
    [
        {"lease_id": 0},
        {"lease_id": -3},
        {"lease_id": True},
        {"lease_id": 1.5},
        {"lease_id": "7"},
        {"attempt_base": -1},
        {"attempt_base": True},
        {"attempt_base": 2.0},
        {"grant_usd": -0.01},
        {"grant_usd": True},
        {"grant_usd": "1.25"},
        {"grant_usd": None},
        {"run_key": ""},
        {"run_key": 5},
        {"campaign_sha256": "ab" * 31},
        {"campaign_sha256": "AB" * 32},
        {"expires_utc": "2026-11-18"},
        {"expires_utc": "2026-11-18T12:30:00+00:00"},
        {"expires_utc": "2026-11-18T12:30:00.5Z"},
        {"expires_utc": 1700000000},
        {"schema": "expcore-lease/2"},
    ],
)
def test_c41_a_lease_file_with_a_field_of_another_kind_is_refused(tmp_path, change):
    """C41: each field has one form; anything else is a refusal that names the field, never a guess."""
    with pytest.raises(LeaseError, match="lease"):
        read_lease(written(tmp_path, {**FILE, **change}))


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_c41_a_grant_that_is_not_a_finite_number_is_refused_whatever_json_allows(tmp_path, constant):
    """C41: Python's json reads NaN and Infinity; a ceiling made of one would never hold, so the file is refused."""
    text = json.dumps(FILE).replace("1.25", constant)
    with pytest.raises(LeaseError, match=r"NaN|Infinity|finite"):
        read_lease(written(tmp_path, None, text=text))


@pytest.mark.parametrize("drop", sorted(set(FILE)))
def test_c41_a_lease_file_lacking_a_key_or_with_another_is_refused(tmp_path, drop):
    """C41: exactly the keys of the shape: a missing one and an extra one are both refused."""
    short = {key: value for key, value in FILE.items() if key != drop}
    with pytest.raises(LeaseError, match="exactly the keys"):
        read_lease(written(tmp_path, short))
    with pytest.raises(LeaseError, match="exactly the keys"):
        read_lease(written(tmp_path, {**FILE, "note": "x"}))


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", "null", '"a lease"'])
def test_c41_a_lease_file_that_is_not_a_json_object_is_refused(tmp_path, text):
    """C41: a refusal, not a traceback."""
    with pytest.raises(LeaseError, match="lease file"):
        read_lease(written(tmp_path, None, text=text))


def test_c41_a_lease_file_that_is_not_there_is_a_refusal(tmp_path):
    """C41: the one call the CLI makes refuses a missing file in words."""
    with pytest.raises(LeaseError, match="cannot read the lease file"):
        read_lease(tmp_path / "absent.json")


def test_c41_a_lease_refusal_is_a_shard_refusal_so_the_command_line_refuses_it():
    """C41: `cli._REFUSALS` holds ShardError; a lease error needs no entry of its own."""
    assert issubclass(LeaseError, ShardError)


def test_c41_a_lease_is_expired_from_its_expiry_on_and_not_before():
    """C41: the term ends at the expiry second itself, so a lease started at it is refused."""
    lease = RunLease(KEY, 7, 0, 0.0, SHA, "2026-11-18T12:30:00Z")
    before = dt.datetime(2026, 11, 18, 12, 29, 59, tzinfo=dt.UTC)
    assert not lease.expired(before)
    assert lease.expired(before + dt.timedelta(seconds=1))
    assert lease.expired(before + dt.timedelta(days=1))


def test_c41_a_spec_with_another_rule_or_keys_does_not_read_as_a_lease():
    """C41: a shard's spec has no lease id, and a lease's spec is not read as a shard's."""
    lease = RunLease(KEY, 7, 0, 1.0, SHA, "2026-11-18T12:30:00Z")
    with pytest.raises(LeaseError, match="exactly the keys"):
        RunLease.from_json({**lease.to_json(), "index": 1})
    with pytest.raises(ShardError):
        RunLease(KEY, 7, 0, 1.0, SHA, "2026-11-18T12:30:00Z", rule="expcore-shard/2: per model")
