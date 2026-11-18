"""C43: the elastic fleet's controller (deploy/hpc-federation/fleetcontrol.py) on simulated sites over a fake Slurm: its
read of a site is read-only and fails whole rather than guess, and its loop joins, drains, retires (collect first,
never with a lease in flight) and replaces federations through the fleet's own commands; a dry run prints exactly
what a live tick runs, and runs nothing."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from control_site_support import FakeSite, Job, observed
from elastic_support import ENVIRONMENT, FINGERPRINT, FakeClock, config, plan
from hpc_fleet_support import fleet_modules

from expcore.campaign.elastic import Ledger
from expcore.campaign.elastic.control import Add, Retire, SiteSettings

DIGEST = "c" * 64


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetcontrol
        import fleetexecute
        import fleetobserve
        import fleetrecord
        import hpcslurm
        import hpcstate

        yield SimpleNamespace(fleet=found.fleet, fleetcontrol=fleetcontrol, fleetexecute=fleetexecute,
                              fleetobserve=fleetobserve, fleetrecord=fleetrecord, hpcslurm=hpcslurm,
                              hpcstate=hpcstate)  # fmt: skip


@pytest.fixture
def site(deploy, tmp_path):
    return FakeSite(tmp_path / "site-a", deploy)


def running(site, minutes_ago: float = 60, ends_in: float = 4 * 3600, wait: float = 50) -> Job:
    started = site.now - minutes_ago * 60
    return Job("RUNNING", submitted=started - wait, started=started, ends=site.now + ends_in)


def up(site, label: str, base_id: int, **job) -> None:
    """A member up, linked and verified, its two held jobs running."""
    site.add_member(label, jobs={"allocation": (str(base_id), running(site, **job)),
                                 "sandbox": (str(base_id + 1), running(site, **job))})  # fmt: skip


class Bench:
    """A ledger, a sites file and the controller's hooks over one or more fake sites, on one clock."""

    def __init__(self, tmp_path: Path, deploy, sites: list, runs=None, **ledger_changes) -> None:
        self.deploy, self.sites, self.tmp = deploy, {f"site-{chr(97 + i)}": s for i, s in enumerate(sites)}, tmp_path
        self.clock = FakeClock(sites[0].now)
        self.ledger = tmp_path / "out" / "ledger.sqlite"
        Ledger.create(self.ledger, runs or plan(tasks=2), config(**ledger_changes), clock=self.clock).close()
        self.log = tmp_path / "control.jsonl"
        self.printed: list = []
        self.settings: dict = {}

    def file(self) -> Path:
        lines = ["[controller]", f'evidence = "{self.tmp / "evidence"}"', f'log = "{self.log}"']
        for label, fake in self.sites.items():
            fields = {"max_federations": 7, "allowance_units": 1e12, "rate_units_per_hour": 47.0,
                      "parallel_ups": 4, "lease_bound_s": 1800.0, **self.settings.get(label, {})}  # fmt: skip
            lines += ["[[site]]", f'label = "{label}"', f'fleet = "{fake.fleet}"']
            lines += [f"{key} = {json.dumps(value)}" for key, value in fields.items()]
        path = self.tmp / "sites.toml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def hooks(self, identity=None):
        sites = self.sites

        class Transport:
            def run(self, config_, argv, timeout):
                return sites[config_.label].transport().run(config_, argv, timeout)

        return self.deploy.fleetcontrol.Hooks(
            runner=lambda: None,
            transport=lambda environ: Transport(),
            clock=self.clock,
            sleep=lambda s: None,
            identity=identity or (lambda site, member: (FINGERPRINT, ENVIRONMENT)),
            say=self.printed.append,
        )

    def tick(self, *extra: str, identity=None) -> int:
        argv = ["control", "--ledger", str(self.ledger), "--sites", str(self.file()), "--once", *extra]
        return self.deploy.fleetcontrol.main(argv, {}, self.hooks(identity))

    def open(self) -> Ledger:
        return Ledger.open(self.ledger, clock=self.clock)

    def lines(self, kind: str) -> list:
        return [e for e in map(json.loads, self.log.read_text().splitlines()) if e["kind"] == kind]


