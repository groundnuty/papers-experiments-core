"""O86: a run's window on the federation's services is read by the platform's launch and recorded in its facts.

`SandboxPlatform(..., federation_watch=watch_for(...))` reads the federation before a run's sandbox exists, which is
before any injection the run's body makes, and reads it again once the body has ended, which is after the injector's
restore when its `running` is inside the launch (the documented order), and records the window in the sandbox's
`metadata()["infrastructure_facts"]["federation_window"]`. The tests run the real injector and the real watch over the
same fake services and the same journal, composed as an executor does: the run's admission, the launch, the injector,
the agent.
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import shutil
import threading
import uuid
from types import SimpleNamespace

import pytest
from apptainer_support import make_settings
from apptainer_support import options as node_options
from fake_host import FakeHost
from fake_node import FakeNode
from fault_support import OUTAGE_TARGET, SERVICE_NAMES, FakeServices, faults_table, injector_for
from sandbox_support import FAST, add_sandbox

from expcore.campaign import liveness
from expcore_onedata.admission import FederationLock
from expcore_onedata.client import PlatformError
from expcore_onedata.compose import CommandResult
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import InjectionJournal
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.fedwatch import watch_for
from expcore_onedata.sandbox import fedwindow
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.users import TokenJournal

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
ATTEMPT, EARLIER = 5, 6  # the store's ids of the run's attempt and of an earlier one
CAMPAIGN = SimpleNamespace(key=None)  # the key of the campaign this test registered in `liveness`
CEILING_S = 600.0
ZONE_SERVICE = "zn-svc"  # the backend's name for the zone: never in a fact
PROVIDER_SERVICES = SERVICE_NAMES  # providers[0], providers[1]
PROVIDER_NAMES = ("alpha", "beta")
SECRETS = (ZONE_SERVICE, *PROVIDER_SERVICES, *PROVIDER_NAMES, "stack-a", "compose.yaml", "cid-0", "cid-1", "cid-2")


@pytest.fixture
def config(tmp_path, fake):
    """The sandbox directory's platform document with a `[faults]` table, as `test_fault_launch` builds it."""

    def edit(data):
        add_sandbox(data)
        data["faults"] = faults_table(admission_timeout_s=10, session_drain_s=320)
        data["tokens"] = {"margin_s": 1125}

    return load_platform(write_platform(tmp_path, edit))


@pytest.fixture(autouse=True)
def campaign():
    """A campaign registered in this process, whose attempts 5 and 6 are running: what `liveness.running` runs a run as,
    so the injector writes the attempt as the owner of each injection (O80) and the window knows whose it is."""
    CAMPAIGN.key = liveness.register({ATTEMPT: "running", EARLIER: "running"}.get)
    yield
    liveness.unregister(CAMPAIGN.key)


def earlier_attempt():
    """The owner token of an earlier attempt of the campaign, as the injector would have written it."""
    return {"process": liveness.this_process(), "campaign": CAMPAIGN.key, "attempt_id": EARLIER}


class Ctx:
    """A call context that notes each wait it is asked for."""

    def __init__(self):
        self.waits: list[str] = []

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        self.waits.append(kind)
        yield


class Client:
    """The platform client's `get`, as far as the watch's liveness read uses it."""

    def __init__(self, failing=False):
        self.failing = failing

    def get(self, path, *, provider=None, timeout_s=None, retry=None, **rest):
        if self.failing:
            raise PlatformError("GET zone failed: the connection was refused")
        return {"version": "21.02"}


class Containers:
    """What `docker compose ps` and `docker inspect` answer for the injector's own fake services: one container each,
    whose start time is its start count, so a restart is a new start time. `broken` makes every command fail."""

    def __init__(self, services: FakeServices, *, broken: bool = False):
        self.services, self.broken = services, broken

    def __call__(self, argv, *, timeout_s):
        if self.broken:
            return CommandResult(1, "", "docker: could not reach the daemon")
        if "ps" in argv:
            return CommandResult(0, self.services.identity(argv[-1]).split("@")[0] + "\n")
        for service in self.services._load():
            cid, _, count = self.services.identity(service).partition("@start-")
            if cid == argv[-1]:
                return CommandResult(0, json.dumps([self._document(cid, int(count), self.services.running(service))]))
        raise AssertionError(f"a command that is not a read: {argv}")

    @staticmethod
    def _document(cid, count, running):
        state = {
            "Status": "running" if running else "exited", "Running": running, "Restarting": False, "Paused": False,
            "OOMKilled": False, "ExitCode": 0, "StartedAt": f"2026-11-17T{count:02d}:00:00Z",
            "FinishedAt": "0001-01-01T00:00:00Z" if running else "2026-11-17T23:00:00Z", "Error": "",
            "Health": {"Status": "healthy"},
        }  # fmt: skip
        return {"Id": cid, "RestartCount": 0, "State": state}


