"""Test support for the platform layer: a platform.toml writer with a good default, and a scripted tool-server command.

Every name here is a test name (`.test` hosts, `u-*` users), never a federation's. `write_platform` renders the default
document, lets the test edit the parsed dictionary, and writes it as TOML, so a test changes one key and sees one error.
"""

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

NOW = 1_800_000_000.0  # the issuer's clock in tests
CEILING_S = 600.0  # a run's whole-time ceiling in tests
ADMIN_ENV = "PLATFORM_TEST_ADMIN"
SPACE_NAME = "study-a-data"
SPACE_PREFIX = "study-a-"
BASELINE = ["space_view", "space_read_data"]
MANAGE = [*BASELINE, "space_manage_qos"]
SUPPORT_DIR = Path(__file__).resolve().parent
FAKE_TOOL_SERVER = str(SUPPORT_DIR / "fake_onedata_mcp.py")


def platform_data(tmp_path: Path) -> dict[str, Any]:
    """Return the default platform document as a dictionary; the manifests it names exist (empty unless frozen)."""
    manifests = {}
    for name in ("read", "manage"):
        manifests[name] = tmp_path / f"{name}.manifest.json"
        if not manifests[name].exists():  # a test that froze a real manifest first keeps it
            manifests[name].write_text("{}", encoding="utf-8")
    return {
        "zone": {"name": "zone", "url": "https://zone.test"},
        "providers": {"alpha": {"url": "https://alpha.test"}, "beta": {"url": "https://beta.test"}},
        "space": {"name": SPACE_NAME, "id": "spmock0001", "prefixes": [SPACE_PREFIX]},
        "support": {"providers": ["alpha"], "local": "alpha"},
        "credentials": {"admin": {"env": ADMIN_ENV}},
        "principals": {"data_path": "u-data", "toolserver": "u-tools", "service": "u-svc"},
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
