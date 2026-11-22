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
    assert proof["negative_problems"] == []
    assert proof["negative"]["next_cell"] == "cell_unkillable"


def test_o97_the_control_reads_the_same_file_three_times_and_is_never_touched(proof):
    assert proof["control_problems"] == []
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
    assert any("the proof stopped: the local provider holds" in p for p in facts["problems"])
    assert facts["teardown_problems"] == []


def test_o97_the_control_condition_never_reads_the_owner_only_file():
    """O97: the campaign's control run reads the local file alone, so it never hands the local provider a copy of the
    file the registered run must find only on its owner."""
    control = FP.script("/data/s/owner.bin", "/data/s/local.bin", "/data/s", second_cycle=True, owner_reads=False)
    assert not any(FP.OWNER_MARK in call[1]["code"] for turn in control if isinstance(turn, list) for call in turn)
