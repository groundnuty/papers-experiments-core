"""O58: where a relayed verb's time went, from the relay's answer to the injector's reading. The relay puts the time
from a verb's issue to each step's accounted Start, and from its answer to each End, in the answer
(`fault_relay_timing`); the HPC driver keeps it (`HpcDriver.last_timing`) and the injector journals it where the verb's
own evidence goes: a stop's with the stop's (`stop.timing`), a start's in the restore (`start_timing`), a link
restart's in the heal's action. A controller stall during a scored batch is then the site's in the record.

The federation is the fake one of `hpc_support`; a stand-in for the relay's transport adds the timing to each answer.
"""

import json
import subprocess

import pytest
from fault_support import SERVICES, FakeClock, injector_for, write_faults
from heal_support import CRASHED, LABEL, READER, TARGET, four_providers
from hpc_support import PING, TOOL, FakeHpc

from expcore_onedata.compose import CommandResult
from expcore_onedata.fault_hpc import HpcDriver, VerbCommand, timing_evidence
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.fault_relay import RelayTimeout
from expcore_onedata.testing import write_platform

SERVICE = SERVICES["beta"]
LABELS = {service: f"providers[{index}]" for index, service in enumerate(SERVICES.values())}
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
STALL = 201.0  # a controller that stalled for its message timeout: the step started this long after the verb's issue


def timing(word="stop", start=1.0, end=-0.5, **changes):
    """A relay's `timing` for a verb of one step (its name, which carries the allocation's, included)."""
    step = {
        "word": word,
        "name": f"hpcop-study-0a1b2c3d-{SERVICE}-{word}-ab12cd34ef56",
        "id": "4242.7",
        "state": "COMPLETED",
        "start_after_issue_s": start,
        "end_after_answer_s": end,
    }
    return {
        "resolution_s": 1,
        "accounting": "read",
        "reason": None,
        "issued_wall": 1.0,
        "answered_wall": 2.0,
        "steps": [{**step, **changes}],
    }


class Relayed:
    """The relay's transport as the driver sees it: the fake federation's answers, each with a timing the test chose by
    verb; a stop that hangs raises what the relay's client raises (with the timing when there is one)."""

    def __init__(self, federation, timings):
        self.federation, self.timings, self.asked = federation, timings, []

    def __call__(self, argv, *, timeout_s):
        verb = argv[1] if argv[1] != "exec" else ("ping" if argv[4:] == PING else "restart_link")
        self.asked.append(verb)
        found = self.timings.get(verb)
        try:
            done = self.federation(argv, timeout_s=timeout_s)
        except subprocess.TimeoutExpired as error:
            late = RelayTimeout(error.cmd, error.timeout)
            late.timing = found
            raise late from None
        return CommandResult(done.returncode, done.stdout, done.stderr, found)


def driver_over(federation, timings, **options):
    relayed = Relayed(federation, timings)
    driver = HpcDriver(VerbCommand([TOOL], relayed), probe=federation.probe, **options)
    driver.relayed = relayed  # the test's way to change what the relay says next
    return driver


def lines_of(tmp_path):
    path = tmp_path / "fault-journal" / JOURNAL
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]


def outage(tmp_path, timings, federation_changes=None):
    platform = write_faults(tmp_path, command_timeout_s=3, settle_timeout_s=10)
    clock = FakeClock()
    federation = FakeHpc(clock, LABELS)
    for name, value in (federation_changes or {}).items():
        setattr(federation, name, value)
    driver = driver_over(federation, timings, timeout_s=3)
    return injector_for(platform, driver, clock=clock), driver, federation


