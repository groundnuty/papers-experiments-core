"""A provider that can be down, around the public fakes: what the day-0 outage check's tests need.

`FakeData` serves a file's bytes whoever holds them and answers every call of a provider that is "up". It models no
provider that is down, so this module adds one outside the fakes, as a transport in front of them:

- `OutageWorld` answers a call to a downed provider's host with a refused connection, and makes a read through a
  provider that lacks a file's bytes, when every provider that holds them is down, wait out its timeout (the clock is
  the test's `Ticker`, so nothing waits) and then time out. A transfer of such a file stays active. What each of
  these does is a stand-in's choice, not a fact about Onedata: the live day-0 run is what measures them.
- `FakeServices` is the fault injector's driver's stand-in (`expcore_onedata.faults.FaultDriver`): `stop` takes the host
  down, `start` brings it up, and a service's identity is its generation, which a start changes (or not, to model a
  restore that did not take effect). `WorldDaemon` is `fault_support.FakeDaemon` (the Docker daemon behind the real
  Compose driver, on the test's clock) with the world's hosts following its containers, for a stop that times out.
- `AgentWorld` is what the agent meets of the same world: the mount of a sandbox on the sandbox's fake host (the
  answer to each mount cell the check runs in the agent's container) and the four knowledge tools, which the stand-in
  tool server (`outage_tools_server.py`, a real MCP stdio server) relays to the test through `ToolRelay`.

Every name is invented, as in `expcore_onedata.testing`.
"""

import ast
import asyncio
import hashlib
import json
import re
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
from fake_host import FakeHost
from fault_support import FakeDaemon
from mcp import StdioServerParameters
from sandbox_support import FAST, SLOT_USERS, add_sandbox

from expcore.tools.manifest import FrozenManifest, live_manifest
from expcore_onedata.compose import DriverError, Observation
from expcore_onedata.config import load_platform
from expcore_onedata.day0.outage_tools import KNOWLEDGE_TOOLS
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.testing import BASELINE, CREDENTIALS_DIR, FakeData, Ticker

GAMMA_ID, GAMMA_HOST = "pr-gamma", "gamma.test"
SERVICE = "svc-alpha"


class OutageWorld:
    """The fakes' federation seen through a transport on which providers can be down."""

    def __init__(self, fake: FakeData, ticker: Ticker) -> None:
        self.fake, self.ticker = fake, ticker
        self.down: set[str] = set()  # hosts that are down
        self.reads_while_down = "stall"  # what a read with every holder down does: "stall", or "refuse" (a 500)
        self.onezone_notices = True  # Onezone marks a provider whose service is down offline at once
        self.listing_loses = 0  # while a provider is down, a children listing leaves out this many entries
        self.listing_resizes = False  # ... or shows every entry with another size
        self.listing_cut = False  # ... or answers a page after the first with a 500
        self.calls: list[tuple[str, str]] = []  # (host, route) of every provider call, in order

    def holders_up(self, file_id: str) -> bool:
        """Whether some provider that holds any of a file's bytes is up."""
        hosts = {p["domain"]: pid for pid, p in self.fake.providers.items()}
        held = {pid for (fid, pid), blocks in self.fake.blocks.items() if fid == file_id and blocks}
        return any(host not in self.down for host, pid in hosts.items() if pid in held)

    def respond(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host in self.down:
            raise httpx.ConnectError("connection refused")
        read = re.fullmatch(r".*/oneprovider/data/([^/]+)/content", path)
        if read and self.down and not self.holds_here(host, read.group(1)) and not self.holders_up(read.group(1)):
            return self.cannot_fetch(request)
        if self.down and self.listing_cut and path.endswith("/children") and "token" in request.url.params:
            return httpx.Response(500, json={"error": {"id": "internalServerError", "details": {}}})
        record = re.fullmatch(r".*/oneprovider/transfers/([^/]+)", path)
        if record and self.down and request.method == "GET" and self.stalled(record.group(1)):
            return httpx.Response(200, json={"replicationStatus": "active", "type": "replication", "scheduleTime": 1})
        self.calls.append((host, path))
        answer = self.fake.respond(request)
        if self.down and request.method == "GET" and path.endswith("/children") and answer.status_code == 200:
            return self.altered(answer)
        return answer

    def altered(self, answer: httpx.Response) -> httpx.Response:
        """A listing as a provider cut off from the owner might show it: entries lost, or sizes not what they were."""
        body = answer.json()
        if self.listing_loses:
            body["children"] = body["children"][: -self.listing_loses]
        if self.listing_resizes:
            body["children"] = [{**child, "size": child["size"] + 1} for child in body["children"]]
        return httpx.Response(200, json=body)

    def holds_here(self, host: str, file_id: str) -> bool:
        provider = self.fake.provider_of(host)
        return bool(provider and self.fake.blocks.get((file_id, provider)))

    def stalled(self, transfer_id: str) -> bool:
        transfer = self.fake.transfers.get(transfer_id)
        return bool(transfer and transfer.replicates and not self.holders_up(transfer.file_id))

    def cannot_fetch(self, request: httpx.Request) -> httpx.Response:
        if self.reads_while_down == "refuse":
            return httpx.Response(500, json={"error": {"id": "internalServerError", "details": {}}})
        self.ticker.sleep(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("timed out")

    def client_http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.respond))

    def holds(self, path: str, provider_id: str) -> bool:
        """Whether a provider holds any block of the file at a path of the study space."""
        return bool(self.fake.blocks.get((self.fake.id_of(path), provider_id)))


