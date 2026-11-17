"""The home guard of the HPC federation's `up` (deploy/hpc-federation/hpcquota.py, DESIGN.md section 11): the account's
home is small, and every agent of the account shares it, so before it submits anything `up` reads the home's usage and
limits through the command `site.env` names and refuses when its files or its bytes are past 95% of their limit.
Every command, path, user and figure here is invented; the real site's tool and output are never in this repository.
Over the fake Slurm of hpc_fed_support, whose `tools` answer the quota command, and over a real `Runner` against a
small script."""

import shlex
import sys

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path
from hpc_home_support import (
    BYTES_LIMIT,
    BYTES_LINE,
    FILES_LIMIT,
    FILES_LINE,
    PATTERN,
    TOOL,
    Quota,
    printed,
)

PRIVATE = ("/invented/home/someone", "someone-invented")  # what a tool's own stderr or argv might name


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    for directory in (images, logs, tmp_path / "fed"):
        directory.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    return {
        **SITE,
        **agent_site(tmp_path / "agent"),
        "SH": str(images),
        "L": str(logs),
        "HPC_HOME_QUOTA_CMD": f"{TOOL} --home",
        "HPC_HOME_QUOTA_PATTERN": PATTERN,
    }


def parsed(hpc, site):
    return hpc.settings.site_from_environment(site)


def guard(hpc, site, quota, **given):
    return hpc.quota.home_guard(FakeSlurm(tools={TOOL: quota}), parsed(hpc, {**site, **given}))


class Rig:
    """A driver over the fake Slurm, the core's phase recorded instead of run, and the home quota command answered."""

    def __init__(self, hpc, tmp_path, site, quota):
        self.hpc, self.site, self.quota = hpc, site, quota
        self.fake = FakeSlurm(tools={TOOL: quota})
        self.root = tmp_path / "fed" / "root"
        self.driver = hpc.cli.make_driver(LAYOUT, str(self.root), runner=self.fake)
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


# -- the 95% line, for files and for bytes -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "bytes_"),
    [
        (0, 0),
        (FILES_LINE - 1, 0),
        (FILES_LINE, 0),
        (0, BYTES_LINE - 1),
        (0, BYTES_LINE),
        (FILES_LINE, BYTES_LINE),
    ],
    ids=["empty", "files-under", "files-at", "bytes-under", "bytes-at", "both-at"],
)
def test_a_home_under_or_at_the_line_passes_and_says_its_figures(hpc, site, files, bytes_):
    """O57: usage under the 95% line, and exactly at it, passes (`used <= limit * 95 // 100`, in integers), saying each
    used figure, its limit and the line."""
    quota = Quota(hpc, files, bytes_)
    said = guard(hpc, site, quota)
    for part in (
        f"files used {files}, limit {FILES_LIMIT}, 95% line {FILES_LINE}",
        f"bytes used {bytes_}, limit {BYTES_LIMIT}, 95% line {BYTES_LINE}",
        "under the line",
    ):
        assert part in said
    assert said.startswith("home check")
    assert len(quota.calls) == 1


@pytest.mark.parametrize(
    ("files", "bytes_", "named", "not_named"),
    [
        (FILES_LINE + 1, 0, ["files"], ["bytes"]),
        (0, BYTES_LINE + 1, ["bytes"], ["files"]),
        (FILES_LINE + 1, BYTES_LINE + 1, ["files", "bytes"], []),
        (FILES_LIMIT + 5, 0, ["files"], ["bytes"]),
    ],
    ids=["files-one-past", "bytes-one-past", "both-one-past", "files-over-the-limit"],
)
def test_a_home_one_past_the_line_is_refused_naming_the_used_figure_the_limit_and_the_line(
    hpc, site, files, bytes_, named, not_named
):
    """O57: one file or one byte past the 95% line refuses, naming the figure that is past (its used figure, its limit
    and its line) and not the one that is not, and that nothing was submitted."""
    with pytest.raises(hpc.quota.QuotaRefused) as refused:
        guard(hpc, site, Quota(hpc, files, bytes_))
    text = str(refused.value)
    figures = {
        "files": f"files used {files}, limit {FILES_LIMIT}, 95% line {FILES_LINE}",
        "bytes": f"bytes used {bytes_}, limit {BYTES_LIMIT}, 95% line {BYTES_LINE}",
    }
    assert "nothing was submitted" in text
    for name in named:
        assert figures[name] in text
    for name in not_named:
        assert figures[name] not in text


