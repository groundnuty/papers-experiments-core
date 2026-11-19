"""C43: the elastic fleet's controller (deploy/hpc-federation/fleetcontrol.py) on simulated sites over a fake Slurm: its
read of a site is read-only and fails whole rather than guess, and its loop joins, drains, retires (collect first,
never with a lease in flight) and replaces federations through the fleet's own commands; a dry run prints exactly
what a live tick runs, and runs nothing."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from control_site_support import FakeSite, Job, observed
from elastic_support import FINGERPRINT, FakeClock, config, plan
from hpc_fleet_support import fleet_modules

from expcore.campaign.elastic import Lease, Ledger, Wait
from expcore.campaign.elastic.control import Add, Drain, Retire, SiteSettings, read_only

DIGEST = "c" * 64
IDENTITY = {"core_commit": "0123abcd", "python": "3.12.7"}  # an environment identity as a lease store pins it
IDENTITY_DIGEST = "78bd403ced04f551ce0683afa7aedfd2d6d5df93e9375054cef49f4b54a94ffe"


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

    def hooks(self, identity=None, spend=None):
        sites = self.sites

        class Transport:
            def run(self, config_, argv, timeout):
                return sites[config_.label].transport().run(config_, argv, timeout)

        return self.deploy.fleetcontrol.Hooks(
            runner=lambda: None,
            transport=lambda environ: Transport(),
            clock=self.clock,
            sleep=lambda s: None,
            identity=identity or (lambda site, member: (FINGERPRINT, IDENTITY)),
            say=self.printed.append,
            spend=spend or (lambda site, action: None),
        )

    def tick(self, *extra: str, identity=None, spend=None) -> int:
        argv = ["control", "--ledger", str(self.ledger), "--sites", str(self.file()), "--once", *extra]
        return self.deploy.fleetcontrol.main(argv, {}, self.hooks(identity, spend))

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


def test_c54_observe_says_which_member_a_down_could_not_sweep_and_the_controller_takes_it_down_no_more(
    deploy, tmp_path, site
):
    """C54: a member whose down could not sweep a node of it (its kept state records `unswept`) reads `unswept`, so
    the controller retires it no more and leaves it to an operator's explicit down; one whose down failed otherwise
    reads nothing of it and is taken down again."""
    for label in ("fed-1", "fed-2"):
        site.add_member(label, status="down-failed", phase="down")
    root = site.deploy.fleetrecord.root_of(site.fleet, "fed-1")
    state = site.deploy.hpcstate.load(root)
    site.deploy.hpcstate.save(root, {**state, "unswept": ["the agent's node"]})
    members = {m["label"]: m for m in observed(site)["members"]}
    assert members["fed-1"]["unswept"] is True
    assert "unswept" not in members["fed-2"]
    bench = Bench(tmp_path, deploy, [site])
    assert bench.tick() == 0
    downs = [c for c in site.commands if "down" in c]
    assert [c[c.index("--member") + 1] for c in downs] == ["fed-2"]


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
    site.jobs["41"].state = site.jobs["42"].state = ended  # both held jobs: one alone is BROKEN, not proven
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


# ------------------------------------------------------------------------------------------------ the additions
def test_c43_a_burst_of_lost_priced_leases_blocks_dispatch_until_the_controller_settles_them(deploy, tmp_path, site):
    """C43: three priced leases whose federations die are lost, each still charged its USD 10 grant, which locks the
    whole USD 30 budget, so a fresh federation is told to wait; the controller settles each to the spend its call log
    shows (USD 0.50, from the root), the lock is shown going in its status line and log, and dispatch resumes."""
    for n in (1, 2, 3):
        up(site, f"fed-{n}", 40 + 2 * n)
    up(site, "fed-4", 60)
    bench = Bench(tmp_path, deploy, [site], runs=plan(tasks=3), max_usd=30.0, grants_usd={"m1": 10.0})
    bench.tick()
    ledger = bench.open()
    leases = [ledger.issue(f"site-a-fed-{n}") for n in (1, 2, 3)]
    for lease in leases:
        ledger.mark_sent(lease.lease_id)
    ledger.close()
    for n in (1, 2, 3):
        site.jobs[str(40 + 2 * n)].state = site.jobs[str(41 + 2 * n)].state = "NODE_FAIL"
    bench.clock.advance(60)
    bench.tick()  # each job's end proven terminal: three lost leases, their grants locked
    ledger = bench.open()
    assert ledger.status().locked_micro == 30_000_000
    assert isinstance(ledger.issue("site-a-fed-4"), Wait)
    ledger.close()
    assert "locked in unsettled leases 30.000000 USD" in bench.printed[-1]
    read = []

    def spend(config, action):
        read.append((action.lease_id, action.root))
        return 500_000, "root", 1

    bench.clock.advance(60)
    bench.tick(spend=spend)
    assert sorted(lease_id for lease_id, _ in read) == sorted(lease.lease_id for lease in leases)
    assert all(root.endswith("/root") for _, root in read)
    ledger = bench.open()
    assert ledger.status().locked_micro == 0
    assert isinstance(ledger.issue("site-a-fed-4"), Lease)
    ledger.close()
    tick = bench.lines("tick")[-1]
    assert tick["money"]["locked_micro"] == 30_000_000  # read before this tick's settlements
    settled = [r for r in bench.lines("result") if r.get("action") == "Settle"]
    assert len(settled) == 3
    assert all(r["ok"] and r["source"] == "root" for r in settled)


def test_c43_a_lost_lease_whose_log_cannot_be_read_stays_at_its_grant_said_once(deploy, tmp_path, site):
    """C43: with no reader of the dead store's spend (step 6 hooks it up), the lease stays charged its grant and the
    loop says so once, not every tick."""
    up(site, "fed-1", 41)
    bench = Bench(tmp_path, deploy, [site], max_usd=30.0, grants_usd={"m1": 10.0})
    bench.tick()
    ledger = bench.open()
    ledger.mark_sent(ledger.issue("site-a-fed-1").lease_id)
    ledger.close()
    site.jobs["41"].state = site.jobs["42"].state = "NODE_FAIL"

    class Stop(Exception):
        pass

    ticks = []

    def sleep(seconds):
        ticks.append(seconds)
        bench.clock.advance(seconds)
        if len(ticks) == 4:
            raise Stop

    hooks = bench.hooks()
    hooks.sleep = sleep
    with pytest.raises(Stop):
        bench.deploy.fleetcontrol.main(
            ["control", "--ledger", str(bench.ledger), "--sites", str(bench.file())], {}, hooks
        )
    unread = [r for r in bench.lines("result") if r.get("action") == "Settle"]
    assert len(unread) == 1
    assert "could not be read" in unread[0]["said"]
    ledger = bench.open()
    assert ledger.status().locked_micro == 10_000_000
    ledger.close()


def test_c43_an_add_is_refused_at_the_login_memory_cap_read_from_the_relays_beating_here(deploy, tmp_path, site):
    """C43: two relays beating on this login node and one on another: under a cap that leaves less than one more
    federation's share (its up, then its relay), nothing is added and the budget is logged beside quota and nodes."""
    for n, where in ((1, "here"), (2, "here"), (3, "elsewhere")):
        site.add_member(f"fed-{n}", jobs={"allocation": (str(40 + n), running(site))}, relay=where)
    bench = Bench(tmp_path, deploy, [site])
    used = 512 + 2 * 40
    bench.settings = {"site-a": {"login_memory_mb": used + 295.0}}
    bench.tick()
    assert "add" not in verbs(site)
    limits = bench.lines("tick")[-1]["targets"]["site-a"]["limits"]
    assert (limits["login_memory"], limits["login_memory_mb"]["relays"]) == (0, 2)
    assert {"inodes", "nodes", "login_memory"} <= set(limits)
    bench.settings = {"site-a": {"login_memory_mb": used + 296.0}}
    bench.clock.advance(60)
    bench.tick()
    assert site.commands[-1][-2:] == ["add", "1"]


