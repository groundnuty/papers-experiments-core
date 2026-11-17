"""C06: the fleet's `plan` and `step` refuse a core that names no code, as `shards.py plan` and `step` do.

The fleet builds its srun lines with `shards.step_argv` itself and never goes through `shards.py plan`, so it asks the
manifest's interpreter which core it imports (`shards.identify_core`) and refuses what `expcore run` would refuse to
start on (`shards.core_refusal`): at `plan`, before the manifest or the record is written, and at `step`, before any
line is launched. The bench stands in the interpreter's answer, in the form `shards.py` reads it; the probe itself is
tested with real interpreters in `test_hpc_shards_core_identity.py`. Every federation runs over a fake cluster
(`hpc_fleet_support.Bench`), and nothing reaches a cluster."""

import json

import pytest
from hpc_fleet_support import fleet_modules, make_bench, make_ca
from tls_support import HAS_OPENSSL

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl")

REMEDY = (
    "use a git checkout of the core, or a wheel built with its build identity "
    "(hatch build writes expcore/_build_identity.json)"
)
UNKNOWN = {"known": False, "stale": False, "why": "no git checkout of the core and no _build_identity.json beside it"}
STALE = {"known": True, "stale": True, "why": "its Python source differs from the build of 0123456789ab that names it"}
REFUSED = [(UNKNOWN, "unknown"), (STALE, "stale")]


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        yield found


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    return make_ca(tmp_path_factory.mktemp("ca"))


@pytest.fixture
def bench(deploy, ca, tmp_path, monkeypatch):
    return make_bench(deploy, tmp_path, ca, monkeypatch)


def record(bench) -> dict:
    return json.loads((bench.fleet / "fleet.json").read_text(encoding="utf-8"))


def plan(bench) -> tuple[int, str]:
    return bench.run("plan", "--manifest", str(bench.settings()))


def planned(printed: str) -> list[str]:
    """The shards whose line a plan printed."""
    return [json.loads(text)["shard"] for text in printed.splitlines() if text.startswith("{")]


def test_c06_plan_asks_the_manifests_interpreter_which_core_it_imports(bench):
    """C06: the fleet's plan reads the core through the manifest's own `python`, the one every step will run."""
    assert bench.up(2)[0] == 0
    code, printed = plan(bench)
    assert code == 0, printed
    assert [(asked.python, asked.step_path) for asked in bench.asked] == [("/usr/bin/python3", "/usr/bin:/bin")]


@pytest.mark.parametrize(("core", "said"), REFUSED, ids=["unknown", "stale"])
def test_c06_plan_refuses_a_core_that_names_no_code_and_records_nothing(bench, core, said):
    """C06: an unknown or stale core is refused naming the remedy: no line printed, no shards.toml, no shard
    assigned."""
    assert bench.up(2)[0] == 0
    bench.core = core
    code, printed = plan(bench)
    assert code == 2
    assert f"the core is {said}" in printed
    assert REMEDY in printed
    assert planned(printed) == []
    assert not (bench.fleet / "shards.toml").exists()
    assert {shard["member"] for shard in record(bench)["shards"]} == {None}


def test_c06_plan_refuses_an_interpreter_that_cannot_report_its_core(bench, deploy):
    """C06: an interpreter that does not run is not a core that is fine: refused, saying the identity could not be
    read, with nothing recorded."""
    assert bench.up(2)[0] == 0
    bench.core = deploy.shards.ShardsError("the manifest's python could not report the core's identity (exit 1)")
    code, printed = plan(bench)
    assert code == 2
    assert "could not report the core's identity" in printed
    assert planned(printed) == []
    assert not (bench.fleet / "shards.toml").exists()


@pytest.mark.parametrize(("core", "said"), REFUSED, ids=["unknown", "stale"])
def test_c06_step_launches_nothing_on_a_core_that_names_no_code(bench, core, said):
    """C06: the core was fine at the plan and is not at the step (the interpreter's environment changed): refused
    before any line is spawned, and no launch is recorded."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    bench.core = core
    code, printed = bench.run("step")
    assert code == 2
    assert f"the core is {said}" in printed
    assert REMEDY in printed
    assert bench.spawned == []
    assert [shard["launches"] for shard in record(bench)["shards"]] == [[], []]


def test_c06_step_asks_only_when_it_has_a_step_to_launch(bench):
    """C06: a step that launches nothing (every shard's step still runs) reads no core and says what it said before."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    assert bench.run("step")[0] == 0
    asked = len(bench.asked)
    assert asked == 2  # the plan's, and the launching step's
    bench.core = UNKNOWN
    code, printed = bench.run("step")
    assert code == 0, printed
    assert "shard-1: its step on fed-1 still runs" in printed
    assert len(bench.asked) == asked
