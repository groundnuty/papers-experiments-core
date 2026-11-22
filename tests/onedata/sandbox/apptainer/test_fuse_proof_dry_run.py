"""O97 (the live proof's dry run): the driver of tests/live/fuse_abort_apptainer.py over the fake world, so the proof's
logic is in CI and a live run can differ only in the site.

The whole sequence runs: the seeding, the control sandbox's reads, the unregistered sandbox's read under the driver's
own outage (P12), and the registered attempt through the real campaign (`expcore run`, in this process) of the minimal
study the driver writes, whose executor launches the sandbox and runs the injector inside the attempt. Every sandbox is
the real `ApptainerSandbox` on a fake node whose owner-only read waits in the kernel until the abort frees it
(`fuse_proof_fake`); the hold, the attribution, the abort, the restore, the code tool, the loop, the record and the
campaign are the core's own. The pass list is the live one (`fuse_proof.problems`), with a bound of 1.5 s in place of
30 s and no gap.
"""

import importlib.util
import json
import shutil
from pathlib import Path

import fuse_proof as FP
import fuse_proof_fake
import pytest
from study_support import clean_environment  # noqa: F401 -- the module's fixture: no secret-like variable leaks in

from expcore.cli import main

DRIVER = Path(__file__).resolve().parents[4] / "tests" / "live" / "fuse_abort_apptainer.py"
BOUND_S = 1.5


