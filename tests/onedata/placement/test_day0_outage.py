"""O28: the day-0 outage check takes a declared provider down, records what each way of reaching the data and the
knowledge plane answers, restores the provider and shows it restored, and fails if it cannot (issue #108).

The fakes model no provider that is down, so `outage_support.OutageWorld` puts one in front of them; what it does
while a provider is down is a stand-in's choice (a read with every holder down waits out its timeout), and these tests
assert what the check recorded of it, never that Onedata behaves so."""

import asyncio
import json
import shutil

import pytest
from fake_host import FakeHost
from fake_reset import FakeReset
from fault_support import faults_table
from outage_support import (
    GAMMA_HOST,
    GAMMA_ID,
    KNOWLEDGE_MANIFEST,
    SERVICE,
    AgentWorld,
    FakeServices,
    OutageWorld,
    ToolRelay,
    WorldDaemon,
    add_slot_users,
    agent_host,
    agent_options,
    freeze_tools_manifest,
    with_agent,
)
from sandbox_support import MOUNT_PATH

from expcore_onedata.admission import OUTAGE_RUN
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.day0 import outage_agent, outage_budget, outage_cells, outage_run, outage_tools
from expcore_onedata.day0.outage_cells import Cells
from expcore_onedata.day0.outage_tools import KNOWLEDGE_TOOLS
from expcore_onedata.day0.runner import EXIT_FAIL, EXIT_UNKNOWN, build_context, run, run_checks
from expcore_onedata.fault_journal import InjectionJournal, holder
from expcore_onedata.fault_recovery import digest
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_settings import compose_driver, load_faults
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.testing import (
    ADMIN_ENV,
    ALPHA_ID,
    BASELINE,
    BETA_ID,
    NOW,
    SPACE_NAME,
    USERS,
    with_placement,
    write_platform,
)
from expcore_onedata.testing.world import SPACE_IDS, SPACE_LABELS, SPACE_NAMES, with_spaces

BOUND_S = 20.0
SPACE_ROOT = f"{MOUNT_PATH}/{SPACE_NAME}"  # the study space's directory as the agent sees it through the mount


def edit(data):
    with_placement(data)
    data["providers"]["gamma"] = {"url": f"https://{GAMMA_HOST}", "id": GAMMA_ID}
    data["support"] = {"providers": ["alpha", "beta", "gamma"], "local": "beta"}
    data["faults"] = faults_table(services={"alpha": SERVICE}, settle_timeout_s=30)


@pytest.fixture(autouse=True)
def no_container_consent(monkeypatch):
    """No test here may start a real container: the consent a live run exports is taken away."""
    monkeypatch.delenv("EXPCORE_LIVE_DOCKER", raising=False)


@pytest.fixture(scope="module")
def tools_manifest(tmp_path_factory):
    """The stand-in tool server's frozen manifest, frozen once for the module (a server takes a second to start)."""
    path = tmp_path_factory.mktemp("manifest") / KNOWLEDGE_MANIFEST
    freeze_tools_manifest(path)
    return path


@pytest.fixture
def world(fake, ticker) -> OutageWorld:
    fake.add_provider(GAMMA_ID, GAMMA_HOST)
    fake.supports[BETA_ID] = {"readonly": False}
    return OutageWorld(fake, ticker)


@pytest.fixture
def agent(world) -> AgentWorld:
    """What the agent meets: the mount of the sandbox on the fake host, and the tools, through beta, the local one."""
    return AgentWorld(world, "beta.test", SPACE_ROOT)


@pytest.fixture
def relay(agent):
    relay = ToolRelay(agent)
    yield relay
    relay.close()


@pytest.fixture
def services(world) -> FakeServices:
    return FakeServices(world, {SERVICE: "alpha.test"})


def write_target(directory, fake, relay, tools_manifest, extra=None):
    """A platform.toml for the outage with the agent's plane: the sandbox, the slots and the stand-in tool server."""
    directory.mkdir()

    def full(data):
        edit(data)
        with_agent(data, relay.url)
        if extra is not None:
            extra(data)

    platform = write_platform(directory, full)
    shutil.copy(tools_manifest, directory / KNOWLEDGE_MANIFEST)
    add_slot_users(fake, directory)
    return platform


@pytest.fixture
def platform(tmp_path, fake, relay, tools_manifest):
    return write_target(tmp_path / "target", fake, relay, tools_manifest)


@pytest.fixture
def context(platform, world, agent, services, ticker, tmp_path):
    def make(target=None, **options):
        path = target or platform
        client = OnezoneClient(load_platform(path), http=world.client_http())
        settings = {
            "client": client,
            "driver": services,
            "allow_mutation": True,
            "allow_faults": True,
            "outage_bound_s": BOUND_S,
            "clock": ticker.clock,
            "sleep": ticker.sleep,
            "now": lambda: NOW,
            "tcp": None,
            "agent_options": agent_options(),
        }
        if "agent_host" not in options:
            settings["agent_host"] = agent_host(path, agent)
        return build_context(path, tmp_path / "out", **{**settings, **options})

    return make


def outage_check(context, **options):
    return run_checks(context(**options), ["provider_outage"])[0]


# -- the outage is observed --------------------------------------------------------------------------------------------


def test_o28_the_provider_is_taken_down_seen_down_and_restored_and_nothing_is_left_down(context, world, services):
    """O28: the provider is taken down seen down and restored and nothing is left down."""
    entry = outage_check(context)
    assert entry.ok is True, entry.summary
    assert services.verbs() == ["stop", "start"]
    assert world.down == set()
    assert entry.facts["outage"]["started"] is True
    assert entry.facts["outage"]["seen_down"] is True
    assert entry.facts["outage"]["rest_answers_while_down"] is False
    assert entry.facts["outage"]["rest"]["transport"] is True
    assert entry.facts["restore"]["cleared"] is True
    assert entry.facts["restore"]["verified"] is True
    assert entry.facts["restore"]["how"] == "restored"
    assert entry.facts["restore"]["ready_after_s"] == 0  # the injector found it serving at its first reading
    assert entry.facts["restore"]["rest_answers_again"] is True
    assert entry.facts["cleanup_verified"] is True


def test_o28_the_outage_is_the_fault_injectors_journalled_held_exclusively_and_settled(context, platform, services):
    """O28: the provider is taken down by the fault injector of #27, not a copy of it: the injection is journalled
    before the stop and cleared after the restore, and while it is down the federation is held exclusively by an
    outage run whose holder record names the target by its label."""
    settings = load_faults(platform, load_platform(platform))
    journal = InjectionJournal(settings.journal)
    seen = {}

    def while_stopping(service):
        seen["open"] = [entry["target"] for entry in journal.open_entries()]
        seen["holder"] = journal.read_holder()

    services.on_stop = while_stopping
    entry = outage_check(context)
    assert entry.ok is True, entry.summary
    assert seen["open"] == ["providers[0]"]  # journalled before the stop
    assert (seen["holder"]["purpose"], seen["holder"]["target"]) == (OUTAGE_RUN, "providers[0]")
    assert journal.open_entries() == []  # settled once restored
    assert journal.read_holder() is None  # and the federation let go
    assert entry.facts["admission"] == {"exclusive": True}
    assert entry.facts["restore"]["journal_steps"][-2:] == ["cleared", "verified"]