def test_c43_a_dead_relay_is_detected_recorded_with_its_last_heartbeat_and_its_federation_drained(
    deploy, tmp_path, site
):
    """C43: a joined federation's relay stops beating (its heartbeat 300 s old): the tick records it gone with no exit
    status and its last heartbeat, and drains the federation in the ledger."""
    site.add_member("fed-1", jobs={"allocation": ("41", running(site))}, relay="here")
    bench = Bench(tmp_path, deploy, [site])
    bench.tick()
    assert federations(bench)["site-a-fed-1"].drained_at is None
    beat = site.fleet / "fed-1" / "root" / "fault-relay" / "heartbeat.json"
    record = json.loads(beat.read_text(encoding="utf-8"))
    site.now += 300
    bench.clock.advance(300)
    bench.tick()
    assert federations(bench)["site-a-fed-1"].drained_at is not None
    tick = bench.lines("tick")[-1]
    assert tick["relays"]["site-a"] == [
        {"member": "fed-1", "relay": "gone, no exit status", "last_beat_at": record["wall"]}
    ]
    (drain,) = [a for a in tick["actions"] if a["action"] == "Drain"]
    assert drain["reason"].startswith("relay gone, no exit status")
    assert "1 relay(s) gone" in bench.printed[-1]


# ------------------------------------------------------------------------------------------------ review round 1
@pytest.mark.parametrize("forged", ["root", "job"])
def test_c43_a_member_whose_root_is_not_the_one_its_up_made_darkens_the_site(deploy, tmp_path, site, forged):
    """C43: a state file written by an up of another root, or naming another job than the fleet recorded, is not this
    fleet's federation: the read fails whole (nothing of it is read as ours) and the site gets no action."""
    up(site, "fed-1", 41)
    path = site.fleet / "fed-1" / "root" / "hpc-federation.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    if forged == "root":
        state["command"][state["command"].index("--root") + 1] = "/elsewhere/fed-1/root"
    else:
        state["allocation"]["job"] = "202"
        site.jobs["202"] = running(site)
    path.write_text(json.dumps(state), encoding="utf-8")
    bench = Bench(tmp_path, deploy, [site])
    bench.tick()
    assert site.commands == []
    assert "dark" in bench.lines("tick")[-1]["targets"]["site-a"]
    assert not any(call[:4] == ["scontrol", "show", "job", "-o"] and call[4] == "202" for call in site.calls)


