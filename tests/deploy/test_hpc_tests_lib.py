"""The HPC test runner's local logic (deploy/hpc-tests): the settings site.env gives, the argv of a test step and the
environment allowlist it runs behind, and the parsing of what a step sends back and of the login node's listing.

Everything here is pure: no ssh, no Slurm, no node. The end-to-end path through fakes is test_hpc_tests_run.py, and
the login node's script test_hpc_tests_alloc.py.
"""

import base64
import hashlib
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "hpc-tests"


@pytest.fixture(scope="module")
def hpct():
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(DEPLOY))
        import hpctestlib
        import hpctestparse

        yield SimpleNamespace(lib=hpctestlib, parse=hpctestparse)


@pytest.fixture
def site(hpct):
    return hpct.lib.site_from({"HPCT_HOST": "login-alias", "HPCT_UV": "/opt/tools/uv"})


def step(hpct, site, **change):
    fields = {
        "job": "4242",
        "name": "hpct-run1",
        "cpus": 4,
        "memory_mib": 6144,
        "minutes": 60,
        "env": hpct.lib.allowlist(hpct.lib.step_environment(site)),
        "command": hpct.lib.boot_command(site.node_root, "run1", "a" * 64, "run"),
    }
    return hpct.lib.Step(**{**fields, **change})


def test_o64_a_step_is_an_exact_step_of_the_job_with_its_own_cores_memory_and_time_never_overlap(hpct, site):
    """O64: a test step is `srun --jobid --exact` with its own cores, memory and time limit, run from `/` with only
    PATH exported, and its command behind `env -i`; it never shares CPUs with another step (`--overlap`)."""
    argv = step(hpct, site).argv()
    assert argv[:13] == [
        "srun",
        "--jobid=4242",
        "--exact",
        "-N1",
        "-n1",
        "-c",
        "4",
        "--mem=6144M",
        "--time=60",
        "-J",
        "hpct-run1",
        "--chdir=/",
        "--export=PATH",
    ]
    assert argv[13:15] == ["/usr/bin/env", "-i"]
    assert "--overlap" not in argv
    assert not any(word.startswith("--mem=0") for word in argv)


@pytest.mark.parametrize(
    "change",
    [{"job": "42;rm"}, {"job": ""}, {"name": "-J"}, {"name": "a b"}, {"cpus": 0}, {"memory_mib": 0}, {"minutes": 0}],
)
def test_a_step_with_a_bad_field_is_refused(hpct, site, change):
    with pytest.raises(hpct.lib.HpcTestsError):
        step(hpct, site, **change).argv()