def test_o28_a_stop_whose_outcome_is_unknown_is_waited_out_and_restored_by_the_injector(
    platform, world, ticker, tmp_path
):
    """O28: a stop command that times out (the CLI killed, the daemon still stopping) is settled the injector's way:
    the restore waits for the service to show down, then starts it and verifies the changed identity; the check is
    unknown, never passed, and leaves nothing down."""
    daemon = WorldDaemon(world, {SERVICE: "alpha.test"})
    daemon.stop_hangs = True
    config = load_platform(platform)
    settings = load_faults(platform, config)
    client = OnezoneClient(config, http=world.client_http())
    driver = compose_driver(settings, runner=daemon, probe=provider_serves(config, settings, client))
    ctx = build_context(
        platform, tmp_path / "o", client=client, driver=driver, allow_mutation=True,
        allow_faults=True, clock=ticker.clock, sleep=ticker.sleep, outage_bound_s=BOUND_S,
    )  # fmt: skip
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is None
    assert entry.facts["outage"]["started"] is False
    assert (entry.facts["restore"]["cleared"], entry.facts["restore"]["how"]) == (True, "restored")
    assert daemon.starts(SERVICE) == 2
    assert daemon.running(SERVICE)
    assert world.down == set()
    assert InjectionJournal(settings.journal).open_entries() == []


def test_o28_a_run_cancelled_while_the_provider_is_stopped_waits_for_the_stop_and_restores_it(
    context, platform, services, world, monkeypatch
):
    """O28: the outage is the injector's `running` (#27): the start runs in a worker thread a cancellation cannot stop,
    so a run cancelled while its provider is being stopped waits for the stop, restores the provider in `finally` and
    only then ends with the cancellation, leaving nothing down and nothing open in the injector's journal."""
    held = {}
    real_main = outage_run.Run.main

    async def main(self):
        held["task"], held["loop"] = asyncio.current_task(), asyncio.get_running_loop()
        await real_main(self)

    monkeypatch.setattr(outage_run.Run, "main", main)
    services.on_stop = lambda service: held["loop"].call_soon_threadsafe(held["task"].cancel)
    with pytest.raises(asyncio.CancelledError):
        outage_check(context)
    assert services.verbs() == ["stop", "start"]
    assert world.down == set()
    assert InjectionJournal(load_faults(platform, load_platform(platform)).journal).open_entries() == []


def test_o28_a_provider_that_still_answers_while_its_service_is_stopped_is_no_observed_outage(context, services):
    """O28: a provider that still answers while its service is stopped is no observed outage."""
    services.stop_has_no_effect = True  # the stop command returns, nothing goes down
    entry = outage_check(context)
    assert entry.ok is None  # never seen down: nothing to measure, and nothing to restore
    assert "could not be taken down" in entry.summary
    assert entry.facts["outage"]["started"] is False
    assert "during" not in entry.facts
    assert (entry.facts["restore"]["cleared"], entry.facts["restore"]["how"]) == (True, "never_down")


def test_o28_a_service_seen_down_whose_rest_still_answers_fails_the_check(context, world, services):
    """O28: a service seen down whose rest still answers fails the check."""
    real_stop = services.stop

    def stop_only_the_container(service: str) -> None:
        real_stop(service)
        world.down.discard("alpha.test")  # the container is stopped, and something still answers for it

    services.stop = stop_only_the_container  # type: ignore[method-assign]
    entry = outage_check(context)
    assert entry.ok is False
    assert "no outage was observed" in entry.summary
    assert entry.facts["outage"]["rest_answers_while_down"] is True
    assert "during" not in entry.facts
    assert entry.facts["restore"]["verified"] is True
    assert entry.facts["restore"]["rest_answers_again"] is True


# -- the answers are recorded ------------------------------------------------------------------------------------------


def test_o28_the_answers_of_each_path_are_recorded_with_status_latency_and_outcome(context):
    """O28: the answers of each path are recorded with status latency and outcome."""
    entry = outage_check(context)
    assert entry.ok is True, entry.summary
    rest = entry.facts["during"]["rest"]
    assert rest["replicated"]["outcome"] == "served"
    assert rest["replicated"]["hash_matches"] is True
    assert rest["replicated"]["bytes"] == rest["replicated"]["expected_bytes"] == outage_run.FILE_BYTES
    assert rest["owner_only"]["outcome"] == "blocked"  # no provider that is up holds the bytes: it waited its bound
    assert rest["owner_only"]["seconds"] == BOUND_S
    assert rest["owner_only"]["bound_s"] == BOUND_S
    assert rest["owner_only"]["transport"] is True
    assert rest["owner_only"]["bytes"] == 0
    assert rest["listing"]["outcome"] == "served"
    assert rest["listing"]["entries"] == rest["listing"]["expected_entries"]
    assert entry.facts["during"]["knowledge"]["get_file_distribution"]["replicated"]["outcome"] == "served"


def test_o28_a_read_the_platform_refuses_records_its_status_and_error_id(context, world):
    """O28: a read the platform refuses records its status and error id."""
    world.reads_while_down = "refuse"
    entry = outage_check(context)
    refused = entry.facts["during"]["rest"]["owner_only"]
    assert (refused["outcome"], refused["status"], refused["code"], refused["transport"]) == (
        "refused",
        500,
        "internalServerError",
        False,
    )
    assert refused["seconds"] < BOUND_S
    assert entry.ok is True  # what a read answered is a fact, not a failure


def test_o28_the_knowledge_plane_is_the_agents_own_tools_called_through_mcp_all_up_and_during_the_outage(
    context, agent
):
    """O28: the knowledge plane is measured through the condition's tool server, started as the sandbox starts it (the
    leased slot's tool-server user and token), its four tools called through MCP with everything up and while the
    provider is down; each call records its outcome, seconds against the bound, whether it timed out and its attempts,
    and the transfer of a file no up provider holds is told from the one that finished."""
    entry = outage_check(context)
    assert entry.ok is True, entry.summary
    assert entry.facts["agent"]["tool_server"]["started"] is True
    assert {name for name, _ in agent.tool_calls} == set(KNOWLEDGE_TOOLS)  # every call came through the server
    baseline = entry.facts["baseline"]["knowledge"]
    assert baseline["get_file_distribution"]["outcome"] == "served"
    assert baseline["schedule_file_replication"]["get_transfer"]["ended"] is True
    knowledge, labels = entry.facts["during"]["knowledge"], entry.facts["roles"]
    owner_only = knowledge["get_file_distribution"]["owner_only"]
    assert (owner_only["outcome"], owner_only["timed_out"], owner_only["attempts"]) == ("served", False, 1)
    assert owner_only["bound_s"] == BOUND_S
    assert owner_only["providers"][labels["target"]] == {"listed": True, "held_bytes": outage_run.FILE_BYTES}
    assert owner_only["providers"][labels["mirror"]]["held_bytes"] == 0
    assert knowledge["list_space_providers"]["listed"] == dict.fromkeys(labels.values(), True)
    scheduled = knowledge["schedule_file_replication"]
    assert scheduled["replicated"]["schedule_file_replication"]["outcome"] == "served"
    assert scheduled["replicated"]["get_transfer"]["ended"] is True
    assert scheduled["replicated"]["get_transfer"]["states"][-1][1] == "completed"
    assert scheduled["owner_only"]["get_transfer"]["ended"] is False
    assert scheduled["owner_only"]["get_transfer"]["outcome"] == "blocked"
    assert scheduled["owner_only"]["get_transfer"]["calls"] > 1
    assert entry.facts["after"]["transfers"]["owner_only"]["outcome"] == "served"


