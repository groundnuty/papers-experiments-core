"""Helpers for the sandbox tests: the `[sandbox]` document, specs for each data path, and options for a fast start.

Every name here is invented. `add_sandbox` is an `edit` for `platform_support.write_platform`, so the sandbox table is
added to the platform document without that module knowing it.
"""

import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox import teardown
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxSpec
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.tokens import Secret

TOKEN = "tok-test-0123456789"  # an invented token value
AGENT_IMAGE_ID = "sha256:" + "a" * 64
GATE_IMAGE_ID = "sha256:" + "b" * 64
ONECLIENT_IMAGE_ID = "sha256:" + "c" * 64
POOL = "10.77.0.0/24"
MOUNT_PATH = "/data"
LEVELS = {"mounted": "mount", "via-gate": "rest", "isolated": "none"}  # invented levels of a data_path factor
COMPLETE_LEVEL = "via-gate-whole"  # an invented REST level whose gate answers every listing complete (O21)
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
        "mount": {"path": MOUNT_PATH},
        "data_paths": {**LEVELS, COMPLETE_LEVEL: {"kind": "rest", "listing": "complete"}},
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
        listing=world.s.listing_of(level) or "paged",
    )


def start_options(world) -> dict:
    """No sampler thread, no real sleeping, short teardown waits, and the test's own registry."""
    return {"registry": world.registry, "waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None}


def is_listing(argv: Sequence[str]) -> bool:
    """Whether a host command lists what a run left: the copy helper's `list`, or `find` as earlier versions listed."""
    return argv[0] == "find" or (argv[0] == sys.executable and argv[-1] == "list")


def is_copy(argv: Sequence[str]) -> bool:
    """Whether a host command copies what a run left: the copy helper's `copy`, or `cp` as earlier versions copied."""
    return argv[0] == "cp" or (argv[0] == sys.executable and argv[-1] == "copy")


def around_run(host, wrapper: Callable[..., CmdResult]) -> Callable[[], None]:
    """Put `wrapper(original, argv, timeout_s, sudo=, input=)` around the host's `run`; return what undoes it."""
    original = host.run

    def run(argv, timeout_s, *, sudo=False, input=None):
        return wrapper(original, list(argv), timeout_s, sudo=sudo, input=input)

    host.run = run
    return lambda: setattr(host, "run", original)


def after_first(host, matches: Callable[[Sequence[str]], bool], action: Callable[[], None]) -> None:
    """Run `action` once, right after the first host command that `matches` has returned."""
    done: list[bool] = []

    def wrapper(original, argv, timeout_s, **kwargs):
        result = original(argv, timeout_s, **kwargs)
        if not done and matches(argv):
            done.append(True)
            action()
        return result

    around_run(host, wrapper)


def fail_copies(host, stderr: str = "disk full") -> Callable[[], None]:
    """Make every copy out of the sandbox fail as a full disk makes it fail; return what undoes it."""

    def wrapper(original, argv, timeout_s, **kwargs):
        return CmdResult(1, "", stderr) if is_copy(argv) else original(argv, timeout_s, **kwargs)

    return around_run(host, wrapper)


# The agent's own use of the published client (O23), as a study's agent uses it: nothing but the environment the
# sandbox gives it, the placeholder token among it, and the trust it sets up by default. It lists the directory of
# `{space}/{path}` and reads that file through `onedata:///` URLs (the client drops the first name after `onedata://`)
# and prints what it saw; an image without the client prints `client: None`. Shared by the live test and the in-process
# one, so the code the live test sends has run.
PUBLISHED_CLIENT = """
import hashlib, json, os, posixpath
try:
    import fsspec, onedatarestfsspec
except ImportError:
    print(json.dumps({{"client": None}}))
    raise SystemExit
target = {space!r} + "/" + {path!r}
fs, root = fsspec.core.url_to_fs("onedata:///" + target)
listing = sorted(fs.ls(posixpath.dirname(root), detail=False))
with fsspec.open("onedata:///" + target, "rb") as handle:
    data = handle.read()
print(json.dumps({{"client": onedatarestfsspec.__version__, "token": os.environ.get("ONEDATA_TOKEN"),
                  "listing": listing, "size": fs.size(root), "read": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}}))
"""