def test_o64_the_step_environment_is_the_allowlist_whatever_the_login_shell_holds(hpct, site, monkeypatch):
    """O64: the environment a step starts with is PATH, the locale and TMPDIR on the node's tmpfs; nothing of the
    login shell (an agent socket, a site variable, the runner's own settings) is in it."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    monkeypatch.setenv("HPCT_ACCOUNT", "grant-1")
    monkeypatch.setenv("SLURM_CONF", "/etc/slurm.conf")
    env = hpct.lib.step_environment(site)
    assert set(env) == {"PATH", "LC_ALL", "LANG", "TMPDIR"}
    assert env["TMPDIR"] == "/tmp"
    words = hpct.lib.allowlist(env)
    assert all(word.split("=", 1)[0] in env for word in words)


@pytest.mark.parametrize("pair", [("PATH", "/bin; rm -rf /"), ("PATH", "$HOME/bin"), ("lower", "x"), ("TOKEN", "a b")])
def test_the_allowlist_refuses_a_word_that_could_carry_shell_or_a_secret(hpct, pair):
    with pytest.raises(hpct.lib.HpcTestsError):
        hpct.lib.allowlist(dict([pair]))


def test_o64_nothing_of_site_env_but_the_job_root_and_path_is_on_the_step_command_line(hpct):
    """O64: the remote command carries the job id, the node's tmpfs root and the step's PATH from site.env, and no
    other setting: not the host, the account, the partition, the login directory or the uv path."""
    values = {
        "HPCT_HOST": "secret-alias",
        "HPCT_ACCOUNT": "grant-account-x",
        "HPCT_PARTITION": "part-y",
        "HPCT_LOGIN_DIR": "login-dir-z",
        "HPCT_UV": "/home/someone/tools/uv",
        "HPCT_NODE_ROOT": "/tmp/node-root-w",
    }
    site = hpct.lib.site_from(values)
    line = hpct.lib.remote_command(
        step(hpct, site, command=hpct.lib.boot_command(site.node_root, "r1", "b" * 64, "run")).argv()
    )
    for key in ("HPCT_HOST", "HPCT_ACCOUNT", "HPCT_PARTITION", "HPCT_LOGIN_DIR", "HPCT_UV"):
        assert values[key] not in line
    assert "/tmp/node-root-w" in line
    assert "secret-alias" not in repr(site)


def test_the_remote_command_quotes_every_word_and_execs_srun(hpct, site):
    argv = step(hpct, site).argv()
    assert "\n" in hpct.lib.BOOT
    line = hpct.lib.remote_command(argv)
    login, _, rest = line.partition("; exec ")
    assert login.startswith('printf "HPCT-LOG login %s\\n" "$(hostname -s | sha256sum | cut -c1-8)"')
    assert shlex.split(rest) == argv
    with pytest.raises(hpct.lib.HpcTestsError, match="NUL"):
        hpct.lib.remote_command(["srun", "a\0b"])


@pytest.mark.parametrize("root", ["/home/user/x", "/tmp", "/tmp/a/b", "/scratch/x", "/tmp/../etc", "tmp/x"])
def test_o64_a_node_root_off_the_tmpfs_is_refused_before_anything_runs(hpct, root):
    """O64: the node's root is one directory under /tmp or /dev/shm; any other path is refused locally (and the
    step's boot refuses one that is not on a tmpfs, or goes through a link)."""
    with pytest.raises(hpct.lib.HpcTestsError):
        hpct.lib.boot_command(root, "run1", "a" * 64, "run")
    with pytest.raises(hpct.lib.HpcTestsError):
        hpct.lib.site_from({"HPCT_HOST": "h", "HPCT_NODE_ROOT": root})


def test_the_boot_refuses_a_root_that_is_no_tmpfs_on_the_node_too(hpct):
    assert '[ "$(stat -f -c %T "$root")" = tmpfs ] || fail' in hpct.lib.BOOT
    assert '[ "$(readlink -f "$root")" = "$root" ] || fail' in hpct.lib.BOOT


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("HPCT_HOST=h\nHPCT_NODE=x\n", "does not know"),
        ("HPCT_HOST=$(id)\n", "quote"),
        ("HPCT_HOST=-oProxyCommand=x\n", "ssh destination"),
        ("HPCT_UV=/opt/uv\n", "HPCT_HOST"),
        ("HPCT_HOST=h\nHPCT_MEM=6\n", "memory"),
        ("HPCT_HOST=h\nHPCT_CPUS=0\n", "HPCT_CPUS"),
        ("not a line\n", "KEY=value"),
    ],
)
def test_a_site_env_that_is_not_as_the_runner_reads_it_is_refused_naming_the_reason(hpct, text, message):
    with pytest.raises(hpct.lib.HpcTestsError, match=message):
        hpct.lib.site_from(hpct.lib.parse_env_file(text))


def test_site_env_values_may_be_quoted_and_the_host_may_come_from_a_file(hpct):
    values = hpct.lib.parse_env_file("# a comment\nexport HPCT_HOST_FILE='/x/alias'\nHPCT_MEM=\"30G\"  # note\n")
    site = hpct.lib.site_from(values, host_file_text="alias-1\n")
    assert site.host == "alias-1"
    assert site.mem == "30G"