class Federation:
    """The injector's fake services and the watch over them: one state, one journal, and the order things happened."""

    def __init__(self, config, *, broken=False, client=None):
        self.settings = load_faults(config.file, config)
        self.services = FakeServices((*PROVIDER_SERVICES, ZONE_SERVICE))
        self.events: list[str] = []
        self.services.on_stop = lambda service: self.events.append("stop")
        started = self.services.start

        def start(service):
            self.events.append("start")
            started(service)

        self.services.start = start
        self.watch = watch_for(
            config, self.settings, client or Client(), zone=ZONE_SERVICE,
            runner=Containers(self.services, broken=broken),
        )  # fmt: skip
        self.config = config

    def injector(self):
        return injector_for(self.config.file, self.services)

    def spy(self, watch=None):
        """The watch, noting each read and window in the order of the run's events."""
        return Spy(watch or self.watch, self.events)


class Spy:
    def __init__(self, watch, events):
        self.watch, self.events = watch, events

    def read(self):
        self.events.append("read")
        return self.watch.read()

    def window(self, start, *, owner=None):
        self.events.append("window")
        return self.watch.window(start, owner=owner)


class Raising:
    """A watch whose reads fail with a message that holds a name a fact must never carry."""

    def __init__(self, *, read=False, window=False):
        self.fail_read, self.fail_window = read, window

    def read(self):
        if self.fail_read:
            raise RuntimeError("the zone zn-svc at zone.internal.example refused the read")
        return {"format": "expcore-fedwatch/1"}

    def window(self, start, *, owner=None):
        if self.fail_window:
            raise RuntimeError("the zone zn-svc at zone.internal.example refused the read")
        return {"format": "expcore-fedwatch/1"}


