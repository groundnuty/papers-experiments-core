"""O27: the day-0 outage check takes a declared provider down, records what each way of reaching the data and the
knowledge plane answers, restores the provider and shows it restored, and fails if it cannot (issue #108).

The fakes model no provider that is down, so `outage_support.OutageWorld` puts one in front of them; what it does
while a provider is down is a stand-in's choice (a read with every holder down waits out its timeout), and these tests
assert what the check recorded of it, never that Onedata behaves so."""

import json

import pytest
from fake_reset import FakeReset
from outage_support import GAMMA_HOST, GAMMA_ID, SERVICE, FakeMount, FakeServices, OutageWorld

from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.day0 import outage
from expcore_onedata.day0.outage_cells import Cells
from expcore_onedata.day0.runner import EXIT_FAIL, EXIT_UNKNOWN, build_context, run, run_checks
from expcore_onedata.testing import (
    ADMIN_ENV,
    ALPHA_ID,
    BASELINE,
    BETA_ID,
    SPACE_NAME,
    USERS,
    with_placement,
    write_platform,
)
from expcore_onedata.testing.world import SPACE_IDS, SPACE_LABELS, SPACE_NAMES, with_spaces

BOUND_S = 20.0


def edit(data):
    with_placement(data)
    data["providers"]["gamma"] = {"url": f"https://{GAMMA_HOST}", "id": GAMMA_ID}
    data["support"] = {"providers": ["alpha", "beta", "gamma"], "local": "beta"}
    data["faults"] = {
        "sanctioned": True,
        "compose_file": "compose.yaml",
        "settle_timeout_s": 30,
        "poll_s": 1,
        "services": {"alpha": SERVICE},
    }


@pytest.fixture
def world(fake, ticker) -> OutageWorld:
    fake.add_provider(GAMMA_ID, GAMMA_HOST)
    fake.supports[BETA_ID] = {"readonly": False}
    return OutageWorld(fake, ticker)


@pytest.fixture
def services(world) -> FakeServices:
    def onezone_notices(service: str) -> None:
        if world.onezone_notices:
            world.fake.providers[ALPHA_ID]["online"] = False

    def onezone_forgets(service: str) -> None:
        world.fake.providers[ALPHA_ID]["online"] = True

    driver = FakeServices(world, {SERVICE: "alpha.test"}, on_stop=onezone_notices)
    start = driver.start

    def starting(service: str) -> None:
        start(service)
        if not driver.stays_down:
            onezone_forgets(service)

    driver.start = starting  # type: ignore[method-assign]
    return driver


@pytest.fixture
def platform(tmp_path):
    directory = tmp_path / "target"
    directory.mkdir()
    return write_platform(directory, edit)


@pytest.fixture
def context(platform, world, services, ticker, tmp_path):
    def make(**options):
        client = OnezoneClient(load_platform(platform), http=world.client_http())
        settings = {
            "client": client,
            "driver": services,
            "allow_mutation": True,
            "allow_faults": True,
            "outage_bound_s": BOUND_S,
            "clock": ticker.clock,
            "sleep": ticker.sleep,
            "now": lambda: 1_800_000_000.0,
            "tcp": None,
        }
        return build_context(platform, tmp_path / "out", **{**settings, **options})

    return make


def outage_check(context, **options):
    return run_checks(context(**options), ["provider_outage"])[0]


# -- the outage is observed --------------------------------------------------------------------------------------------


def test_o27_the_provider_is_taken_down_seen_down_and_restored_and_nothing_is_left_down(context, world, services):
    """O27: the provider is taken down seen down and restored and nothing is left down."""
    entry = outage_check(context)
    assert entry.ok is True, entry.summary
    assert services.verbs() == ["stop", "start"]
    assert world.down == set()
    assert entry.facts["outage"]["started"] is True
    assert entry.facts["outage"]["seen_down"] is True
    assert entry.facts["outage"]["rest_answers_while_down"] is False
    assert entry.facts["outage"]["rest"]["transport"] is True
    assert entry.facts["restore"]["verified"] is True
    assert entry.facts["restore"]["how"] == "restored"
    assert entry.facts["restore"]["rest_answers_again"] is True
    assert entry.facts["cleanup_verified"] is True


def test_o27_a_provider_that_still_answers_while_its_service_is_stopped_is_no_observed_outage(context, services):
    """O27: a provider that still answers while its service is stopped is no observed outage."""
    services.stop_has_no_effect = True  # the stop command returns, nothing goes down
    entry = outage_check(context)
    assert entry.ok is None  # never seen down: nothing to measure, and nothing to restore
    assert "could not be taken down" in entry.summary
    assert entry.facts["outage"]["started"] is False
    assert "during" not in entry.facts
    assert entry.facts["restore"]["how"] == "never_seen_down"


