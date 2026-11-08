"""O14: each sandbox gets its own small network from a dedicated pool; leftovers of a dead process are reaped."""

import ipaddress
import threading

import pytest
from fake_host import BOOT_ID, FakeHost

from expcore_onedata.sandbox import netpool
from expcore_onedata.sandbox.api import SandboxError

LABELS = netpool.Labels("org.test")
SMALL_POOL = ["10.77.0.0/24"]  # sixteen networks of 16 addresses: sixteen concurrent creates must all collide at once


def test_o14_sixteen_concurrent_creates_get_disjoint_networks():
    """O14: 16 concurrent creates get disjoint networks, even when the pool holds exactly 16 and every start collides."""
    host = FakeHost()
    results, errors = {}, []

    def make(i):
        try:
            results[i] = netpool.create(host, f"net-{i}", pools=SMALL_POOL, prefix=28)
        except SandboxError as error:  # pragma: no cover -- would fail the assertions below
            errors.append(error)

    threads = [threading.Thread(target=make, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    nets = [ipaddress.ip_network(subnet) for subnet, _ in results.values()]
    assert len(nets) == 16
    assert all(not a.overlaps(b) for i, a in enumerate(nets) for b in nets[i + 1 :])
    assert all(n.prefixlen == 28 and n.subnet_of(ipaddress.ip_network(SMALL_POOL[0])) for n in nets)


def test_o14_a_seventeenth_network_finds_the_pool_exhausted_and_leaves_nothing_behind():
    """O14: an exhausted pool raises and creates nothing, instead of drawing on the docker default pools."""
    host = FakeHost()
    for i in range(16):
        netpool.create(host, f"net-{i}", pools=SMALL_POOL, prefix=28)
    with pytest.raises(SandboxError, match="no free /28"):
        netpool.create(host, "net-extra", pools=SMALL_POOL, prefix=28)
    assert "net-extra" not in host.networks


def test_o14_every_network_is_created_internal_with_an_explicit_subnet():
    """O14: an explicit subnet never draws on docker's default pools, and the network has no route out."""
    host = FakeHost()
    subnet, _ = netpool.create(host, "net-a", pools=SMALL_POOL, prefix=28)
    args = host.networks["net-a"]["args"]
    assert "--internal" in args
    assert args[args.index("--subnet") + 1] == subnet
    assert "com.docker.network.bridge.inhibit_ipv4=true" in args


def test_o14_a_pool_that_the_host_already_routes_is_refused():
    """O14: sandbox networks inside a range the host routes would shadow real destinations."""
    host = FakeHost()
    host.routes = "10.77.0.0/16 via 192.0.2.9 dev eth1\n"
    with pytest.raises(SandboxError, match="overlaps this host's routes"):
        netpool.create(host, "net-a", pools=SMALL_POOL, prefix=28)
    assert host.networks == {}


def test_a_refusal_other_than_an_overlap_is_not_retried():
    host = FakeHost()
    host.fail_network_create.append("Error response from daemon: something else")
    with pytest.raises(SandboxError, match="failed"):
        netpool.create(host, "net-a", pools=SMALL_POOL, prefix=28)
    assert host.calls_of("network_created") == []


def test_several_pools_are_all_candidates():
    host = FakeHost()
    pools = ["10.77.0.0/28", "10.201.0.0/28"]
    got = {netpool.create(host, f"net-{i}", pools=pools, prefix=28)[0] for i in range(2)}
    assert got == {"10.77.0.0/28", "10.201.0.0/28"}


def test_the_labels_carry_the_owner_the_directory_and_the_lease(tmp_path):
    host = FakeHost()
    args = LABELS.args(host, tmp_path / "d", "key-1")
    pairs = dict(arg.split("=", 1) for arg in args if "=" in arg)
    assert pairs["org.test.sandbox"] == "1"
    assert pairs["org.test.dir"] == str(tmp_path / "d")
    assert pairs["org.test.lease"] == "key-1"
    assert pairs["org.test.owner"].startswith(BOOT_ID + "/4026531836/")


def test_an_owner_is_gone_only_when_proved():
    host = FakeHost()
    own = netpool.owner_id(host)
    assert netpool.owner_gone(host, own) is False
    boot, ns, pid, start = own.split("/")
    assert netpool.owner_gone(host, f"{boot}/{ns}/{pid}/{int(start) + 1}") is True  # the pid was reused
    assert netpool.owner_gone(host, f"{boot}/{ns}/4242/9") is True  # no such process: the kernel says so
    other_boot = "11111111-1111-1111-1111-111111111111"
    assert netpool.owner_gone(host, f"{other_boot}/{ns}/{pid}/{start}") is True  # an earlier boot
    assert netpool.owner_gone(host, f"{boot}/9999/{pid}/{start}") is False  # another namespace: not ours to judge
    assert netpool.owner_gone(host, "garbage") is False


def test_reaping_removes_only_the_resources_of_a_provably_dead_owner(tmp_path):
    host = FakeHost()
    own, boot = netpool.owner_id(host), BOOT_ID
    dead = f"{boot}/4026531836/4242/9"
    for name, owner in (("mine-gate", own), ("dead-gate", dead)):
        labels = {"org.test.sandbox": "1", "org.test.owner": owner, "org.test.dir": str(tmp_path)}
        host.docker(
            "run", "-d", "--name", name, *[a for k, v in labels.items() for a in ("--label", f"{k}={v}")], "img"
        )
    host.docker("network", "create", "--subnet", "10.77.0.0/28", "--label", f"org.test.sandbox=1", "--label", f"org.test.owner={dead}", "--label", f"org.test.dir={tmp_path}", "dead-net")  # fmt: skip
    out = netpool.reap_orphans(host, LABELS)
    assert set(host.containers) == {"mine-gate"}
    assert out["networks"] == ["dead-net"]
    assert len(out["containers"]) == 1
