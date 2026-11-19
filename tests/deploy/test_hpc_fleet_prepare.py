"""C49: the elastic fleet's prepare hook (deploy/hpc-federation/fleetprepare.py, fleetcontrol.py): a federation the
controller adds gets the study's ingest and verify-data run for it, and joins only once it reads verified. The site's
wrapper and the study's command here are real (short commands on a fake site); the transport, Slurm and the quota are
the fake site's."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from control_site_support import FakeSite, Job
from elastic_support import FINGERPRINT, FakeClock, config, plan
from hpc_fleet_support import SEPARATE_LAYOUT, fleet_modules

from expcore.campaign.elastic import Ledger
from expcore.campaign.elastic.control import Member, SiteSettings, SiteView, decide, ledger_view, prepare_key, read_only

DIGEST = "c" * 64
IDENTITY = {"core_commit": "0123abcd", "python": "3.12.7"}
CHATTER = "ingest-chatter-never-logged"
# The study's command stand-in: waits, optionally leaves the verified marker `verify-data` leaves, says something,
# exits with its code. argv: root, delay, exit code, `mark` or `bare`, the layout, a word with braces of its own.
STUDY = """
import json, os, sys, time
root, delay, code, mark, layout, kept = sys.argv[1:7]
time.sleep(float(delay))
path = os.path.join(root, "hpc-federation.json")
if mark == "mark":
    state = json.load(open(path))
    space = state["data"]["ingested"]["delta"]
    space["verified"] = {"manifest": space["manifest"], "files": 1, "bytes": 1, "space_id": space["space_id"]}
    json.dump(state, open(path + ".new", "w"))
    os.replace(path + ".new", path)
print("CHATTER", layout, kept)
sys.exit(int(code))
""".replace("CHATTER", CHATTER)
HANG = """
import subprocess, sys, time
grandchild = subprocess.Popen(["sleep", "60"])
open(sys.argv[1], "w").write(str(grandchild.pid))
time.sleep(60)
"""


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetcontrol
        import fleetexecute
        import fleetobserve
        import fleetprepare
        import fleetrecord
        import hpcslurm
        import hpcstate

        yield SimpleNamespace(fleet=found.fleet, fleetcontrol=fleetcontrol, fleetexecute=fleetexecute,
                              fleetobserve=fleetobserve, fleetprepare=fleetprepare, fleetrecord=fleetrecord,
                              hpcslurm=hpcslurm, hpcstate=hpcstate)  # fmt: skip


@pytest.fixture
def site(deploy, tmp_path):
    return FakeSite(tmp_path / "site-a", deploy)


def study(delay: float = 0.1, code: int = 0, mark: str = "mark") -> list:
    """A prepare command as the sites file names it: this interpreter, the stand-in, and the placeholders."""
    return [sys.executable, "-c", STUDY, "{root}", str(delay), str(code), mark, "{layout}", "{keep}"]


def member(site, label: str = "fed-1", base_id: int = 41, verified: bool = False) -> None:
    """A member up and linked, its two held jobs running, its data verified or not."""
    started = site.now - 3600
    jobs = {key: (str(base_id + n), Job("RUNNING", started - 50, started, site.now + 4 * 3600))
            for n, key in enumerate(("allocation", "sandbox"))}  # fmt: skip
    site.add_member(label, verified=verified, jobs=jobs)


class Bench:
    """A ledger, a sites file and the controller's hooks over one fake site whose sites file may name a prepare."""

    def __init__(self, tmp_path: Path, deploy, site, prepare=None, timeout: float = 30.0, **site_keys) -> None:
        self.deploy, self.site, self.tmp = deploy, site, tmp_path
        self.prepare, self.timeout, self.site_keys = prepare, timeout, site_keys
        self.clock = FakeClock(site.now)
        self.ledger = tmp_path / "out" / "ledger.sqlite"
        Ledger.create(self.ledger, plan(tasks=2), config(), clock=self.clock).close()
        self.log = tmp_path / "control.jsonl"
        self.printed: list = []

    def file(self) -> Path:
        keys = {"max_federations": 7, "allowance_units": 1e12, "rate_units_per_hour": 47.0, "parallel_ups": 4,
                "lease_bound_s": 1800.0, **self.site_keys}  # fmt: skip
        if self.prepare is not None:
            keys.update(prepare=self.prepare, prepare_timeout_s=self.timeout)
        lines = ["[controller]", f'evidence = "{self.tmp / "evidence"}"', f'log = "{self.log}"',
                 "[[site]]", 'label = "site-a"', f'fleet = "{self.site.fleet}"']  # fmt: skip
        lines += [f"{key} = {json.dumps(value)}" for key, value in keys.items()]
        path = self.tmp / "sites.toml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def hooks(self):
        site = self.site

        class Transport:
            def run(self, config_, argv, timeout):
                return site.transport().run(config_, argv, timeout)

        return self.deploy.fleetcontrol.Hooks(
            runner=lambda: None, transport=lambda environ: Transport(), clock=self.clock, sleep=lambda s: None,
            identity=lambda label, name: (FINGERPRINT, IDENTITY), say=self.printed.append,
        )  # fmt: skip

    def tick(self, *extra: str) -> int:
        argv = ["control", "--ledger", str(self.ledger), "--sites", str(self.file()), "--once", *extra]
        code = self.deploy.fleetcontrol.main(argv, {}, self.hooks())
        self.clock.advance(60)
        return code

    def lines(self, kind: str) -> list:
        return [e for e in map(json.loads, self.log.read_text().splitlines()) if e["kind"] == kind]

    def joined(self) -> set:
        ledger = Ledger.open(self.ledger, clock=self.clock)
        try:
            return {f.label for f in ledger.federations()}
        finally:
            ledger.close()