def test_o28_from_where_is_measured_on_the_files_actually_read_during_the_outage(context, agent):
    """O28: where each file read during the outage is held is asked of the agent's own tool, `get_file_distribution`
    of the very files read over REST and through the mount, while the provider is down: a replicated file read
    through the local provider is held there afterwards, as the mirror holds it, and the file only the downed
    provider holds is not."""
    entry = outage_check(context)
    assert entry.ok is True, entry.summary
    where, labels, size = entry.facts["during"]["knowledge"]["from_where"], entry.facts["roles"], outage_run.FILE_BYTES
    assert set(where) == {"rest/replicated", "rest/owner_only", "mount/replicated", "mount/owner_only"}
    for pair in ("rest", "mount"):
        replicated, owner_only = where[f"{pair}/replicated"], where[f"{pair}/owner_only"]
        assert (replicated["outcome"], replicated["file"]) == ("served", f"{pair}/replicated")
        assert replicated["providers"][labels["local"]]["held_bytes"] == size
        assert replicated["providers"][labels["mirror"]]["held_bytes"] == size
        assert owner_only["providers"][labels["local"]]["held_bytes"] == 0
        assert owner_only["providers"][labels["target"]]["held_bytes"] == size
    asked = [arguments["file_id"] for name, arguments in agent.tool_calls if name == "get_file_distribution"]
    assert len(asked) == len(set(asked)) == 1 + 2 + 4  # the baseline's, the knowledge pair's, the four files read


def test_o28_the_stand_in_tools_refuse_a_replication_elsewhere_and_a_space_not_under_test(agent, world):
    """O28: the stand-in tools check the agent's arguments: a replication to the provider taken down or to the mirror,
    and the providers of a space not under test, are answered errors and kept as misuses; the right ones serve."""
    file_id = world.fake.add_file("probe.bin", b"x", holder=ALPHA_ID)
    for provider in (ALPHA_ID, GAMMA_ID):
        assert "error" in agent.tool("schedule_file_replication", {"file_id": file_id, "provider_id": provider})
    assert "error" in agent.tool("list_space_providers", {"space_id": SPACE_IDS[1]})
    assert [name for name, _ in agent.misused] == ["schedule_file_replication"] * 2 + ["list_space_providers"]
    assert "result" in agent.tool("schedule_file_replication", {"file_id": file_id, "provider_id": BETA_ID})
    assert "result" in agent.tool("list_space_providers", {"space_id": SPACE_IDS[0]})
    assert len(agent.misused) == 3


@pytest.mark.parametrize(
    ("tool", "kind", "role"),
    [
        ("schedule_file_replication", "provider", "target"),
        ("schedule_file_replication", "provider", "mirror"),
        ("list_space_providers", "space", "local"),
    ],
)
def test_o28_a_tool_called_with_a_wrong_id_is_never_a_pass(context, agent, services, monkeypatch, tool, kind, role):
    """O28: a check that schedules the replication to the provider taken down or to the mirror, or asks for the
    providers of anything but the space under test, is answered an error with everything up: the baseline does not
    serve, nothing is stopped and the check is unknown, never passed."""
    real = outage_tools.Tools.call

    async def wrong(self, name, values):
        if name == tool:
            values = {**values, kind: self._roles.ids[role]}
        return await real(self, name, values)

    monkeypatch.setattr(outage_tools.Tools, "call", wrong)
    entry = outage_check(context)
    assert entry.ok is None
    assert f"knowledge tools did not serve with everything up ({tool})" in entry.summary
    assert agent.misused
    assert {name for name, _ in agent.misused} == {tool}
    assert services.verbs() == []


@pytest.mark.parametrize("tool", ["get_file_distribution", "get_transfer"])
def test_o28_a_knowledge_tool_whose_schema_cannot_be_filled_is_unmeasured_and_the_check_unknown(
    context, agent, monkeypatch, tool
):
    """O28: a knowledge tool whose input schema the check cannot fill is not called: it is unmeasured, named by its
    tool and the reason, and the check is unknown, never passed. Only that gap keeps it from passing: the baseline's
    own test skips a cell that was not measured."""
    real = outage_tools.arguments
    reason = "its input schema requires 1 argument(s) the check cannot fill"

    def unfillable(name, schema, values):
        return reason if name == tool else real(name, schema, values)

    monkeypatch.setattr(outage_tools, "arguments", unfillable)
    entry = outage_check(context)
    assert entry.ok is None
    assert f"during the outage, {tool}: {reason}" in entry.summary
    assert "None: None" not in entry.summary
    assert tool not in {name for name, _ in agent.tool_calls}
    assert entry.facts["restore"]["verified"] is True


def test_o28_a_tool_that_answers_an_error_keeps_its_wordings_digest_and_its_attempts(context, agent):
    """O28: a read-only tool that answers an error while the provider is down is called again, at most three times,
    and each error's wording is recorded as a digest (the SHA-256 prefix of the text with every known name and id-like
    token replaced), its length and the error terms it holds, never as text."""
    agent.tool_errors = {"get_file_distribution": f"https://alpha.test ({ALPHA_ID}) is unreachable for {SPACE_NAME}"}
    entry = outage_check(context)
    cell = entry.facts["during"]["knowledge"]["get_file_distribution"]["owner_only"]
    assert (cell["outcome"], cell["attempts"], len(cell["errors"])) == ("refused", 3, 3)
    assert set(cell["error"]) == {"sha256", "chars", "terms"}
    assert len(cell["error"]["sha256"]) == 16
    assert cell["errors"] == [cell["error"]["sha256"]] * 3  # the same wording each time
    assert cell["error"]["terms"] == ["unreachable"]
    for needle in ("alpha.test", ALPHA_ID, SPACE_NAME):
        assert needle not in json.dumps(entry.facts), needle
    assert entry.ok is True  # what a tool answered is a fact, not a failure


def test_o28_no_name_in_a_tools_answer_reaches_the_facts_whether_the_check_knows_it_or_not(context, agent):
    """O28: a tool's answers and errors are free text holding names the check cannot know: a slot user's id in a
    transfer record, a provider's registered name that is not its key in platform.toml, an address. None of them
    reaches the facts: only structured fields are kept (outcome, seconds, attempts, the providers' held bytes by
    label, a transfer's state from a known set), and a text only as its digest, length and error terms."""
    user_id = "u-slot-a-tools"  # the leased slot's tool-server user
    foreign, address = "Northwind Archive", "192.0.2.17"
    agent.answer_extra = {
        "get_transfer": {"userId": user_id, "replicatingProviderName": foreign, "source": address},
        "get_file_distribution": {"note": f"{foreign} at {address}"},
    }
    agent.tool_errors = {"list_space_providers": f"{foreign} at {address} refused user {user_id}: timed out"}
    entry = outage_check(context)
    assert entry.ok is True, entry.summary
    text = json.dumps({"summary": entry.summary, "facts": entry.facts})
    for needle in (user_id, foreign, "Northwind", address):
        assert needle not in text, needle
    providers = entry.facts["during"]["knowledge"]["list_space_providers"]
    assert providers["outcome"] == "refused"
    assert providers["error"]["terms"] == ["timed out", "refused"]
    follow = entry.facts["during"]["knowledge"]["schedule_file_replication"]["replicated"]["get_transfer"]
    assert follow["states"][-1][1] == "completed"
    assert set(follow["last_call"]["answer"]) == {"sha256", "chars", "terms"}
    owner_only = entry.facts["during"]["knowledge"]["get_file_distribution"]["owner_only"]
    assert owner_only["providers"][entry.facts["roles"]["target"]]["held_bytes"] == outage_run.FILE_BYTES


