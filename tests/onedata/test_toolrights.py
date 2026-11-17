"""O31: which space privileges each tool of the pinned onedata-mcp server needs, kept beside the pin it was read at."""

import json
import re
from pathlib import Path

import pytest

from expcore_onedata.rights import BASELINE_RIGHTS, KNOWN_RIGHTS, WRITE_RIGHTS
from expcore_onedata.toolrights import DATA_WRITE, ENFORCED, OMCP_PIN, TOOL_NEEDS, agreement_problem

ROOT = Path(__file__).resolve().parents[2]
PINNED = json.loads((ROOT / "tests" / "data" / "omcp-pinned-tools.json").read_text(encoding="utf-8"))
VIEW_RIGHTS = {"space_view_transfers", "space_view_qos"}


def inventory_pin():
    """The OMCP row's commit, as docs/inventory.md records it."""
    row = re.search(
        r"^\| OMCP \|.*\|\s*`([0-9a-f]{40})`\s*\|", (ROOT / "docs" / "inventory.md").read_text("utf-8"), re.M
    )
    assert row, "docs/inventory.md has no OMCP row with a full commit id"
    return row.group(1)


def test_o31_the_table_is_read_at_the_commit_the_inventory_pins():
    """O31: a new pin in docs/inventory.md fails here until the table and the recorded tools are reviewed at it."""
    assert inventory_pin() == OMCP_PIN
    assert PINNED["pin"] == OMCP_PIN


def test_o31_the_table_holds_every_tool_of_the_pinned_server_and_no_other():
    """O31: a tool the table lacks would be exempt from the check, so every tool the pinned server registers is in
    it."""
    assert set(TOOL_NEEDS) == set(PINNED["tools"])


def test_o31_the_tools_that_write_are_the_ones_the_server_marks_as_such():
    """O31: the server's own list of its mutating tools (`WRITE_TOOL_NAMES`) is exactly the tools whose rows need a
    right that changes something, so a tool the server marks as mutating cannot go unjudged."""
    writing = {name for name, needs in TOOL_NEEDS.items() if ENFORCED & set(needs)}
    assert writing == set(PINNED["write_tool_names"])
    assert set(PINNED["write_tool_names"]) <= set(PINNED["tools"])


def test_o31_only_the_view_rows_are_documented_and_not_enforced():
    """O31: a refusal rests on what the repository records (a write right, and the privilege no tool-server user is
    granted). The view rows are inferred from the privilege names, so they are kept and not enforced, and the baseline
    rows have no need at all, until a live run records each one."""
    assert {*WRITE_RIGHTS, DATA_WRITE} == ENFORCED
    for name, needs in TOOL_NEEDS.items():
        assert set(needs) - ENFORCED <= VIEW_RIGHTS, name


def test_o31_a_tool_needs_known_rights_or_the_one_no_condition_may_hold():
    """O31: beside the baseline every tool has, a need is a right a condition can declare, or `space_write_data`."""
    for name, needs in TOOL_NEEDS.items():
        assert len(set(needs)) == len(needs), name
        assert not set(needs) & set(BASELINE_RIGHTS), name
        assert set(needs) <= {*KNOWN_RIGHTS, DATA_WRITE}, name
    assert DATA_WRITE not in KNOWN_RIGHTS


def test_o31_the_write_rights_no_pinned_tool_uses_are_the_cancels_and_the_eviction():
    """O31: the pinned server schedules a replication and manages QoS; it has no cancel and no eviction tool, so a
    condition declaring one of these rights is refused. A new pin that adds such a tool changes this set."""
    used = {right for needs in TOOL_NEEDS.values() for right in needs}
    assert set(WRITE_RIGHTS) - used == {"space_cancel_replication", "space_schedule_eviction", "space_cancel_eviction"}


def test_o31_nothing_is_wrong_when_the_tools_and_the_rights_agree():
    assert agreement_problem(["get_file_distribution"], BASELINE_RIGHTS) is None
    assert agreement_problem(["echo"], [*BASELINE_RIGHTS, "space_manage_qos"]) is None


def test_o31_a_view_tool_without_its_view_right_is_not_refused_because_that_need_is_only_inferred():
    """O31: refusing it would rest on a guess (the privilege's name), so it waits for a live run that records it."""
    views = ["get_transfer", "list_space_transfers", "get_qos_requirement", "get_file_qos_summary"]
    assert agreement_problem(views, BASELINE_RIGHTS) is None


@pytest.mark.parametrize(
    ("allow", "rights", "key", "words"),
    [
        (
            ["schedule_file_replication"],
            BASELINE_RIGHTS,
            "rights",
            ["space_schedule_replication", "schedule_file_replication"],
        ),
        (
            ["create_file"],
            BASELINE_RIGHTS,
            "allow",
            ["create_file", "a tool-server user is never granted space_write_data (tokencheck)"],
        ),
        (["get_file_distribution"], [*BASELINE_RIGHTS, "space_manage_qos"], "rights", ["space_manage_qos"]),
    ],
)
def test_o31_the_problem_names_the_key_and_the_tool_or_right(allow, rights, key, words):
    problem = agreement_problem(allow, rights)
    assert problem is not None
    assert problem[0] == key
    for word in words:
        assert word in problem[1]


def test_o31_a_tool_missing_a_right_is_reported_before_a_right_no_tool_uses():
    """O31: the tool the agent would call and see fail comes first."""
    problem = agreement_problem(["schedule_file_replication"], [*BASELINE_RIGHTS, "space_manage_qos"])
    assert problem is not None
    assert "space_schedule_replication" in problem[1]