def verbs(site) -> list:
    """The site's commands as verbs: the wrapper's `prepare` and `abort`, shards.py's, and fleet.py's."""
    found = []
    for words in site.commands:
        script = os.path.basename(words[3])
        if script == "fleetprepare.py":
            found.append("prepare" if words[4] == "run" else words[4])
        else:
            found.append(words[4] if script == "shards.py" else words[words.index("--fleet") + 2])
    return found


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def prepare_files(site, label: str = "fed-1") -> tuple:
    logs = site.fleet / "logs"
    return logs / f"{label}.prepare.json", logs / f"{label}.prepare.log"


# ------------------------------------------------------------------------------------------------ the outcomes
def test_c49_a_prepare_that_leaves_the_marker_runs_once_then_the_member_joins_its_log_stays_at_the_site(
    deploy, tmp_path, site
):
    """C49: up and linked with its data unverified, the member's prepare runs once, with `{root}` and `{layout}`
    replaced (other braces kept), its output in the site's own log; the controller's log holds its outcome, exit status
    and duration and no output, command or path; the next tick finds the member verified and joins it, and the prepare
    is not run again."""
    member(site)
    bench = Bench(tmp_path, deploy, site, prepare=study(), max_federations=1)
    assert bench.tick() == 0
    assert verbs(site) == ["prepare"]
    assert bench.joined() == set()
    (done,) = [r for r in bench.lines("result") if r["action"] == "Prepare"]
    assert (done["ok"], done["outcome"], done["exit"], done["code"]) == (True, "ok", 0, 0)
    assert done["duration_s"] >= 0.1
    assert bench.tick() == 0
    assert bench.joined() == {"site-a-fed-1"}
    assert bench.tick() == 0
    assert verbs(site).count("prepare") == 1
    record, log = prepare_files(site)
    said = log.read_text(encoding="utf-8")
    assert f"{CHATTER} {SEPARATE_LAYOUT} {{keep}}" in said
    logged = bench.log.read_text(encoding="utf-8")
    planned = [a for e in bench.lines("tick") for a in e["actions"] if a["action"] == "Prepare"]
    mine = [*planned, *(r for r in bench.lines("result") if r["action"] == "Prepare")]
    assert CHATTER not in logged
    assert len(mine) == 2
    assert not any(secret in json.dumps(mine) for secret in (sys.executable, str(tmp_path), "{root}"))
    assert planned[0]["steps"] == [["<the site's prepare command: not logged>"]]
    assert json.loads(record.read_text(encoding="utf-8"))["state"] == "ended"