def test_o28_a_tool_that_never_answers_is_blocked_at_the_bound(context, agent):
    """O28: a tool call that does not return within the bound is given up on and recorded as blocked and timed out,
    never as an answer; the server stays usable for the calls after it."""
    agent.tool_hangs = {"list_space_providers"}
    agent.hang_s = 1.0
    entry = outage_check(context, outage_bound_s=1.0)
    cell = entry.facts["during"]["knowledge"]["list_space_providers"]
    assert (cell["outcome"], cell["timed_out"], cell["attempts"]) == ("blocked", True, 1)
    assert cell["seconds"] >= 1.0
    assert entry.facts["during"]["knowledge"]["schedule_file_replication"]["replicated"]["get_transfer"]["ended"]


def test_o28_tools_that_fail_with_everything_up_stop_nothing(context, agent, services):
    """O28: a tool that answers an error with every provider up says nothing about an outage, so the scenario stops
    no provider and the check is unknown."""
    agent.tool_broken = {"schedule_file_replication": "refused: the user may not schedule a replication"}
    entry = outage_check(context)
    assert entry.ok is None
    assert "knowledge tools did not serve with everything up" in entry.summary
    assert services.verbs() == []


def test_o28_a_listing_that_lost_entries_while_the_provider_was_down_is_partial(context, world):
    """O28: a listing that lost entries while the provider was down is partial."""
    world.listing_loses = 2
    entry = outage_check(context)
    listing = entry.facts["during"]["rest"]["listing"]
    assert (listing["outcome"], listing["missing"], listing["unexpected"]) == ("partial", 2, 0)
    assert entry.facts["baseline"]["rest"]["listing"]["outcome"] == "served"  # with everything up it was whole
    assert entry.facts["after"]["listing"]["outcome"] == "served"
    assert entry.ok is True


def test_o28_a_listing_that_fails_partway_is_partial_with_what_it_listed_and_the_error(context, world, monkeypatch):
    """O28: a listing whose second page fails while the provider is down reports the entries it got as partial, with
    the error's status, never as nothing listed."""
    monkeypatch.setattr(outage_cells, "LISTING_LIMIT", 2)
    world.listing_cut = True
    entry = outage_check(context)
    listing = entry.facts["during"]["rest"]["listing"]
    assert (listing["outcome"], listing["entries"], listing["status"]) == ("partial", 2, 500)
    assert listing["missing"] == listing["expected_entries"] - 2
    assert entry.facts["baseline"]["rest"]["listing"]["outcome"] == "served"


def test_o28_a_listing_whose_sizes_changed_while_the_provider_was_down_differs(context, world):
    """O28: a listing whose sizes changed while the provider was down differs."""
    world.listing_resizes = True
    entry = outage_check(context)
    listing = entry.facts["during"]["rest"]["listing"]
    assert (listing["outcome"], listing["size_differs"], listing["missing"]) == ("differs", listing["entries"], 0)


def test_o28_a_failed_baseline_means_no_attributable_difference_so_nothing_is_stopped(context, world, services):
    """O28: a failed baseline means no attributable difference so nothing is stopped."""
    world.fake.statuses.append(["/content", 10**6, 500])  # every read of content fails, with everything up
    entry = outage_check(context)
    assert entry.ok is None
    assert "nothing was taken down" in entry.summary
    assert services.verbs() == []
    assert entry.facts["cleanup_verified"] is True


# -- the restore is verified, and a failed restore fails the check ---------------------------------


def test_o28_a_restore_that_leaves_the_identity_unchanged_fails_the_check(context, services, world):
    """O28: a restore that leaves the identity unchanged fails the check."""
    services.keeps_identity = True
    entry = outage_check(context)
    assert entry.ok is False
    assert "could not be brought back (identity_unchanged)" in entry.summary
    assert entry.facts["restore"] == {
        **entry.facts["restore"],
        "cleared": False,
        "verified": False,
        "how": "identity_unchanged",
        "identity_changed": False,
    }
    assert world.down == set()  # it answers again, and that is not a restore


def test_o28_a_provider_that_does_not_run_again_fails_the_check_after_trying_to_start_it_again(
    context, services, world
):
    """O28: a provider that does not run again fails the check after trying to start it again."""
    services.stays_down = True
    entry = outage_check(context)
    assert entry.ok is False
    assert (entry.facts["restore"]["cleared"], entry.facts["restore"]["verified"]) == (False, False)
    assert entry.facts["restore"]["how"] == "not_running"
    assert services.verbs() == ["stop", "start", "start"]  # clear_attempts = 2 in the fixture's [faults]
    assert "cleanup_verified" in entry.facts  # the teardown ran, and could not reach the provider it deletes through


def stay_unreachable(services, world):
    """Make the provider's service start with a new identity and its host answer nothing."""
    real_start = services.start

    def start_but_stay_unreachable(service: str) -> None:
        real_start(service)
        world.down.add("alpha.test")

    services.start = start_but_stay_unreachable  # type: ignore[method-assign]


def test_o28_a_provider_that_runs_but_does_not_serve_after_the_restore_fails_the_check(context, world, services):
    """O28: a provider back with a changed identity whose REST does not answer does not serve, so the injector of #27
    leaves its restore unverified (`not_serving`), the injection open, and the check fails."""
    stay_unreachable(services, world)
    entry = outage_check(context)
    assert entry.ok is False
    restore = entry.facts["restore"]
    assert (restore["cleared"], restore["verified"], restore["how"]) == (False, False, "not_serving")
    assert "could not be brought back (not_serving)" in entry.summary


def test_o28_a_provider_whose_driver_finds_it_serving_and_whose_rest_does_not_answer_fails_the_check(
    context, world, services
):
    """O28: the check asks the target's REST itself after the injector's restore: a driver that finds the provider
    serving while the target does not answer the check's own read still fails the check, by the settle timeout."""
    stay_unreachable(services, world)
    services.serves = lambda service: True  # type: ignore[method-assign]
    entry = outage_check(context)
    assert entry.ok is False
    assert entry.facts["restore"]["verified"] is True  # the identity changed and the driver found it serving
    assert entry.facts["restore"]["rest_answers_again"] is False
    assert "did not answer REST" in entry.summary


def test_o28_the_restore_runs_when_a_measurement_raises(context, services, world, monkeypatch):
    """O28: the restore runs when a measurement raises."""
    real = Cells.listing

    def broken(self, directory_id, expected):
        if world.down:  # a defect in a measurement made while the provider is down
            raise ValueError("a defect in a measurement")
        return real(self, directory_id, expected)

    monkeypatch.setattr(Cells, "listing", broken)
    entry = outage_check(context)
    assert entry.ok is False
    assert "a measurement raised ValueError" in entry.summary
    assert services.verbs()[-1] == "start"
    assert world.down == set()
    assert entry.facts["restore"]["verified"] is True


def test_o28_a_stop_command_that_fails_leaves_the_injection_unsettled_and_fails_the_check(context, services, world):
    """O28: a stop command that fails has an outcome the injector cannot know; the service never shows down within the
    settle timeout, so the injection stays open, unverified, and the check fails instead of calling it as it was."""
    services.fail_stop = True
    entry = outage_check(context)
    assert entry.ok is False
    assert entry.facts["outage"]["started"] is False
    assert (entry.facts["restore"]["cleared"], entry.facts["restore"]["how"]) == (False, "stop_unsettled")
    assert "could not be brought back" in entry.summary
    assert world.down == set()


