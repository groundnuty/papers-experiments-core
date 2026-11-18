"""C41: an attempt that ran on a lease says which in its record, in one optional field of version 3, `lease_id`.

Two leases of one run can each have an attempt of one number (an attempt that was not charged adds nothing to the base
the ledger sums), so the lease id, with the run key and the attempt, names an attempt. It is written only for an
attempt that ran on a lease: every other record is what it was, and an older version cannot hold it.
"""

import dataclasses
import json

import pytest
from record_support import hand_made

from expcore.records.record import RunRecord, decode
from expcore.records.schema import RecordError, validate


def test_c41_a_record_of_a_lease_writes_its_id_last_and_reads_back_and_a_record_of_none_writes_no_key():
    """C41: the field is absent when the attempt ran on no lease, never a null, and a present one round-trips."""
    plain = hand_made()
    assert plain.lease_id is None
    assert "lease_id" not in json.loads(plain.dumps())
    leased = dataclasses.replace(plain, lease_id=3)
    text = leased.dumps()
    assert list(json.loads(text))[-1] == "lease_id"
    assert json.loads(text)["lease_id"] == 3
    assert RunRecord.from_json(decode(text.encode("utf-8"))) == leased
    assert RunRecord.from_json(decode(text.encode("utf-8"))).dumps() == text


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "3", None, [3]])
def test_c41_a_lease_id_that_is_not_a_whole_number_from_one_is_refused(bad):
    """C41: the fencing token is a whole number from 1: a bool, a float, a string, a zero or a null is no lease."""
    body = json.loads(dataclasses.replace(hand_made(), lease_id=3).dumps())
    body["lease_id"] = bad
    with pytest.raises(RecordError, match="lease_id"):
        validate(body)


def test_c41_a_record_of_an_earlier_version_cannot_hold_a_lease_id():
    """C41: version 3 adds the field; a record of version 2 that holds one is refused when it is written."""
    older = hand_made(schema_version="expcore-record/2")
    with pytest.raises(RecordError, match="lease_id"):
        dataclasses.replace(older, lease_id=3).to_json()