@pytest.mark.parametrize("how", ["exit 1", "timeout", "exit 0 without the marker"])
def test_c49_a_prepare_that_fails_times_out_or_leaves_no_marker_retires_its_member_unjoined_then_it_is_replaced(
    deploy, tmp_path, site, how
):
    """C49: a non-zero exit, a timeout (the command's whole process group killed) and an exit 0 with no verified
    marker each retire the member through the retirement path (collect, verify-copy, down), never join it, and the
    usual add replaces it once the site has room (one federation at most here)."""
    member(site)
    grandchild = tmp_path / "grandchild.pid"
    command = {"exit 1": study(code=1), "exit 0 without the marker": study(mark="bare"),
               "timeout": [sys.executable, "-c", HANG, str(grandchild)]}[how]  # fmt: skip
    bench = Bench(tmp_path, deploy, site, prepare=command, timeout=3.0 if how == "timeout" else 30.0,
                  max_federations=1)  # fmt: skip
    site.answers = {"collect": (0, DIGEST + "\n", "")}
    site.on_command = lambda argv: site.set_status("fed-1", "down") if "down" in argv else None
    assert bench.tick() == 0
    assert verbs(site) == ["prepare"]
    if how == "timeout":
        assert not alive(int(grandchild.read_text()))
        (done,) = [r for r in bench.lines("result") if r["action"] == "Prepare"]
        assert (done["ok"], done["outcome"], done["exit"]) == (False, "timeout", None)
    site.commands.clear()
    assert bench.tick() == 0
    assert verbs(site) == ["collect", "verify-copy", "down"]
    (retire,) = [a for a in bench.lines("tick")[-1]["actions"] if a["action"] == "Retire"]
    assert retire["reason"].startswith("never joined: its prepare")
    assert bench.joined() == set()
    site.commands.clear()
    assert bench.tick() == 0
    assert verbs(site) == ["add"]
    assert bench.joined() == set()


# ------------------------------------------------------------------------------------------------ restart safety
def started_record(deploy, site, label: str, pgid=None, start=None) -> None:
    """The record a prepare leaves while it runs: what a controller that died, or a dropped call, leaves behind."""
    claim = {"format": deploy.fleetprepare.FORMAT, "member": label, "state": "started", "started_at": site.now - 99.0,
             "pgid": pgid, "start": start}  # fmt: skip
    (site.fleet / "logs").mkdir(exist_ok=True)
    deploy.fleetprepare._write(deploy.fleetprepare.record_path(site.fleet, label), claim, exclusive=True)


def test_c49_a_prepare_in_flight_when_the_controller_died_retires_its_member_and_ends_the_command(
    deploy, tmp_path, site
):
    """C49: a prepare found started and never ended, with no run of this controller's, is failed: the member is
    retired unjoined, and the retirement begins by ending the command's process group (named by its pid and start
    tick), which is gone before the member is collected."""
    member(site)
    child = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        started_record(deploy, site, "fed-1", child.pid, deploy.fleetprepare.start_tick(child.pid))
        bench = Bench(tmp_path, deploy, site, prepare=study(), max_federations=1)
        site.answers = {"collect": (0, DIGEST + "\n", "")}
        assert bench.tick() == 0
        assert verbs(site) == ["abort", "collect", "verify-copy", "down"]
        assert child.wait(timeout=10) is not None
    finally:
        child.kill()
        child.wait()
    assert bench.joined() == set()
    assert json.loads(prepare_files(site)[0].read_text(encoding="utf-8"))["outcome"] == "aborted"