def test_o28_a_service_that_cannot_be_read_before_the_stop_is_not_stopped(context, services):
    """O28: a service that cannot be read before the stop is not stopped."""
    services.unreadable = True
    entry = outage_check(context)
    assert entry.ok is None
    assert services.verbs() == []
    assert entry.facts["restore"]["how"] == "not_injected"


# -- consent, the sanction and the scenario --------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "needs"),
    [
        ({"allow_mutation": False}, "EXPCORE_LIVE_MUTATE=1"),
        ({"allow_faults": False}, "EXPCORE_LIVE_FAULTS=1"),
    ],
)
def test_o28_without_both_consents_nothing_is_stopped_or_sent(context, services, world, options, needs):
    """O28: without both consents nothing is stopped or sent."""
    entry = outage_check(context, **options)
    assert entry.ok is None
    assert needs in entry.summary
    assert services.calls == []
    assert [c for c in world.fake.provider_calls if c[1] in ("POST", "PUT", "DELETE")] == []


def test_o28_without_the_sanction_in_platform_toml_nothing_is_stopped(tmp_path, world, services, ticker):
    """O28: without the sanction in platform toml nothing is stopped."""
    directory = tmp_path / "unsanctioned"
    directory.mkdir()

    def unsanctioned(data):
        edit(data)
        data["faults"]["sanctioned"] = False

    platform = write_platform(directory, unsanctioned)
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    ctx = build_context(
        platform, tmp_path / "o", client=client, driver=services, allow_mutation=True, allow_faults=True,
        clock=ticker.clock, sleep=ticker.sleep,
    )  # fmt: skip
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is None
    assert "sanctioned = true" in entry.summary
    assert services.calls == []


def test_o28_a_platform_without_a_faults_table_gives_no_driver_and_stops_nothing(tmp_path, world, ticker):
    """O28: a platform without a faults table gives no driver and stops nothing."""
    directory = tmp_path / "plain"
    directory.mkdir()

    def plain(data):
        edit(data)
        del data["faults"]

    platform = write_platform(directory, plain)
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    ctx = build_context(platform, tmp_path / "o", client=client, allow_mutation=True, allow_faults=True)
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is None
    assert "sanctioned = true" in entry.summary


def test_o28_a_space_whose_data_provider_is_the_agents_local_provider_has_no_outage_to_read_through(
    tmp_path, world, services, ticker
):
    """O28: a space whose data provider is the agents local provider has no outage to read through."""
    directory = tmp_path / "same"
    directory.mkdir()

    def same(data):
        edit(data)
        data["support"]["local"] = "alpha"

    platform = write_platform(directory, same)
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    ctx = build_context(
        platform, tmp_path / "o", client=client, driver=services, allow_mutation=True, allow_faults=True,
        clock=ticker.clock, sleep=ticker.sleep,
    )  # fmt: skip
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is None
    assert "local provider" in entry.summary
    assert "providers[0]" in entry.summary
    assert services.calls == []


def test_o28_a_support_set_without_a_third_provider_has_no_replica_to_hold(tmp_path, world, services, ticker):
    """O28: a support set without a third provider has no replica to hold."""
    directory = tmp_path / "two"
    directory.mkdir()

    def two(data):
        edit(data)
        data["support"]["providers"] = ["alpha", "beta"]

    platform = write_platform(directory, two)
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    ctx = build_context(
        platform, tmp_path / "o", client=client, driver=services, allow_mutation=True, allow_faults=True,
        clock=ticker.clock, sleep=ticker.sleep,
    )  # fmt: skip
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is None
    assert "no provider besides the data provider and the local one" in entry.summary
    assert services.calls == []


def test_o28_the_scenario_is_proved_by_the_local_providers_own_block_map_before_anything_is_stopped(context, world):
    """O28: the scenario is proved by the local providers own block map before anything is stopped."""
    entry = outage_check(context)
    held = entry.facts["held_before"]
    labels = entry.facts["roles"]
    size = outage_run.FILE_BYTES
    assert held["rest/owner_only"] == {labels["target"]: size, labels["mirror"]: 0, labels["local"]: 0}
    assert held["rest/replicated"] == {labels["target"]: size, labels["mirror"]: size, labels["local"]: 0}
    pairs = (*outage_run.BASE_PAIRS, *outage_run.MOUNT_PAIRS)  # the platform declares a mount: its pairs too
    assert set(held) == {f"{pair}/{kind}" for pair in pairs for kind in outage_run.KINDS}


def test_o28_a_file_that_the_local_provider_already_holds_is_not_one_held_only_by_the_target(
    context, world, services, monkeypatch
):
    """O28: a file that the local provider already holds is not one held only by the target."""
    real = outage_run.Run.prepare

    def local_holds_one(self):
        real_blocks = self.provisioner.blocks

        def blocks(file_id, provider, *, reader=None, **options):
            found = real_blocks(file_id, provider, reader=reader, **options)  # the setup passes its `retry` (O29)
            if provider == self.roles.local and file_id == self.files[("rest", "owner_only")].file_id:
                return [(0, outage_run.FILE_BYTES)]
            return found

        self.provisioner.blocks = blocks  # the local provider is shown holding a file it must not
        real(self)

    monkeypatch.setattr(outage_run.Run, "prepare", local_holds_one)
    entry = outage_check(context)
    assert entry.ok is None
    assert "not held as the scenario needs" in entry.summary
    assert services.verbs() == []


# -- names -----------------------------------------------------------------------------------------


def test_o28_providers_and_spaces_are_named_by_position_and_nothing_identifying_is_recorded(
    context, platform, world, services, tmp_path, ticker
):
    """O28: providers and spaces are named by position and nothing identifying is recorded."""
    entry = outage_check(context)
    assert entry.ok is True
    text = json.dumps({"summary": entry.summary, "facts": entry.facts})
    for needle in (
        "alpha",
        "beta",
        "gamma",
        SPACE_NAME,
        ALPHA_ID,
        BETA_ID,
        GAMMA_ID,
        SERVICE,
        "compose.yaml",
        "container-",
        "stack-a",
        "fault-journal",
    ):
        assert needle not in text, needle
    assert entry.facts["roles"] == {"target": "providers[0]", "mirror": "providers[2]", "local": "providers[1]"}
    assert "providers[0]" in entry.summary
    assert len(entry.facts["restore"]["identity_after_sha256"]) == 64
    assert len(entry.facts["outage"]["identity_before_sha256"]) == 64


def test_o28_the_report_of_a_whole_run_holds_the_check_and_none_of_the_names(
    platform, world, agent, services, tmp_path, ticker
):
    """O28: the report of a whole run holds the check and none of the names."""
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    code, json_path, md_path = run(
        platform, tmp_path / "reports", client=client, driver=services, allow_mutation=True, allow_faults=True,
        clock=ticker.clock, sleep=ticker.sleep, now=lambda: NOW, tcp=lambda *a: 1.0,
        outage_bound_s=BOUND_S, names=["provider_outage"], agent_host=agent_host(platform, agent),
        agent_options=agent_options(),
    )  # fmt: skip
    text = json_path.read_text(encoding="utf-8") + md_path.read_text(encoding="utf-8")
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert [(c["name"], c["status"], c["mutates"]) for c in report["checks"]] == [("provider_outage", "pass", True)]
    assert code == 0
    for needle in ("alpha.test", "beta.test", GAMMA_HOST, SPACE_NAME, SERVICE):
        assert needle not in text, needle


