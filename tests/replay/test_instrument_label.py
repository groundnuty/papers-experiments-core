"""C31: the reading an executor takes for the first-turn hook is in the record's clock and is not the loop's, so a
replay neither serves it nor counts it missing; a run without a hook has no such reading and replays as it did."""

import asyncio

from loop_support import FakeClock, FakeProvider, ScriptedBackend, never
from record_support import CODE, IDENTITY, make_config

from expcore.plugin import FIRST_TURN_LABEL
from expcore.records.build import record_attempt
from expcore.records.instruments import finish_instruments, first_turn_instruments
from expcore.records.record import RunRecord
from expcore.replay.check import check_record
from expcore.replay.clock import ReplayClock
from expcore.replay.replay import replay_record


class Hooked:
    namespace = "hooked"

    def start(self, identity):
        pass

    def at_first_turn(self, moment):
        self.moment = dict(moment)

    def finish(self):
        return {"first_turn_clock_s": self.moment["clock_s"]}

    def check(self):
        pass


def live_record(*instruments) -> RunRecord:
    """A run through the real loop the way an executor does it: the first-turn hook, then the loop on the same timer,
    then the instruments' snapshots."""
    clock, config = FakeClock(), make_config()
    timer = config.timer(clock)
    first_turn_instruments(list(instruments), timer)
    backend = ScriptedBackend([[("look", {})]])
    record = asyncio.run(
        record_attempt(
            backend,
            [FakeProvider({"look": lambda arguments: "seen"})],
            "be brief",
            "do the task",
            config,
            IDENTITY,
            code=dict(CODE),
            core_version="0.1.0",
            timer=timer,
            clock=clock,
            sleep=never,
        )
    )
    return finish_instruments(record, list(instruments))


def test_c31_the_first_turn_reading_is_in_the_clock_before_the_loops_start_and_the_snapshot_carries_its_value():
    """C31: the executor's reading comes first in the record's clock, under its own label, and the instrument wrote
    the same number into its snapshot, so the snapshot's reading is labelled with the clock the record holds."""
    record = live_record(Hooked())
    labels = [label for label, _ in record.clock]
    assert labels[:2] == [FIRST_TURN_LABEL, "start"]
    assert record.instruments["hooked"]["first_turn_clock_s"] == record.clock[0][1]


def test_c31_a_record_with_the_first_turn_reading_replays_byte_for_byte_and_passes_the_check():
    """C31: the replay's clock serves the loop's readings and not the executor's, and the comparison of the clock
    excuses the label on the stored side, so the replayed record is the stored one."""
    record = live_record(Hooked())
    replayed = replay_record(record)
    assert replayed.dumps() == record.dumps()
    assert check_record(record.dumps().encode("utf-8")) is None


def test_c31_the_replay_clock_does_not_serve_the_first_turn_label_and_counts_it_as_neither_read_nor_unread():
    """C31: like a backend's `time_left` reading, the label is dropped before the replay serves anything."""

    async def first_values() -> tuple[float, int]:
        clock = ReplayClock([[FIRST_TURN_LABEL, 7.0], ["start", 1000.0], ["end", 1001.0]])
        return clock(), clock.unused

    assert asyncio.run(first_values()) == (1000.0, 1)


def test_c31_a_run_whose_instruments_have_no_hook_takes_no_reading_and_its_record_has_no_trace_of_one():
    """C31: byte-identity for a study that declares no hook: the clock holds only the loop's readings, and the record
    has neither the label nor the refusal field."""

    class Plain:
        namespace = "plain"

        def start(self, identity):
            pass

        def finish(self):
            return {"ok": True}

    record = live_record(Plain())
    assert FIRST_TURN_LABEL not in [label for label, _ in record.clock]
    assert "instrument_refusal" not in record.dumps()
    assert record.clock[0][0] == "start"
    assert replay_record(record).dumps() == record.dumps()
