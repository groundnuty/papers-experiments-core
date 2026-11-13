"""A cell's hard deadline against a live federation (O34, #190), skipped unless every gate below is set.

    EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_FAULTS=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml \\
    EXPCORE_LIVE_SANDBOX_POOL=<a private IPv4 range the host routes nowhere> \\
    uv run pytest tests/live/test_cell_deadline.py

It starts containers (`EXPCORE_LIVE_DOCKER`), writes one file into the study space and deletes it again
(`EXPCORE_LIVE_MUTATE`), and takes a provider of the federation down through the fault injector and brings it back
(`EXPCORE_LIVE_FAULTS`), so never set them on a federation another run is using. `EXPCORE_LIVE_PLATFORM` is the
Compose federation's own platform.toml with a sanctioned `[faults]` table and `[principals.data_path]`; the test
writes a copy with a `[sandbox]` table of one mount level, as tests/live/test_mount_path.py does (`derive_platform`,
the same variables: `EXPCORE_LIVE_AGENT_IMAGE`, `EXPCORE_LIVE_ONECLIENT_IMAGE`, `EXPCORE_LIVE_FED_NETWORK`,
`EXPCORE_LIVE_ONECLIENT_ARGS`), with `kill_grace_s` set to `GRACE_S`. The provider taken down is
`EXPCORE_LIVE_FAULT_TARGET` (a label such as providers[0]) or the first of `[faults.services]` that is not the agent's
local one. `EXPCORE_LIVE_FACTS_OUT` names a file the facts are written to as JSON: outcomes and seconds, never a token,
an address, a name or a path.

The attempt runs once, in a fixture, as the outage rerun of 12 Nov found the defect: a file written through the target
alone (the local provider holds none of it) is seen at its size through the mount with every provider up, by a stat
that reads no byte; the target is stopped through the injector; a cell reads the file through the mount with a cap of
`CELL_S`. The read waits in the kernel, the cell is killed at its cap, outlives the kill, and the sidecar aborts the
mount's FUSE connection; the run is then over. The injector restores the target in `finally`, and the sandbox is
stopped and shown gone. Each test asserts one fact of it.
"""

import json
import os
import shlex
import time
import uuid
from pathlib import Path

import pytest

GATES = ("EXPCORE_LIVE_DOCKER", "EXPCORE_LIVE_MUTATE", "EXPCORE_LIVE_FAULTS")
if any(os.environ.get(gate) != "1" for gate in GATES) or not os.environ.get("EXPCORE_LIVE_PLATFORM"):
    pytest.skip(
        "EXPCORE_LIVE_DOCKER=1, EXPCORE_LIVE_MUTATE=1, EXPCORE_LIVE_FAULTS=1 and EXPCORE_LIVE_PLATFORM are needed: "
        "this test starts containers, writes into the study space and takes a provider of a live federation down",
        allow_module_level=True,
    )

from mount_live import (  # noqa: E402 -- only once the module is known to run
    AGENT_IMAGE,
    LEASE_KEY,
    ONECLIENT_IMAGE,
    SpaceFiles,
    derive_platform,
    federation_network,
    pattern,
    sandbox_table,
)

from expcore.errors import InfraError  # noqa: E402
from expcore_onedata import consistency  # noqa: E402
from expcore_onedata.client import OnezoneClient  # noqa: E402
from expcore_onedata.config import PlatformConfig, load_platform  # noqa: E402
from expcore_onedata.faults import FaultInjector, compose_driver, load_faults, provider_serves  # noqa: E402
from expcore_onedata.sandbox import deadline, hostops, kinds  # noqa: E402
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxError, SandboxSpec  # noqa: E402
from expcore_onedata.sandbox.hostops import DockerHost  # noqa: E402
from expcore_onedata.sandbox.sandbox import DockerSandbox  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec  # noqa: E402

PLATFORM = Path(os.environ["EXPCORE_LIVE_PLATFORM"])
CEILING_S = 1800.0
FILE_BYTES = 16384  # as the outage check's scratch files
CELL_S = 20.0  # the reading cell's cap: well inside the 60 s the 12 Nov read was still waiting at
GRACE_S = 5.0  # [sandbox] kill_grace_s of the derived platform
VISIBLE_S = 180.0
# the deadline's whole bound: the cap and the escalation's margin past it (deadline.margin_s: two graces and the
# commands' allowance). An abort that works ends the escalation about one grace after the cap, which leaves the rest of
# the margin for the docker client's return and the cell's record
HARD_BOUND_S = CELL_S + deadline.margin_s(GRACE_S)

STAT = """
import json, os, time
deadline, size = time.monotonic() + {wait_s!r}, None
while time.monotonic() < deadline:
    try:
        size = os.stat({path!r}).st_size
    except OSError:
        size = None
    if size == {size!r}:
        break
    time.sleep(2)
print(json.dumps({{"size": size}}))
"""
READ = """
with open({path!r}, "rb") as handle:
    print(len(handle.read()))
"""


