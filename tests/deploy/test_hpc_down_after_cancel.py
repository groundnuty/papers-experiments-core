"""K1 (live slot 5): a step that down cancels at the stop's bound is ended outright on the site that showed it (its
processes killed, no trap run), which leaves its service's work directory (2 to 2.6 GB of the node's tmpfs) on its
node. down then removes the work directory the state recorded for that service, and only that one, shown gone by the
node's check, so it finishes in one pass; a cancel whose own exit status says it failed (225 on a het job) is judged by
the step leaving the list. The fake Slurm of hpc_fed_support stands for the cluster; each node's directories are a set
here. Nothing reaches a cluster."""

from itertools import count

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeProcess, FakeSlurm, agent_site, hpc_on_path

PROVIDERS = ["aspen", "larch", "rowan", "alder", "hazel"]


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


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


class Nodes:
    """Each node's tmpfs as down's control steps see it: the work directories still there. A service's teardown fails
    (the service would not stop) and leaves its step listed; `removal_fails` keeps a directory a removal was asked
    for."""

    def __init__(self, removal_fails=()):
        self.left: set = set()
        self.removals: list = []
        self.removal_fails = set(removal_fails)

    def answer(self, srun):
        from hpcslurm import Completed

        command = srun.command
        if "-teardown-" in srun.name:  # the stop's own teardown: the service would not end, its step stays listed
            return FakeProcess(Completed(1, "", "hpcop: ERROR: the service is still running after 600s\n"))
        if "-remove-" in srun.name:
            workdir = command[command.index("--workdir") + 1]
            self.removals.append((srun.node, command))
            if workdir not in self.removal_fails:
                self.left.discard(workdir)
            return FakeProcess(Completed(0, "", ""))
        if command[0] == "test":
            return FakeProcess(Completed(1 if command[-1] in self.left else 0, "", ""))
        return None


def rig(hpc, tmp_path, site, nodes, fake=None):
    fake = fake or FakeSlurm(on_control=nodes.answer)
    driver = hpc.cli.make_driver(LAYOUT, str(tmp_path / "fed" / "root"), runner=fake)
    driver.sleep = driver.slurm.sleep = lambda s: None
    driver.poll_s = driver.slurm.poll_s = 0.0
    driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
    driver.relay_available = lambda: False
    driver.up(hpc.settings.site_from_environment(site))
    ticks = count(0, 100)
    driver.clock = lambda: next(ticks)  # the stop's bound passes in a few polls
    nodes.left = {record["workdir"] for record in driver.store.read()["services"].values() if record["workdir"]}
    return driver, fake


@pytest.mark.parametrize("cancel_rc", [0, 225], ids=["a cancel that says it ended the step", "one that says it failed"])
def test_k1_down_removes_each_cancelled_services_recorded_work_directory_and_finishes_in_one_pass(
    hpc, tmp_path, site, cancel_rc
):
    """K1: every service would not stop, so down cancels each step at the bound; once each has left the list, it
    removes the work directory the state recorded for that service (teardown.sh, on the service's own node), each
    shown gone, and the root goes: one pass. A cancel that exits 225 as it ends the step changes nothing."""
    nodes = Nodes()
    driver, fake = rig(hpc, tmp_path, site, nodes, FakeSlurm(on_control=nodes.answer, step_cancel_rc=cancel_rc))
    services = driver.store.read()["services"]
    printed = driver.down()
    assert printed.startswith("down: the held job 4242 is gone, and so is the root")
    assert nodes.left == set()
    removed = {command[command.index("--workdir") + 1]: (node, command) for node, command in nodes.removals}
    for name in [*PROVIDERS, "zone"]:
        record = services[name]
        node, command = removed[record["workdir"]]
        assert node == record["node"]
        assert command[2:] == [record["kind"], "--workdir", record["workdir"], "--timeout", "0"]
    assert len(nodes.removals) == 6
    assert [c for c in fake.cancelled if "." not in c] == ["4242"]


def test_k1_a_removal_that_leaves_the_directory_keeps_the_root_and_still_cancels_the_job(hpc, tmp_path, site):
    """K1: a work directory still there after its removal is never passed: down says the service is unverified, keeps
    the root and its state for a second down, and cancels the job all the same."""
    nodes = Nodes()
    driver, fake = rig(hpc, tmp_path, site, nodes)
    stuck = driver.store.read()["services"]["larch"]["workdir"]
    nodes.removal_fails = {stuck}
    with pytest.raises(RuntimeError, match=r"but larch could not be verified clean"):
        driver.down()
    assert nodes.left == {stuck}
    assert fake.cancelled[-1] == "4242"
    assert (tmp_path / "fed" / "root").exists()


def test_k1_down_removes_nothing_its_state_did_not_record_as_the_bundles(hpc, tmp_path, site):
    """K1: a recorded work directory that is not one the bundle makes (here, edited into the state) is never removed:
    no removal step runs for it, the service stays unverified, and the others are removed as recorded."""
    nodes = Nodes()
    driver, _ = rig(hpc, tmp_path, site, nodes)
    driver.store.update(lambda s: (s["services"]["rowan"].update(workdir="/tmp/not-the-bundles"), s)[1])
    with pytest.raises(RuntimeError, match=r"but rowan could not be verified clean"):
        driver.down()
    removed = [command[command.index("--workdir") + 1] for _, command in nodes.removals]
    assert "/tmp/not-the-bundles" not in removed
    assert len(removed) == 5
