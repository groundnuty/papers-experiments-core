"""One sandboxed attempt on the mount data path against a live federation, skipped unless every gate below is set.

    EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml \\
    EXPCORE_LIVE_SANDBOX_POOL=<a private IPv4 range the host routes nowhere> \\
    uv run pytest tests/live/test_mount_path.py

It starts containers (`EXPCORE_LIVE_DOCKER`), and it writes three files into the study space and deletes them again
(`EXPCORE_LIVE_MUTATE`). `EXPCORE_LIVE_PLATFORM` is the Compose federation's own platform.toml
(deploy/compose-federation, with `facts.json` beside it). The test writes a copy with a `[sandbox]` table into a
directory of its own
(`mount_live.derive_platform`): images pinned to the ids `EXPCORE_LIVE_AGENT_IMAGE` and `EXPCORE_LIVE_ONECLIENT_IMAGE`
resolve to now (defaults `mount_live.AGENT_IMAGE` and `ONECLIENT_IMAGE`; nothing is pulled), the pool from
`EXPCORE_LIVE_SANDBOX_POOL`, its work directory under pytest's temporary directory, one mount level, and
`[sandbox.federation]`: the oneclient sidecar on the federation's network (`EXPCORE_LIVE_FED_NETWORK`, default
`mount_live.FED_NETWORK`), dialling the local provider by its domain there. `EXPCORE_LIVE_FACTS_OUT` names a file the
attempt's facts are written to as JSON: outcomes, sizes and seconds, never a token, an address or a path.

The attempt runs once, in a fixture, and each test asserts one thing it showed:
1. launch: the sidecar alone has /dev/fuse and SYS_ADMIN, and it alone is on the federation's network;
2. confinement: the agent sees the mount read-only and only the study space, and reaches the federation by no other way;
3. reads: a file that only a provider other than the local one holds reads whole through the mount, its bytes the
   file's, and the probes attribute the bytes the local provider fetched on demand to that owner;
4. unwritten ranges: files at their final size with only a prefix written, through the local provider and through the
   owner, read through the mount; what each read returned, and how long it took, is recorded, never assumed;
5. teardown: unmount, then the sidecar, then the agent; nothing of the sandbox left on the host; the federation's
   network as it was.
"""

import dataclasses
import hashlib
import json
import os
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
    FED_NETWORK,
    LEASE_KEY,
    ONECLIENT_IMAGE,
    SpaceFiles,
    confinement_cell,
    derive_platform,
    pattern,
    read_cell,
    sandbox_table,
    unwritten_cell,
    unwritten_cell_timeout,
)

from expcore_onedata import consistency  # noqa: E402
from expcore_onedata.client import OnezoneClient  # noqa: E402
from expcore_onedata.config import PlatformConfig, load_platform  # noqa: E402
from expcore_onedata.placement import held_bytes, merged  # noqa: E402
from expcore_onedata.probes import PlatformProbes  # noqa: E402
from expcore_onedata.provision import Provisioner, resolve_provider_ids  # noqa: E402
from expcore_onedata.rangemap import load_placement  # noqa: E402
from expcore_onedata.sandbox import hostops  # noqa: E402
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxError, SandboxSpec  # noqa: E402
from expcore_onedata.sandbox.hostops import DockerHost  # noqa: E402
from expcore_onedata.sandbox.provider import SandboxProbe  # noqa: E402
from expcore_onedata.sandbox.sandbox import MOUNT_RECORDS, DockerSandbox  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec  # noqa: E402

PLATFORM = Path(os.environ["EXPCORE_LIVE_PLATFORM"])
CEILING_S = 1800.0
WHOLE_SIZE = (4 << 20) + 123  # not a multiple of any block size
PREFIX, PARTIAL_SIZE = 64 << 10, 1 << 20  # as the REST measurement of the day-0 facts, so the two compare
READ_DEADLINE_S = 30.0  # a read through the mount that has not returned by then is recorded as blocked
VISIBLE_TIMEOUT_S = 180.0


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
    host = DockerHost()
    images = {
        "agent": pinned(host, os.environ.get("EXPCORE_LIVE_AGENT_IMAGE") or AGENT_IMAGE),
        "oneclient": pinned(host, os.environ.get("EXPCORE_LIVE_ONECLIENT_IMAGE") or ONECLIENT_IMAGE),
    }
    table = sandbox_table(
        work_root=base / "work",
        images=images,
        pool=pool,
        network=os.environ.get("EXPCORE_LIVE_FED_NETWORK") or FED_NETWORK,
        local=local,
        local_address=json.loads(facts_file.read_text(encoding="utf-8"))["providers"][local][
            "domain_on_project_network"
        ],
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


def launch_facts(host: DockerHost, sandbox: DockerSandbox, network: str) -> dict:
    """From the host: which container has FUSE and SYS_ADMIN, and which networks each is on, by role."""
    out = {}
    for role, name in (("sidecar", sandbox.names.sidecar), ("agent", sandbox.names.agent)):
        info = hostops.inspect(host, name) or {}
        config = info.get("HostConfig") or {}
        networks = set((info.get("NetworkSettings") or {}).get("Networks") or {})
        out[role] = {
            "dev_fuse": any(d.get("PathOnHost") == "/dev/fuse" for d in config.get("Devices") or []),
            "sys_admin": "SYS_ADMIN" in (config.get("CapAdd") or []),
            "cap_drop_all": (config.get("CapDrop") or []) == ["ALL"],
            "privileged": bool(config.get("Privileged")),
            "read_only_root": bool(config.get("ReadonlyRootfs")),
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
        partials = {}
        for label, through in (("written_through_local", local), ("written_through_owner", owner)):
            name = f"{label}.bin"
            file_id = files.make(through, top, name, "REG")
            files.write(through, file_id, 0, pattern(f"{run}/{name}", PREFIX))
            files.write(through, file_id, PARTIAL_SIZE, b"")  # sets the size and writes nothing
            partials[label] = name
        facts["visible_after_s"] = {
            "whole": wait_visible(probes_view, f"{run}/whole.bin", local, WHOLE_SIZE),
            **{label: wait_visible(probes_view, f"{run}/{n}", local, PARTIAL_SIZE) for label, n in partials.items()},
        }
        network = s.federation.network
        federation_before = network_containers(host, network)
        sandbox = DockerSandbox.start(host, s, mount_spec(config, run))
        try:
            facts["launch"] = launch_facts(host, sandbox, network)
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
