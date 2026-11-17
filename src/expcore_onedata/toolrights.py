# Provenance: new
"""Which space privileges each tool of the pinned onedata-mcp server needs, and whether a condition agrees (O31).

A condition declares two things the platform cannot check against each other at run time: the tools its server
shows the agent (`allow`) and the privileges its token's user holds (`rights`). When they disagree, the agent sees a
tool that fails on every call (a write tool without its right, a view tool without its view right), or the token is
wider than the tools use (a write right no tool needs). `agreement_problem` refuses both at load.

`TOOL_NEEDS` is read at `OMCP_PIN`, the onedata-mcp commit `docs/inventory.md` records, and holds every tool that
server registers: the commit's own list of mutating tools (`token_policy.WRITE_TOOL_NAMES`, each annotated
`destructiveHint`) is the set that needs a privilege that changes something, and `tests/data/omcp-pinned-tools.json`
records the names and annotations it was read from. A test fails when the inventory's pin moves, or the table and
the record differ, so a new pin is reviewed here before it is used.

A tool the table does not know (the stand-in server of a test, another server's tool) is not judged, and a condition
that holds one is not refused for a write right nobody uses, since the tool may use it. A name the server does not
show fails at the server's start against the frozen manifest, as before.
"""

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType

from expcore_onedata.rights import WRITE_RIGHTS

# The onedata-mcp commit this table was read at: `docs/inventory.md`, the OMCP row.
OMCP_PIN = "8c9b10c34efdad29498362fc4be4e785ff2a102b"
# What a tool that writes file content or metadata needs. It is no right a condition can declare: a tool-server user
# never holds it (`tokencheck` refuses a user that does), so the file-writing tools fail on every call.
DATA_WRITE = "space_write_data"

_VIEW_TRANSFERS, _VIEW_QOS = "space_view_transfers", "space_view_qos"
_REPLICATE, _MANAGE_QOS = "space_schedule_replication", "space_manage_qos"

# Tool name -> the privileges it needs beside the baseline (`space_view`, `space_read_data`), which every tool has.
# Recorded in the repository: replication needs its own right (`space_schedule_replication`, which the read-only
# slot's user lacks: the 12 Nov outage facts) and adding and removing a QoS requirement share `space_manage_qos`
# (`rights.MANAGE_QOS`, O33). The view rows are read from the privilege names, and the file-writing rows from the token
# check that bars `space_write_data`; neither was measured one tool at a time.
TOOL_NEEDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        # transfers
        "schedule_file_replication": (_REPLICATE,),
        "list_space_transfers": (_VIEW_TRANSFERS,),
        "get_transfer": (_VIEW_TRANSFERS,),
        # QoS
        "add_file_qos_requirement": (_MANAGE_QOS,),
        "remove_qos_requirement": (_MANAGE_QOS,),
        "get_file_qos_summary": (_VIEW_QOS,),
        "get_qos_requirement": (_VIEW_QOS,),
        # files that change
        "create_file": (DATA_WRITE,),
        "create_directory": (DATA_WRITE,),
        "delete_file": (DATA_WRITE,),
        "set_file_metadata": (DATA_WRITE,),
        "move_file": (DATA_WRITE,),
        # reads of files and spaces, and the server's harvester tools: the baseline alone
        "get_file_id": (),
        "get_file_attributes": (),
        "get_file_metadata": (),
        "get_file_distribution": (),
        "list_children": (),
        "list_files_recursively": (),
        "download_file": (),
        "grep_file_content": (),
        "query_by_metadata": (),
        "list_user_spaces": (),
        "list_marketplace_spaces": (),
        "list_space_providers": (),
        "list_user_harvesters": (),
        "get_harvester_index_schema": (),
        "query_harvester_index": (),
    }
)


def _names(items: Iterable[str]) -> str:
    return ", ".join(items)


def agreement_problem(allow: Sequence[str], rights: Sequence[str]) -> tuple[str, str] | None:
    """What is wrong between a condition's allow-list and its rights, as the key to name and the rest of a message
    about it, or None when they agree. Only names of the table and of `KNOWN_RIGHTS` appear in the message.

    A tool whose privilege the rights lack comes first, since the agent would see it fail. Then a write right no tool
    of the allow-list needs, unless the list holds a tool the table does not know."""
    granted = set(rights)
    missing: dict[str, list[str]] = {}
    for tool in allow:
        for right in TOOL_NEEDS.get(tool, ()):
            if right not in granted:
                missing.setdefault(right, []).append(tool)
    for right, tools in missing.items():
        if right == DATA_WRITE:
            return "allow", (
                f"holds {_names(tools)}, which need {right}: a tool-server user never holds it, so every call fails; "
                "remove them"
            )
        return "rights", f"lacks {right}, which {_names(tools)} of 'allow' need"
    if any(tool not in TOOL_NEEDS for tool in allow):
        return None
    used = {right for tool in allow for right in TOOL_NEEDS[tool]}
    unused = [right for right in rights if right in WRITE_RIGHTS and right not in used]
    if unused:
        return (
            "rights",
            f"holds {_names(unused)}, which no tool of 'allow' needs: the token would be wider than its tools",
        )
    return None
