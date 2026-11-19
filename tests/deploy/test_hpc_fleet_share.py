"""C53: several controllers, one per ledger, on simulated sites over a fake Slurm: a site's maximum shared through one
file is never passed by them together, a controller that defers to another ledger adds nothing while that ledger has
runs waiting for capacity (or cannot be read) and adds once it has none, and shared caps hold the target too."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from control_site_support import FakeSite, Job
from elastic_support import ENVIRONMENT, FINGERPRINT, FakeClock, config, plan
from hpc_fleet_support import fleet_modules

from expcore.campaign.elastic import Lease, Ledger
from expcore.campaign.elastic.control import HOLDS_NOTHING

IDENTITY = {"core_commit": "0123abcd", "python": "3.12.7"}


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetcontrol
        import fleetrecord
        import fleetshare
        import hpcstate

        yield SimpleNamespace(fleet=found.fleet, fleetcontrol=fleetcontrol, fleetrecord=fleetrecord,
                              fleetshare=fleetshare, hpcstate=hpcstate)  # fmt: skip


def adding(site: FakeSite, base: int):
    """The site's `fleet.py add K`: K members come up at once, each holding two running jobs of its own."""
    made = {"next": base}

    def on_command(words: list) -> None:
        at = words.index("--fleet")
        if words[at + 2] != "add":
            return
        for _ in range(int(words[at + 3])):
            k = made["next"]
            made["next"] += 2
            started = site.now - 600
            job = {"state": "RUNNING", "submitted": started - 50, "started": started, "ends": site.now + 4 * 3600}
            label = f"fed-{len(site.deploy.fleetrecord.Record(site.fleet).read()['members']) + 1}"
            site.add_member(label, jobs={"allocation": (str(k), Job(**job)), "sandbox": (str(k + 1), Job(**job))})

    return on_command


class Controller:
    """One controller: its ledger (in a directory of its own: one dispatch lock each), its site, its sites file."""

    def __init__(self, tmp: Path, deploy, name: str, clock: FakeClock, base: int, **controller) -> None:
        self.deploy, self.tmp, self.clock = deploy, tmp / name, clock
        self.site = FakeSite(self.tmp / "site", deploy, now=clock())
        self.site.on_command = adding(self.site, base)
        self.ledger = self.tmp / "ledger" / "ledger.sqlite"
        Ledger.create(self.ledger, plan(tasks=2), config(), clock=clock).close()
        self.log = self.tmp / "control.jsonl"
        self.controller, self.site_fields, self.printed = controller, {}, []

    def file(self) -> Path:
        lines = ["[controller]", f'evidence = "{self.tmp / "evidence"}"', f'log = "{self.log}"']
        lines += [f"{key} = {json.dumps(value)}" for key, value in self.controller.items() if key != "shared_caps"]
        if "shared_caps" in self.controller:
            lines.append("shared_caps = { " + ", ".join(f"{m} = {n}" for m, n in self.controller["shared_caps"].items())
                         + " }")  # fmt: skip
        fields = {"max_federations": 3, "allowance_units": 1e12, "rate_units_per_hour": 47.0, "parallel_ups": 4,
                  "lease_bound_s": 1800.0, **self.site_fields}  # fmt: skip
        lines += ["[[site]]", 'label = "site-a"', f'fleet = "{self.site.fleet}"']
        lines += [f"{key} = {json.dumps(value)}" for key, value in fields.items()]
        path = self.tmp / "sites.toml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def tick(self, *extra: str) -> int:
        site = self.site

        class Transport:
            def run(self, config_, argv, timeout):
                return site.transport().run(config_, argv, timeout)

        hooks = self.deploy.fleetcontrol.Hooks(runner=lambda: None, transport=lambda environ: Transport(),
                                               clock=self.clock, sleep=lambda s: None,
                                               identity=lambda s, m: (FINGERPRINT, IDENTITY),
                                               say=self.printed.append, spend=lambda s, a: None)  # fmt: skip
        argv = ["control", "--ledger", str(self.ledger), "--sites", str(self.file()), "--once", *extra]
        return self.deploy.fleetcontrol.main(argv, {}, hooks)

    def live(self) -> int:
        members = self.deploy.fleetrecord.Record(self.site.fleet).read()["members"]
        return sum(1 for m in members if m["status"] not in HOLDS_NOTHING)

    def ticks(self) -> list:
        return [e for e in map(json.loads, self.log.read_text().splitlines()) if e["kind"] == "tick"]

    def adds(self) -> int:
        return sum(1 for e in self.ticks() for a in e["actions"] if a["action"] == "Add")