def test_o27_a_service_seen_down_whose_rest_still_answers_fails_the_check(context, world, services):
    """O27: a service seen down whose rest still answers fails the check."""
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


# -- the answers are recorded ------------------------------------------------------------------------------------------


def test_o27_the_answers_of_each_path_are_recorded_with_status_latency_and_outcome(context):
    """O27: the answers of each path are recorded with status latency and outcome."""
    entry = outage_check(context)
    assert entry.ok is True, entry.summary
    rest = entry.facts["during"]["rest"]
    assert rest["replicated"]["outcome"] == "served"
    assert rest["replicated"]["hash_matches"] is True
    assert rest["replicated"]["bytes"] == rest["replicated"]["expected_bytes"] == outage.FILE_BYTES
    assert rest["owner_only"]["outcome"] == "blocked"  # no provider that is up holds the bytes: it waited its bound
    assert rest["owner_only"]["seconds"] == BOUND_S
    assert rest["owner_only"]["bound_s"] == BOUND_S
    assert rest["owner_only"]["transport"] is True
    assert rest["owner_only"]["bytes"] == 0
    assert rest["listing"]["outcome"] == "served"
    assert rest["listing"]["entries"] == rest["listing"]["expected_entries"]
    assert entry.facts["during"]["knowledge"]["get_file_distribution"]["rest/replicated"]["outcome"] == "served"


def test_o27_a_read_the_platform_refuses_records_its_status_and_error_id(context, world):
    """O27: a read the platform refuses records its status and error id."""
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


def test_o27_the_knowledge_plane_is_recorded_per_tool_and_the_blocked_transfer_is_told_from_the_finished_one(context):
    """O27: the knowledge plane is recorded per tool and the blocked transfer is told from the finished one."""
    entry = outage_check(context)
    knowledge = entry.facts["during"]["knowledge"]
    labels = entry.facts["roles"]
    rows = knowledge["get_file_distribution"]["knowledge/owner_only"]["providers"]
    assert rows[labels["target"]] == {"listed": True, "held_bytes": outage.FILE_BYTES}
    assert rows[labels["mirror"]]["held_bytes"] == 0
    providers = knowledge["list_space_providers"]
    assert providers["first"]["online"][labels["target"]] is False
    assert providers["until_target_offline"]["target_offline_after_s"] == 0.0
    scheduled = knowledge["schedule_file_replication"]
    assert scheduled["replicated"]["schedule_file_replication"]["outcome"] == "served"
    assert scheduled["replicated"]["get_transfer"]["ended"] is True
    assert scheduled["replicated"]["get_transfer"]["states"][-1][1] == "completed"
    assert scheduled["replicated"]["local_held_bytes"] == outage.FILE_BYTES
    assert scheduled["owner_only"]["get_transfer"]["ended"] is False
    assert scheduled["owner_only"]["get_transfer"]["outcome"] == "blocked"
    assert scheduled["owner_only"]["local_held_bytes"] == 0


def test_o27_the_wait_for_onezone_to_call_the_provider_offline_is_bounded_and_unknown_is_not_a_time(context, world):
    """O27: the wait for onezone to call the provider offline is bounded and unknown is not a time."""
    world.onezone_notices = False
    entry = outage_check(context)
    later = entry.facts["during"]["knowledge"]["list_space_providers"]["until_target_offline"]
    assert later["target_offline_after_s"] is None
    assert later["seconds"] >= BOUND_S
    assert entry.ok is True


def test_o27_a_listing_that_lost_entries_while_the_provider_was_down_is_partial(context, world):
    """O27: a listing that lost entries while the provider was down is partial."""
    world.listing_loses = 2
    entry = outage_check(context)
    listing = entry.facts["during"]["rest"]["listing"]
    assert (listing["outcome"], listing["missing"], listing["unexpected"]) == ("partial", 2, 0)
    assert entry.facts["baseline"]["rest"]["listing"]["outcome"] == "served"  # with everything up it was whole
    assert entry.facts["after"]["listing"]["outcome"] == "served"
    assert entry.ok is True


def test_o27_a_listing_whose_sizes_changed_while_the_provider_was_down_differs(context, world):
    """O27: a listing whose sizes changed while the provider was down differs."""
    world.listing_resizes = True
    entry = outage_check(context)
    listing = entry.facts["during"]["rest"]["listing"]
    assert (listing["outcome"], listing["size_differs"], listing["missing"]) == ("differs", listing["entries"], 0)


