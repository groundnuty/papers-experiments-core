"""Lease groups by tool-server rights (O30): one campaign runs a read-only and a writing condition interleaved, and each
run leases only a slot whose tool-server user holds exactly its condition's rights."""

import asyncio
import itertools
import os
import threading
from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from sandbox_support import FAST, SLOT_USERS, add_sandbox, slots_data
from scheduler_support import FakeExecutor, build, settings_for

from expcore.campaign.factors import FactorSchema
from expcore.campaign.lease import LeasePool
from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.testing import BASELINE, CREDENTIALS_DIR, NOW, write_platform
from expcore_onedata.tokens import TokenIssuer
from expcore_onedata.users import GROUP, SlotProbe, SlotTokens, TokenJournal, group_of, load_slots

CEILING_S = 600.0
WRITE = [  # a writing condition's six rights, the read-only one's two and four more
    "space_view",
    "space_read_data",
    "space_view_transfers",
    "space_schedule_replication",
    "space_view_qos",
    "space_manage_qos",
]
LATE = NOW + 10**7  # a moment by which every token minted at NOW has ended
PLAN = {"tasks": ("task-a", "task-b"), "seeds": (1, 2)}


def no_events(kind, detail):
    """The event sink of a test that is not about events."""


def schema(*tools: str) -> FactorSchema:
    """A study with a condition per level of its `tools` factor, which names the platform's tool-server condition; every
    run reads through the gate."""
    return FactorSchema(
        {"data_path": ["via-gate"], "tools": list(tools)},
        ["task", "data_path", "tools", "model", "seed"],
        {f"with-{tool}": {"data_path": "via-gate", "tools": tool} for tool in tools},
    )


def platform_file(tmp_path, fake, rights):
    """The sandbox's platform document with a writing condition `write` of six rights beside `read` (the baseline) and
    `manage`, and the two slots, each tool-server user declaring the rights `rights` gives its slot and holding them
    in the fake federation; every data-path user holds the baseline."""

    def edit(data):
        add_sandbox(data)
        data["slots"] = slots_data(rights)
        conditions = data["toolserver"]["conditions"]
        conditions["write"] = {**conditions["manage"], "rights": WRITE}

    path = write_platform(tmp_path, edit)
    for login, (password, user_id) in SLOT_USERS.items():
        file = tmp_path / CREDENTIALS_DIR / f"{login}.pass"
        file.write_text(password + "\n", encoding="utf-8")
        os.chmod(file, 0o600)
        fake.add_user(login, password, user_id)
        slot, plane = login.rsplit("-", 1)
        fake.grant(user_id, rights.get(slot, BASELINE) if plane == "tools" else BASELINE)
    return path


