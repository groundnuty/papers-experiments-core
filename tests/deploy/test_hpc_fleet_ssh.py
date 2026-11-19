"""#462 step 6 (ssh): the elastic fleet's controller driving one REMOTE site's login node. Every site command goes
over a fake `ssh` executable on PATH (it checks the options, logs each call and runs the command through `sh -c`, as
sshd does), to stub scripts standing in for the deploy directory on the login node. The ledger and the pulled copies
stay on this host. A call that fails or hangs is the site unread, never ended; the host is in no log, error or
record."""

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from control_site_support import FakeSite, Job, observed
from elastic_support import FINGERPRINT, FakeClock, config, plan
from fake_ssh_bin import install_fake_ssh
from hpc_fleet_support import fleet_modules
from leased_support import killed_mid_lease, ran_for_real
from study_support import clean_environment

from expcore.campaign.elastic import Ledger, SiteReading
from expcore.campaign.elastic.control import Drain, Retire, Settle, SiteSettings
from expcore.campaign.ledger_read import read_ledger, take_ledger_snapshot

HOST = "zz-login-7f3a.invalid"
SITE_LABEL = "site-a"  # the label of both the ledger's site (`elastic_support.SITE`) and the controller's
__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

ROOT_ONLY = "expcore-hpc-root-archives/1"
STEM = "federation"
FILES = {f"{STEM}.root.tar": b"the root's evidence archive", f"{STEM}.root.json": b'{"its": "manifest"}'}

STUB_SHARDS = """
import hashlib, json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
with open(here / "calls.jsonl", "a") as handle:
    handle.write(json.dumps({"script": "shards", "argv": sys.argv[1:], "cwd": os.getcwd(),
                             "account": os.environ.get("HPC_ACCOUNT")}) + "\\n")
mode = (here / "mode.txt").read_text()
if sys.argv[1] != "collect" or mode == "collect-fails":
    sys.exit(0 if sys.argv[1] != "collect" else 1)
into = Path(sys.argv[sys.argv.index("--into") + 1])
if (into / "archives.sha256.json").exists():  # collected before: `already`, the same hash
    print(hashlib.sha256((into / "archives.sha256.json").read_bytes()).hexdigest())
    sys.exit(0)
into.mkdir(mode=0o700, parents=True)
files = json.loads((here / "files.json").read_text())
entries = []
for name, text in sorted(files.items()):
    data = text.encode()
    (into / name).write_bytes(data[:-1] + b"X" if mode == "corrupt" and name.endswith(".tar") else data)
    entries.append({"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
payload = (json.dumps({"format": "@FORMAT@", "evidence": entries}, sort_keys=True, indent=2) + "\\n").encode()
(into / "archives.sha256.json").write_bytes(payload)
print("0" * 64 if mode == "wrong-digest" else hashlib.sha256(payload).hexdigest())
""".replace("@FORMAT@", ROOT_ONLY)

STUB_FLEET = """
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
with open(here / "calls.jsonl", "a") as handle:
    handle.write(json.dumps({"script": "fleet", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
if "down" in sys.argv:
    (here / "downed").write_text("yes")
"""

STUB_OBSERVE = """
import sys
from pathlib import Path
print((Path(__file__).resolve().parent / "observed.json").read_text())
"""

STUB_IDENTITY = """
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent
with open(here / "calls.jsonl", "a") as handle:
    handle.write(json.dumps({"script": "identity", "argv": sys.argv[1:]}) + "\\n")
print(json.dumps({"fingerprint": "f" * 64, "environment": {"core_commit": "0123abcd"}}))
"""


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetcontrol
        import fleetexecute
        import fleetidentity
        import fleetobserve
        import fleetrecord
        import fleetssh
        import hpcslurm
        import hpcstate

        yield SimpleNamespace(fleet=found.fleet, fleetcontrol=fleetcontrol, fleetexecute=fleetexecute,
                              fleetidentity=fleetidentity, fleetssh=fleetssh, fleetobserve=fleetobserve,
                              fleetrecord=fleetrecord, hpcslurm=hpcslurm, hpcstate=hpcstate)  # fmt: skip