def test_o27_a_failed_baseline_means_no_attributable_difference_so_nothing_is_stopped(context, world, services):
    """O27: a failed baseline means no attributable difference so nothing is stopped."""
    world.fake.statuses.append(["/content", 10**6, 500])  # every read of content fails, with everything up
    entry = outage_check(context)
    assert entry.ok is None
    assert "nothing was taken down" in entry.summary
    assert services.verbs() == []
    assert entry.facts["cleanup_verified"] is True


# -- the restore is verified, and a failed restore fails the check ---------------------------------


def test_o27_a_restore_that_leaves_the_identity_unchanged_fails_the_check(context, services, world):
    """O27: a restore that leaves the identity unchanged fails the check."""
    services.keeps_identity = True
    entry = outage_check(context)
    assert entry.ok is False
    assert "could not be restored" in entry.summary
    assert entry.facts["restore"] == {
        **entry.facts["restore"],
        "verified": False,
        "how": "identity_unchanged",
        "identity_changed": False,
    }
    assert world.down == set()  # it answers again, and that is not a restore


def test_o27_a_provider_that_does_not_run_again_fails_the_check_after_trying_to_start_it_again(
    context, services, world
):
    """O27: a provider that does not run again fails the check after trying to start it again."""
    services.stays_down = True
    entry = outage_check(context)
    assert entry.ok is False
    assert entry.facts["restore"]["verified"] is False
    assert entry.facts["restore"]["how"] == "not_running_again"
    assert entry.facts["restore"]["attempts"] == 3
    assert services.verbs() == ["stop", "start", "start", "start"]
    assert "cleanup_verified" in entry.facts  # the teardown ran, and could not reach the provider it deletes through


def test_o27_a_provider_that_runs_but_does_not_answer_rest_after_the_restore_fails_the_check(context, world, services):
    """O27: a provider that runs but does not answer rest after the restore fails the check."""
    real_start = services.start

    def start_but_stay_unreachable(service: str) -> None:
        real_start(service)
        world.down.add("alpha.test")

    services.start = start_but_stay_unreachable  # type: ignore[method-assign]
    entry = outage_check(context)
    assert entry.ok is False
    assert entry.facts["restore"]["verified"] is True  # the identity changed
    assert entry.facts["restore"]["rest_answers_again"] is False
    assert "did not answer REST" in entry.summary


def test_o27_the_restore_runs_when_a_measurement_raises(context, services, world, monkeypatch):
    """O27: the restore runs when a measurement raises."""
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


def test_o27_a_service_that_cannot_be_stopped_is_unknown_and_is_found_as_it_was(context, services, world):
    """O27: a service that cannot be stopped is unknown and is found as it was."""
    services.fail_stop = True
    entry = outage_check(context)
    assert entry.ok is None
    assert "could not be taken down" in entry.summary
    assert entry.facts["restore"]["verified"] is True
    assert entry.facts["restore"]["how"] == "never_seen_down"
    assert world.down == set()


def test_o27_a_service_that_cannot_be_read_before_the_stop_is_not_stopped(context, services):
    """O27: a service that cannot be read before the stop is not stopped."""
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
def test_o27_without_both_consents_nothing_is_stopped_or_sent(context, services, world, options, needs):
    """O27: without both consents nothing is stopped or sent."""
    entry = outage_check(context, **options)
    assert entry.ok is None
    assert needs in entry.summary
    assert services.calls == []
    assert [c for c in world.fake.provider_calls if c[1] in ("POST", "PUT", "DELETE")] == []


def test_o27_without_the_sanction_in_platform_toml_nothing_is_stopped(tmp_path, world, services, ticker):
    """O27: without the sanction in platform toml nothing is stopped."""
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


def test_o27_a_platform_without_a_faults_table_gives_no_driver_and_stops_nothing(tmp_path, world, ticker):
    """O27: a platform without a faults table gives no driver and stops nothing."""
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


def test_o27_a_space_whose_data_provider_is_the_agents_local_provider_has_no_outage_to_read_through(
    tmp_path, world, services, ticker
):
    """O27: a space whose data provider is the agents local provider has no outage to read through."""
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


def test_o27_a_support_set_without_a_third_provider_has_no_replica_to_hold(tmp_path, world, services, ticker):
    """O27: a support set without a third provider has no replica to hold."""
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


