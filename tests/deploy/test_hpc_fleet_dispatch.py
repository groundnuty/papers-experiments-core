"""#462 step 6: the elastic dispatcher runs in the controller's own loop, on the ledger the controller holds (the
ledger's lock admits one holder), each site through a local transport: a lease goes out through the site's channel."""

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from control_site_support import FakeSite
from elastic_support import ENVIRONMENT, FINGERPRINT, FakeClock, config, plan
from hpc_fleet_support import fleet_modules

from expcore.campaign.elastic import Ledger
from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.exchange import MODULE
from expcore.campaign.elastic.exchange import main as exchange_main
from expcore.campaign.elastic.transport import LocalTransport, Missing, Site


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


class LocalRun:
    """`subprocess.run` for the local transport's two calls: the exchange verb in this process, `cat` as a read; each
    call's directory is recorded."""

    def __init__(self) -> None:
        self.cwds: list[str | None] = []

    def __call__(self, argv: list[str], *, check: bool = False, timeout: float | None = None, **kwargs):
        self.cwds.append(kwargs.get("cwd"))
        if argv[0] == "cat":
            try:
                data = Path(argv[2]).read_bytes()
            except OSError:
                return subprocess.CompletedProcess(argv, 1, None, None)
            kwargs["stdout"].write(data)
            return subprocess.CompletedProcess(argv, 0, None, None)
        assert argv[-2:] == ["-m", MODULE], argv
        out = io.StringIO()
        status = exchange_main(io.BytesIO(kwargs["input"]), out)
        return subprocess.CompletedProcess(argv, status, out.getvalue().encode("utf-8"), b"")


def sites_file(tmp_path: Path, site: FakeSite, *, archives: bool = True, channel: bool = True) -> Path:
    lines = ["[controller]", f'evidence = "{tmp_path / "evidence"}"', f'log = "{tmp_path / "control.jsonl"}"']
    if archives:
        lines.append(f'archives = "{tmp_path / "archives"}"')
    lines += ["[[site]]", 'label = "site-a"', f'fleet = "{site.fleet}"', "max_federations = 7"]
    lines += ["allowance_units = 1e12", "rate_units_per_hour = 47.0"]
    if channel:
        lines.append(f'channel = "{tmp_path / "channels"}"')
    path = tmp_path / "sites.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class Stop(Exception):
    pass


def loop(deploy, tmp_path: Path, site: FakeSite, ledger: Path, clock: FakeClock, run: LocalRun, rounds: int) -> None:
    """The controller's loop (no `--once`) for `rounds` goes round, on the fake clock."""
    seen: list[float] = []

    def sleep(seconds: float) -> None:
        seen.append(seconds)
        clock.advance(seconds)
        if len(seen) == rounds:
            raise Stop

    hooks = deploy.fleetcontrol.Hooks(
        runner=lambda: None,
        transport=lambda environ: site.transport(),
        clock=clock,
        sleep=sleep,
        identity=lambda s, m: (FINGERPRINT, {"a": "b"}),
        say=lambda line: None,
        dispatch=lambda cwds: LocalTransport(cwds, run=run),
    )
    argv = ["control", "--ledger", str(ledger), "--sites", str(sites_file(tmp_path, site))]
    with pytest.raises(Stop):
        deploy.fleetcontrol.main(argv, {}, hooks)
    assert seen[0] == 30.0  # a dispatcher takes the loop round every 30 s, the controller's interval being 60


def test_a_lease_goes_out_through_the_sites_channel_in_the_controllers_own_loop(deploy, tmp_path):
    """The controller holds the ledger (a second holder is refused), ticks the dispatcher in the same loop, and the
    dispatcher's second tick issues the joined federation its lease, which lands in its inbox and is marked sent; each
    call of the transport runs in the site's own fleet directory."""
    clock = FakeClock(1_800_000_000.0)
    site = FakeSite(tmp_path / "site-a", deploy)
    ledger = tmp_path / "out" / "ledger.sqlite"
    made = Ledger.create(ledger, plan(tasks=1, conditions=("api-none",)), config(), clock=clock)
    made.join(
        "site-a-fed-1",
        site="site-a",
        job_ids=("41",),
        job_end_at=clock() + 86_400.0,
        fingerprint=FINGERPRINT,
        environment=ENVIRONMENT,
        kill_wait_s=180.0,
    )
    made.close()
    run = LocalRun()
    loop(deploy, tmp_path, site, ledger, clock, run, rounds=3)
    reader = Ledger.open(ledger, clock=clock)
    try:
        (lease,) = reader.leases()
    finally:
        reader.close()
    assert lease.state == "sent"
    assert (tmp_path / "channels" / "site-a-fed-1" / ch.INBOX / ch.lease_name(lease.lease_id)).is_file()
    assert set(run.cwds) == {str(site.fleet)}


@pytest.mark.parametrize(("archives", "channel"), [(True, False), (False, True)])
def test_archives_and_every_sites_channel_go_together(deploy, tmp_path, archives, channel):
    """A sites file that names archives without a site's channel, or a channel without archives, is refused whole."""
    site = FakeSite(tmp_path / "site-a", deploy)
    path = sites_file(tmp_path, site, archives=archives, channel=channel)
    with pytest.raises(deploy.fleetcontrol.ConfigError, match="go together"):
        deploy.fleetcontrol.load_config(path)


def test_a_sites_file_without_them_runs_no_dispatcher(deploy, tmp_path):
    """Neither key: the controller is what it was (no channel is read, no archive kept)."""
    site = FakeSite(tmp_path / "site-a", deploy)
    controller, sites = deploy.fleetcontrol.load_config(sites_file(tmp_path, site, archives=False, channel=False))
    assert controller.archives is None
    assert sites[0].channel is None
    hooks = deploy.fleetcontrol.Hooks(runner=lambda: None)
    assert deploy.fleetcontrol.dispatcher_of(None, controller, sites, hooks) is None


def test_the_local_transport_pulls_an_archive_and_says_a_missing_one_is_missing(tmp_path):
    """`fetch` copies a file of this host, and a file that is not there is `Missing`, as over ssh."""
    here = Site(name="site-a", root=str(tmp_path), python=("python3", "-I", "-B"))
    transport = LocalTransport(run=LocalRun())
    source = tmp_path / "remote.tar"
    source.write_bytes(b"an archive")
    transport.fetch(here, str(source), tmp_path / "pulled.tar")
    assert (tmp_path / "pulled.tar").read_bytes() == b"an archive"
    with pytest.raises(Missing):
        transport.fetch(here, str(tmp_path / "absent.tar"), tmp_path / "never.tar")
    assert not (tmp_path / "never.tar").exists()


def test_the_exchange_verb_runs_here_and_answers_for_the_site(tmp_path):
    """`exchange` runs the verb through the local transport and reads its one reply."""
    here = Site(name="site-a", root=str(tmp_path / "channels"), python=("python3", "-I", "-B"))
    request = {
        "format": "expcore-elastic-exchange/1",
        "site": "site-a",
        "root": here.root,
        "contact": {"seq": 1},
        "federations": [{"label": "site-a-fed-1", "deliver": [], "settle": []}],
    }
    reply = LocalTransport(run=LocalRun()).exchange(here, request)
    assert reply["site"] == "site-a"
    assert json.dumps(reply["federations"]["site-a-fed-1"]["inbox"]) == "{}"
