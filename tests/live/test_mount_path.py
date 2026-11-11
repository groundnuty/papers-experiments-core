"""One sandboxed attempt on the mount data path against a live federation, skipped unless every gate below is set.

    EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml \\
    EXPCORE_LIVE_SANDBOX_POOL=<a private IPv4 range the host routes nowhere> \\
    uv run pytest tests/live/test_mount_path.py

It starts containers (`EXPCORE_LIVE_DOCKER`), and it writes five files into the study space and deletes them again
(`EXPCORE_LIVE_MUTATE`). `EXPCORE_LIVE_PLATFORM` is the Compose federation's own platform.toml
(deploy/compose-federation, with `facts.json` beside it). The test writes a copy with a `[sandbox]` table into a
directory of its own (`mount_live.derive_platform`): images pinned to the ids `EXPCORE_LIVE_AGENT_IMAGE` and
`EXPCORE_LIVE_ONECLIENT_IMAGE` resolve to now (defaults `mount_live.AGENT_IMAGE` and `ONECLIENT_IMAGE`; nothing is
pulled), the pool from `EXPCORE_LIVE_SANDBOX_POOL`, its work directory under pytest's temporary directory, one mount
level with `EXPCORE_LIVE_ONECLIENT_ARGS` (split as a shell would) as oneclient's further arguments, and
`[sandbox.federation]`: the oneclient sidecar on the federation's network (`EXPCORE_LIVE_FED_NETWORK`, default the
layout's project's network, from facts.json), dialling the local provider by its domain there.
`EXPCORE_LIVE_FACTS_OUT` names a file the attempt's facts are written to as JSON: outcomes, sizes and seconds, never a
token, an address or a path.

The attempt runs once, in a fixture, and each test asserts one thing it showed:
1. launch: the sidecar alone has /dev/fuse and SYS_ADMIN, and it alone is on the federation's network;
2. confinement: the agent sees the mount read-only and only the study space, and reaches the federation by no other way;
3. reads: a file that only a provider other than the local one holds reads whole through the mount, its bytes the
   file's, and the probes attribute the bytes the local provider fetched on demand to that owner;
4. unwritten ranges: files at their final size with only a prefix written, through the local provider and through the
   owner, read through the mount; what each read returned, and how long it took, is recorded, never assumed;
5. teardown: unmount, then the sidecar, then the agent; nothing of the sandbox left on the host; the federation's
   network as it was;
6. accounting: whether the sidecar can account for what the agent read, per file. The agent stats one file it never
   reads and reads one range of another twice; before the stop, oneclient's options that may record reads, its whole
   log directory (copied under the test's temporary directory, the token replaced) with the lines naming each file,
   and the entries of the mount's FUSE connection on the host are captured. The test asserts only that the evidence
   was captured; the verdict, with its method and a measured example or the reason it cannot, goes into the day-0
   facts. A first run finds oneclient's options; a second, with `EXPCORE_LIVE_ONECLIENT_ARGS`, can switch one on.
"""

import dataclasses
import hashlib
import json
import os
import re
import shlex
import time
import uuid
from pathlib import Path

import pytest

GATES = ("EXPCORE_LIVE_DOCKER", "EXPCORE_LIVE_MUTATE")
if any(os.environ.get(gate) != "1" for gate in GATES) or not os.environ.get("EXPCORE_LIVE_PLATFORM"):
    pytest.skip(
        "EXPCORE_LIVE_DOCKER=1, EXPCORE_LIVE_MUTATE=1 and EXPCORE_LIVE_PLATFORM are needed: this test starts "
        "containers and writes into the study space of a live federation",
        allow_module_level=True,
    )

from mount_live import (  # noqa: E402 -- only once the module is known to run
    AGENT_IMAGE,
    LEASE_KEY,
    ONECLIENT_IMAGE,
    SpaceFiles,
    accounting_cell,
    candidate_options,
    confinement_cell,
    derive_platform,
    federation_network,
    file_guid,
    mentions,
    pattern,
    read_cell,
    reader_cell,
    reads_cell,
    sandbox_table,
    unwritten_cell,
    unwritten_cell_timeout,
)

from expcore_onedata import consistency  # noqa: E402
from expcore_onedata.client import OnezoneClient  # noqa: E402
from expcore_onedata.config import PlatformConfig, load_platform  # noqa: E402
from expcore_onedata.placement import held_bytes, merged, uncovered  # noqa: E402
from expcore_onedata.probes import PlatformProbes  # noqa: E402
from expcore_onedata.provision import Provisioner, resolve_provider_ids  # noqa: E402
from expcore_onedata.rangemap import load_placement  # noqa: E402
from expcore_onedata.sandbox import hostops, teardown  # noqa: E402
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxError, SandboxSpec  # noqa: E402
from expcore_onedata.sandbox.containers import ONECLIENT_BIN, effective_log_dir, sidecar_command  # noqa: E402
from expcore_onedata.sandbox.hostops import DockerHost  # noqa: E402
from expcore_onedata.sandbox.provider import SandboxProbe  # noqa: E402
from expcore_onedata.sandbox.sandbox import MOUNT_RECORDS, TOKEN_FILE, DockerSandbox  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec  # noqa: E402

