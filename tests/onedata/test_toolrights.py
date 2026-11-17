"""O31: which space privileges each tool of the pinned onedata-mcp server needs, kept beside the pin it was read at."""

import json
import re
from pathlib import Path

import pytest

from expcore_onedata.rights import BASELINE_RIGHTS, KNOWN_RIGHTS, WRITE_RIGHTS
from expcore_onedata.toolrights import DATA_WRITE, OMCP_PIN, TOOL_NEEDS, agreement_problem

ROOT = Path(__file__).resolve().parents[2]
PINNED = json.loads((ROOT / "tests" / "data" / "omcp-pinned-tools.json").read_text(encoding="utf-8"))
MUTATING = {*WRITE_RIGHTS, DATA_WRITE}


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
    """O31: the server's own list of its mutating tools (`WRITE_TOOL_NAMES`) is the annotated-destructive set, and
    those tools, and only those, need a privilege that changes something."""
    destructive = {name for name, kind in PINNED["tools"].items() if kind == "destructive"}
    assert destructive == set(PINNED["write_tool_names"])
    for name, needs in TOOL_NEEDS.items():
        assert bool(MUTATING & set(needs)) == (name in destructive), name


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


@pytest.mark.parametrize(
    ("allow", "rights", "key", "words"),
    [
        (
            ["schedule_file_replication"],
            BASELINE_RIGHTS,
            "rights",
            ["space_schedule_replication", "schedule_file_replication"],
        ),
        (["create_file"], BASELINE_RIGHTS, "allow", ["create_file", "space_write_data"]),
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
