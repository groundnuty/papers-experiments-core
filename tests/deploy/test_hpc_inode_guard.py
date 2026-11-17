"""The inode guard of the HPC federation's `up` (deploy/hpc-federation/hpcquota.py, DESIGN.md section 11): provider
storage goes on the cluster's shared scratch filesystem, which has room but a limited inode quota, so before it submits
anything `up` reads the account's inode usage and quota through the command `site.env` names, estimates the
federation's inodes from its data manifests, and refuses a federation that would pass 95% of the quota. Every command
and figure here is invented; the real site's tool and output are never in this repository. Over the fake Slurm of
hpc_fed_support, whose `tools` answer the quota command, and over a real `Runner` against a small script."""

import shlex
import sys
from pathlib import Path, PurePosixPath

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path
from hpc_import_support import IMPORT_LAYOUT, make_environ

PATTERN = r"inodes used (?P<used>[0-9,]+) of (?P<quota>[0-9,]+)"  # an invented format, as invented as its tool
TOOL = "fake-quota"  # an invented command name


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def site(tmp_path):
    images, logs, scratch = tmp_path / "images", tmp_path / "logs", tmp_path / "scratch"
    for directory in (images, logs, scratch, tmp_path / "fed"):
        directory.mkdir()
    scratch.chmod(0o755)  # this user's alone to write, whatever the umask: the guard refuses a root others can write
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    return {
        **SITE,
        **agent_site(tmp_path / "agent"),
        "SH": str(images),
        "L": str(logs),
        "HPC_STORAGE_ROOT": str(scratch),
        "HPC_QUOTA_CMD": f"{TOOL} --inodes",
        "HPC_QUOTA_PATTERN": PATTERN,
    }


class Quota:
    """The invented quota command: what it prints, its exit status, or what the runner raises for it."""

    def __init__(self, hpc, used=0, quota=1_000_000, *, text=None, rc=0, stderr="", raises=None):
        self.completed, self.raises, self.calls = hpc.slurm.Completed, raises, []
        self.text = f"inodes used {used:,} of {quota:,}\n" if text is None else text
        self.rc, self.stderr = rc, stderr

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.raises is not None:
            raise self.raises
        return self.completed(self.rc, self.text, self.stderr)


class Rig:
    """A driver over the fake Slurm, the core's phase recorded instead of run, and the quota command answered."""

    def __init__(self, hpc, tmp_path, site, quota, layout=LAYOUT, environ=None):
        self.hpc, self.site, self.quota = hpc, site, quota
        self.fake = FakeSlurm(tools={TOOL: quota})
        self.root = tmp_path / "fed" / "root"
        given = {} if environ is None else {"environ": environ}
        self.driver = hpc.cli.make_driver(layout, str(self.root), runner=self.fake, **given)
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False
        self.said = []
        self.driver.say = self.said.append

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def submitted(self):
        return any(call[0] == "sbatch" for call in self.fake.calls)

    def state(self):
        return self.driver.store.read()


def estimate_of(hpc, layout=LAYOUT, environ=None) -> int:
    import fedlayout
    import fedspec

    spec = fedspec.load_spec(layout)
    return sum(hpc.quota.estimate_inodes(fedlayout.Layout(Path("/x/root"), Path("/x"), spec), environ or {}).values())


def figures(hpc, estimate, *, used_over=0):
    """A quota whose 95% line is exactly `used + estimate - used_over`: the quota is a multiple of 20, so the line is a
    whole number, and `used` is what remains of the line."""
    line = 19_000
    return Quota(hpc, used=line - estimate + used_over, quota=20_000)


# -- the estimate -------------------------------------------------------------------------------------------------


def test_the_estimate_counts_each_spaces_manifest_once_its_files_and_its_directories(hpc):
    """O57: the estimate is every space's manifest once, files and directories (each is an inode), from the baseline
    the layout declares: the same plan `up` writes the data manifests from, counted by hand here."""
    import fedlayout
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    plan = fedlayout.baseline_plan(spec)
    by_hand = {
        space: len(files) + len({str(p) for f in files for p in PurePosixPath(f.path).parents if str(p) != "."})
        for space, files in plan.items()
    }
    found = hpc.quota.estimate_inodes(fedlayout.Layout(Path("/x/root"), Path("/x"), spec), {})
    assert found == by_hand
    assert sum(found.values()) > 0
    assert found["link-probes"] == 0
    assert found["delta-space"] == 0