def test_c43_a_member_is_verified_only_by_markers_bound_to_its_recorded_manifest(deploy, site):
    """C43: an older state with no record of its ingested spaces, or a marker for another manifest, is not verified,
    so its inodes stay reserved."""
    up(site, "fed-1", 41)
    path = site.fleet / "fed-1" / "root" / "hpc-federation.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    assert observed(site)["members"][0]["verified"] is True
    other = dict(state, data={"ingested": {"delta": {**state["data"]["ingested"]["delta"],
                                                     "verified": {"manifest": "e" * 64, "files": 1, "bytes": 1,
                                                                  "space_id": "space-one"}}}})  # fmt: skip
    for changed in ({k: v for k, v in state.items() if k != "data"}, other):
        path.write_text(json.dumps(changed), encoding="utf-8")
        assert observed(site)["members"][0]["verified"] is False


def test_c43_the_busy_operations_are_taken_before_the_sites_are_read(deploy, tmp_path, site):
    """C43: an add that ends while the sites are read is still busy for that tick, so the read taken before it ended
    never lets the same room be spent twice."""
    bench = Bench(tmp_path, deploy, [site])
    order = []

    class Executor:
        def busy(self):
            order.append("busy")
            return frozenset({"site-a"})

        def run(self, decision):
            order.append("run")

    class Transport:
        def run(self, config_, argv, timeout):
            order.append("read")
            return site.transport().run(config_, argv, timeout)

    controller, sites = deploy.fleetcontrol.load_config(bench.file())
    log = deploy.fleetexecute.Log(None)
    views = read_only(bench.ledger)
    try:
        decision = deploy.fleetcontrol.tick(views, bench.ledger, controller, sites, {}, Transport(), Executor(), log,
                                            bench.hooks())  # fmt: skip
    finally:
        views.close()
    assert order == ["busy", "read", "run"]
    assert [type(a).__name__ for a in decision.actions] == []


def test_c43_two_sites_never_share_one_fleet(deploy, tmp_path, site):
    """C43: two site labels naming one fleet directory would read and act on its members twice: refused."""
    bench = Bench(tmp_path, deploy, [site, site])
    assert bench.tick() == 2
    assert site.commands == []


