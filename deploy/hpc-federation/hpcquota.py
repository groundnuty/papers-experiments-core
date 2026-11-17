"""The inode guard of the HPC federation's `up` (DESIGN.md, section 11): provider storage goes on the cluster's shared
scratch filesystem, which has room for the bytes but a limited inode quota, so before anything is submitted `up` reads
the account's inode usage and quota, estimates the federation's inodes from its data manifests, and refuses a
federation that would pass 95% of the quota.

The site's tool and its output are never in this repository. `site.env` names the command (`HPC_QUOTA_CMD`, split as a
shell would split it and run without one, on the login node) and the regular expression that reads its output
(`HPC_QUOTA_PATTERN`, with the named groups `used` and `quota`); the tests run an invented command and format.

**Failing closed, and mandatory.** The guard applies whenever `HPC_STORAGE_ROOT` is set, since the providers' storage
is then bound onto scratch (nothing of the federation lands on scratch without it). Anything that leaves the quota
unread refuses `up`, naming why: no command or pattern set, a pattern that does not compile or lacks a group, a command
that cannot run, exits non-zero or does not answer, an output the pattern matches never or more than once, a figure
that is no count, a quota of 0 or "unlimited". No override skips it: `HPC_QUOTA_CHECK=off` with a scratch root is
refused as the site is loaded (`hpcsettings.checked_site`), and again here for a site however it was built.

**The estimate** is every space's manifest counted once, files and directories (each is an inode), a shared import's
manifest being the layout's own file and every other space's the baseline `up` writes (`hpcdata.declared`, the one count
the core's phase writes the data manifests from). A shared import's owner holds the tree where it already is and adds
nothing on scratch, but the copy a mirror fetches does, so it counts once like the rest; a replica on every mirror is
not multiplied in, and the 5% below the line is the room for what the estimate leaves out. A layout whose spaces
declare no data (`baseline = false`, no manifest) estimates what its baseline writes, a handful. An ingested space that
declares its manifest (`data_manifest = "@study_artifacts@/..."`, which `up` loads and records) counts it, as the study
will fill the space with what it declares: the pilot's largest space is 15,340 files.

**The home guard** (`home_guard`) is the other check of `up`, and does not depend on a scratch root: the account's home
is small and shared by every agent of the account, and a checkout and its venv in it run to thousands of files each, so
before it submits anything `up` (and the fleet, once for all its federations) reads the home's usage and limits and
refuses when its files or its bytes are past 95% of their limit. `site.env` names the command (`HPC_HOME_QUOTA_CMD`) and
the pattern that reads it (`HPC_HOME_QUOTA_PATTERN`, one match, the named groups `files_used`, `files_limit`,
`bytes_used` and `bytes_limit`: the limit that binds, which a tool that prints a soft figure beside it may not be the
one it calls the quota). It guards whenever either key is set, and fails closed as the inode guard does; the one
override, `HPC_HOME_QUOTA_CHECK=off`, skips it and says so. With neither key set there is no check, as before. Its
messages hold figures only: a home quota tool's stderr and command line name the user and the home's path, so they are
never repeated.
"""

import re
import shlex
import string
from typing import Any, Dict, Mapping, Optional, Tuple

import hpcdata
from hpcsettings import QUOTA_CHECK_OFF, Site
from hpcslurm import SlurmError

LINE_PERCENT = 95  # up refuses a federation whose inodes would pass this share of the quota
QUOTA_TIMEOUT_S = 120.0  # the command's bound
STDERR_TAIL = 200  # the most of its stderr a message repeats
COUNT = re.compile(r"[0-9]+(?:,[0-9]{3})*")  # a count, with "," as a thousands separator
# What a figure may not touch in the output: a figure the pattern cut out of a longer one (`1000.5`, `2G`, `1e6`,
# `12-3`, `95%`) would read as a smaller count, so each is refused where the pattern stopped short of the token.
TOUCHING = frozenset(string.ascii_letters + string.digits + "_.,+-%")
MAX_COUNT = 10**15  # no inode quota is above this: a figure that is, is not one
MAX_BYTES = 10**19  # nor is a byte quota, in whatever unit the tool counts (a byte, a kilobyte): above it, no figure
GROUPS = ("used", "quota")
HOME_GROUPS = ("files_used", "files_limit", "bytes_used", "bytes_limit")
HOME_KINDS = ("files", "bytes")


