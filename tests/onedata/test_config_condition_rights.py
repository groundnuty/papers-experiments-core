"""O31: a condition's tool allow-list and its declared rights agree, or the platform file is refused at load."""

import pytest

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.testing import BASELINE, write_platform

REPLICATE = "space_schedule_replication"
MANAGE_QOS = "space_manage_qos"
VIEW_TRANSFERS = "space_view_transfers"
VIEW_QOS = "space_view_qos"


def condition(allow, rights):
    """An `edit` for `write_platform`: the first condition's allow-list and rights (it sits at position 0)."""

    def edit(data):
        data["toolserver"]["conditions"]["read"].update(allow=list(allow), rights=list(rights))

    return edit


def load(tmp_path, allow, rights):
    return load_platform(write_platform(tmp_path, condition(allow, rights)), require_secrets=False)


def refused(tmp_path, allow, rights):
    """The text of the refusal of a condition, and nothing else of the file's contents in it."""
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, allow, rights)
    return str(caught.value).replace(str(tmp_path), "")  # the test's directory name holds words of its own


def test_o31_a_write_tool_without_its_write_right_is_refused_naming_the_tool_and_the_right(tmp_path):
    """O31: the agent would see a tool that always fails (401 on a read-only token, 403 on a user without the
    privilege), so the file is refused, by the condition's position."""
    text = refused(tmp_path, ["get_file_distribution", "schedule_file_replication"], BASELINE)
    assert "conditions[0]" in text
    assert "rights" in text
    assert REPLICATE in text
    assert "schedule_file_replication" in text
    assert "get_file_distribution" not in text  # only the tool that disagrees is named


@pytest.mark.parametrize("tool", ["add_file_qos_requirement", "remove_qos_requirement"])
def test_o31_a_qos_tool_without_the_right_to_manage_qos_is_refused(tmp_path, tool):
    """O31: adding and removing a QoS requirement share one privilege."""
    text = refused(tmp_path, [tool], [*BASELINE, VIEW_QOS])
    assert MANAGE_QOS in text
    assert tool in text


def test_o31_a_write_right_that_is_not_the_tools_is_no_substitute(tmp_path):
    """O31: any write right is not enough: replication needs its own, and the QoS right does not stand for it."""
    text = refused(tmp_path, ["schedule_file_replication"], [*BASELINE, MANAGE_QOS])
    assert REPLICATE in text
    assert "schedule_file_replication" in text


@pytest.mark.parametrize(
    "tool", ["get_transfer", "list_space_transfers", "get_qos_requirement", "get_file_qos_summary"]
)
def test_o31_a_view_tool_without_its_view_right_is_not_refused_because_that_need_is_only_inferred(tmp_path, tool):
    """O31: the view privileges are read from their names, not recorded per tool, so a refusal on them would rest on
    a guess; the rows stay in the table and wait for a live run that records each."""
    assert load(tmp_path, ["get_file_distribution", tool], BASELINE).condition("read").allow == (
        "get_file_distribution",
        tool,
    )


@pytest.mark.parametrize("tool", ["create_file", "create_directory", "delete_file", "set_file_metadata", "move_file"])
def test_o31_a_file_writing_tool_is_refused_because_no_condition_can_grant_its_privilege(tmp_path, tool):
    """O31: a tool-server user is never granted `space_write_data` (the token check refuses it), so these tools fail
    on every call whatever the condition declares, a write right included."""
    text = refused(tmp_path, [tool], [*BASELINE, MANAGE_QOS])
    assert tool in text
    assert "a tool-server user is never granted space_write_data (tokencheck)" in text


def test_o31_a_write_right_that_no_allowed_tool_uses_is_refused_naming_the_right(tmp_path):
    """O31: the token would be wider than the tools need (a user able to add QoS requirements the agent has no tool
    for), so the right is refused, by name."""
    text = refused(tmp_path, ["get_file_distribution", "get_transfer"], [*BASELINE, VIEW_TRANSFERS, MANAGE_QOS])
    assert "conditions[0]" in text
    assert MANAGE_QOS in text
    assert VIEW_TRANSFERS not in text


def test_o31_only_the_unused_write_right_is_named_when_another_write_right_is_used(tmp_path):
    """O31: replication is allowed and its right declared; the QoS right beside it has no tool."""
    text = refused(tmp_path, ["schedule_file_replication"], [*BASELINE, REPLICATE, MANAGE_QOS])
    assert MANAGE_QOS in text
    assert REPLICATE not in text


@pytest.mark.parametrize("right", ["space_cancel_replication", "space_schedule_eviction", "space_cancel_eviction"])
def test_o31_a_write_right_that_no_tool_of_the_pinned_server_has_is_refused(tmp_path, right):
    """O31: the pinned server has no tool to cancel a transfer or to evict, so these rights are never used."""
    text = refused(tmp_path, ["schedule_file_replication"], [*BASELINE, REPLICATE, right])
    assert right in text