def test_c53_two_controllers_sharing_a_site_file_never_pass_its_maximum_together(deploy, tmp_path):
    """C53: two controllers, each its own ledger and fleet at one site whose `max_federations = 3` is shared through one
    file, each wanting four federations: whoever ticks first adds within the total, the other only within what is
    left, and over many ticks the two together never hold more than 3 members; the file holds both entries."""
    clock = FakeClock(1_800_000_000.0)
    shared = tmp_path / "shared-site.json"
    a = Controller(tmp_path, deploy, "a", clock, 100)
    b = Controller(tmp_path, deploy, "b", clock, 500)
    a.site_fields = {"shared_site_file": str(shared), "parallel_ups": 2}
    b.site_fields = {"shared_site_file": str(shared)}
    for _ in range(6):
        for one in (a, b):
            assert one.tick() == 0
            assert a.live() + b.live() <= 3
            clock.advance(60)
            a.site.now = b.site.now = clock()
    assert (a.live(), b.live()) == (2, 1)
    holders = json.loads(shared.read_text())["holders"]
    assert sorted(entry["members"] for entry in holders.values()) == [1, 2]
    last = b.ticks()[-1]["shared_sites"]["site-a"]
    assert (last["others_members"], last["max_federations"]) == (2, 1)


def test_c53_an_unreadable_shared_site_file_leaves_the_site_no_room(deploy, tmp_path):
    """C53: a shared site file that is not one of its format is no room: no add, and the reason is logged."""
    clock = FakeClock(1_800_000_000.0)
    shared = tmp_path / "shared-site.json"
    shared.write_text('{"format": "something else"}\n', encoding="utf-8")
    a = Controller(tmp_path, deploy, "a", clock, 100)
    a.site_fields = {"shared_site_file": str(shared)}
    assert a.tick() == 0
    assert a.adds() == 0
    assert "not a file of" in a.ticks()[-1]["shared_sites"]["site-a"]["error"]


def join_all(ledger_path: Path, clock: FakeClock, count: int) -> None:
    ledger = Ledger.open(ledger_path, clock=clock)
    try:
        for k in range(count):
            ledger.join(f"core-fed-{k}", site="site-a", job_ids=(f"job-{k}",), job_end_at=clock() + 86_400,
                        fingerprint=FINGERPRINT, environment=ENVIRONMENT, kill_wait_s=180.0)  # fmt: skip
    finally:
        ledger.close()


def test_c53_a_controller_deferring_to_the_core_adds_nothing_while_it_waits_and_adds_once_it_does_not(deploy, tmp_path):
    """C53: the optional ledger's controller defers to the core's ledger. While the core has four runs to lease and no
    federation, the optional one adds nothing (the add is logged as held back) but still joins its own member that is
    up; an unreadable core is deferred to as well; once the core has a free federation for each run it wants, the
    optional one adds."""
    clock = FakeClock(1_800_000_000.0)
    core = Controller(tmp_path, deploy, "core", clock, 100)
    optional = Controller(tmp_path, deploy, "optional", clock, 500, defer_to=[str(core.ledger)])
    started = clock() - 600
    job = {"state": "RUNNING", "submitted": started - 50, "started": started, "ends": clock() + 4 * 3600}
    optional.site.add_member("fed-1", jobs={"allocation": ("900", Job(**job)), "sandbox": ("901", Job(**job))})
    assert optional.tick() == 0
    tick = optional.ticks()[-1]
    assert tick["defer"]["deferred"] is True
    assert tick["defer"]["ledgers"][str(core.ledger)]["waiting"] == 4
    assert [a["action"] for a in tick["actions"]] == ["Join"]
    assert [held["action"] for held in tick["defer"]["held_back"]] == ["Add"]
    assert optional.live() == 1
    moved = core.ledger.with_name("moved.sqlite")
    core.ledger.rename(moved)
    assert optional.tick() == 0
    assert "unreadable" in optional.ticks()[-1]["defer"]["ledgers"][str(core.ledger)]
    assert optional.adds() == 0
    moved.rename(core.ledger)
    join_all(core.ledger, clock, 4)
    assert optional.tick() == 0
    tick = optional.ticks()[-1]
    assert tick["defer"]["deferred"] is False
    assert [a["action"] for a in tick["actions"]] == ["Add"]
    assert optional.live() > 1


