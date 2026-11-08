"""C01: a record is read, and replayed, under the treatment rule of its own schema version."""

import copy

import pytest
from record_support import no_secrets_in_the_environment, record_run

from expcore.errors import InfraError
from expcore.records.record import RunRecord, decode
from expcore.records.schema import CURRENT, RecordError, validate
from expcore.replay.replay import replay_record

pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]

V1, V2 = "expcore-record/1", "expcore-record/2"
BEFORE_A_TURN = [InfraError("knowledge", "the tool server did not start")]
AFTER_A_TURN = [[("echo", {"x": "1"})], InfraError("knowledge", "the tool server went down")]
ECHO = {"echo": lambda args: "echoed"}


def record_as(version, script):
    """A record of a scripted run under a condition declaring knowledge as its treatment, written as `version`."""
    return record_run(script, ECHO, record_options={"treatment_kinds": ["knowledge"], "schema_version": version})


def test_c01_new_records_are_version_two():
    """C01: the current version is 2, and records are built as the current version."""
    assert CURRENT == V2
    assert record_run(AFTER_A_TURN, ECHO).schema_version == V2


def test_c01_a_version_one_record_flags_treatment_by_the_declared_kind_alone():
    """C01: under the version-1 rule a failure before the first turn is treatment, and the record is valid, loads,
    writes back byte for byte and replays to the same flag."""
    record = record_as(V1, BEFORE_A_TURN)
    assert record.transcript == []
    assert (record.schema_version, record.outcome["treatment"], record.set_aside) == (V1, True, False)
    text = record.dumps()
    again = RunRecord.from_json(decode(text.encode()))
    assert again.dumps() == text
    replayed = replay_record(again)
    assert (replayed.schema_version, replayed.outcome["treatment"]) == (V1, True)
    assert replayed.dumps() == text


def test_c01_a_version_two_record_flags_it_set_aside_and_replays_to_the_same_flag():
    """C01: the same run written as version 2 is not treatment; the flag is the record's own version's."""
    record = record_as(V2, BEFORE_A_TURN)
    assert (record.schema_version, record.outcome["treatment"], record.set_aside) == (V2, False, True)
    replayed = replay_record(record)
    assert (replayed.schema_version, replayed.outcome["treatment"]) == (V2, False)
    assert replayed.dumps() == record.dumps()


@pytest.mark.parametrize("version", [V1, V2])
def test_c01_after_a_completed_turn_both_versions_flag_treatment(version):
    """C01: the rules differ only before the first turn."""
    record = record_as(version, AFTER_A_TURN)
    assert (record.outcome["treatment"], len(record.transcript)) == (True, 1)
    assert replay_record(record).outcome["treatment"] is True


def test_c01_each_version_refuses_the_flag_the_other_would_set():
    """C01: validation follows the record's version: the flag a version-1 record may carry with no turn is refused in
    a version-2 record, and the version-2 flag (false) is refused in a version-1 record."""
    v1 = decode(record_as(V1, BEFORE_A_TURN).dumps().encode())
    assert validate(v1) == V1
    as_v2 = copy.deepcopy(v1)
    as_v2["schema_version"] = V2
    with pytest.raises(RecordError, match=r"outcome\.treatment: is True but .* 0 completed turn\(s\) make it False"):
        validate(as_v2)
    v2 = decode(record_as(V2, BEFORE_A_TURN).dumps().encode())
    assert validate(v2) == V2
    as_v1 = copy.deepcopy(v2)
    as_v1["schema_version"] = V1
    with pytest.raises(RecordError, match=r"outcome\.treatment: is False but .* 0 completed turn\(s\) make it True"):
        validate(as_v1)


def test_c01_a_live_run_cannot_choose_an_older_record_version():
    """C01: record_attempt always writes the current version, so a live run cannot apply the version-1 treatment
    rule to a failure before its first turn."""
    with pytest.raises(TypeError, match="current record version"):
        record_run(BEFORE_A_TURN, ECHO, record_options={"schema_version": V1, "use_public": True})
