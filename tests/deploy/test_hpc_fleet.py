"""O68: the fleet launcher (deploy/hpc-federation/fleet.py) brings N federations up from one layout, each its own root,
state file and held job, refuses past the operator's maximum and the inode guard before anything is made, keeps the
others up when one fails, and takes down only what its own up made, each verified.

Every federation runs over a fake cluster of its own (`hpc_fleet_support.Bench`); nothing reaches a cluster."""

import dataclasses
import json
import os
import shutil
import sys
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, data_stand_in
from hpc_fleet_support import SEPARATE_LAYOUT, fleet_modules, job_ids, make_bench, make_ca
from hpc_ingested_support import artifacts, manifest_of, study_layout
from tls_support import HAS_OPENSSL

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl")


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


def state(bench, label: str) -> dict:
    return json.loads((bench.root(label) / "hpc-federation.json").read_text(encoding="utf-8"))


def submitted(bench) -> dict:
    return {label: list(fake.submitted) for label, fake in bench.fakes.items()}


# ------------------------------------------------------------------------------------------------ up
def test_o68_up_brings_n_federations_up_each_its_own_root_state_file_and_held_job(bench):
    """O68: three federations from one layout: each root `<fleet>/fed-k/root`, its state linked, one held job of its
    own (its own fake cluster's), a job name with up's random part, all recorded by the fleet."""
    code, printed = bench.up(3)
    assert code == 0, printed
    found = record(bench)
    assert [(m["label"], m["status"]) for m in found["members"]] == [("fed-1", "up"), ("fed-2", "up"), ("fed-3", "up")]
    names = set()
    for member in found["members"]:
        label = member["label"]
        assert member["root"] == str(bench.root(label))
        assert state(bench, label)["phase"] == "linked"
        assert (bench.root(label) / "platform.toml").is_file()
        job, agent = job_ids(label)
        assert list(bench.fake(label).submitted) == [job, agent]  # the services' job, the agent's own
        assert member["jobs"] == {"allocation": job, "sandbox": agent}
        assert state(bench, label)["sandbox"]["name"] == f"{member['name']}-sandbox"
        assert member["name"] == state(bench, label)["allocation"]["name"]
        names.add(member["name"])
    assert len(names) == 3
    assert [s["label"] for s in found["shards"]] == ["shard-1", "shard-2", "shard-3"]
    assert all(s["member"] is None for s in found["shards"])


def test_o68_up_refuses_a_layout_that_holds_the_agents_node_in_a_heterogeneous_job(bench):
    """O68: a fleet's federations hold each agent's node as a job of its own: steps on a heterogeneous job's services
    failed live on the site's Slurm, so the committed `het` layout is refused before anything is made."""
    code, printed = bench.run("--layout", str(LAYOUT), "up", "2")
    assert code == 2
    assert 'sandbox_placement = "separate"' in printed
    assert not bench.fleet.exists()
    assert bench.fakes == {}


@pytest.mark.parametrize(("maximum", "count"), [("2", 3), ("5", 6)])
def test_o68_up_past_the_maximum_is_refused_before_anything_is_made(bench, maximum, count):
    """O68: N past the operator's maximum: refused, no fleet directory, no sbatch on any cluster."""
    code, printed = bench.up(count, environ={**bench.environ, "HPC_FLEET_MAX": maximum})
    assert code == 2
    assert f"past the maximum of {maximum} (HPC_FLEET_MAX)" in printed
    assert not bench.fleet.exists()
    assert bench.fakes == {}


@pytest.mark.parametrize("value", [None, "", "0", "five", "65"])
def test_o68_up_without_a_maximum_the_operator_set_is_refused(bench, value):
    environ = {k: v for k, v in bench.environ.items() if k != "HPC_FLEET_MAX"}
    if value is not None:
        environ["HPC_FLEET_MAX"] = value
    code, printed = bench.up(2, environ=environ)
    assert code == 2
    assert "HPC_FLEET_MAX" in printed
    assert not bench.fleet.exists()


