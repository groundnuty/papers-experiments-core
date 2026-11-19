"""#462 step 6: a federation joins the ledger with the identity read for it and only once its worker is launched, and a
lost lease's store is found where a place holds it (deploy/hpc-federation/fleetexecute.py, fleetidentity.py). The
ledger, the transport and the interpreter are stand-ins; nothing reaches a cluster."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_fleet_support import fleet_modules

from expcore.campaign.elastic.control import Join, Settle, SiteSettings

FINGERPRINT = "f" * 64
IDENTITY = {"layout": "a" * 64, "platform": "b" * 64}
PLATFORM = "c" * 64  # C50: the federation's own platform fingerprint, as a read prints it


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetexecute
        import fleetidentity
        import fleetrecord

        yield SimpleNamespace(
            found=found, fleetexecute=fleetexecute, fleetidentity=fleetidentity, fleetrecord=fleetrecord
        )


class Ledger:
    def __init__(self, events: list) -> None:
        self.events = events

    def join(self, label, **fields) -> None:
        self.events.append(("join", label, fields))


class Transport:
    """The site's login node: each command it is run, in order, and the exit status the test chose."""

    def __init__(self, events: list, code: int = 0) -> None:
        self.events, self.code = events, code

    def run(self, site, argv, timeout):
        self.events.append(("run", list(argv)))
        return self.code, "", "srun refused" if self.code else ""


def executor(deploy, tmp_path: Path, events: list, *, code: int = 0, channel: bool = True, settings: bool = True):
    config = deploy.fleetexecute.SiteConfig(
        SiteSettings("site-a", 7, 1e12, 47.0),
        tmp_path / "fleet",
        channel=tmp_path / "channels" if channel else None,
    )
    log = deploy.fleetexecute.Log(None)
    run = deploy.fleetexecute.Executor(
        Ledger(events), tmp_path / "ledger.sqlite", {"site-a": config}, Transport(events, code), tmp_path / "evidence",
        log, lambda site, member: (FINGERPRINT, IDENTITY), lambda site, action: None,
        settings=tmp_path / "shard-settings.toml" if settings else None,
    )  # fmt: skip
    return run, log, config


def join_action() -> Join:
    return Join("site-a", "fed-1", "site-a-fed-1", ("41", "42"), 1_800_007_200.0, 180.0, 30.0, "up and verified")


def test_a_federation_joins_only_once_its_worker_is_launched_on_its_site(deploy, tmp_path):
    """The worker is started first (`fleetworker.py launch`, with the federation's ledger label, its channel under the
    site's channel root and its job's recorded end), then the ledger's join, which carries the identity read for it."""
    events: list = []
    run, _, _ = executor(deploy, tmp_path, events)
    run.in_ledger(join_action())
    assert [event[0] for event in events] == ["run", "join"]
    argv = events[0][1]
    assert Path(argv[3]).name == "fleetworker.py"
    assert argv[4] == "launch"
    assert argv[argv.index("--federation") + 1] == "site-a-fed-1"
    assert argv[argv.index("--channel") + 1] == str(tmp_path / "channels" / "site-a-fed-1")
    assert argv[argv.index("--member") + 1] == "fed-1"
    assert argv[argv.index("--job-end") + 1] == repr(1_800_007_200.0)
    assert events[1][2]["fingerprint"] == FINGERPRINT


def test_a_worker_that_cannot_be_launched_keeps_its_federation_out_of_the_ledger(deploy, tmp_path):
    """A launch that fails is logged and the join waits (it is decided again next tick): no lease can go to a
    federation that has no worker."""
    events: list = []
    run, log, _ = executor(deploy, tmp_path, events, code=1)
    run.in_ledger(join_action())
    assert [event[0] for event in events] == ["run"]
    failed = [entry for entry in log.lines if entry.get("action") == "Worker"]
    assert failed
    assert failed[0]["ok"] is False


