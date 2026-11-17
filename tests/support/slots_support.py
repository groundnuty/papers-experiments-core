"""The slots and tool server a layout declares (O83): one layout text every test of them builds on, and the study's
artifacts directory the layout's `@study_artifacts@` names, made as a study ships it.

Every name here is invented. The commit is a stand-in of forty hex digits: nothing here runs a real tool server.
"""

import json
from pathlib import Path

from federation_support import MINIMAL

from expcore.tools.manifest import FrozenManifest, digest

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BASE = '"space_view", "space_read_data"'
READ = '"space_view", "space_read_data", "space_view_transfers", "space_view_qos"'
WRITE = '"space_view", "space_read_data", "space_view_transfers", "space_view_qos", "space_schedule_replication"'
READ_TOOLS = ["get_file_distribution", "list_space_providers", "get_transfer"]
WRITE_TOOLS = [*READ_TOOLS, "schedule_file_replication"]

SLOTS = f"""
[principals.slot1-data]
privileges = [{BASE}]

[principals.slot1-tools]
privileges = [{READ}]

[principals.slot2-data]
privileges = [{BASE}]

[principals.slot2-tools]
privileges = [{WRITE}]

[slots.slot-1]
data_path = "slot1-data"
toolserver = "slot1-tools"

[slots.slot-2]
data_path = "slot2-data"
toolserver = "slot2-tools"
"""

TOOLSERVER = f"""
[toolserver]
command = "@study_artifacts@/venv/bin/python"
args = ["-m", "onedata_mcp"]
clone = "@study_artifacts@/onedata-mcp"
commit = "{COMMIT}"
start_timeout_s = 90

[toolserver.conditions.knowledge]
allow = {json.dumps(READ_TOOLS)}
rights = [{READ}]
manifest = "@study_artifacts@/manifests/knowledge.json"

[toolserver.conditions.writing]
allow = {json.dumps(WRITE_TOOLS)}
rights = [{WRITE}]
manifest = "@study_artifacts@/manifests/writing.json"
"""

WITH_SLOTS = MINIMAL + SLOTS
WITH_TOOLSERVER = MINIMAL + SLOTS + TOOLSERVER
USERS = ("alpha-reader", "slot1-data", "slot1-tools", "slot2-data", "slot2-tools")  # the fixed user and the slots'

# a minimal platform template: the study space, support, credentials and tls, and nothing of slots or a tool server
TEMPLATE = """\
zone = { name = "zone", url = "@zone_url@" }
providers.oak = { url = "@provider_url.oak@", id = "@provider_id.oak@" }
providers.elm = { url = "@provider_url.elm@", id = "@provider_id.elm@" }
space = { name = "@space_name@", id = "@space_id.alpha-space@", prefixes = ["alpha-"] }
support = { providers = ["oak", "elm"], local = "oak" }
credentials.admin = { file = "secrets/admin.token" }
credentials.users = { directory = "secrets/users", pattern = "{user}.pass" }
principals.data_path = { username = "alpha-reader", user_id = "@user_id.alpha-reader@" }
tls = { ca_file = "ca/federation-ca.pem" }
"""


def layout_with_template(text: str, directory: Path) -> Path:
    """The layout `text` written into `directory` with the platform template above beside it and named by it."""
    (directory / "platform.toml.template").write_text(TEMPLATE, encoding="utf-8")
    path = directory / "layout.toml"
    path.write_text(
        text.replace(
            'study_space = "alpha-space"',
            'study_space = "alpha-space"\nplatform_template = "platform.toml.template"',
            1,
        ),
        encoding="utf-8",
    )
    return path


def frozen(tools: list[str]) -> str:
    """The text of a frozen manifest of the named tools, as `expcore.tools.manifest freeze` writes one."""
    listed = [
        {
            "name": n,
            "description": f"the {n} tool",
            "input_schema": {"type": "object"},
            "output_schema": None,
            "annotations": None,
        }
        for n in sorted(tools)
    ]
    return FrozenManifest(listed, digest(listed), "invented-server", "1.0", "2025-06-18").dump()


def artifacts(directory: Path) -> Path:
    """A study's artifacts directory as the layout names it: the interpreter, the clone and one frozen manifest per
    condition. The interpreter is a file of the right name, and the clone a directory; nothing of either runs."""
    (directory / "venv" / "bin").mkdir(parents=True)
    interpreter = directory / "venv" / "bin" / "python"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)
    (directory / "onedata-mcp").mkdir()
    (directory / "manifests").mkdir()
    (directory / "manifests" / "knowledge.json").write_text(frozen(READ_TOOLS), encoding="utf-8")
    (directory / "manifests" / "writing.json").write_text(frozen(WRITE_TOOLS), encoding="utf-8")
    return directory