def test_c43_a_drain_decided_before_the_end_is_not_run_after_it_nor_by_hand(deploy, tmp_path, site):
    """C43: a drain whose federation's recorded end has passed by the time it runs is refused and logged, and the
    `drain` verb refuses one past its end too."""
    up(site, "fed-1", 41, ends_in=3000)
    bench = Bench(tmp_path, deploy, [site])
    bench.tick()
    bench.clock.advance(3000)
    ledger = bench.open()
    log = deploy.fleetexecute.Log(None)
    executor = deploy.fleetexecute.Executor(ledger, bench.ledger, {}, None, tmp_path / "evidence", log,
                                            clock=bench.clock)  # fmt: skip
    executor.in_ledger(Drain("site-a", "fed-1", "site-a-fed-1", "decided a moment before its end"))
    assert ledger.federations()[0].drained_at is None
    ledger.close()
    assert "past its recorded job end" in log.lines[-1]["said"]
    argv = ["drain", "--ledger", str(bench.ledger), "--label", "site-a-fed-1"]
    assert deploy.fleetcontrol.main(argv, {}, bench.hooks()) == 2
    assert federations(bench)["site-a-fed-1"].drained_at is None


def test_c43_a_site_whose_read_times_out_is_dark_and_the_loop_goes_on(deploy, tmp_path, site):
    """C43: a read that times out (the local transport's subprocess timeout) darkens that site for the tick; the other
    site is still read and acted on."""
    other = FakeSite(tmp_path / "site-b", deploy)
    bench = Bench(tmp_path, deploy, [site, other])
    real = bench.hooks

    def hooks(identity=None, spend=None):
        found = real(identity, spend)
        inner = found.transport({})

        class Transport:
            def run(self, config_, argv, timeout):
                if config_.label == "site-a":
                    raise subprocess.TimeoutExpired(argv, timeout)
                return inner.run(config_, argv, timeout)

        found.transport = lambda environ: Transport()
        return found

    bench.hooks = hooks
    assert bench.tick() == 0
    tick = bench.lines("tick")[-1]
    assert "TimeoutExpired" in tick["targets"]["site-a"]["dark"]
    assert verbs(other) == ["add"]


# ------------------------------------------------------------------------------------------------ review round 2
def test_c43_a_relay_whose_start_tick_is_text_as_the_relay_writes_it_is_read_here(deploy, site):
    """C43: the relay records its /proc start tick as text; a live local relay so written is `here`, and counted."""
    site.add_member("fed-1", jobs={"allocation": ("41", running(site))}, relay="here")
    path = site.fleet / "fed-1" / "root" / "fault-relay" / "heartbeat.json"
    beat = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**beat, "start": str(beat["start"])}), encoding="utf-8")
    assert observed(site)["members"][0]["relay"] == "here"


def test_c43_a_state_file_replaced_after_the_identity_check_is_not_read_as_ours(deploy, site, monkeypatch):
    """C43: the state read for the jobs is checked as the identity check read its own: one replaced in between by
    another federation's (another job) fails the read whole."""
    up(site, "fed-1", 41)
    real = deploy.hpcstate.load
    reads = []

    def load(root):
        found = real(root)
        reads.append(root)
        if found is not None and len(reads) == 2:
            found["allocation"]["job"] = "202"
        return found

    monkeypatch.setattr(deploy.hpcstate, "load", load)
    code, _out, err = site.transport().run(None, ["python", "-I", "-B", "fleetcontrol.py", "observe", "--fleet",
                                                 str(site.fleet)], 60)  # fmt: skip
    assert code == 2
    assert "not the federation this fleet made" in err


def test_c43_one_ended_job_beside_a_running_one_is_broken_not_ended(deploy, site):
    """C43: an ended agent's job beside running services, or a job still completing, reads BROKEN: not proven ended."""
    site.add_member("fed-1", jobs={"allocation": ("41", running(site)), "sandbox": ("42", Job("TIMEOUT", site.now))})
    site.add_member("fed-2", jobs={"allocation": ("43", Job("COMPLETING", site.now - 99, site.now - 9))})
    states = {m["label"]: m["state"] for m in observed(site)["members"]}
    assert states == {"fed-1": "BROKEN", "fed-2": "BROKEN"}