def test_the_login_env_carries_what_sbatch_needs_and_needs_an_account(hpct, site):
    with pytest.raises(hpct.lib.HpcTestsError, match="HPCT_ACCOUNT"):
        hpct.lib.login_env(site)
    full = hpct.lib.site_from({"HPCT_HOST": "h", "HPCT_ACCOUNT": "acct", "HPCT_PARTITION": "std"})
    text = hpct.lib.login_env(full)
    assert "HPCT_ACCOUNT=acct\n" in text
    assert "HPCT_HOURS=12\n" in text
    assert "HPCT_HOST" not in text


def test_the_node_settings_hold_only_checked_values(hpct):
    text = hpct.lib.node_settings(
        sha="c" * 40, uv="/opt/uv", uv_version="0.11.19", python="3.13", timeout_s=600, cores=4, keep=False
    )
    assert text.splitlines()[0] == "HPCT_SHA=" + "c" * 40
    with pytest.raises(hpct.lib.HpcTestsError):
        hpct.lib.node_settings(sha="HEAD", uv="/opt/uv", uv_version="", python="3.13", timeout_s=1, cores=1, keep=False)


def frame(data: bytes, rc: int = 0, sha: str = "", size: int = -1) -> list[str]:
    body = base64.encodebytes(data).decode().splitlines()
    head = f"HPCT-RESULT {sha or hashlib.sha256(data).hexdigest()} {len(data) if size < 0 else size}"
    return ["HPCT-LOG clone ok", head, *body, f"HPCT-END {rc}", "trailing noise"]


def feed(hpct, lines):
    reader = hpct.parse.StreamParser()
    for line in lines:
        reader.feed(line + "\n")
    return reader


def test_o64_the_results_archive_comes_back_only_with_its_sha256_and_size(hpct):
    """O64: what a step streams back is accepted only when its SHA-256 and size equal its header's; pytest's own
    exit status is the one in the end marker."""
    data = bytes(range(256)) * 40
    reader = feed(hpct, frame(data, rc=1))
    assert reader.done
    got = reader.result()
    assert (got.archive, got.rc, got.logs) == (data, 1, ["clone ok"])
    with pytest.raises(hpct.lib.HpcTestsError, match="sha256"):
        feed(hpct, frame(data, sha="0" * 64)).result()
    with pytest.raises(hpct.lib.HpcTestsError, match="bytes"):
        feed(hpct, frame(data, size=len(data) - 1)).result()


def test_a_boot_refusal_ends_the_stream_and_a_cut_stream_has_no_archive(hpct):
    refused = feed(hpct, ["HPCT-FAIL boot: the node root is not on a tmpfs"]).result()
    assert refused.failure == "boot: the node root is not on a tmpfs"
    cut = feed(hpct, frame(b"abc")[:2]).result()
    assert cut.archive is None
    assert cut.rc is None
    with pytest.raises(hpct.lib.HpcTestsError, match="exit status"):
        feed(hpct, ["HPCT-RESULT " + "a" * 64 + " 3", "YWJj", "HPCT-END x"])


LOG = """\
tests/test_a.py ..s.F
=========================== short test summary info ============================
SKIPPED [1] tests/test_a.py:9: needs openssl
FAILED tests/test_a.py::test_e - assert 1 == 2
=================== 1 failed, 3 passed, 1 skipped in 2.31s ====================
"""


def test_the_summary_line_and_the_ra_section_are_read_from_pytest_output(hpct):
    assert hpct.parse.summary_line(LOG) == "1 failed, 3 passed, 1 skipped in 2.31s"
    assert hpct.parse.summary_line("12 passed in 0.50s\n") == "12 passed in 0.50s"
    assert hpct.parse.summary_line("==== no tests ran in 0.01s ====\n") == "no tests ran in 0.01s"
    assert hpct.parse.summary_line("collecting ...\n") is None
    assert hpct.parse.short_summary(LOG) == [
        "SKIPPED [1] tests/test_a.py:9: needs openssl",
        "FAILED tests/test_a.py::test_e - assert 1 == 2",
    ]