def federations(bench) -> dict:
    ledger = bench.open()
    try:
        return {f.label: f for f in ledger.federations()}
    finally:
        ledger.close()


def verbs(site) -> list:
    return [cmd[4] if cmd[3].endswith("shards.py") else cmd[cmd.index("--fleet") + 2] for cmd in site.commands]


def retiring(site) -> list:
    """The site's commands other than adds, in order: a retirement's collect, verify-copy and down."""
    return [verb for verb in verbs(site) if verb != "add"]


# ------------------------------------------------------------------------------------------------ the site's read
def test_c43_observe_reads_members_jobs_nodes_and_quota_and_asks_slurm_only_of_its_own_jobs(site):
    """C43: each member's jobs by the ids its up recorded: running ones with the earliest submit, the latest start and
    the earliest end; a waiting one; an id Slurm forgot and one that now names another job are GONE (never touched);
    a member down is not read. Only exactly idle nodes are free; the quota and one federation's estimate are read.
    Slurm is asked nothing but sinfo and scontrol on those ids."""
    up(site, "fed-1", 41)
    site.add_member(
        "fed-2", status="made", phase="submitted", jobs={"allocation": ("43", Job("PENDING", site.now - 100))}
    )
    site.add_member("fed-3", jobs={"allocation": ("44", running(site))})
    del site.jobs["44"]
    site.add_member("fed-4", jobs={"allocation": ("45", Job("RUNNING", site.now - 99, site.now - 9, name="another"))})
    site.add_member("fed-5", status="down", phase=None, jobs={"allocation": ("46", running(site))})
    read = observed(site)
    members = {m["label"]: m for m in read["members"]}
    one = members["fed-1"]
    assert (one["state"], one["linked"], one["verified"], one["jobs"]) == ("RUNNING", True, True, ["41", "42"])
    assert one["started_at"] - one["submitted_at"] == 50.0
    assert one["ends_at"] == site.now + 4 * 3600
    assert (members["fed-2"]["state"], members["fed-2"]["started_at"]) == ("PENDING", None)
    assert members["fed-3"]["state"] == members["fed-4"]["state"] == "GONE"
    assert members["fed-5"]["state"] is None
    assert (read["idle_nodes"], read["mixed_nodes"], read["nodes_per_federation"], read["time_s"]) == (
        70,
        300,
        7,
        7200.0,
    )
    assert (read["inodes_used"], read["inodes_quota"], read["inodes_error"]) == (826_221, 1_000_000, None)
    assert read["inode_estimate"] > 0
    asked = {tuple(call[:4]) for call in site.calls}
    assert asked <= {("sinfo", "-h", "-p", "test-partition"), ("scontrol", "show", "job", "-o"),
                     ("invented-quota", "--files")}  # fmt: skip
    assert sorted(call[4] for call in site.calls if call[0] == "scontrol") == ["41", "42", "43", "44", "45"]


@pytest.mark.parametrize("broken", ["scontrol", "sinfo", "state"])
def test_c43_a_read_that_fails_anywhere_fails_whole_and_the_site_is_dark(deploy, tmp_path, site, broken):
    """C43: a job read that times out, sinfo failing, or a job state the controller does not read makes the whole
    read fail: the site is dark, gets no action and proves no end, and the demand comes up at the other site."""
    other = FakeSite(tmp_path / "site-b", deploy)
    up(site, "fed-1", 41)
    bench = Bench(tmp_path, deploy, [site, other])
    assert bench.tick() == 0
    joined = federations(bench)
    assert "site-a-fed-1" in joined
    site.commands.clear()
    if broken == "scontrol":
        site.scontrol_fails = True
    elif broken == "sinfo":
        site.sinfo_fails = True
    else:
        site.jobs["41"].state = "SUSPENDED"
    bench.clock.advance(60)
    assert bench.tick() == 0
    assert site.commands == []
    assert federations(bench)["site-a-fed-1"].ended_at is None
    tick = bench.lines("tick")[-1]
    assert tick["targets"]["site-a"]["target"] == 0
    assert "site-b" in {a["site"] for a in tick["actions"] if a["action"] == "Add"}


