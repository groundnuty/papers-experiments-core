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
