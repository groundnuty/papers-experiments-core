"""O57, O68: the fleet launcher (deploy/hpc-federation/fleet.py) reads the account's home once for all its federations,
as `up` and `add` start, and refuses a home past 95% of its files or bytes before the fleet's directory, a slot or a job
exists (the home guard of `hpcquota`, DESIGN.md section 11, which every federation's own `up` also runs). Every figure
and command here is invented; every federation runs over a fake cluster of its own (`hpc_fleet_support.Bench`)."""

import pytest
from hpc_fed_support import SITE, FakeSlurm
from hpc_fleet_support import fleet_modules, make_bench, make_ca
from hpc_home_support import PATTERN, TOOL, Quota
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


class Guards:
    """The home and inode guards' stand-ins, recording what each was asked and in which order."""

    def __init__(self, refuse_home=None):
        self.order, self.sites, self.counts, self.refuse_home = [], [], [], refuse_home

    def home(self, site):
        self.order.append("home")
        self.sites.append(site)
        if self.refuse_home:
            raise RuntimeError(self.refuse_home)
        return "home check: stand-in"

    def inodes(self, count, layout, site, environ):
        self.order.append("inodes")
        self.counts.append(count)
        return f"inode check: stand-in for {count}"


def installed(bench, **given):
    guards = Guards(**given)
    bench.home_guard, bench.guard = guards.home, guards.inodes
    return guards


def test_o57_up_reads_the_home_once_for_the_whole_fleet_before_the_inode_guard_and_says_so(bench):
    """O57: a fleet of three asks the home guard once (not once per federation), with the site the fleet loaded, ahead
    of the inode guard, and prints its words."""
    guards = installed(bench)
    code, printed = bench.up(3)
    assert code == 0, printed
    assert guards.order == ["home", "inodes"]
    assert guards.counts == [3]
    assert [site.account for site in guards.sites] == [SITE["HPC_ACCOUNT"]]
    assert "fleet: home check: stand-in" in printed


def test_o57_up_refused_by_the_home_guard_makes_nothing_and_never_asks_the_inode_guard(bench):
    """O57: a full home refuses the fleet's up before the inode guard, the fleet's directory or any job: the refusal
    is the guard's, printed whole."""
    guards = installed(bench, refuse_home="the account's home is past 95% of its files (files used 38001)")
    code, printed = bench.up(4)
    assert code == 2
    assert "files used 38001" in printed
    assert guards.order == ["home"]
    assert not bench.fleet.exists()
    assert bench.fakes == {}


def test_o57_add_reads_the_home_once_and_a_full_home_refuses_it_before_a_slot_is_made(bench):
    """O57: `add` asks the home guard once for all K new federations; once the home is full, a later `add` is refused
    before any slot, root or job is made, and the fleet's record is as it was."""
    guards = installed(bench)
    assert bench.up(2)[0] == 0
    guards.order.clear()
    code, printed = bench.run("add", "2")
    assert code == 0, printed
    assert guards.order == ["home", "inodes"]
    assert guards.counts[-1] == 2
    members = sorted(bench.fleet.glob("fed-*"))
    assert len(members) == 4
    guards.refuse_home = "the account's home is past 95% of its bytes"
    guards.order.clear()
    code, printed = bench.run("add", "1")
    assert code == 2
    assert "past 95% of its bytes" in printed
    assert guards.order == ["home"]
    assert sorted(bench.fleet.glob("fed-*")) == members
    assert "fed-5" not in bench.fakes


def test_o57_a_fleet_without_a_home_guard_stand_in_still_brings_every_federation_up(bench):
    """O57: with the home guard answering nothing (a site with no home key set), the fleet is what it was: no home line
    is printed and every federation is up."""
    bench.home_guard = lambda site: ""
    code, printed = bench.up(2)
    assert code == 0, printed
    assert "home check" not in printed


def test_o57_the_default_hooks_ask_the_cores_home_guard_with_the_login_nodes_runner(deploy):
    """O57: the fleet's default hooks wire `home_guard` to the core's `hpcquota.home_guard`: a site without the keys
    answers nothing and runs nothing, the override answers that the check was skipped, and keys naming a command that
    does not exist reach the real runner and are refused as unreadable (a local command, never a cluster's)."""
    hooks = deploy.fleet.default_hooks({})
    base = {**SITE, "SH": "/invented/images", "L": "/invented/logs"}
    settings = deploy.hpc.settings
    assert hooks.home_guard(settings.site_from_environment(base)) == ""
    off = settings.site_from_environment({**base, "HPC_HOME_QUOTA_CHECK": "off"})
    assert hooks.home_guard(off) == "home check: skipped: HPC_HOME_QUOTA_CHECK=off"
    missing = {**base, "HPC_HOME_QUOTA_CMD": "/invented/no-such-home-quota", "HPC_HOME_QUOTA_PATTERN": PATTERN}
    with pytest.raises(deploy.hpc.quota.QuotaRefused, match="could not be run"):
        hooks.home_guard(settings.site_from_environment(missing))


def test_o57_the_fleets_home_guard_is_the_cores_own_and_refuses_a_full_home_over_a_fake_cluster(deploy):
    """O57: the function the default hooks call is `hpcquota.home_guard` over the runner it is given: at the line it
    passes and past it refuses, with nothing submitted, whatever the runner."""
    base = {**SITE, "SH": "/invented/images", "L": "/invented/logs"}
    site = deploy.hpc.settings.site_from_environment(
        {**base, "HPC_HOME_QUOTA_CMD": f"{TOOL} --home", "HPC_HOME_QUOTA_PATTERN": PATTERN}
    )
    assert deploy.fleet.hpcquota is deploy.hpc.quota
    at = FakeSlurm(tools={TOOL: Quota(deploy.hpc, 38_000, 0)})
    assert "under the line" in deploy.hpc.quota.home_guard(at, site)
    past = FakeSlurm(tools={TOOL: Quota(deploy.hpc, 38_001, 0)})
    with pytest.raises(deploy.hpc.quota.QuotaRefused, match="95% line 38000"):
        deploy.hpc.quota.home_guard(past, site)