def test_the_line_is_the_floor_of_95_percent_so_a_limit_that_is_not_a_multiple_of_20_is_exact(hpc, site):
    """O57: with a files limit of 1001 the line is 950 (950.95 floored): 950 passes and 951 is refused."""
    at = guard(hpc, site, Quota(hpc, files=950, files_limit=1001))
    assert "files used 950, limit 1001, 95% line 950" in at
    with pytest.raises(hpc.quota.QuotaRefused, match="95% line 950"):
        guard(hpc, site, Quota(hpc, files=951, files_limit=1001))


def test_the_figures_may_carry_thousands_separators_and_are_read_whole(hpc, site):
    """O57: a count printed with `,` separators is read whole, a bytes count in the tens of billions included."""
    quota = Quota(hpc, 1_234, 5_000_000_000, bytes_limit=20_000_000_000)
    assert "bytes used 5000000000, limit 20000000000, 95% line 19000000000" in guard(hpc, site, quota)


# -- an unreadable home quota fails closed -------------------------------------------------------------------------


def refused(hpc, site, quota, why, **given):
    """The guard over a quota command that cannot be read is refused naming `why`, and the message holds nothing a
    tool's output or command line might name."""
    with pytest.raises(hpc.quota.QuotaRefused, match=why) as found:
        guard(hpc, site, quota, **given)
    for private in PRIVATE:
        assert private not in str(found.value)
    assert TOOL not in str(found.value)


def test_a_home_quota_command_that_fails_is_refused_naming_its_status_and_never_its_stderr(hpc, site):
    """O57: a command that exits non-zero is refused with its status; what it said on stderr is never repeated, since a
    home quota tool names the user and the home's path."""
    said = f"quota for {PRIVATE[1]} on {PRIVATE[0]}: no such account\n"
    refused(hpc, site, Quota(hpc, rc=3, text="", stderr=said), "status 3")


def test_a_home_quota_command_that_cannot_run_or_does_not_answer_is_refused(hpc, site):
    """O57: a missing command, one the user may not run, and one that does not answer within its bound are refused as
    unreadable, whatever the exception's own text names."""
    refused(hpc, site, Quota(hpc, raises=FileNotFoundError(2, "No such file", PRIVATE[0])), "could not be run")
    refused(hpc, site, Quota(hpc, raises=PermissionError(13, "denied", PRIVATE[0])), "could not be run")
    refused(hpc, site, Quota(hpc, raises=hpc.slurm.SlurmError(f"{TOOL} did not answer in 120 s")), "did not answer")
    refused(hpc, site, Quota(hpc, raises=TimeoutError(PRIVATE[1])), "did not answer")


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "matched nothing"),
        ("files unlimited (soft 0; hard unlimited); bytes unlimited (soft 0; hard unlimited)\n", "matched nothing"),
        (printed(1, 1) + printed(2, 2), "matched 2 times"),
        (printed(1, 1, files_limit=0), "files_limit.*limit of 0.*hard limit"),
        (printed(1, 1, bytes_limit=0), "bytes_limit.*limit of 0.*hard limit"),
        (printed(1, 1).replace("hard 40,000", "hard 99999999999999999999999"), "files_limit figure.*not a count"),
        (printed(1, 1).replace("hard 8,000,000", "hard 99999999999999999999999"), "bytes_limit figure.*not a count"),
        (printed(1, 1).replace("files 1 ", "files 1.5 "), "matched nothing"),
        (printed(1, 1).replace("bytes 1 ", "bytes 1e6 "), "matched nothing"),
    ],
)
def test_output_that_is_not_one_set_of_four_whole_figures_with_a_limit_above_zero_is_refused(hpc, site, text, why):
    """O57: no match, two matches, an unlimited figure, a limit of 0 (the soft column's reading, so the message points
    at the hard limit) and a count that is no integer or too large to be a quota all fail closed."""
    refused(hpc, site, Quota(hpc, text=text), why)