class Login:
    """The login node of a remote site: a deploy directory of stub scripts, a fleet directory, a site.env, and the
    site as the controller is told about it."""

    def __init__(self, tmp_path: Path, monkeypatch, deploy, host: str = HOST, **settings) -> None:
        self.deploy = deploy
        self.fake = install_fake_ssh(tmp_path / "bin", monkeypatch, host)
        self.base = tmp_path / ("login" if host == HOST else host)  # a directory named for the alias, if it has one
        self.scripts = self.base / "deploy"
        self.scripts.mkdir(parents=True)
        for name, text in (("shards.py", STUB_SHARDS), ("fleet.py", STUB_FLEET), ("fleetcontrol.py", STUB_OBSERVE),
                           ("fleetidentity.py", STUB_IDENTITY)):  # fmt: skip
            (self.scripts / name).write_text(text, encoding="utf-8")
        (self.scripts / "mode.txt").write_text("ok", encoding="utf-8")
        (self.scripts / "files.json").write_text(json.dumps({k: v.decode() for k, v in FILES.items()}), "utf-8")
        self.fleet = self.base / "fleet"
        self.fleet.mkdir(mode=0o700)
        self.env_file = self.base / "site env.sh"
        self.env_file.write_text("HPC_ACCOUNT='an account'\n", encoding="utf-8")
        self.config = deploy.fleetexecute.SiteConfig(
            SiteSettings(label="site-a", max_federations=7, allowance_units=1e12, rate_units_per_hour=47.0, **settings),
            self.fleet,
            "ssh",
            sys.executable,
            self.scripts,
            None,
            host,
            str(self.env_file),
            self.base / "settings.toml",
        )
        self.transport = deploy.fleetexecute.SiteTransport(dict(os.environ), ssh=deploy.fleetssh.SshSiteTransport(
            dict(os.environ), fetch_timeout_s=2.0))  # fmt: skip

    def mode(self, text: str) -> None:
        (self.scripts / "mode.txt").write_text(text, encoding="utf-8")

    def calls(self, script: str | None = None) -> list:
        path = self.scripts / "calls.jsonl"
        found = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [c for c in found if script in (None, c["script"])]

    def executor(self, tmp_path: Path, ledger=None):
        self.log = self.deploy.fleetexecute.Log(None)
        return self.deploy.fleetexecute.Executor(ledger, tmp_path / "ledger.sqlite", {"site-a": self.config},
                                                 self.transport, tmp_path / "evidence", self.log)  # fmt: skip


@pytest.fixture
def login(deploy, tmp_path, monkeypatch):
    return Login(tmp_path, monkeypatch, deploy)


def retire(root: str = "/a root/it's") -> Retire:
    return Retire("site-a", "fed-1", "site-a-fed-1", root, True, "its end is near")


def whole_copy(tmp_path: Path) -> Path:
    return tmp_path / "evidence" / "site-a-fed-1"


# ------------------------------------------------------------------------------------------------ the sites file
def sites_file(tmp_path: Path, site_lines: list, controller: list = (), fleet: str = "/login/fleet") -> Path:
    lines = ["[controller]", f'evidence = "{tmp_path / "evidence"}"', *controller, "[[site]]", 'label = "site-a"']
    lines += [f'fleet = "{fleet}"', "max_federations = 7", "allowance_units = 1e12", "rate_units_per_hour = 47.0"]
    path = tmp_path / "sites.toml"
    path.write_text("\n".join([*lines, *site_lines]) + "\n", encoding="utf-8")
    return path


SSH_ENV = ['env_file = "/login/site.env"']
SSH = ['transport = "ssh"', f'host = "{HOST}"', 'interpreter = "/login/py"', 'deploy = "/login/deploy"', *SSH_ENV]


def test_an_ssh_site_is_read_from_the_sites_file_and_its_host_is_never_shown(deploy, tmp_path):
    """The ssh site names its host and its login node's interpreter and deploy directory (and site.env and shard
    settings); the host is in no repr of the site."""
    path = sites_file(tmp_path, [*SSH, 'settings = "/login/settings.toml"'],
                      [f'settings = "{tmp_path / "settings.toml"}"'])  # fmt: skip
    _, (site,) = deploy.fleetcontrol.load_config(path)
    assert (site.transport, site.host, site.python, site.env_file) == ("ssh", HOST, "/login/py", "/login/site.env")
    assert (str(site.deploy), str(site.shard_settings)) == ("/login/deploy", "/login/settings.toml")
    assert site.settings.login_node is None  # a remote site is not on this host's login node
    assert HOST not in repr(site)