def test_o28_a_failed_restore_gives_the_failing_exit_status(platform, world, services, tmp_path, ticker):
    """O28: a failed restore gives the failing exit status."""
    services.keeps_identity = True
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    code, _, _ = run(
        platform, tmp_path / "reports", client=client, driver=services, allow_mutation=True, allow_faults=True,
        clock=ticker.clock, sleep=ticker.sleep, now=lambda: 1_800_000_000.0, tcp=None,
        outage_bound_s=BOUND_S, names=["provider_outage"],
    )  # fmt: skip
    assert code == EXIT_FAIL


# -- the mount -------------------------------------------------------------------------------------


def test_o28_the_mount_is_read_through_a_real_sandbox_in_mount_mode_all_up_and_during_the_outage(context, agent):
    """O28: the mount is read in the agent's container of the sandbox package's sandbox in mount mode (its sidecar
    owning the mount): the replicated file, the file only the downed provider holds and a listing, each with its
    outcome, seconds and whether it blocked, with everything up and while the provider is down."""
    entry = outage_check(context)
    assert entry.ok is True, entry.summary
    assert entry.facts["mount"] == {"declared": True, "measured": True}
    assert entry.facts["agent"]["sandbox"] == {**entry.facts["agent"]["sandbox"], "started": True, "data_path": "mount"}
    baseline = entry.facts["baseline"]["mount"]
    assert {name: row["outcome"] for name, row in baseline.items()} == dict.fromkeys(
        ("replicated", "owner_only", "listing"), "served"
    )
    during = entry.facts["during"]["mount"]
    assert (during["replicated"]["outcome"], during["replicated"]["blocked"]) == ("served", False)
    assert during["replicated"]["hash_matches"] is True
    owner_only = during["owner_only"]
    assert (owner_only["outcome"], owner_only["blocked"], owner_only["seconds"]) == ("blocked", True, BOUND_S)
    assert during["listing"]["outcome"] == "served"
    assert entry.facts["after"]["mount_owner_only"]["outcome"] == "served"
    assert ("read", "owner_only") in agent.mount_ops


def test_o28_a_mount_read_that_times_out_at_the_bound_is_blocked_with_its_errno(context, agent):
    """O28: a mount's ETIMEDOUT, which Python reports as TimeoutError, after the read waited its bound got no answer:
    it is `blocked`, never a refusal, and the cell keeps the errno's name and number and the exception's class (the
    child was not killed, so its own `blocked` flag stays false)."""
    agent.mount_down = ("ETIMEDOUT", 110, "TimeoutError")
    entry = outage_check(context)
    owner_only = entry.facts["during"]["mount"]["owner_only"]
    assert (owner_only["outcome"], owner_only["errno"], owner_only["errno_number"]) == ("blocked", "ETIMEDOUT", 110)
    assert (owner_only["error_kind"], owner_only["blocked"]) == ("TimeoutError", False)
    assert entry.ok is True


def test_o28_a_mount_read_that_fails_with_another_errno_is_refused_with_it(context, agent):
    """O28: a read through the mount that fails with an errno other than a timeout's is refused, with that errno."""
    agent.mount_down = ("EIO", 5, "OSError")
    entry = outage_check(context)
    owner_only = entry.facts["during"]["mount"]["owner_only"]
    assert (owner_only["outcome"], owner_only["errno"], owner_only["errno_number"]) == ("refused", "EIO", 5)


def test_o28_the_agents_tokens_are_minted_for_the_scenarios_worst_case(
    tmp_path, fake, relay, tools_manifest, context, monkeypatch
):
    """O28: the sandbox's and the tool server's tokens are minted for the longest the scenario can need them (the
    injector's waits, and a settle of 600 s for the wait for the target and for each re-read among them) plus a margin,
    never for a fixed 1800 s that a slow restore outlasts."""
    target = write_target(
        tmp_path / "slow", fake, relay, tools_manifest, lambda data: data["faults"].update(settle_timeout_s=600)
    )
    seen = []
    real_launch, real_server = SandboxPlatform.launch, SandboxPlatform.tool_server

    def launch(self, *args, ceiling_s, **options):
        seen.append(ceiling_s)
        return real_launch(self, *args, ceiling_s=ceiling_s, **options)

    def tool_server(self, *args, ceiling_s, **options):
        seen.append(ceiling_s)
        return real_server(self, *args, ceiling_s=ceiling_s, **options)

    monkeypatch.setattr(SandboxPlatform, "launch", launch)
    monkeypatch.setattr(SandboxPlatform, "tool_server", tool_server)
    ctx = context(target=target)
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is True, entry.summary
    assert len(seen) == 2
    assert seen[0] == seen[1] == outage_budget.scenario_s(ctx, mount=True)
    assert seen[0] >= 5 * 600 + 1800  # the wait and the four re-reads at the settle, and more
    assert entry.facts["agent"]["tokens"]["ceiling_s"] == seen[0]


def test_o28_a_platform_that_grants_no_token_as_long_as_the_scenario_needs_runs_no_sandbox(
    tmp_path, fake, relay, tools_manifest, context, services
):
    """O28: a platform whose `[tokens]` limit is below the lifetime the scenario needs refuses the mint, so no sandbox
    and no tool server start: the check is unknown with that reason, never run on tokens that may end midway."""

    def short_tokens(data):
        data.setdefault("tokens", {})["max_ttl_s"] = 3600

    target = write_target(tmp_path / "short", fake, relay, tools_manifest, short_tokens)
    entry = outage_check(context, target=target)
    assert entry.ok is None
    assert entry.facts["agent"]["sandbox"]["reason"].startswith("the sandbox did not start")
    assert entry.facts["mount"]["measured"] is False
    assert entry.facts["knowledge"]["measured"] is False


def test_o28_tokens_that_ended_before_a_reread_are_the_checks_own_failure_never_the_providers(
    context, services, ticker
):
    """O28: when the agent's tokens have ended before a step that uses them (a restore slower than the lifetime they
    were minted for), the step is not measured and the check fails with its own reason, naming the tokens; the
    provider is never blamed for a read the tokens could not make. REST reads use no slot token and are measured."""
    real_start = services.start

    def slow_start(service):
        ticker.sleep(10_000.0)  # a restore slower than the tokens' lifetime
        real_start(service)

    services.start = slow_start  # type: ignore[method-assign]
    ctx = context()
    ctx.outage_ceiling_s = 2_000.0
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is False
    assert "the agent's tokens had ended before the re-read through the mount" in entry.summary
    assert "did not return what was written" not in entry.summary
    after = entry.facts["after"]
    assert after["mount_owner_only"]["tokens_ended"] is True
    assert after["owner_only"]["outcome"] == "served"
    assert "transfers" not in after
    assert entry.facts["restore"]["verified"] is True


def test_o28_a_mount_baseline_that_does_not_serve_stops_nothing_and_the_check_is_unknown(context, agent, services):
    """O28: a mount whose reads fail with every provider up says nothing about an outage: the scenario stops no
    provider, the check is unknown with the reason, and the scratch is still torn down."""
    agent.mount_broken = ("EIO", 5, "OSError")
    entry = outage_check(context)
    assert entry.ok is None
    assert "the baseline did not read back through the mount" in entry.summary
    assert services.verbs() == []
    assert entry.facts["baseline"]["mount"]["owner_only"]["outcome"] == "refused"
    assert entry.facts["cleanup_verified"] is True