def staged(tmp_path, fake, rights):
    """The platform over a fake host, whose runs' `tools` factor names their tool-server condition, with the minting of
    its slots and a lease pool over its keys whose probe sees every token ended."""
    path = platform_file(tmp_path, fake, rights)
    config = load_platform(path)
    slots = load_slots(path, config)
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    journal = TokenJournal(tmp_path / "leases")
    issuer = TokenIssuer(OnezoneClient(config, http=fake.client_http()), config, clock=lambda: NOW)
    platform = SandboxPlatform(
        config,
        issuer,
        host,
        slots=slots,
        journal=journal,
        events=no_events,
        tool_condition=lambda factors: factors.get("tools"),
        sandbox_options={"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None},
        clock=lambda: NOW,
    )
    pool = LeasePool(
        tmp_path / "leases", platform.lease_keys, SlotProbe(journal, clock=lambda: LATE), sleep=lambda s: None
    )
    minter = SlotTokens(issuer, config, slots, journal, events=no_events, clock=lambda: NOW)
    return SimpleNamespace(config=config, slots=slots, platform=platform, pool=pool, minter=minter, fake=fake)


@pytest.fixture
def mixed(tmp_path, fake):
    """`slot-a` for the read-only condition (two rights), `slot-b` for the writing one (six)."""
    return staged(tmp_path, fake, {"slot-a": BASELINE, "slot-b": WRITE})


class MintingExecutor(FakeExecutor):
    """Mints each run's tokens on the slot it leased, as the platform's launch does, and notes what the slot was, before
    it finishes the run. A refused mint fails the run."""

    def __init__(self, stage, *, meet: threading.Barrier | None = None):
        super().__init__()
        self.stage, self.meet = stage, meet
        self.drawn: list[dict] = []

    async def _run(self, request, behaviour):
        condition = request.identity.factors["tools"]
        slot = self.stage.slots.users_of(request.lease)
        if self.meet is not None:  # wait, holding the lease, until the run of the other condition holds its own
            await asyncio.to_thread(self.meet.wait, 20)
        tokens = self.stage.minter.mint(request.lease, ceiling_s=CEILING_S, condition=condition)
        held = self.stage.fake.privileges[(self.stage.fake.SPACE_ID, slot.tool_server.user_id)]["direct"]
        self.drawn.append(
            {
                "condition": condition,
                "group": request.lease.group,
                "key": request.lease.key,
                "declared": list(slot.rights),
                "held": sorted(held),
                "wanted": sorted(self.stage.config.condition(condition).rights),
                "subject": tokens.tool_server.subject,
                "user": slot.tool_server.user_id,
            }
        )
        return await super()._run(request, "finish")


def changes(sequence):
    """How often a sequence changes from one item to the next."""
    return sum(1 for before, after in itertools.pairwise(sequence) if before != after)


def test_o30_one_campaign_runs_a_read_only_and_a_writing_condition_interleaved_each_on_a_slot_of_its_rights(
    mixed, tmp_path
):
    """O30: a campaign whose plan interleaves a read-only condition (two rights) and a writing one (six), in its
    randomised order, leases each run a slot whose tool-server user declares and holds exactly its condition's
    rights, so every token mint succeeds; nothing has to be split into one campaign per condition."""
    executor = MintingExecutor(mixed)
    with build(
        tmp_path / "campaign",
        schema=schema("read", "write"),
        executor=executor,
        leases=mixed.pool,
        lease_group=mixed.platform.lease_group,
        plan=PLAN,
    ) as scheduler:
        summary = scheduler.run()
    assert summary.statuses == {"done": 8}
    order = [d["condition"] for d in executor.drawn]
    assert sorted(order) == ["read"] * 4 + ["write"] * 4
    assert changes(order) >= 2  # interleaved in the plan's order, not one condition's block after the other's
    for drawn in executor.drawn:
        assert drawn["declared"] == drawn["held"] == drawn["wanted"]
        assert drawn["subject"] == drawn["user"]
        assert drawn["group"] == group_of(drawn["wanted"])
    assert {d["key"] for d in executor.drawn if d["condition"] == "read"} == {"slot-a"}
    assert {d["key"] for d in executor.drawn if d["condition"] == "write"} == {"slot-b"}


def test_o30_runs_of_two_conditions_hold_a_slot_of_each_group_at_once(mixed, tmp_path):
    """O30: a read-only run and a writing run are in flight together, each on its own group's slot: neither waits for
    the other, since they lease from different groups."""
    executor = MintingExecutor(mixed, meet=threading.Barrier(2))
    with build(
        tmp_path / "campaign",
        schema=schema("read", "write"),
        executor=executor,
        leases=mixed.pool,
        lease_group=mixed.platform.lease_group,
        plan={"tasks": ("task-a",), "seeds": (1,)},
        settings=settings_for(workers=2),
    ) as scheduler:
        scheduler.run()
    assert executor.peak == 2
    assert {(d["condition"], d["key"]) for d in executor.drawn} == {("read", "slot-a"), ("write", "slot-b")}


def test_o30_a_run_waits_only_for_a_slot_of_its_own_group(mixed):
    """O30: with the read-only group's one slot held, another read-only run gets no slot while the writing group's
    slot is free, and a writing run takes that one at once."""
    platform, pool = mixed.platform, mixed.pool
    read = platform.lease_group({"data_path": "via-gate", "tools": "read"})
    write = platform.lease_group({"data_path": "via-gate", "tools": "write"})
    assert read != write
    assert (platform.lease_keys(read), platform.lease_keys(write)) == (["slot-a"], ["slot-b"])
    first = pool.acquire(read, run_key="run-1", timeout_s=0)
    assert pool.try_acquire(read, run_key="run-2") is None
    writing = pool.try_acquire(write, run_key="run-3")
    assert writing is not None
    assert (first.key, writing.key) == ("slot-a", "slot-b")


def test_o30_a_run_without_a_tool_server_may_take_any_slot(mixed):
    """O30: a run with a data path and no tool server needs only a data-path user, which every slot has with the
    baseline rights, so it leases from every slot (the group of the platform's users), whichever rights its tool-server
    user declares."""
    assert mixed.platform.lease_group({"data_path": "via-gate"}) == GROUP
    assert mixed.platform.lease_keys(GROUP) == ["slot-a", "slot-b"]


def test_o30_a_condition_whose_rights_no_slot_declares_is_refused_when_the_campaign_is_built(mixed, tmp_path):
    """O30: `manage` (three rights) is in the plan and no slot's tool-server user declares its rights, so its group
    would be empty. The campaign is refused when it is built, naming the condition by its position and the rights,
    before its store is opened or any run is tried; never at a run's mint."""
    with pytest.raises(ConfigError, match=r"\[toolserver\] conditions\[1\]") as refused:
        build(
            tmp_path / "campaign",
            schema=schema("read", "manage"),
            executor=MintingExecutor(mixed),
            leases=mixed.pool,
            lease_group=mixed.platform.lease_group,
            plan=PLAN,
        )
    assert "space_manage_qos" in str(refused.value)
    assert "slot-a" not in str(refused.value)
    assert not mixed.fake.issued
    with pytest.raises(ConfigError, match="no slot"):
        mixed.platform.lease_group({"data_path": "via-gate", "tools": "manage"})


def test_o30_a_slot_leased_for_another_condition_mints_nothing(mixed):
    """O30: a lease of the writing group handed to a read-only run is refused before anything is journalled or minted,
    naming the slot and the condition by their positions."""
    lease = mixed.pool.acquire(mixed.platform.lease_group({"tools": "write", "data_path": "via-gate"}), timeout_s=0)
    with pytest.raises(ValueError, match=r"slots\[1\].*conditions\[0\]"):
        mixed.minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    assert not mixed.fake.issued
    assert TokenJournal(mixed.pool.dir).latest_expiry(lease.key) is None


def test_o30_a_campaign_of_one_rights_set_leases_every_slot_as_before(tmp_path, fake):
    """O30: when every slot declares the rights of the campaign's one condition, its group is every slot, as the one
    group of the platform's users was, and its runs lease, mint and finish as they did."""
    single = staged(tmp_path, fake, {})
    read = single.platform.lease_group({"data_path": "via-gate", "tools": "read"})
    assert single.platform.lease_keys(read) == single.platform.lease_keys(GROUP) == ["slot-a", "slot-b"]
    executor = MintingExecutor(single)
    with build(
        tmp_path / "campaign",
        schema=schema("read"),
        executor=executor,
        leases=single.pool,
        lease_group=single.platform.lease_group,
        plan=PLAN,
        settings=settings_for(workers=2),
    ) as scheduler:
        summary = scheduler.run()
    assert summary.statuses == {"done": 4}
    assert len(executor.drawn) == 4
    assert all(d["declared"] == d["held"] == d["wanted"] == sorted(BASELINE) for d in executor.drawn)


@pytest.mark.parametrize(
    ("rights", "message"),
    [
        (None, r"key 'rights' is required"),
        (["space_view"], r"key 'rights' must include space_view and space_read_data"),
        ([*BASELINE, "space_fly"], r"key 'rights' must list known rights, each once"),
        ([*BASELINE, "space_view"], r"key 'rights' must list known rights, each once"),
    ],
)
def test_o30_a_slot_must_declare_the_rights_of_its_tool_server_user(tmp_path, fake, rights, message):
    """O30: the rights a slot's tool-server user holds decide the conditions whose runs may lease it, so they are
    declared, with a condition's vocabulary and rules; a slot without them, or with rights a condition could not
    declare, is refused at load, naming the slot by its position."""

    def edit(data):
        add_sandbox(data)
        tool = data["slots"]["slot-b"]["toolserver"]
        if rights is None:
            del tool["rights"]
        else:
            tool["rights"] = rights

    path = write_platform(tmp_path, edit)
    with pytest.raises(ConfigError, match=message) as refused:
        load_slots(path, load_platform(path, require_secrets=False), require_secrets=False)
    assert "slots[2" in str(refused.value)
    assert "slot-b" not in str(refused.value)