def test_a_site_with_no_channel_or_a_controller_with_no_settings_joins_as_it_did(deploy, tmp_path):
    """Without a channel or shard settings nothing is launched: the join is the one step 5 made."""
    for n, changes in enumerate(({"channel": False}, {"settings": False})):
        events: list = []
        run, _, _ = executor(deploy, tmp_path / str(n), events, **changes)
        run.in_ledger(join_action())
        assert [event[0] for event in events] == ["join"]


def test_the_dry_run_prints_the_launch_before_the_join(deploy, tmp_path):
    """What the dry run prints is what a live join runs: the site's launch first, then the ledger's join."""
    _, _, config = executor(deploy, tmp_path, [])
    rendered = deploy.fleetexecute.render(
        join_action(), config, tmp_path / "e", tmp_path / "l.sqlite", settings=tmp_path / "shard-settings.toml"
    )
    text = deploy.fleetexecute.printed(deploy.fleetexecute.described(join_action(), rendered))
    assert text.index("fleetworker.py launch") < text.index("fleetcontrol.py join")


def test_the_identity_is_read_beside_the_federations_own_platform_file_by_the_manifests_python(deploy, tmp_path):
    """The study is copied to a private directory with the member's own platform.toml beside it and the manifest's
    interpreter asks the core, `-I -B`, behind an environment of the allowlist and a home of its own; the answer is the
    fingerprint's digest and the identity; the directory is gone after; a failing read, or a member that is not up, is
    None."""
    fleet = tmp_path / "fleet"
    fleet.mkdir(mode=0o700)
    study = tmp_path / "study"
    study.mkdir()
    (study / "study.toml").write_text("[study]\n")
    settings = tmp_path / "shard-settings.toml"
    settings.write_text(
        f'study = "{study}"\npython = "/venv/bin/python"\ncore_src = "/c"\nresults = "/r"\ntmpfs = "/t"\n'
        'step_path = "/usr/bin:/bin"\ncpus = 2\nmemory = "4g"\n'
    )
    record = deploy.fleetrecord.new_record(tmp_path / "layout.toml", "digest", 2, 5, 1)
    member = deploy.fleetrecord.new_member(fleet, "fed-1")
    member["status"] = "up"
    root = deploy.fleetrecord.root_of(fleet, "fed-1")
    root.mkdir(parents=True)
    (root / "platform.toml").write_text('[space]\nname = "s"\n')
    member["made"] = [root.stat().st_dev, root.stat().st_ino]
    record["members"].append(member)
    deploy.fleetrecord.Record(fleet).save(record)
    seen = {}

    def runner(argv, **kwargs):
        copy = Path(argv[-1])
        seen.update(
            argv=list(argv), env=dict(kwargs["env"]), cwd=kwargs["cwd"], platform=(copy / "platform.toml").read_text()
        )
        out = json.dumps({"fingerprint": FINGERPRINT, "environment": IDENTITY, "platform": PLATFORM})
        return subprocess.CompletedProcess(argv, seen.get("code", 0), out, "")

    found = deploy.fleetidentity.read_identity(fleet, "fed-1", settings, runner)
    assert found == (FINGERPRINT, IDENTITY, PLATFORM)
    assert seen["argv"][:4] == ["/venv/bin/python", "-I", "-B", "-c"]
    assert seen["platform"] == '[space]\nname = "s"\n'
    allowed = {"PATH", "LC_ALL", "EXPCORE_FED_ROOT_PARENT", "EXPCORE_FED_ROOT", "TMPDIR"}
    assert set(seen["env"]) <= allowed | set(deploy.found.shards.home_env("/home"))
    assert not Path(seen["cwd"]).exists()
    seen["code"] = 3
    assert deploy.fleetidentity.read_identity(fleet, "fed-1", settings, runner) is None
    assert deploy.fleetidentity.read_identity(fleet, "fed-9", settings, runner) is None