def test_o68_up_refused_by_the_inode_guard_makes_nothing(bench):
    """O68: the guard is asked once for all N federations, before the fleet's directory or any job."""
    asked = []

    def refuse(count, layout, site, environ):
        asked.append(count)
        raise RuntimeError("the federations' inodes would pass 95% of the account's inode quota on scratch")

    bench.guard = refuse
    code, printed = bench.up(4)
    assert code == 2
    assert asked == [4]
    assert "inode quota" in printed
    assert not bench.fleet.exists()
    assert bench.fakes == {}


def test_o68_up_never_adopts_an_existing_fleet_directory(bench):
    bench.fleet.mkdir()
    code, printed = bench.up(2)
    assert code == 2
    assert "never adopts" in printed
    assert list(bench.fleet.iterdir()) == []
    assert bench.fakes == {}


def test_o68_one_failed_up_leaves_the_others_up_and_says_its_job_may_be_held(bench):
    """O68: fed-2's data is not its manifest's, so its up fails in the core's phase, after its sbatch: fed-1 and fed-3
    stay up, fed-2 is recorded as failed, and its held job is named, never cancelled by the fleet behind its back."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    code, printed = bench.up(3)
    assert code == 1
    found = {m["label"]: m for m in record(bench)["members"]}
    assert (found["fed-1"]["status"], found["fed-2"]["status"], found["fed-3"]["status"]) == ("up", "failed", "up")
    assert found["fed-2"]["jobs"] == dict(zip(("allocation", "sandbox"), job_ids("fed-2"), strict=True))
    assert f"its job(s) {', '.join(job_ids('fed-2'))} may still be held: `fleet.py down --member fed-2`" in printed
    assert bench.fake("fed-2").cancelled == []
    for label in ("fed-1", "fed-3"):
        assert state(bench, label)["phase"] == "linked"
        assert bench.fake(label).cancelled == []


def test_o68_up_runs_at_most_the_parallel_setting_at_once(bench):
    """O68: HPC_FLEET_PARALLEL=2 over four federations: two ups at once, never a third, every one run."""
    lock, entered, gate, full = threading.Lock(), [], threading.Event(), threading.Event()
    inflight = {"now": 0, "most": 0}

    def operate(label, command, layout, root, log):
        with lock:
            inflight["now"] += 1
            inflight["most"] = max(inflight["most"], inflight["now"])
            entered.append(label)
            if inflight["now"] == 2:
                full.set()
        assert gate.wait(30), "the test never opened the gate"
        with lock:
            inflight["now"] -= 1
        return 1, "error: up: a stand-in"

    environ = {**bench.environ, "HPC_FLEET_PARALLEL": "2"}
    result = {}
    runner = threading.Thread(target=lambda: result.update(code=bench.up(4, environ=environ, federation=operate)))
    runner.start()
    assert full.wait(30)
    with lock:
        assert len(entered) == 2  # the third waits for a free worker
    gate.set()
    runner.join(30)
    assert not runner.is_alive()
    assert inflight["most"] == 2
    assert len(entered) == 4
    assert result["code"][0] == 1  # the stand-ins fail every up: what is tested is how many ran at once


# ------------------------------------------------------------------------------------------------ add
def test_o68_add_counts_every_federation_that_may_hold_a_job_against_the_maximum(bench):
    """O68: a failed federation may still hold its job, so it counts; one taken down does not."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    environ = {**bench.environ, "HPC_FLEET_MAX": "3"}
    assert bench.up(2, environ=environ)[0] == 1
    code, printed = bench.run("add", "2", environ=environ)
    assert code == 2
    assert "2 federation(s) may hold a job and 2 more were asked for, past the maximum of 3" in printed
    assert "fed-3" not in bench.fakes
    assert bench.run("down", "--member", "fed-2", environ=environ)[0] == 0
    code, printed = bench.run("add", "2", environ=environ)
    assert code == 0, printed
    assert [m["status"] for m in record(bench)["members"]] == ["up", "down", "up", "up"]


def test_o68_add_never_adopts_a_slot_someone_else_made(bench):
    """O68: fed-3's slot made by hand before the add: never brought up, never taken down, left as it is."""
    assert bench.up(2)[0] == 0
    (bench.fleet / "fed-3" / "root").mkdir(parents=True)
    (bench.fleet / "fed-3" / "root" / "theirs").write_text("not the fleet's\n", encoding="utf-8")
    code, printed = bench.run("add", "1")
    assert code == 1
    assert "fed-3: its slot exists and is never adopted" in printed
    assert "fed-3" not in bench.fakes
    assert bench.run("down")[0] == 0
    assert (bench.fleet / "fed-3" / "root" / "theirs").is_file()
    assert "fed-3" not in bench.fakes


