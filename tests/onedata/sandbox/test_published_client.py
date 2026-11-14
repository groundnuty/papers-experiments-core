"""O23, O05: the published Onedata client, `onedatarestfsspec`, works through the data gate on a REST data path.

The sandbox is started as a campaign starts it, on the fake host, whose one-shot containers of the agent image run
here: the image's interpreter is this one, so the certifi bundle the sandbox finds and extends is this environment's.
Its CA is a real one (openssl), and the data gate runs in-process with the configuration the sandbox wrote, before the
fake platform (`fake_onedata.MockOnedata`), which answers only the token the gate holds. The agent's code is a separate
process with exactly the environment the sandbox gives the agent container. Two things of the container are emulated:
its `--add-host` lines (the zone and the provider resolve to the gate, on port 443, the only port the client uses) and
its read-only bind mounts, at the one place the client reads a mounted path, `certifi.where()`. Nothing leaves the
machine.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import pytest
from fake_host import FakeHost, _parse_run
from sandbox_support import PUBLISHED_CLIENT, TOKEN, make_spec, start_options

from expcore_onedata.sandbox import gate
from expcore_onedata.sandbox.containers import ENV_ZONE_HOST
from expcore_onedata.sandbox.hostops import CmdResult, DockerHost
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.testing import MockOnedata

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the run CA")

SPACE = "invented-space"  # the space the fake platform's token scope names; invented
FILES = {"dir/a.bin": bytes(range(256)) * 4, "dir/b.txt": b"a second file\n", "top.txt": b"at the space root\n"}

# What the container does before the agent's code runs: the zone and the provider resolve to the gate on port 443, as
# the sandbox's --add-host lines make them, and the image's certifi bundle reads as the file mounted over it. It prints
# whether a mount covered that bundle; then the agent's code, the live test's, runs.
CONTAINER = r"""
import json, socket, sys
routes, mounts = json.loads(sys.argv[1]), json.loads(sys.argv[2])
resolve = socket.getaddrinfo
def through_the_gate(host, port, *rest, **options):
    if host in routes and int(port) == 443:
        return resolve("127.0.0.1", routes[host], *rest, **options)
    raise socket.gaierror(socket.EAI_NONAME, "no such host in the sandbox")
socket.getaddrinfo = through_the_gate
import certifi
image_bundle = certifi.where()
if image_bundle in mounts:
    certifi.where = lambda: mounts[image_bundle]
print(json.dumps({"mounted_bundle": image_bundle in mounts}))
"""
AGENT_CODE = CONTAINER + PUBLISHED_CLIENT.format(space=SPACE, path="dir/a.bin")


class ImageOnThisHost(FakeHost):
    """The fake host, with a real run CA and one-shot containers of the agent image that run here: `cat` reads the file
    and the image's interpreter is this one."""

    def __init__(self, python: str) -> None:
        super().__init__()
        self.python = python

    def make_ca(self, directory, names) -> None:
        DockerHost().make_ca(directory, names)

    def _run_container(self, args, env):
        options, _image, command = _parse_run(args)
        if not options.get("--rm"):
            return super()._run_container(args, env)
        entry = options["--entrypoint"]
        assert entry in ("cat", self.python), entry
        argv = ["cat", *command] if entry == "cat" else [sys.executable, *command]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        return CmdResult(done.returncode, done.stdout, done.stderr)


