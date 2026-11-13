"""R02: the totals of a version-3 record may say whether the attempt was held to the reservation R, in two fields.

`totals.reserved_capped` is the bound the budget gate reserved the run by, the same value the store's attempt row keeps
(True: by the reply maximum, False: by the window), and `totals.reply_cap_unheld` is whether the campaign had taken the
attempt's model off the reply maximum when the loop ended. A run that no gate decided supplies neither, and its record
has neither: absent is "not recorded", never a null (R10).
"""

import pytest
from record_support import no_secrets_in_the_environment, record_run

from expcore.records.record import RunRecord, decode
from expcore.records.schema import SCHEMAS, RecordError, fields_of, optional_in, validate
from expcore.replay.replay import replay_record

pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]

V2, V3 = "expcore-record/2", "expcore-record/3"
FIELDS = ("reserved_capped", "reply_cap_unheld")


def written(reserved_capped=None, unheld=None, **options):
    """A record of a one-turn run whose executor supplied the two values, as `record_run` of the executor does: the
    mark is read when the loop has ended, so it is a function."""
    supplied = {"reserved_capped": reserved_capped, "reply_cap_unheld": lambda: unheld, **options}
    return record_run(["hello"], {}, record_options=supplied)


@pytest.mark.parametrize("unheld", [True, False])
@pytest.mark.parametrize("capped", [True, False])
def test_R02_true_and_false_are_written_after_the_last_total_and_read_back_as_they_were(capped, unheld):
    """R02: each value is a boolean at the end of the totals, in the order the schema lists them, and the file loads
    and writes back to its own bytes."""
    record = written(capped, unheld)
    assert record.totals["reserved_capped"] is capped
    assert record.totals["reply_cap_unheld"] is unheld
    assert list(record.totals)[-3:] == ["final_text", *FIELDS]
    text = record.dumps()
    assert validate(decode(text)) == V3
    assert RunRecord.from_json(decode(text)).dumps() == text


@pytest.mark.parametrize("unheld", [True, False])
@pytest.mark.parametrize("capped", [True, False])
def test_R03_a_replay_serves_the_two_values_back_as_recorded(capped, unheld):
    """R03: the replay runs no gate and reads no store, so the stored values are what it writes."""
    record = written(capped, unheld)
    assert replay_record(record).dumps() == record.dumps()


@pytest.mark.parametrize(
    ("capped", "unheld", "kept"),
    [(None, None, ()), (True, None, ("reserved_capped",)), (None, False, ("reply_cap_unheld",))],
    ids=["neither", "only-reserved", "only-mark"],
)
def test_R02_a_value_the_run_does_not_supply_is_absent_never_null(capped, unheld, kept):
    """R02 and R10: None means no gate decided it. The field is left out, in the record and so in its file, and a
    replay does not fill it in."""
    record = written(capped, unheld)
    assert [name for name in FIELDS if name in record.totals] == list(kept)
    text = record.dumps()
    assert all((f'"{name}"' in text) is (name in kept) for name in FIELDS)
    assert RunRecord.from_json(decode(text)).dumps() == text
    assert replay_record(record).dumps() == text


def test_R02_a_record_written_without_the_options_is_the_record_written_before_them():
    """R02: a run that passes nothing writes the totals it always wrote."""
    plain = record_run(["hello"], {})
    assert not set(FIELDS) & set(plain.totals)
    assert list(plain.totals)[-1] == "final_text"
    assert written(None, None).dumps() == plain.dumps()


def test_R02_the_two_fields_are_optional_in_version_three_and_in_no_earlier_version():
    """R02: added to the totals by version 3 as optional; a version-2 record has no such field."""
    (v3,) = (s for s in SCHEMAS if s.name == V3)
    assert set(FIELDS) <= set(v3.added["totals"])
    assert set(FIELDS) <= set(v3.optional["totals"])
    assert set(FIELDS) <= optional_in("totals", V3)
    assert not set(FIELDS) & set(fields_of("totals", V2))


@pytest.mark.parametrize("name", FIELDS)
@pytest.mark.parametrize("bad", [None, 1, 0, "true", []], ids=["null", "one", "zero", "text", "list"])
def test_R02_a_present_field_is_a_boolean(name, bad):
    """R02: a null is refused, since the writer leaves the field out when there is nothing to say, and a number is not a
    flag."""
    data = decode(written(True, False).dumps())
    data["totals"][name] = bad
    with pytest.raises(RecordError, match=rf"totals\.{name}: expected bool"):
        validate(data)


@pytest.mark.parametrize("name", FIELDS)
def test_R02_a_record_of_version_two_that_holds_one_is_refused(name):
    """R02: a version-2 record predates the fields, so one that holds either is unknown to its own version."""
    data = decode(record_run(["hello"], {}, record_options={"schema_version": V2}).dumps())
    assert validate(data) == V2
    data["totals"][name] = True
    with pytest.raises(RecordError, match=rf"totals: unknown field\(s\) \['{name}'\]"):
        validate(data)


@pytest.mark.parametrize("options", [{"reserved_capped": True}, {"reply_cap_unheld": lambda: False}], ids=FIELDS)
def test_R02_the_builder_refuses_a_value_for_a_version_that_predates_the_field(options):
    """R02: a record of version 2 is only built by a test, and holds no field a later version added; a value for one
    is an error, never dropped."""
    with pytest.raises(RecordError, match="predates"):
        record_run(["hello"], {}, record_options={"schema_version": V2, **options})


@pytest.mark.parametrize("name", FIELDS)
@pytest.mark.parametrize("bad", [1, "yes"])
def test_R02_the_builder_refuses_a_value_that_is_not_true_false_or_none(name, bad):
    """R02: the store refuses such a value for its column, and the record does the same."""
    options = {"reserved_capped": bad} if name == "reserved_capped" else {"reply_cap_unheld": lambda: bad}
    with pytest.raises(RecordError, match=name):
        record_run(["hello"], {}, record_options=options)
