"""The slots and tool server a platform template declares (O83): a layout whose principals include the slots' four
users, the template that pairs them, and the study's artifacts directory the template's `@study_artifacts@` names, made
as a study ships it.

Every name here is invented. The commit is a stand-in of forty hex digits: nothing here runs a real tool server.
"""

import hashlib
import json
from pathlib import Path

from federation_support import MINIMAL

from expcore.tools.manifest import FrozenManifest, digest

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BASE = '"space_view", "space_read_data"'
READ = '"space_view", "space_read_data", "space_view_transfers", "space_view_qos"'
WRITE = '"space_view", "space_read_data", "space_view_transfers", "space_view_qos", "space_schedule_replication"'
BASE_SET = {"space_view", "space_read_data"}
READ_SET = {*BASE_SET, "space_view_transfers", "space_view_qos"}
WRITE_SET = {*READ_SET, "space_schedule_replication"}
READ_TOOLS = ["get_file_distribution", "list_space_providers", "get_transfer"]
WRITE_TOOLS = [*READ_TOOLS, "schedule_file_replication"]
SLOT_USERS = ("slot1-data", "slot1-tools", "slot2-data", "slot2-tools")

# the layout: the users are ordinary principals; nothing here says they are slots
PRINCIPALS = f"""
[principals.slot1-data]
privileges = [{BASE}]

[principals.slot1-tools]
privileges = [{READ}]

[principals.slot2-data]
privileges = [{BASE}]

[principals.slot2-tools]
privileges = [{WRITE}]
"""
LAYOUT = MINIMAL + PRINCIPALS
USERS = ("alpha-reader", *SLOT_USERS)  # the fixed user and the slots'

# a minimal platform template: the study space, support and credentials, and nothing of slots or a tool server
TEMPLATE = """\
zone = { name = "zone", url = "@zone_url@" }
providers.oak = { url = "@provider_url.oak@", id = "@provider_id.oak@" }
providers.elm = { url = "@provider_url.elm@", id = "@provider_id.elm@" }
space = { name = "@space_name@", id = "@space_id.alpha-space@", prefixes = ["alpha-"] }
support = { providers = ["oak", "elm"], local = "oak" }
credentials.admin = { file = "secrets/admin.token" }
credentials.users = { directory = "secrets/users", pattern = "{user}.pass" }
principals.data_path = { username = "alpha-reader", user_id = "@user_id.alpha-reader@" }
"""

SLOTS = f"""
[slots.slot-1.data_path]
username = "slot1-data"
user_id = "@user_id.slot1-data@"

[slots.slot-1.toolserver]
username = "slot1-tools"
user_id = "@user_id.slot1-tools@"
rights = [{READ}]

[slots.slot-2.data_path]
username = "slot2-data"
user_id = "@user_id.slot2-data@"

[slots.slot-2.toolserver]
username = "slot2-tools"
user_id = "@user_id.slot2-tools@"
rights = [{WRITE}]
"""

TOOLSERVER = f"""
[toolserver]
command = "@study_artifacts@/venv/bin/python"
args = ["-m", "onedata_mcp"]
start_timeout_s = 90
clone = "@study_artifacts@/onedata-mcp"
commit = "{COMMIT}"

[toolserver.conditions.knowledge]
allow = {json.dumps(READ_TOOLS)}
rights = [{READ}]
manifest = "@study_artifacts@/manifests/knowledge.json"

[toolserver.conditions.writing]
allow = {json.dumps(WRITE_TOOLS)}
rights = [{WRITE}]
manifest = "@study_artifacts@/manifests/writing.json"
"""

WITH_SLOTS = TEMPLATE + SLOTS
WITH_TOOLSERVER = TEMPLATE + SLOTS + TOOLSERVER


def layout_with_template(template: str, directory: Path, layout: str = LAYOUT) -> Path:
    """The layout text `layout` written into `directory` with `template` beside it and named by it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "platform.toml.template").write_text(template, encoding="utf-8")
    path = directory / "layout.toml"
    named = 'study_space = "alpha-space"\nplatform_template = "platform.toml.template"'
    path.write_text(layout.replace('study_space = "alpha-space"', named, 1), encoding="utf-8")
    return path


def frozen(tools: list[str]) -> str:
    """The text of a frozen manifest of the named tools, as `expcore.tools.manifest freeze` writes one."""
    listed = [
        {"name": n, "description": f"the {n} tool", "input_schema": {"type": "object"}, "output_schema": None,
         "annotations": None}
        for n in sorted(tools)
    ]  # fmt: skip
    return FrozenManifest(listed, digest(listed), "invented-server", "1.0", "2025-06-18").dump()


def artifacts(directory: Path) -> Path:
    """A study's artifacts directory as the template names it: the interpreter, the clone and one frozen manifest per
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


def secret_of(user: str) -> str:
    """The password a test gives a user: one nothing else in the root may hold."""
    return f"pw-{user}-" + hashlib.sha256(user.encode()).hexdigest()[:20]


def make_root(layout, *, passwords: bool = True) -> dict[str, str]:
    """The root's secrets as `up` leaves them once the users exist: the admin token and one password file per
    principal (each 0600, in 0700 directories). Returns each secret's value by its file's name, to scan the rest."""
    layout.secrets.mkdir(parents=True, exist_ok=True)
    layout.users.mkdir(parents=True, exist_ok=True)
    layout.secrets.chmod(0o700)
    layout.users.chmod(0o700)
    found = {"admin.token": "an-admin-token-" + hashlib.sha256(b"admin").hexdigest()[:20]}
    layout.admin_token.write_text(found["admin.token"], encoding="utf-8")
    layout.admin_token.chmod(0o600)
    if passwords:
        for principal in layout.spec.principals:
            path = layout.password(principal.name)
            path.write_text(secret_of(principal.name), encoding="utf-8")
            path.chmod(0o600)
            found[path.name] = secret_of(principal.name)
    return found


def ids_of(spec) -> dict:
    """The ids Onezone would have given: the facts' view of every user, provider and space."""
    return {
        "users": {p.name: f"uid-{p.name}" for p in spec.principals},
        "providers": {p.name: f"pid-{p.name}" for p in spec.providers},
        "spaces": {s.name: f"sid-{s.name}" for s in spec.spaces},
    }