def test_o58_the_driver_keeps_each_kinds_latest_timing_from_the_relays_answer(tmp_path):
    """O58: every relayed request's timing is kept under its kind (`stop`, `start`, `status`, `ping`, `restart_link`),
    the latest of each, as the checked object the injector journals: the names, which carry the allocation's, are left
    out, and the figures are as the relay gave them."""
    injector, driver, _ = outage(
        tmp_path,
        {"stop": timing("stop", 1.0, -0.5), "start": timing("run", STALL, 3.0), "status": timing("status", 2.0)},
    )
    injector.start(RUN)
    assert injector.clear() is True
    kept = driver.last_timing
    assert set(kept) >= {"stop", "start", "status"}
    (step,) = kept["start"]["steps"]
    assert (step["word"], step["start_after_issue_s"], step["end_after_answer_s"]) == ("run", STALL, 3.0)
    assert "name" not in step
    assert json.dumps(kept).count("hpcop-study") == 0


def test_o58_a_stops_timing_is_journalled_with_the_stops_evidence(tmp_path):
    """O58: the `injected` line's `stop` holds the stop's own evidence and where its time went, beside each other; the
    snapshot's steps hold the same."""
    injector, _, _ = outage(tmp_path, {"stop": timing("stop", STALL, -2.0)})
    injector.start(RUN)
    assert injector.clear() is True
    injected = next(line for line in lines_of(tmp_path) if line["event"] == "injected")
    evidence = injected["stop"]
    assert evidence["stopped"] is True
    (step,) = evidence["timing"]["steps"]
    assert (step["word"], step["start_after_issue_s"], step["end_after_answer_s"]) == ("stop", STALL, -2.0)
    assert evidence["timing"]["accounting"] == "read"


def test_o58_a_stop_that_timed_out_journals_its_timing_with_no_evidence_of_the_stop(tmp_path):
    """O58: a stop with no answer has no evidence, but where its time went (the step started STALL seconds late, or
    never) is the very figure that attributes the stall to the site: `stop_unknown` holds it, and the stop is still of
    unknown outcome."""
    injector, driver, _ = outage(
        tmp_path,
        {"stop": timing("stop", None, None, unknown={"start_after_issue_s": "no_such_step"})},
        {"stop_hangs": True},
    )
    injector.start(RUN)
    unknown = next(line for line in lines_of(tmp_path) if line["event"] == "stop_unknown")
    assert set(unknown["stop"]) == {"timing"}
    (step,) = unknown["stop"]["timing"]["steps"]
    assert step["start_after_issue_s"] is None
    assert step["unknown"]["start_after_issue_s"] == "no_such_step"
    assert driver.last_stop is None  # still no evidence of the stop's own


def test_o58_a_starts_timing_is_recorded_in_the_restore_served_or_not(tmp_path):
    """O58: the restore's `verified` step and the injection hold where the start's time went: here a start whose step
    began 201 s after it was issued, a stall the record attributes to the site."""
    injector, _, _ = outage(tmp_path, {"start": timing("run", STALL, 4.0)})
    injector.start(RUN)
    assert injector.clear() is True
    verified = [step for step in injector.snapshot()["steps"] if step["step"] == "verified"][-1]
    (step,) = verified["start_timing"]["steps"]
    assert step["start_after_issue_s"] == STALL
    assert injector.snapshot()["injections"][0]["start_timing"] == verified["start_timing"]


def test_o58_a_failed_restore_still_records_where_the_starts_time_went(tmp_path):
    """O58: the provider never serves again (its start fails): the restore is open, and the record still says the
    start's figures, which are what show a stall."""
    injector, _, _ = outage(tmp_path, {"start": timing("run", STALL, None)}, {"start_fails": {SERVICE}})
    injector.start(RUN)
    assert injector.clear() is False
    shot = injector.snapshot()["injections"][0]
    assert shot["cleared"] is False
    assert shot["start_timing"]["steps"][0]["start_after_issue_s"] == STALL


def test_o58_a_driver_with_no_timing_journals_what_it_always_did(tmp_path):
    """O58: a relay that gave no timing (an older answer, an unreadable one) leaves the journal and the snapshot exactly
    as they were: no key, never a 0."""
    injector, driver, _ = outage(tmp_path, {})
    injector.start(RUN)
    assert injector.clear() is True
    assert driver.last_timing == {}
    injected = next(line for line in lines_of(tmp_path) if line["event"] == "injected")
    assert "timing" not in injected["stop"]
    assert "start_timing" not in json.dumps(injector.snapshot())


