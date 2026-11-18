"""#462 step 6 (ssh): the dispatcher's calls over a fake `ssh` executable on PATH. The fake parses the argv as ssh does,
checks the options, logs each call, and hands the command string to `sh -c` as sshd does, so what a word becomes on
the login node is what the login shell makes of it. A site that cannot be read is unread, never ended."""

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest
from channel_support import channel_of, complete
from elastic_support import SITE, assert_invariants, join, make, plan
from fake_ssh_bin import install_fake_ssh

import expcore
from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.dispatch import FETCH_TRIES, TICK_S, Dispatcher
from expcore.campaign.elastic.exchange import FORMAT
from expcore.campaign.elastic.transport import (
    SSH_OPTIONS,
    Missing,
    RoutedTransport,
    Site,
    SshTransport,
    Unreachable,
    bounded_run,
    remote_command,
    ssh_argv,
)

HOST = "zz-login-7f3a.invalid"
ONE_RUN = {"runs": plan(tasks=1, conditions=("api-none",))}
NASTY = ["a b", "$(touch pwned)", "`id`", "x;y", "it's", '"q"', "*", "~", "-n", "a\nb", "&&", "\\", "$HOME", "", "!x"]
REAL_SOURCE = str(Path(expcore.__file__).resolve().parents[1])


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", REAL_SOURCE)  # the exchange verb runs from this checkout, not an installed core
    return install_fake_ssh(tmp_path / "bin", monkeypatch, HOST)


def site_over_ssh(tmp_path: Path) -> Site:
    return Site(name=SITE, root=str(tmp_path / "channels"), ssh=ssh_argv(HOST), python=(sys.executable, "-B"))


def on_login_node(argv: list[str], **bounds) -> subprocess.CompletedProcess:
    """One command through the fake ssh, built as every ssh call is."""
    command = remote_command(argv, **{k: v for k, v in bounds.items() if k in ("cwd", "env_file")})
    return bounded_run([*ssh_argv(HOST), command], capture_output=True, timeout=bounds.get("timeout", 20))


def ticks(dispatch, clock, count):
    for _ in range(count):
        clock.advance(TICK_S)
        dispatch.tick()


def test_the_options_are_the_four_and_never_weaken_host_key_checking(tmp_path, fake):
    """C47: The ssh argv holds exactly the four options then `--` and the host; the fake saw the same, and nothing in it
    names a host-key setting."""
    argv = ssh_argv(HOST)
    assert argv == ("ssh", *SSH_OPTIONS, "--", HOST)
    assert argv.count("-o") == 4
    on_login_node([sys.executable, "-I", "-c", "pass"])
    (call,) = fake.calls()
    assert call["options"] == ["BatchMode=yes", "ConnectTimeout=20", "ServerAliveInterval=20", "ServerAliveCountMax=3"]
    assert call["host"] == HOST
    flat = " ".join([*argv, *call["options"]]).lower()
    for weakened in ("stricthostkeychecking", "userknownhostsfile", "accept-new", "hashknownhosts", "=no"):
        assert weakened not in flat


@pytest.mark.parametrize("host", ["-oProxyCommand=id", "a b", "", "host;id", "x\ny", "$(id)"])
def test_a_host_that_could_be_an_option_or_split_by_a_shell_is_refused(host):
    with pytest.raises(ValueError, match="ssh destination or alias"):
        ssh_argv(host)


def test_an_argument_with_spaces_or_metacharacters_reaches_the_remote_argv_unchanged(tmp_path, fake):
    """C47: Every word is quoted, so the login shell hands each one to the program as it was."""
    program = "import json, sys; print(json.dumps(sys.argv[1:]))"
    done = on_login_node([sys.executable, "-I", "-c", program, *NASTY])
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == NASTY
    assert not (Path.cwd() / "pwned").exists()