@pytest.mark.parametrize(
    ("lines", "controller", "match"),
    [
        (['transport = "ssh"', 'interpreter = "/p"', 'deploy = "/d"', *SSH_ENV], [], "names host"),
        (['transport = "ssh"', f'host = "{HOST}"', 'deploy = "/d"', *SSH_ENV], [], "names interpreter"),
        (['transport = "ssh"', f'host = "{HOST}"', 'interpreter = "/p"', *SSH_ENV], [], "names deploy"),
        # a command is never run on the login shell's defaults (the account's default Slurm account may be another's)
        (SSH[:4], [], "names env_file"),
        ([*SSH[:1], 'host = "-oProxyCommand=id"', *SSH[2:]], [], "not an ssh destination"),
        ([*SSH[:4], 'env_file = "site.env"'], [], "env_file must be an absolute path"),
        (SSH, ['settings = "/x/settings.toml"'], "names settings"),  # the login node's copy, for the worker's launch
        ([f'host = "{HOST}"'], [], "belong to an ssh site"),
    ],
)
def test_an_ssh_site_that_is_not_whole_is_refused_and_the_message_never_names_the_host(
    deploy, tmp_path, lines, controller, match
):
    with pytest.raises(deploy.fleetcontrol.ConfigError, match=match) as error:
        deploy.fleetcontrol.load_config(sites_file(tmp_path, lines, controller))
    assert HOST not in str(error.value)


# ------------------------------------------------------------------------------------------------ one call
def test_a_site_command_runs_over_ssh_from_the_fleet_with_site_env_and_every_word_unchanged(login):
    """C47: The argv the local transport would run, quoted word by word: it runs from the fleet's directory with
    site.env exported, and a word with spaces, a quote or a metacharacter is the same word there."""
    nasty = "/a root/it's $(touch pwned); `id` *"
    code, _, err = login.transport.run(login.config, login.config.script("shards.py", "verify", nasty), 20.0)
    assert (code, err) == (0, "")
    (call,) = login.calls("shards")
    assert call["argv"] == ["verify", nasty]
    assert (call["cwd"], call["account"]) == (str(login.fleet.resolve()), "an account")
    (ssh,) = login.fake.calls()
    assert ssh["host"] == HOST
    assert len(ssh["options"]) == 4
    assert not (Path.cwd() / "pwned").exists()


def test_a_missing_env_file_on_the_login_node_fails_the_call_and_nothing_runs_on_defaults(deploy, login):
    """site.env is required of an ssh site: where its file is missing or unreadable on the login node the call fails
    (the `.` returns non-zero before the command), so a read leaves the site unread and an action fails, never a run
    on the login shell's defaults (the account's default Slurm account may be another project's)."""
    gone = deploy.fleetexecute.SiteConfig(**{**vars(login.config), "env_file": str(login.base / "no-such-site.env")})
    code, _, _ = login.transport.run(gone, gone.script("shards.py", "collect", "--into", str(login.base / "x")), 20.0)
    assert code != 0
    assert login.calls("shards") == []  # the stub never ran
    view = deploy.fleetcontrol.observe_site(login.transport, gone, None)
    assert view.error
    executor = login.executor(login.base.parent)
    assert executor.site_steps(retire(), gone) is False
    assert not (login.scripts / "downed").exists()


def test_ssh_failing_is_said_without_the_host_and_a_remote_message_naming_it_is_scrubbed(login):
    """Exit 255 is ssh's own: the call failed, and its message (which names the host) is dropped. What a remote command
    prints on stderr is returned with the host replaced; its stdout, which the controller parses, is never altered."""
    login.fake.rule("shards.py", "fail")
    code, out, err = login.transport.run(login.config, login.config.script("shards.py", "x"), 20.0)
    assert (code, out, err) == (255, "", "its ssh call failed")
    login.fake.clear()
    noisy = [sys.executable, "-I", "-c", f"import sys; print({HOST!r}); print('refused by {HOST}', file=sys.stderr)"]
    code, out, err = login.transport.run(login.config, noisy, 20.0)
    assert HOST in out
    assert HOST not in err
    assert "<host>" in err


def test_a_short_alias_never_alters_what_the_controller_parses_and_is_removed_from_what_it_logs(deploy, login):
    """An alias such as `dead` (or one that is a word of a JSON key) must not corrupt collect's hash or an observed
    read: stdout is returned as it was, and the host is removed only from stderr and from what is logged."""
    site = deploy.fleetexecute.SiteConfig(**{**vars(login.config), "host": "dead"})
    digest = "dead" * 16
    code, out, err = deploy.fleetssh.SshSiteTransport({})._said(site, 0, f"{digest}\n", "dead said no")
    assert (code, out, err) == (0, f"{digest}\n", "<host> said no")
    assert deploy.fleetexecute.said_of(site, "", "refused by dead") == "refused by <host>"


