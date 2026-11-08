"""O17: kill causes come from the kernel's counters and the container's state, never from the exit code.

Fixture files under tests/data/sandbox hold invented content in the formats the kernel and docker print.
"""

import json
from pathlib import Path

import pytest
from campaign_support import infra, planned_store, run_one

from expcore.errors import InfraError, infra_kinds
from expcore_onedata.sandbox import earlyoom, infradeath, killcause, kinds

DATA = Path(__file__).resolve().parents[2] / "data" / "sandbox"
CID = "ab12" * 16
LIMIT_S = 60.0


def events(name: str) -> dict[str, int] | None:
    return killcause.parse_events((DATA / name).read_text(encoding="utf-8"))


def kernel(name: str, since_s: float | None = None) -> list[dict]:
    return killcause.parse_kernel_oom((DATA / name).read_text(encoding="utf-8"), since_s=since_s)


def classify(after: str | None, *, running: bool | None = True, duration: float = 5.0, lines=None, exit_code=137):
    """What the harness would conclude for a cell that exited with `exit_code`: the exit code is not an input."""
    del exit_code  # the point of O17: the same exit code, whatever it is, gives the cause the evidence gives
    before = events("memory_events_start.txt")
    return killcause.classify(
        before,
        events(after) if after else None,
        container_running=running,
        duration_s=duration,
        timeout_s=LIMIT_S,
        kernel=lines,
        container_id=CID,
    )


def test_o17_the_counters_of_the_cell_name_the_containers_own_limit():
    got = classify("memory_events_own_limit.txt")
    assert (got["reason"], got["category"]) == ("memory_limit", "agent")


def test_o17_a_kill_with_no_rise_of_the_containers_own_counter_is_the_hosts():
    got = classify("memory_events_host_killer.txt")
    assert (got["reason"], got["category"]) == ("host_oom", "infrastructure")


def test_o17_the_exit_code_decides_nothing():
    """O17: 137, 124, 143 or 0 with the same counters is the same cause, and a cell that ran to its limit is the agent's."""
    causes = {classify("memory_events_host_killer.txt", exit_code=code)["reason"] for code in (137, 124, 143, 0, 1)}
    assert causes == {"host_oom"}
    got = classify("memory_events_start.txt", duration=LIMIT_S)
    assert (got["reason"], got["category"]) == ("time_limit", "agent")


def test_o17_a_dead_container_is_infrastructure_whatever_else_is_on_record():
    got = classify("memory_events_own_limit.txt", running=False)
    assert (got["reason"], got["category"]) == ("container_dead", "infrastructure")


def test_o17_the_kernel_log_decides_only_for_lines_that_name_this_container():
    own = classify(None, lines=kernel("dmesg_own_limit.txt"))
    assert (own["reason"], own["category"]) == ("memory_limit", "agent")
    host = classify(None, lines=kernel("dmesg_host_killer.txt"))
    assert (host["reason"], host["category"]) == ("host_oom", "infrastructure")
    other = classify(None, lines=kernel("dmesg_other_container.txt"))
    assert other["category"] == "unknown"  # another container's kill says nothing about this cell


def test_o17_conflicting_evidence_is_unknown_never_a_guess():
    """O17: counters that say the container's limit and a log that says the host's name nothing."""
    got = classify("memory_events_own_limit.txt", lines=kernel("dmesg_host_killer.txt"))
    assert (got["reason"], got["category"]) == ("unknown", "unknown")


def test_o17_unreadable_counters_with_a_quiet_container_are_unknown():
    got = classify(None, running=None)
    assert got["category"] == "unknown"
    assert killcause.parse_events((DATA / "memory_events_malformed.txt").read_text(encoding="utf-8")) is None


def test_o17_a_kill_nothing_attributes_is_unknown():
    got = classify("memory_events_start.txt", duration=3.0)
    assert (got["reason"], got["category"]) == ("killed_other", "unknown")


def test_o17_the_kernel_window_keeps_only_lines_after_the_start():
    assert len(kernel("dmesg_own_limit.txt", since_s=1000.0)) == 1
    assert kernel("dmesg_own_limit.txt", since_s=5000.0) == []


def test_o17_earlyoom_kills_are_infrastructure_and_beat_the_counters():
    kills = earlyoom.parse_kills((DATA / "journal_earlyoom.txt").read_text(encoding="utf-8"))
    assert [k.pid for k in kills] == [4321, 4400, 99]
    seen: dict[int, list[list[float]]] = {}
    earlyoom.note_pids(seen, "4321\n4322\n", 1790000099.0)
    mine = earlyoom.attribute(kills, {"agent": seen})
    assert [(k["pid"], k["role"]) for k in mine] == [(4321, "agent")]  # 4400 and 99 were nobody's here
    cause = killcause.earlyoom_cause(mine)
    assert (cause["reason"], cause["category"]) == ("earlyoom", "infrastructure")
    assert killcause.run_cause(["memory_limit", "earlyoom", "time_limit"]) == "earlyoom"


