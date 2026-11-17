"""O68: a fleet gives a campaign that injects faults only to a federation whose fault relay serves, and says each
federation's state plainly (deploy/hpc-federation/fleet.py, fleetplan.py and fleetrecord.py; hpcdriver.py records the
relay).

The relay's state is what `up` wrote in the federation's state file (`fault_relay`), never what its log printed: a
federation without a serving relay can neither inject nor restore, so `plan` and `step` refuse it for a campaign with an
outage condition or a sanctioned `[faults]`, and a campaign without either is unaffected.

Every federation runs over a fake cluster of its own (`hpc_fleet_support.Bench`); `Bench.no_relay` names the ones whose
core has no relay."""

import json

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, data_stand_in
from hpc_fleet_support import fleet_modules, job_ids, make_bench, make_ca
from tls_support import HAS_OPENSSL

from expcore.errors import PROVIDER_OUTAGE

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl")

OUTAGE_STUDY = (
    '[conditions.control]\nmodel = "m"\n\n'
    f'[conditions.provider-down]\nmodel = "m"\ninfrastructure_is_treatment = [{{ kind = "{PROVIDER_OUTAGE}", '
    'target = "providers[1]" }]\n'
)
PLAIN_STUDY = (  # a treatment that is no outage: nothing here injects a fault
    '[conditions.control]\nmodel = "m"\n\n[conditions.mount]\nmodel = "m"\ninfrastructure_is_treatment = ["sandbox"]\n'
)


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


def assigned(bench) -> dict:
    return {s["label"]: s["member"] for s in record(bench)["shards"]}


def plan(bench) -> tuple[int, str, dict]:
    code, printed = bench.run("plan", "--manifest", str(bench.settings()))
    lines = {}
    for text in printed.splitlines():
        if text.startswith("{"):
            entry = json.loads(text)
            lines[entry["shard"]] = entry
    return code, printed, lines


def study(bench, text: str) -> None:
    (bench.base / "study").mkdir(exist_ok=True)
    (bench.base / "study" / "study.toml").write_text(text, encoding="utf-8")


def state_file(bench, label: str):
    return bench.root(label) / "hpc-federation.json"


