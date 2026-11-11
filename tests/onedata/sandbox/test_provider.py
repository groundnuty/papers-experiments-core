"""The sandbox as the platform offers it: leased slots, tokens by data path, waits, and the teardown probe (C19)."""

import asyncio
import contextlib
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from platform_support import NOW
from sandbox_support import COMPLETE_LEVEL, FAST

from expcore.campaign.lease import LeasePool
from expcore.errors import InfraError
from expcore_onedata.sandbox.api import DataPathMode, SandboxError
from expcore_onedata.sandbox.hostops import CmdResult, MountTableError, fuse_mounts, parse_mountinfo
from expcore_onedata.sandbox.provider import SandboxPlatform, SandboxProbe
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.tokens import TokenRefused
from expcore_onedata.users import GROUP, JOURNAL, SAFETY_MARGIN_S, TokenJournal

CEILING_S = 600.0
KEY = "slot-a"  # the first slot of the platform document: the key a pool grants first


class RecordingContext:
    """A call context that records each wait it is asked for, with a marker for when a body runs inside it."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        self.events.append(("enter", kind))
        try:
            yield
        finally:
            self.events.append(("exit", kind))


@pytest.fixture
def stage(config, issuer, fake, slots, tmp_path):
    """A platform over a fake host and the fake federation, with the host knowing the configured images, the slots of
    the document, a token journal beside the leases, and a clock the test moves (it starts where the issuer's is)."""
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    options = {"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None}
    registry = Registry()
    journal = TokenJournal(tmp_path / "leases")
    now = [NOW]
    events: list[tuple[str, str]] = []

    def record_event(kind, detail):
        events.append((kind, detail))

    platform = SandboxPlatform(
        config,
        issuer,
        host,
        slots=slots,
        journal=journal,
        events=record_event,
        registry=registry,
        sandbox_options=options,
        clock=lambda: now[0],
    )
    return SimpleNamespace(
        host=host,
        platform=platform,
        config=config,
        s=s,
        fake=fake,
        registry=registry,
        slots=slots,
        journal=journal,
        events=events,
        record_event=record_event,
        now=now,
        issuer=issuer,
        leases=tmp_path / "leases",
    )


def tokens_ended(stage):
    """Move the platform's clock past every journalled end plus the margin: the slots' tokens have all ended."""
    ends = [stage.journal.latest_expiry(key) or 0 for key in stage.slots.keys_of(GROUP)]
    stage.now[0] = max([NOW, *ends]) + SAFETY_MARGIN_S + 1


def lease_pool(stage, tmp_path):
    return LeasePool(
        tmp_path / "leases",
        stage.platform.lease_keys,
        stage.platform.probe(),
        clock=lambda: 0.0,
        sleep=lambda s: None,
        quarantine_retry_s=0.0,
    )


def run_launch(stage, level, lease=None, body=None, ctx=None, condition=None):
    ctx = ctx or RecordingContext()

    async def go():
        async with stage.platform.launch(
            ctx, run_key="run-1", level=level, ceiling_s=CEILING_S, lease=lease, condition=condition
        ) as sandbox:
            if body:
                await body(sandbox)
            return sandbox

    return asyncio.run(go()), ctx


def test_the_levels_of_the_data_path_factor_decide_the_lease_group_and_the_mode(stage):
    platform = stage.platform
    assert platform.mode({"data_path": "mounted"}) is DataPathMode.MOUNT
    assert platform.lease_group({"data_path": "mounted"}) == GROUP
    assert platform.lease_group({"data_path": "via-gate"}) == GROUP
    assert platform.lease_group({"data_path": "isolated"}) is None
    assert platform.lease_keys(GROUP) == ["slot-a", "slot-b"]
    with pytest.raises(ValueError, match="lease group"):  # a typo is refused, never an empty pool
        platform.lease_keys("something else")
    with pytest.raises(ValueError, match="does not map the level"):
        platform.mode({"data_path": "unmapped"})


def test_c19_launch_waits_only_for_the_start_and_the_teardown(stage, tmp_path):
    """C19: the start and the stop are each inside the sandbox_lifecycle wait; the body is the agent's own time."""
    pool = lease_pool(stage, tmp_path)
    lease = pool.acquire(GROUP, run_key="run-1")
    seen_inside = []

    async def body(sandbox):
        seen_inside.append(list(ctx.events))

    ctx = RecordingContext()
    run_launch(stage, "via-gate", lease, body, ctx)
    assert seen_inside == [[("enter", "sandbox_lifecycle"), ("exit", "sandbox_lifecycle")]]  # closed before the body
    assert ctx.events == [("enter", "sandbox_lifecycle"), ("exit", "sandbox_lifecycle")] * 2
    tokens_ended(stage)
    assert pool.finish(lease) is True
    assert not stage.host.containers


@pytest.mark.parametrize(("level", "interface"), [("via-gate", "rest"), ("mounted", "oneclient")])
def test_o05_each_data_path_gets_a_read_only_token_of_its_own_interface_minted_by_the_leased_user(
    stage, level, interface, tmp_path
):
    """O05: the leased slot's data-path user mints its own token, with the interface of the path it is for."""
    lease = lease_pool(stage, tmp_path).acquire(GROUP, run_key="run-1")
    sandbox, _ = run_launch(stage, level, lease)
    mint = [r for r in stage.fake.requests if r.path.endswith("/user/tokens/temporary")][-1]
    caveats = {c["type"]: c for c in json.loads(mint.body)["caveats"]}
    assert "data.readonly" in caveats
    assert caveats["interface"]["interface"] == interface
    assert sandbox.metadata()["data_path_token"]["slot"] == 0  # the first of [slots], by position
    assert sandbox.spec.token.subject == stage.slots.get(KEY).data_path.user_id
    assert "token" not in json.dumps(sandbox.metadata()["data_path_token"]).lower().replace("data_path_token", "")


@pytest.mark.parametrize(("level", "listing"), [(COMPLETE_LEVEL, "complete"), ("via-gate", "paged"), ("mounted", None)])
def test_o21_the_launch_gives_a_run_the_listing_mode_its_level_selects(stage, level, listing):
    """O21: a study selects the listing mode per condition through the level of its data path; the launch gives the
    run's sandbox that level's mode, and the run's metadata records it."""
    sandbox, _ = run_launch(stage, level)
    assert sandbox.spec.listing == (listing or "paged")
    assert sandbox.metadata()["listing"] == listing


def test_o12_the_platform_launch_records_the_hash_of_the_platform_file(stage):
    """O12: the run's metadata names the configuration by the SHA-256 of the file, never by a value in it."""
    sandbox, _ = run_launch(stage, "isolated")
    assert sandbox.metadata()["platform_config_sha256"] == stage.config.sha256
    assert len(stage.config.sha256) == 64


def test_a_run_with_no_data_path_mints_no_token(stage):
    before = len(stage.fake.requests)
    sandbox, _ = run_launch(stage, "isolated")
    assert len(stage.fake.requests) == before
    assert sandbox.metadata()["data_path"] == "none"


@pytest.mark.parametrize(
    ("lease", "error"),
    [
        (SimpleNamespace(key=KEY, run_key="run-1", group=GROUP, held=False), ValueError),  # released
        (SimpleNamespace(key="intruder", run_key="run-1", group=GROUP, held=True), KeyError),  # no such slot
        (SimpleNamespace(key=KEY, run_key="run-1", group="other", held=True), ValueError),  # another pool's
    ],
    ids=["released", "unknown-key", "other-group"],
)
def test_a_lease_that_is_not_a_held_lease_of_a_slot_is_refused_before_anything_is_minted(stage, lease, error):
    before = len(stage.fake.requests)
    with pytest.raises(error):
        run_launch(stage, "via-gate", lease)
    assert len(stage.fake.requests) == before
    assert not stage.host.containers


def test_o05_a_token_the_platform_cannot_vouch_for_refuses_the_run_before_any_container(stage, tmp_path):
    """O05: fail closed. A user without exactly view and read is refused, and nothing starts."""
    stage.fake.grant(stage.slots.get(KEY).data_path.user_id, ["space_view", "space_read_data", "space_manage_qos"])
    lease = lease_pool(stage, tmp_path).acquire(GROUP, run_key="run-1")
    with pytest.raises(TokenRefused):
        run_launch(stage, "via-gate", lease)
    assert not stage.host.containers
    assert not stage.host.networks


def test_c19_a_token_the_platform_lets_outlive_its_request_is_recorded_through_the_platforms_event_sink(
    stage, tmp_path
):
    """C19 with O12: the platform hands back a token 50 s longer than asked. The run is refused before any container,
    and the sink the sandbox platform was given receives `token_expiry_beyond_request` for the leased slot, named by
    its position and never by its configured name."""
    stage.fake.mint_hook = lambda user, caveats: [
        {**c, "validUntil": c["validUntil"] + 50} if c["type"] == "time" else c for c in caveats
    ]
    lease = lease_pool(stage, tmp_path).acquire(GROUP, run_key="run-1")
    with pytest.raises(TokenRefused):
        run_launch(stage, "via-gate", lease)
    assert [kind for kind, _ in stage.events] == ["token_expiry_beyond_request"]
    assert f"slots[{stage.slots.keys_of(GROUP).index(lease.key)}]" in stage.events[0][1]
    assert lease.key not in stage.events[0][1]
    assert not stage.host.containers


def test_a_teardown_that_leaves_something_raises_a_sandbox_error_and_keeps_the_lease_quarantined(stage, tmp_path):
    """C19: nothing frees the key unless the probe shows the sandbox gone."""
    pool = lease_pool(stage, tmp_path)
    lease = pool.acquire(GROUP, run_key="run-1")

    async def body(sandbox):
        stage.host.fail_remove.add(sandbox.names.gate)

    with pytest.raises(InfraError) as caught:
        run_launch(stage, "via-gate", lease, body)
    assert caught.value.kind == "sandbox"
    assert pool.finish(lease) is False  # the probe finds the gate container: the key stays held
    assert lease.quarantined
    stage.host.fail_remove.clear()
    for name in list(stage.host.containers):
        stage.host.docker("rm", "-f", name)
    for name in list(stage.host.networks):
        stage.host.docker("network", "rm", name)
    stage.host.mounts.clear()
    tokens_ended(stage)
    assert pool.settle_quarantines() == [KEY]


@pytest.mark.parametrize("body_fails", [False, True], ids=["returns", "raises"])
def test_o05_c01_a_gate_refusal_no_cell_reported_ends_the_run_as_data_gate_integrity(stage, body_fails):
    """O05, C01: a refusal of the data gate in its log when the run's body ends (a read the agent's code left running)
    ends the run as infrastructure of kind data_gate_integrity, in place of whatever the body raised, and the
    sandbox is stopped all the same."""
    refusal = {"status": 502, "upstream_status": 200, "gate_refusal": "credential_in_header", "client_saw": "5xx"}

    async def body(sandbox):
        with open(sandbox.dir / "gatelog" / "requests.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(refusal) + "\n")
        if body_fails:
            raise RuntimeError("the agent's run failed after the refusal")

    with pytest.raises(InfraError) as caught:
        run_launch(stage, "via-gate", body=body)
    assert caught.value.kind == "data_gate_integrity"
    assert "credential_in_header" in caught.value.detail
    assert not stage.host.containers


def test_o16_a_gate_log_that_is_not_utf8_at_the_runs_end_is_data_gate_integrity_and_the_sandbox_still_stops(stage):
    """O16: bytes that are not UTF-8 in the gate log at the end of the run's body leave its refusals unknown: the run
    ends as data_gate_integrity (gate_log_unreadable), and the sandbox is stopped all the same."""

    async def body(sandbox):
        with open(sandbox.dir / "gatelog" / "requests.jsonl", "ab") as f:
            f.write(b"\xff\xfe not utf-8\n")

    with pytest.raises(InfraError) as caught:
        run_launch(stage, "via-gate", body=body)
    assert caught.value.kind == "data_gate_integrity"
    assert "gate_log_unreadable" in caught.value.detail
    assert not stage.host.containers


def test_a_body_that_fails_still_stops_the_sandbox_and_the_error_is_the_bodys(stage):
    async def body(sandbox):
        raise RuntimeError("the agent's loop failed")

    with pytest.raises(RuntimeError, match="the agent's loop failed"):
        run_launch(stage, "via-gate", None, body)
    assert not stage.host.containers


def test_a_body_that_fails_while_the_teardown_also_fails_keeps_the_bodys_error(stage):
    async def body(sandbox):
        stage.host.fail_remove.add(sandbox.names.gate)
        raise RuntimeError("the agent's loop failed")

    with pytest.raises(RuntimeError, match="the agent's loop failed"):
        run_launch(stage, "via-gate", None, body)


def probe_world(stage, tmp_path):
    """A started sandbox under a lease key, and a probe over the same host."""
    sandbox = None

    async def keep(s):
        nonlocal sandbox
        sandbox = s

    lease = lease_pool(stage, tmp_path).acquire(GROUP, run_key="run-1")
    run_launch(stage, "mounted", lease, keep)
    return sandbox, SandboxProbe(stage.host, stage.s)


def test_c19_the_probe_says_exactly_true_only_when_container_network_and_mount_are_gone(stage, tmp_path):
    """C19: True only when nothing started with the key is left; the answer is True itself, not a truthy value."""
    _, probe = probe_world(stage, tmp_path)
    assert probe.resource_gone(KEY, {}) is True  # the launch has stopped it
    # a leftover of each kind, one at a time, makes the proof fail
    stage.host.docker("run", "-d", "--name", "leftover", "--label", f"org.test.lease={KEY}", "agent:1")
    assert probe.resource_gone(KEY, {}) is False
    stage.host.docker("rm", "-f", "leftover")
    stage.host.docker(
        "network", "create", "--subnet", "10.77.0.0/28", "--label", f"org.test.lease={KEY}", "leftover-net"
    )
    assert probe.resource_gone(KEY, {}) is False
    stage.host.docker("network", "rm", "leftover-net")
    stage.host.mounts[str(stage.s.work_root / f"{KEY}-0123abcd" / "work")] = "ext4"
    assert probe.resource_gone(KEY, {}) is False
    stage.host.mounts.clear()
    assert probe.resource_gone(KEY, {}) is True


def test_c19_the_probe_is_about_its_own_key_only(stage, tmp_path):
    """C19: The probe is about its own key only."""
    _, probe = probe_world(stage, tmp_path)
    stage.host.docker("run", "-d", "--name", "other", "--label", "org.test.lease=slot-b", "agent:1")
    stage.host.mounts[str(stage.s.work_root / "slot-b-0123abcd" / "work")] = "ext4"
    assert probe.resource_gone(KEY, {}) is True
    assert probe.resource_gone("slot-b", {}) is False
    stage.host.mounts[str(stage.s.work_root / "slot-a-extra-0123abcd" / "work")] = "ext4"
    assert probe.resource_gone(KEY, {}) is True  # a key that merely starts with this one is another key


def test_o15_an_unreadable_proc_state_at_the_teardown_keeps_the_mount_sandbox_and_its_key_held(stage, tmp_path):
    """O15: when /proc refuses the state of the agent's and the sidecar's processes, and their namespaces cannot be
    read, nothing proves their copies of the FUSE mount gone: the teardown removes neither container, the launch fails
    as infrastructure, and the probe does not answer True, so the key stays quarantined."""
    pool = lease_pool(stage, tmp_path)
    lease = pool.acquire(GROUP, run_key="run-1")

    async def body(sandbox):
        host = stage.host
        for m in fuse_mounts(host.mountinfo()):  # the connection exists, and its abort is refused
            host.files[f"/sys/fs/fuse/connections/{m['dev'].split(':')[1]}"] = ""
        host.run_handlers["sh"] = lambda argv: CmdResult(1, "", "denied")
        for name in (sandbox.names.agent, sandbox.names.sidecar):
            host.denied.add(f"/proc/{host.containers[name].pid}/stat")

    with pytest.raises(InfraError) as caught:
        run_launch(stage, "mounted", lease, body)
    assert caught.value.kind == "sandbox"
    assert {c.labels.get("org.test.lease") for c in stage.host.containers.values()} == {KEY}
    assert len(stage.host.containers) == 2  # the agent and the sidecar, held
    assert stage.platform.probe().resource_gone(KEY, {}) is not True
    assert pool.finish(lease) is False
    assert lease.quarantined


def test_c19_a_probe_that_cannot_ask_fails_safe(stage, tmp_path):
    """C19: both questions fail safe: a docker that does not answer or an unreadable mount table is not proof."""
    _, probe = probe_world(stage, tmp_path)
    stage.host.docker = lambda *a, **k: CmdResult(1, "", "daemon down")
    assert probe.resource_gone(KEY, {}) is False
    del stage.host.docker
    original = stage.host.read_text

    def unreadable(path):
        if str(path) == "/proc/self/mountinfo":
            raise OSError("no mount table")
        return original(path)

    stage.host.read_text = unreadable
    assert probe.resource_gone(KEY, {}) is False


ROOT = "1 0 8:1 / / rw - ext4 /dev/root rw\n"


@pytest.mark.parametrize(
    "table",
    [
        "",  # nothing read
        "\n",
        "this is not a mount table\n",  # malformed
        ROOT + "36 25 0:30 / /mnt rw\n",  # a line cut off before its separator
        ROOT + "36 25 zero:30 / /mnt rw - ext4 src rw\n",  # a device that is not major:minor
        ROOT + "36 25 0:30 / /mnt rw - ext4\n",  # no source
        "36 25 0:30 / /mnt rw - ext4 src rw\n",  # well-formed lines, but no root mount: it may be cut off
    ],
    ids=["empty", "blank", "malformed", "cut-off", "bad-device", "no-source", "no-root"],
)
def test_c19_a_mount_table_that_cannot_be_shown_whole_is_not_proof(stage, tmp_path, table):
    """C19: an empty, malformed or truncated mountinfo is unknown, so the probe does not say True."""
    _, probe = probe_world(stage, tmp_path)
    assert probe.resource_gone(KEY, {}) is True  # the control: the whole table, with the sandbox gone
    original = stage.host.read_text
    stage.host.read_text = lambda path: table if str(path) == "/proc/self/mountinfo" else original(path)
    assert probe.resource_gone(KEY, {}) is False


def test_c19_a_root_line_cut_before_its_super_options_makes_the_whole_table_unknown(stage, tmp_path):
    """C19: a table cut off to its root line, and that line cut before its superblock options, is not a table: every
    line needs the file system type, the source and the super options after its separator, so the probe says no."""
    _, probe = probe_world(stage, tmp_path)
    assert probe.resource_gone(KEY, {}) is True  # the control: the whole table, with the sandbox gone
    cut = "1 0 8:1 / / rw - ext4 /dev/root\n"
    with pytest.raises(MountTableError):
        parse_mountinfo(cut)
    original = stage.host.read_text
    stage.host.read_text = lambda path: cut if str(path) == "/proc/self/mountinfo" else original(path)
    assert probe.resource_gone(KEY, {}) is False


def test_c19_the_probe_asks_the_cores_check_whether_the_holder_is_gone(stage):
    """C19: The probe asks the cores check whether the holder is gone."""
    calls = []
    probe = SandboxProbe(stage.host, stage.s, holder_gone=lambda owner: calls.append(owner) or None)
    assert probe.holder_gone("some-owner") is None
    assert calls == ["some-owner"]


def test_the_clock_of_the_token_follows_the_runs_ceiling(stage, tmp_path):
    """O06: the token's expiry is derived from the whole-time ceiling the launch is given."""
    lease = lease_pool(stage, tmp_path).acquire(GROUP, run_key="run-1")
    sandbox, _ = run_launch(stage, "via-gate", lease)
    expires = sandbox.metadata()["data_path_token"]["expires_at"]
    assert expires >= NOW + CEILING_S + stage.config.tokens.margin_s


def block_the_agents_docker_run(stage):
    """Make the start thread wait inside the agent's `docker run`, after the network and the gate exist."""
    entered, release = threading.Event(), threading.Event()
    original = stage.host.docker

    def slow(*args, **kwargs):
        if args[0] == "run" and "--init" in args:  # the agent's `docker run`
            entered.set()
            assert release.wait(20), "the test never released the start thread"
        return original(*args, **kwargs)

    stage.host.docker = slow
    return entered, release


def nothing_of_the_sandbox_is_left(stage) -> bool:
    return not stage.host.containers and not stage.host.networks and not stage.host.mounts


async def cancel_mid_start(stage, lease, entered):
    """Cancel a launch whose start thread is inside the agent's `docker run`; returns the sandbox probe's answers on the
    way (the slot's tokens, minted before the start, are the slot probe's other half)."""
    probe = stage.platform.sandbox_probe()
    seen = {}

    async def launch():
        async with stage.platform.launch(
            RecordingContext(), run_key="run-1", level="via-gate", ceiling_s=CEILING_S, lease=lease
        ):
            pytest.fail("a cancelled start must not hand out a sandbox")

    task = asyncio.ensure_future(launch())
    assert await asyncio.to_thread(entered.wait, 20)  # the thread is creating the sandbox
    task.cancel()
    await asyncio.sleep(0.05)
    seen["during"] = probe.resource_gone(lease.key, {})
    return task, probe, seen


def test_o15_a_launch_cancelled_mid_start_tears_down_what_the_start_thread_created(stage, tmp_path):
    """O15, C19: a cancelled launch waits for the start thread (bounded), then tears down all of it; then True."""
    lease = lease_pool(stage, tmp_path).acquire(GROUP, run_key="run-1")
    entered, release = block_the_agents_docker_run(stage)

    async def go():
        task, probe, seen = await cancel_mid_start(stage, lease, entered)
        threading.Timer(0.2, release.set).start()  # the thread's docker call returns, after the cancellation
        with pytest.raises(asyncio.CancelledError):
            await task
        seen["after"] = probe.resource_gone(lease.key, {})
        return seen

    seen = asyncio.run(go())
    assert seen["during"] is False  # the gate, the network and the mount exist, and the agent is being created
    assert nothing_of_the_sandbox_is_left(stage)
    assert stage.host.calls_of("removed")
    assert stage.host.calls_of("network_removed")
    assert seen["after"] is True


def test_o15_a_cancelled_launch_that_gives_up_waiting_leaves_the_probe_not_true_until_the_thread_cleaned_up(
    stage, tmp_path
):
    """O15, C19: past the deadline the thread is still running, so nothing is shown gone; what it creates after that
    carries the lease labels, and it removes it on its way out."""
    stage.platform = SandboxPlatform(
        stage.config,
        stage.issuer,
        stage.host,
        slots=stage.slots,
        journal=stage.journal,
        events=stage.record_event,
        clock=lambda: stage.now[0],
        registry=stage.registry,
        sandbox_options={"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None},
        cancel_wait_s=0.05,
    )
    lease = lease_pool(stage, tmp_path).acquire(GROUP, run_key="run-1")
    entered, release = block_the_agents_docker_run(stage)
    answers = []

    async def go():
        task, probe, _ = await cancel_mid_start(stage, lease, entered)
        with pytest.raises(asyncio.CancelledError):
            await task  # it gave up waiting after 50 ms, and tore down what existed
        answers.append(probe.resource_gone(lease.key, {}))  # the thread is still inside docker: not gone
        release.set()  # now the agent container is created, labelled, after the teardown
        deadline = time.monotonic() + 20
        while not nothing_of_the_sandbox_is_left(stage) or probe.resource_gone(lease.key, {}) is not True:
            answers.append(probe.resource_gone(lease.key, {}))
            assert time.monotonic() < deadline, "the start thread never cleaned up after itself"
            await asyncio.sleep(0.02)

    asyncio.run(go())
    assert answers[0] is False
    assert nothing_of_the_sandbox_is_left(stage)
    assert not stage.host.containers


def test_o15_a_start_that_failed_is_stopped_again_by_the_launch_and_the_error_is_the_starts(stage, tmp_path):
    """O15: any failure of the start, not only a cancellation, ends with the launch stopping what the sandbox made."""
    stage.host.exec_handler = lambda container, argv, stdin: CmdResult(0, "WRITABLE\n")
    lease = lease_pool(stage, tmp_path).acquire(GROUP, run_key="run-1")
    with pytest.raises(SandboxError, match="not read-only"):
        run_launch(stage, "via-gate", lease)
    assert nothing_of_the_sandbox_is_left(stage)
    assert stage.platform.sandbox_probe().resource_gone(lease.key, {}) is True


class RecordingLauncher:
    """Stands for the tool-server launcher: records what each start was given and yields a stand-in provider."""

    def __init__(self):
        self.started = []

    @contextlib.asynccontextmanager
    async def launch(self, ctx, condition, token, *, ceiling_s, extra_env=None, principal=None):
        self.started.append(SimpleNamespace(condition=condition, token=token, principal=principal, ceiling_s=ceiling_s))
        yield "the run's tools"


def with_tools(stage):
    """Rebuild the stage's platform so that a run's `tools` factor names its tool-server condition."""
    launcher = RecordingLauncher()
    stage.platform = SandboxPlatform(
        stage.config,
        stage.issuer,
        stage.host,
        slots=stage.slots,
        journal=stage.journal,
        events=stage.record_event,
        tool_condition=lambda factors: factors.get("tools"),
        tool_server=launcher,
        clock=lambda: stage.now[0],
        registry=stage.registry,
        sandbox_options={"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None},
    )
    return launcher


def minted(stage):
    """The user ids of the tokens the fake federation has issued, in the order it issued them."""
    return [token["subject"]["id"] for token in stage.fake.issued.values()]


def journal_lines(stage):
    path = stage.leases / JOURNAL
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_c19_one_lease_key_per_run_serves_both_planes(stage, tmp_path):
    """C19: a run with a data path and a tool server takes one slot, and that key gives both planes their users. The
    data-path token is minted by the slot's data-path user and the tool server starts with a token of the same slot's
    tool-server user, required to be that user; both are minted at once with one journalled expiry, and the other
    slot stays free."""
    launcher = with_tools(stage)
    factors = {"data_path": "via-gate", "tools": "read"}
    pool = lease_pool(stage, tmp_path)
    lease = pool.acquire(stage.platform.lease_group(factors), run_key="run-1")
    slot = stage.slots.get(lease.key)
    ctx, seen = RecordingContext(), {}

    async def body(sandbox):
        async with stage.platform.tool_server(ctx, sandbox, ceiling_s=CEILING_S) as tools:
            seen["tools"] = tools
        seen["data"] = sandbox.spec.token
        seen["metadata"] = sandbox.metadata()

    run_launch(stage, "via-gate", lease, body, ctx, condition="read")
    assert stage.platform.lease_group(factors) == GROUP
    assert seen["tools"] == "the run's tools"
    [started] = launcher.started
    assert seen["data"].subject == slot.data_path.user_id
    assert (started.condition, started.principal, started.token.subject) == (
        "read",
        slot.tool_server,
        slot.tool_server.user_id,
    )
    assert minted(stage) == [slot.data_path.user_id, slot.tool_server.user_id]
    lines = journal_lines(stage)
    assert [(line["slot"], line.get("provisional", False)) for line in lines] == [(KEY, True), (KEY, False)]
    assert lines[0]["mint"] == lines[1]["mint"]
    assert seen["data"].expires_at == started.token.expires_at == stage.journal.latest_expiry(KEY)
    assert stage.journal.latest_expiry("slot-b") is None
    assert seen["metadata"]["tool_server_token"] == {"slot": 0, "expires_at": started.token.expires_at}


def test_c19_a_run_with_a_tool_server_and_no_data_path_leases_a_slot_and_mints_only_the_tool_servers_token(
    stage, tmp_path
):
    """C19: a tool server alone needs a slot; its run mints the tool-server token on the slot's tool-server user and
    no data-path token."""
    launcher = with_tools(stage)
    factors = {"data_path": "isolated", "tools": "read"}
    assert stage.platform.lease_group(factors) == GROUP
    lease = lease_pool(stage, tmp_path).acquire(GROUP, run_key="run-1")
    slot = stage.slots.get(lease.key)
    ctx = RecordingContext()

    async def body(sandbox):
        async with stage.platform.tool_server(ctx, sandbox, ceiling_s=CEILING_S):
            pass

    sandbox, _ = run_launch(stage, "isolated", lease, body, ctx, condition="read")
    assert minted(stage) == [slot.tool_server.user_id]
    assert sandbox.spec.token is None
    assert "data_path_token" not in sandbox.metadata()
    assert launcher.started[0].token.subject == slot.tool_server.user_id


def test_c19_a_run_that_needs_neither_plane_takes_no_slot_and_has_no_tool_server(stage):
    """C19: no data-path token and no tool server: no lease group, so no slot is taken, and nothing is minted."""
    with_tools(stage)
    assert stage.platform.lease_group({"data_path": "isolated"}) is None
    assert stage.platform.lease_group({"data_path": "isolated", "tools": None}) is None
    ctx, refused = RecordingContext(), []

    async def body(sandbox):
        with pytest.raises(ValueError, match="without a tool-server condition") as caught:
            async with stage.platform.tool_server(ctx, sandbox, ceiling_s=CEILING_S):
                pass
        refused.append(caught.value)

    run_launch(stage, "isolated", None, body, ctx)
    assert refused
    assert minted(stage) == []


def test_c19_a_run_with_a_tool_server_needs_a_leased_slot(stage):
    """C19: the tool-server user is always a slot's, so a run with a tool server and no lease starts nothing."""
    with_tools(stage)
    with pytest.raises(ValueError, match="leased slot"):
        run_launch(stage, "via-gate", None, condition="read")
    assert minted(stage) == []
    assert not stage.host.containers


def test_c19_a_key_is_not_freed_while_its_sandbox_remains_or_a_token_of_its_slot_lives(stage, tmp_path):
    """C19: the platform's probe frees a key only when both halves hold: the sandbox is proven gone and the slot's
    tokens have ended, by their journalled expiry plus the margin. Either alone keeps it quarantined."""
    pool = lease_pool(stage, tmp_path)
    lease = pool.acquire(GROUP, run_key="run-1")
    run_launch(stage, "via-gate", lease)
    probe = stage.platform.probe()
    end = stage.journal.latest_expiry(KEY)
    assert stage.platform.sandbox_probe().resource_gone(KEY, {}) is True  # the sandbox is gone
    assert probe.resource_gone(KEY, {}) is False  # but its slot's token lives
    stage.now[0] = end + SAFETY_MARGIN_S
    assert probe.resource_gone(KEY, {}) is False  # at the end plus the margin, not yet past it
    stage.host.docker("run", "-d", "--name", "leftover", "--label", f"org.test.lease={KEY}", "agent:1")
    stage.now[0] = end + SAFETY_MARGIN_S + 1
    assert probe.resource_gone(KEY, {}) is False  # the tokens have ended, but something of the sandbox is left
    assert pool.finish(lease) is False
    assert lease.quarantined
    stage.host.docker("rm", "-f", "leftover")
    assert probe.resource_gone(KEY, {}) is True
    assert pool.settle_quarantines() == [KEY]


def test_c19_a_launch_cancelled_while_its_mint_is_blocked_holds_the_slot_until_the_worker_has_journalled(
    stage, tmp_path
):
    """C19: the mint runs in a thread, which a cancellation does not stop. Cancelled while the mint is blocked before
    its provisional line, the launch waits for the worker before it ends, and while the worker runs the probe shows
    nothing gone; once it has ended its tokens are journalled, so the slot stays held until they have ended too, and a
    finished lease is never released while the worker still mints on the slot's users."""
    pool = lease_pool(stage, tmp_path)
    lease = pool.acquire(GROUP, run_key="run-1")
    entered, release = threading.Event(), threading.Event()
    record = stage.journal.record

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(20), "the test never released the mint"
        return record(*args, **kwargs)

    stage.journal.record = blocked  # the journal SlotTokens writes: its first line is the provisional one
    probe = stage.platform.probe()

    async def go():
        async def launch():
            async with stage.platform.launch(
                RecordingContext(), run_key="run-1", level="via-gate", ceiling_s=CEILING_S, lease=lease
            ):
                pytest.fail("a cancelled launch must not hand out a sandbox")

        task = asyncio.ensure_future(launch())
        assert await asyncio.to_thread(entered.wait, 20)  # the worker is inside the mint, before any line
        task.cancel()
        await asyncio.sleep(0.05)
        during = probe.resource_gone(KEY, {})
        threading.Timer(0.2, release.set).start()
        with pytest.raises(asyncio.CancelledError):
            await task
        return during

    during = asyncio.run(go())
    assert during is False  # nothing is shown gone while the worker runs
    assert stage.journal.latest_expiry(KEY) is not None  # the launch ended only after the worker journalled
    assert len(stage.fake.issued) == 1
    assert probe.resource_gone(KEY, {}) is False
    assert pool.finish(lease) is False
    tokens_ended(stage)
    assert probe.resource_gone(KEY, {}) is True
    assert pool.settle_quarantines() == [KEY]


def declared_platform(stage, tmp_path):
    """The stage's platform with one declared read-only input, and that input as declared."""
    from expcore_onedata.sandbox import inputs
    from expcore_onedata.sandbox.api import ReadOnlyInput

    supplied = tmp_path / "supplied"
    supplied.mkdir()
    (supplied / "analyse.py").write_text("print('analysed')\n", encoding="utf-8")
    declared = inputs.declare(stage.s, {"every-run": [ReadOnlyInput(supplied, "/opt/analysis")]})
    options = {"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None}
    platform = SandboxPlatform(
        stage.config,
        stage.issuer,
        stage.host,
        slots=stage.slots,
        journal=stage.journal,
        events=stage.record_event,
        registry=stage.registry,
        sandbox_options=options,
        clock=lambda: stage.now[0],
        read_only_inputs=declared,
    )
    return platform, declared["every-run"][0], supplied


def test_o20_launch_hands_a_read_only_input_to_the_runs_sandbox_and_its_record(stage, tmp_path):
    """O20: `launch(..., inputs=..., inputs_record=...)` gives the run's sandbox a declared read-only input, mounted
    read-only in the agent from the run's own copy, and puts its hash in the run's record itself, through the
    instrument the executor finishes into `RunRecord.instruments["read_only_inputs"]`."""
    from record_support import hand_made

    from expcore.plugin import Instrument
    from expcore.records.instruments import finish_instruments
    from expcore_onedata.sandbox import inputs

    platform, item, _ = declared_platform(stage, tmp_path)
    kept = inputs.InputsRecord()
    assert isinstance(kept, Instrument)
    kept.start({"run_key": "run-1"})
    seen = {}

    async def go():
        async with platform.launch(
            RecordingContext(),
            run_key="run-1",
            level="isolated",
            ceiling_s=CEILING_S,
            inputs=[item],
            inputs_record=kept,
        ) as sandbox:
            seen["mounts"] = stage.host.containers[sandbox.names.agent].options["--mount"]

    asyncio.run(go())
    assert any(m.endswith(",dst=/opt/analysis,readonly") for m in seen["mounts"])
    record = finish_instruments(hand_made(), [kept])
    [recorded] = record.instruments["read_only_inputs"]["inputs"]
    assert record.instruments["read_only_inputs"]["ok"] is True
    assert (recorded["path"], recorded["sha256"]) == ("/opt/analysis", item.sha256)


def test_o20_launch_refuses_an_input_it_was_not_declared_or_a_record_it_was_not_given(stage, tmp_path):
    """O20: an input the platform was not declared with (another source, path or hash), and an input with no
    instrument to keep it in the run's record, are refused before any token is minted or anything is created."""
    import dataclasses

    from expcore_onedata.sandbox import inputs
    from expcore_onedata.sandbox.api import ReadOnlyInput, StartRefused

    platform, item, supplied = declared_platform(stage, tmp_path)
    undeclared = [ReadOnlyInput(supplied, "/opt/analysis"), dataclasses.replace(item, target="/opt/elsewhere")]

    async def go(given, record):
        async with platform.launch(
            RecordingContext(),
            run_key="run-1",
            level="isolated",
            ceiling_s=CEILING_S,
            inputs=given,
            inputs_record=record,
        ):
            pass

    for given in undeclared:
        with pytest.raises(StartRefused, match="not declared"):
            asyncio.run(go([given], inputs.InputsRecord()))
    with pytest.raises(ValueError, match="record"):
        asyncio.run(go([item], None))
    assert not stage.host.containers
    assert not stage.host.networks
