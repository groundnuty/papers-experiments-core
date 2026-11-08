"""Test support for the platform layer: a platform.toml writer with a good default, and a scripted tool-server command.

Every name here is a test name (`.test` hosts, `u-*` users), never a federation's. `write_platform` renders the default
document, lets the test edit the parsed dictionary, and writes it as TOML, so a test changes one key and sees one error.
"""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from expcore_onedata.credentials import Principal

NOW = 1_800_000_000.0  # the issuer's clock in tests
CEILING_S = 600.0  # a run's whole-time ceiling in tests
ADMIN_ENV = "PLATFORM_TEST_ADMIN"
SPACE_NAME = "study-a-data"
SPACE_PREFIX = "study-a-"
ALPHA_ID, BETA_ID = "prmock0001", "pr-beta"  # the ids the fake federation reports for the two providers
BASELINE = ["space_view", "space_read_data"]
MANAGE = [*BASELINE, "space_manage_qos"]
# The test users: login name -> (password, Onezone user id). The password files are written by `platform_data`.
USERS = {
    "login-data": ("pw-data-0001", "u-data"),
    "login-tools": ("pw-tools-0001", "u-tools"),
    "login-svc": ("pw-svc-0001", "u-svc"),
}
DATA, TOOLS, SVC = (Principal(name, USERS[name][1]) for name in ("login-data", "login-tools", "login-svc"))
CREDENTIALS_DIR = "credentials"
SUPPORT_DIR = Path(__file__).resolve().parent
FAKE_TOOL_SERVER = str(SUPPORT_DIR / "fake_onedata_mcp.py")


AGENT_IMAGE_ID = "sha256:" + "a" * 64
GATE_IMAGE_ID = "sha256:" + "b" * 64
ONECLIENT_IMAGE_ID = "sha256:" + "c" * 64
POOL = "10.77.0.0/24"
MOUNT_PATH = "/data"
LEVELS = {"mounted": "mount", "via-gate": "rest", "isolated": "none"}  # invented levels of a data_path factor


def sandbox_data() -> dict[str, Any]:
    """The default `[sandbox]` document: invented images and levels, a small pool, and two leasable users."""
    return {
        "work_root": "work",
        "name_prefix": "sbx",
        "label_namespace": "org.test",
        "agent_uid": 10001,
        "python": "python",
        "sudo": ["sudo", "-n"],
        "images": {
            "agent": {"name": "agent:1", "id": AGENT_IMAGE_ID},
            "gate": {"name": "gate:1", "id": GATE_IMAGE_ID},
            "oneclient": {"name": "mounter:1", "id": ONECLIENT_IMAGE_ID},
        },
        "network": {"pools": [POOL], "prefix": 28, "max_attempts": 64},
        "limits": {
            "cpus": 2.0,
            "memory_bytes": 1 << 30,
            "pids": 256,
            "nofile": 1024,
            "tmp_bytes": 1 << 26,
            "shm_bytes": 1 << 26,
            "work_disk_bytes": 1 << 28,
        },
        "mount": {"path": MOUNT_PATH, "tls": "verify"},
        "data_paths": dict(LEVELS),
        "users": {"login-data": "u-data", "login-data2": "u-data2"},
    }


def platform_data(tmp_path: Path) -> dict[str, Any]:
    """Return the default platform document as a dictionary; the manifests it names exist (empty unless frozen)."""
    for name in ("read", "manage"):
        manifest = tmp_path / f"{name}.manifest.json"
        if not manifest.exists():  # a test that froze a real manifest first keeps it
            manifest.write_text("{}", encoding="utf-8")
    directory = tmp_path / CREDENTIALS_DIR
    directory.mkdir(exist_ok=True)
    for login, (password, _) in USERS.items():
        file = directory / f"{login}.pass"
        if not file.exists():
            file.write_text(password + "\n", encoding="utf-8")
            os.chmod(file, 0o600)
    return platform_document(tmp_path)