def pinned(host: DockerHost, name: str) -> tuple[str, str]:
    """An image already on the host, by its name and the id it resolves to now; never pulled."""
    try:
        return name, host.image_id(name)
    except SandboxError:
        pytest.skip("an image the attempt needs is not on the host; it is never pulled here")


def platform_with_mount(base: Path) -> Path:
    """The federation's platform.toml, copied with a `[sandbox]` table of one mount level and the test's grace."""
    pool = os.environ.get("EXPCORE_LIVE_SANDBOX_POOL")
    if not pool:
        pytest.skip("EXPCORE_LIVE_SANDBOX_POOL is not set: the sandbox's networks need a range the host routes nowhere")
    facts_file = PLATFORM.parent / "facts.json"
    if not facts_file.is_file():
        pytest.skip("no facts.json beside the platform file: the local provider's domain on its network is read there")
    source = load_platform(PLATFORM, require_secrets=False)
    local = source.support.local
    facts = json.loads(facts_file.read_text(encoding="utf-8"))
    host = DockerHost()
    table = sandbox_table(
        work_root=base / "work",
        images={
            "agent": pinned(host, os.environ.get("EXPCORE_LIVE_AGENT_IMAGE") or AGENT_IMAGE),
            "oneclient": pinned(host, os.environ.get("EXPCORE_LIVE_ONECLIENT_IMAGE") or ONECLIENT_IMAGE),
        },
        pool=pool,
        network=os.environ.get("EXPCORE_LIVE_FED_NETWORK") or federation_network(facts),
        local=local,
        local_address=facts["providers"][local]["domain_on_project_network"],
        extra_args=shlex.split(os.environ.get("EXPCORE_LIVE_ONECLIENT_ARGS", "")),
    )
    return derive_platform(PLATFORM, base, {**table, "kill_grace_s": GRACE_S})


def target_of(config: PlatformConfig, services: dict) -> str:
    """The provider the injector takes down: `EXPCORE_LIVE_FAULT_TARGET`, or the first of `[faults.services]` that is
    not the agent's local provider; by its name, which never leaves this module."""
    wanted, local = os.environ.get("EXPCORE_LIVE_FAULT_TARGET"), config.support.local
    for name in services:
        if name != local and (wanted is None or config.provider_label(name) == wanted):
            return name
    pytest.skip("[faults.services] has no provider besides the local one to take down")


def mount_spec(config: PlatformConfig, run_key: str) -> SandboxSpec:
    """The spec of a mount run, its token minted for the fixed data-path user, as a live check does."""
    client = OnezoneClient(config)
    try:
        token = TokenIssuer(client, config).mint(
            data_path_spec(config.space.id, "oneclient"), config.principals.data_path, ceiling_s=CEILING_S
        )
    finally:
        client.close()
    return SandboxSpec(
        run_key=run_key,
        mode=DataPathMode.MOUNT,
        token=token,
        zone=Endpoint.from_url(config.zone.url),
        provider=Endpoint.from_url(config.local_provider.url),
        lease_key=LEASE_KEY,
    )


def json_line(sandbox: DockerSandbox, code: str, timeout_s: float) -> dict:
    result = sandbox.exec(code, timeout_s)
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"cell_failed": True, "exit_code": result.exit_code, "timed_out": result.timed_out}


def the_read_at_its_cap(sandbox: DockerSandbox, path: str) -> dict:
    """The reading cell during the outage, and what the deadline made of it; then whether a later cell still runs."""
    began = time.monotonic()
    result = sandbox.exec(READ.format(path=path), CELL_S)
    out: dict = {"exec_s": round(time.monotonic() - began, 3), "exit_code": result.exit_code}
    kill = result.kill or {}
    out["kill"] = {"reason": kill.get("reason"), "category": kill.get("category")}
    out["deadline"] = kill.get("deadline") or (sandbox.metadata().get("cell_deadlines") or [None])[-1]
    error = kinds.kill_error(result.kill)
    out["error_kind"] = error.kind if error is not None else None
    out["mount_aborted"] = (sandbox.metadata().get("mount") or {}).get("aborted")
    try:
        sandbox.exec("print(1)", 30)
        out["next_cell"] = "ran"
    except InfraError as refused:
        out["next_cell"] = refused.kind
    return out


def left_behind(host: DockerHost, sandbox: DockerSandbox) -> dict:
    """After the stop, from the host: whether the sandbox stopped, and how many FUSE mounts are left under its root."""
    under = str(sandbox.s.work_root).rstrip("/") + "/"
    table = hostops.parse_mountinfo(host.read_text("/proc/self/mountinfo"))
    return {
        "stopped": sandbox.stopped,
        "fuse_mounts_left": sum(m["mountpoint"].startswith(under) and m["fstype"].startswith("fuse") for m in table),
    }