def test_the_host_is_removed_from_every_logged_argv_even_where_a_path_holds_it(deploy, tmp_path, monkeypatch):
    """An alias that is also a directory name in the paths of the commands is still never logged: what is logged and
    printed has it removed, and what is sent and run is unchanged."""
    login = Login(tmp_path, monkeypatch, deploy, host="far-alias")  # the login node's directories are named for it
    executor = login.executor(tmp_path)
    assert executor.site_steps(retire(), login.config) is True
    assert "far-alias" not in json.dumps(login.log.lines)
    assert any("far-alias" in word for call in login.calls("shards") for word in call["argv"])  # what ran: unchanged
    rendered = deploy.fleetexecute.render(retire(), login.config, Path("/ev"), Path("/ledger.sqlite"))
    entry = deploy.fleetexecute.described(retire(), rendered, login.config)
    assert "far-alias" not in json.dumps(entry) + deploy.fleetexecute.printed(entry)


def test_a_call_past_its_bound_is_killed_with_its_group_and_raises(login):
    """A hung call (a child holding its pipes included) is killed at its bound: a failed call, not a wait."""
    login.fake.rule("shards.py", "hang_child")
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as error:
        login.transport.run(login.config, login.config.script("shards.py", "x"), 2.0)
    assert HOST not in str(error.value)
    assert time.monotonic() - began < 20
    assert login.fake.child_gone()


@pytest.mark.parametrize("action", ["fail", "hang_child", "garbage"])
def test_a_site_read_that_fails_hangs_or_is_garbled_is_dark_with_no_error_naming_the_host(
    deploy, login, monkeypatch, action
):
    monkeypatch.setattr(deploy.fleetcontrol, "OBSERVE_TIMEOUT_S", 2.0)
    login.fake.rule("fleetcontrol.py", action)
    view = deploy.fleetcontrol.observe_site(login.transport, login.config, None)
    assert view.error
    assert HOST not in view.error


# ------------------------------------------------------------------------------------------ no proof, no retirement
def joined_ledger(tmp_path: Path, clock: FakeClock) -> Path:
    path = tmp_path / "out" / "ledger.sqlite"
    ledger = Ledger.create(path, plan(tasks=1, conditions=("api-none",)), config(), clock=clock)
    ledger.join("site-a-fed-1", site="site-a", job_ids=("41", "42"), job_end_at=clock() + 86_400.0,
                fingerprint=FINGERPRINT, environment="e" * 64, kill_wait_s=180.0)  # fmt: skip
    ledger.close()
    return path