class FakeServices:
    """A `FaultDriver` over the world: `services` maps a Compose service to the host it serves."""

    def __init__(
        self, world: OutageWorld, services: dict[str, str], on_stop: Callable[[str], None] | None = None
    ) -> None:
        self.world, self.services, self.on_stop = world, services, on_stop
        self.generation: dict[str, int] = dict.fromkeys(services, 1)
        self.running: dict[str, bool] = dict.fromkeys(services, True)
        self.calls: list[tuple[str, str]] = []  # ("stop" | "start" | "observe", service)
        self.fail_stop = False  # the stop command fails
        self.stop_has_no_effect = False  # the stop command returns and the service goes on running, and REST answering
        self.stays_down = False  # start returns and the service does not run again
        self.keeps_identity = False  # start brings the service back with the identity it had
        self.unreadable = False  # observe cannot read the service

    def stop(self, service: str) -> None:
        self.calls.append(("stop", service))
        if self.fail_stop:
            raise DriverError("the command to stop the service failed with exit code 1")
        if self.stop_has_no_effect:
            return
        self.running[service] = False
        self.world.down.add(self.services[service])
        if self.on_stop:
            self.on_stop(service)

    def start(self, service: str) -> None:
        self.calls.append(("start", service))
        if self.stays_down:
            return
        self.world.down.discard(self.services[service])
        if not self.running[service] and not self.keeps_identity:
            self.generation[service] += 1
        self.running[service] = True

    def observe(self, service: str) -> Observation:
        self.calls.append(("observe", service))
        if self.unreadable:
            raise DriverError("the container's inspection could not be read")
        return Observation(self.running[service], f"container-{service}@{self.generation[service]}")

    def serves(self, service: str) -> bool:
        """Whether the provider serves, as the real probe finds it: its service runs and its host answers."""
        self.calls.append(("serves", service))
        return self.running[service] and self.services[service] not in self.world.down

    def verbs(self) -> list[str]:
        """The stops and starts, in order."""
        return [verb for verb, _ in self.calls if verb in ("stop", "start")]


class WorldDaemon(FakeDaemon):
    """The fake Docker daemon behind the real Compose driver, whose providers' hosts are down while their containers do
    not run: `services` maps a Compose service to the host it serves. The daemon's clock is the test's `Ticker`."""

    def __init__(self, world: OutageWorld, services: dict[str, str]) -> None:
        super().__init__(world.ticker, tuple(services))
        self.world, self.hosts = world, services

    def sync(self) -> None:
        for service, host in self.hosts.items():
            if self.running(service):
                self.world.down.discard(host)
            else:
                self.world.down.add(host)

    def __call__(self, argv, *, timeout_s):
        try:
            return super().__call__(argv, timeout_s=timeout_s)
        finally:
            self.sync()


# -- what the agent meets: the mount of a sandbox on the fake host, and the tool server's relay -----------------------

PROVIDER_API = "/api/v3/oneprovider"
TOOLS_SERVER = Path(__file__).resolve().parent / "outage_tools_server.py"
REQUEST_LINE = "REQUEST = json.loads("