def test_o17_one_cause_per_run_in_a_fixed_order():
    assert killcause.run_cause(["time_limit", "memory_limit"]) == "memory_limit"
    assert killcause.run_cause(["unknown", "host_oom"]) == "host_oom"
    assert killcause.run_cause([]) is None
    assert killcause.run_cause(["something new"]) == "unknown"


@pytest.mark.parametrize(
    ("category", "kind"), [("agent", None), ("infrastructure", "sandbox"), ("unknown", "unattributed_kill")]
)
def test_o17_each_category_maps_to_a_kind_of_the_cores_vocabulary(category, kind):
    assert kinds.kind_of(category) == kind
    if kind is not None:
        assert kind in infra_kinds()
    error = kinds.kill_error({"category": category, "reason": "x", "basis": "y"})
    assert (error.kind if error else None) == kind


def test_o17_the_agents_own_kill_is_not_an_infrastructure_error():
    assert kinds.kill_error(classify("memory_events_own_limit.txt")) is None
    assert kinds.kill_error(None) is None
    assert kinds.kill_error({}) is None


def test_o17_an_unrecognised_category_is_unknown_and_retried():
    assert kinds.kill_error({"category": "novel"}).kind == "unattributed_kill"


def test_o17_unknown_is_retried_within_the_attempt_limit(tmp_path):
    """O17: a kill nothing attributes ends the attempt as infrastructure, charged, and the run is retried until the limit."""
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    error = kinds.kill_error(classify("memory_events_start.txt", duration=3.0))
    assert isinstance(error, InfraError)
    statuses = [run_one(store, key, infra(error.kind, str(error)), max_attempts=3) for _ in range(3)]
    assert statuses == ["pending", "pending", "infra_exhausted"]
    assert store.charged_attempts(key) == 3


def inspect_state(name: str) -> dict:
    return infradeath.parse_inspect((DATA / name).read_text(encoding="utf-8"))


def test_o17_a_container_that_ran_throughout_has_no_problem():
    running = {**inspect_state("inspect_running.json"), "oom": 0, "oom_kill": 0}
    assert infradeath.problems("gate", "rest", "sbx-gate", running, dict(running)) == []
    unreadable = inspect_state("inspect_running.json")  # counters that could not be read: unknown is not clean
    assert infradeath.problems("gate", "rest", "sbx-gate", unreadable, dict(unreadable))


@pytest.mark.parametrize(
    ("end", "cause", "category"),
    [
        ("inspect_exited_oom.json", "undetermined", "infrastructure"),  # an OOM kill whose limit nothing names
        ("inspect_exited_clean.json", "undetermined", "infrastructure"),  # stopped from outside with no cause on record
        ("inspect_crashed.json", "component_defect", "infrastructure"),  # a crash signal of its own
        ("inspect_restarted.json", "undetermined", "infrastructure"),
    ],
)
def test_o17_the_death_of_a_component_is_infrastructure_and_retried(end, cause, category):
    start, now = inspect_state("inspect_running.json"), inspect_state(end)
    assert infradeath.problems("gate", "rest", "sbx-gate", start, now)
    got = infradeath.cause_of("gate", start, now)
    assert (got["cause"], got["category"]) == (cause, category)


def test_o17_a_death_that_the_host_caused_is_host_pressure():
    start, now = inspect_state("inspect_running.json"), inspect_state("inspect_exited_oom.json")
    got = infradeath.cause_of("gate", start, now, kernel=kernel("dmesg_host_killer.txt"))
    assert got["cause"] == "host_pressure"
    assert infradeath.cause_of("gate", start, now, earlyoom=True)["cause"] == "host_pressure"


def test_o17_a_death_the_state_cannot_show_is_undetermined_not_clean():
    start = inspect_state("inspect_running.json")
    assert infradeath.problems("gate", "rest", "sbx-gate", start, None, "daemon timeout")
    assert infradeath.cause_of("gate", start, None, "daemon timeout")["cause"] == "undetermined"
    assert infradeath.parse_inspect("not json") is None


def test_o17_the_sidecar_hitting_its_own_limit_under_the_agents_reads_is_the_agents_outcome():
    start, now = inspect_state("inspect_running.json"), inspect_state("inspect_exited_oom.json")
    start["oom"], start["oom_kill"] = 0, 0
    now["oom"], now["oom_kill"] = 1, 1
    got = infradeath.cause_of("sidecar", start, now)
    assert (got["cause"], got["category"]) == ("own_limit_agent", "agent")
    assert kinds.death_error([{**got, "role": "sidecar", "problems": ["x"]}]) is None


def test_o17_a_death_of_infrastructure_makes_a_sandbox_error_that_names_the_cause():
    start, now = inspect_state("inspect_running.json"), inspect_state("inspect_crashed.json")
    problems = infradeath.problems("gate", "rest", "sbx-gate", start, now)
    death = {**infradeath.cause_of("gate", start, now), "role": "gate", "problems": problems}
    error = kinds.death_error([death])
    assert error.kind == "sandbox"
    assert str(error).startswith(infradeath.MARKER) or infradeath.MARKER in str(error)
    assert json.dumps(str(error))  # plain text