class QuotaRefused(RuntimeError):
    """The federation is refused: the quota cannot be read, or the federation would pass the line. Names why."""


def estimate_inodes(
    layout: Any, environ: Mapping[str, str], values: Optional[Mapping[str, str]] = None
) -> Dict[str, int]:
    """Each space's inodes: its manifest's files and directories, nothing written (`hpcdata.declared`). An ingested
    space counts the manifest its layout declares for it (`values` fills `@study_artifacts@`: the site's
    `HPC_STUDY_ARTIFACTS`), as `up` loads it; one that declares none counts nothing."""
    return {name: len(m.files) + len(m.directories) for name, m in hpcdata.declared(layout, environ, values).items()}


def pattern_of(site: Site) -> "re.Pattern[str]":
    """The compiled `HPC_QUOTA_PATTERN`, refused unless it compiles and names the groups `used` and `quota`."""
    if not site.quota_pattern:
        raise QuotaRefused(
            "HPC_QUOTA_PATTERN is not set, so the inode quota cannot be read: give the regular expression, with the "
            "named groups used and quota, that reads the command's output (the check is mandatory on scratch)"
        )
    try:
        found = re.compile(site.quota_pattern)
    except re.error as error:
        raise QuotaRefused(f"HPC_QUOTA_PATTERN is not a regular expression ({error.msg})") from None
    if not set(GROUPS) <= set(found.groupindex):
        raise QuotaRefused("HPC_QUOTA_PATTERN needs the named groups used and quota")
    return found


def argv_of(command: str, variable: str, what: str, advice: str) -> list:
    """`variable`'s `command` as an argv: split as a shell would split it, never passed to one."""
    if not command:
        raise QuotaRefused(f"{variable} is not set, so {what} cannot be read: {advice}")
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise QuotaRefused(f"{variable} is not a command line ({error})") from None
    if not argv:
        raise QuotaRefused(f"{variable} holds no command")
    return argv


def command_of(site: Site) -> list:
    """The quota command as an argv: `HPC_QUOTA_CMD` split as a shell would split it, never passed to one."""
    return argv_of(
        site.quota_cmd,
        "HPC_QUOTA_CMD",
        "the inode quota",
        "name the site's command (the check is mandatory on scratch)",
    )


def count_of(value: Optional[str], which: str, ceiling: int = MAX_COUNT, command: str = "quota command") -> int:
    if value is None or COUNT.fullmatch(value) is None or int(value.replace(",", "")) >= ceiling:
        raise QuotaRefused(f"the {which} figure the {command} printed is not a count")
    return int(value.replace(",", ""))


def check_whole(text: str, found: "re.Match[str]", names: Tuple[str, ...], variable: str, command: str) -> None:
    """Refuse a figure the pattern cut out of a longer one: each of `names` must stop where the output's token does."""
    for name in names:
        start, end = found.span(name)
        if text[max(start - 1, 0) : start] in TOUCHING or text[end : end + 1] in TOUCHING:
            raise QuotaRefused(
                f"the {name} figure the {command} printed is cut out of a longer one (a decimal point, a unit or a "
                f"longer number touches it): {variable} must capture the whole figure"
            )


def parse_quota(text: str, pattern: "re.Pattern[str]") -> Tuple[int, int]:
    """The used count and the quota from the command's output: exactly one match of the pattern, two counts, a quota
    above 0 (a quota of 0, and "unlimited", which no count matches, set no line to check against)."""
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        found = "nothing" if not matches else f"{len(matches)} times"
        raise QuotaRefused(f"HPC_QUOTA_PATTERN matched {found} in the quota command's output (it must match once)")
    found = matches[0]
    used, quota = (count_of(found.group(name), name) for name in GROUPS)
    check_whole(text, found, GROUPS, "HPC_QUOTA_PATTERN", "quota command")
    if quota == 0:
        raise QuotaRefused("a quota of 0 sets no line to check against")
    return used, quota


