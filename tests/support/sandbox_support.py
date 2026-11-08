"""Helpers for the sandbox tests: the `[sandbox]` document, specs for each data path, and options for a fast start.

Every name here is invented. `add_sandbox` is an `edit` for `platform_support.write_platform`, so the sandbox table is
added to the platform document without that module knowing it.
"""

from pathlib import Path
from typing import Any

from expcore_onedata.sandbox import teardown
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxSpec
from expcore_onedata.tokens import Secret

TOKEN = "tok-test-0123456789"  # an invented token value
AGENT_IMAGE_ID = "sha256:" + "a" * 64
GATE_IMAGE_ID = "sha256:" + "b" * 64
ONECLIENT_IMAGE_ID = "sha256:" + "c" * 64
POOL = "10.77.0.0/24"
MOUNT_PATH = "/data"
LEVELS = {"mounted": "mount", "via-gate": "rest", "isolated": "none"}  # invented levels of a data_path factor
FAST = teardown.Waits(
    reader_term_s=0.05,
    reader_kill_s=0.05,
    after_abort_s=0.02,
    unmount_s=0.05,
    sidecar_stop_s=1,
    command_s=1.0,
    probe_s=1.0,
    poll_s=0.01,
)


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


def add_sandbox(data: dict[str, Any]) -> None:
    """An `edit` for `write_platform`: put the default `[sandbox]` table in the document."""
    data["sandbox"] = sandbox_data()


def configured_sandbox_values(tmp_path: Path) -> list[str]:
    """The values of the default `[sandbox]` table that a message must never hold (O12), as the file writes them.

    Image names and ids, the pool, the work directory, the mount path, the name prefix and label namespace, the
    logins and user ids, and the study's levels (looked for quoted, as `{level!r}` would write them).
    """
    document = sandbox_data()
    values = [ref[key] for ref in document["images"].values() for key in ("name", "id")]
    values += document["network"]["pools"] + [document["mount"]["path"], document["label_namespace"]]
    values += [document["name_prefix"] + "-", str(tmp_path / document["work_root"]), str((tmp_path / "work").resolve())]
    values += [*document["users"], *document["users"].values()]
    values += [repr(level) for level in document["data_paths"]]
    return values


def make_spec(world, level="via-gate", *, token=TOKEN, lease_key=None, run_key="run-key-1") -> SandboxSpec:
    """The spec of a run whose study level maps to a data path."""
    mode = world.s.mode_of(level)
    none = mode is DataPathMode.NONE
    return SandboxSpec(
        run_key=run_key,
        mode=mode,
        token=None if none else Secret(token),
        zone=None if none else Endpoint.from_url(world.config.zone.url),
        provider=None if none else Endpoint.from_url(world.config.local_provider.url),
        lease_key=lease_key,
    )


def start_options(world) -> dict:
    """No sampler thread, no real sleeping, short teardown waits, and the test's own registry."""
    return {"registry": world.registry, "waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None}