def platform_document(tmp_path: Path) -> dict[str, Any]:
    """Return the default platform document without writing anything: the files it names are not created."""
    manifests = {name: tmp_path / f"{name}.manifest.json" for name in ("read", "manage")}
    return {
        "zone": {"name": "zone", "url": "https://zone.test"},
        "providers": {
            "alpha": {"url": "https://alpha.test", "id": ALPHA_ID},
            "beta": {"url": "https://beta.test", "id": BETA_ID},
        },
        "space": {"name": SPACE_NAME, "id": "spmock0001", "prefixes": [SPACE_PREFIX]},
        "support": {"providers": ["alpha"], "local": "alpha"},
        "credentials": {"admin": {"env": ADMIN_ENV}, "users": {"directory": CREDENTIALS_DIR}},
        "principals": {
            "data_path": {"username": DATA.username, "user_id": DATA.user_id},
            "toolserver": {"username": TOOLS.username, "user_id": TOOLS.user_id},
            "service": {"username": SVC.username, "user_id": SVC.user_id},
        },
        "sandbox": sandbox_data(),
        "toolserver": {
            "command": sys.executable,
            "args": [FAKE_TOOL_SERVER],
            "start_timeout_s": 30,
            "conditions": {
                "read": {"allow": ["echo", "add"], "rights": BASELINE, "manifest": manifests["read"].name},
                "manage": {"allow": ["echo", "add", "fail"], "rights": MANAGE, "manifest": manifests["manage"].name},
            },
        },
    }


def _value(value: Any) -> str:
    """Render a scalar or a list of scalars as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    return json.dumps(value)


def render(data: dict[str, Any]) -> str:
    """Render a nested dictionary of tables, scalars and lists as TOML."""
    lines: list[str] = []

    def emit(prefix: str, table: dict[str, Any]) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        if prefix and (scalars or not any(isinstance(v, dict) for v in table.values())):
            lines.append(f"[{prefix}]")
        lines.extend(f"{k} = {_value(v)}" for k, v in scalars.items())
        for key, sub in table.items():
            if isinstance(sub, dict):
                emit(f"{prefix}.{key}" if prefix else key, sub)

    emit("", data)
    return "\n".join(lines) + "\n"


def write_platform(tmp_path: Path, edit: Callable[[dict[str, Any]], None] | None = None) -> Path:
    """Write platform.toml into tmp_path after letting `edit` change the default document; return its path."""
    data = platform_data(tmp_path)
    if edit is not None:
        edit(data)
    path = tmp_path / "platform.toml"
    path.write_text(render(data), encoding="utf-8")
    return path


def configured_values(tmp_path: Path) -> list[str]:
    """The values of the default document that a message must never hold (O12), as they would be written in one.

    Hosts, URLs, names, ids, users and the paths of the credential directory and the manifests. Left out are the words
    the layer uses for its own roles (the zone's label, the key names, the privilege vocabulary) and the names of the
    conditions, which are ordinary words such as "read" and are looked for quoted, as `{name!r}` would write them.
    """
    document = platform_document(tmp_path)
    values: list[str] = []
    for name, provider in document["providers"].items():
        values += [name, provider["url"], provider["url"].split("//")[1], provider["id"]]
    zone, space = document["zone"], document["space"]
    values += [zone["url"], zone["url"].split("//")[1], space["name"], space["id"], *space["prefixes"]]
    for principal in document["principals"].values():
        values += [principal["username"], principal["user_id"]]
    credentials = document["credentials"]
    values += [credentials["admin"]["env"], str((tmp_path / credentials["users"]["directory"]).resolve())]
    values += [str(tmp_path / credentials["users"]["directory"])]
    server = document["toolserver"]
    values += [server["command"], *server["args"]]
    for name, condition in server["conditions"].items():
        manifest = tmp_path / condition["manifest"]
        values += [str(manifest), str(manifest.resolve()), condition["manifest"], repr(name), f'"{name}"']
    return values


def leaked_values(message: str, tmp_path: Path) -> list[str]:
    """Return the configured values a message holds. The path of platform.toml itself is where the operator pointed
    the layer, not a value in it, so it is taken out first."""
    for config_file in {str(tmp_path / "platform.toml"), str((tmp_path / "platform.toml").resolve())}:
        message = message.replace(config_file, "platform.toml")
    return sorted({value for value in configured_values(tmp_path) if value.lower() in message.lower()})