def one_tick(deploy, tmp_path: Path, login: Login, clock: FakeClock, ledger: Path) -> list:
    ssh = ['transport = "ssh"', f'host = "{HOST}"', f'interpreter = "{sys.executable}"', f'deploy = "{login.scripts}"',
           f'env_file = "{login.env_file}"']  # fmt: skip
    sites = sites_file(tmp_path, ssh, [f'log = "{tmp_path / "control.jsonl"}"'], fleet=str(login.fleet))
    hooks = deploy.fleetcontrol.Hooks(runner=lambda: None, clock=clock, sleep=lambda s: None, say=lambda line: None,
                                      identity=lambda site, member: (FINGERPRINT, {"a": "b"}))  # fmt: skip
    argv = ["control", "--ledger", str(ledger), "--sites", str(sites), "--once"]
    assert deploy.fleetcontrol.main(argv, dict(os.environ), hooks) == 0
    return [json.loads(line) for line in (tmp_path / "control.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("action", [None, "fail", "garbage"])
def test_a_site_that_cannot_be_read_over_ssh_gets_no_proof_and_no_retirement(deploy, login, tmp_path, action):
    """C47: Both held jobs ended, which a read of the site proves: the controller proves the federation's end. The
    same tick with the read failing, or garbled, proves nothing and retires nothing, and its log never names the
    host."""
    clock = FakeClock(1_800_000_000.0)
    fake = FakeSite(tmp_path / "far", deploy)
    fake.now = clock()
    times = (clock() - 900, clock() - 800, clock() - 100)
    fake.add_member("fed-1", jobs={"allocation": ("41", Job("COMPLETED", *times)),
                                   "sandbox": ("42", Job("COMPLETED", *times))})  # fmt: skip
    (login.scripts / "observed.json").write_text(json.dumps(observed(fake)), encoding="utf-8")
    if action is not None:
        login.fake.rule("fleetcontrol.py", action)
    ledger = joined_ledger(tmp_path, clock)
    entries = one_tick(deploy, tmp_path, login, clock, ledger)
    reader = Ledger.open(ledger, clock=clock)
    try:
        (fed,) = reader.federations()
    finally:
        reader.close()
    actions = [a["action"] for e in entries if e["kind"] == "tick" for a in e["actions"]]
    if action is None:
        assert fed.end_proof == "terminal"
        assert "ProveEnded" in actions
    else:
        assert fed.ended_at is None
        assert fed.end_proof is None
        assert not actions
        assert login.calls("shards") == []
        assert login.calls("fleet") == []
    assert HOST not in (tmp_path / "control.jsonl").read_text()


# ------------------------------------------------------------------------------------------------ the retirement
def test_a_retirement_over_ssh_collects_pulls_verifies_here_then_takes_down(deploy, login, tmp_path):
    """C47: collect (at the site, into its fleet's `collected/`), the pull of that copy to this host's evidence,
    verify-copy with this host's own shards.py over the pulled copy against collect's hash, and only then down: each
    only after the one before. No log line names the host."""
    executor = login.executor(tmp_path)
    assert executor.site_steps(retire(), login.config) is True
    assert [c["argv"][0] if c["script"] == "shards" else "down" for c in login.calls()] == ["collect", "down"]
    collect = login.calls("shards")[0]
    assert collect["argv"][collect["argv"].index("--root") + 1] == "/a root/it's"
    assert collect["argv"][-1] == str(login.fleet / "collected" / "fed-1")
    assert {p.name: p.read_bytes() for p in whole_copy(tmp_path).iterdir() if p.name != "archives.sha256.json"} == FILES
    assert sum(c["command"][0].startswith("cat -- ") for c in login.fake.calls()) == 3  # the manifest and its files
    results = [line for line in login.log.lines if line["kind"] == "result"]
    assert [r["ok"] for r in results] == [True] * 4
    assert [r["step"] for r in results] == [0, 1, 2, 3]
    assert HOST not in json.dumps(login.log.lines)
    assert (login.scripts / "downed").exists()


@pytest.mark.parametrize(
    ("trouble", "pulls"),
    [("collect-fails", 0), ("wrong-digest", 1), ("corrupt", 3), ("ssh-fails", 1), ("ssh-hangs", 1)],
)
def test_nothing_is_taken_down_unless_its_copy_is_here_whole(deploy, login, tmp_path, trouble, pulls):
    """C47: A collect that fails, a manifest that is not the one collect printed, a file that is not what the manifest
    names, an ssh call that fails and one that hangs past its bound each stop the retirement before `down`, leave no
    copy here, and are logged without the host."""
    if trouble == "ssh-fails":
        login.fake.rule("cat -- ", "fail")
    elif trouble == "ssh-hangs":
        login.fake.rule("cat -- ", "hang_child")
    else:
        login.mode(trouble)
    executor = login.executor(tmp_path)
    assert executor.site_steps(retire(), login.config) is False
    assert not (login.scripts / "downed").exists()
    assert not whole_copy(tmp_path).exists()
    assert not list((tmp_path / "evidence").glob(".*"))  # the staging directory is gone
    assert sum(c["command"][0].startswith("cat -- ") for c in login.fake.calls()) == pulls
    assert login.log.lines[-1]["ok"] is False
    assert HOST not in json.dumps(login.log.lines)
    if trouble == "ssh-hangs":
        assert login.fake.child_gone()


def test_a_retry_finds_its_pulled_copy_and_a_different_copy_in_its_place_is_never_replaced(deploy, login, tmp_path):
    """A down that failed leaves the verified copy: the retry takes it as it is. Another copy in its place stops it."""
    executor = login.executor(tmp_path)
    assert executor.site_steps(retire(), login.config) is True
    (login.scripts / "downed").unlink()
    assert executor.site_steps(retire(), login.config) is True  # collect says `already`; the copy here is the same
    assert (login.scripts / "downed").exists()
    (whole_copy(tmp_path) / "archives.sha256.json").write_text("another copy", encoding="utf-8")
    (login.scripts / "downed").unlink()
    assert executor.site_steps(retire(), login.config) is False
    assert not (login.scripts / "downed").exists()
    shutil.rmtree(whole_copy(tmp_path))
    whole_copy(tmp_path).symlink_to(
        login.fleet / "collected" / "fed-1"
    )  # a link to the site's own copy is no copy here
    assert executor.site_steps(retire(), login.config) is False
    assert not (login.scripts / "downed").exists()


def test_a_retirement_at_an_ssh_site_waits_for_a_reported_lease_whose_archive_is_not_here_yet(deploy, login, tmp_path):
    """The archive of a `reported` lease is pulled by the dispatcher over ssh: until it is, no down (a local site's
    retirement is as it was)."""
    fed = SimpleNamespace(label="site-a-fed-1", drained_at=1.0, ended_at=None)
    state = {"state": "reported"}
    ledger = SimpleNamespace(federations=lambda: [fed],
                             leases=lambda: [SimpleNamespace(lease_id=7, federation=fed.label, **state)])  # fmt: skip
    executor = login.executor(tmp_path, ledger)
    assert "7" in executor.retire_refused(retire())
    state["state"] = "counted"
    assert executor.retire_refused(retire()) is None


def test_the_dry_run_prints_the_ssh_retirement_as_the_steps_it_runs(deploy, login):
    """The pull and the verify-copy are in the dry run, in order, with where each runs; none names the host."""
    rendered = deploy.fleetexecute.render(retire(), login.config, Path("/ev"), Path("/ledger.sqlite"))
    entry = deploy.fleetexecute.described(retire(), rendered)
    assert entry["places"] == ["site", "pull", "here", "site"]
    printed = deploy.fleetexecute.printed(entry)
    assert printed.index("collect") < printed.index("# pulled to this host") < printed.index("verify-copy")
    assert printed.index("verify-copy") < printed.index("down")
    assert HOST not in printed + json.dumps(entry)
    with_channel = dataclasses.replace(login.config, channel=Path("/channels"))
    entry = deploy.fleetexecute.described(retire(), deploy.fleetexecute.render(retire(), with_channel, Path("/ev"),
                                                                              Path("/ledger.sqlite")))  # fmt: skip
    assert entry["places"] == ["site", "pull", "pull-places", "here", "site"]
    printed = deploy.fleetexecute.printed(entry)
    assert (
        printed.index("# pulled to this host")
        < printed.index("/channels/site-a-fed-1/places")
        < printed.index("verify-copy")
    )
    drain = Drain("site-a", "fed-1", "site-a-fed-1", "its end is near")
    (step,) = deploy.fleetexecute.render(drain, login.config, Path("/ev"), Path("/ledger.sqlite")).steps
    assert step[0] == sys.executable
    assert str(login.scripts) not in step[3]  # the ledger is this host's: never the login node's interpreter or deploy


# ------------------------------------------------------------------------------------ the places of a retired member
def lay_place(channel: Path, lease_id: int = 1, snapshots: int = 3) -> Path:
    """A lease's place on the site's shared file system, as a worker leaves it: snapshots and its spend journal."""
    place = channel / "site-a-fed-1" / "places" / f"lease-{lease_id:012d}"
    for number in range(1, snapshots + 1):
        (place / "snapshots" / f"snap-{number:06d}").mkdir(parents=True)
        (place / "snapshots" / f"snap-{number:06d}" / "state.bin").write_text(f"store {number}", encoding="utf-8")
    (place / "spend.jsonl").write_text('{"kind": "header"}\n', encoding="utf-8")
    return place


def retired_after_a_kill(deploy, login, tmp_path, env) -> SimpleNamespace:
    """A federation killed in the middle of its real lease at an ssh site, drained and retired (its root evidence and
    its channel's places pulled, down), its end proven and KillWait passed: only the settle is left to run."""
    leased, kept = ran_for_real(tmp_path, env, "site-a-fed-1")
    killed_mid_lease(kept, tmp_path)
    ledger, clock, lease = leased.ledger, leased.now, leased.lease
    config = dataclasses.replace(login.config, channel=Path(leased.site.root))
    log = deploy.fleetexecute.Log(None)
    evidence = tmp_path / "evidence"
    spend = deploy.fleetexecute.dead_store_reader(
        deploy.fleetidentity.locator(evidence, login.transport), leased.loaded
    )
    executor = deploy.fleetexecute.Executor(ledger, tmp_path / "ledger.sqlite", {SITE_LABEL: config}, login.transport,
                                            evidence, log, spend=spend, clock=lambda: clock[0])  # fmt: skip
    clock[0] += 4000.0  # past the lease's term: no heartbeat, it expires
    ledger.observe_site(SITE_LABEL, SiteReading())
    ledger.drain("site-a-fed-1")
    retire = Retire(SITE_LABEL, "fed-1", "site-a-fed-1", "/a root", True, "drained, and no lease in flight")
    assert executor.retire_refused(retire) is None
    assert executor.site_steps(retire, config) is True
    assert (login.scripts / "downed").exists()
    ledger.prove_ended("site-a-fed-1", "cancelled")  # the down was verified: the end is proven only now
    clock[0] += 181.0  # KillWait
    lost = Settle(SITE_LABEL, "fed-1", "site-a-fed-1", lease.lease_id, None, "lost")  # the member's root is gone

    def pulled_only():  # the read of the copy the retirement pulled, as the settle makes it when the channel gives none
        read = deploy.fleetexecute.dead_store_reader(deploy.fleetidentity.locator(evidence), leased.loaded)
        return read(dataclasses.replace(config, channel=None), lost)

    return SimpleNamespace(kept=kept, log=log, executor=executor, lost=lost, lease=lease, ledger=ledger,
                           pulled_only=pulled_only)  # fmt: skip


def settled(after: SimpleNamespace, tmp_path: Path) -> dict:
    """The settle's result line, once the lease is settled: the ledger holds that charge, and no lease of the run is
    left unsettled for the merge by run to refuse."""
    done = [r for r in after.log.lines if r.get("action") == "Settle"][-1]
    assert done["ok"] is True, done
    assert after.ledger.lease(after.lease.lease_id).charge_micro == done["spend_micro"]
    snapshot = tmp_path / "ledger-snapshot.sqlite"
    take_ledger_snapshot(after.ledger.path, snapshot)
    data = snapshot.read_bytes()
    assert read_ledger(data, hashlib.sha256(data).hexdigest()).unsettled(after.lease.run_key) == []
    return done


def test_c47_a_member_killed_mid_lease_settles_from_the_places_its_retirement_pulled_if_the_channel_is_gone(
    deploy, login, tmp_path, clean_environment
):
    """C47: a federation killed in the middle of its lease is drained and retired with its root gone; the retirement
    pulled its channel's places beside its evidence, so when the channel's place is gone by the settle the lease is
    settled at the journal's spend read from the pulled place (never left charged its whole grant), and the merge by
    run has no unsettled lease of the run to refuse."""
    after = retired_after_a_kill(deploy, login, tmp_path, clean_environment)
    shutil.rmtree(after.kept.parent)  # the channel cleaned up since
    after.executor.in_ledger(after.lost)
    done = settled(after, tmp_path)
    assert done["source"] == "pulled"
    assert 0 < done["spend_micro"] < 5_000_000  # the journal's spend, not the grant of the model (5 USD)


def test_c47_a_spend_appended_to_the_channels_journal_after_the_retirements_pull_is_counted_in_the_settle(
    deploy, login, tmp_path, clean_environment
):
    """C47: the settle reads the member's place from its channel whatever its root, over ssh, in preference to the
    copy pulled at the retirement: by then its job is proven ended and KillWait has passed, so the journal there is
    the final one, and a probe charge written after the pull is in the lease's charge."""
    after = retired_after_a_kill(deploy, login, tmp_path, clean_environment)
    journal = after.kept / "spend.jsonl"
    header = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    probe = {"kind": "probe", "campaign": header["campaign"], "shard": header["shard"], "event_id": 10**6,
             "model": after.lease.model, "usd": 0.25}  # fmt: skip
    pulled_spend = after.pulled_only()[0]
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(probe, sort_keys=True) + "\n")
    after.executor.in_ledger(after.lost)
    done = settled(after, tmp_path)
    assert done["source"] == "root"
    assert done["spend_micro"] == pulled_spend + 250_000


def test_c47_a_failed_read_of_the_channel_leaves_the_lease_charged_and_never_settles_it_from_the_older_pulled_copy(
    deploy, login, tmp_path, clean_environment
):
    """C47: the pulled copy was taken before the job's end was proven, so a settle from it after a call at the channel
    failed or hung could miss a spend written since, and a settled charge is final: the lease stays charged its grant
    and the next tick, the call answering, settles it from the channel's journal."""
    after = retired_after_a_kill(deploy, login, tmp_path, clean_environment)
    assert after.pulled_only() is not None  # the pulled copy is whole and readable
    login.fake.rule("echo yes", "fail")
    grant = after.ledger.lease(after.lease.lease_id).charge_micro
    after.executor.in_ledger(after.lost)
    assert after.ledger.lease(after.lease.lease_id).charge_micro == grant
    assert [r["ok"] for r in after.log.lines if r.get("action") == "Settle"] == [False]
    login.fake.clear()
    after.executor.in_ledger(after.lost)
    assert settled(after, tmp_path)["source"] == "root"


@pytest.mark.parametrize(("call", "trouble"), [("tar -C", "fail"), ("echo yes", "fail"), ("echo yes", "garbage")])
def test_c47_a_place_that_cannot_be_pulled_stops_the_retirement_before_down_and_a_retry_pulls_it_whole(
    deploy, login, tmp_path, call, trouble
):
    """C47: the places of the member's leases are pulled after its evidence and before verify-copy and down (the
    newest two snapshots of each place and its journal): a snapshot that cannot be streamed, a directory that cannot
    be told from one that is not there, and an answer that is not whole each stop the retirement with nothing taken
    down and no part of the places here, and the retry, over the evidence already here, pulls them whole."""
    config = dataclasses.replace(login.config, channel=tmp_path / "channels")
    lay_place(config.channel)
    login.fake.rule(call, trouble)
    executor = login.executor(tmp_path)
    assert executor.site_steps(retire(), config) is False
    assert not (login.scripts / "downed").exists()
    assert not (whole_copy(tmp_path) / "places").exists()
    assert not list(whole_copy(tmp_path).glob(".*"))  # no staging directory is left in the copy
    assert login.log.lines[-1]["ok"] is False
    assert HOST not in json.dumps(login.log.lines)
    login.fake.clear()
    assert executor.site_steps(retire(), config) is True
    assert (login.scripts / "downed").exists()
    place = whole_copy(tmp_path) / "places" / "lease-000000000001"
    assert sorted(p.name for p in (place / "snapshots").iterdir()) == ["snap-000002", "snap-000003"]
    assert (place / "snapshots" / "snap-000003" / "state.bin").read_text(encoding="utf-8") == "store 3"
    assert (place / "spend.jsonl").read_text(encoding="utf-8") == '{"kind": "header"}\n'


def test_c47_a_member_with_no_places_pulls_none_and_is_still_taken_down(deploy, login, tmp_path):
    """C47: a joined member whose channel holds no place (no lease ever ran there) has nothing to pull, and that is
    no failure: it goes down with its evidence copy as any other."""
    config = dataclasses.replace(login.config, channel=tmp_path / "channels")
    executor = login.executor(tmp_path)
    assert executor.site_steps(retire(), config) is True
    assert (login.scripts / "downed").exists()
    assert not (whole_copy(tmp_path) / "places").exists()


# ------------------------------------------------------------------------------------------------ join and worker
def test_a_joining_federations_identity_is_read_on_the_login_node_and_its_worker_launched_from_the_sites_settings(
    deploy, login
):
    """The identity is read by `fleetidentity.py read` over ssh with the login node's own settings file (a failed call
    reads none, so the join waits); the worker's launch names that same file, never the controller's."""
    read = deploy.fleetidentity.identity_reader(Path("/this/hosts/settings.toml"), {"site-a": login.config},
                                                transport=login.transport)  # fmt: skip
    assert read("site-a", "fed-1") == ("f" * 64, {"core_commit": "0123abcd"}, None)  # C50: a read with no platform
    (call,) = login.calls("identity")
    assert call["argv"][call["argv"].index("--settings") + 1] == str(login.base / "settings.toml")
    login.fake.rule("fleetidentity.py", "fail")
    assert (
        deploy.fleetidentity.identity_reader(Path("/x"), {"site-a": login.config}, transport=login.transport)(
            "site-a", "fed-1"
        )
        is None
    )
    action = SimpleNamespace(member="fed-1", label="site-a-fed-1", job_end_at=1.0)
    site = deploy.fleetexecute.SiteConfig(**{**vars(login.config), "channel": Path("/login/channels")})
    (argv,) = deploy.fleetexecute.worker_launch(action, site, Path("/this/hosts/settings.toml"))
    assert argv[argv.index("--settings") + 1] == str(login.base / "settings.toml")


def test_the_dispatcher_of_a_remote_site_goes_over_ssh_with_the_core_on_its_login_node(deploy, login):
    """`dispatcher_of` gives the remote site an ssh command (never the host in its repr) and its interpreter."""
    controller = deploy.fleetcontrol.Controller(Path("/ev"), archives=Path("/archives"))
    site = deploy.fleetexecute.SiteConfig(**{**vars(login.config), "channel": Path("/login/channels")})
    dispatcher = deploy.fleetcontrol.dispatcher_of(
        object(), controller, [site], deploy.fleetcontrol.Hooks(lambda: None)
    )
    named = dispatcher.sites["site-a"]
    assert named.ssh[-1] == HOST
    assert named.python == (sys.executable, "-I", "-B")
    assert HOST not in repr(named)
    assert HOST not in repr(dispatcher.transport)