def test_c43_a_fleet_named_twice_through_a_link_is_refused(deploy, tmp_path, site):
    """C43: a link to a fleet is the same fleet: two sites naming it either way are refused."""
    alias = tmp_path / "alias"
    alias.symlink_to(site.fleet)
    bench = Bench(tmp_path, deploy, [site])
    path = bench.file()
    text = path.read_text(encoding="utf-8")
    path.write_text(text + text[text.index("[[site]]") :].replace('"site-a"', '"site-b"').replace(
        str(site.fleet), str(alias)), encoding="utf-8")  # fmt: skip
    argv = ["control", "--ledger", str(bench.ledger), "--sites", str(path), "--once"]
    assert deploy.fleetcontrol.main(argv, {}, bench.hooks()) == 2


def test_c43_a_spend_reader_that_fails_leaves_the_lease_at_its_grant_and_the_tick_goes_on(deploy, tmp_path, site):
    """C43: a reader whose evidence cannot be read (an OSError) is no spend: the lease stays charged its grant."""
    up(site, "fed-1", 41)
    bench = Bench(tmp_path, deploy, [site], max_usd=30.0, grants_usd={"m1": 10.0})
    bench.tick()
    ledger = bench.open()
    ledger.mark_sent(ledger.issue("site-a-fed-1").lease_id)
    ledger.close()
    site.jobs["41"].state = site.jobs["42"].state = "NODE_FAIL"
    bench.clock.advance(60)
    bench.tick()

    def spend(config, action):
        raise OSError("the call log is gone")

    bench.clock.advance(60)
    assert bench.tick(spend=spend) == 0
    ledger = bench.open()
    assert ledger.status().locked_micro == 10_000_000
    ledger.close()


# ------------------------------------------------------------------------------------------------ step 4's contract
def test_c43_a_federation_joins_with_the_digest_of_its_environment_identity_by_loop_and_by_hand(deploy, tmp_path, site):
    """C43: the ledger's environment is the SHA-256 of the identity's canonical JSON (step 4's `environment_digest`),
    whether the loop joins a federation or an operator does with `join --environment-file`."""
    up(site, "fed-1", 41)
    bench = Bench(tmp_path, deploy, [site])
    bench.tick()
    ledger = bench.open()
    pinned = ledger.db.execute("SELECT value FROM meta WHERE key='environment'").fetchone()[0]
    ledger.close()
    assert pinned == IDENTITY_DIGEST
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"python": "3.12.7", "core_commit": "0123abcd"}), encoding="utf-8")
    argv = ["join", "--ledger", str(bench.ledger), "--label", "site-a-fed-9", "--site", "site-a", "--jobs", "91",
            "--job-end", str(site.now + 9000), "--kill-wait", "180", "--fingerprint", FINGERPRINT,
            "--environment-file", str(path)]  # fmt: skip
    assert deploy.fleetcontrol.main(argv, {}, bench.hooks()) == 0  # refused if its digest differed from the pinned
    path.write_text(json.dumps({"python": "3.13.0", "core_commit": "0123abcd"}), encoding="utf-8")
    argv[argv.index("site-a-fed-9")] = "site-a-fed-10"
    assert deploy.fleetcontrol.main(argv, {}, bench.hooks()) == 2


def test_c50_a_campaign_that_declares_its_platforms_joins_the_one_read_and_refuses_another_by_hand(
    deploy, tmp_path, site
):
    """C50: the loop joins a federation on the platform its identity read gives, which the ledger keeps; by hand, a
    `join --platform` the campaign does not declare is refused (exit 2) and one it declares joins."""
    declared, other = "d" * 64, "e" * 64
    up(site, "fed-1", 41)
    bench = Bench(tmp_path, deploy, [site], platforms=(declared,))
    bench.tick(identity=lambda s, m: (FINGERPRINT, IDENTITY, declared))
    ledger = bench.open()
    joined = dict(ledger.db.execute("SELECT label, platform FROM federation_platforms").fetchall())
    ledger.close()
    assert list(joined.values()) == [declared]
    path = tmp_path / "identity.json"
    path.write_text(json.dumps(IDENTITY), encoding="utf-8")
    argv = ["join", "--ledger", str(bench.ledger), "--label", "site-a-fed-9", "--site", "site-a", "--jobs", "91",
            "--job-end", str(site.now + 9000), "--kill-wait", "180", "--fingerprint", FINGERPRINT,
            "--environment-file", str(path), "--platform", other]  # fmt: skip
    assert deploy.fleetcontrol.main(argv, {}, bench.hooks()) == 2
    argv[-1] = declared
    assert deploy.fleetcontrol.main(argv, {}, bench.hooks()) == 0