PLATFORM = Path(os.environ["EXPCORE_LIVE_PLATFORM"])
CEILING_S = 1800.0
WHOLE_SIZE = (4 << 20) + 123  # not a multiple of any block size
PREFIX, PARTIAL_SIZE = 64 << 10, 1 << 20  # as the REST measurement of the day-0 facts, so the two compare
READ_DEADLINE_S = 30.0  # a read through the mount that has not returned by then is recorded as blocked
VISIBLE_TIMEOUT_S = 180.0
REPEAT_SIZE, UNTOUCHED_SIZE = 1 << 20, 64 << 10  # the accounting check's two files
REPEAT_OFFSET, REPEAT_LENGTH = (512 << 10) + 7, 4096  # the one range of repeat.bin the agent reads, twice
LOG_FILE_LIMIT = 32 << 20  # a sidecar log file larger than this is listed and not copied
MIB = 1 << 20
GRANULAR_SIZES = {"8m": 8 * MIB, "64m": 64 * MIB}  # files only the owner holds, read through the mount by range
# (label, file, reads as (offset, length), read in order by one descriptor); None: computed from the block map then
GRANULARITY = (
    ("1 B at 0", "8m", [(0, 1)], False),
    ("1 B at 3 MiB + 5", "8m", [(3 * MIB + 5, 1)], False),
    ("4 KiB at 5 MiB + 100", "8m", [(5 * MIB + 100, 4096)], False),
    ("1 MiB at 6 MiB + 7", "8m", [(6 * MIB + 7, MIB)], False),
    ("1 MiB from 4 KiB before the end of a held block", "8m", None, False),
    ("1 B at 10 MiB + 3", "64m", [(10 * MIB + 3, 1)], False),
    ("4 KiB at 20 MiB", "64m", [(20 * MIB, 4096)], False),
    ("1 MiB at 30 MiB + 12345", "64m", [(30 * MIB + 12345, MIB)], False),
    ("5 MiB at 40 MiB", "64m", [(40 * MIB, 5 * MIB)], False),
    ("three 1 MiB reads in order from 50 MiB", "64m", [(50 * MIB, MIB), (51 * MIB, MIB), (52 * MIB, MIB)], True),
)
SETTLE_TIMEOUT_S, SETTLE_POLL_S = 60.0, 2.0  # a block map is settled when two reads this far apart agree


def pinned(host: DockerHost, name: str) -> tuple[str, str]:
    """An image already on the host, by its name and the id it resolves to now; never pulled."""
    try:
        return name, host.image_id(name)
    except SandboxError:
        pytest.skip("an image the attempt needs is not on the host; it is never pulled here")


def platform_for_mount(base: Path) -> Path:
    """The federation's platform.toml, copied with a `[sandbox]` table of one mount level (module docstring)."""
    pool = os.environ.get("EXPCORE_LIVE_SANDBOX_POOL")
    if not pool:
        pytest.skip(
            "EXPCORE_LIVE_SANDBOX_POOL is not set: the sandbox's own networks need a range the host routes nowhere"
        )
    facts_file = PLATFORM.parent / "facts.json"
    if not facts_file.is_file():
        pytest.skip("no facts.json beside the platform file: the local provider's domain on its network is read there")
    source = load_platform(PLATFORM, require_secrets=False)
    local = source.support.local
    facts = json.loads(facts_file.read_text(encoding="utf-8"))
    host = DockerHost()
    images = {
        "agent": pinned(host, os.environ.get("EXPCORE_LIVE_AGENT_IMAGE") or AGENT_IMAGE),
        "oneclient": pinned(host, os.environ.get("EXPCORE_LIVE_ONECLIENT_IMAGE") or ONECLIENT_IMAGE),
    }
    table = sandbox_table(
        work_root=base / "work",
        images=images,
        pool=pool,
        network=os.environ.get("EXPCORE_LIVE_FED_NETWORK") or federation_network(facts),
        local=local,
        local_address=facts["providers"][local]["domain_on_project_network"],
        extra_args=shlex.split(os.environ.get("EXPCORE_LIVE_ONECLIENT_ARGS", "")),
    )
    return derive_platform(PLATFORM, base, table)


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


def network_containers(host: DockerHost, network: str) -> dict[str, str]:
    """The containers attached to a network: id -> its IPv4 address there (never printed)."""
    found = host.docker("network", "inspect", network, "--format", "{{json .Containers}}", check=False, timeout=30)
    if found.returncode != 0:
        return {}
    return {cid: str(c.get("IPv4Address", "")).split("/")[0] for cid, c in (json.loads(found.stdout) or {}).items()}


