"""The inode guard of the HPC federation's `up` (DESIGN.md, section 11): provider storage goes on the cluster's shared
scratch filesystem, which has room for the bytes but a limited inode quota, so before anything is submitted `up` reads
the account's inode usage and quota, estimates the federation's inodes from its data manifests, and refuses a
federation that would pass 95% of the quota.

The site's tool and its output are never in this repository. `site.env` names the command (`HPC_QUOTA_CMD`, split as a
shell would split it and run without one, on the login node) and the regular expression that reads its output
(`HPC_QUOTA_PATTERN`, with the named groups `used` and `quota`); the tests run an invented command and format.

**Failing closed.** The guard applies whenever `HPC_STORAGE_ROOT` is set (nothing of the federation lands on scratch
without it). Anything that leaves the quota unread refuses `up`, naming why: no command or pattern set, a pattern that
does not compile or lacks a group, a command that cannot run, exits non-zero or does not answer, an output the pattern
matches never or more than once, a figure that is no count, a quota of 0 or "unlimited". Only `HPC_QUOTA_CHECK=off`,
which `up` says it honoured, skips the check; unset is not off.

**The estimate** is every space's manifest counted once, files and directories (each is an inode), a shared import's
manifest being the layout's own file and every other space's the baseline `up` writes (`hpcdata.declared`, the one count
the core's phase writes the data manifests from). A shared import's owner holds the tree where it already is and adds
nothing on scratch, but the copy a mirror fetches does, so it counts once like the rest; a replica on every mirror is
not multiplied in, and the 5% below the line is the room for what the estimate leaves out. A layout whose spaces
declare no data (the ingest layout's, `baseline = false`) estimates what its baseline writes, a handful: the study's own
ingest is outside every manifest `up` knows.
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
GROUPS = ("used", "quota")


class QuotaRefused(RuntimeError):
    """The federation is refused: the quota cannot be read, or the federation would pass the line. Names why."""


def estimate_inodes(layout: Any, environ: Mapping[str, str]) -> Dict[str, int]:
    """Each space's inodes: its manifest's files and directories, nothing written (`hpcdata.declared`)."""
    return {name: len(m.files) + len(m.directories) for name, m in hpcdata.declared(layout, environ).items()}


def pattern_of(site: Site) -> "re.Pattern[str]":
    """The compiled `HPC_QUOTA_PATTERN`, refused unless it compiles and names the groups `used` and `quota`."""
    if not site.quota_pattern:
        raise QuotaRefused(
            "HPC_QUOTA_PATTERN is not set, so the inode quota cannot be read: give the regular expression, with the "
            "named groups used and quota, that reads the command's output (or set HPC_QUOTA_CHECK=off)"
        )
    try:
        found = re.compile(site.quota_pattern)
    except re.error as error:
        raise QuotaRefused(f"HPC_QUOTA_PATTERN is not a regular expression ({error.msg})") from None
    if not set(GROUPS) <= set(found.groupindex):
        raise QuotaRefused("HPC_QUOTA_PATTERN needs the named groups used and quota")
    return found


def command_of(site: Site) -> list:
    """The quota command as an argv: `HPC_QUOTA_CMD` split as a shell would split it, never passed to one."""
    if not site.quota_cmd:
        raise QuotaRefused(
            "HPC_QUOTA_CMD is not set, so the inode quota cannot be read: name the site's command (or set "
            f"HPC_QUOTA_CHECK={QUOTA_CHECK_OFF} to skip the check)"
        )
    try:
        argv = shlex.split(site.quota_cmd)
    except ValueError as error:
        raise QuotaRefused(f"HPC_QUOTA_CMD is not a command line ({error})") from None
    if not argv:
        raise QuotaRefused("HPC_QUOTA_CMD holds no command")
    return argv


def count_of(value: Optional[str], which: str) -> int:
    if value is None or COUNT.fullmatch(value) is None or int(value.replace(",", "")) >= MAX_COUNT:
        raise QuotaRefused(f"the {which} figure the quota command printed is not a count")
    return int(value.replace(",", ""))


def parse_quota(text: str, pattern: "re.Pattern[str]") -> Tuple[int, int]:
    """The used count and the quota from the command's output: exactly one match of the pattern, two counts, a quota
    above 0 (a quota of 0, and "unlimited", which no count matches, set no line to check against)."""
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        found = "nothing" if not matches else f"{len(matches)} times"
        raise QuotaRefused(f"HPC_QUOTA_PATTERN matched {found} in the quota command's output (it must match once)")
    found = matches[0]
    used, quota = (count_of(found.group(name), name) for name in GROUPS)
    for name in GROUPS:
        start, end = found.span(name)
        if text[max(start - 1, 0) : start] in TOUCHING or text[end : end + 1] in TOUCHING:
            raise QuotaRefused(
                f"the {name} figure the quota command printed is cut out of a longer one (a decimal point, a unit or a "
                "longer number touches it): HPC_QUOTA_PATTERN must capture the whole figure"
            )
    if quota == 0:
        raise QuotaRefused("a quota of 0 sets no line to check against (HPC_QUOTA_CHECK=off skips the check)")
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
    if site.quota_check == QUOTA_CHECK_OFF:
        return f"inode check: skipped (HPC_QUOTA_CHECK={QUOTA_CHECK_OFF}): the inode quota was not read"
    used, quota = read_quota(runner, site, timeout_s)
    line = quota * LINE_PERCENT // 100
    figures = f"used {used}, estimate {estimate}, quota {quota}, {LINE_PERCENT}% line {line}"
    if used + estimate > line:
        raise QuotaRefused(
            f"the federation's inodes would pass {LINE_PERCENT}% of the account's inode quota on scratch ({figures}): "
            "free inodes, or ask for a larger quota; nothing was submitted"
        )
    return f"inode check: {figures}: under the line"


def guard(runner: Any, site: Site, layout: Any, environ: Mapping[str, str]) -> str:
    """`up`'s check: nothing without a scratch root; else the estimate from the layout's manifests against the quota."""
    if not site.storage_root:
        return ""
    if site.quota_check == QUOTA_CHECK_OFF:
        return check_inodes(runner, site, 0)
    return check_inodes(runner, site, sum(estimate_inodes(layout, environ).values()))
