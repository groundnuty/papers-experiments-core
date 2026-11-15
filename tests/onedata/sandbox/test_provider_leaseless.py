"""O38: a platform built without slots and a journal, for a run that takes no lease (a one-off check).

`SandboxPlatform` needed `[slots]` and a token journal at construction even for a run that leases nothing, so a study
that wanted a single check of its data path had to invent both. Without them the platform leases nothing: a data-path
run mints as the fixed `[principals.data_path]` user, as a run without a lease always did, and never as a slot's
user, and every call that needs a lease, a slot or the journal is refused, naming what is missing, before anything is
minted or created. A platform built with them behaves as it did (test_provider.py, test_lease_groups.py).
"""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from sandbox_support import FAST, SLOT_USERS

from expcore.config.loading import ConfigError
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.testing import NOW
from expcore_onedata.users import GROUP, JOURNAL, TokenJournal

CEILING_S = 600.0


class Context:
    """A call context that lets every wait through."""

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        yield


@pytest.fixture
def world(config, issuer, fake):
    """A lease-less platform over a fake host that knows the configured images: no slots, no journal."""
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)

    def build(**options):
        return SandboxPlatform(
            config,
            issuer,
            host,
            events=lambda kind, detail: None,
            registry=Registry(),
            sandbox_options={"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None},
            clock=lambda: NOW,
            **options,
        )

    return SimpleNamespace(build=build, host=host, fake=fake, config=config)


def launch(platform, level, **options):
    async def go():
        async with platform.launch(Context(), run_key="run-1", level=level, ceiling_s=CEILING_S, **options) as sandbox:
            return sandbox

    return asyncio.run(go())


def test_o38_a_platform_builds_without_slots_and_a_journal(world):
    platform = world.build()
    assert platform.mode({"data_path": "via-gate"}) is DataPathMode.REST


def test_o38_a_leaseless_platform_gives_no_run_a_lease_group(world):
    """O38: a data-path run on a platform with slots leases from every slot (`GROUP`); with none, it leases nothing."""
    platform = world.build()
    assert [platform.lease_group({"data_path": level}) for level in ("mounted", "via-gate", "isolated")] == [None] * 3


def test_o38_a_leaseless_run_mints_as_the_fixed_data_path_user_and_never_as_a_slots(world, tmp_path):
    """O38: the token of a lease-less run is the `[principals.data_path]` user's; no slot's user is asked for one, no
    token is revoked, and the sandbox's record names the fixed user's place and no slot's."""
    platform = world.build()
    sandbox = launch(platform, "via-gate")
    issued = [token["subject"]["id"] for token in world.fake.issued.values()]
    assert issued == [world.config.principals.data_path.user_id]
    assert not {user_id for _, user_id in SLOT_USERS.values()} & set(issued)
    assert not [r for r in world.fake.requests if r.method == "DELETE" and r.path.endswith("/user/tokens/temporary")]
    assert sandbox.metadata()["data_path_token"]["slot"] == "principals.data_path"
    assert not (tmp_path / "leases" / JOURNAL).exists()  # nothing was journalled: there is no journal


def test_o38_a_leaseless_run_with_no_data_path_mints_nothing(world):
    launch(world.build(), "isolated")
    assert not world.fake.issued


@pytest.mark.parametrize(
    "ask",
    [
        lambda platform: platform.lease_keys(GROUP),
        lambda platform: platform.probe(),
        lambda platform: platform.condition_group("read"),
    ],
    ids=["lease_keys", "probe", "condition_group"],
)
def test_o38_what_needs_the_slots_or_the_journal_is_refused_naming_them(world, ask):
    """O38: nothing that needs a lease's slots or the journal answers on a lease-less platform; each says what is
    missing and where to get it, and none returns an empty answer that a pool would take for "no slots free"."""
    with pytest.raises(ConfigError, match=r"without `slots` and `journal`.*users\.load_slots.*TokenJournal"):
        ask(world.build())


def test_o38_a_tool_server_condition_has_no_group_on_a_leaseless_platform(world):
    """O38: the tool-server user is a slot's, so a run of a tool-server condition is refused when the plan is built (the
    scheduler asks `lease_group` of every run then), never silently left without a lease."""
    platform = world.build(tool_condition=lambda factors: factors.get("tools"))
    assert platform.lease_group({"data_path": "via-gate"}) is None  # a data-path run alone leases nothing
    with pytest.raises(ConfigError, match="without `slots` and `journal`"):
        platform.lease_group({"data_path": "via-gate", "tools": "read"})


def test_o38_a_launch_with_a_lease_is_refused_before_anything_is_minted_or_created(world):
    """O38: a lease names a slot of the platform's `slots`; a platform without any refuses it, whatever it names, and
    starts nothing."""
    lease = SimpleNamespace(key="slot-a", run_key="run-1", group=GROUP, held=True)
    with pytest.raises(ConfigError, match="without `slots` and `journal`"):
        launch(world.build(), "via-gate", lease=lease)
    assert not world.fake.issued
    assert not world.host.containers


def test_o38_a_launch_with_a_tool_server_condition_is_refused_before_anything_is_minted(world):
    with pytest.raises(ConfigError, match="without `slots` and `journal`"):
        launch(world.build(), "via-gate", condition="read")
    assert not world.fake.issued
    assert not world.host.containers


@pytest.mark.parametrize(("given", "missing"), [("slots", "journal"), ("journal", "slots")])
def test_o38_slots_and_a_journal_come_together_or_not_at_all(world, slots, tmp_path, given, missing):
    """O38: slots without a journal could mint tokens whose expiry nothing records, and a journal without slots has
    nothing to record; the constructor refuses either, naming the one that is missing."""
    options = {"slots": slots} if given == "slots" else {"journal": TokenJournal(tmp_path / "leases")}
    with pytest.raises(ValueError, match=f"`{missing}`"):
        world.build(**options)