def test_c53_shared_caps_hold_the_target_and_issue_nothing_while_a_sibling_holds_the_cap(deploy, tmp_path):
    """C53: with `shared_caps = {m1 = 2}` and a sibling ledger holding both slots of m1, the controller's target wants
    no lease (a dry run's tick says 0 wanted, not 4), and its ledger issues none of m1."""
    clock = FakeClock(1_800_000_000.0)
    sibling = Controller(tmp_path, deploy, "sibling", clock, 100)
    join_all(sibling.ledger, clock, 2)
    held = Ledger.open(sibling.ledger, clock=clock)
    try:
        for k in range(2):
            assert isinstance(held.issue(f"core-fed-{k}"), Lease)
    finally:
        held.close()
    one = Controller(tmp_path, deploy, "one", clock, 500, archives=str(tmp_path / "archives"),
                     siblings=[str(sibling.ledger)], shared_caps={"m1": 2})  # fmt: skip
    one.site_fields = {"channel": str(tmp_path / "channel")}
    assert one.tick("--dry-run") == 0
    tick = one.ticks()[-1]
    assert tick["wanted"] == 0
    assert tick["shared_caps"]["limit"] == {"m1": 0}
    ledger = deploy.fleetshare.open_ledger(one.ledger, clock, deploy.fleetcontrol.load_config(one.file())[0].sharing)
    try:
        ledger.join("fed-9", site="site-a", job_ids=("job-9",), job_end_at=clock() + 86_400,
                    fingerprint=FINGERPRINT, environment=ENVIRONMENT, kill_wait_s=180.0)  # fmt: skip
        assert not isinstance(ledger.issue("fed-9"), Lease)
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("controller", "said"),
    [
        ({"siblings": ["/elsewhere/ledger.sqlite"]}, "siblings and shared_caps go together"),
        ({"siblings": ["/elsewhere/ledger.sqlite"], "shared_caps": {"m1": 2}}, "shared_caps needs archives"),
        ({"defer_to": ["relative/ledger.sqlite"]}, "absolute path"),
    ],
)
def test_c53_the_sharing_keys_are_checked(deploy, tmp_path, controller, said):
    """C53: siblings without shared caps, shared caps with no dispatcher here, and a relative ledger are refused."""
    one = Controller(tmp_path, deploy, "one", FakeClock(1_800_000_000.0), 100, **controller)
    with pytest.raises(deploy.fleetcontrol.ConfigError, match=said):
        deploy.fleetcontrol.load_config(one.file())


def test_c53_a_controller_never_defers_to_its_own_ledger(deploy, tmp_path):
    """C53: its own ledger among the ledgers it defers to is refused before anything runs."""
    clock = FakeClock(1_800_000_000.0)
    one = Controller(tmp_path, deploy, "one", clock, 100)
    one.controller = {"defer_to": [str(one.ledger)]}
    assert one.tick() == 2
    assert not one.log.exists()


def test_c53_a_lone_controller_logs_no_sharing(deploy, tmp_path):
    """C53: a controller with none of the new keys opens a plain ledger and logs exactly the keys it logged before."""
    clock = FakeClock(1_800_000_000.0)
    one = Controller(tmp_path, deploy, "one", clock, 100)
    assert one.tick() == 0
    assert deploy.fleetcontrol.load_config(one.file())[0].sharing is None
    assert set(one.ticks()[-1]) == {"kind", "at", "dry_run", "wanted", "targets", "waits", "relays", "money",
                                    "actions"}  # fmt: skip