def test_c49_a_prepare_found_started_by_a_dead_controller_joins_a_member_that_already_reads_verified(
    deploy, tmp_path, site
):
    """C49: the same record on a member whose data is already verified: it joins, and nothing is retired."""
    member(site, verified=True)
    started_record(deploy, site, "fed-1")
    bench = Bench(tmp_path, deploy, site, prepare=study())
    assert bench.tick() == 0
    assert bench.joined() == {"site-a-fed-1"}
    assert [v for v in verbs(site) if v != "add"] == []


def test_c49_the_abort_never_ends_a_process_that_is_not_the_one_the_record_names(deploy, site):
    """C49: a record whose start tick is not the process's now (its pid reused) ends nothing."""
    child = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        started_record(deploy, site, "fed-1", child.pid, (deploy.fleetprepare.start_tick(child.pid) or 0) + 1)
        assert deploy.fleetprepare.main(["abort", "--fleet", str(site.fleet), "--member", "fed-1"]) == 0
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_c49_a_members_prepare_is_started_at_most_once_whoever_asks(deploy, tmp_path, site, capsys):
    """C49: a second `run` for a member whose record exists is refused (status 3), runs nothing, and says no path."""
    member(site)
    touched = tmp_path / "ran-again"
    argv = ["run", "--fleet", str(site.fleet), "--member", "fed-1", "--timeout", "30", "--"]
    assert deploy.fleetprepare.main([*argv, sys.executable, "-c", "pass"]) == 0
    code = deploy.fleetprepare.main([*argv, sys.executable, "-c", f"open({str(touched)!r}, 'w')"])
    assert code == 3
    assert not touched.exists()
    assert str(tmp_path) not in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------ the decision
def test_c49_a_prepare_in_flight_counts_as_on_its_way_and_takes_a_verify_runs_share_of_the_login_memory(
    deploy, tmp_path, site
):
    """C49: with a member's prepare in flight (its busy key) the member is still one on its way, so no extra federation
    is added for it and nothing is decided about it; its share of the login node's memory is the verify-data run's
    that a member up, linked and unverified takes today (`verify_mb`, 1024): at a cap that leaves less than one more
    federation's share beside it, nothing is added, and the same member verified leaves room."""
    bench = Bench(tmp_path, deploy, site, prepare=study())
    views = read_only(bench.ledger)
    try:
        ledger = ledger_view(views)
    finally:
        views.close()
    now = site.now
    members = {verified: Member("fed-1", "up", "/r", ("41", "42"), "RUNNING", now - 3650, now - 3600, now + 4 * 3600,
                                True, verified) for verified in (False, True)}  # fmt: skip
    cap = 512 + 1024 + 295.0
    settings = SiteSettings("site-a", 7, 1e12, 47.0, parallel_ups=4, login_memory_mb=cap, prepare_timeout_s=60.0)

    def decided(verified: bool, busy=frozenset()):
        view = SiteView("site-a", idle_nodes=70, nodes_per_federation=7, time_s=7200.0, members=(members[verified],))
        return decide(now, ledger, [(settings, view)], busy)

    quiet, flying = decided(False), decided(False, frozenset({prepare_key("site-a", "fed-1")}))
    assert [type(a).__name__ for a in quiet.actions] == ["Prepare"]
    assert flying.actions == []
    assert flying.targets["site-a"]["available"] == 1
    limits = flying.targets["site-a"]["limits"]
    assert (limits["login_memory"], limits["login_memory_mb"]["verifying"], limits["login_memory_mb"]["used_mb"]) == (
        0,
        1,
        512 + 1024,
    )
    assert decided(True).targets["site-a"]["limits"]["login_memory"] > 0
    ended = Member("fed-1", "up", "/r", ("41", "42"), "NODE_FAIL", now - 3650, now - 3600, now - 60, True, False)
    view = SiteView("site-a", idle_nodes=70, nodes_per_federation=7, time_s=7200.0, members=(ended,))
    key = frozenset({prepare_key("site-a", "fed-1")})
    assert [type(a).__name__ for a in decide(now, ledger, [(settings, view)], frozenset()).actions] == ["Retire"]
    assert decide(now, ledger, [(settings, view)], key).actions == []  # one operation per member at a time


