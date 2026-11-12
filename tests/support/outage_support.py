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
- `FakeMount` reads and lists through "the mount" from the same world.

Every name is invented, as in `expcore_onedata.testing`.
"""

import re
from collections.abc import Callable
from typing import Any

import httpx
from fault_support import FakeDaemon

from expcore_onedata.compose import DriverError, Observation
from expcore_onedata.testing import FakeData, Ticker

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

    def verbs(self) -> list[str]:
        """The stops and starts, in order."""
        return [verb for verb, _ in self.calls if verb != "observe"]


class FakeMount:
    """The mount, over the same world: it reads a scratch file by its path in the study space, as the local provider
    would serve it, and lists a scratch directory. It stalls, as the world does, when no up provider holds the bytes."""

    def __init__(self, world: OutageWorld, local_id: str) -> None:
        self.world, self.local_id = world, local_id
        self.error: OSError | None = None  # when set, every read fails with it
        self.reads: list[str] = []

    def read(self, space: Any, path: str, *, timeout_s: float) -> bytes:
        self.reads.append(path)
        if self.error is not None:
            raise self.error
        fake = self.world.fake
        file_id = fake.id_of(path)
        if not fake.blocks.get((file_id, self.local_id)) and not self.world.holders_up(file_id):
            self.world.ticker.sleep(timeout_s)
            raise TimeoutError("the mount did not answer")
        fake.blocks[(file_id, self.local_id)] = [[0, len(fake.nodes[file_id].content)]]  # a read through it places
        return bytes(fake.nodes[file_id].content)

    def listdir(self, space: Any, path: str, *, timeout_s: float) -> list[str]:
        fake = self.world.fake
        directory = fake.id_of(path)
        return [node.name for node in fake.nodes.values() if node.parent == directory]


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