def test_the_estimate_is_what_the_core_phase_writes_and_reads_as_the_data_manifests(hpc, tmp_path):
    """O57: the estimate and `up`'s data manifests (written once the services are up) are one count: a shared
    import's manifest is the layout's own file, every other space's the one written into the root."""
    import fedlayout
    import fedspec
    import hpcdata

    from expcore_onedata.importmanifest import load_manifest

    environ = make_environ(tmp_path)
    spec = fedspec.load_spec(IMPORT_LAYOUT)
    root = tmp_path / "root"
    root.mkdir()
    layout = fedlayout.Layout(root, tmp_path, spec)
    estimate = hpc.quota.estimate_inodes(layout, environ)
    written = {name: load_manifest(path) for name, path in hpcdata.data_manifests(layout, environ).items()}
    assert estimate == {name: len(m.files) + len(m.directories) for name, m in written.items()}
    assert sum(estimate.values()) >= 4  # each of the four imported spaces holds a file


# -- the line ----------------------------------------------------------------------------------------------------


def test_a_federation_under_the_line_passes_and_the_figures_are_said(hpc, tmp_path, site):
    """O57: usage plus the estimate under 95% of the quota: up goes on, and says used, estimate, quota and the line."""
    estimate = estimate_of(hpc)
    rig = Rig(hpc, tmp_path, site, figures(hpc, estimate, used_over=-1))
    assert rig.up().startswith("core: the link check passed")
    said = " ".join(rig.said)
    assert f"estimate {estimate}" in said
    assert "quota 20000" in said
    assert "95% line 19000" in said
    assert rig.quota.calls
    assert rig.quota.calls[0][0] == [TOOL, "--inodes"]


def test_a_federation_exactly_at_the_line_passes(hpc, tmp_path, site):
    """O57: usage plus the estimate equal to 95% of the quota is at the line, not past it."""
    rig = Rig(hpc, tmp_path, site, figures(hpc, estimate_of(hpc)))
    assert rig.up().startswith("core: the link check passed")


def test_a_federation_one_inode_over_the_line_is_refused_naming_every_figure(hpc, tmp_path, site):
    """O57: one inode past the line is refused, before anything is submitted, written or made: the message names the
    used count, the estimate, the quota and the 95% line, and the scratch root, the root and Slurm are untouched."""
    estimate = estimate_of(hpc)
    quota = figures(hpc, estimate, used_over=1)
    rig = Rig(hpc, tmp_path, site, quota)
    with pytest.raises(RuntimeError) as refused:
        rig.up()
    text = str(refused.value)
    for named in (f"used {19_000 - estimate + 1}", f"estimate {estimate}", "quota 20000", "95% line 19000"):
        assert named in text
    assert not rig.submitted()
    assert not rig.root.exists()
    assert list(Path(site["HPC_STORAGE_ROOT"]).iterdir()) == []
    assert not (tmp_path / "fed" / "root.lifecycle.lock").exists()


def test_the_line_is_the_floor_of_95_percent_so_a_quota_that_is_not_a_multiple_of_20_is_exact(hpc):
    """O57: with a quota of 1001 the line is 950 (950.95 floored): 950 passes and 951 is refused, in integers."""
    runner = FakeSlurm(tools={TOOL: Quota(hpc, used=940, quota=1001)})
    parsed = hpc.settings.site_from_environment(
        {**SITE, "SH": "/s", "L": "/l", "HPC_STORAGE_ROOT": "/s/x", "HPC_QUOTA_CMD": TOOL, "HPC_QUOTA_PATTERN": PATTERN}
    )
    assert "950" in hpc.quota.check_inodes(runner, parsed, 10)
    with pytest.raises(hpc.quota.QuotaRefused, match="95% line 950"):
        hpc.quota.check_inodes(runner, parsed, 11)