# ------------------------------------------------------------------------------------------------ down
def test_o68_down_tears_every_federation_down_each_verified(bench):
    """O68: every federation's driver down: each cluster cancels its own job alone, each root is gone, and the fleet
    records each as down."""
    assert bench.up(3)[0] == 0
    code, printed = bench.run("down")
    assert code == 0, printed
    for label in ("fed-1", "fed-2", "fed-3"):
        assert bench.fake(label).cancelled == list(job_ids(label))
        assert not bench.root(label).exists()
    assert [m["status"] for m in record(bench)["members"]] == ["down", "down", "down"]
    assert "3 of 3 federation(s) down" in printed
    assert bench.run("down")[0] == 0  # nothing left: every one is down already


def test_o68_down_touches_no_federation_and_no_job_it_did_not_make(bench):
    """O68: a federation brought up by hand in the fleet's directory, in a slot of the fleet's own form that its record
    does not list (its own root, its own cluster), and another user's job on a fleet federation's cluster: the fleet's
    down cancels neither and removes nothing of the first."""
    assert bench.up(2)[0] == 0
    other_root = bench.fleet / "fed-9" / "root"
    other_root.parent.mkdir()
    assert bench.federation("fed-9", "up", bench.layout, other_root, bench.base / "other.log")[0] == 0
    calls = len(bench.fake("fed-9").calls)
    assert bench.fake("fed-9").submitted  # it is up, on a cluster of its own
    bench.fake("fed-1").submitted["7777"] = [{"--job-name": "someone-elses-job", "--nodes": "1"}]
    assert bench.run("down")[0] == 0
    assert len(bench.fake("fed-9").calls) == calls
    assert bench.fake("fed-9").cancelled == []
    assert (other_root / "hpc-federation.json").is_file()
    assert bench.fake("fed-1").cancelled == list(job_ids("fed-1"))
    assert "7777" not in bench.fake("fed-1").cancelled


def test_c54_a_member_its_down_left_unswept_goes_down_only_with_the_operators_node_check(bench, monkeypatch):
    """C54: fed-1's worker step outlives its TERM and its cancel, so its down cannot sweep the agent's node: down
    fails with the root kept and the node recorded unswept; a second `down --member fed-1`, its job gone, still keeps
    the root; `--node-checked TEXT` (the operator's word that the README's sweep in a job on that node passed) takes it
    down, and the member's down log keeps the text. `--node-checked` without `--member` is refused."""
    monkeypatch.setattr(sys.modules["hpcdown"], "STEP_TERM_S", 0.0)
    monkeypatch.setitem(sys.modules["hpcdriver"].TIMEOUTS, "cancel", 0.0)
    assert bench.up(2)[0] == 0
    step = f"{job_ids('fed-1')[1]}.9"
    bench.fake("fed-1").steps[f"{state(bench, 'fed-1')['sandbox']['name']}-worker"] = step
    bench.fake("fed-1").unending.append(step)
    assert bench.run("down", "--member", "fed-1")[0] == 1
    assert state(bench, "fed-1")["unswept"] == ["the agent's node"]
    code, printed = bench.run("down", "--member", "fed-1")
    assert code == 1
    assert "the agent's node was never swept" in printed
    assert bench.root("fed-1").is_dir()
    assert bench.run("down", "--node-checked", "looked")[0] == 2
    checked = "shards.py sweep in a job on its node: exit 0"
    code, printed = bench.run("down", "--member", "fed-1", "--node-checked", checked)
    assert code == 0, printed
    assert not bench.root("fed-1").exists()
    assert f"the operator checked: {checked}" in (bench.fleet / "logs" / "fed-1.down.log").read_text(encoding="utf-8")
    assert [m["status"] for m in record(bench)["members"]] == ["down", "up"]