def test_o28_the_sandbox_and_the_tool_plane_are_stopped_when_a_measurement_raises(context, world, monkeypatch):
    """O28: a measurement that raises while the provider is down fails the check, and the sandbox's teardown and the
    tool plane's check at its stop still run, after the restore."""
    real = Cells.listing

    def broken(self, directory_id, expected):
        if world.down:
            raise ValueError("a defect in a measurement")
        return real(self, directory_id, expected)

    monkeypatch.setattr(Cells, "listing", broken)
    entry = outage_check(context)
    assert entry.ok is False
    assert (entry.facts["agent"]["sandbox"]["stopped"], entry.facts["agent"]["tool_server"]["stopped"]) == (True, True)


def test_o28_the_sandbox_and_the_tool_plane_are_stopped_when_the_run_itself_raises(
    context, platform, agent, world, monkeypatch
):
    """O28: an error that escapes the run (here, from the checks after the restore) fails the check, and on its way out
    the tool plane's check at its stop and the sandbox's teardown run, in that order, and leave no container."""
    left = []
    real_leave = outage_agent.AgentPlane._leave

    async def leave(self, manager, what):
        left.append(what)
        return await real_leave(self, manager, what)

    async def raising(self, went_down):
        raise RuntimeError("a defect after the restore")

    monkeypatch.setattr(outage_agent.AgentPlane, "_leave", leave)
    monkeypatch.setattr(outage_run.Run, "after_restore", raising)
    host = agent_host(platform, agent)
    entry = outage_check(context, agent_host=host)
    assert entry.ok is False
    assert "the check raised RuntimeError" in entry.summary
    assert left == ["the tool plane's check at its stop", "the sandbox's teardown"]
    assert host.containers == {}
    assert world.down == set()


def test_o28_a_declared_mount_left_unmeasured_makes_the_check_unknown_never_passed(context):
    """O28: without a host to start the sandbox on (EXPCORE_LIVE_DOCKER unset), a mount the platform declares is not
    read and the tools are not called: the REST part runs, and the check is unknown, with both reasons, never passed."""
    entry = outage_check(context, agent_host=None)
    assert entry.ok is None
    assert entry.facts["mount"]["declared"] is True
    assert entry.facts["mount"]["measured"] is False
    assert "EXPCORE_LIVE_DOCKER=1" in entry.facts["mount"]["reason"]
    assert entry.facts["knowledge"]["measured"] is False
    assert "mount data path and it was not measured" in entry.summary
    assert entry.facts["during"]["rest"]["replicated"]["outcome"] == "served"
    assert entry.facts["restore"]["verified"] is True


def test_o28_a_sandbox_that_does_not_start_leaves_the_mount_unmeasured_and_the_check_unknown(context, platform, agent):
    """O28: a sandbox whose start fails (its images are not on the host) leaves the mount and the tools unmeasured,
    named with the reason, and the check unknown."""
    host = agent_host(platform, agent)
    host.images.clear()
    entry = outage_check(context, agent_host=host)
    assert entry.ok is None
    assert entry.facts["agent"]["sandbox"]["reason"].startswith("the sandbox did not start")
    assert entry.facts["mount"]["measured"] is False


def test_o28_a_platform_without_a_sandbox_table_is_named_as_such_whatever_the_consent(
    tmp_path, fake, relay, tools_manifest, context
):
    """O28: with a host to start containers on (the consent given) and no [sandbox] table, the reason the mount and the
    tools were not measured names the table, never the consent."""
    target = write_target(tmp_path / "bare", fake, relay, tools_manifest, lambda data: data.pop("sandbox"))
    entry = outage_check(context, target=target, agent_host=FakeHost())
    assert entry.ok is None
    assert entry.facts["agent"]["sandbox"]["reason"].startswith("platform.toml has no [sandbox] table")
    assert "EXPCORE_LIVE_DOCKER" not in entry.summary


def test_o28_without_a_declared_mount_the_mount_is_absent_not_unknown(tmp_path, fake, relay, tools_manifest, context):
    """O28: a platform whose data paths have no mount level has no mount to measure: absent, not unknown, and the
    tools are still called from a sandbox with no data path."""

    def no_mount(data):
        data["sandbox"]["data_paths"] = {"isolated": "none"}

    target = write_target(tmp_path / "plain", fake, relay, tools_manifest, no_mount)
    entry = outage_check(context, target=target)
    assert entry.facts["mount"] == {"declared": False}
    assert "mount" not in entry.facts["during"]
    assert entry.facts["agent"]["sandbox"]["data_path"] == "none"
    assert entry.ok is True, entry.summary


# -- several spaces --------------------------------------------------------------------------------


@pytest.fixture
def two_spaces(tmp_path, ticker, monkeypatch, tools_manifest):
    """Two spaces on the three providers, each with alpha as its data provider and beta local, and their world; the
    agent's plane has the tool server and a sandbox with no data path (the fake mounts one space's directory only)."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    federation.add_provider(GAMMA_ID, GAMMA_HOST)
    federation.add_data_space(SPACE_IDS[1], SPACE_NAMES[1], [ALPHA_ID, BETA_ID, GAMMA_ID])
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        for space in SPACE_IDS[:2]:
            federation.grant(user_id, BASELINE, space=space)

    def several(data):
        with_placement(data)
        data["providers"]["gamma"] = {"url": f"https://{GAMMA_HOST}", "id": GAMMA_ID}
        with_spaces(data, 2, providers=["alpha", "beta", "gamma"], local="beta", holder="alpha")
        data["faults"] = faults_table(services={"alpha": SERVICE})
        with_agent(data, relay.url)
        data["sandbox"]["data_paths"] = {"isolated": "none"}

    world = OutageWorld(federation, ticker)
    agent = AgentWorld(world, "beta.test", SPACE_ROOT, space_ids=SPACE_IDS[:2])
    relay = ToolRelay(agent)
    directory = tmp_path / "several"
    directory.mkdir()
    platform = write_platform(directory, several)
    shutil.copy(tools_manifest, directory / KNOWLEDGE_MANIFEST)
    add_slot_users(federation, directory, spaces=SPACE_IDS[:2])
    services = FakeServices(world, {SERVICE: "alpha.test"})
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    ctx = build_context(
        platform, tmp_path / "o", client=client, driver=services, allow_mutation=True, allow_faults=True,
        clock=ticker.clock, sleep=ticker.sleep, outage_bound_s=BOUND_S, now=lambda: NOW,
        agent_host=agent_host(platform, agent), agent_options=agent_options(),
    )  # fmt: skip
    yield ctx, services, world, agent
    relay.close()


def test_o28_in_each_of_several_spaces_the_provider_is_taken_down_and_restored_and_spaces_are_named_by_position(
    two_spaces,
):
    """O28: in each of several spaces the provider is taken down and restored and spaces are named by position; each
    space's tools are asked about that space, the one under test, with everything up and while it is down."""
    ctx, services, world, agent = two_spaces
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is True, entry.summary
    assert list(entry.facts["spaces"]) == ["spaces[0]", "spaces[1]"]
    assert {row["result"] for row in entry.facts["spaces"].values()} == {"pass"}
    assert services.verbs() == ["stop", "start", "stop", "start"]  # restored and verified before the next space
    asked = [arguments["space_id"] for name, arguments in agent.tool_calls if name == "list_space_providers"]
    assert asked == [SPACE_IDS[0]] * 2 + [SPACE_IDS[1]] * 2
    text = json.dumps({"summary": entry.summary, "facts": entry.facts})
    for needle in (*SPACE_NAMES[:2], *SPACE_IDS[:2], *SPACE_LABELS[:2], "alpha.test"):
        assert needle not in text, needle
    assert world.down == set()