# A looser invented pattern, with no literal after a figure: it lets the pattern stop short of the token, which the
# guard refuses where it happens.
LOOSE = (
    r"files (?P<files_used>[0-9,]+).*?hard (?P<files_limit>[0-9,]+).*?"
    r"bytes (?P<bytes_used>[0-9,]+).*?hard (?P<bytes_limit>[0-9,]+)"
)


@pytest.mark.parametrize(
    "text",
    [
        printed(1, 1).replace("files 1 ", "files 1.5 "),
        printed(1, 1).replace("bytes 1 ", "bytes 1e6 "),
        printed(1, 1).replace("hard 40,000", "hard 40,000."),
        printed(1, 1).replace("hard 40,000", "hard 40,000%"),
        printed(1, 1).replace("hard 8,000,000", "hard 8,000,000G"),
        printed(1, 1).replace("hard 8,000,000", "hard 8,000,000-9"),
    ],
    ids=["decimal-used", "exponent", "decimal-limit", "percent", "unit", "range"],
)
def test_a_figure_the_pattern_cut_out_of_a_longer_one_is_refused(hpc, site, text):
    """O57: a figure the pattern stopped short of (a decimal point, an exponent, a percent, a unit, a range) would read
    as a smaller count, so it is refused where the pattern stopped: the pattern must capture the whole figure."""
    refused(hpc, site, Quota(hpc, text=text), "cut out of a longer one", HPC_HOME_QUOTA_PATTERN=LOOSE)


def test_the_loose_pattern_reads_whole_figures_so_the_refusals_above_are_the_cut_figures_alone(hpc, site):
    """O57: the control of the cut-out cases: the same loose pattern over whole figures reads them and passes."""
    assert "files used 1000, limit 40000" in guard(hpc, site, Quota(hpc, 1_000, 2), HPC_HOME_QUOTA_PATTERN=LOOSE)


def test_a_pattern_on_the_soft_column_reads_a_limit_of_zero_and_is_refused(hpc, site):
    """O57: on a site whose soft column reads 0 and whose hard column binds, a pattern that captures the soft figure
    would read a limit of 0 and never refuse a full home: it is refused, so a mis-written pattern cannot pass."""
    soft = (
        r"files (?P<files_used>[0-9,]+) \(soft (?P<files_limit>[0-9,]+); hard [0-9,]+\); "
        r"bytes (?P<bytes_used>[0-9,]+) \(soft (?P<bytes_limit>[0-9,]+); hard [0-9,]+\)"
    )
    assert soft != PATTERN
    refused(hpc, site, Quota(hpc, FILES_LIMIT, BYTES_LIMIT), "limit of 0", HPC_HOME_QUOTA_PATTERN=soft)


@pytest.mark.parametrize("group", ["files_used", "files_limit", "bytes_used", "bytes_limit"])
def test_a_pattern_that_lacks_one_of_the_four_groups_is_refused(hpc, site, group):
    """O57: the pattern names the used figure and the limit of the files and of the bytes: each group is required."""
    without = PATTERN.replace(f"(?P<{group}>", "(")
    refused(
        hpc,
        site,
        Quota(hpc),
        "named groups files_used, files_limit, bytes_used and bytes_limit",
        HPC_HOME_QUOTA_PATTERN=without,
    )


def test_a_pattern_that_does_not_compile_is_refused(hpc, site):
    """O57: a regular expression that does not compile is refused, naming the variable."""
    refused(
        hpc,
        site,
        Quota(hpc),
        "HPC_HOME_QUOTA_PATTERN is not a regular expression",
        HPC_HOME_QUOTA_PATTERN="files (?P<a>",
    )