def test_o68_down_refuses_a_root_whose_state_is_another_federations(bench):
    """O68: fed-2's state file replaced by one of another federation (another job name): fed-2 is not touched and is
    reported, fed-1 goes down, and down fails."""
    assert bench.up(2)[0] == 0
    path = bench.root("fed-2") / "hpc-federation.json"
    replaced = json.loads(path.read_text(encoding="utf-8"))
    replaced["allocation"]["name"] = "hpcop-another-federation-0000"
    path.write_text(json.dumps(replaced), encoding="utf-8")
    code, printed = bench.run("down")
    assert code == 1
    assert "fed-2: NOT TOUCHED: fed-2: its state file is of another federation" in printed
    assert bench.fake("fed-2").cancelled == []
    assert bench.root("fed-2").is_dir()
    assert bench.fake("fed-1").cancelled == list(job_ids("fed-1"))
    assert [m["status"] for m in record(bench)["members"]] == ["down", "down-failed"]


@pytest.mark.parametrize("change", ["job", "name"])
def test_o68_down_refuses_a_state_whose_agents_job_is_not_the_one_its_up_held(bench, change):
    """O68: fed-2's state keeps its own allocation and command, but its agent's own job record is replaced by another
    job's (another id, or another name): down would cancel that job by the record, so fed-2 is not touched at all."""
    assert bench.up(2)[0] == 0
    path = bench.root("fed-2") / "hpc-federation.json"
    replaced = json.loads(path.read_text(encoding="utf-8"))
    if change == "job":
        replaced["sandbox"]["job"] = "7777"
    else:
        replaced["sandbox"]["name"] = "hpcop-another-federation-0000-sandbox"
    path.write_text(json.dumps(replaced), encoding="utf-8")
    code, printed = bench.run("down")
    assert code == 1
    assert "fed-2: NOT TOUCHED" in printed
    assert bench.fake("fed-2").cancelled == []
    assert bench.fake("fed-1").cancelled == list(job_ids("fed-1"))


def test_o68_down_refuses_a_root_that_is_not_the_directory_the_fleet_made(bench):
    """O68: fed-2's root moved away and another directory put in its place, its state copied: not the fleet's mkdir
    (device and inode), so it is not touched."""
    assert bench.up(2)[0] == 0
    root = bench.root("fed-2")
    os.rename(root, root.with_name("moved"))
    root.mkdir()
    shutil.copyfile(root.with_name("moved") / "hpc-federation.json", root / "hpc-federation.json")
    code, printed = bench.run("down")
    assert code == 1
    assert "fed-2: its root is not the directory this fleet made" in printed
    assert bench.fake("fed-2").cancelled == []


def test_o68_a_record_that_names_a_root_the_fleet_does_not_make_is_refused_whole(bench):
    """O68: the record is the only list down acts on, and it accepts no root but `<fleet>/fed-k/root`."""
    assert bench.up(2)[0] == 0
    path = bench.fleet / "fleet.json"
    edited = json.loads(path.read_text(encoding="utf-8"))
    edited["members"][1]["root"] = str(bench.base / "elsewhere" / "root")
    path.write_text(json.dumps(edited), encoding="utf-8")
    code, printed = bench.run("down")
    assert code == 2
    assert "is not a root this fleet makes" in printed
    assert all(fake.cancelled == [] for fake in bench.fakes.values())


def test_o68_a_federation_whose_up_stopped_before_it_marked_its_root_is_still_taken_down(bench):
    """O68: fed-2's up refused in its preflight, before it touched the root: no state file, no mark, the fleet's empty
    mkdir alone, which hpcfed's down refuses as unmarked; the fleet removes that empty root itself, runs no down for it,
    and fed-2 no longer counts against the maximum."""
    calls = []

    def operate(label, command, layout, root, log):
        calls.append((label, command))
        if (label, command) == ("fed-2", "up"):
            return 2, "error: up: the root lies inside another federation's root, or holds one; refused\n"
        return bench.federation(label, command, layout, root, log)

    assert bench.up(2, federation=operate)[0] == 1
    assert {m["label"]: m["status"] for m in record(bench)["members"]} == {"fed-1": "up", "fed-2": "failed"}
    assert bench.fake("fed-2").submitted == {}
    code, printed = bench.run("down", "--member", "fed-2", federation=operate)
    assert code == 0, printed
    assert ("fed-2", "down") not in calls
    assert not bench.root("fed-2").exists()
    assert [m["status"] for m in record(bench)["members"]] == ["up", "down"]