def test_a_command_runs_from_the_fleet_directory_with_site_env_or_not_at_all(tmp_path, fake):
    """The `cd` and the sourcing of site.env come first and gate the command: its directory and variables are the
    and a missing directory or env file means the command never runs."""
    fleet = tmp_path / "a fleet's dir"
    fleet.mkdir()
    env_file = tmp_path / "site env.sh"
    env_file.write_text("HPC_PARTITION='some part'\n", encoding="utf-8")
    probe = "import os; print(os.getcwd()); print(os.environ.get('HPC_PARTITION'))"
    done = on_login_node([sys.executable, "-I", "-c", probe], cwd=str(fleet), env_file=str(env_file))
    assert done.stdout.decode().splitlines() == [str(fleet.resolve()), "some part"]
    marker = tmp_path / "ran"
    touch = [sys.executable, "-I", "-c", f"open({str(marker)!r}, 'w')"]
    assert on_login_node(touch, cwd=str(tmp_path / "absent")).returncode != 0
    assert on_login_node(touch, cwd=str(fleet), env_file=str(tmp_path / "absent.env")).returncode != 0
    assert not marker.exists()


def test_a_tick_over_ssh_issues_exchanges_and_fetches_and_the_archive_hash_settles_the_lease(tmp_path, fake):
    """C47: The dispatcher over the real ssh transport: the lease is issued and lands in the inbox in the exchange
    completion is read, the archive is pulled by `cat` into archives/ and its hash settles the lease."""
    ledger, clock = make(tmp_path, **ONE_RUN)
    join(ledger, clock, "fed-1")
    site = site_over_ssh(tmp_path)
    dispatch = Dispatcher(ledger, [site], SshTransport(), tmp_path / "out" / "leases")
    ticks(dispatch, clock, 2)
    (lease,) = ledger.leases()
    assert lease.state == "sent"
    assert (channel_of(site, "fed-1") / ch.INBOX / ch.lease_name(lease.lease_id)).is_file()
    complete(site, "fed-1", lease, archive=b"an archive that is pulled over ssh")
    ticks(dispatch, clock, 3)
    assert ledger.lease(lease.lease_id).state == "counted"
    pulled = tmp_path / "out" / "leases" / ch.archive_name(lease.lease_id)
    assert pulled.read_bytes() == b"an archive that is pulled over ssh"
    commands = [call["command"][0] for call in fake.calls()]
    assert sum(" -m expcore.campaign.elastic.exchange" in c for c in commands) == 5
    assert sum(c.startswith("cat -- ") for c in commands) == 1
    assert all(call["options"] == list(SSH_OPTIONS[1::2]) and call["host"] == HOST for call in fake.calls())
    assert_invariants(ledger)


@pytest.mark.parametrize("action", ["fail", "hang", "garbage"])
def test_an_exchange_that_fails_hangs_or_is_not_a_reply_leaves_the_site_unread(tmp_path, fake, action):
    """C47: The call failing, its bound passing (the call killed, a child holding its pipes included) and a reply that
    not whole each make the site unread for the tick: no reading reaches the ledger, nothing is marked sent, and no
    error names the host."""
    ledger, clock = make(tmp_path, **ONE_RUN)
    join(ledger, clock, "fed-1")
    site = site_over_ssh(tmp_path)
    dispatch = Dispatcher(ledger, [site], SshTransport(exchange_timeout_s=2.0), tmp_path / "out" / "leases")
    ticks(dispatch, clock, 1)  # the first tick reads the site and issues nothing
    fake.rule(" -m expcore.campaign.elastic.exchange", "hang_child" if action == "hang" else action)
    began = time.monotonic()
    clock.advance(TICK_S)
    done = dispatch.tick()[SITE]
    assert time.monotonic() - began < 20
    assert not done.read
    assert done.sent == []
    assert done.settled == {}
    assert [lease.state for lease in ledger.leases()] == ["issued"]  # issued before the call, never confirmed sent
    if action == "hang":
        assert fake.child_gone()
    with pytest.raises(Unreachable) as error:
        SshTransport(exchange_timeout_s=2.0).exchange(
            site, {"format": FORMAT, "site": SITE, "root": site.root, "contact": {}, "federations": []}
        )
    assert HOST not in str(error.value)
    assert_invariants(ledger)