def test_c43_the_dead_store_reader_settles_only_from_a_read_of_that_very_lease(deploy, tmp_path):
    """C43: over step 4's reader of a dead store, a read that is None, of another lease, or of a store nobody located
    settles nothing; one of the lease gives its spend, its source and its charged attempts."""
    from types import SimpleNamespace as Read

    settle = SimpleNamespace(lease_id=11, site="site-a", member="fed-1")
    store = tmp_path / "store-out"
    answers = {"none": None, "other": Read(lease_id=12, spend_micro=5, charged_attempts=1)}
    answers["ours"] = Read(lease_id=11, spend_micro=250_000, charged_attempts=2, settled_running=0)
    seen = []

    def read(out, study):
        seen.append((out, study))
        return answers[study]

    def located(site, action):
        return store, "pulled"

    for study, expected in (("none", None), ("other", None), ("ours", (250_000, "pulled", 2))):
        assert deploy.fleetexecute.dead_store_reader(located, study, read)(None, settle) == expected
    assert deploy.fleetexecute.dead_store_reader(lambda site, action: None, "ours", read)(None, settle) is None
    assert seen == [(store, "none"), (store, "other"), (store, "ours")]


def test_c43_a_none_read_is_retried_each_tick_and_a_settle_always_carries_its_charged_attempts(deploy, tmp_path, site):
    """C43: the dead store's read gives None while its run has not ended (the lease stays at its grant); the next tick
    reads again, and a read without its charged attempts settles nothing; then one settles with the attempts shown."""
    up(site, "fed-1", 41)
    bench = Bench(tmp_path, deploy, [site], max_usd=30.0, grants_usd={"m1": 10.0})
    bench.tick()
    ledger = bench.open()
    lease = ledger.issue("site-a-fed-1")
    ledger.mark_sent(lease.lease_id)
    ledger.close()
    site.jobs["41"].state = site.jobs["42"].state = "NODE_FAIL"
    bench.clock.advance(60)
    bench.tick()
    reads = iter([None, (300_000, "pulled", None), (300_000, "pulled", 2)])

    class Stop(Exception):
        pass

    ticks = []

    def sleep(seconds):
        ticks.append(seconds)
        bench.clock.advance(seconds)
        if len(ticks) == 3:
            raise Stop

    hooks = bench.hooks(spend=lambda config, action: next(reads))
    hooks.sleep = sleep
    with pytest.raises(Stop):
        bench.deploy.fleetcontrol.main(
            ["control", "--ledger", str(bench.ledger), "--sites", str(bench.file())], {}, hooks
        )
    ledger = bench.open()
    (settled,) = ledger.settlements()
    assert (settled.lease_id, settled.spend_micro, settled.source, settled.charged) == (
        lease.lease_id,
        300_000,
        "pulled",
        2,
    )
    ledger.close()
    said = [r["said"] for r in bench.lines("result") if r.get("action") == "Settle" and not r["ok"]]
    assert said == [
        "its call log could not be read: it stays charged its grant",
        "its read gave no charged attempts: not settled",
    ]


def test_c44_the_controller_stops_gracefully_on_sigterm_and_on_a_sigint_a_background_shell_ignored(tmp_path):
    """fleetcontrol.stop_on_signals: SIGTERM, and SIGINT even when the process started with it ignored (a background
    job of a non-interactive shell), both raise KeyboardInterrupt, so a `finally` runs and nothing is left half done."""
    import os
    import signal
    import subprocess
    import sys
    import time

    from hpc_fed_support import HPC

    code = "\n".join(
        [
            "import sys, time",
            f"sys.path.insert(0, {str(HPC)!r})",
            "import fleetcontrol",
            "fleetcontrol.stop_on_signals()",
            "try:",
            "    print('ready', flush=True)",
            "    time.sleep(60)",
            "except KeyboardInterrupt as stop:",
            "    print('stopped', stop.args[0] if stop.args else 'SIGINT', flush=True)",
            "finally:",
            "    print('finally ran', flush=True)",
        ]
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    for sent in (signal.SIGTERM, signal.SIGINT):
        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=tmp_path,
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),  # as a background job starts
        )
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(0.2)
        proc.send_signal(sent)
        out, err = proc.communicate(timeout=30)
        assert f"stopped {sent.name}" in out, (out, err)
        assert "finally ran" in out