def test_the_junit_totals_sum_every_suite(hpct):
    xml = (
        b'<testsuites><testsuite name="pytest" errors="1" failures="2" skipped="3" tests="10" time="1.5"/>'
        b'<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="2" time="0.5"/></testsuites>'
    )
    assert hpct.parse.junit_counts(xml) == {"tests": 12, "failures": 2, "errors": 1, "skipped": 3, "time": 2.0}
    with pytest.raises(hpct.lib.HpcTestsError):
        hpct.parse.junit_counts(b"<testsuites")


LISTING = """\
NOW 1000
JOB 11 expcore-tests RUNNING 100 5000 16 30G
JOB 12 expcore-tests RUNNING 900 9000 16 30G
JOB 13 expcore-tests PENDING 8000 51000 16 30G
RENEWER status=running heartbeat=990 until=99999 submitted=1
"""


def test_o64_a_step_goes_to_the_running_allocation_of_ours_that_ends_last_and_fits_it(hpct):
    """O64: only allocations the login node listed as ours (its ledger's, under our name) are candidates; a step goes
    to the RUNNING one that ends last, and is refused when it would outlast it."""
    now, jobs, renewer = hpct.parse.parse_listing(LISTING)
    assert now == 1000
    assert renewer["status"] == "running"
    assert hpct.parse.pick_job(jobs, now, 3600).id == "12"
    assert hpct.parse.pick_job(jobs, now, 3600, given="11").id == "11"
    with pytest.raises(hpct.lib.NoAllocation, match="not a RUNNING allocation of ours"):
        hpct.parse.pick_job(jobs, now, 60, given="13")
    with pytest.raises(hpct.lib.NoAllocation, match="not a RUNNING allocation of ours"):
        hpct.parse.pick_job(jobs, now, 60, given="999")
    with pytest.raises(hpct.lib.NoAllocation, match="ends in 8000 s"):
        hpct.parse.pick_job(jobs, now, 8001)
    with pytest.raises(hpct.lib.HpcTestsError, match="NOW"):
        hpct.parse.parse_listing("JOB 1 x RUNNING 1 2 3 4\n")


def test_o64_a_run_s_remote_command_holds_the_use_lock_shared_and_is_bounded_on_the_login_node(hpct, site):
    """O64: a run's step runs under a shared lock on the login directory's use.lock (which the idle release must take
    exclusively) and under `timeout`, so a client that never returns cannot hold it past the run's bound."""
    argv = step(hpct, site).argv()
    words = shlex.split(hpct.lib.remote_command(argv, ".expcore-hpct", 3900).partition("; exec ")[2])
    assert words[:9] == ["flock", "-s", "-w", "600", ".expcore-hpct/use.lock", "timeout", "-k", "30", "3900"]
    assert words[9:] == argv
    with pytest.raises(hpct.lib.HpcTestsError):
        hpct.lib.remote_command(argv, "../elsewhere", 60)


@pytest.mark.parametrize(
    "args",
    [["tests", "-q"], ["tests/x.py::test_a[1-2]", "-k", "a and not b", "-m", "not benchmark"],
     ["-x", "--tb=short", "-ra", "--maxfail=2", "-W", "error::DeprecationWarning"], ["--deselect", "tests/x.py::t"]],
)  # fmt: skip
def test_the_pytest_arguments_a_run_takes(hpct, args):
    assert hpct.lib.check_pytest_args(args) == args


@pytest.mark.parametrize(
    "args",
    [["-qo", "log_file=/x"], ["@args.txt"], ["--basetemp=/x"], ["--setup-only"], ["/abs/tests"], ["../x"],
     ["tests/../../x"], ["-k"], ["--co"], ["-o", "x=y"], ["-p", "myplugin"], ["--junitxml=/x"], ["-c", "x.ini"]],
)  # fmt: skip
def test_o64_any_other_pytest_argument_is_refused(hpct, args):
    """O64: pytest reads clustered short options (`-qo ...`) and argument files (`@file`), so a run's arguments are an
    allowlist: test paths relative to the clone and options that can neither write outside the run, move its results,
    nor run no test; anything else is refused before a step starts."""
    with pytest.raises(hpct.lib.HpcTestsError):
        hpct.lib.check_pytest_args(args)