def test_o28_a_space_whose_provider_could_not_be_shown_restored_stops_the_spaces_after_it(two_spaces):
    """O28: a space whose provider could not be shown restored stops the spaces after it."""
    ctx, services, _, _ = two_spaces
    services.keeps_identity = True
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is False
    spaces = entry.facts["spaces"]
    assert (spaces["spaces[0]"]["result"], spaces["spaces[1]"]["result"]) == ("fail", "unknown")
    assert "spaces[0] left a provider" in spaces["spaces[1]"]["summary"]
    assert services.verbs() == ["stop", "start"]  # the second space never stopped it again


def test_o28_a_provider_that_never_answers_rest_again_halts_the_spaces_after_it(two_spaces):
    """O28: a provider back with a changed identity that never answers REST again does not serve, so it is not shown
    back: the space fails and the spaces after it are not run."""
    ctx, services, world, _ = two_spaces
    stay_unreachable(services, world)
    entry = run_checks(ctx, ["provider_outage"])[0]
    spaces = entry.facts["spaces"]
    assert (spaces["spaces[0]"]["result"], spaces["spaces[1]"]["result"]) == ("fail", "unknown")
    assert spaces["spaces[0]"]["restore"]["how"] == "not_serving"
    assert "spaces[0] left a provider" in spaces["spaces[1]"]["summary"]
    assert services.verbs() == ["stop", "start"]


def test_o28_a_provider_whose_rest_never_answers_the_checks_own_read_halts_the_spaces_after_it(two_spaces):
    """O28: a provider the driver finds serving that never answers the check's own REST read is not shown back
    either: the space fails and the spaces after it are not run."""
    ctx, services, world, _ = two_spaces
    stay_unreachable(services, world)
    services.serves = lambda service: True  # type: ignore[method-assign]
    entry = run_checks(ctx, ["provider_outage"])[0]
    spaces = entry.facts["spaces"]
    assert (spaces["spaces[0]"]["result"], spaces["spaces[1]"]["result"]) == ("fail", "unknown")
    assert spaces["spaces[0]"]["restore"]["rest_answers_again"] is False
    assert services.verbs() == ["stop", "start"]


def leave_open(ctx, services, *later, alive=False):
    """What a run that died mid-injection left in the journal for the first space's data provider: `injecting`, then
    the events `later` names. `alive` names this process as its holder, whose lock the test then takes, as a live
    injector's. Return the lock, to be closed by the test."""
    journal = InjectionJournal(ctx.fault_settings.journal)
    line = {
        "target": "providers[0]",
        "service": SERVICE,
        "identity_sha256": digest(services.observe(SERVICE).identity),
        "holder": holder() if alive else {"pid": 999999, "boot_id": "x"},
    }
    journal.append({"event": "injecting", "id": "left-open", **line})
    for event in later:
        journal.append({"event": event, "id": "left-open"})
    return journal.try_lock("providers[0]") if alive else None


def test_o28_a_start_that_cannot_settle_an_injection_an_earlier_run_left_open_halts_the_spaces_after_it(two_spaces):
    """O28: the injector settles an injection an earlier run left open before it takes anything down. When that fails
    (its stop was asked for and the service never shows down, so `stop_unsettled`), nothing was stopped by this start
    but a provider may still be down: the space fails, naming the provider by its position and the injector's word, and
    the spaces after it are not run, as for a provider that could not be restored."""
    ctx, services, _, _ = two_spaces
    leave_open(ctx, services, "stopping")
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is False
    spaces = entry.facts["spaces"]
    assert (spaces["spaces[0]"]["result"], spaces["spaces[1]"]["result"]) == ("fail", "unknown")
    restore = spaces["spaces[0]"]["restore"]
    assert (restore["cleared"], restore["verified"], restore["how"]) == (False, False, "earlier_injection_open")
    assert restore["left_open"] == [{"target": "providers[0]", "how": "stop_unsettled"}]
    assert "providers[0]" in spaces["spaces[0]"]["summary"]
    assert "stop_unsettled" in spaces["spaces[0]"]["summary"]
    assert "spaces[0] left a provider" in spaces["spaces[1]"]["summary"]
    assert services.verbs() == []  # the leftover was never restarted, and no space took the provider down


def test_o28_an_injection_a_live_injector_holds_down_is_left_open_by_the_start_and_halts_the_spaces_after_it(
    two_spaces,
):
    """O28: a start refused because an injection is held down by a live injector records no recovery step, so the
    check reads the journal after the failed start: an injection still open there leaves the space failed and the
    spaces after it not run."""
    ctx, services, _, _ = two_spaces
    lock = leave_open(ctx, services, "stopping", "injected", alive=True)
    try:
        entry = run_checks(ctx, ["provider_outage"])[0]
    finally:
        lock.close()
    spaces = entry.facts["spaces"]
    assert (spaces["spaces[0]"]["result"], spaces["spaces[1]"]["result"]) == ("fail", "unknown")
    restore = spaces["spaces[0]"]["restore"]
    assert (restore["cleared"], restore["how"]) == (False, "earlier_injection_open")
    assert restore["left_open"] == [{"target": "providers[0]", "how": "left_open"}]
    assert "providers[0]" in spaces["spaces[0]"]["summary"]
    assert services.verbs() == []


def test_o28_a_journal_that_cannot_be_read_after_a_failed_start_is_not_shown_to_hold_nothing_open(
    two_spaces,
):
    """O28: when the journal cannot be read, the injector's start fails with no recovery step and the check cannot show
    that nothing is open: it fails closed, as for an injection left open, naming no provider."""
    ctx, services, _, _ = two_spaces
    journal = InjectionJournal(ctx.fault_settings.journal)
    journal.append({"event": "unheard-of", "id": "x"})
    entry = run_checks(ctx, ["provider_outage"])[0]
    spaces = entry.facts["spaces"]
    assert (spaces["spaces[0]"]["result"], spaces["spaces[1]"]["result"]) == ("fail", "unknown")
    assert spaces["spaces[0]"]["restore"]["left_open"] == [{"target": None, "how": "journal_unreadable"}]
    assert "the injection journal" in spaces["spaces[0]"]["summary"]
    assert services.verbs() == []


def test_o28_a_start_that_fails_before_anything_is_journalled_or_stopped_leaves_the_spaces_after_it_to_run(two_spaces):
    """O28: a start that fails cleanly (the service cannot be read before the stop, so nothing is journalled or
    stopped and nothing is open) is no provider left down: each space is unknown for what it could not do, with
    `not_injected`, and none halts the other."""
    ctx, services, _, _ = two_spaces
    services.unreadable = True
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is None
    spaces = entry.facts["spaces"]
    assert (spaces["spaces[0]"]["result"], spaces["spaces[1]"]["result"]) == ("unknown", "unknown")
    assert [spaces[name]["restore"]["how"] for name in ("spaces[0]", "spaces[1]")] == ["not_injected"] * 2
    assert all("left a provider" not in row["summary"] for row in spaces.values())
    assert services.verbs() == []
    assert InjectionJournal(ctx.fault_settings.journal).open_entries() == []


def test_o28_an_unknown_run_exits_with_the_unknown_status(platform, world, services, tmp_path, ticker):
    """O28: an unknown run exits with the unknown status."""
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    code, _, _ = run(
        platform, tmp_path / "reports", client=client, driver=services, allow_mutation=True, allow_faults=False,
        clock=ticker.clock, sleep=ticker.sleep, now=lambda: 1_800_000_000.0, tcp=None, names=["provider_outage"],
    )  # fmt: skip
    assert code == EXIT_UNKNOWN