class AgentWorld:
    """What a sandbox's mount and the tool server see of the world, through the agent's local provider `local_host`.

    The mount reads a scratch file as the local provider would serve it (and places its bytes there, as a read through
    a mount does); with every provider that holds it down, it blocks to its deadline (`mount_down = "blocked"`) or
    fails with an errno (`mount_down = ("ETIMEDOUT", 110, "TimeoutError")`, as Python reports a mount's ETIMEDOUT). The
    tools answer from the providers' REST surface through the same transport, so a downed host is refused; while a host
    is down `tool_errors` makes a tool answer an error with that wording, and `tool_hangs` makes one never answer (the
    ticker moved by `hang_s` first). What each does is a stand-in's choice, not a fact about Onedata.

    The tools check the arguments the agent's side would pass, as a real server's caller must get them right: a
    replication's target must be the agent's local provider (never the provider taken down, nor the mirror), and
    `list_space_providers` must be asked about a space under test (`space_ids`, the fake's study space by default). A
    call that is not is answered an error and kept in `misused`, so a check that sends a wrong id cannot pass."""

    def __init__(
        self, world: OutageWorld, local_host: str, space_root: str, space_ids: tuple[str, ...] | None = None
    ) -> None:
        self.world, self.local_host, self.space_root = world, local_host, space_root.rstrip("/") + "/"
        self.space_ids = space_ids or (world.fake.SPACE_ID,)
        self.misused: list[tuple[str, str]] = []  # (tool, what was wrong) of each call with a wrong argument
        self.mount_down: Any = "blocked"
        self.tool_errors: dict[str, str] = {}
        self.tool_broken: dict[str, str] = {}  # answers an error with this wording whether or not anything is down
        self.tool_hangs: set[str] = set()
        self.hang_s = 0.0
        self.tool_calls: list[tuple[str, dict]] = []
        self.mount_ops: list[tuple[str, str]] = []

    # -- the mount ----------------------------------------------------------------------------------------------
    def exec_handler(self, container, argv, stdin):
        """The fake host's answer to a cell: a mount cell from the world, anything else as the fake host answers."""
        if not stdin or REQUEST_LINE not in stdin:
            return FakeHost._default_exec(container, argv, stdin)
        line = next(text for text in stdin.splitlines() if text.startswith(REQUEST_LINE))
        request = json.loads(ast.literal_eval(line[len(REQUEST_LINE) : -1]))
        rows = [self.mount_op(op, request["deadline_s"]) for op in request["ops"]]
        return CmdResult(0, json.dumps({"results": rows}) + "\n")

    def mount_op(self, op: dict, deadline_s: float) -> dict:
        self.mount_ops.append((op["op"], op["key"]))
        fake, row = self.world.fake, {"key": op["key"], "seconds": 0.01, "blocked": False}
        assert op["path"].startswith(self.space_root), "a mount cell read outside the space's directory"
        node = fake.id_of(op["path"][len(self.space_root) :])
        if op["op"] == "list":
            return {**row, "names": sorted(n.name for n in fake.nodes.values() if n.parent == node)}
        local = fake.provider_of(self.local_host)
        if not fake.blocks.get((node, local)) and not self.world.holders_up(node):
            if self.mount_down == "blocked":
                return {**row, "seconds": deadline_s, "blocked": True, "killed": True, "bytes": 0}
            name, number, kind = self.mount_down
            return {**row, "seconds": deadline_s, "error": name, "errno": number, "kind": kind, "bytes": 0}
        content = bytes(fake.nodes[node].content)
        fake.blocks[(node, local)] = [[0, len(content)]]
        return {**row, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}

    # -- the tools ----------------------------------------------------------------------------------------------
    def _call(self, method: str, path: str, **options: Any) -> httpx.Response:
        headers = {"X-Auth-Token": self.world.fake.admin_token}
        request = httpx.Request(method, f"https://{self.local_host}{PROVIDER_API}{path}", headers=headers, **options)
        return self.world.respond(request)

    def wrong_argument(self, name: str, arguments: dict) -> str | None:
        """What is wrong with a call's arguments, or None: the checks of the class docstring."""
        local = self.world.fake.provider_of(self.local_host)
        if name == "schedule_file_replication" and arguments.get("provider_id") != local:
            return "the replication's target is not the agent's local provider"
        if name == "list_space_providers" and arguments.get("space_id") not in self.space_ids:
            return "the space is not one under test"
        return None

    def tool(self, name: str, arguments: dict) -> dict:
        self.tool_calls.append((name, arguments))
        wrong = self.wrong_argument(name, arguments)
        if wrong is not None:
            self.misused.append((name, wrong))
            return {"error": f"invalid argument: {wrong}"}
        if self.world.down and name in self.tool_hangs:
            self.world.ticker.sleep(self.hang_s)
            return {"hang": True}
        if self.world.down and name in self.tool_errors:
            return {"error": self.tool_errors[name]}
        if name in self.tool_broken:
            return {"error": self.tool_broken[name]}
        try:
            if name == "list_space_providers":
                fake = self.world.fake
                rows = [
                    {"providerId": pid, "online": provider["domain"] not in self.world.down}
                    for pid, provider in fake.providers.items()
                ]
                return {"result": {"providers": rows}}
            if name == "get_file_distribution":
                answer = self._call("GET", f"/data/{arguments['file_id']}/distribution")
            elif name == "schedule_file_replication":
                body = {"type": "replication", "replicatingProviderId": arguments["provider_id"]}
                body.update(dataSourceType="file", fileId=arguments["file_id"])
                answer = self._call("POST", "/transfers", json=body)
            else:
                answer = self._call("GET", f"/transfers/{arguments['transfer_id']}")
        except httpx.HTTPError as error:
            return {"error": f"{type(error).__name__} talking to https://{self.local_host}"}
        if answer.status_code >= 300:
            return {"error": f"HTTP {answer.status_code} from https://{self.local_host}"}
        return {"result": answer.json()}