def build(config, issuer, slots, tmp_path, watch):
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    platform = SandboxPlatform(
        config,
        issuer,
        host,
        slots=slots,
        journal=TokenJournal(tmp_path / "leases"),
        registry=Registry(),
        sandbox_options={"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None},
        clock=lambda: NOW,
        events=lambda kind, detail: None,
        federation_watch=watch,
    )
    return platform, host


def window_of(sandbox) -> dict:
    return sandbox.metadata()["infrastructure_facts"][fedwindow.KEY]


def launching(platform, ctx=None):
    return platform.launch(ctx or Ctx(), run_key=RUN["run_key"], level="isolated", ceiling_s=CEILING_S)


@contextlib.asynccontextmanager
async def admitted(settings, *, exclusive):
    """The run as the campaign runs it: an attempt, admitted to the federation."""
    with liveness.running(CAMPAIGN.key, ATTEMPT):
        async with FederationLock(settings).admitted(exclusive=exclusive, timeout_s=10, target=OUTAGE_TARGET):
            yield


def run(platform, settings, body, *, exclusive=False, ctx=None):
    """The run's admission, the launch, then `body(sandbox)`; the sandbox once the launch has ended."""
    seen = []

    async def go():
        async with admitted(settings, exclusive=exclusive), launching(platform, ctx) as box:
            seen.append(box)
            await body(box)
        return seen[0]

    return asyncio.run(go())


async def nothing(box):
    return None


def test_o86_a_run_on_a_stable_federation_carries_a_stable_window(config, issuer, slots, tmp_path):
    """O86: no service changed between the read before the run and the one after it: every run's facts say stable,
    with each service's status, in the same shape as the other facts, as plain JSON."""
    federation = Federation(config)
    platform, host = build(config, issuer, slots, tmp_path, federation.watch)
    box = run(platform, federation.settings, nothing)
    window = window_of(box)
    assert window["version"] == 1
    assert window["verdict"] == "stable"
    assert window["backend"] == "compose"
    assert (window["findings"], window["unknown"]) == ([], [])
    assert sorted(window["services"]) == ["providers[0]", "providers[1]", "zone"]
    assert all(seen["status"] == "stable" and seen["restarts"] == [] for seen in window["services"].values())
    assert window["services"]["zone"]["start"] == window["services"]["zone"]["end"] == "running"
    assert window["window"]["start"]
    assert window["window"]["end"]
    assert json.loads(json.dumps(window)) == window
    assert not host.containers


def test_o86_an_s3_shaped_runs_own_outage_is_explained_by_its_own_injection(config, issuer, slots, tmp_path):
    """O86: the read comes before the injection, the injector stops and restarts providers[1] inside the launch, and
    the window comes after the restore: the restart is explained by the run's own injection and labelled `outage`."""
    federation = Federation(config)
    platform, host = build(config, issuer, slots, tmp_path, federation.spy())
    injector = federation.injector()

    async def agent(box):
        async with injector.running(RUN):
            federation.events.append("agent")

    box = run(platform, federation.settings, agent, exclusive=True)
    assert federation.events == ["read", "stop", "agent", "start", "window"]
    window = window_of(box)
    assert window["verdict"] == "restarted"
    assert window["findings"] == []
    hit = window["services"]["providers[1]"]
    assert hit["status"] == "restarted"
    assert [(r["label"], r["explained"]) for r in hit["restarts"]] == [("outage", True)]
    assert "this run's own" in hit["restarts"][0]["reason"]
    assert (window["ownership"], hit["held"], window["restore"]) == ("run", None, None)
    assert hit["start"] == hit["end"] == "running"
    assert window["services"]["providers[0]"]["status"] == window["services"]["zone"]["status"] == "stable"
    assert window["journal"]["lines_end"] > window["journal"]["lines_start"]
    assert not host.containers


def pilots_order(federation, platform, body=None):
    """The pilot's S3 order: prepare (admission, recovery, drain), the injection (which returns once the target reads
    not running), the launch with the agent inside it, the launch's exit, and only then the restore, verified."""
    injector = federation.injector()
    seen = []

    def inject():
        with injector.prepared(RUN) as prepared:
            prepared.inject()

    async def go():
        async with admitted(federation.settings, exclusive=True):
            await asyncio.to_thread(inject)
            async with launching(platform) as box:
                seen.append(box)
                if body is not None:
                    await body(box)
            return await asyncio.to_thread(injector.finish)

    restore = asyncio.run(go())
    return seen[0], restore, injector


def test_o86_in_the_pilots_order_the_target_is_held_down_by_the_runs_own_injection_across_the_window(
    config, issuer, slots, tmp_path
):
    """O86: the injection is made before the launch and restored after it: the target is down at the window's start and
    at its end, with no restart inside it. The window is `held` for the target, `stable` for every other service, with
    no finding, the reason says the run owns the injection, and it points at where the restore is recorded: the
    restore comes after the window and is the run's own to prove (`restore_verified` of the injector's snapshot)."""
    federation = Federation(config)
    platform, host = build(config, issuer, slots, tmp_path, federation.spy())
    evidence = tmp_path / "evidence"

    async def agent(box):
        box.collect(evidence)

    box, restore, injector = pilots_order(federation, platform, agent)
    assert federation.events == ["stop", "read", "window", "start"]  # the restore is after the window
    window = window_of(box)
    assert window["verdict"] == "held"
    assert (window["findings"], window["unknown"], window["ownership"]) == ([], [], "run")
    target = window["services"]["providers[1]"]
    assert (target["status"], target["start"], target["end"], target["restarts"]) == (
        "held", "not_running", "not_running", [],
    )  # fmt: skip
    assert target["held"]["injections"] == 1
    assert target["held"]["opened"] == "before the window"
    assert "this run owns" in target["held"]["reason"]
    assert [window["services"][label]["status"] for label in ("zone", "providers[0]")] == ["stable", "stable"]
    assert window["restore"]["instrument"] == "fault_injector"
    assert window["restore"]["field"] == "restore_verified"
    assert restore["ok"] is True
    assert injector.snapshot()["restore_verified"] is True  # what the window points at
    assert json.loads((evidence / fedwindow.FILE).read_text(encoding="utf-8")) == window
    assert not host.containers


@pytest.mark.parametrize("owner", ["another attempt's", "no owner"])
def test_o86_a_target_held_down_by_an_injection_that_is_not_the_runs_is_a_finding(
    config, issuer, slots, tmp_path, owner
):
    """O86: the target was already down, held by an earlier attempt's injection or by a line written before owners were
    recorded: neither is this run's, so neither holds it down for the window, and it is a finding, never `held`."""
    federation = Federation(config)
    platform, host = build(config, issuer, slots, tmp_path, federation.watch)
    leave_open(federation, PROVIDER_SERVICES[1], owner=earlier_attempt() if owner == "another attempt's" else None)

    box = run(platform, federation.settings, nothing, exclusive=True)
    window = window_of(box)
    assert window["verdict"] == "finding"
    assert window["services"]["providers[1]"]["status"] == "down"
    assert window["services"]["providers[1]"]["held"] is None
    assert any("providers[1]: not running at the window's end" in text for text in window["findings"])
    assert not host.containers


def leave_open(federation, service, *, owner):
    """An injection of `service` that was made, and not cleared, before the run: as another attempt's, or an older
    version's, journal line, with the service stopped."""
    journal = InjectionJournal(federation.settings.journal)
    entry = uuid.uuid4().hex
    line = {
        "target": OUTAGE_TARGET,
        "service": service,
        "identity_sha256": "f" * 64,
        "holder": {"pid": 1, "boot_id": None},
    }
    journal.append({"event": "injecting", "id": entry, **line, **({} if owner is None else {"owner": owner})})
    journal.append({"event": "stopping", "id": entry})
    journal.append({"event": "injected", "id": entry})
    federation.services.stop(service)


def test_o86_in_the_pilots_order_a_second_service_that_restarts_is_a_finding(config, issuer, slots, tmp_path):
    """O86: only the injection's target may be held or restarted: a provider that restarts in an injecting run with no
    injection of its own is a finding, whatever holds the target."""
    federation = Federation(config)
    platform, _ = build(config, issuer, slots, tmp_path, federation.watch)

    async def outside(box):
        federation.services.stop(PROVIDER_SERVICES[0])
        federation.services.start(PROVIDER_SERVICES[0])

    box, _, _ = pilots_order(federation, platform, outside)
    window = window_of(box)
    assert window["verdict"] == "finding"
    assert window["services"]["providers[1]"]["status"] == "held"
    assert [r["label"] for r in window["services"]["providers[0]"]["restarts"]] == ["finding"]


def test_o86_a_restart_the_journal_does_not_explain_is_a_finding(config, issuer, slots, tmp_path):
    """O86: a service restarted outside the injector (no journal line) is a finding, never an outage."""
    federation = Federation(config)
    platform, _ = build(config, issuer, slots, tmp_path, federation.watch)

    async def outside(box):
        federation.services.stop(PROVIDER_SERVICES[0])
        federation.services.start(PROVIDER_SERVICES[0])

    window = window_of(run(platform, federation.settings, outside))
    assert window["verdict"] == "finding"
    assert [r["label"] for r in window["services"]["providers[0]"]["restarts"]] == ["finding"]
    assert any(text.startswith("providers[0]: restart not explained") for text in window["findings"])
    assert window["services"]["providers[1]"]["status"] == "stable"


@pytest.mark.parametrize("how", ["every command fails", "the read raises", "the window raises"])
def test_o86_a_federation_that_cannot_be_read_is_unknown_and_the_run_finishes(config, issuer, slots, tmp_path, how):
    """O86: an unreadable federation (a failing Docker, a watch that raises at either end) makes the window unknown,
    with a reason that names no message of the failure, and the run goes on: its body's value is returned, its sandbox
    is stopped and nothing is raised."""
    federation = Federation(config, broken=how == "every command fails")
    watch = {
        "every command fails": federation.watch,
        "the read raises": Raising(read=True, window=True),
        "the window raises": Raising(window=True),
    }[how]
    platform, host = build(config, issuer, slots, tmp_path, watch)
    ran = []

    async def body(box):
        ran.append(box)

    box = run(platform, federation.settings, body)
    window = window_of(box)
    assert ran == [box]
    assert window["verdict"] == "unknown"
    assert window["unknown"]
    text = json.dumps(window)
    assert "zone.internal.example" not in text
    assert "refused the read" not in text
    if how != "every command fails":
        assert "RuntimeError" in text
    assert not host.containers


def test_o86_a_start_reading_that_failed_leaves_the_window_unknown_never_stable(config, issuer, slots, tmp_path):
    """O86: the window that follows a read that could not be made has no start, and says so, whatever the end shows."""
    federation = Federation(config)
    real = federation.watch
    starts = []

    class FirstReadFails:
        def read(self):
            raise RuntimeError("no")

        def window(self, start, *, owner=None):
            starts.append(start)
            return real.window(start, owner=owner)

    platform, _ = build(config, issuer, slots, tmp_path, FirstReadFails())
    window = window_of(run(platform, federation.settings, nothing))
    assert starts == [None]
    assert window["verdict"] == "unknown"
    assert any("RuntimeError" in text for text in window["unknown"])
    assert window["window"]["start"] is None


def test_o86_a_bodys_own_error_stands_when_the_window_cannot_be_read(config, issuer, slots, tmp_path):
    """O86: the run's body raised and the window cannot be read: the body's error is what is raised, the sandbox is
    stopped, and the facts still say unknown."""
    federation = Federation(config)
    platform, host = build(config, issuer, slots, tmp_path, Raising(window=True))
    seen = []

    async def failing(box):
        seen.append(box)
        raise ValueError("the agent's own failure")

    with pytest.raises(ValueError, match="the agent's own failure"):
        run(platform, federation.settings, failing)
    assert window_of(seen[0])["verdict"] == "unknown"
    assert not host.containers


def test_o86_a_cancelled_run_is_stopped_and_its_window_says_it_was_not_read(config, issuer, slots, tmp_path):
    """O86: a cancellation during the body reads no window (nothing blocking is awaited under it), records one that
    says so, and the sandbox is still stopped."""
    federation = Federation(config)
    platform, host = build(config, issuer, slots, tmp_path, federation.spy())
    seen = []

    async def go():
        async def body(box):
            seen.append(box)
            await asyncio.sleep(60)

        task = asyncio.ensure_future(run_in_loop(platform, federation.settings, body))
        while not seen:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert federation.events == ["read"]
    assert window_of(seen[0])["verdict"] == "unknown"
    assert "cancelled" in json.dumps(window_of(seen[0]))
    assert not host.containers


def test_o86_a_cancellation_during_the_closing_read_still_records_the_window_and_stops(config, issuer, slots, tmp_path):
    """O86: the body has ended and the task is cancelled while the window is being read: the window says it was
    cancelled, the sandbox is stopped, and the cancellation is what is raised."""
    federation = Federation(config)
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def read(self):
            return {}

        def window(self, start, *, owner=None):
            entered.set()
            release.wait(10)
            return {}

    platform, host = build(config, issuer, slots, tmp_path, Slow())
    seen = []

    async def go():
        async def body(box):
            seen.append(box)

        task = asyncio.ensure_future(run_in_loop(platform, federation.settings, body))
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(go())
    window = window_of(seen[0])
    assert window["verdict"] == "unknown"
    assert window["unknown"] == [fedwindow.CANCELLED]
    assert not host.containers


def test_o86_a_cancellation_during_the_opening_read_creates_nothing_and_keeps_no_share(config, issuer, slots, tmp_path):
    """O86, O11: cancelled while the window's start is being read, nothing of the run exists (no token, no
    container) and the admission's share the launch took is given up: the federation can be taken exclusively."""
    federation = Federation(config)
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def read(self):
            entered.set()
            release.wait(10)
            return {}

        def window(self, start, *, owner=None):
            raise AssertionError("the run never began")

    platform, host = build(config, issuer, slots, tmp_path, Slow())
    lock = FederationLock(federation.settings)

    async def go():
        async def body(box):
            raise AssertionError("the body never ran")

        task = asyncio.ensure_future(run_in_loop(platform, federation.settings, body))
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(go())
    assert not host.containers
    taken = lock.try_acquire(exclusive=True)
    assert taken is not None
    taken.close()


async def run_in_loop(platform, settings, body):
    async with admitted(settings, exclusive=False), launching(platform) as box:
        await body(box)


def test_o86_an_executor_whose_running_encloses_the_launch_reads_held_never_stable_or_a_finding(
    config, issuer, slots, tmp_path
):
    """O86: `running` encloses the launch, so the target is down before the read and restored after the window: the
    run's own open injection holds it down across the window, `held`, explained: not `stable`, not a finding."""
    federation = Federation(config)
    platform, host = build(config, issuer, slots, tmp_path, federation.watch)
    injector = federation.injector()
    seen = []

    async def go():
        async with admitted(federation.settings, exclusive=True), injector.running(RUN), launching(platform) as box:
            seen.append(box)

    asyncio.run(go())
    window = window_of(seen[0])
    assert (window["verdict"], window["findings"]) == ("held", [])
    assert window["services"]["providers[1]"]["status"] == "held"
    assert window["services"]["providers[1]"]["end"] == "not_running"
    assert not host.containers


def test_o86_the_waits_are_the_lifecycle_waits_and_none_without_a_watch(config, issuer, slots, tmp_path):
    """O86: the reads are infrastructure time, never the agent's: each is inside a sandbox_lifecycle wait. A platform
    given no watch enters the two waits it always did, and records the gap (`unknown`, no watch configured)."""
    federation = Federation(config)
    platform, _ = build(config, issuer, slots, tmp_path, federation.watch)
    ctx = Ctx()
    run(platform, federation.settings, nothing, ctx=ctx)
    assert ctx.waits == ["sandbox_lifecycle"] * 4  # the read, the start, the window, the stop
    bare, _ = build(config, issuer, slots, tmp_path, None)
    ctx = Ctx()
    box = run(bare, federation.settings, nothing, ctx=ctx)
    assert ctx.waits == ["sandbox_lifecycle"] * 2
    assert window_of(box)["reason"] == fedwindow.NO_WATCH


def test_o86_no_fact_names_a_service_a_container_an_address_or_a_provider(config, issuer, slots, tmp_path):
    """O86: the recorded window holds labels (`zone`, `providers[i]`), statuses and counts: none of the names the
    backend, the configuration or the injector's journal use, and no URL, whatever the run met."""
    federation = Federation(config, client=Client(failing=True))
    platform, _ = build(config, issuer, slots, tmp_path, federation.watch)
    injector = federation.injector()

    async def agent(box):
        async with injector.running(RUN):
            pass

    text = json.dumps(window_of(run(platform, federation.settings, agent, exclusive=True)))
    for name in (*SECRETS, "http", "://", str(tmp_path)):
        assert name not in text


def test_o86_the_window_is_persisted_beside_the_metadata_json_of_the_runs_evidence(config, issuer, slots, tmp_path):
    """O86: `collect` writes `metadata.json` before the restore, so the closed window is written at the launch's exit
    as `federation-window.json` beside it: the same facts as `metadata()`, an S3-shaped run's outage explained, and
    the `metadata.json` collect wrote says the window was not closed yet and where it is."""
    federation = Federation(config)
    platform, _ = build(config, issuer, slots, tmp_path, federation.watch)
    injector = federation.injector()
    evidence = tmp_path / "evidence"

    async def agent(box):
        async with injector.running(RUN):
            box.collect(evidence)  # inside the injector, before its restore, as the documented order collects

    box = run(platform, federation.settings, agent, exclusive=True)
    persisted = json.loads((evidence / fedwindow.FILE).read_text(encoding="utf-8"))
    assert persisted == window_of(box)
    assert persisted["verdict"] == "restarted"
    assert [(r["label"], r["explained"]) for r in persisted["services"]["providers[1]"]["restarts"]] == [
        ("outage", True)
    ]
    collected = json.loads((evidence / "metadata.json").read_text(encoding="utf-8"))
    placeholder = collected["infrastructure_facts"][fedwindow.KEY]
    assert placeholder["verdict"] == "unknown"
    assert fedwindow.FILE in placeholder["reason"]
    assert not [path for path in evidence.iterdir() if path.name.startswith(".")]  # no temporary file is left


def test_o86_every_collected_directory_that_still_exists_gets_the_file_and_a_run_never_collected_has_none(
    config, issuer, slots, tmp_path
):
    """O86: a study may collect into a scratch directory (a side file's staging) as well as the run's own: each
    directory `collect` wrote into gets the file beside its `metadata.json`, one that is gone is skipped, and a run
    that never collected has the window in its metadata alone."""
    federation = Federation(config)
    platform, _ = build(config, issuer, slots, tmp_path, federation.watch)
    first, scratch = tmp_path / "first", tmp_path / "scratch"

    async def body(box):
        box.collect(first)
        box.collect(scratch)
        shutil.rmtree(scratch)

    box = run(platform, federation.settings, body)
    assert json.loads((first / fedwindow.FILE).read_text(encoding="utf-8")) == window_of(box)
    assert not scratch.exists()
    uncollected = run(
        build(config, issuer, slots, tmp_path / "other", federation.watch)[0], federation.settings, nothing
    )
    assert window_of(uncollected)["verdict"] == "stable"
    assert sorted(path.parent.name for path in tmp_path.rglob(fedwindow.FILE)) == ["first"]


def test_o86_a_file_that_cannot_be_written_is_logged_and_never_fails_the_run(
    config, issuer, slots, tmp_path, monkeypatch, caplog
):
    """O86: the window's file cannot be written (the rename fails): the run finishes, the window is in its metadata,
    and the failure is logged with the error's type."""

    def refuse(source, target):
        raise PermissionError("the directory is read-only")

    monkeypatch.setattr(fedwindow, "os", SimpleNamespace(replace=refuse))
    federation = Federation(config)
    platform, host = build(config, issuer, slots, tmp_path, federation.watch)

    async def body(box):
        box.collect(tmp_path / "evidence")

    with caplog.at_level(logging.ERROR, logger=fedwindow.log.name):
        box = run(platform, federation.settings, body)
    assert window_of(box)["verdict"] == "stable"
    assert "could not be written beside the run's evidence (PermissionError)" in caplog.text
    assert "read-only" not in caplog.text
    assert not host.containers


def test_o86_a_write_that_fails_in_any_way_leaves_the_window_recorded_and_the_sandbox_stopped(
    config, issuer, slots, tmp_path, monkeypatch
):
    """O86: persisting runs before the stop, so it raises nothing, an error that is no `OSError` included (a stale
    handle on a shared file system): the window stays what the watch said, never "interrupted", the sandbox is stopped
    and nothing is raised; whether the body returned or was cancelled."""

    def explode(source, target):
        raise RuntimeError("a stale handle")

    monkeypatch.setattr(fedwindow, "os", SimpleNamespace(replace=explode))
    federation = Federation(config)
    platform, host = build(config, issuer, slots, tmp_path, federation.watch)
    evidence = tmp_path / "evidence"

    async def body(box):
        box.collect(evidence)

    box = run(platform, federation.settings, body)
    assert window_of(box)["verdict"] == "stable"
    assert not host.containers
    assert not [path for path in evidence.iterdir() if path.name.startswith(".")]  # no partial file is left

    seen = []

    async def cancelled():
        async def waiting(box):
            seen.append(box)
            box.collect(tmp_path / "second")
            await asyncio.sleep(60)

        task = asyncio.ensure_future(run_in_loop(platform, federation.settings, waiting))
        while not seen:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())
    assert window_of(seen[0])["unknown"] == [fedwindow.CANCELLED]
    assert not host.containers