@pytest.fixture
def image_here(world):
    """The conftest's world, on a host whose agent image is this environment."""
    host = ImageOnThisHost(world.s.python)
    for ref in (world.s.agent, world.s.gate, world.s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(world.s.oneclient.id)
    world.host = host
    return world


@pytest.fixture
def platform(world):
    """The fake platform, whose token scope names the platform document's zone and pinned local provider, which the
    gate serves; it answers only the run's token."""
    zone, provider = (urlsplit(url).hostname for url in (world.config.zone.url, world.config.local_provider.url))
    mock = MockOnedata(FILES, space_name=SPACE, zone_host=zone, provider_host=provider, token=TOKEN)
    mock.start(host="127.0.0.1")
    yield mock
    mock.stop()


def serve_gate(sbx, mock, log):
    """The data gate, in-process, with the configuration the sandbox wrote for its container, its upstream the fake
    platform and its token the run's: the one the sandbox hands the gate container in its environment."""
    config = json.loads((sbx.dir / "gate.json").read_text(encoding="utf-8"))
    for host in config["hosts"].values():
        host.update(addr="127.0.0.1", port=mock.port, tls=False)
    gate.CFG.clear()
    gate.CFG.update(config, token=TOKEN, log=str(log), tls_dir=str(sbx.dir / "tls"))
    log.touch()
    httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
    httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, list(config["hosts"])


def agent_environment(container) -> tuple[dict[str, str], dict[str, str]]:
    """The environment of the agent container (each `-e NAME` with the value the docker client passed) and its bind
    mounts (destination -> source)."""
    names = container.options.get("-e", [])
    env = {name: container.options["_env"][name] for name in names}
    mounts = {}
    for spec in container.options.get("-v", []):
        source, destination, *_ = spec.split(":")
        mounts[destination] = source
    return env, mounts


def run_agent_code(env, mounts, routes) -> tuple[dict, dict]:
    """The agent's code in a process of its own, with exactly the agent's environment: what the container emulation
    said, and what the published client saw."""
    argv = [sys.executable, "-I", "-c", AGENT_CODE, json.dumps(routes), json.dumps(mounts)]
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    container, client = (json.loads(line) for line in done.stdout.splitlines())
    return container, client


def test_o23_the_published_client_lists_and_reads_through_the_gate_with_the_placeholder_and_the_default_trust(
    image_here, platform, tmp_path
):
    """O23, O05: with only the environment the sandbox gives the agent on a REST data path (the zone's host, the
    placeholder token, nothing else of the credential kind) and the trust it sets up by default (the image's certifi
    bundle extended with the run's CA), the published client initialises, lists a directory and reads a file's bytes
    through the gate over verified TLS. Every request the platform received carried the run's token, which the gate
    holds; the placeholder never reached it, and the run's token is nowhere in the agent's environment."""
    world = image_here
    sbx = DockerSandbox.start(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    httpd = None
    try:
        env, mounts = agent_environment(world.host.containers[sbx.names.agent])
        httpd, served = serve_gate(sbx, platform, tmp_path / "requests.jsonl")
        assert env[ENV_ZONE_HOST] in served
        container, said = run_agent_code(env, mounts, dict.fromkeys(served, httpd.server_port))
        metadata = sbx.metadata()
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        gate.CFG.clear()
        sbx.stop()
    assert container["mounted_bundle"] is True
    assert metadata["certifi"] == "run_ca_added"
    assert said["client"] == "25.2.0"  # the version the dev dependency pins
    assert said["token"] == env["ONEDATA_TOKEN"] == world.s.placeholder_token
    assert said["listing"] == [f"{SPACE}/dir/a.bin", f"{SPACE}/dir/b.txt"]
    assert said["read"] == said["size"] == len(FILES["dir/a.bin"])
    assert said["sha256"] == hashlib.sha256(FILES["dir/a.bin"]).hexdigest()
    assert TOKEN not in json.dumps(env)
    assert platform.requests, "the platform received nothing"
    scope = [request for request in platform.requests if request.path.endswith("infer_access_token_scope")]
    assert scope
    # O30: the inference carries the gate's token in its body and none in a header (the zone answers 401 to a
    # data-path token there); every other call carries the gate's token in its header
    assert {json.loads(request.body)["token"] for request in scope} == {TOKEN}
    assert {request.token for request in scope} == {None}
    assert {request.token for request in platform.requests if request not in scope} == {TOKEN}
    assert not [r for r in platform.requests if world.s.placeholder_token.encode() in r.body]
