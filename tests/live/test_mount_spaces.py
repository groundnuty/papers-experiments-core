"""One sandbox reads a small file from each of several spaces through one oneclient process (O36), skipped unless every
gate below is set.

    EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml \\
    EXPCORE_LIVE_SANDBOX_POOL=<a private IPv4 range the host routes nowhere> \\
    uv run pytest tests/live/test_mount_spaces.py

oneclient 25.0 started with neither force-I/O option hangs the first read of a second space backed by the same storage,
with nothing logged (`src/cache/helpersCache.h`, `performAutoIOStorageDetection`: it locks `m_accessTypeMutex` again on
the thread that holds it). The core always starts the sidecar's oneclient with the table's `io_mode`; this test shows,
on a live federation, that with it every space of one mount reads.

It starts containers (`EXPCORE_LIVE_DOCKER`) and writes one small file into each declared space and deletes it again
(`EXPCORE_LIVE_MUTATE`). `EXPCORE_LIVE_PLATFORM` is a federation's platform.toml (the Compose federation's, from a
layout whose study declares at least two spaces in `[[spaces]]`, with `facts.json` beside it); the data-path principal
must be a member of every space it declares, since the mount's token is scoped to all of them. The test writes a copy
with a `[sandbox]` table into a directory of its own (`mount_live.derive_platform`), as `test_mount_path.py` does, with
`io_mode = "proxy"`. It skips, naming the reason, when the file declares fewer than two spaces, and never writes into a
space the file does not declare. For the check to reach the failure it guards, the local provider supports the spaces
on one storage, which the Compose federation arranges (each provider has one storage, which supports every space it
supports).

The attempt runs once, in a fixture: one file is written into each space through the local provider, one sandbox
mounts them all, and the agent reads the files one after another, each in a child process given `READ_DEADLINE_S`.
`EXPCORE_LIVE_FACTS_OUT` names a file the outcomes are written to as JSON (seconds and words, never a name, an address
or a path).
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

GATES = ("EXPCORE_LIVE_DOCKER", "EXPCORE_LIVE_MUTATE")
if any(os.environ.get(gate) != "1" for gate in GATES) or not os.environ.get("EXPCORE_LIVE_PLATFORM"):
    pytest.skip(
        "EXPCORE_LIVE_DOCKER=1, EXPCORE_LIVE_MUTATE=1 and EXPCORE_LIVE_PLATFORM are needed: this test starts "
        "containers and writes into the spaces of a live federation",
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
    spaces_cell,
    spaces_cell_timeout,
)

from expcore_onedata.client import OnezoneClient  # noqa: E402
from expcore_onedata.config import PlatformConfig, load_platform  # noqa: E402
from expcore_onedata.sandbox import hostops  # noqa: E402
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxError, SandboxSpec  # noqa: E402
from expcore_onedata.sandbox.hostops import DockerHost  # noqa: E402
from expcore_onedata.sandbox.provider import SandboxProbe  # noqa: E402
from expcore_onedata.sandbox.sandbox import DockerSandbox  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec  # noqa: E402

PLATFORM = Path(os.environ["EXPCORE_LIVE_PLATFORM"])
CEILING_S = 1800.0
PLANE = "data_path"  # the token plane the mount reaches its spaces through
FILE_SIZE = 64 * 1024 + 7  # small, and not a multiple of any block size
READ_DEADLINE_S = 30.0  # a read through the mount that has not returned by then is recorded as blocked (a hang)
APPEAR_S = 120.0  # how long a file may take to reach the mount after it was written


def pinned(host: DockerHost, name: str) -> tuple[str, str]:
    """An image already on the host, by its name and the id it resolves to now; never pulled."""
    try:
        return name, host.image_id(name)
    except SandboxError:
        pytest.skip("an image the attempt needs is not on the host; it is never pulled here")


def platform_with_mount(base: Path) -> Path:
    """The federation's platform.toml, copied with a `[sandbox]` table of one mount level, `io_mode = "proxy"`."""
    pool = os.environ.get("EXPCORE_LIVE_SANDBOX_POOL")
    if not pool:
        pytest.skip(
            "EXPCORE_LIVE_SANDBOX_POOL is not set: the sandbox's own networks need a range the host routes nowhere"
        )
    facts_file = PLATFORM.parent / "facts.json"
    if not facts_file.is_file():
        pytest.skip("no facts.json beside the platform file: the local provider's domain on its network is read there")
    local = load_platform(PLATFORM, require_secrets=False).spaces[0].support.local
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
    )
    return derive_platform(PLATFORM, base, table)