def test_o86_a_platform_with_no_watch_records_the_gap_on_both_backends_and_writes_no_file(
    config, issuer, slots, tmp_path
):
    """O86: no run has the key absent: with no watch the fact is `unknown` with the reason, so a post-check sees the
    gap, in the sandbox's metadata (and so in the `metadata.json` collect writes) and in no extra file."""
    federation = Federation(config)
    docker, _ = build(config, issuer, slots, tmp_path, None)
    batch = build_apptainer(config, issuer, slots, tmp_path, None)

    for name, platform in (("docker", docker), ("apptainer", batch)):

        async def body(box, name=name):
            box.collect(tmp_path / name)

        fact = window_of(run(platform, federation.settings, body))
        assert (fact["verdict"], fact["reason"], fact["unknown"]) == (
            "unknown",
            fedwindow.NO_WATCH,
            [fedwindow.NO_WATCH],
        )
        collected = json.loads((tmp_path / name / "metadata.json").read_text(encoding="utf-8"))
        assert collected["infrastructure_facts"][fedwindow.KEY] == fact
    assert not list(tmp_path.rglob(fedwindow.FILE))


# --- the Apptainer backend, and an HPC federation -----------------------------------------------------------------


def build_apptainer(config, issuer, slots, tmp_path, watch):
    """The same platform on a batch node's sandbox (`backend = "apptainer"`), whose launch is the shared one."""
    (tmp_path / "node").mkdir()
    node_config = dataclasses.replace(config, sandbox=make_settings(tmp_path / "node"))
    return SandboxPlatform(
        node_config,
        issuer,
        FakeNode(),
        slots=slots,
        journal=TokenJournal(tmp_path / "leases"),
        events=lambda kind, detail: None,
        clock=lambda: NOW,
        registry=Registry(),
        sandbox_options={k: v for k, v in node_options().items() if k != "registry"},
        federation_watch=watch,
    )