def test_c49_with_no_prepare_declared_nothing_changes_and_the_site_files_pair_prepare_with_its_bound(
    deploy, tmp_path, site
):
    """C49: a site that names no prepare leaves an up, linked, unverified member waiting as before (no wrapper call,
    no prepare key in its read); `prepare` without `prepare_timeout_s`, the bound without the command, or an argv that
    is not a list of words refuses the sites file."""
    member(site)
    bench = Bench(tmp_path, deploy, site)
    assert bench.tick() == 0
    assert [v for v in verbs(site) if v != "add"] == []
    assert bench.joined() == set()
    read = json.loads(site.transport().run(None, ["python", "-I", "-B", "fleetcontrol.py", "observe", "--fleet",
                                                  str(site.fleet)], 60)[1])  # fmt: skip
    assert "prepare" not in read["members"][0]
    for keys in ({"prepare": ["a"]}, {"prepare_timeout_s": 5.0}, {"prepare": "a", "prepare_timeout_s": 5.0},
                 {"prepare": [], "prepare_timeout_s": 5.0}):  # fmt: skip
        sites = bench.file()
        text = sites.read_text(encoding="utf-8") + "".join(f"{k} = {json.dumps(v)}\n" for k, v in keys.items())
        sites.write_text(text, encoding="utf-8")
        with pytest.raises(deploy.fleetcontrol.ConfigError):
            deploy.fleetcontrol.load_config(sites)


def test_c49_a_dry_run_prints_the_prepare_it_would_run_and_runs_nothing(deploy, tmp_path, site):
    """C49: the dry run prints the wrapper's call with the study's command after `--`, as a live tick runs it."""
    member(site)
    bench = Bench(tmp_path, deploy, site, prepare=study())
    assert bench.tick("--dry-run") == 0
    assert site.commands == []
    printed = "\n".join(bench.printed)
    assert "# Prepare fed-1" in printed
    assert "fleetprepare.py run --fleet" in printed
    assert " -- " in printed
    assert "{root}" in printed


def test_c49_a_retirement_that_ends_a_prepare_does_so_first_on_a_site_over_ssh_too(deploy, tmp_path):
    """C49: `abort` is the retirement's first step, ahead of collect, at a local and at an ssh site (where the pull and
    the copy check follow collect, and `down` is last)."""
    from expcore.campaign.elastic.control import Retire

    settings = SiteSettings("site-a", 7, 1e12, 47.0, prepare_timeout_s=5.0)
    retire = Retire("site-a", "fed-3", None, "/r", True, "r", True)
    local = deploy.fleetexecute.SiteConfig(settings, tmp_path / "fleet", prepare=("a",))
    remote = deploy.fleetexecute.SiteConfig(settings, tmp_path / "fleet", "ssh", "/py", Path("/deploy"), None, "h",
                                            "/env", None, ("a",))  # fmt: skip
    for found in (local, remote):
        rendered = deploy.fleetexecute.render(retire, found, tmp_path / "e", tmp_path / "l")
        assert rendered.steps[0][4] == "abort"
        assert rendered.steps[0][3].endswith("fleetprepare.py")
        assert "down" in rendered.steps[-1]
    assert deploy.fleetexecute.render(retire, remote, tmp_path / "e", tmp_path / "l").places[0] == "site"