def test_o58_a_request_that_never_ran_leaves_no_earlier_requests_timing(tmp_path):
    """O58: a refused request keeps no figures, and does not keep the last one's: the latest of a kind is the latest."""
    federation = FakeHpc(FakeClock(), LABELS)
    driver = driver_over(federation, {"status": timing("status", 2.0)})
    driver.observe(SERVICE)
    assert "status" in driver.last_timing
    federation.unreadable.add(SERVICE)  # the next status fails, and the relay has no timing for it
    driver.relayed.timings = {}
    with pytest.raises(Exception, match="failed"):
        driver.observe(SERVICE)
    assert "status" not in driver.last_timing


@pytest.mark.parametrize(
    "value",
    [None, "x", {}, {"accounting": "other", "steps": []}, {"accounting": "read"}, {"steps": []}],
)
def test_o58_anything_that_is_not_a_timing_is_none(value):
    """O58: the driver journals only what the relay writes: anything else is nothing."""
    assert timing_evidence(value) is None


def test_o58_a_timing_is_checked_figure_by_figure_and_names_are_dropped():
    """O58: a figure that is not a finite number is unknown with its reason (or a generic one), an id or a state of the
    wrong form is None, a step of no known word is left out, and at most eight steps are kept."""
    steps = [
        {
            "word": "stop",
            "name": "secret-name",
            "id": "4242.7",
            "state": "CANCELLED",
            "start_after_issue_s": float("nan"),
            "end_after_answer_s": True,
            "unknown": {"start_after_issue_s": "not_recorded"},
        },
        {"word": "bash", "start_after_issue_s": 1.0, "end_after_answer_s": 1.0},
        {
            "word": "exec",
            "id": "../etc",
            "state": "cancelled by me",
            "start_after_issue_s": 2.0,
            "end_after_answer_s": -1.0,
        },
        *[{"word": "status", "start_after_issue_s": 0.0, "end_after_answer_s": 0.0}] * 12,
    ]
    kept = timing_evidence({"accounting": "unreadable", "reason": "sacct_timeout", "steps": steps})
    assert (kept["accounting"], kept["reason"]) == ("unreadable", "sacct_timeout")
    first, second = kept["steps"][0], kept["steps"][1]
    assert first["start_after_issue_s"] is None
    assert first["end_after_answer_s"] is None
    assert first["unknown"] == {"start_after_issue_s": "not_recorded", "end_after_answer_s": "unknown"}
    assert (first["id"], first["state"]) == ("4242.7", "CANCELLED")
    assert (second["word"], second["id"], second["state"]) == ("exec", None, None)
    assert len(kept["steps"]) <= 8
    assert "secret-name" not in json.dumps(kept)


# -- the heal's link restart ------------------------------------------------------------------------------------------


def test_o58_a_link_restarts_timing_is_in_the_heals_action(tmp_path):
    """O58: the heal's `restart_link` action holds where the exec's time went (the step that restarted the link began
    201 s after it was issued), and the ping's timing is kept by the driver but is no action."""
    platform = write_platform(tmp_path, lambda data: four_providers(data, heal="all_links"))
    clock = FakeClock()
    federation = FakeHpc(clock, LABEL)
    federation.stuck = {(READER, CRASHED)}
    driver = driver_over(federation, {"restart_link": timing("exec", STALL, 1.0), "ping": timing("exec", 0.5, 0.0)})
    injector = injector_for(platform, driver, target=TARGET, clock=clock)
    injector.start(RUN)
    assert injector.clear() is True
    heal = [s for s in injector.snapshot()["steps"] if s["step"] == "heal"][-1]
    restarts = [a for a in heal["actions"] if a["action"] == "restart_link"]
    assert restarts
    for action in restarts:
        assert action["timing"]["steps"][0]["start_after_issue_s"] == STALL
    assert all("timing" not in a for a in heal["actions"] if a["action"] != "restart_link")