def test_o86_an_apptainer_run_carries_the_window_beside_its_departures(config, issuer, slots, tmp_path):
    """O86: the Apptainer backend's `infrastructure_facts` keep every key they had and gain the window."""
    federation = Federation(config)
    platform = build_apptainer(config, issuer, slots, tmp_path, federation.watch)
    facts = run(platform, federation.settings, nothing).metadata()["infrastructure_facts"]
    assert facts[fedwindow.KEY]["verdict"] == "stable"
    assert (facts["backend"], facts["dns"]) == ("apptainer", "none")
    assert "dns_none" in facts["facts"]
    assert fedwindow.KEY not in facts["facts"]  # a fact of its own, not one of the node's departures


def hpc_record(step, *, launch=1, started="2026-11-17T01:00:00", stops=0, kind="op"):
    stopped = [{"requested_utc": "2026-11-17T02:30:00Z", "confirmed_utc": "2026-11-17T02:30:05Z", "stopped": True}]
    return {
        "kind": kind, "launch": launch, "step": step, "running": True, "state": "RUNNING", "started_at": started,
        "ended_at": None, "exit_code": None, "state_differs": False, "stops": stopped * stops, "unknown": None,
    }  # fmt: skip


class Cluster:
    """The document `hpcobserve.py` prints, as the watch's runner answers it; the body changes `services`."""

    def __init__(self):
        self.services = {
            ZONE_SERVICE: hpc_record("4242.3", kind="oz"),
            PROVIDER_SERVICES[0]: hpc_record("4242.5"),
            PROVIDER_SERVICES[1]: hpc_record("4242.6"),
        }

    def __call__(self, argv, *, timeout_s):
        doc = {"format": "expcore-hpc-observe/1", "at": "2026-11-17T04:00:00Z", "services": self.services}
        return CommandResult(0, json.dumps(doc) + "\n", "")