def test_c43_a_quota_that_cannot_be_read_leaves_no_room_but_the_site_stays_readable(deploy, tmp_path, site):
    """C43: the quota command failing is reported and stops adds at the site; the rest of its read stands."""
    site.quota = None
    bench = Bench(tmp_path, deploy, [site])
    bench.tick()
    tick = bench.lines("tick")[-1]
    assert tick["targets"]["site-a"]["limits"]["inodes"] == 0
    assert "dark" not in tick["targets"]["site-a"]
    assert site.commands == []


# ------------------------------------------------------------------------------------------------ the loop
def test_c43_a_dry_run_prints_exactly_what_a_live_tick_runs_and_runs_nothing(deploy, tmp_path, site):
    """C43: with 4 runs pending and no federation, the dry run prints `fleet.py --fleet F add 4`, the very argv the live
    tick then gives the site; the dry run gives the site nothing and leaves the ledger's file as it was."""
    bench = Bench(tmp_path, deploy, [site])
    before = bench.ledger.read_bytes()
    assert bench.tick("--dry-run") == 0
    assert site.commands == []
    assert bench.ledger.read_bytes() == before
    printed = "\n".join(bench.printed)
    (action,) = bench.lines("tick")[-1]["actions"]
    assert action["action"] == "Add"
    assert action["steps"][0][-4:] == ["--fleet", str(site.fleet), "add", "4"]
    assert " ".join(action["steps"][0]) in printed
    assert bench.tick() == 0
    assert site.commands == [action["steps"][0]]


def test_c43_a_read_pulled_earlier_is_for_a_dry_run_and_never_acted_on(deploy, tmp_path, site):
    """C43: `--observed SITE=FILE` takes a read an operator pulled; a live tick refuses it (a stale read could start an
    up past what the site holds now), and a dry run prints from it."""
    pulled = tmp_path / "site-a.json"
    pulled.write_text(json.dumps(observed(site)), encoding="utf-8")
    bench = Bench(tmp_path, deploy, [site])
    assert bench.tick("--observed", f"site-a={pulled}") == 2
    assert site.commands == []
    assert bench.tick("--dry-run", "--observed", f"site-a={pulled}") == 0
    assert site.commands == []
    assert "add 4" in "\n".join(bench.printed)


def test_c43_a_federation_joins_drains_before_its_end_is_collected_then_taken_down_and_proven_cancelled(
    deploy, tmp_path, site
):
    """C43: up and verified, it joins with its earliest job end and its queue wait logged; inside the cut-off it is
    drained; with no lease it is collected, its copy verified with collect's own hash, and taken down, in that order;
    once the fleet shows it down, its end is proven `cancelled`."""
    up(site, "fed-1", 41, ends_in=3000)
    bench = Bench(tmp_path, deploy, [site], runs=plan(tasks=1, conditions=("api-none",)))
    bench.tick()
    fed = federations(bench)["site-a-fed-1"]
    assert (fed.job_ids, fed.job_end_at, fed.drained_at) == (("41", "42"), site.now + 3000, None)
    assert bench.lines("queue_wait")[-1]["queue_wait_s"] == 50.0
    bench.clock.advance(700)  # 2300 s left, inside 1800 + 60 + 180 + 300
    bench.tick()
    assert federations(bench)["site-a-fed-1"].drained_at is not None
    site.answers = {"collect": (0, DIGEST + "\n", "")}
    site.on_command = lambda argv: site.set_status("fed-1", "down") if "down" in argv else None
    site.commands.clear()
    bench.clock.advance(60)
    bench.tick()
    assert retiring(site) == ["collect", "verify-copy", "down"]
    collect, verify, down = [cmd for cmd in site.commands if "add" not in cmd]
    assert collect[collect.index("--root") + 1] == str(site.fleet / "fed-1" / "root")
    assert verify[-2:] == ["--manifest-sha256", DIGEST]
    assert down[-2:] == ["--member", "fed-1"]
    assert "--discard-evidence" not in down
    assert federations(bench)["site-a-fed-1"].ended_at is None  # only once the record shows it down
    bench.clock.advance(60)
    bench.tick()
    fed = federations(bench)["site-a-fed-1"]
    assert (fed.ended_at is not None, fed.end_proof) == (True, "cancelled")