def one_line(text: str, limit: int = STDERR_TAIL) -> str:
    said = " | ".join(line.strip() for line in text.splitlines() if line.strip())[-limit:]
    said = "".join(c if c.isprintable() else "?" for c in said)
    return f"its stderr: {said}" if said else "nothing on its stderr"


def read_quota(runner: Any, site: Site, timeout_s: float = QUOTA_TIMEOUT_S) -> Tuple[int, int]:
    """The account's inode usage and quota, from the site's command run through `runner` (the login node's Runner):
    (used, quota). QuotaRefused for whatever leaves them unread."""
    argv, pattern = command_of(site), pattern_of(site)
    try:
        done = runner.run(argv, timeout_s)
    except (SlurmError, TimeoutError):
        raise QuotaRefused(f"the quota command did not answer within {timeout_s:.0f} s") from None
    except (OSError, ValueError) as error:
        raise QuotaRefused(f"the quota command could not be run ({type(error).__name__})") from None
    if done.returncode != 0:
        raise QuotaRefused(f"the quota command failed with status {done.returncode} ({one_line(done.stderr)})")
    return parse_quota(done.stdout, pattern)


def check_inodes(runner: Any, site: Site, estimate: int, timeout_s: float = QUOTA_TIMEOUT_S) -> str:
    """Refuse a federation whose estimate, added to the usage, passes 95% of the quota; what `up` says otherwise. The
    comparison is in integers, so a total exactly at the line passes: used + estimate <= quota * 95 // 100."""
    used, quota = read_quota(runner, site, timeout_s)
    line = quota * LINE_PERCENT // 100
    figures = f"used {used}, estimate {estimate}, quota {quota}, {LINE_PERCENT}% line {line}"
    if used + estimate > line:
        raise QuotaRefused(
            f"the federation's inodes would pass {LINE_PERCENT}% of the account's inode quota on scratch ({figures}): "
            "free inodes, or ask for a larger quota; nothing was submitted"
        )
    return f"inode check: {figures}: under the line"


def guard(runner: Any, site: Site, layout: Any, environ: Mapping[str, str], federations: int = 1) -> str:
    """`up`'s check: nothing without a scratch root; else, always, the estimate from the layout's manifests against the
    quota. A site with a scratch root and `HPC_QUOTA_CHECK=off` is refused, never taken as a skip. `federations` is how
    many federations of this layout come up at once (`fleet.py`): the usage is read once, against that many estimates,
    since each of that many separate ups would read the same usage."""
    if not site.storage_root:
        return ""
    if site.quota_check == QUOTA_CHECK_OFF:
        raise QuotaRefused(
            f"HPC_QUOTA_CHECK={QUOTA_CHECK_OFF} with HPC_STORAGE_ROOT: the inode check of a federation bound onto "
            "scratch cannot be turned off; nothing was submitted"
        )
    values = hpcdata.study_values(site.study_artifacts)
    return check_inodes(runner, site, federations * sum(estimate_inodes(layout, environ, values).values()))


# -- the home guard -------------------------------------------------------------------------------------------------


def home_pattern_of(site: Site) -> "re.Pattern[str]":
    """The compiled `HPC_HOME_QUOTA_PATTERN`, refused unless it compiles and names the four groups."""
    named = ", ".join(HOME_GROUPS[:-1]) + f" and {HOME_GROUPS[-1]}"
    if not site.home_quota_pattern:
        raise QuotaRefused(
            "HPC_HOME_QUOTA_PATTERN is not set, so the account's home quota cannot be read: give the regular "
            f"expression, with the named groups {named}, that reads the command's output (or unset "
            "HPC_HOME_QUOTA_CMD too, for no check)"
        )
    try:
        found = re.compile(site.home_quota_pattern)
    except re.error as error:
        raise QuotaRefused(f"HPC_HOME_QUOTA_PATTERN is not a regular expression ({error.msg})") from None
    if not set(HOME_GROUPS) <= set(found.groupindex):
        raise QuotaRefused(f"HPC_HOME_QUOTA_PATTERN needs the named groups {named}")
    return found