def test_o27_the_scenario_is_proved_by_the_local_providers_own_block_map_before_anything_is_stopped(context, world):
    """O27: the scenario is proved by the local providers own block map before anything is stopped."""
    entry = outage_check(context)
    held = entry.facts["held_before"]
    labels = entry.facts["roles"]
    size = outage.FILE_BYTES
    assert held["rest/owner_only"] == {labels["target"]: size, labels["mirror"]: 0, labels["local"]: 0}
    assert held["rest/replicated"] == {labels["target"]: size, labels["mirror"]: size, labels["local"]: 0}
    assert set(held) == {f"{pair}/{kind}" for pair in outage.BASE_PAIRS for kind in outage.KINDS}


def test_o27_a_file_that_the_local_provider_already_holds_is_not_one_held_only_by_the_target(
    context, world, services, monkeypatch
):
    """O27: a file that the local provider already holds is not one held only by the target."""
    real = outage.Run.prepare

    def local_holds_one(self):
        real_blocks = self.provisioner.blocks

        def blocks(file_id, provider, *, reader=None):
            found = real_blocks(file_id, provider, reader=reader)
            if provider == self.roles.local and file_id == self.files[("rest", "owner_only")].file_id:
                return [(0, outage.FILE_BYTES)]
            return found

        self.provisioner.blocks = blocks  # the local provider is shown holding a file it must not
        real(self)

    monkeypatch.setattr(outage.Run, "prepare", local_holds_one)
    entry = outage_check(context)
    assert entry.ok is None
    assert "not held as the scenario needs" in entry.summary
    assert services.verbs() == []


# -- names -----------------------------------------------------------------------------------------


def test_o27_providers_and_spaces_are_named_by_position_and_nothing_identifying_is_recorded(
    context, platform, world, services, tmp_path, ticker
):
    """O27: providers and spaces are named by position and nothing identifying is recorded."""
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
    ):
        assert needle not in text, needle
    assert entry.facts["roles"] == {"target": "providers[0]", "mirror": "providers[2]", "local": "providers[1]"}
    assert "providers[0]" in entry.summary
    assert len(entry.facts["restore"]["identity_after_sha256"]) == 64
    assert len(entry.facts["outage"]["identity_before_sha256"]) == 64


def test_o27_the_report_of_a_whole_run_holds_the_check_and_none_of_the_names(
    platform, world, services, tmp_path, ticker
):
    """O27: the report of a whole run holds the check and none of the names."""
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    code, json_path, md_path = run(
        platform, tmp_path / "reports", client=client, driver=services, allow_mutation=True, allow_faults=True,
        clock=ticker.clock, sleep=ticker.sleep, now=lambda: 1_800_000_000.0, tcp=lambda *a: 1.0,
        outage_bound_s=BOUND_S, names=["provider_outage"],
    )  # fmt: skip
    text = json_path.read_text(encoding="utf-8") + md_path.read_text(encoding="utf-8")
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert [(c["name"], c["status"], c["mutates"]) for c in report["checks"]] == [("provider_outage", "pass", True)]
    assert code == 0
    for needle in ("alpha.test", "beta.test", GAMMA_HOST, SPACE_NAME, SERVICE):
        assert needle not in text, needle


def test_o27_a_failed_restore_gives_the_failing_exit_status(platform, world, services, tmp_path, ticker):
    """O27: a failed restore gives the failing exit status."""
    services.keeps_identity = True
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    code, _, _ = run(
        platform, tmp_path / "reports", client=client, driver=services, allow_mutation=True, allow_faults=True,
        clock=ticker.clock, sleep=ticker.sleep, now=lambda: 1_800_000_000.0, tcp=None,
        outage_bound_s=BOUND_S, names=["provider_outage"],
    )  # fmt: skip
    assert code == EXIT_FAIL


# -- the mount -------------------------------------------------------------------------------------


def test_o27_a_mount_the_platform_declares_but_the_run_lacks_is_unknown_not_skipped(context, monkeypatch):
    """O27: a mount the platform declares but the run lacks is unknown not skipped."""
    monkeypatch.setattr(outage, "mount_declared", lambda ctx: True)
    entry = outage_check(context)
    assert entry.ok is None
    assert entry.facts["mount"] == {
        "declared": True,
        "measured": False,
        "reason": "this run has no reader of the mount",
    }
    assert entry.facts["restore"]["verified"] is True  # the rest of the scenario ran and the provider is back
    assert entry.facts["during"]["rest"]["replicated"]["outcome"] == "served"


def test_o27_without_a_declared_mount_the_mount_is_absent_not_unknown(context):
    """O27: without a declared mount the mount is absent not unknown."""
    entry = outage_check(context)
    assert entry.facts["mount"] == {"declared": False}
    assert "mount" not in entry.facts["during"]
    assert entry.ok is True