def test_o68_an_unmarked_root_that_holds_anything_is_kept(bench):
    """O68: the fleet removes only its own empty mkdir: a root its up never marked that holds a file is kept."""

    def operate(label, command, layout, root, log):
        return (2, "error: up: refused\n") if label == "fed-1" else bench.federation(label, command, layout, root, log)

    assert bench.up(2, federation=operate)[0] == 1
    (bench.root("fed-1") / "someone-elses").write_text("kept\n", encoding="utf-8")
    code, printed = bench.run("down", "--member", "fed-1")
    assert code == 1
    assert "its root holds files no up of this fleet's marked" in printed
    assert (bench.root("fed-1") / "someone-elses").is_file()


def test_o68_down_of_one_member_leaves_the_others_up(bench):
    assert bench.up(3)[0] == 0
    assert bench.run("down", "--member", "fed-2")[0] == 0
    assert bench.fake("fed-2").cancelled == list(job_ids("fed-2"))
    assert bench.fake("fed-1").cancelled == bench.fake("fed-3").cancelled == []
    assert [m["status"] for m in record(bench)["members"]] == ["up", "down", "up"]


# ------------------------------------------------------------------------------------------------ the layout
def test_o68_the_committed_separate_layout_is_the_pilots_but_for_the_agents_nodes_placement():
    """O68: `layouts/pilot-shape-separate.toml` is the pilot's layout with the agent's node held as a job of its own,
    which a fleet requires, and nothing else: the same file read as TOML but for `[hpc] sandbox_placement`, the same
    template, and a comment that says why it exists."""
    pilot = tomllib.loads(LAYOUT.read_text(encoding="utf-8"))
    separate_text = SEPARATE_LAYOUT.read_text(encoding="utf-8")
    separate = tomllib.loads(separate_text)
    assert pilot["hpc"]["sandbox_placement"] == "het"
    assert separate["hpc"]["sandbox_placement"] == "separate"
    pilot["hpc"]["sandbox_placement"] = separate["hpc"]["sandbox_placement"]
    assert separate == pilot
    assert SEPARATE_LAYOUT.parent == LAYOUT.parent  # so the template named in the file is the pilot's own
    assert (SEPARATE_LAYOUT.parent / separate["platform_template"]).is_file()
    comment = " ".join(line.lstrip("# ") for line in separate_text.split("\n\n")[0].splitlines())
    for word in ("pilot-shape.toml", "separate", "interconnect"):
        assert word in comment


def test_o68_a_fleet_takes_the_committed_separate_layout_and_refuses_the_pilots_own(deploy):
    """O68: `separate_only` accepts the committed separate layout and refuses `pilot-shape.toml` as before."""
    deploy.fleet.separate_only(deploy.fleet.load_spec(SEPARATE_LAYOUT))
    with pytest.raises(deploy.fleet.FleetError, match='sandbox_placement = "separate"'):
        deploy.fleet.separate_only(deploy.fleet.load_spec(LAYOUT))


# ------------------------------------------------------------------------------------------------ the inode guard
PATTERN = r"inodes used (?P<used>[0-9,]+) of (?P<quota>[0-9,]+)"  # an invented format, as invented as its tool
TOOL = "fake-quota"  # an invented command name
LINE = 19_000  # the 95% line of a quota of 20,000


class Quota:
    """The invented quota command: what it prints, every call recorded."""

    def __init__(self, deploy, used, quota=20_000):
        self.completed, self.calls, self.text = deploy.hpc.slurm.Completed, [], f"inodes used {used:,} of {quota:,}\n"

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        return self.completed(0, self.text, "")


def scratch_site(deploy):
    """A site with a scratch root and the invented quota command, as `site.env` would give them."""
    environ = {**SITE, "SH": "/invented/images", "L": "/invented/logs", "HPC_STORAGE_ROOT": "/invented/scratch",
               "HPC_QUOTA_CMD": f"{TOOL} --inodes", "HPC_QUOTA_PATTERN": PATTERN}  # fmt: skip
    return deploy.hpc.settings.site_from_environment(environ)