class ToolRelay:
    """The test's end of the stand-in tool server: an HTTP server on the loopback, in a thread of its own, that answers
    one relayed tool call at a time from an `AgentWorld` (the fakes are not thread-safe)."""

    def __init__(self, agent: AgentWorld) -> None:
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                asked = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with relay.lock:
                    reply = json.dumps(agent.tool(asked["tool"], asked["arguments"])).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args: Any) -> None:
                pass

        self.lock = threading.Lock()
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def freeze_tools_manifest(path: Path) -> None:
    """Freeze the stand-in server's manifest (its four tools) into a file, as `expcore.tools.manifest` freezes one."""
    params = StdioServerParameters(command=sys.executable, args=[str(TOOLS_SERVER), "--relay", "http://127.0.0.1:9"])
    path.write_text(FrozenManifest.from_live(asyncio.run(live_manifest(params))).dump(), encoding="utf-8")


KNOWLEDGE_RIGHTS = ["space_view", "space_read_data", "space_view_transfers", "space_schedule_replication"]
KNOWLEDGE_MANIFEST = "knowledge.manifest.json"


def with_agent(data: dict[str, Any], relay_url: str, *, allow: tuple[str, ...] = KNOWLEDGE_TOOLS) -> None:
    """An `edit` for `write_platform`: the default `[sandbox]` (its level `mounted` maps to the mount) and `[slots]`,
    and a `[toolserver]` that runs the stand-in tool server against `relay_url`, with a condition `knowledge` that
    allows the tools in `allow` (the four knowledge tools by default)."""
    add_sandbox(data)
    data["toolserver"]["args"] = [str(TOOLS_SERVER), "--relay", relay_url]
    condition = {"allow": list(allow), "rights": KNOWLEDGE_RIGHTS, "manifest": KNOWLEDGE_MANIFEST}
    data["toolserver"]["conditions"]["knowledge"] = condition


def add_slot_users(fake: Any, directory: Path, spaces: tuple[str | None, ...] = (None,)) -> None:
    """The slots' users, known to the fake with their credential files beside the platform file: each data-path user
    with the baseline rights, each tool-server user with the knowledge condition's, in each space of `spaces`."""
    for login, (password, user_id) in SLOT_USERS.items():
        file = directory / CREDENTIALS_DIR / f"{login}.pass"
        file.write_text(password + "\n", encoding="utf-8")
        file.chmod(0o600)
        fake.add_user(login, password, user_id)
        for space in spaces:
            fake.grant(user_id, KNOWLEDGE_RIGHTS if login.endswith("-tools") else BASELINE, space=space)


def agent_host(platform: Path, agent: AgentWorld) -> FakeHost:
    """The sandbox's fake host, knowing the configured images, whose cells the agent world answers."""
    settings = load_platform(platform, require_secrets=False).sandbox
    host = FakeHost()
    for ref in (settings.agent, settings.gate, settings.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(settings.oneclient.id)
    host.exec_handler = agent.exec_handler
    return host


def agent_options() -> dict[str, Any]:
    """The platform's options for a fast sandbox on the fake host: its own registry, no sampler, no real waits."""
    fast = {"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None}
    return {"registry": Registry(), "sandbox_options": fast}