# -- an unreadable quota fails closed ----------------------------------------------------------------------------


def refused(hpc, tmp_path, site, quota, why):
    """`up` over a quota command that cannot be read is refused naming `why`, with nothing submitted or made."""
    rig = Rig(hpc, tmp_path, site, quota)
    with pytest.raises(RuntimeError, match=why):
        rig.up()
    assert not rig.submitted()
    assert not rig.root.exists()
    assert list(Path(site["HPC_STORAGE_ROOT"]).iterdir()) == []


def test_a_quota_command_that_fails_is_refused_naming_its_status(hpc, tmp_path, site):
    """O57: a command that exits non-zero is refused, with its status and what it said on stderr (bounded)."""
    refused(hpc, tmp_path, site, Quota(hpc, rc=3, text="", stderr="no such account\n"), r"status 3.*no such account")


def test_a_quota_command_that_cannot_run_or_does_not_answer_is_refused(hpc, tmp_path, site):
    """O57: a missing command, and one that does not answer within its bound, are refused as unreadable."""
    refused(hpc, tmp_path, site, Quota(hpc, raises=FileNotFoundError(2, "No such file")), "could not be run")
    timeout = hpc.slurm.SlurmError(f"{TOOL} did not answer in 120 s")
    refused(hpc, tmp_path, site, Quota(hpc, raises=timeout), "did not answer")


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "matched nothing"),
        ("inodes used unlimited of unlimited\n", "matched nothing"),
        ("inodes used 5 of 100\ninodes used 6 of 100\n", "matched 2 times"),
        ("inodes used 5 of 0\n", "quota of 0"),
        ("inodes used 99999999999999999999999 of 100\n", "not a count"),
        ("inodes used 5 of 1000.5\n", "cut out of a longer one"),
        ("inodes used 5 of 1000unlimited\n", "cut out of a longer one"),
        ("inodes used 5 of 2G\n", "cut out of a longer one"),
        ("inodes used 1.5 of 100\n", "matched nothing"),
        ("inodes used 5 of 100%\n", "cut out of a longer one"),
        ("inodes used 5 of 1,000,\n", "not a count"),
    ],
)
def test_output_that_is_not_one_used_count_and_one_quota_is_refused(hpc, tmp_path, site, text, why):
    """O57: no match, two matches, an unlimited quota, a quota of 0, a count that is no integer and a figure the pattern
    cut out of a longer one (a decimal, a unit, a percent: it would read as a smaller count) all fail closed."""
    refused(hpc, tmp_path, site, Quota(hpc, text=text), why)


def test_a_quota_command_that_is_not_set_is_refused_and_the_override_does_not_apply_on_scratch(hpc, tmp_path, site):
    """O57: with the scratch root set, no command means no check, and that is refused naming the variable; the
    override HPC_QUOTA_CHECK=off does not apply to a federation bound onto scratch, so it is refused as the site is
    loaded, naming the combination (test_hpc_storage_bind.py has the rest)."""
    refused(hpc, tmp_path, {k: v for k, v in site.items() if k != "HPC_QUOTA_CMD"}, Quota(hpc), "HPC_QUOTA_CMD")
    refused(hpc, tmp_path, {k: v for k, v in site.items() if k != "HPC_QUOTA_PATTERN"}, Quota(hpc), "HPC_QUOTA_PATTERN")
    off = {k: v for k, v in site.items() if k not in ("HPC_QUOTA_CMD", "HPC_QUOTA_PATTERN")}
    with pytest.raises(hpc.settings.SettingsError, match=r"HPC_QUOTA_CHECK=off.*HPC_STORAGE_ROOT"):
        hpc.settings.site_from_environment({**off, "HPC_QUOTA_CHECK": "off"})


