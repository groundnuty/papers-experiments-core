"""One sandboxed REST (gate) read against the Compose federation, skipped unless every variable below is set (#203).

    EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml EXPCORE_LIVE_CONTENT_PATH=hello.txt \\
    EXPCORE_LIVE_SANDBOX_POOL=<a private IPv4 range the host routes nowhere> \\
    uv run pytest tests/live/test_rest_path.py

It starts containers (`EXPCORE_LIVE_DOCKER`) and writes nothing to the federation: it reads one provisioned file,
`EXPCORE_LIVE_CONTENT_PATH`, by its path in the study space. `EXPCORE_LIVE_PLATFORM` is the Compose federation's own
platform.toml (deploy/compose-federation, with `facts.json` beside it), whose URLs are the host's loopback ports. The
test writes a copy with a `[sandbox]` table into a directory of its own (`mount_live.derive_platform`,
`rest_live.rest_sandbox_table`): images pinned to the ids `EXPCORE_LIVE_AGENT_IMAGE` and `EXPCORE_LIVE_GATE_IMAGE`
resolve to now (default `mount_live.AGENT_IMAGE` for both; nothing is pulled), the pool from
`EXPCORE_LIVE_SANDBOX_POOL`, its work directory under pytest's temporary directory, one REST level, and
`[sandbox.federation]`: the gate on the federation's network (`EXPCORE_LIVE_FED_NETWORK`, default the layout's
project's network, from facts.json), dialling the zone and the local provider by their domains there. Nothing in the
file is overridden for the test: this is the configuration a study's A+ condition runs with.

The attempt runs once, in a fixture, and each test asserts one thing it showed:
1. the gate started (it said "gate ready") as root with every capability dropped but NET_BIND_SERVICE, reading keys
   given to root;
2. the gate alone is on the federation's network; the agent is on the sandbox's internal network only;
3. through the gate, the zone answers the token-scope inference and the local provider serves the file whole, its
   SHA-256 the one read from the host through the provider's own URL;
4. the gate refused nothing for its own integrity, and the collection counted the file's bytes as served;
5. the teardown leaves nothing of the sandbox on the host, and the federation's network as it was.
"""

import json
import os
import time
from pathlib import Path

import pytest

GATES = ("EXPCORE_LIVE_DOCKER",)
NEEDED = ("EXPCORE_LIVE_PLATFORM", "EXPCORE_LIVE_CONTENT_PATH", "EXPCORE_LIVE_SANDBOX_POOL")
if any(os.environ.get(gate) != "1" for gate in GATES) or not all(os.environ.get(name) for name in NEEDED):
    pytest.skip(
        "EXPCORE_LIVE_DOCKER=1, EXPCORE_LIVE_PLATFORM, EXPCORE_LIVE_CONTENT_PATH and EXPCORE_LIVE_SANDBOX_POOL are "
        "needed: this test starts containers and reads a provisioned file of a live federation through the data gate",
        allow_module_level=True,
    )

from mount_live import AGENT_IMAGE, derive_platform, federation_network  # noqa: E402 -- only once the module runs
from rest_live import LEASE_KEY, REST_LEVEL, expected_digest, read_cell, rest_sandbox_table  # noqa: E402

from expcore_onedata.client import OnezoneClient  # noqa: E402
from expcore_onedata.config import PlatformConfig, load_platform  # noqa: E402
from expcore_onedata.sandbox import hostops  # noqa: E402
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxError, SandboxSpec  # noqa: E402
from expcore_onedata.sandbox.hostops import DockerHost  # noqa: E402
from expcore_onedata.sandbox.provider import SandboxProbe  # noqa: E402
from expcore_onedata.sandbox.sandbox import DockerSandbox  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec  # noqa: E402

PLATFORM = Path(os.environ["EXPCORE_LIVE_PLATFORM"])
CONTENT_PATH = os.environ["EXPCORE_LIVE_CONTENT_PATH"]
CEILING_S = 1800.0
READ_TIMEOUT_S = 300.0


def pinned(host: DockerHost, name: str) -> tuple[str, str]:
    """An image already on the host, by its name and the id it resolves to now; never pulled."""
    try:
        return name, host.image_id(name)
    except SandboxError:
        pytest.skip("an image the attempt needs is not on the host; it is never pulled here")