def test_c43_a_failed_collect_stops_the_retirement_before_down(deploy, tmp_path, site):
    """C43: collect failing, or printing no manifest hash, leaves the federation up: nothing verified or taken down."""
    up(site, "fed-1", 41)
    bench = Bench(tmp_path, deploy, [site])
    bench.tick()
    ledger = bench.open()
    ledger.drain("site-a-fed-1")
    ledger.close()
    for answer in ((1, "", "error: collect: the root holds a link\n"), (0, "collected\n", "")):
        site.commands.clear()
        site.answers = {"collect": answer}
        bench.clock.advance(60)
        bench.tick()
        assert retiring(site) == ["collect"]


def test_c43_a_federation_with_a_lease_in_flight_is_never_retired(deploy, tmp_path, site):
    """C43: drained by hand while its lease is sent, it is not collected or taken down; a retirement decided earlier
    is refused when the ledger shows a lease in flight at the moment it would start."""
    up(site, "fed-1", 41)
    bench = Bench(tmp_path, deploy, [site])
    bench.tick()
    site.commands.clear()
    ledger = bench.open()
    lease = ledger.issue("site-a-fed-1")
    ledger.mark_sent(lease.lease_id)
    ledger.drain("site-a-fed-1")
    log = deploy.fleetexecute.Log(None)
    config_ = deploy.fleetexecute.SiteConfig(SiteSettings("site-a", 7, 1e12, 47.0), site.fleet)
    executor = deploy.fleetexecute.Executor(ledger, bench.ledger, {"site-a": config_}, site.transport(),
                                            tmp_path / "evidence", log)  # fmt: skip
    executor.start(Retire("site-a", "fed-1", "site-a-fed-1", str(site.fleet / "fed-1" / "root"), True, "a test"))
    executor.wait()
    ledger.close()
    assert site.commands == []
    assert "in flight" in log.lines[-1]["said"]
    bench.clock.advance(60)
    bench.tick()
    assert retiring(site) == []


@pytest.mark.parametrize("ended", ["NODE_FAIL", "TIMEOUT"])
def test_c43_a_node_failure_or_walltime_end_is_proven_terminal_replaced_then_collected(deploy, tmp_path, site, ended):
    """C43: Slurm shows a joined federation's job ended: its end is proven `terminal` (its lease expires, its run goes
    back to pending); on the next tick a replacement is added and the ended one is collected and taken down."""
    up(site, "fed-1", 41)
    bench = Bench(tmp_path, deploy, [site], runs=plan(tasks=1, conditions=("api-none",)))
    bench.tick()
    ledger = bench.open()
    lease = ledger.issue("site-a-fed-1")
    ledger.mark_sent(lease.lease_id)
    ledger.close()
    site.jobs["41"].state = ended
    bench.clock.advance(60)
    bench.tick()
    fed = federations(bench)["site-a-fed-1"]
    assert (fed.end_proof, verbs(site)) == ("terminal", [])
    ledger = bench.open()
    assert (ledger.lease(lease.lease_id).state, ledger.run(lease.run_key).state) == ("expired", "pending")
    ledger.close()
    site.answers = {"collect": (0, DIGEST, "")}
    bench.clock.advance(60)
    bench.tick()
    assert retiring(site) == ["collect", "verify-copy", "down"]
    assert verbs(site).count("add") == 1


def test_c43_a_stalled_queue_stops_adds_there_and_its_wait_is_logged(deploy, tmp_path, site):
    """C43: an up whose job has waited 1000 s (past the site's 900 s bound) stops adds at its site; the other site
    takes them, and the wait is in the log."""
    other = FakeSite(tmp_path / "site-b", deploy)
    site.add_member(
        "fed-1", status="made", phase="submitted", jobs={"allocation": ("43", Job("PENDING", site.now - 1000))}
    )
    bench = Bench(tmp_path, deploy, [site, other])
    bench.settings = {"site-a": {"queue_stall_s": 900.0}}
    bench.tick()
    assert site.commands == []
    assert verbs(other) == ["add"]
    tick = bench.lines("tick")[-1]
    assert tick["targets"]["site-a"]["limits"]["stalled"] is True
    assert tick["waits"]["site-a"] == [{"member": "fed-1", "queue_wait_s": 1000.0, "waiting": True}]


