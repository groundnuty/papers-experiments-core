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
CHECK_FILE = "checks/content.bin"  # an invented file under the space root, read by the pre-campaign check
SLOT_USERS = {  # login name -> (password, user id) of the users of the two slots below; all invented
    "slot-a-data": ("pw-slot-a-data", "u-slot-a-data"),
    "slot-a-tools": ("pw-slot-a-tools", "u-slot-a-tools"),
    "slot-b-data": ("pw-slot-b-data", "u-slot-b-data"),
    "slot-b-tools": ("pw-slot-b-tools", "u-slot-b-tools"),
}
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


def slots_data() -> dict[str, Any]:
    """The default `[slots]` document: two slots, each a lease key with a data-path user and a tool-server user."""
    return {
        name: {
            "data_path": {"username": f"{name}-data", "user_id": SLOT_USERS[f"{name}-data"][1]},
            "toolserver": {"username": f"{name}-tools", "user_id": SLOT_USERS[f"{name}-tools"][1]},
        }
        for name in ("slot-a", "slot-b")
    }


def sandbox_data() -> dict[str, Any]:
    """The default `[sandbox]` document: invented images and levels, a small pool and the check file."""
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
        "content_check_file": CHECK_FILE,
    }


def add_sandbox(data: dict[str, Any]) -> None:
    """An `edit` for `write_platform`: put the default `[sandbox]` and `[slots]` tables in the document."""
    data["sandbox"] = sandbox_data()
    data["slots"] = slots_data()


def configured_sandbox_values(tmp_path: Path) -> list[str]:
    """The values of the default `[sandbox]` table that a message must never hold (O12), as the file writes them.

    Image names and ids, the pool, the work directory, the mount path, the name prefix and label namespace, the
    slots' names, logins and user ids, and the study's levels (looked for quoted, as `{level!r}` would write them).
    """
    document = sandbox_data()
    values = [ref[key] for ref in document["images"].values() for key in ("name", "id")]
    values += document["network"]["pools"] + [document["mount"]["path"], document["label_namespace"]]
    values += [document["name_prefix"] + "-", str(tmp_path / document["work_root"]), str((tmp_path / "work").resolve())]
    values += [*slots_data(), *SLOT_USERS, *(user_id for _, user_id in SLOT_USERS.values())]
    values += [repr(level) for level in document["data_paths"]]
    values += [document["content_check_file"]]
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