@pytest.fixture(scope="module")
def attempt(tmp_path_factory):
    """The whole attempt, once (module docstring); the facts each test reads."""
    base = tmp_path_factory.mktemp("cell-deadline-live")
    config = load_platform(platform_with_mount(base))
    if config.principals.data_path is None:
        pytest.skip("[principals.data_path] is needed to mint the mount's token")
    # the injector reads the federation's own file: its journal and Compose file lie beside it, not beside the copy
    source = load_platform(PLATFORM)
    settings = load_faults(PLATFORM, source)
    local, target = config.support.local, target_of(source, dict(settings.services))
    host, client, files = DockerHost(sudo_command=config.sandbox.sudo), OnezoneClient(source), SpaceFiles(config)
    run = f"expcore-live-deadline-{uuid.uuid4().hex[:8]}"
    facts: dict = {"grace_s": config.sandbox.kill_grace_s, "cap_s": CELL_S, "target": source.provider_label(target)}
    sandbox = None
    try:
        top = files.make(local, files.root(local), run, "DIR")
        seen = consistency.converge(lambda: files.seen(target, top), bool, timeout_s=VISIBLE_S, poll_s=2.0)
        assert seen.converged, "the directory did not reach the target"
        owner_only = files.make(target, top, "owner-only.bin", "REG")  # the local provider never holds a byte of it
        files.write(target, owner_only, 0, pattern(f"{run}/owner-only.bin", FILE_BYTES))
        seen = consistency.converge(lambda: files.seen(local, owner_only), bool, timeout_s=VISIBLE_S, poll_s=2.0)
        assert seen.converged, "the file did not reach the local provider"
        sandbox = DockerSandbox.start(host, config.sandbox, mount_spec(config, run))
        path = f"{config.sandbox.mount.path}/{config.space.name}/{run}/owner-only.bin"
        facts["visible"] = json_line(sandbox, STAT.format(path=path, size=FILE_BYTES, wait_s=VISIBLE_S), VISIBLE_S + 30)
        assert facts["visible"].get("size") == FILE_BYTES, "the file never showed at its size through the mount"
        driver = compose_driver(settings, probe=provider_serves(source, settings, client))
        injector = FaultInjector(source, settings, source.provider_label(target), sanctioned=True, driver=driver)
        with injector.session({"run_key": run, "attempt": 1}):
            facts["during"] = the_read_at_its_cap(sandbox, path)
        facts["restored"] = injector.snapshot()["ok"]
    finally:
        if sandbox is not None:
            try:
                sandbox.stop()
            except SandboxError as error:
                facts["stop_error"] = type(error).__name__
            facts["after"] = left_behind(host, sandbox)
        facts["files_left"] = len(files.remove_all())
        client.close()
    if os.environ.get("EXPCORE_LIVE_FACTS_OUT"):
        Path(os.environ["EXPCORE_LIVE_FACTS_OUT"]).write_text(json.dumps(facts, indent=1, default=str), "utf-8")
    return facts


def test_o34_a_read_of_a_file_only_the_stopped_provider_holds_outlives_its_kill_at_the_cap(attempt):
    """O34: the read through the mount waits in the kernel past the cap: the deadline records it as killed, then
    survived, with the process in D state."""
    record = attempt["during"]["deadline"]
    steps = {s["step"]: s for s in record["steps"]}
    assert steps["kill"]["t_s"] == 0.0
    assert steps["survived"]["left"] >= 1
    assert "D" in steps["survived"]["states"]


def test_o34_the_sidecar_aborts_the_mount_and_the_read_then_ends(attempt):
    """O34: the abort from the sidecar (its SYS_ADMIN, fusectl in its own namespace) ends the waiting read: the
    outcome is `mount_aborted`, and no process of the cell is left. A `sandbox_stopped` here means the sidecar could
    not write the abort, and the stop did the rest."""
    record = attempt["during"]["deadline"]
    assert record["outcome"] == "mount_aborted", record["steps"]
    assert record["steps"][-1]["step"] == "ended"
    assert attempt["during"]["mount_aborted"] is True


def test_o34_the_cell_returns_within_its_hard_deadline(attempt):
    """O34: `exec` returns within the cap and the escalation's margin (two graces and the commands' allowance), not
    when the read would."""
    assert attempt["during"]["exec_s"] < HARD_BOUND_S


def test_o34_the_attempt_is_infrastructure_and_the_run_is_over(attempt):
    """O34: the cell is `kill_survived`, kind `cell_unkillable`, and the next cell is refused with that kind."""
    during = attempt["during"]
    assert during["kill"] == {"reason": "kill_survived", "category": "infrastructure"}
    assert during["error_kind"] == "cell_unkillable"
    assert during["next_cell"] == "cell_unkillable"


def test_o34_the_provider_is_restored_and_nothing_of_the_sandbox_is_left(attempt):
    """O34: the injector restored the target, the sandbox stopped with no FUSE mount left, and the file is gone."""
    assert attempt["restored"] is True
    assert attempt["after"] == {"stopped": True, "fuse_mounts_left": 0}
    assert attempt["files_left"] == 0