def test_o31_a_read_only_condition_that_agrees_loads(tmp_path):
    """O31: read tools, the baseline and the views their tools use."""
    config = load(
        tmp_path,
        ["get_file_distribution", "list_space_providers", "get_transfer", "get_qos_requirement"],
        [*BASELINE, VIEW_TRANSFERS, VIEW_QOS],
    )
    assert not config.condition("read").writes


def test_o31_a_writing_condition_that_agrees_loads(tmp_path):
    """O31: replication and QoS tools, each with the right it uses and the views beside them."""
    rights = [*BASELINE, VIEW_TRANSFERS, VIEW_QOS, REPLICATE, MANAGE_QOS]
    allow = [
        "schedule_file_replication",
        "add_file_qos_requirement",
        "remove_qos_requirement",
        "get_file_distribution",
        "get_transfer",
        "list_space_transfers",
        "get_file_qos_summary",
        "get_qos_requirement",
    ]
    config = load(tmp_path, allow, rights)
    assert config.condition("read").writes
    assert config.condition("read").manages_qos
    assert config.condition("read").allow == tuple(allow)


def test_o31_a_view_right_without_a_view_tool_is_allowed(tmp_path):
    """O31: only write rights widen what the agent can change; a view right alone changes nothing, and a slot's user
    is leased with exactly the rights of its condition, so a view right may be there for the user's sake."""
    config = load(tmp_path, ["get_file_distribution"], [*BASELINE, VIEW_TRANSFERS, VIEW_QOS])
    assert not config.condition("read").writes


def test_o31_a_tool_the_table_does_not_know_is_not_judged_and_keeps_a_write_right_unused_by_the_rest(tmp_path):
    """O31: a tool of another server (the study's stand-in) may use any right, so a write right is not refused as
    unused beside one; a known tool that disagrees still is."""
    assert load(tmp_path, ["echo", "get_file_distribution"], [*BASELINE, MANAGE_QOS]).condition("read").writes
    text = refused(tmp_path, ["echo", "schedule_file_replication"], BASELINE)
    assert REPLICATE in text
    assert "echo" not in text


def test_o31_the_refusal_names_the_position_of_the_condition_and_never_its_name(tmp_path):
    """O31: a condition is known by `conditions[i]` in a message, as in every other refusal."""

    def edit(data):
        data["toolserver"]["conditions"]["hidden-name"] = {
            "allow": ["schedule_file_replication"],
            "rights": list(BASELINE),
            "manifest": data["toolserver"]["conditions"]["read"]["manifest"],
        }

    with pytest.raises(ConfigError, match=r"conditions\[2\]") as caught:
        load_platform(write_platform(tmp_path, edit), require_secrets=False)
    assert "hidden-name" not in str(caught.value)


def test_o31_the_default_test_world_still_loads(tmp_path):
    """O31: its two conditions use the stand-in server's tools, which the table does not know."""
    config = load_platform(write_platform(tmp_path), require_secrets=False)
    assert set(config.toolserver.conditions) == {"read", "manage"}


# A real study's two conditions, as it declares them: the platform file must load them exactly as written.
READ_BASIC = {
    "allow": ["get_file_attributes", "get_file_metadata"],
    "rights": ["space_view", "space_read_data"],
}
WRITE_QOS = {
    "allow": [
        "add_file_qos_requirement",
        "get_file_attributes",
        "get_file_distribution",
        "get_file_metadata",
        "get_file_qos_summary",
        "get_qos_requirement",
        "get_transfer",
        "list_space_providers",
        "list_space_transfers",
        "remove_qos_requirement",
        "schedule_file_replication",
    ],
    "rights": [
        "space_view",
        "space_read_data",
        "space_view_transfers",
        "space_view_qos",
        "space_manage_qos",
        "space_schedule_replication",
    ],
}


def test_o31_a_real_studys_read_only_and_writing_conditions_load_as_declared(tmp_path):
    """O31: acceptance: the conditions of the first federation study (a basic reader of attributes and metadata, and a
    writer of QoS requirements and replications with their views) are neither refused nor changed by the check."""

    def edit(data):
        conditions = data["toolserver"]["conditions"]
        manifest = conditions["read"]["manifest"]
        conditions.clear()
        conditions["read-basic"] = {**READ_BASIC, "manifest": manifest}
        conditions["write-qos"] = {**WRITE_QOS, "manifest": manifest}

    config = load_platform(write_platform(tmp_path, edit), require_secrets=False)
    reader, writer = config.condition("read-basic"), config.condition("write-qos")
    assert (reader.allow, reader.rights) == (tuple(READ_BASIC["allow"]), tuple(sorted(READ_BASIC["rights"])))
    assert (writer.allow, writer.rights) == (tuple(WRITE_QOS["allow"]), tuple(sorted(WRITE_QOS["rights"])))
    assert not reader.writes
    assert writer.writes
    assert writer.manages_qos