def edit_state(bench, label: str, **changes) -> None:
    """The state file as an up left it, with `changes` made (a value of None removes the key)."""
    found = json.loads(state_file(bench, label).read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            found.pop(key, None)
        else:
            found[key] = value
    state_file(bench, label).write_text(json.dumps(found, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ------------------------------------------------------------------------------------------------ up records the relay
@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


class Rig:
    """One driver over the fake Slurm, the core's phase stood in, as the updown tests have it."""

    def __init__(self, deploy, tmp_path, site, relay: bool):
        self.deploy, self.site = deploy, site
        self.fake = FakeSlurm()
        self.root = tmp_path / "fed" / "root"
        self.driver = deploy.hpc.cli.make_driver(LAYOUT, str(self.root), runner=self.fake)
        self.driver.sleep = self.driver.slurm.sleep = lambda seconds: None
        self.driver.poll_s = self.driver.slurm.poll_s = 0.0
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: relay

    def up(self) -> str:
        return self.driver.up(self.deploy.hpc.settings.site_from_environment(self.site))

    def state(self) -> dict:
        return self.driver.store.read()


def test_o68_up_records_a_serving_fault_relay_in_the_state_file(deploy, tmp_path, site):
    """O68: once the relay's heartbeat is seen, up records `fault_relay = "serving"` in the state file, which is what
    the fleet reads, and still says it on its last line."""
    rig = Rig(deploy, tmp_path, site, relay=True)
    assert rig.up().endswith("fault relay: serving")
    assert rig.state()["fault_relay"] == "serving"


def test_o68_up_records_no_fault_relay_when_its_core_has_none(deploy, tmp_path, site):
    """O68: a core without the relay module starts none, and the state file says `none` rather than saying nothing."""
    rig = Rig(deploy, tmp_path, site, relay=False)
    assert rig.up().endswith("fault relay: none, this core has no expcore_onedata.fault_relay")
    assert rig.state()["fault_relay"] == "none"
    assert "relay" not in rig.state()  # no client of a relay: down has none to end


def test_o68_a_relay_that_never_beats_is_never_recorded_serving(deploy, tmp_path, site):
    """O68: the relay's client is recorded at its spawn, so that down can end it, but it serves only once its heartbeat
    is seen: a relay that never beats fails up and leaves no `fault_relay` record."""
    rig = Rig(deploy, tmp_path, site, relay=True)
    rig.fake.relay_rc["alive"] = 1
    ticks = iter(range(0, 10**6, 10))
    rig.driver.clock = lambda: next(ticks)
    with pytest.raises(RuntimeError, match="did not beat"):
        rig.up()
    assert rig.state()["relay"]["pid"] == 9999
    assert "fault_relay" not in rig.state()


def test_o68_a_state_whose_fault_relay_is_not_serving_or_none_is_refused(deploy, tmp_path, site):
    """O68: the state file's reader refuses a `fault_relay` that is neither word, rather than reading it as one."""
    rig = Rig(deploy, tmp_path, site, relay=True)
    rig.up()
    rig.driver.set(lambda state: state.update(fault_relay="yes"))
    with pytest.raises(deploy.hpc.state.StateError, match="fault_relay"):
        deploy.hpc.state.load(rig.root)


# ------------------------------------------------------------------------------------------------ plan and step
def test_o68_plan_refuses_a_federation_with_no_relay_for_a_campaign_with_an_outage_condition(bench):
    """O68: fed-2's core has no relay and the study has an outage condition: plan names fed-2 and the reason, gives
    its shard to no one, and gives the other two their own."""
    study(bench, OUTAGE_STUDY)
    bench.no_relay.add("fed-2")
    assert bench.up(3)[0] == 0
    code, printed, lines = plan(bench)
    assert code == 1, printed
    assert "not used: fed-2: its fault relay is not serving" in printed
    assert "none: its core has no relay" in printed
    assert "inject" in printed  # why it matters, in words: a federation without a relay can neither inject nor restore
    assert assigned(bench) == {"shard-1": "fed-1", "shard-2": None, "shard-3": "fed-3"}
    assert set(lines) == {"shard-1", "shard-3"}


@pytest.mark.parametrize("text", [PLAIN_STUDY, None], ids=["no outage condition", "no study file"])
def test_o68_plan_accepts_the_same_federation_for_a_campaign_with_no_outage_condition(bench, text):
    """O68: the same fed-2 with no relay takes its shard when nothing in the campaign injects a fault."""
    if text is not None:
        study(bench, text)
    bench.no_relay.add("fed-2")
    assert bench.up(3)[0] == 0
    code, printed, _ = plan(bench)
    assert code == 0, printed
    assert "fault relay" not in printed
    assert assigned(bench) == {"shard-1": "fed-1", "shard-2": "fed-2", "shard-3": "fed-3"}


def test_o68_plan_names_every_federation_it_refuses_when_none_has_a_relay(bench):
    """O68: with no federation fit for the campaign the first plan refuses, and its message names each member and its
    reason (the lines of a plan that assigns nothing were lost before)."""
    study(bench, OUTAGE_STUDY)
    bench.no_relay.update({"fed-1", "fed-2", "fed-3"})
    assert bench.up(3)[0] == 0
    code, printed, _ = plan(bench)
    assert code == 2, printed
    assert "no federation of the fleet is ready to run a shard" in printed
    for label in ("fed-1", "fed-2", "fed-3"):
        assert f"{label}: its fault relay is not serving" in printed
    assert assigned(bench) == {"shard-1": None, "shard-2": None, "shard-3": None}


def test_o68_the_relay_is_read_from_the_state_file_and_not_from_the_up_log(bench):
    """O68: fed-2's up log says `fault relay: serving`, and its state file says none: it is refused. The log is never
    read for this."""
    study(bench, OUTAGE_STUDY)
    assert bench.up(3)[0] == 0
    assert "fault relay: serving" in (bench.fleet / "logs" / "fed-2.up.log").read_text(encoding="utf-8")
    edit_state(bench, "fed-2", fault_relay="none")
    code, printed, _ = plan(bench)
    assert code == 1, printed
    assert "not used: fed-2: its fault relay is not serving" in printed
    assert assigned(bench)["shard-2"] is None


def test_o68_a_federation_whose_log_says_none_and_whose_state_file_says_serving_is_accepted(bench):
    """O68: the reverse: fed-1's up log says `fault relay: none` and its state file says serving: the file decides."""
    study(bench, OUTAGE_STUDY)
    bench.no_relay.add("fed-1")
    assert bench.up(3)[0] == 0
    assert "fault relay: none" in (bench.fleet / "logs" / "fed-1.up.log").read_text(encoding="utf-8")
    edit_state(bench, "fed-1", fault_relay="serving")
    code, printed, _ = plan(bench)
    assert code == 0, printed
    assert assigned(bench)["shard-1"] == "fed-1"


def test_o68_a_state_file_with_no_relay_record_does_not_count_as_serving(bench):
    """O68: a federation an older core brought up (or one whose up never reached the relay) has no `fault_relay`: it is
    not serving for a campaign that injects faults, and the line says it is not recorded."""
    study(bench, OUTAGE_STUDY)
    assert bench.up(3)[0] == 0
    edit_state(bench, "fed-3", fault_relay=None)
    code, printed, _ = plan(bench)
    assert code == 1, printed
    assert "not used: fed-3: its fault relay is not serving (not recorded" in printed
    assert "fleet: fed-3: up with no relay (its state file has no relay record)" in printed
    assert assigned(bench)["shard-3"] is None


def test_o68_a_sanctioned_faults_table_in_the_platform_file_counts_as_injecting(bench):
    """O68: a study that declares no outage condition but whose federations' platform.toml sanctions injection
    (`[faults] sanctioned = true`) needs the relay all the same."""
    study(bench, PLAIN_STUDY)
    bench.no_relay.add("fed-2")
    assert bench.up(3)[0] == 0
    for label in ("fed-1", "fed-2", "fed-3"):
        platform = bench.root(label) / "platform.toml"
        text = platform.read_text(encoding="utf-8")
        assert "sanctioned = false" in text
        platform.write_text(text.replace("sanctioned = false", "sanctioned = true"), encoding="utf-8")
    code, printed, _ = plan(bench)
    assert code == 1, printed
    assert "not used: fed-2: its fault relay is not serving" in printed
    assert assigned(bench) == {"shard-1": "fed-1", "shard-2": None, "shard-3": "fed-3"}


def test_o68_a_study_file_that_cannot_be_read_counts_as_injecting(bench):
    """O68: an outage condition cannot be ruled out of a study.toml that is not TOML: fail closed for a member with no
    relay, and say why."""
    study(bench, "[conditions\n")
    bench.no_relay.add("fed-2")
    assert bench.up(3)[0] == 0
    code, printed, _ = plan(bench)
    assert code == 1, printed
    assert "not used: fed-2: its fault relay is not serving" in printed
    assert "could not be read" in printed


def test_o68_step_checks_the_relay_again_before_it_launches_a_shard(bench):
    """O68: a shard planned while fed-1's relay served is not launched once its state file says none; the others are."""
    study(bench, OUTAGE_STUDY)
    assert bench.up(3)[0] == 0
    assert plan(bench)[0] == 0
    edit_state(bench, "fed-1", fault_relay="none")
    code, printed = bench.run("step")
    assert code == 1, printed
    assert "shard-1: not launched: fed-1: its fault relay is not serving" in printed
    assert len(bench.spawned) == 2


# ------------------------------------------------------------------------------------------------ member states
def test_o68_status_says_each_federation_plainly_and_a_failed_one_that_still_bills(bench):
    """O68: fed-1 up and ready, fed-2 failed with its two jobs still held (counted, and the command that releases it
    named), fed-3 up with no relay; only the fleet's own labels, never a host."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    bench.no_relay.add("fed-3")
    assert bench.up(3)[0] == 1
    code, printed = bench.run("status")
    assert code == 0
    assert "fed-1: up and ready" in printed
    assert "fed-2: failed, still HOLDING 2 job(s): it bills until `fleet.py down --member fed-2`" in printed
    assert "fed-3: up with no relay" in printed
    assert all(job in printed for job in job_ids("fed-2"))


def test_o68_status_says_a_failed_federation_that_holds_nothing_and_one_taken_down(bench):
    """O68: a member whose up was refused in its preflight holds no job (no state, nothing recorded), and says so, and a
    member taken down says down, never the warning."""
    calls = []

    def operate(label, command, layout, root, log):
        calls.append((label, command))
        if (label, command) == ("fed-2", "up"):
            return 2, "error: up: the root lies inside another federation's root, or holds one; refused\n"
        return bench.federation(label, command, layout, root, log)

    assert bench.up(3, federation=operate)[0] == 1
    printed = bench.run("status")[1]
    assert "fed-2: failed, no job on record" in printed
    assert "HOLDING" not in printed
    assert bench.run("down", "--member", "fed-1", federation=operate)[0] == 0
    printed = bench.run("status")[1]
    assert "fed-1: down (phase None, job(s) " in printed  # the record keeps the ids a down member held, as it did
    assert "HOLDING" not in printed


def test_o68_plan_says_each_federations_state_before_it_assigns_anything(bench):
    """O68: plan prints the states, on stderr beside its other lines (stdout stays one JSON line per shard), so that a
    failed federation that still bills shows at once, and does so when the plan is refused too."""
    study(bench, OUTAGE_STUDY)
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    bench.no_relay.add("fed-3")
    assert bench.up(3)[0] == 1
    code, printed, _ = plan(bench)
    assert "fleet: fed-1: up and ready" in printed
    assert "fleet: fed-2: failed, still HOLDING 2 job(s): it bills until `fleet.py down --member fed-2`" in printed
    assert "fleet: fed-3: up with no relay" in printed
    assert code == 1


def test_o68_a_refused_plan_still_says_each_federations_state(bench):
    """O68: the plan that refuses (no federation fit for the campaign) says the states too: the operator sees what
    still bills before reading why nothing was assigned."""
    study(bench, OUTAGE_STUDY)
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    bench.no_relay.update({"fed-1", "fed-3"})
    assert bench.up(3)[0] == 1
    code, printed, _ = plan(bench)
    assert code == 2, printed
    assert "fleet: fed-2: failed, still HOLDING 2 job(s)" in printed
    assert "fleet: fed-1: up with no relay" in printed