def test_the_read_command_prints_the_one_json_line_the_controller_of_an_ssh_site_parses(deploy, tmp_path, capsys):
    """`fleetidentity.py read` (run on the login node of an ssh site) prints `read_identity`'s answer as one JSON line
    that `read_identity_remote` takes back as the same pair, and exits 1, printing nothing, where there is none."""
    fleet = tmp_path / "fleet"
    fleet.mkdir(mode=0o700)
    study = tmp_path / "study"
    study.mkdir()
    (study / "study.toml").write_text("[study]\n")
    settings = tmp_path / "shard-settings.toml"
    settings.write_text(
        f'study = "{study}"\npython = "/venv/bin/python"\ncore_src = "/c"\nresults = "/r"\ntmpfs = "/t"\n'
        'step_path = "/usr/bin:/bin"\ncpus = 2\nmemory = "4g"\n'
    )
    record = deploy.fleetrecord.new_record(tmp_path / "layout.toml", "digest", 2, 5, 1)
    member = deploy.fleetrecord.new_member(fleet, "fed-1")
    member["status"] = "up"
    root = deploy.fleetrecord.root_of(fleet, "fed-1")
    root.mkdir(parents=True)
    (root / "platform.toml").write_text('[space]\nname = "s"\n')
    member["made"] = [root.stat().st_dev, root.stat().st_ino]
    record["members"].append(member)
    deploy.fleetrecord.Record(fleet).save(record)

    def runner(argv, **kwargs):
        out = json.dumps({"fingerprint": FINGERPRINT, "environment": IDENTITY, "platform": PLATFORM})
        return subprocess.CompletedProcess(argv, 0, out, "")

    words = ["read", "--fleet", str(fleet), "--settings", str(settings)]
    assert deploy.fleetidentity.main([*words, "--member", "fed-1"], runner) == 0
    printed = capsys.readouterr().out
    assert len(printed.splitlines()) == 1
    config = SimpleNamespace(
        shard_settings=settings, fleet=fleet, script=lambda name, *rest: ["python", name, *rest], remote=True
    )

    class Said:
        def run(self, site, argv, timeout):
            return 0, printed, ""

    assert deploy.fleetidentity.read_identity_remote(Said(), config, "fed-1") == (FINGERPRINT, IDENTITY, PLATFORM)
    assert deploy.fleetidentity.main([*words, "--member", "fed-9"], runner) == 1
    assert capsys.readouterr().out == ""


def test_a_lost_leases_store_is_found_in_its_channel_and_failing_that_in_the_pulled_evidence(deploy, tmp_path):
    """The store as a place holds it: `study/out/` of `lease-<id>`, in the federation's channel (`root`, whatever the
    member's root) or, failing that, in the evidence collected from the member (`pulled`)."""
    locate = deploy.fleetidentity.locator(tmp_path / "evidence")
    config = deploy.fleetexecute.SiteConfig(
        SiteSettings("site-a", 7, 1e12, 47.0), tmp_path / "fleet", channel=tmp_path / "channels"
    )
    name = f"lease-{7:012d}"
    channel = tmp_path / "channels" / "site-a-fed-1" / "places" / name / "study" / "out"
    pulled = tmp_path / "evidence" / "site-a-fed-1" / "places" / name / "study" / "out"
    settle = Settle("site-a", "fed-1", "site-a-fed-1", 7, str(tmp_path / "root"), "lost")
    assert locate(config, settle) is None
    pulled.mkdir(parents=True)
    (pulled / "state.sqlite").write_bytes(b"x")
    assert locate(config, settle) == (pulled, "pulled")
    channel.mkdir(parents=True)
    (channel / "state.sqlite").write_bytes(b"x")
    assert locate(config, settle) == (channel, "root")
    gone = Settle("site-a", "fed-1", "site-a-fed-1", 7, None, "lost")  # the site no longer shows the member
    assert locate(config, gone) == (channel, "root")  # its channel outlives it: still read first
    shutil.rmtree(channel.parents[2])
    assert locate(config, gone) == (pulled, "pulled")  # the channel's place gone: the copy pulled at its retirement