def launch_facts(host: DockerHost, sandbox: DockerSandbox, network: str, secret: str) -> dict:
    """From the host: which container has FUSE and SYS_ADMIN, which networks each is on, and whether `docker inspect`
    of it shows the run's token (`secret`, only ever compared), by role."""
    out = {}
    for role, name in (("sidecar", sandbox.names.sidecar), ("agent", sandbox.names.agent)):
        shown = host.docker("inspect", name, check=False, timeout=30)
        info = hostops.inspect_from(shown) or {}
        config = info.get("HostConfig") or {}
        networks = set((info.get("NetworkSettings") or {}).get("Networks") or {})
        out[role] = {
            "dev_fuse": any(d.get("PathOnHost") == "/dev/fuse" for d in config.get("Devices") or []),
            # docker names it CAP_SYS_ADMIN in a container's configuration, whichever form `--cap-add` was given
            "sys_admin": bool({"SYS_ADMIN", "CAP_SYS_ADMIN"} & set(config.get("CapAdd") or [])),
            "cap_drop_all": (config.get("CapDrop") or []) == ["ALL"],
            "privileged": bool(config.get("Privileged")),
            "read_only_root": bool(config.get("ReadonlyRootfs")),
            "inspected": shown.returncode == 0,
            "token_in_inspect": secret in shown.stdout,
            "networks": sorted(
                "federation" if n == network else "sandbox" if n == sandbox.names.net else "other" for n in networks
            ),
        }
    meta = sandbox.metadata()
    out["mount"] = {k: meta["mount"].get(k) for k in ("network", "io_mode", "wait_s")}
    out["read_only_verified"] = meta["read_only_verified"]
    out["tls_mode"] = meta["tls"]["mode"]
    return out


def cell(sandbox: DockerSandbox, code: str, timeout_s: float) -> dict:
    """Run one cell and return its JSON line, or what went wrong with it, by outcome only."""
    result = sandbox.exec(code, timeout_s)
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"cell_failed": True, "exit_code": result.exit_code, "timed_out": result.timed_out}


def teardown_facts(host: DockerHost, sandbox: DockerSandbox, config: PlatformConfig) -> dict:
    """After the stop, from the host: the order of the teardown's steps, and what of the sandbox is left anywhere."""
    s = config.sandbox
    label = f"label={s.label('lease')}={LEASE_KEY}"
    containers = host.docker("ps", "-a", "-q", "--filter", label, check=False, timeout=30)
    networks = host.docker("network", "ls", "-q", "--filter", label, check=False, timeout=30)
    table = hostops.parse_mountinfo(host.read_text("/proc/self/mountinfo"))
    under = str(s.work_root).rstrip("/") + "/"
    steps = [
        (r["step"], r.get("outcome") or r.get("action"))
        for r in sandbox.teardown_log
        if r["step"] in ("unmount", "sidecar", "agent", "end")
    ]
    end = next((r for r in sandbox.teardown_log if r["step"] == "end"), {})
    return {
        "stopped": sandbox.stopped,
        "steps": steps,
        "fuse_safe": end.get("fuse_safe"),
        "containers_left": len(containers.stdout.split()) if containers.returncode == 0 else None,
        "networks_left": len(networks.stdout.split()) if networks.returncode == 0 else None,
        "mounts_left": sum(m["mountpoint"].startswith(under) for m in table),
        "fuse_mounts_left": sum(m["mountpoint"].startswith(under) and m["fstype"].startswith("fuse") for m in table),
        "run_directory_left": sandbox.dir.exists(),
        "mount_record_left": (s.work_root / MOUNT_RECORDS / f"{sandbox.sid}.json").exists(),
        "probe_gone": SandboxProbe(host, s).resource_gone(LEASE_KEY, {}) is True,
    }


def ground_truth() -> dict:
    """What the agent and the harness did to each fixture file through the mount, for holding the sidecar's record
    against: the agent's reads per file, and the touches that are not the agent's reads."""
    return {
        "whole": {"agent": [{"op": "read", "offset": 0, "length": WHOLE_SIZE, "times": 1}]},
        **{
            label: {
                "agent": [
                    {"op": "stat"},
                    {"op": "read", "offset": PREFIX, "length": 4096, "times": 1},
                    {"op": "read", "offset": PREFIX - 16, "length": 32, "times": 1},
                    {"op": "read", "offset": 0, "length": PARTIAL_SIZE, "times": 1},
                ]
            }
            for label in ("written_through_local", "written_through_owner")
        },
        "repeat": {"agent": [{"op": "read", "offset": REPEAT_OFFSET, "length": REPEAT_LENGTH, "times": 2}]},
        "untouched": {"agent": [{"op": "stat"}]},
        **{
            f"gran-{which}": {
                "agent": [
                    {"op": "read", "step": label, "reads": [list(r) for r in reads] if reads else "computed"}
                    for label, of, reads, _ in GRANULARITY
                    if of == which
                ]
                + (
                    [{"op": "read", "step": "the freeze check's reader: 1 MiB at random offsets, until killed"}]
                    if which == "64m"
                    else []
                )
            }
            for which in GRANULAR_SIZES
        },
        "not_the_agents_reads": [
            "the start's write probe at the mount root, refused",
            "the confinement cell's listing of the mount root and its write probe in the space, refused",
            "each path's lookups on the way to a file",
        ],
    }


