"""Sandbox tests against a real docker daemon, skipped unless EXPCORE_LIVE_DOCKER=1.

    EXPCORE_LIVE_DOCKER=1 uv run pytest tests/live/test_sandbox.py
    EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_PLATFORM=/path/to/platform.toml uv run pytest tests/live/test_sandbox.py

The first checks need only a docker daemon. The ones that start a sandbox need `EXPCORE_LIVE_PLATFORM` as well: its
`[sandbox]` table names the images, the address pool, the work directory and the levels of the data path, and the
data-path levels mint a read-only token for a fixed user (`[principals.data_path]`). They start a sandbox for each
configured level, run a line of code in it, prove it refuses a write, stop it, and require that the lease key's
probe then answers True. Nothing here writes to a space; without the variables nothing is constructed or contacted.
"""

import os
import time

import pytest

if os.environ.get("EXPCORE_LIVE_DOCKER") != "1":
    pytest.skip("EXPCORE_LIVE_DOCKER is not set; these tests use a real docker daemon", allow_module_level=True)

from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxSpec
from expcore_onedata.sandbox.hostops import DockerHost
from expcore_onedata.sandbox.provider import SandboxProbe
from expcore_onedata.sandbox.sandbox import DockerSandbox

PLATFORM = os.environ.get("EXPCORE_LIVE_PLATFORM")
CEILING_S = 300.0
needs_platform = pytest.mark.skipif(not PLATFORM, reason="EXPCORE_LIVE_PLATFORM is not set")


def test_docker_answers_and_the_host_facts_hold_no_name():
    facts = DockerHost().host_facts()
    assert facts["docker_server"]
    assert set(facts) == {"kernel", "docker_server"}


@pytest.fixture(scope="module")
def config():
    from expcore_onedata.config import load_platform

    loaded = load_platform(PLATFORM)
    if loaded.sandbox is None:
        pytest.skip("the platform file has no [sandbox] table")
    return loaded


@pytest.fixture(scope="module")
def host(config):
    return DockerHost(sudo_command=config.sandbox.sudo)


def configured_levels() -> list[str]:
    """The levels of the platform file's `[sandbox.data_paths]`, read at collection so each one is a test of its own."""
    if not PLATFORM:
        return []
    from expcore_onedata.config import load_platform

    loaded = load_platform(PLATFORM, require_secrets=False)
    return sorted(loaded.sandbox.data_paths) if loaded.sandbox is not None else []


LEVELS = configured_levels()


def levels(config):
    return sorted(config.sandbox.data_paths)


def spec_for(config, level, run_key, lease_key=None):
    from expcore_onedata.client import OnezoneClient
    from expcore_onedata.tokens import TokenIssuer, data_path_spec

    mode = config.sandbox.mode_of(level)
    if mode is DataPathMode.NONE:
        return SandboxSpec(run_key=run_key, mode=mode, lease_key=lease_key)
    principal = config.principals.data_path
    if principal is None:
        pytest.skip("[principals.data_path] is needed to mint a data-path token")
    client = OnezoneClient(config)
    try:
        token = TokenIssuer(client, config).mint(
            data_path_spec(config.space.id, mode.interface or "rest"), principal, ceiling_s=CEILING_S
        )
    finally:
        client.close()
    return SandboxSpec(
        run_key=run_key,
        mode=mode,
        token=token,
        zone=Endpoint.from_url(config.zone.url),
        provider=Endpoint.from_url(config.local_provider.url),
        lease_key=lease_key,
    )


@needs_platform
def test_each_configured_data_path_starts_runs_code_refuses_a_write_and_is_gone_after_stop(config, host):
    probe = SandboxProbe(host, config.sandbox)
    for level in levels(config):
        key = "live-check"  # a label for the probe; no lease is taken
        sandbox = DockerSandbox.start(host, config.sandbox, spec_for(config, level, f"live-{int(time.time())}", key))
        try:
            assert sandbox.metadata()["read_only_verified"]["root"] is True
            result = sandbox.exec("print(6 * 7)", 60)
            assert result.stdout.strip() == "42"
            assert result.kill is None
        finally:
            sandbox.stop()
        assert sandbox.stopped
        assert probe.resource_gone(key, {}) is True


@needs_platform
def test_a_write_to_the_agents_root_is_refused_in_a_started_sandbox(config, host):
    level = levels(config)[0]
    sandbox = DockerSandbox.start(host, config.sandbox, spec_for(config, level, f"live-ro-{int(time.time())}"))
    try:
        result = sandbox.exec(
            "import errno\ntry:\n    open('/probe', 'w')\nexcept OSError as e:\n    print(e.errno == errno.EROFS)", 60
        )
        assert result.stdout.strip() == "True"
    finally:
        sandbox.stop()


NO_CREDENTIAL = """
import os, pathlib
needle = {needle!r}.encode()
hits = []
for name in os.environ:
    if any(w in name.upper() for w in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
        hits.append("an environment variable named like a credential")
for pid in filter(str.isdigit, os.listdir("/proc")):
    for what in ("environ", "cmdline"):
        try:
            data = pathlib.Path("/proc/" + pid + "/" + what).read_bytes()
        except OSError:
            continue
        if needle in data:
            hits.append("the " + what + " of a process")
for top in ("/work", "/tmp", "/etc", "/home", "/root", "/run", "/var", "/opt", "/srv", "/mnt"):
    for here, _, files in os.walk(top, onerror=lambda e: None):
        for name in files:
            try:
                path = pathlib.Path(here, name)
                if path.stat().st_size < (1 << 22) and needle in path.read_bytes():
                    hits.append("a file")
            except OSError:
                continue
print("clean" if not hits else sorted(set(hits)))
"""


@needs_platform
@pytest.mark.parametrize("level", LEVELS)
def test_no_credential_is_visible_inside_the_agent_on_the_data_path_of_this_level(config, host, level):
    """O05: the agent's code cannot read the data-path token: not in its environment, its files or its processes."""
    spec = spec_for(config, level, f"live-cred-{int(time.time())}")
    sandbox = DockerSandbox.start(host, config.sandbox, spec)
    try:
        needle = spec.token.reveal() if spec.token is not None else "no-token-for-this-level"
        result = sandbox.exec(NO_CREDENTIAL.format(needle=needle), 120)
        assert result.stdout.strip() == "clean"
    finally:
        sandbox.stop()