def platform_for_rest(base: Path) -> tuple[Path, dict]:
    """The federation's platform.toml, copied with a `[sandbox]` table of one REST level (module docstring), and the
    federation's facts."""
    facts_file = PLATFORM.parent / "facts.json"
    if not facts_file.is_file():
        pytest.skip("no facts.json beside the platform file: the domains on the federation's network are read there")
    source = load_platform(PLATFORM, require_secrets=False)
    local = source.support.local
    facts = json.loads(facts_file.read_text(encoding="utf-8"))
    host = DockerHost()
    agent = pinned(host, os.environ.get("EXPCORE_LIVE_AGENT_IMAGE") or AGENT_IMAGE)
    gate = pinned(host, os.environ["EXPCORE_LIVE_GATE_IMAGE"]) if os.environ.get("EXPCORE_LIVE_GATE_IMAGE") else agent
    table = rest_sandbox_table(
        work_root=base / "work",
        images={"agent": agent, "gate": gate},
        pool=os.environ["EXPCORE_LIVE_SANDBOX_POOL"],
        network=os.environ.get("EXPCORE_LIVE_FED_NETWORK") or federation_network(facts),
        zone_address=facts["zone"]["domain_on_project_network"],
        local=local,
        local_address=facts["providers"][local]["domain_on_project_network"],
        content_check_file=CONTENT_PATH.strip("/"),
    )
    return derive_platform(PLATFORM, base, table), facts


def rest_spec(config: PlatformConfig, run_key: str) -> SandboxSpec:
    """The spec of a REST run, its token minted for the fixed data-path user, as a live check does; the endpoints are
    the platform file's own URLs, the loopback ports, which `[sandbox.federation]` must make the gate not dial."""
    client = OnezoneClient(config)
    try:
        token = TokenIssuer(client, config).mint(
            data_path_spec(config.space.id, "rest"), config.principals.data_path, ceiling_s=CEILING_S
        )
    finally:
        client.close()
    assert config.sandbox.mode_of(REST_LEVEL) is DataPathMode.REST
    return SandboxSpec(
        run_key=run_key,
        mode=DataPathMode.REST,
        token=token,
        zone=Endpoint.from_url(config.zone.url),
        provider=Endpoint.from_url(config.local_provider.url),
        lease_key=LEASE_KEY,
        listing=config.sandbox.listing_of(REST_LEVEL),
    )


def container_facts(host: DockerHost, sandbox: DockerSandbox, network: str) -> dict:
    """From the host, by role: the gate's user and capabilities, and the networks each container is on, by kind."""
    out = {}
    for role, name in (("gate", sandbox.names.gate), ("agent", sandbox.names.agent)):
        info = hostops.inspect(host, name) or {}
        config, settings = info.get("HostConfig") or {}, info.get("Config") or {}
        networks = set((info.get("NetworkSettings") or {}).get("Networks") or {})
        out[role] = {
            "user": settings.get("User"),
            "cap_drop": config.get("CapDrop") or [],
            "cap_add": sorted(c.removeprefix("CAP_") for c in config.get("CapAdd") or []),
            "running": (info.get("State") or {}).get("Running") is True,
            "networks": sorted(
                "federation" if n == network else "sandbox" if n == sandbox.names.net else "other" for n in networks
            ),
        }
    return out


def teardown_facts(host: DockerHost, sandbox: DockerSandbox, config: PlatformConfig, network: str) -> dict:
    """After the stop, from the host: what of the sandbox is left, and whether the federation's network is there."""
    label = f"label={config.sandbox.label('lease')}={LEASE_KEY}"
    containers = host.docker("ps", "-a", "-q", "--filter", label, check=False, timeout=30)
    networks = host.docker("network", "ls", "-q", "--filter", label, check=False, timeout=30)
    return {
        "stopped": sandbox.stopped,
        "containers_left": len(containers.stdout.split()) if containers.returncode == 0 else None,
        "networks_left": len(networks.stdout.split()) if networks.returncode == 0 else None,
        "run_directory_left": sandbox.dir.exists(),
        "probe_gone": SandboxProbe(host, config.sandbox).resource_gone(LEASE_KEY, {}) is True,
        "federation_network_there": hostops.network_exists(host, network),
    }