def sidecar_accounting(host: DockerHost, sandbox: DockerSandbox, spec: SandboxSpec, base: Path, forms: dict) -> dict:
    """What the sidecar itself shows of the agent's reads, read before the stop: oneclient's options that may record
    reads, every file of its log directory (copied, the token replaced, under `base/sidecar-evidence`, never into the
    facts) with the lines that name each fixture file by its REST id, its uuid or its name, and the entries of the
    mount's FUSE connection on the host. Counts and names only: the verdict is written from them after the run."""
    secret, sidecar = spec.token.reveal(), sandbox.names.sidecar
    keep = base / "sidecar-evidence"
    keep.mkdir(mode=0o700)

    def save(name: str, text: str) -> str:
        clean = text.replace(secret, "<token>")
        path = keep / re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        path.write_text(clean, encoding="utf-8")
        path.chmod(0o600)
        return clean

    shown = host.docker("exec", sidecar, ONECLIENT_BIN, "--help", check=False, timeout=60)
    help_text = save("oneclient-help.txt", shown.stdout + shown.stderr)
    out: dict = {
        "help_exit_code": shown.returncode,
        "help_bytes": len(help_text),
        "candidate_options": candidate_options(help_text),
        "oneclient_args": list(sandbox.s.mount.extra_args),
    }
    log_dir = effective_log_dir(sidecar_command(spec.provider.host, sandbox.s))
    listed = host.docker("exec", sidecar, "find", log_dir, "-type", "f", "-printf", "%s %p\\n", check=False, timeout=60)
    found = [(int(size), path) for size, _, path in (line.partition(" ") for line in listed.stdout.splitlines())]
    out["log_files"] = [{"name": os.path.relpath(path, log_dir), "bytes": size} for size, path in found]
    texts = []
    for size, path in found:
        if size <= LOG_FILE_LIMIT:
            copied = host.docker("exec", sidecar, "cat", path, check=False, timeout=120)
            texts.append(save(os.path.relpath(path, log_dir), copied.stdout))
    out["mentions"] = mentions("\n".join(texts), forms)
    out["uuid_decoded"] = {label: f["uuid"] is not None for label, f in forms.items()}
    share = teardown.host_path(sandbox.dir / "mount")
    devices = [
        m["dev"] for m in hostops.fuse_mounts(host.read_text("/proc/self/mountinfo")) if m["mountpoint"] == share
    ]
    out["fuse_connection_entries"] = None
    if devices:
        listing = host.run(["ls", f"/sys/fs/fuse/connections/{devices[-1].partition(':')[2]}"], 20, sudo=True)
        out["fuse_connection_entries"] = sorted(listing.stdout.split()) if listing.returncode == 0 else None
    return out


def settled_blocks(view: Provisioner, file_id: str, provider: str) -> tuple[list, float, bool]:
    """A provider's block map of a file once two reads `SETTLE_POLL_S` apart agree (oneclient may go on fetching after
    the agent's read returned); the map, the seconds it took, and whether it settled within `SETTLE_TIMEOUT_S`."""
    began, last = time.monotonic(), None
    while True:
        now = merged(view.blocks(file_id, provider))
        if now == last:
            return now, round(time.monotonic() - began, 1), True
        if time.monotonic() - began > SETTLE_TIMEOUT_S:
            return now, round(time.monotonic() - began, 1), False
        last = now
        time.sleep(SETTLE_POLL_S)


def inside_held(blocks: list, size: int) -> list[tuple[int, int]]:
    """A 1 MiB read that starts 4 KiB before the end of the last held block that leaves room for it, so it begins in
    bytes the provider holds and runs past them; at the file's start when nothing is held."""
    for offset, length in reversed(blocks):
        start = offset + length - 4096
        if start >= 0 and start + MIB <= size:
            return [(start, MIB)]
    return [(0, MIB)]


def granularity(sandbox: DockerSandbox, view: Provisioner, local: str, inside: str, ids: dict) -> list[dict]:
    """What the local provider's block map gains for each read through the mount of files only the owner held: each
    step's reads, what they returned, the map's growth (the ranges it gained and their bytes) once settled."""
    out = []
    for label, which, reads, sequential in GRANULARITY:
        file_id, size = ids[f"gran-{which}"], GRANULAR_SIZES[which]
        before, _, _ = settled_blocks(view, file_id, local)
        reads = reads if reads is not None else inside_held(before, size)
        done = cell(sandbox, reads_cell(f"{inside}/gran-{which}.bin", reads, sequential=sequential), 300)
        after, seconds, settled = settled_blocks(view, file_id, local)
        gained = merged(uncovered(before, after))
        out.append(
            {
                "step": label,
                "file_bytes": size,
                "reads": [list(r) for r in reads],
                "sequential": sequential,
                "returned": done,
                "held_before": held_bytes(before),
                "gained": [list(g) for g in gained],
                "gained_bytes": held_bytes(gained),
                "held_after": held_bytes(after),
                "blocks_after": len(after),
                "settle_s": seconds,
                "settled": settled,
            }
        )
    return out