def one_federation(deploy):
    """The separate layout as one federation's, for the estimate alone (`Fleet.scratch_layout`)."""
    return deploy.fleet.Layout(Path("/x/fed-1/root"), Path("/x/fed-1"), deploy.fleet.load_spec(SEPARATE_LAYOUT))


def test_o68_the_inode_guard_is_the_cores_own_read_once_for_n_times_one_federations_estimate(deploy):
    """O68: the fleet's check is hpcquota's guard (#343) for N federations at once: the account's usage, read once,
    against N times one federation's estimate: N ups would each read the same usage. A fleet exactly at the line
    passes and says the figures; one inode over is refused, naming them."""
    layout, site = one_federation(deploy), scratch_site(deploy)
    each = sum(deploy.hpc.quota.estimate_inodes(layout, {}).values())
    assert each > 0
    at_the_line = Quota(deploy, used=LINE - 4 * each)
    said = deploy.fleet.inode_guard(FakeSlurm(tools={TOOL: at_the_line}), 4, layout, site, {})
    assert at_the_line.calls == [[TOOL, "--inodes"]]
    for part in (f"estimate {4 * each}", f"95% line {LINE}", "under the line"):
        assert part in said
    over = Quota(deploy, used=LINE - 4 * each + 1)
    with pytest.raises(deploy.hpc.quota.QuotaRefused, match=f"estimate {4 * each}, quota 20000") as refused:
        deploy.fleet.inode_guard(FakeSlurm(tools={TOOL: over}), 4, layout, site, {})
    assert "nothing was submitted" in str(refused.value)
    assert len(over.calls) == 1


def test_o68_the_fleets_estimate_counts_each_federations_ingested_space_n_times(deploy, tmp_path):
    """O68, O57: a layout whose ingested space declares its manifest (the study's own data, which `up` loads) counts it
    in every federation of the fleet: N times one federation's estimate, ingested space included, at the line and one
    inode over, from the site's `HPC_STUDY_ARTIFACTS`."""
    manifest = manifest_of({f"records/d{k % 20:02d}/f{k:04d}.csv": f"{k}".encode() for k in range(2_000)})
    folder = artifacts(tmp_path / "art", manifest)
    spec = deploy.fleet.load_spec(study_layout(tmp_path / "layout"))
    layout = deploy.fleet.Layout(Path("/x/fed-1/root"), Path("/x/fed-1"), spec)
    site = dataclasses.replace(scratch_site(deploy), study_artifacts=str(folder))
    values = {"study_artifacts": str(folder)}
    each = sum(deploy.hpc.quota.estimate_inodes(layout, {}, values).values())
    assert each >= len(manifest.files) + len(manifest.directories)
    at_the_line = Quota(deploy, used=LINE - 3 * each)
    said = deploy.fleet.inode_guard(FakeSlurm(tools={TOOL: at_the_line}), 3, layout, site, {})
    assert f"estimate {3 * each}" in said
    over = Quota(deploy, used=LINE - 3 * each + 1)
    with pytest.raises(deploy.hpc.quota.QuotaRefused, match=f"estimate {3 * each}"):
        deploy.fleet.inode_guard(FakeSlurm(tools={TOOL: over}), 3, layout, site, {})


def test_o68_the_inode_guard_of_a_site_without_a_scratch_root_runs_no_command_and_says_so(deploy):
    """O68: nothing of the federations is on scratch without `HPC_STORAGE_ROOT`: no quota is read, and the fleet says
    that no check was made."""
    quota = Quota(deploy, used=0)
    site = dataclasses.replace(scratch_site(deploy), storage_root="")
    said = deploy.fleet.inode_guard(FakeSlurm(tools={TOOL: quota}), 4, one_federation(deploy), site, {})
    assert said.startswith("inode check: none")
    assert "HPC_STORAGE_ROOT is not set" in said
    assert quota.calls == []