def parse_home(text: str, pattern: "re.Pattern[str]") -> Dict[str, int]:
    """The four figures of the home's files and bytes from the command's output: exactly one match of the pattern,
    whole counts, and a limit above 0 (a limit of 0 sets no line, and is what a soft figure that reads 0 gives)."""
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        found = "nothing" if not matches else f"{len(matches)} times"
        raise QuotaRefused(
            f"HPC_HOME_QUOTA_PATTERN matched {found} in the home quota command's output (it must match once)"
        )
    found = matches[0]
    command = "home quota command"
    figures = {
        name: count_of(found.group(name), name, MAX_BYTES if name.startswith("bytes") else MAX_COUNT, command)
        for name in HOME_GROUPS
    }
    check_whole(text, found, HOME_GROUPS, "HPC_HOME_QUOTA_PATTERN", command)
    for kind in HOME_KINDS:
        if figures[f"{kind}_limit"] == 0:
            raise QuotaRefused(
                f"the {kind}_limit figure is 0: a limit of 0 sets no line to check against "
                "(HPC_HOME_QUOTA_PATTERN must capture the hard limit, not a soft one that reads 0)"
            )
    return figures


def read_home(runner: Any, site: Site, timeout_s: float = QUOTA_TIMEOUT_S) -> Dict[str, int]:
    """The home's used and limit figures of its files and bytes, from the site's command run through `runner`.
    QuotaRefused for whatever leaves them unread, naming a status or a variable and never what the tool said: its
    stderr and its command line name the user and the home's path."""
    argv = argv_of(
        site.home_quota_cmd,
        "HPC_HOME_QUOTA_CMD",
        "the account's home quota",
        "name the site's command (or unset HPC_HOME_QUOTA_PATTERN too, for no check)",
    )
    pattern = home_pattern_of(site)
    try:
        done = runner.run(argv, timeout_s)
    except (SlurmError, TimeoutError):
        raise QuotaRefused(f"the home quota command did not answer within {timeout_s:.0f} s") from None
    except (OSError, ValueError) as error:
        raise QuotaRefused(f"the home quota command could not be run ({type(error).__name__})") from None
    if done.returncode != 0:
        raise QuotaRefused(f"the home quota command failed with status {done.returncode}")
    return parse_home(done.stdout, pattern)


def check_home(runner: Any, site: Site, timeout_s: float = QUOTA_TIMEOUT_S) -> str:
    """Refuse a home whose files or bytes are past 95% of their limit, naming each figure that is past; what `up` says
    otherwise. Integers: used <= limit * 95 // 100 passes, so a figure exactly at the line passes."""
    figures = read_home(runner, site, timeout_s)
    said, past = [], []
    for kind in HOME_KINDS:
        used, limit = figures[f"{kind}_used"], figures[f"{kind}_limit"]
        line = limit * LINE_PERCENT // 100
        said.append(f"{kind} used {used}, limit {limit}, {LINE_PERCENT}% line {line}")
        if used > line:
            past.append(said[-1])
    if past:
        raise QuotaRefused(
            f"the account's home is past {LINE_PERCENT}% of its quota ({'; '.join(past)}): free its files or bytes, or "
            "ask for a larger quota; nothing was submitted"
        )
    return f"home check: {'; '.join(said)}: under the line"


def home_guard(runner: Any, site: Site, timeout_s: float = QUOTA_TIMEOUT_S) -> str:
    """`up`'s home check: what it says (a line for `up` to print, empty for nothing to say). `HPC_HOME_QUOTA_CHECK=off`
    skips it and says so; with neither key set there is no check and nothing to say; with either set it reads the home
    and fails closed. It does not depend on a scratch root: the home is shared whatever the storage is."""
    if site.home_quota_check == QUOTA_CHECK_OFF:
        return f"home check: skipped: HPC_HOME_QUOTA_CHECK={QUOTA_CHECK_OFF}"
    if not site.home_quota_cmd and not site.home_quota_pattern:
        return ""
    return check_home(runner, site, timeout_s)