def test_keys_set_on_one_side_only_are_refused_never_taken_as_unset(hpc, site):
    """O57: a command without its pattern, or a pattern without its command, is a half-set check: it refuses, naming
    the missing variable, and runs nothing."""
    quota = Quota(hpc)
    only_command = {k: v for k, v in site.items() if k != "HPC_HOME_QUOTA_PATTERN"}
    with pytest.raises(hpc.quota.QuotaRefused, match="HPC_HOME_QUOTA_PATTERN is not set"):
        guard(hpc, only_command, quota)
    only_pattern = {k: v for k, v in site.items() if k != "HPC_HOME_QUOTA_CMD"}
    with pytest.raises(hpc.quota.QuotaRefused, match="HPC_HOME_QUOTA_CMD is not set"):
        guard(hpc, only_pattern, quota)
    assert quota.calls == []


def test_a_command_that_is_no_command_line_is_refused(hpc, site):
    """O57: an unbalanced quote leaves no argv to run: refused, naming the variable, nothing run."""
    quota = Quota(hpc)
    with pytest.raises(hpc.quota.QuotaRefused, match="HPC_HOME_QUOTA_CMD is not a command line"):
        guard(hpc, site, quota, HPC_HOME_QUOTA_CMD=f"{TOOL} 'unbalanced")
    assert quota.calls == []


def test_the_command_runs_as_an_argv_under_the_bound_and_never_through_a_shell(hpc, site):
    """O57: the command is split as a shell would split it and run without one (a word with a `;` is one argument and
    `$HOME` stays a literal), under the 120 s bound."""
    quota = Quota(hpc)
    guard(hpc, site, quota, HPC_HOME_QUOTA_CMD=f"{TOOL} --home '/a b;c' $HOME")
    assert quota.calls == [([TOOL, "--home", "/a b;c", "$HOME"], 120.0)]


def test_a_real_runner_reads_a_real_command_and_refuses_one_that_fails(hpc, site, tmp_path):
    """O57: through the real `Runner` (a subprocess, no shell): a script's output is read, one that exits non-zero is
    refused naming its status and not its stderr, and a command that does not exist is refused as unreadable."""
    script = tmp_path / "home_quota.py"
    script.write_text(
        "import sys\n"
        f"print({printed(1_234, 56_789)!r}, end='')\n"
        f"print({PRIVATE[1]!r}, file=sys.stderr)\n"
        "sys.exit(int(sys.argv[1]))\n"
    )
    run = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"

    def read(command):
        return hpc.quota.home_guard(hpc.slurm.Runner(), parsed(hpc, {**site, "HPC_HOME_QUOTA_CMD": command}))

    assert "files used 1234, limit 40000" in read(f"{run} 0")
    with pytest.raises(hpc.quota.QuotaRefused, match="status 4") as failed:
        read(f"{run} 4")
    assert PRIVATE[1] not in str(failed.value)
    with pytest.raises(hpc.quota.QuotaRefused, match="could not be run"):
        read(str(tmp_path / "no-such-command"))


# -- where the guard applies, and the override ----------------------------------------------------------------------


def test_with_no_key_set_the_guard_runs_no_command_and_says_nothing(hpc, site):
    """O57: a site that sets neither key has no home check, as before: no command runs and the guard says nothing, so
    `up`'s output is what it was."""
    quota = Quota(hpc, FILES_LIMIT, BYTES_LIMIT)
    plain = {k: v for k, v in site.items() if not k.startswith("HPC_HOME_QUOTA")}
    assert guard(hpc, plain, quota) == ""
    assert quota.calls == []


def test_the_explicit_override_skips_the_check_runs_no_command_and_says_so(hpc, site):
    """O57: `HPC_HOME_QUOTA_CHECK=off` skips the check even when the keys are set and the home is full; it says so, and
    no command runs. Set without the keys it says so as well."""
    quota = Quota(hpc, FILES_LIMIT, BYTES_LIMIT)
    said = guard(hpc, site, quota, HPC_HOME_QUOTA_CHECK="off")
    assert said == "home check: skipped: HPC_HOME_QUOTA_CHECK=off"
    plain = {k: v for k, v in site.items() if not k.startswith("HPC_HOME_QUOTA")}
    assert guard(hpc, plain, quota, HPC_HOME_QUOTA_CHECK="off") == said
    assert quota.calls == []