@pytest.mark.parametrize("limit", ["quota", "nodes", "allowance"])
def test_c43_quota_exhaustion_no_free_whole_nodes_or_a_spent_allowance_add_nothing(deploy, tmp_path, site, limit):
    """C43: inodes past the 95% line, 6 idle nodes beside 300 mixed ones, or an allowance short of one more whole
    --time: the site adds nothing, and the log names the limit."""
    bench = Bench(tmp_path, deploy, [site])
    if limit == "quota":
        site.quota = (949_000, 1_000_000)
    elif limit == "nodes":
        site.nodes = {"idle": 6, "mixed": 300}
    else:
        bench.settings = {"site-a": {"allowance_units": 47.0 * 2 - 0.5}}
    bench.tick()
    assert site.commands == []
    limits = bench.lines("tick")[-1]["targets"]["site-a"]["limits"]
    assert limits[{"quota": "inodes", "nodes": "nodes", "allowance": "allowance"}[limit]] == 0


def test_c43_with_every_model_at_its_cap_idle_federations_are_drained_then_taken_down(deploy, tmp_path, site):
    """C43: one model capped at 1 with its slot held: the idle federations are drained once idle past their re-up
    cost, then collected and taken down; the one holding the lease is kept."""
    for n, label in enumerate(("fed-1", "fed-2", "fed-3")):
        up(site, label, 41 + 2 * n, ends_in=(9 - n) * 3600)
    bench = Bench(tmp_path, deploy, [site], caps={"m1": 1})
    bench.tick()
    ledger = bench.open()
    held = ledger.issue("site-a-fed-1")
    ledger.mark_sent(held.lease_id)
    ledger.close()
    bench.clock.advance(1000)
    bench.tick()
    feds = federations(bench)
    assert [label for label, f in sorted(feds.items()) if f.drained_at is not None] == ["site-a-fed-2", "site-a-fed-3"]
    site.answers = {"collect": (0, DIGEST, "")}
    bench.clock.advance(60)
    bench.tick()
    downs = [cmd[-1] for cmd in site.commands if "down" in cmd]
    assert sorted(downs) == ["fed-2", "fed-3"]


# ------------------------------------------------------------------------------------------------ today's commands
def test_c43_todays_fleet_commands_are_unchanged_and_the_controllers_calls_are_theirs(deploy, tmp_path, monkeypatch):
    """C43: fleet.py keeps exactly today's commands; the controller's add and down are those commands' own argv
    (`--fleet F add K`, `--fleet F down --member fed-k`), parsed by fleet.py's own parser, and its collect and
    verify-copy are shards.py's."""
    assert deploy.fleet.COMMANDS == ("up", "add", "plan", "step", "step-stop", "status", "down")
    settings = SiteSettings("site-a", 7, 1e12, 47.0)
    config_ = deploy.fleetexecute.SiteConfig(settings, tmp_path / "fleet")
    render = deploy.fleetexecute.render
    (add,) = render(Add("site-a", 2, "r"), config_, tmp_path / "e", tmp_path / "l").steps
    retire = render(Retire("site-a", "fed-3", None, "/r", True, "r"), config_, tmp_path / "e", tmp_path / "l").steps
    assert add[3].endswith("fleet.py")
    assert list(add[4:]) == ["--fleet", str(tmp_path / "fleet"), "add", "2"]
    assert list(retire[-1][4:]) == ["--fleet", str(tmp_path / "fleet"), "down", "--member", "fed-3"]
    assert [step[4] for step in retire[:2]] == ["collect", "verify-copy"]
    seen = []
    monkeypatch.setattr(deploy.fleet.Fleet, "add", lambda self, count: seen.append(("add", count)) or 0)
    monkeypatch.setattr(deploy.fleet.Fleet, "down", lambda self, only: seen.append(("down", only)) or 0)
    hooks = SimpleNamespace()
    for argv in (add[4:], retire[-1][4:]):
        assert deploy.fleet.main(list(argv), {}, hooks) == 0
    assert seen == [("add", 2), ("down", "fed-3")]