def reader_count(sandbox: DockerSandbox) -> int | None:
    """How many reads the background reader has done, from its counter in the agent's working disk, read by the host."""
    try:
        return int((sandbox.dir / "work" / "reader-count").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def moves(sandbox: DockerSandbox, seconds: float = 3.0) -> bool | None:
    """Whether the background reader's count changes over `seconds`; None when it cannot be read."""
    first = reader_count(sandbox)
    time.sleep(seconds)
    second = reader_count(sandbox)
    return None if first is None or second is None else first != second


def freeze_facts(host: DockerHost, sandbox: DockerSandbox, path: str, size: int) -> dict:
    """Whether the collection's freeze of the agent's container (`docker pause`, confirmed by `frozen 1` in its cgroup)
    holds while a reader in the agent keeps reading through the FUSE mount: the reader's count before, during and after
    the freeze, and how long the freeze took or why it failed. The freeze is #129/#130's (`_frozen`); without it, only
    the reader is measured."""
    out: dict = {"freeze_available": hasattr(sandbox, "_frozen")}
    started = cell(sandbox, reader_cell(path, size, "/work/reader-count"), 60)
    out["reader_started"] = "pid" in started
    deadline = time.monotonic() + 30
    while (reader_count(sandbox) or 0) < 3 and time.monotonic() < deadline:
        time.sleep(0.5)
    out["reader_moving_before"] = moves(sandbox)
    if not out["freeze_available"]:
        return out
    began = time.monotonic()
    try:
        with sandbox._frozen():
            out["frozen_after_s"] = round(time.monotonic() - began, 3)
            out["frozen_confirmed"] = sandbox._meta.get("frozen_for_collection") is True
            out["reader_moving_while_frozen"] = moves(sandbox)
    except SandboxError as error:
        out["freeze_error"] = str(error)
        out["failed_after_s"] = round(time.monotonic() - began, 3)
    state = (hostops.inspect(host, sandbox.names.agent) or {}).get("State", {})
    out["paused_after_thaw"] = state.get("Paused")
    out["reader_moving_after_thaw"] = moves(sandbox)
    return out


def collect_facts(sandbox: DockerSandbox, base: Path) -> dict:
    """The run's real collection, with the background reader still reading: it stops the agent's processes, freezes
    the container (with #129/#130), copies the named output out and thaws it. What it returned, or why it failed."""
    began = time.monotonic()
    try:
        result = sandbox.collect(base / "collected", outputs=["reader-count"])
    except Exception as error:  # recorded, never guessed: the attempt's tests read it
        return {"error": type(error).__name__, "seconds": round(time.monotonic() - began, 3)}
    return {
        "outputs": result["outputs"],
        "refused_output": result.get("refused_output"),
        "frozen_for_collection": sandbox._meta.get("frozen_for_collection"),
        "copied": (base / "collected" / "work" / "reader-count").is_file(),
        "seconds": round(time.monotonic() - began, 3),
    }


def wait_visible(provisioner: Provisioner, path: str, provider: str, size: int) -> float:
    """Wait, outside the agent's time, until `provider` lists the file at its size; the seconds it took."""
    began = time.monotonic()

    def read():
        try:
            return provisioner.resolve(path, provider=provider, wait=False).size
        except KeyError:
            return None

    done = consistency.converge(read, lambda found: found == size, timeout_s=VISIBLE_TIMEOUT_S, poll_s=2.0)
    assert done.converged, "a fixture file did not become visible at the local provider"
    return round(time.monotonic() - began, 1)


@pytest.fixture(scope="module")
def attempt(tmp_path_factory):
    """The whole attempt, once: write the files, start the sandbox, run the cells, stop it, check the host, clean up.
    Each test reads one fact from what it returns."""
    base = tmp_path_factory.mktemp("mount-live")
    config = load_platform(platform_for_mount(base))
    if config.principals.data_path is None:
        pytest.skip("[principals.data_path] is needed to mint the mount's token")
    s, local = config.sandbox, config.support.local
    owners = [p for p in config.support.providers if p != local]
    if not owners:
        pytest.skip("the space needs a supporting provider other than the local one, to own a file the local one lacks")
    owner = owners[0]
    host = DockerHost(sudo_command=s.sudo)
    client = OnezoneClient(config)
    placement = load_placement(config.file, config).settings
    probes_view = Provisioner(
        client, config, dataclasses.replace(placement, holder=local), provider_ids=resolve_provider_ids(config),
        log=base / "never-written.jsonl",
    )  # fmt: skip
    files = SpaceFiles(config)
    run = f"expcore-live-mount-{uuid.uuid4().hex[:8]}"
    whole = pattern(f"{run}/whole.bin", WHOLE_SIZE)
    facts: dict = {"providers": {"local": "local", "owner": "owner"}}
    try:
        top = files.make(local, files.root(local), run, "DIR")
        known = consistency.converge(lambda: files.seen(owner, top), bool, timeout_s=VISIBLE_TIMEOUT_S, poll_s=2.0)
        assert known.converged, "the fixture directory did not reach the owner"
        whole_id = files.make(owner, top, "whole.bin", "REG")
        files.write(owner, whole_id, 0, whole)
        ids = {"whole": whole_id}
        partials = {}
        for label, through in (("written_through_local", local), ("written_through_owner", owner)):
            name = f"{label}.bin"
            ids[label] = file_id = files.make(through, top, name, "REG")
            files.write(through, file_id, 0, pattern(f"{run}/{name}", PREFIX))
            files.write(through, file_id, PARTIAL_SIZE, b"")  # sets the size and writes nothing
            partials[label] = name
        for label, size in (("repeat", REPEAT_SIZE), ("untouched", UNTOUCHED_SIZE)):  # for the accounting check
            ids[label] = files.make(local, top, f"{label}.bin", "REG")
            files.write(local, ids[label], 0, pattern(f"{run}/{label}.bin", size))
        for which, size in GRANULAR_SIZES.items():  # only the owner holds them: every byte the mount reads is fetched
            ids[f"gran-{which}"] = files.make(owner, top, f"gran-{which}.bin", "REG")
            files.write(owner, ids[f"gran-{which}"], 0, pattern(f"{run}/gran-{which}.bin", size))
        facts["visible_after_s"] = {
            **{
                f"gran-{w}": wait_visible(probes_view, f"{run}/gran-{w}.bin", local, n)
                for w, n in GRANULAR_SIZES.items()
            },
            "whole": wait_visible(probes_view, f"{run}/whole.bin", local, WHOLE_SIZE),
            **{label: wait_visible(probes_view, f"{run}/{n}", local, PARTIAL_SIZE) for label, n in partials.items()},
            "repeat": wait_visible(probes_view, f"{run}/repeat.bin", local, REPEAT_SIZE),
            "untouched": wait_visible(probes_view, f"{run}/untouched.bin", local, UNTOUCHED_SIZE),
        }
        names = {"whole": "whole.bin", **{label: f"{n}" for label, n in partials.items()}}
        names |= {"repeat": "repeat.bin", "untouched": "untouched.bin"}
        names |= {f"gran-{which}": f"gran-{which}.bin" for which in GRANULAR_SIZES}
        forms = {label: {"id": ids[label], "uuid": file_guid(ids[label]), "name": names[label]} for label in ids}
        facts["ground_truth"] = ground_truth()
        network = s.federation.network
        federation_before = network_containers(host, network)
        spec = mount_spec(config, run)
        sandbox = DockerSandbox.start(host, s, spec)
        try:
            facts["launch"] = launch_facts(host, sandbox, network, spec.token.reveal())
            facts["token_file_left_on_host"] = (sandbox.dir / TOKEN_FILE).exists()  # gone once oneclient started
            inside = f"{s.mount.path}/{config.space.name}/{run}"
            urls = [config.zone.url, *(p.url for p in config.providers.values())]
            ports = sorted({Endpoint.from_url(url).port for url in urls})  # the host's, through the agent's loopback
            facts["confinement"] = cell(
                sandbox,
                confinement_cell(
                    s.mount.path,
                    config.space.name,
                    [s.federation.local_provider],
                    list(federation_before.values()),
                    ports,
                ),
                120,
            )
            probes = PlatformProbes(client, probes_view, {"whole": f"{run}/whole.bin"}, [local, owner])
            probes.start({})
            facts["read"] = cell(sandbox, read_cell(f"{inside}/whole.bin"), 600)
            facts["read"]["matches_the_file"] = facts["read"].get("sha256") == hashlib.sha256(whole).hexdigest()
            facts["read"].pop("sha256", None)
            entry = probes_view.resolve(f"{run}/whole.bin", provider=local, wait=False)
            settled = consistency.converge(
                lambda: held_bytes(merged(probes_view.blocks(entry.file_id, local))),
                lambda held: held == WHOLE_SIZE,
                timeout_s=180,
                poll_s=2.0,
            )
            facts["local_block_map_settled"] = settled.converged
            facts["probes"] = probes.finish()
            targets = {label: (f"{inside}/{name}", f"{run}/{name}") for label, name in partials.items()}
            facts["unwritten"] = cell(
                sandbox,
                unwritten_cell(targets, PREFIX, PARTIAL_SIZE, READ_DEADLINE_S),
                unwritten_cell_timeout(len(targets), READ_DEADLINE_S),
            )
            facts["accounting_reads"] = cell(
                sandbox,
                accounting_cell(f"{inside}/untouched.bin", f"{inside}/repeat.bin", REPEAT_OFFSET, REPEAT_LENGTH),
                120,
            )
            try:  # before the stop: the sidecar's log directory goes with it
                facts["accounting"] = sidecar_accounting(host, sandbox, spec, base, forms)
            except Exception as error:  # recorded, never guessed: the reason the evidence is missing
                facts["accounting"] = {"error": type(error).__name__}
            facts["granularity"] = granularity(sandbox, probes_view, local, inside, ids)
            facts["freeze"] = freeze_facts(host, sandbox, f"{inside}/gran-64m.bin", GRANULAR_SIZES["64m"])
            facts["collect"] = collect_facts(sandbox, base)
        finally:
            try:
                sandbox.stop()
                facts["stop_error"] = None
            except SandboxError as error:
                facts["stop_error"] = str(error)
            facts["teardown"] = teardown_facts(host, sandbox, config)
        facts["federation_network_unchanged"] = network_containers(host, network) == federation_before
        facts["federation_network_present"] = hostops.network_exists(host, network) is True
    finally:
        facts["fixture_cleanup_failures"] = files.remove_all()
        files.close()
        client.close()
        out = os.environ.get("EXPCORE_LIVE_FACTS_OUT")
        if out:  # also when the attempt failed part-way: what it showed until then
            text = json.dumps(redacted(facts, local, owner), indent=1, sort_keys=True, default=str)
            Path(out).write_text(text + "\n", encoding="utf-8")
    facts["names"] = {"local": local, "owner": owner}
    return facts


def redacted(facts: dict, local: str, owner: str) -> dict:
    """The facts with the providers named by role, for a file that may be committed."""
    text = json.dumps(facts)
    for name, role in ((local, "local"), (owner, "owner")):
        text = text.replace(f'"{name}"', f'"{role}"')
    return json.loads(text)


# -- 1. launch ---------------------------------------------------------------------------------------------------------


def test_o03_the_sidecar_alone_is_on_the_federation_network_and_mounted_with_the_federations_ca(attempt):
    """O03: the sidecar crossed to the federation on its own network, and on no other, verifying the provider's
    certificate against the federation's CA (`ca_file`); the agent is on the sandbox's network alone."""
    launch = attempt["launch"]
    assert launch["sidecar"]["networks"] == ["federation"]
    assert launch["agent"]["networks"] == ["sandbox"]
    assert launch["mount"]["network"] == "federation"
    assert launch["mount"]["wait_s"] is not None
    assert launch["tls_mode"] == "ca_file"


def test_o05_docker_inspect_of_the_running_sidecar_and_agent_shows_no_token(attempt):
    """O05: anyone with access to the docker daemon reads a container's configuration with `docker inspect`; on the
    running sidecar and agent it shows no token, and the host kept no copy of the token once oneclient had started
    (issue 137: the token was given to the sidecar as an environment variable, which docker keeps)."""
    launch = attempt["launch"]
    for role in ("sidecar", "agent"):
        assert launch[role]["inspected"] is True, role
        assert launch[role]["token_in_inspect"] is False, role
    assert attempt["token_file_left_on_host"] is False


def test_o05_only_the_sidecar_holds_fuse_and_sys_admin(attempt):
    """O05: /dev/fuse and SYS_ADMIN are the sidecar's alone; the agent has every capability dropped, no device, a
    read-only root and, from inside, an empty effective capability set and no /dev/fuse."""
    sidecar, agent = attempt["launch"]["sidecar"], attempt["launch"]["agent"]
    assert (sidecar["dev_fuse"], sidecar["sys_admin"]) == (True, True)
    assert (agent["dev_fuse"], agent["sys_admin"], agent["privileged"]) == (False, False, False)
    assert (agent["cap_drop_all"], agent["read_only_root"]) == (True, True)
    assert attempt["confinement"]["dev_fuse"] is False
    assert attempt["confinement"]["cap_eff_zero"] is True


# -- 2. confinement ----------------------------------------------------------------------------------------------------


def test_o05_the_agent_sees_the_mount_read_only_and_only_the_study_space(attempt):
    """O05: the mount holds the study space and nothing else, every mount at its path is read-only, a write into the
    space is refused as a read-only file system, and the start proved the root and the mount refuse a write."""
    seen = attempt["confinement"]
    assert seen["only_the_space"] is True
    assert seen["mount_lines"] >= 1
    assert seen["mount_read_only"] is True
    assert seen["write_in_the_space"] == "EROFS"
    assert attempt["launch"]["read_only_verified"] == {"root": True, "mount": True}


def test_o03_the_agent_reaches_the_federation_by_no_way_but_the_mount(attempt):
    """O03: from the agent, the local provider's name on the federation's network does not resolve, no container of
    that network accepts a connection at its address, and the host's published ports are not reachable through the
    agent's own loopback."""
    seen = attempt["confinement"]
    assert seen["by_name"] == ["unresolved"]
    assert seen["by_address"]
    assert "connected" not in seen["by_address"]
    assert "connected" not in seen["loopback_ports"]


# -- 3. reads ----------------------------------------------------------------------------------------------------------


def test_o08_the_mount_serves_the_whole_of_a_file_only_its_owner_held_byte_for_byte(attempt):
    """O08: a file written through a provider other than the local one reads whole through the mount: its size, the
    bytes read and their SHA-256 are the file's."""
    read = attempt["read"]
    assert read.get("error") is None
    assert read["size"] == read["read"] == WHOLE_SIZE
    assert read["matches_the_file"] is True


def test_o01_the_probes_attribute_the_bytes_fetched_on_demand_to_the_owner(attempt):
    """O01: before the read the local provider held nothing of the file; after it, its block map holds the whole file,
    and the probes attribute every one of those bytes to the owner, the one provider that held them, with no transfer
    in the window, so the lower bound on what came on demand is the whole file."""
    names, probes = attempt["names"], attempt["probes"]
    local, owner = names["local"], names["owner"]
    assert attempt["local_block_map_settled"] is True
    assert probes["ok"] is True
    readings = probes["files"]["whole"]["providers"]
    assert readings[local]["before"]["held_bytes"] == 0
    assert readings[owner]["before"]["held_bytes"] == WHOLE_SIZE
    assert probes["pairs"][local]["from"] == {owner: WHOLE_SIZE}
    assert probes["on_demand"] is not None, "the space's transfer listing could not be established"
    assert probes["on_demand"][local]["on_demand_lower_bound"] == WHOLE_SIZE


# -- 4. unwritten ranges -----------------------------------------------------------------------------------------------


def test_o08_each_read_of_a_partly_written_file_through_the_mount_is_classified_and_timed(attempt):
    """O08: every read of a file at its final size with only a prefix written, through the local provider and through
    the owner, has an outcome (zeros, nonzero, short, an errno, blocked, or not tried after a blocked one) and its
    seconds; the mount shows the final size, and every prefix byte a read returned is the one written."""
    found = attempt["unwritten"]
    assert set(found) == {"written_through_local", "written_through_owner"}
    for label, entry in found.items():
        assert entry["size"] == PARTIAL_SIZE, label
        for name in ("unwritten", "across_prefix_end", "whole"):
            read = entry[name]
            assert read["outcome"] in ("zeros", "nonzero", "short", "error", "blocked", "not_tried"), (label, name)
            if read["outcome"] != "not_tried":
                assert read["seconds"] >= 0, (label, name)
            if read["outcome"] in ("zeros", "nonzero", "short"):
                assert read["prefix_matches"] is True, (label, name)


# -- 5. teardown -------------------------------------------------------------------------------------------------------


def test_o15_the_teardown_unmounts_then_removes_the_sidecar_then_the_agent(attempt):
    """O15: the stop raised nothing, the FUSE mount was released before any container went, the sidecar was removed
    before the agent, and the teardown ended with the mount safe to drop."""
    down = attempt["teardown"]
    assert attempt["stop_error"] is None
    assert down["stopped"] is True
    steps = down["steps"]
    unmounted = steps.index(next(s for s in steps if s[0] == "unmount" and s[1] in ("unmounted", "detached")))
    sidecar = steps.index(("sidecar", "removed"))
    agent = steps.index(("agent", "removed"))
    assert unmounted < sidecar < agent
    assert down["fuse_safe"] is True


def test_o15_nothing_of_the_sandbox_is_left_on_the_host(attempt):
    """O15: no container or network carries the run's lease label, no mount is left under the work root, FUSE or
    other, the run's directory and its mount record are gone, and the lease's probe shows it all gone (C19)."""
    down = attempt["teardown"]
    assert down["containers_left"] == 0
    assert down["networks_left"] == 0
    assert down["mounts_left"] == 0
    assert down["fuse_mounts_left"] == 0
    assert down["run_directory_left"] is False
    assert down["mount_record_left"] is False
    assert down["probe_gone"] is True


def test_o15_the_federation_network_is_left_as_it_was_and_the_fixture_files_are_gone(attempt):
    """O15: the federation's network still exists with exactly the containers it had before the sandbox started, and
    every file the test wrote into the space was deleted."""
    assert attempt["federation_network_present"] is True
    assert attempt["federation_network_unchanged"] is True
    assert attempt["fixture_cleanup_failures"] == []


# -- 6. accounting -----------------------------------------------------------------------------------------------------


def test_o08_what_the_sidecar_can_show_of_the_agents_reads_is_captured_before_the_teardown(attempt):
    """O08: the agent's known pattern ran (one file only stat'd, one range read twice with the same bytes), and before
    the stop the sidecar's evidence was captured: oneclient's help, every file of its log directory with the lines
    naming each fixture file, and the entries of the mount's FUSE connection. Whether that evidence accounts for the
    reads is judged from it into the day-0 facts, never here."""
    reads, evidence = attempt["accounting_reads"], attempt["accounting"]
    assert reads["untouched_size"] == UNTOUCHED_SIZE
    assert [r.get("bytes") for r in reads["reads"]] == [REPEAT_LENGTH, REPEAT_LENGTH]
    assert reads["same_bytes_twice"] is True
    assert evidence.get("error") is None
    assert evidence["help_bytes"] > 0
    assert evidence["log_files"]
    assert set(evidence["mentions"]) == set(attempt["ground_truth"]) - {"not_the_agents_reads"}
    assert evidence["fuse_connection_entries"]


# -- 7. placement granularity through the mount ------------------------------------------------------------------------


def test_o08_each_read_through_the_mount_has_its_block_map_growth_recorded_once_settled(attempt):
    """O08: for every read of the granularity steps (1 B, 4 KiB and 1 MiB at several offsets, one starting inside held
    bytes, one of 5 MiB, three in order), the bytes it returned and the local provider's settled block-map growth are
    recorded; what that growth is, is a fact for the day-0 report, never assumed here."""
    steps = attempt["granularity"]
    assert [s["step"] for s in steps] == [label for label, *_ in GRANULARITY]
    for step in steps:
        assert step["settled"] is True, step["step"]
        assert [r.get("bytes") for r in step["returned"]] == [n for _, n in step["reads"]], step["step"]
        assert step["held_after"] >= step["held_before"], step["step"]


# -- 8. the collection's freeze with a reader in the agent ------------------------------------------------------------


def test_o08_the_collections_freeze_holds_a_reader_that_reads_through_the_mount(attempt):
    """O08: with a reader in the agent reading through the FUSE mount, the collection's freeze (`docker pause`,
    confirmed by `frozen 1` in the agent's cgroup) is reached, the reader stops while frozen and goes on once thawed.
    Measured only where the freeze exists (#129/#130); the reader's own movement is recorded either way."""
    freeze = attempt["freeze"]
    assert freeze["reader_started"] is True
    assert freeze["reader_moving_before"] is True
    if not freeze["freeze_available"]:
        pytest.skip("this tree has no collection freeze (#129/#130); the reader was measured alone")
    assert freeze.get("freeze_error") is None
    assert freeze["frozen_confirmed"] is True
    assert freeze["reader_moving_while_frozen"] is False
    assert freeze["paused_after_thaw"] is False
    assert freeze["reader_moving_after_thaw"] is True


def test_o08_the_runs_collection_copies_its_output_with_a_reader_still_running(attempt):
    """O08: the run's collection, started while the reader still reads through the mount, stops the agent's
    processes, copies the named output out (frozen while it copies, where the freeze exists) and returns normally."""
    collected = attempt["collect"]
    assert collected.get("error") is None
    assert collected["outputs"] == {"reader-count": True}
    assert collected["copied"] is True
    assert collected["refused_output"] is None
    if attempt["freeze"]["freeze_available"]:
        assert collected["frozen_for_collection"] is True
