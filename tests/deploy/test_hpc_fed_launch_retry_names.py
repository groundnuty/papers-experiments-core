"""A service launched again after its launch failed at launch is launched with the command of the first launch, the
names of the registered-names change (O73) included. The launch retry (O57) and the declared names landed apart; a
retry that rebuilt the command from the bare arguments would start the service without `--declared` and every
`--host`, so it would register and resolve by its node's own name. Over the fake Slurm of hpc_fed_support: every name
here is invented."""

import time

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path

T0 = time.mktime(time.strptime("2027-01-15T12:00:00", "%Y-%m-%dT%H:%M:%S"))  # where the fake wall clock starts


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


def rig_up(hpc, tmp_path, site, fake):
    """`up` over the fake Slurm, the core's phase stubbed, no waiting; the steps the fake spawned are returned."""
    driver = hpc.cli.make_driver(LAYOUT, str(tmp_path / "fed" / "root"), runner=fake)
    now = [T0]
    driver.say = lambda text: None
    driver.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    driver.wall = lambda: now[0]
    driver.poll_s = 0.0
    driver.slurm.sleep = lambda s: None
    driver.slurm.poll_s = 0.0
    driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
    driver.relay_available = lambda: False
    driver.up(hpc.settings.site_from_environment(site))
    return driver


@pytest.mark.parametrize("service", ["zone", "larch"])
def test_o57_a_service_launched_again_carries_the_names_of_its_first_launch(hpc, tmp_path, site, service):
    """O57: the zone's and a provider's second launch is the first launch's command to the last argument, its
    `--declared` name and every service's `--host` (O73) in it, so a service that failed at launch and started on the
    retry registers by its declared name and resolves its peers by theirs."""
    fake = FakeSlurm(launch_failures={service: [{}]})
    rig_up(hpc, tmp_path, site, fake)
    first, second = [
        s for s in fake.spawned if s.name.endswith(f"-{service}-run-1") or s.name.endswith(f"-{service}-run-2")
    ]
    declared = [a for a in first.command if a.startswith("--declared=")]
    hosts = [a for a in first.command if a.startswith("--host=")]
    assert len(declared) == 1, "the first launch carries its declared name"
    assert hosts, "the first launch carries the hosts"
    assert second.command == first.command