@pytest.mark.parametrize("value", ["0", "no", "OFF", "false", "on", " off", "skip"])
def test_the_override_takes_one_value_and_anything_else_is_refused(hpc, site, value):
    """O57: HPC_HOME_QUOTA_CHECK is `off` or unset; unset is not off, and no other spelling turns the check off."""
    with pytest.raises(hpc.settings.SettingsError, match="HPC_HOME_QUOTA_CHECK"):
        parsed(hpc, {**site, "HPC_HOME_QUOTA_CHECK": value})


def test_the_home_keys_are_never_recorded_in_the_state_file(hpc, site):
    """O57: the command line names the user and the home's path, so the site's JSON, which the state file holds, carries
    none of the home keys or their values, and `up` and `down` need none of them after."""
    recorded = parsed(hpc, site).to_json()
    text = repr(recorded)
    assert TOOL not in text
    assert "home_quota" not in text
    assert hpc.settings.Site.from_json(recorded).home_quota_cmd == ""


# -- through up ----------------------------------------------------------------------------------------------------


def test_up_refuses_a_home_past_the_line_before_anything_is_submitted_or_made(hpc, tmp_path, site):
    """O57: `up` over a home one file past the line is refused, naming the figure, the limit and the line, with no job
    submitted and no root made."""
    rig = Rig(hpc, tmp_path, site, Quota(hpc, FILES_LINE + 1))
    with pytest.raises(RuntimeError, match=f"files used {FILES_LINE + 1}, limit {FILES_LIMIT}, 95% line {FILES_LINE}"):
        rig.up()
    assert not rig.submitted()
    assert not rig.root.exists()
    assert not (tmp_path / "fed" / "root.lifecycle.lock").exists()


def test_up_over_a_home_under_the_line_says_so_and_goes_on(hpc, tmp_path, site):
    """O57: a home under the line lets `up` go on, and `up` says the home's figures once, before the core's phase."""
    quota = Quota(hpc, 100, 200)
    rig = Rig(hpc, tmp_path, site, quota)
    assert rig.up().startswith("core: the link check passed")
    homes = [line for line in rig.said if line.startswith("home check")]
    assert len(homes) == 1
    assert "files used 100" in homes[0]
    assert len(quota.calls) == 1


def test_up_runs_the_home_check_without_a_scratch_root_and_can_refuse(hpc, tmp_path, site):
    """O57: the home check does not depend on `HPC_STORAGE_ROOT` (the home is shared whatever the storage is): with
    only the home keys set, `up` runs the command and refuses a full home."""
    assert "HPC_STORAGE_ROOT" not in site
    rig = Rig(hpc, tmp_path, site, Quota(hpc, 0, BYTES_LINE + 1))
    with pytest.raises(RuntimeError, match="bytes used"):
        rig.up()
    assert not rig.submitted()


def test_up_with_the_override_goes_on_over_a_full_home_and_says_the_check_was_skipped(hpc, tmp_path, site):
    """O57: with the override `up` goes on over a full home without reading it, and says that the check was skipped."""
    quota = Quota(hpc, FILES_LIMIT, BYTES_LIMIT)
    rig = Rig(hpc, tmp_path, {**site, "HPC_HOME_QUOTA_CHECK": "off"}, quota)
    assert rig.up().startswith("core: the link check passed")
    assert "home check: skipped: HPC_HOME_QUOTA_CHECK=off" in rig.said
    assert quota.calls == []


def test_up_with_no_home_key_set_says_nothing_of_the_home_and_runs_no_command(hpc, tmp_path, site):
    """O57: a site without the keys has `up` as it was: nothing said of the home, no command run."""
    plain = {k: v for k, v in site.items() if not k.startswith("HPC_HOME_QUOTA")}
    quota = Quota(hpc, FILES_LIMIT, BYTES_LIMIT)
    rig = Rig(hpc, tmp_path, plain, quota)
    assert rig.up().startswith("core: the link check passed")
    assert not any("home check" in line for line in rig.said)
    assert quota.calls == []