def load_driver():
    spec = importlib.util.spec_from_file_location("fuse_abort_apptainer", DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def proof(tmp_path_factory, clean_environment):  # noqa: F811 -- the imported fixture, by its name
    base = tmp_path_factory.mktemp("fuse-proof")
    with pytest.MonkeyPatch.context() as patch:
        world = fuse_proof_fake.FakeWorld(base / "world")
        world.install(patch)
        patch.setattr(fuse_proof_fake, "CURRENT", world)
        driver = load_driver()
        facts = driver.run_proof(
            world, base / "out", bound_s=BOUND_S, time_cap_s=120, gap_s=0.0, second_cycle=True,
            run_campaign=lambda study: main(["run", str(study)]), settings_world="fake",
        )  # fmt: skip
    return facts


def test_o97_the_dry_run_passes_every_check_of_the_live_proof(proof):
    """O97: P1 to P12 hold over the fake world: nothing of the pass list is left to the live run but the site."""
    assert proof["problems"] == [], "\n".join(proof["problems"])


def test_o97_the_registered_attempt_is_the_agents_and_its_mount_comes_back_twice(proof):
    first, second, third = proof["registered"]["calls"][:3]
    assert proof["registered"]["outcome"] == "finished"
    assert first["deadline"]["attribution"]["verdict"] == "agent"
    assert (first["deadline"]["outcome"], first["deadline"]["restored_generation"]) == ("mount_restored", 2)
    assert second["status"] == "ok"
    assert (third["deadline"]["outcome"], third["deadline"]["restored_generation"]) == ("mount_restored", 3)


def test_o97_the_unregistered_read_under_the_same_outage_stays_infrastructure(proof):
    assert FP.negative_problems(proof["negative"]) == []
    assert proof["negative"]["next_cell"] == "cell_unkillable"
    assert (proof["negative"]["outcome"], proof["negative"]["blocked_on_expected"]) == ("mount_aborted", True)


def test_o97_the_control_reads_the_same_file_three_times_and_is_never_touched(proof):
    assert FP.control_problems(proof["control"], proof["local_sha256"]) == []
    assert len(proof["control"]["reads"]) == 3


def test_o97_a_check_whose_facts_are_missing_is_a_problem_never_a_pass():
    """O97: an empty set of facts fails every check it reads, with "path not exercised" where nothing blocked."""
    found = FP.problems({})
    assert any(p.startswith(f"{FP.check(1)}: path not exercised") for p in found)
    for name in (FP.check(n) for n in (4, 5, 7, 9, 10, 12)):
        assert any(p.startswith(f"{name}:") for p in found), name


def test_o97_the_target_is_a_space_whose_holder_is_not_its_own_local_provider_in_a_file_of_several_spaces(
    tmp_path, monkeypatch
):
    """O97 (H2 on Helios, 21 Nov): the live world picks its target from each `[[spaces]]` entry's own support set; a
    file of several spaces refuses `config.support`, so the one-space form's accessor is never used for it."""
    from fuse_proof_world import pick_space

    from expcore_onedata.config import load_platform
    from expcore_onedata.testing import ADMIN_ENV, write_platform
    from expcore_onedata.testing.world import SPACE_NAMES, with_placement, with_spaces

    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")
    for name in ("several", "local", "one"):
        (tmp_path / name).mkdir()

    def several(data):
        with_spaces(data, 2)
        data["spaces"][1].update(holder="beta")

    entry, target = pick_space(load_platform(write_platform(tmp_path / "several", several)))
    assert (entry.space.name, target) == (SPACE_NAMES[1], "providers[1]")
    with pytest.raises(SystemExit):  # every space held by its own local provider: nothing to take down
        pick_space(load_platform(write_platform(tmp_path / "local", lambda data: with_spaces(data, 2))))
    one = load_platform(write_platform(tmp_path / "one", lambda data: with_placement(data, holder="beta")))
    assert pick_space(one)[1] == "providers[1]"


def test_o97_the_live_worlds_rest_calls_dial_the_mapped_addresses_of_a_federation_with_an_address_map():
    """O97: an HPC federation's names resolve only through its `[addresses]` map (O73), so the seeding's REST calls go
    through the platform's `MappedTransport`, as its own client's do; a file with no map dials each name itself."""
    from types import SimpleNamespace

    from fuse_proof_world import space_files

    from expcore_onedata.client import MappedTransport

    def config(addresses):
        return SimpleNamespace(
            addresses=addresses,
            tls=SimpleNamespace(client_verify=lambda: True),
            admin=SimpleNamespace(read=lambda: "a"),
        )

    assert isinstance(space_files(config(object()))._http._transport, MappedTransport)
    assert not isinstance(space_files(config(None))._http._transport, MappedTransport)


def test_o97_a_direct_sandbox_takes_the_platforms_own_token_and_spec_with_nothing_held_down():
    """O97: the control and the unregistered sandboxes are the platform's own mount runs in all but the held-down set,
    which is empty: no injection of theirs explains a block, so a survivor of theirs is never the agent's."""
    import dataclasses
    from types import SimpleNamespace

    from fuse_proof_world import direct_spec

    from expcore_onedata.sandbox.api import DataPathMode, SandboxSpec

    asked = {}

    def mint(mode, lease, ceiling_s, condition):
        asked["mint"] = (mode, lease, ceiling_s, condition)
        return SimpleNamespace(data_path="the-token")

    def spec(run_key, level, token, lease):
        asked["spec"] = (run_key, level, token, lease)
        return SandboxSpec(run_key=run_key, mode=DataPathMode.MOUNT, held_down=lambda: frozenset({"providers[1]"}))

    made = direct_spec(SimpleNamespace(_mint=mint, _spec=spec), "fuse-proof-control", "mounted", 60.0)
    assert asked == {
        "mint": (DataPathMode.MOUNT, None, 60.0, None),
        "spec": ("fuse-proof-control", "mounted", "the-token", None),
    }
    assert made.held_down() == frozenset()
    assert dataclasses.replace(made, held_down=None).run_key == "fuse-proof-control"


def test_o97_a_registered_file_the_local_provider_already_holds_stops_the_proof_before_any_outage(
    tmp_path, monkeypatch
):
    """O97: a read of the owner-only file with its owner up gives the local provider a copy, and the registered read is
    then served locally and never blocks: the proof stops before the campaign, and says so, rather than run 40 minutes
    to "path not exercised"."""
    world = fuse_proof_fake.FakeWorld(tmp_path / "world")
    world.install(monkeypatch)
    world.federation.replicated.add("/data/orchard/fuse-proof/owner-registered.bin")
    campaigns = []
    facts = load_driver().run_proof(
        world, tmp_path / "out", bound_s=BOUND_S, time_cap_s=120, gap_s=0.0, second_cycle=True,
        run_campaign=lambda study: campaigns.append(study) or 0, settings_world="fake",
    )  # fmt: skip
    assert campaigns == []
    assert facts["local_held_before"] == len(fuse_proof_fake.OWNER_BYTES)
    assert facts["stopped"].startswith("the local provider holds")
    assert any("did not run whole (stopped)" in p for p in facts["problems"])
    assert any("held 16384 byte(s) of the registered file" in p for p in facts["problems"])
    assert facts["teardown_problems"] == []


def test_o97_the_control_condition_never_reads_the_owner_only_file():
    """O97: the campaign's control run reads the local file alone, so it never hands the local provider a copy of the
    file the registered run must find only on its owner."""
    control = FP.script("/data/s/owner.bin", "/data/s/local.bin", "/data/s", second_cycle=True, owner_reads=False)
    assert not any(FP.OWNER_MARK in call[1]["code"] for turn in control if isinstance(turn, list) for call in turn)


def test_o97_the_live_study_passes_the_campaigns_deferral_checks_at_its_start(tmp_path, monkeypatch):
    """O97 (before H3 on Helios): the minimal study as the driver writes it live, beside a platform file that heals
    every link over several spaces, is held to the platform's own deferral as the scheduler holds it at its start
    (`Scheduler._check_deferral`, with the core's own `platform_deferrals`, the platform's health check and run
    admission): the driver's `max_defer_s` covers what the platform declares, so the campaign is not refused at its
    start after the federation is up."""
    from fault_support import faults_table

    from expcore.campaign.healthgate import deferral_bound
    from expcore.study.config import load_study
    from expcore.study.platform_deferral import platform_deferrals
    from expcore_onedata.admission import run_admission
    from expcore_onedata.client import OnezoneClient
    from expcore_onedata.config import load_platform
    from expcore_onedata.health import PlatformHealth
    from expcore_onedata.testing import ADMIN_ENV, write_platform
    from expcore_onedata.testing.world import with_link_check, with_spaces
    from expcore_onedata.tokens import TokenIssuer

    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")

    def heals_every_link(data):
        with_spaces(data, 2)
        data["spaces"][1].update(holder="beta")
        with_link_check(data)
        data["faults"] = faults_table(  # the journal in full, as `up` renders it: one beside study.toml is refused
            heal="all_links",
            heal_container_fallback=True,
            gate_timeout_s=300,
            journal=str(tmp_path / "fed" / "journal"),
        )
        data["tokens"] = {**data.get("tokens", {}), "margin_s": 20000}

    (tmp_path / "fed").mkdir()
    platform = write_platform(tmp_path / "fed", heals_every_link)
    driver = load_driver()
    settings = {"support": str(DRIVER.parents[1] / "support"), "world": "live", "platform": str(platform)}
    study_dir = driver.write_study(
        tmp_path / "out", settings, target="providers[1]", bound_s=30, time_cap_s=900, platform=platform
    )
    # the fixture's platform file names its credentials and manifests relatively; a rendered one names them absolutely
    shutil.copytree(tmp_path / "fed", study_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns("platform.toml"))
    study = load_study(study_dir)
    config = load_platform(study_dir / "platform.toml")
    client = OnezoneClient(config)
    health = PlatformHealth(config, client, TokenIssuer(client, config))
    admission = run_admission(config, study.schema)
    scheduler = study.campaign.scheduler
    declared, reported = platform_deferrals(study), deferral_bound(health)
    assert declared  # the platform declares its deferral from its own file
    assert reported is not None  # and so does its health check
    assert scheduler.max_defer_s == driver.max_defer_s_for(study_dir / "platform.toml")
    for found in declared:  # the scheduler's own checks, in its order; each raises on a refusal
        scheduler.check_declared_deferral(found.bound, reported, exposed=True, source="the platform")
    scheduler.check_declared_deferral(admission.declared_deferral_s(), reported, exposed=True)
    scheduler.check_deferral_bound(reported)
    client.close()


def test_o97_the_live_world_makes_the_sandboxes_work_root_as_the_worker_does(tmp_path):
    """O97 (H3 try 1 on Helios): a sandbox refuses a start without its work root, and the driver starts its control and
    negative sandboxes itself, so it makes the root first, 0700, as the HPC worker does; an existing root is kept."""
    import stat

    from fuse_proof_world import make_work_root

    root = tmp_path / "agent"
    make_work_root(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    make_work_root(root)  # already there: kept, never an error
    assert root.is_dir()


# ------------------------------------------------------------------------------------------------ the Codex round
def test_o97_the_facts_hold_no_space_name_no_path_of_the_mount_and_no_text_of_the_record(proof):
    """O97 (Codex): the facts hold labels, counts, numbers and booleans; no space name, no mount path, no tool text."""
    written = json.dumps(proof)
    assert fuse_proof_fake.SPACE not in written
    assert "/data/" not in written
    assert "text_tail" not in written
    assert "deadline" not in json.dumps(proof["negative"])


def test_o97_the_pass_list_computes_every_verdict_from_the_facts(proof):
    """O97 (Codex): a passing set of facts fails once its control or negative summary is gone, its control digests are
    not the local file's, its campaign failed, or it was stopped or cut: no verdict is taken from a cached list."""
    import copy

    assert FP.problems(proof) == []

    def without(change):
        facts = copy.deepcopy(proof)
        change(facts)
        return FP.problems(facts)

    assert any(p.startswith("P7:") for p in without(lambda f: f.pop("control")))
    assert any(p.startswith("P12:") for p in without(lambda f: f.pop("negative")))
    assert any(p.startswith("P7:") for p in without(lambda f: f["control"].update(reads=["0" * 64] * 3)))
    assert any(p.startswith("P4:") for p in without(lambda f: f.update(campaign_exit=1)))
    assert any(p.startswith("P1:") for p in without(lambda f: f.update(stopped="a stop")))
    assert any(p.startswith("P1:") for p in without(lambda f: f.update(cut="KeyboardInterrupt")))
    assert any(p.startswith("P1:") for p in without(lambda f: f.update(seeded=False)))
    assert any(p.startswith("P12:") for p in without(lambda f: f["negative"].update(outcome="sandbox_stopped")))


def test_o97_a_survivors_file_and_another_survivors_call_are_never_joined():
    """O97 (Codex): the registered file named by one survivor and a task in D of another prove nothing together."""
    deadline = {
        "outcome": "mount_restored",
        "steps": [{"step": "survived", "left": 2, "states": ["D", "S"]}],
        "survivors": [
            {"comm": "a", "state": "S", "tasks": [{"state": "S"}], "waits_on": [{"mount": "data", "path": "f/x.bin"}]},
            {"comm": "b", "state": "D", "tasks": [{"state": "D", "wchan": "w", "syscall": {"nr": 0, "fd": 3}}]},
        ],
    }
    assert FP.deadline_facts(deadline, "f/x.bin")["blocked_on_expected"] is False
    deadline["survivors"][1]["waits_on"] = [{"mount": "data", "path": "f/x.bin"}]
    assert FP.deadline_facts(deadline, "f/x.bin")["blocked_on_expected"] is True


def test_o97_a_seeding_that_fails_part_way_leaves_nothing_behind(tmp_path, monkeypatch):
    """O97 (Codex): what the seeding made before it failed is removed by the teardown, which the seeding is inside."""
    world = fuse_proof_fake.FakeWorld(tmp_path / "world")
    world.install(monkeypatch)
    made = fuse_proof_fake.FakeFiles()
    calls = {"writes": 0}

    def failing_write(provider, file_id, offset, data):
        calls["writes"] += 1
        if calls["writes"] == 2:
            raise RuntimeError("a write answered 500")

    monkeypatch.setattr(made, "write", failing_write)
    monkeypatch.setattr(world, "files", lambda: made)
    with pytest.raises(RuntimeError):
        load_driver().run_proof(
            world, tmp_path / "out", bound_s=BOUND_S, time_cap_s=120, gap_s=0.0, second_cycle=True,
            run_campaign=lambda study: 0, settings_world="fake",
        )  # fmt: skip
    assert made.created == []
    facts = json.loads((tmp_path / "out" / "facts.json").read_text())
    assert facts["cut"] == "RuntimeError"
    assert facts["problems"]


def test_o97_one_teardown_step_that_fails_never_skips_the_others(tmp_path):
    """O97 (Codex): a mount count that raises still lets the seeded files be removed and the injector be cleared."""
    cleared, made = [], fuse_proof_fake.FakeFiles()
    made.created.append("a-file")

    class World:
        def mounts_left(self):
            raise OSError("the mount table could not be read")

    class Injector:
        def clear(self):
            cleared.append(True)
            return False

    found = load_driver().teardown(World(), made, {"control": None}, injector=Injector())
    assert cleared == [True]
    assert made.created == []
    assert "the negative's provider restore is not verified" in found
    assert any("could not be counted" in p for p in found)


def test_o97_the_target_is_never_a_space_the_data_path_token_does_not_reach(tmp_path, monkeypatch):
    """O97 (Codex): a space whose token planes leave out the data path is never mounted, so it is never the target."""
    from fuse_proof_world import pick_space

    from expcore_onedata.config import load_platform
    from expcore_onedata.testing import ADMIN_ENV, write_platform
    from expcore_onedata.testing.world import SPACE_NAMES, with_spaces

    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")

    def two_remote(data):
        with_spaces(data, 3)
        data["spaces"][1].update(holder="beta", tokens=["tool_server"])  # the first remote one: no data path
        data["spaces"][2].update(holder="beta")

    entry, target = pick_space(load_platform(write_platform(tmp_path, two_remote)))
    assert (entry.space.name, target) == (SPACE_NAMES[2], "providers[1]")