def test_a_failed_pull_is_the_site_unreachable_and_a_hung_one_is_killed_and_leaves_no_file(tmp_path, fake):
    """`fetch` over ssh: a file the login node does not have is Missing; ssh's own failure and a pull past its bound are
    Unreachable, and none leaves a partial file."""
    site = site_over_ssh(tmp_path)
    remote = tmp_path / "remote.tar"
    remote.write_bytes(b"whole")
    transport = SshTransport(fetch_timeout_s=2.0)
    transport.fetch(site, str(remote), tmp_path / "got.tar")
    assert (tmp_path / "got.tar").read_bytes() == b"whole"
    with pytest.raises(Missing):
        transport.fetch(site, str(tmp_path / "absent.tar"), tmp_path / "never.tar")
    kept = tmp_path / "kept.tar"
    kept.write_bytes(b"a copy already here")
    with pytest.raises(FileExistsError):
        transport.fetch(site, str(remote), kept)  # never replaced, and never removed by the failed attempt
    assert kept.read_bytes() == b"a copy already here"
    fake.rule("cat -- ", "fail")
    with pytest.raises(Unreachable) as error:
        transport.fetch(site, str(remote), tmp_path / "failed.tar")
    assert HOST not in str(error.value)
    fake.clear()
    fake.rule("cat -- ", "hang_child")
    with pytest.raises(Unreachable):
        transport.fetch(site, str(remote), tmp_path / "hung.tar")
    assert fake.child_gone()
    assert not any((tmp_path / name).exists() for name in ("never.tar", "failed.tar", "hung.tar"))


def test_an_archive_whose_pull_keeps_failing_is_never_a_try_against_it(tmp_path, fake):
    """A pull whose ssh fails is no try: after the site answers again, the archive verifies and settles the lease."""
    ledger, clock = make(tmp_path, **ONE_RUN)
    join(ledger, clock, "fed-1")
    site = site_over_ssh(tmp_path)
    dispatch = Dispatcher(ledger, [site], SshTransport(), tmp_path / "out" / "leases")
    ticks(dispatch, clock, 2)
    (lease,) = ledger.leases()
    complete(site, "fed-1", lease)
    fake.rule("cat -- ", "fail")
    ticks(dispatch, clock, FETCH_TRIES + 1)
    assert ledger.lease(lease.lease_id).state == "reported"
    fake.clear()
    ticks(dispatch, clock, 1)
    assert ledger.lease(lease.lease_id).state == "counted"


def test_the_host_is_in_no_log_no_record_and_no_site_repr(tmp_path, fake, caplog):
    """C47: A failing site, a pull and a normal tick leave the host in no log line, no file under the work directory
    ledger included) and not in the site's repr."""
    caplog.set_level(logging.DEBUG)
    ledger, clock = make(tmp_path, **ONE_RUN)
    join(ledger, clock, "fed-1")
    site = site_over_ssh(tmp_path)
    dispatch = Dispatcher(ledger, [site], SshTransport(), tmp_path / "out" / "leases")
    fake.rule(" -m expcore.campaign.elastic.exchange", "fail")
    ticks(dispatch, clock, 2)  # unread: the warning says the site's label
    fake.clear()
    ticks(dispatch, clock, 2)
    (lease,) = ledger.leases()
    complete(site, "fed-1", lease)
    ticks(dispatch, clock, 2)
    assert HOST not in caplog.text
    assert "site-a not read" in caplog.text
    assert HOST not in repr(site)
    ledger.close()
    for path in (tmp_path / "out").rglob("*"):
        if path.is_file():
            assert HOST.encode() not in path.read_bytes(), path.name


def test_the_routed_transport_goes_over_ssh_for_a_site_with_an_ssh_command_and_here_for_the_rest(tmp_path, fake):
    """One transport for a sites file with a local site beside a remote one: only the remote one is an ssh call."""
    remote_site = site_over_ssh(tmp_path)
    here = Site(name="site-b", root=str(tmp_path / "b"), python=(sys.executable, "-B"))
    transport = RoutedTransport({"site-b": str(tmp_path)})
    source = tmp_path / "archive.tar"
    source.write_bytes(b"x")
    transport.fetch(remote_site, str(source), tmp_path / "a.tar")
    transport.fetch(here, str(source), tmp_path / "b.tar")
    assert [call["command"][0].split()[0] for call in fake.calls()] == ["cat"]
    assert (tmp_path / "a.tar").read_bytes() == (tmp_path / "b.tar").read_bytes() == b"x"
