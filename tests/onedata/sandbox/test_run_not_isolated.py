"""O72: a run the watch ended for a cell it could not isolate is counted under a kind of its own.

Such a run is set aside, and a set-aside attempt reaches the campaign's export and report only through its
infrastructure kind (`retries_by_kind`, "Infrastructure failures by condition and kind"), so the kind is what lets a
campaign count these runs apart from every other sandbox failure."""

import pytest
from campaign_support import infra, planned_store, run_one

from expcore import report
from expcore.campaign.export import export_run_state
from expcore.errors import infra_kinds, is_treatable, uncharged_kinds
from expcore_onedata.sandbox import kinds

RUN_ENDED = {
    "outcome": "watch_ended_run",
    "scope": "run",
    "attributed": False,
    "reason": "memory_limit",
    "what": "memory_hidden",
    "why": "another run's cell is charged to the same cgroup",
}
KILL_THAT_ENDED_THE_RUN = {
    "reason": "memory_limit",
    "category": "infrastructure",
    "scope": "run",
    "basis": "the harness's watch killed the run's processes; the watch ended the run: another run's cell is charged",
}


def test_o72_the_kind_is_the_platform_s_own_never_a_treatment_and_costs_the_run_an_attempt():
    """O72: set aside like every sandbox failure, charged as the kind `sandbox` is, and no condition may declare it."""
    assert kinds.CELL_NOT_ISOLATED == "cell_not_isolated"
    assert kinds.CELL_NOT_ISOLATED in infra_kinds()
    assert not is_treatable(kinds.CELL_NOT_ISOLATED)
    assert kinds.CELL_NOT_ISOLATED not in uncharged_kinds()
    assert "sandbox" in infra_kinds()  # the core's kind stays for every other sandbox failure


def test_o72_the_run_s_end_names_it_for_every_later_exec_and_the_collection():
    """O72: the refusal of an exec or a collection after the watch ended the run."""
    error = kinds.run_ended_error(RUN_ENDED)
    assert error.kind == kinds.CELL_NOT_ISOLATED
    assert "another run's cell is charged to the same cgroup" in error.detail


def test_o72_the_kill_that_ended_the_run_names_it_for_the_cell_it_killed():
    """O72: the code tool raises the cell's own kill, which is the one that ended the run."""
    error = kinds.kill_error(KILL_THAT_ENDED_THE_RUN)
    assert error is not None
    assert error.kind == kinds.CELL_NOT_ISOLATED


@pytest.mark.parametrize("scope", [None, "cell"])
def test_o72_infrastructure_that_ended_no_run_stays_a_sandbox_failure(scope):
    """O72: a kill of the host's making (an OOM kill of the step's cgroup) has no `scope: run`, so it is `sandbox`."""
    kill = {"reason": "host_oom", "category": "infrastructure", "basis": "the step's cgroup counted an OOM kill"}
    error = kinds.kill_error({**kill, **({} if scope is None else {"scope": scope})})
    assert error is not None
    assert error.kind == "sandbox"


def test_o72_the_export_and_the_report_count_these_runs_per_condition_under_the_kind(tmp_path):
    """O72: three attempts ended `cell_not_isolated`, two in one condition and one in another, and one ended as any
    other sandbox failure. The export counts each under its own kind and condition, and the report's table of
    infrastructure failures by condition and kind has a row for the kind in each, apart from the `sandbox` row."""
    store, schema, manifest = planned_store(tmp_path, models=("m1", "m2"))
    chosen = {}
    for row in manifest.rows:
        chosen.setdefault(schema.condition_of(row.key), []).append(row.key.text)
    (first, first_keys), (second, second_keys) = list(chosen.items())[:2]
    run_one(store, first_keys[0], infra(kinds.CELL_NOT_ISOLATED, "the watch ended the run"), max_attempts=10)
    run_one(store, first_keys[0], infra(kinds.CELL_NOT_ISOLATED, "the watch ended the run"), max_attempts=10)
    run_one(store, second_keys[0], infra(kinds.CELL_NOT_ISOLATED, "the watch ended the run"), max_attempts=10)
    run_one(store, second_keys[0], infra("sandbox", "a container died"), max_attempts=10)
    data = export_run_state(store)
    assert kinds.CELL_NOT_ISOLATED in data["kinds"]
    counted = {(row["condition"], row["kind"]): row["set_aside"] for row in data["infrastructure_by_condition"]}
    assert counted[(first, kinds.CELL_NOT_ISOLATED)] == 2
    assert counted[(second, kinds.CELL_NOT_ISOLATED)] == 1
    assert counted[(second, "sandbox")] == 1
    lines = report._infrastructure(data, [first, second])
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines if line.startswith("|")]
    shown = {
        (row[0], row[1]): row[3] for row in rows[1:]
    }  # the header is the first row; the set-aside column is the fourth
    assert shown[(first, kinds.CELL_NOT_ISOLATED)] == "2"
    assert shown[(second, kinds.CELL_NOT_ISOLATED)] == "1"
    assert shown[(second, "sandbox")] == "1"