@pytest.fixture(scope="module")
def attempt(tmp_path_factory):
    """Start one REST sandbox on the federation, read the file through the gate, collect, stop; keep the facts."""
    base = tmp_path_factory.mktemp("rest-path")
    derived, facts = platform_for_rest(base)
    config = load_platform(derived)
    expected = expected_digest(config, CONTENT_PATH)
    if expected.get("absent"):
        pytest.skip("EXPCORE_LIVE_CONTENT_PATH names a file the study space does not hold")
    network = config.sandbox.federation.network
    host = DockerHost(sudo_command=config.sandbox.sudo)
    out: dict = {"expected": expected}
    sandbox = DockerSandbox.start(host, config.sandbox, rest_spec(config, f"live-rest-{int(time.time())}"))
    try:
        out["containers"] = container_facts(host, sandbox, network)
        out["keys"] = sorted(json.loads((sandbox.dir / "gate.json").read_text(encoding="utf-8"))["hosts"])
        sandbox.mark_run_start()
        result = sandbox.exec(read_cell(config.sandbox.federation.local_provider, config.space.name, CONTENT_PATH),
                              READ_TIMEOUT_S)  # fmt: skip
        try:
            out["read"] = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out["read"] = {"cell_failed": True, "exit_code": result.exit_code, "timed_out": result.timed_out}
        out["refusals"] = sandbox.gate_refusals()
        out["collected"] = sandbox.collect(base / "collected")
        out["served"] = sandbox.served_bytes()
    finally:
        sandbox.stop()
    out["teardown"] = teardown_facts(host, sandbox, config, network)
    out["federation"] = {"zone": facts["zone"]["domain_on_project_network"], "local": config.sandbox.federation}
    return out


def test_o05_the_gate_starts_as_root_with_one_capability_on_keys_given_to_root(attempt):
    """O30 (#203): the gate said it was ready, so it loaded its keys; it ran as root with every capability dropped but
    NET_BIND_SERVICE, the flags that kept it from reading keys the driver owned."""
    gate = attempt["containers"]["gate"]
    assert gate["running"] is True
    assert (gate["user"], gate["cap_drop"], gate["cap_add"]) == ("0", ["ALL"], ["NET_BIND_SERVICE"])


def test_o03_the_gate_alone_is_on_the_federation_network_and_serves_the_names_there(attempt):
    """O30 (#203): the gate is on the federation's network and the sandbox's; the agent on the sandbox's alone.
    The gate serves the zone and the local provider by their domains on the federation's network."""
    containers = attempt["containers"]
    assert containers["gate"]["networks"] == ["federation", "sandbox"]
    assert containers["agent"]["networks"] == ["sandbox"]
    federation = attempt["federation"]
    assert attempt["keys"] == sorted([federation["zone"], federation["local"].local_provider])


def test_o08_a_file_read_through_the_gate_is_the_files_bytes(attempt):
    """O30 (#203): through the gate, the zone answers and the local provider serves the provisioned file whole, and its
    SHA-256 is the one read from the host through the provider's own URL."""
    read, expected = attempt["read"], attempt["expected"]
    assert read.get("zone_status") == 200, read
    assert (read.get("lookup_status"), read.get("content_status")) == (200, 200), read
    assert (read["size"], read["sha256"]) == (expected["size"], expected["sha256"])


def test_o22_the_gate_refused_nothing_and_counted_the_bytes_it_served(attempt):
    """O05, O22: no answer was refused for the gate's integrity, and the collection counted at least the file's bytes
    as served to the agent."""
    assert attempt["refusals"] == []
    served = attempt["served"]
    assert served["unknown"] == [], served
    assert served["content_bytes"] >= attempt["expected"]["size"], served


def test_o15_nothing_of_the_sandbox_is_left_and_the_federation_network_is_as_it_was(attempt):
    """O15: the teardown removed the gate, which took it off the federation's network; nothing of the sandbox is left,
    the probe shows it gone, and the federation's network is still there."""
    assert attempt["teardown"] == {
        "stopped": True,
        "containers_left": 0,
        "networks_left": 0,
        "run_directory_left": False,
        "probe_gone": True,
        "federation_network_there": True,
    }