@pytest.mark.parametrize("value", ["0", "no", "OFF", "false", "on", " off"])
def test_the_override_takes_one_value_and_anything_else_is_refused(hpc, site, value):
    """O57: HPC_QUOTA_CHECK is `off` or unset; unset is not off, and no other spelling turns the check off."""
    with pytest.raises(hpc.settings.SettingsError, match="HPC_QUOTA_CHECK"):
        hpc.settings.site_from_environment({**site, "HPC_QUOTA_CHECK": value})


@pytest.mark.parametrize(
    ("pattern", "why"),
    [("inodes used (?P<used>[0-9]+", "HPC_QUOTA_PATTERN"), (r"used (?P<used>[0-9]+)", "named groups used and quota")],
)
def test_a_pattern_that_does_not_compile_or_lacks_a_group_is_refused(hpc, tmp_path, site, pattern, why):
    """O57: the parsing hint is checked: a regular expression that does not compile, or has no `used` and `quota`."""
    refused(hpc, tmp_path, {**site, "HPC_QUOTA_PATTERN": pattern}, Quota(hpc), why)


def test_the_command_runs_as_an_argv_and_never_through_a_shell(hpc, tmp_path, site):
    """O57: the command is split as a shell would split it and run without one: a word with a `;` is one argument."""
    quota = Quota(hpc, used=0, quota=1_000_000)
    rig = Rig(hpc, tmp_path, {**site, "HPC_QUOTA_CMD": f"{TOOL} --inodes '/a b;c' $HOME"}, quota)
    rig.up()
    assert quota.calls[0][0] == [TOOL, "--inodes", "/a b;c", "$HOME"]


def test_a_real_runner_reads_a_real_command_and_refuses_one_that_fails(hpc, tmp_path):
    """O57: through the real `Runner` (a subprocess, no shell): a script's output is read, and a script that exits
    non-zero, or a command that does not exist, is refused as unreadable."""
    script = tmp_path / "quota.py"
    script.write_text("import sys\nprint('inodes used 1,234 of 5,000,000')\nsys.exit(int(sys.argv[1]))\n")
    base = {**SITE, "SH": "/s", "L": "/l", "HPC_STORAGE_ROOT": "/s/x", "HPC_QUOTA_PATTERN": PATTERN}

    def site(command):
        return hpc.settings.site_from_environment({**base, "HPC_QUOTA_CMD": command})

    run = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    assert hpc.quota.read_quota(hpc.slurm.Runner(), site(f"{run} 0")) == (1234, 5_000_000)
    with pytest.raises(hpc.quota.QuotaRefused, match="status 4"):
        hpc.quota.read_quota(hpc.slurm.Runner(), site(f"{run} 4"))
    with pytest.raises(hpc.quota.QuotaRefused, match="could not be run"):
        hpc.quota.read_quota(hpc.slurm.Runner(), site(str(tmp_path / "no-such-command")))


# -- where the guard applies --------------------------------------------------------------------------------------


def test_a_site_without_a_scratch_root_runs_no_check_and_up_is_as_before(hpc, tmp_path, site):
    """O57: with no HPC_STORAGE_ROOT nothing of the federation lands on scratch, so the guard has nothing to guard:
    no quota command is set or run, and up is what it was."""
    plain = {k: v for k, v in site.items() if not k.startswith("HPC_QUOTA") and k != "HPC_STORAGE_ROOT"}
    quota = Quota(hpc)
    rig = Rig(hpc, tmp_path, plain, quota)
    assert rig.up().startswith("core: the link check passed")
    assert quota.calls == []
    assert rig.state()["storage"] is None


def test_the_estimate_of_a_shared_import_layout_is_its_manifests_and_it_is_checked(hpc, tmp_path, site):
    """O57: for the import layout the estimate is the shared manifests' (the variables are read from the driver's
    environment), and the guard refuses on it."""
    environ = make_environ(tmp_path)
    estimate = estimate_of(hpc, IMPORT_LAYOUT, environ)
    assert estimate >= 4
    rig = Rig(hpc, tmp_path, site, figures(hpc, estimate, used_over=1), layout=IMPORT_LAYOUT, environ=environ)
    with pytest.raises(RuntimeError, match=f"estimate {estimate}"):
        rig.up()
    assert not rig.submitted()