def mount_spec(config: PlatformConfig, run_key: str) -> SandboxSpec:
    """The spec of a mount run whose token is scoped to every space the data path reaches, minted for the fixed
    data-path user."""
    client = OnezoneClient(config)
    try:
        token = TokenIssuer(client, config).mint(
            data_path_spec(config.space_ids(PLANE), "oneclient"), config.principals.data_path, ceiling_s=CEILING_S
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


@pytest.fixture(scope="module")
def attempt(tmp_path_factory):
    """The whole attempt, once: one file in each space, one sandbox, each file read through its mount, the stop and
    the cleanup. Each test reads one fact from what it returns."""
    base = tmp_path_factory.mktemp("mount-spaces")
    config = load_platform(platform_with_mount(base))
    mounted = [entry for entry in config.spaces if PLANE in entry.tokens]
    if len(mounted) < 2:
        pytest.skip(
            "the platform file declares fewer than two spaces the data path reaches: the failure this guards is "
            "the first read of a second space"
        )
    if config.principals.data_path is None:
        pytest.skip("[principals.data_path] is needed to mint the mount's token")
    s, local = config.sandbox, mounted[0].support.local
    host = DockerHost(sudo_command=s.sudo)
    files = SpaceFiles(config)
    run = f"expcore-live-spaces-{uuid.uuid4().hex[:8]}"
    facts: dict = {"spaces": len(mounted)}
    try:
        written: dict[str, tuple[str, int, str]] = {}
        for position, entry in enumerate(mounted):
            data = pattern(f"{run}/{position}", FILE_SIZE)
            top = files.make(local, files.root(local, entry.space.id), run, "DIR")
            files.write(local, files.make(local, top, "small.bin", "REG"), 0, data)
            label = f"space-{position}"  # by position, never by a name (O12)
            written[label] = (
                f"{s.mount.path}/{entry.space.name}/{run}/small.bin",
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
        spec = mount_spec(config, run)
        sandbox = DockerSandbox.start(host, s, spec)
        try:
            facts["mount"] = {k: sandbox.metadata()["mount"].get(k) for k in ("io_mode", "options", "network")}
            result = sandbox.exec(
                spaces_cell(written, READ_DEADLINE_S, APPEAR_S),
                spaces_cell_timeout(len(written), READ_DEADLINE_S, APPEAR_S),
            )
            try:
                facts["reads"] = json.loads(result.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                facts["reads"] = {"cell_failed": True, "exit_code": result.exit_code, "timed_out": result.timed_out}
        finally:
            try:
                sandbox.stop()
                facts["stop_error"] = None
            except SandboxError as error:
                facts["stop_error"] = str(error)
            facts["gone"] = SandboxProbe(host, s).resource_gone(LEASE_KEY, {}) is True
        facts["federation_network_present"] = hostops.network_exists(host, s.federation.network) is True
    finally:
        facts["fixture_cleanup_failures"] = files.remove_all()
        files.close()
        out = os.environ.get("EXPCORE_LIVE_FACTS_OUT")
        if out:  # also when the attempt failed part-way: what it showed until then
            Path(out).write_text(json.dumps(facts, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return facts


def test_o36_a_file_of_every_space_reads_through_one_mount_within_the_bound_with_its_own_bytes(attempt):
    """O36: the first read of each of several spaces, in one oneclient process, returns within the deadline and returns
    the file's bytes: none is `blocked` (the hang of oneclient's own detection on a second space), none an error, none
    a mismatch."""
    reads = attempt["reads"]
    assert "cell_failed" not in reads, reads
    assert len(reads) == attempt["spaces"]
    for label, found in reads.items():
        assert found["outcome"] == "read", (label, found)
        assert found["matches"] is True, label
        assert found["seconds"] < READ_DEADLINE_S, label


def test_o36_the_mount_ran_with_the_declared_io_mode_and_the_options_the_sandbox_passed(attempt):
    """O36: the run's metadata names the effective I/O mode, `proxy`, and the force option among the options the sandbox
    itself passed to oneclient."""
    assert attempt["mount"]["io_mode"] == "proxy"
    assert "--force-proxy-io" in attempt["mount"]["options"]
    assert "--force-direct-io" not in attempt["mount"]["options"]


def test_o36_the_sandbox_that_read_several_spaces_leaves_nothing_behind(attempt):
    """O36: the stop is clean, the probe finds nothing of the run's lease on the host, the fixture files are shown
    gone, and the federation's network is as it was."""
    assert attempt["stop_error"] is None
    assert attempt["gone"] is True
    assert attempt["fixture_cleanup_failures"] == []
    assert attempt["federation_network_present"] is True