def test_o86_an_hpc_federations_window_is_read_the_same_way_on_a_batch_nodes_sandbox(config, issuer, slots, tmp_path):
    """O86: an HPC federation (read through the observe command) and an Apptainer sandbox: a provider's restart that
    the body's injection and the state's recorded stop explain is labelled outage, and nothing in the facts names a
    step, a node or a service."""
    settings = dataclasses.replace(load_faults(config.file, config), backend="hpc")
    cluster = Cluster()
    watch = watch_for(
        config, settings, Client(), zone=ZONE_SERVICE, hpc_command=["python", "-I", "hpcobserve.py", "--root", "/r"],
        runner=cluster,
    )  # fmt: skip
    platform = build_apptainer(config, issuer, slots, tmp_path, watch)

    async def body(box):
        entry = uuid.uuid4().hex
        journal = InjectionJournal(settings.journal)
        journal.append({"event": "injecting", "id": entry, "target": "a-target", "service": PROVIDER_SERVICES[1],
                        "identity_sha256": "f" * 64, "holder": {"pid": 1, "boot_id": None},
                        "owner": liveness.owner()})  # fmt: skip
        for event in ("stopping", "injected"):
            journal.append({"event": event, "id": entry})
        journal.append({"event": "cleared", "id": entry, "how": "restored"})
        cluster.services[PROVIDER_SERVICES[1]] = hpc_record("4242.9", launch=2, started="2026-11-17T03:00:00", stops=1)

    window = window_of(run(platform, settings, body, exclusive=True))
    assert window["backend"] == "hpc"
    assert window["verdict"] == "restarted"
    assert [(r["label"], r["explained"]) for r in window["services"]["providers[1]"]["restarts"]] == [("outage", True)]
    text = json.dumps(window)
    for name in (*SECRETS, "4242", "hpcobserve", "/r"):
        assert name not in text


def test_o86_an_apptainer_runs_window_is_persisted_beside_its_metadata_json(config, issuer, slots, tmp_path):
    """O86: the Apptainer backend's collection is tracked as Docker's: the window is written beside metadata.json."""
    federation = Federation(config)
    platform = build_apptainer(config, issuer, slots, tmp_path, federation.watch)
    evidence = tmp_path / "evidence"

    async def body(box):
        box.collect(evidence)

    box = run(platform, federation.settings, body)
    persisted = json.loads((evidence / fedwindow.FILE).read_text(encoding="utf-8"))
    assert persisted == window_of(box)
    assert persisted["verdict"] == "stable"
    collected = json.loads((evidence / "metadata.json").read_text(encoding="utf-8"))
    assert collected["infrastructure_facts"][fedwindow.KEY]["verdict"] == "unknown"