@pytest.mark.parametrize(
    ("change", "why"),
    [({"quota_check": "off"}, "cannot be turned off"), ({"quota_cmd": ""}, "HPC_QUOTA_CMD is not set")],
    ids=["override", "no-command"],
)
def test_o68_the_inode_guard_of_a_scratch_site_fails_closed_as_the_cores_own_does(deploy, change, why):
    """O68: the mandatory guard of #343 is the fleet's: an override of the check and an unset command each refuse a
    fleet on scratch, however the site was built, and no quota is read for the override."""
    quota = Quota(deploy, used=0)
    site = dataclasses.replace(scratch_site(deploy), **change)
    with pytest.raises(deploy.hpc.quota.QuotaRefused, match=why):
        deploy.fleet.inode_guard(FakeSlurm(tools={TOOL: quota}), 3, one_federation(deploy), site, {})
    assert quota.calls == []


def test_o68_the_default_hooks_ask_the_cores_inode_guard_and_the_fleet_no_longer_has_a_stand_in(deploy):
    """O68: the fleet's default hooks wire `inode_guard` to the core's `hpcquota` with the login node's runner, and the
    fleet module imports it as every driver module does, with no fallback for a core without it."""
    hooks = deploy.fleet.default_hooks({})
    site = dataclasses.replace(scratch_site(deploy), storage_root="")
    assert "HPC_STORAGE_ROOT is not set" in hooks.inode_guard(2, one_federation(deploy), site, {})
    assert deploy.fleet.hpcquota is deploy.hpc.quota
    assert "ImportError" not in Path(deploy.fleet.__file__).read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ one process each
def test_o68_each_federations_up_and_down_is_hpcfed_in_a_process_of_its_own(deploy, tmp_path):
    """O68: a federation's driver changes process-wide state (the umask its secrets are written under), so the fleet
    runs each up and down as `hpcfed.py --layout L --root R <command>` in a child of its own, isolated (`-I`) and from
    the fleet's own directory, with the site's environment, its output appended to the federation's log (0600)."""
    seen = []

    def run(argv, **options):
        seen.append((argv, options))
        os.write(options["stdout"], b"error: up: refused\n")
        return SimpleNamespace(returncode=1)

    operate = deploy.fleet.hpcfed_process({"HPC_ACCOUNT": "invented"}, run=run)
    log = tmp_path / "fed-1.up.log"
    code, printed = operate("fed-1", "up", LAYOUT, tmp_path / "fed-1" / "root", log)
    ((argv, options),) = seen
    assert argv[:3] == [sys.executable, "-I", "-B"]
    assert options["cwd"] == str(tmp_path)
    assert argv[3:] == [str(deploy.fleet.HERE / "hpcfed.py"), "--layout", str(LAYOUT), "--root",
                        str(tmp_path / "fed-1" / "root"), "up"]  # fmt: skip
    # an up also gets the lock the fleet's ups share (`<fleet>/.placement.lock`, the root's grandparent); a down none
    lock = str(tmp_path / ".placement.lock")
    assert options["env"] == {"HPC_ACCOUNT": "invented", "HPC_FLEET_PLACEMENT_LOCK": lock}
    assert (code, printed) == (1, "error: up: refused\n")
    assert log.stat().st_mode & 0o777 == 0o600
    said = deploy.fleet.last_error("hpcfed: zone launched\nerror: up: the zone failed\n")
    assert said == "error: up: the zone failed"


def test_o68_hpcfed_runs_in_its_child_isolated_from_the_fleets_directory(deploy, tmp_path):
    """O68: the child really runs: `hpcfed.py describe` (nothing of a cluster) under `-I`, so a PYTHONPATH that would
    put another `expcore_onedata` first is ignored, from the fleet's directory, its output in the federation's log."""
    fleet, planted = tmp_path / "fleet", tmp_path / "planted" / "expcore_onedata"
    (fleet / "fed-1").mkdir(parents=True, mode=0o700)
    planted.mkdir(parents=True)
    (planted / "__init__.py").write_text("raise SystemExit('a planted module was imported')\n", encoding="utf-8")
    environ = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "PYTHONPATH": str(planted.parent)}
    operate = deploy.fleet.hpcfed_process(environ)
    code, printed = operate("fed-1", "describe", LAYOUT, fleet / "fed-1" / "root", fleet / "fed-1.describe.log")
    assert code == 0, printed
    assert "held job: 6 node(s)" in printed
    assert not (fleet / "fed-1" / "root").exists()