def test_o27_the_mount_is_read_when_the_run_has_one_and_its_answers_are_recorded(context, world, monkeypatch):
    """O27: the mount is read when the run has one and its answers are recorded."""
    monkeypatch.setattr(outage, "mount_declared", lambda ctx: True)
    mount = FakeMount(world, BETA_ID)
    entry = outage_check(context, mount=mount)
    assert entry.ok is True, entry.summary
    during = entry.facts["during"]["mount"]
    assert during["replicated"]["outcome"] == "served"
    assert during["owner_only"]["outcome"] == "blocked"  # the mount waited as long as it was allowed to
    assert during["owner_only"]["seconds"] == BOUND_S
    assert during["listing"]["outcome"] == "served"
    assert entry.facts["baseline"]["mount"]["local_owner_only"]["outcome"] == "served"
    assert entry.facts["after"]["mount_owner_only"]["outcome"] == "served"


def test_o27_a_mount_that_answers_with_an_errno_is_recorded_with_it(context, world, monkeypatch):
    """O27: a mount that answers with an errno is recorded with it."""
    monkeypatch.setattr(outage, "mount_declared", lambda ctx: True)
    mount = FakeMount(world, BETA_ID)
    entry = outage_check(context, mount=mount)  # first, a run on which it works, so the baseline can hold
    assert entry.ok is True
    mount.error = OSError(5, "input/output error")
    again = outage_check(context, mount=mount)  # now every read fails, the baseline included
    assert again.ok is None  # a baseline that does not read back stops the scenario before anything is stopped
    assert "baseline" in again.summary


# -- several spaces --------------------------------------------------------------------------------


@pytest.fixture
def two_spaces(tmp_path, ticker, monkeypatch):
    """Two spaces on the three providers, each with alpha as its data provider and beta local, and their world."""
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
        data["faults"] = {"sanctioned": True, "compose_file": "compose.yaml", "services": {"alpha": SERVICE}}

    directory = tmp_path / "several"
    directory.mkdir()
    platform = write_platform(directory, several)
    world = OutageWorld(federation, ticker)
    services = FakeServices(world, {SERVICE: "alpha.test"})
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    ctx = build_context(
        platform, tmp_path / "o", client=client, driver=services, allow_mutation=True, allow_faults=True,
        clock=ticker.clock, sleep=ticker.sleep, outage_bound_s=BOUND_S,
    )  # fmt: skip
    return ctx, services, world


def test_o27_in_each_of_several_spaces_the_provider_is_taken_down_and_restored_and_spaces_are_named_by_position(
    two_spaces,
):
    """O27: in each of several spaces the provider is taken down and restored and spaces are named by position."""
    ctx, services, world = two_spaces
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is True, entry.summary
    assert list(entry.facts["spaces"]) == ["spaces[0]", "spaces[1]"]
    assert {row["result"] for row in entry.facts["spaces"].values()} == {"pass"}
    assert services.verbs() == ["stop", "start", "stop", "start"]  # restored and verified before the next space
    text = json.dumps({"summary": entry.summary, "facts": entry.facts})
    for needle in (*SPACE_NAMES[:2], *SPACE_IDS[:2], *SPACE_LABELS[:2], "alpha.test"):
        assert needle not in text, needle
    assert world.down == set()


def test_o27_a_space_whose_provider_could_not_be_shown_restored_stops_the_spaces_after_it(two_spaces):
    """O27: a space whose provider could not be shown restored stops the spaces after it."""
    ctx, services, _ = two_spaces
    services.keeps_identity = True
    entry = run_checks(ctx, ["provider_outage"])[0]
    assert entry.ok is False
    spaces = entry.facts["spaces"]
    assert (spaces["spaces[0]"]["result"], spaces["spaces[1]"]["result"]) == ("fail", "unknown")
    assert "spaces[0] left a provider" in spaces["spaces[1]"]["summary"]
    assert services.verbs() == ["stop", "start"]  # the second space never stopped it again


def test_o27_an_unknown_run_exits_with_the_unknown_status(platform, world, services, tmp_path, ticker):
    """O27: an unknown run exits with the unknown status."""
    client = OnezoneClient(load_platform(platform), http=world.client_http())
    code, _, _ = run(
        platform, tmp_path / "reports", client=client, driver=services, allow_mutation=True, allow_faults=False,
        clock=ticker.clock, sleep=ticker.sleep, now=lambda: 1_800_000_000.0, tcp=None, names=["provider_outage"],
    )  # fmt: skip
    assert code == EXIT_UNKNOWN
