"""The pure parts of the HPC test runner (README.md): the settings a local `site.env` gives, the argv of a test step and
the environment allowlist it runs behind, the stream a step sends back, and what pytest wrote. Nothing here runs a
command or touches the network; `hpctests.py` does, and the tests drive these functions directly.

The rules every step obeys are here, in one place each:
- a step is `srun --jobid=<ours> --exact` with its own cores, memory and time limit, never `--overlap`;
- its environment is the allowlist (`step_environment`) behind `/usr/bin/env -i`: nothing of the login shell reaches it;
- nothing from `site.env` but the job id and the node's tmpfs root is on its command line;
- it writes only under the node's tmpfs root (`/tmp/<name>` or `/dev/shm/<name>`, checked again on the node).

What a step sends back, and the login node's listing of our allocations, are read by `hpctestparse.py`.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

ENV = "/usr/bin/env"
BASH = "/bin/bash"
FRAME_START = "HPCT-RESULT"
FRAME_END = "HPCT-END"
LOG = "HPCT-LOG"
FAIL = "HPCT-FAIL"

JOB_ID = re.compile(r"[0-9]{1,12}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
ENV_VALUE = re.compile(r"[A-Za-z0-9_./:@=+-]*")  # as deploy/hpc-federation/hpcslurm.py's allowlist
MEMORY = re.compile(r"([1-9][0-9]{0,6})([MG])")
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
NODE_ROOT = re.compile(r"/(?:tmp|dev/shm)/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
LOGIN_DIR = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,63}(?:/[A-Za-z0-9._-]{1,64}){0,3}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,252}")
STEP_PATH = re.compile(r"/[A-Za-z0-9_./-]*(?::/[A-Za-z0-9_./-]*)*")
PYTHON = re.compile(r"3\.[0-9]{1,2}(?:\.[0-9]{1,2})?")
UV_VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
ABS_PATH = re.compile(r"/[A-Za-z0-9_./-]{1,4095}")

KNOWN_KEYS = {
    "HPCT_HOST",
    "HPCT_HOST_FILE",
    "HPCT_LOGIN_DIR",
    "HPCT_NODE_ROOT",
    "HPCT_STEP_PATH",
    "HPCT_UV",
    "HPCT_UV_VERSION",
    "HPCT_PYTHON",
    "HPCT_OUT",
    "HPCT_WORK",
    "HPCT_ACCOUNT",
    "HPCT_PARTITION",
    "HPCT_JOB_NAME",
    "HPCT_CPUS",
    "HPCT_MEM",
    "HPCT_HOURS",
    "HPCT_MAX_ALIVE",
}


class HpcTestsError(Exception):
    """A setting, an argument or a reply that is not what the runner accepts; the message names it."""


class NoAllocation(HpcTestsError):
    """No allocation of ours that a step fits in."""


def checked(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not pattern.fullmatch(value) or ".." in value.split("/"):
        raise HpcTestsError(f"{what} {value!r} is not accepted (allowed: {pattern.pattern})")
    return value


def memory_mib(value: str) -> int:
    """A size such as `6G` or `512M` in MiB."""
    match = MEMORY.fullmatch(value)
    if not match:
        raise HpcTestsError(f"a memory size is a whole number of M or G, such as 6G, not {value!r}")
    number, unit = int(match.group(1)), match.group(2)
    return number * 1024 if unit == "G" else number


@dataclass(frozen=True)
class Site:
    """What `site.env` says. `host` is the ssh destination: it is passed to ssh and never printed (`repr` hides it)."""

    host: str = field(repr=False)
    login_dir: str = ".expcore-hpct"
    node_root: str = "/tmp/expcore-hpct"
    step_path: str = "/usr/local/bin:/usr/bin:/bin"
    uv: str = ""
    uv_version: str = ""
    python: str = "3.13"
    out: str = ""
    work: str = ""
    account: str = field(default="", repr=False)
    partition: str = field(default="", repr=False)
    job_name: str = "expcore-tests"
    cpus: int = 16
    mem: str = "30G"
    hours: int = 12
    max_alive: int = 2


def parse_env_file(text: str) -> dict[str, str]:
    """`KEY=value` lines (an optional `export `, the value bare or in one pair of quotes); `#` lines and blank lines
    are skipped. Nothing is expanded or run."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if not sep or not ENV_KEY.fullmatch(key):
            raise HpcTestsError(f"line {number} of site.env is not KEY=value")
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if any(c in value for c in "'\"`$\\"):
            raise HpcTestsError(f"{key} in site.env holds a quote, '$', '`' or '\\'")
        values[key] = value
    return values


def _positive(values: Mapping[str, str], key: str, default: int, top: int) -> int:
    raw = values.get(key, str(default))
    if not raw.isdigit() or raw.startswith("0") or not 1 <= int(raw) <= top:
        raise HpcTestsError(f"{key} is a whole number from 1 to {top}, not {raw!r}")
    return int(raw)


def site_from(values: Mapping[str, str], host_file_text: str | None = None) -> Site:
    """The site, every value checked. The host comes from HPCT_HOST or from the file HPCT_HOST_FILE names (read by
    the caller and passed in as `host_file_text`)."""
    unknown = sorted(k for k in values if k.startswith("HPCT_") and k not in KNOWN_KEYS)
    if unknown:
        raise HpcTestsError(f"site.env has keys the runner does not know: {', '.join(unknown)}")
    host = values.get("HPCT_HOST") or (host_file_text or "").strip()
    if not host:
        raise HpcTestsError("site.env gives no HPCT_HOST or HPCT_HOST_FILE")
    if not HOST.fullmatch(host):
        raise HpcTestsError("the ssh destination has characters an ssh destination does not carry")
    site = Site(
        host=host,
        login_dir=checked(LOGIN_DIR, values.get("HPCT_LOGIN_DIR", Site.login_dir), "HPCT_LOGIN_DIR"),
        node_root=checked(NODE_ROOT, values.get("HPCT_NODE_ROOT", Site.node_root), "HPCT_NODE_ROOT"),
        step_path=checked(STEP_PATH, values.get("HPCT_STEP_PATH", Site.step_path), "HPCT_STEP_PATH"),
        uv=checked(ABS_PATH, values["HPCT_UV"], "HPCT_UV") if values.get("HPCT_UV") else "",
        uv_version=checked(UV_VERSION, values["HPCT_UV_VERSION"], "HPCT_UV_VERSION")
        if values.get("HPCT_UV_VERSION")
        else "",
        python=checked(PYTHON, values.get("HPCT_PYTHON", Site.python), "HPCT_PYTHON"),
        out=values.get("HPCT_OUT", ""),
        work=values.get("HPCT_WORK", ""),
        account=checked(NAME, values["HPCT_ACCOUNT"], "HPCT_ACCOUNT") if values.get("HPCT_ACCOUNT") else "",
        partition=checked(NAME, values["HPCT_PARTITION"], "HPCT_PARTITION") if values.get("HPCT_PARTITION") else "",
        job_name=checked(NAME, values.get("HPCT_JOB_NAME", Site.job_name), "HPCT_JOB_NAME"),
        cpus=_positive(values, "HPCT_CPUS", Site.cpus, 192),
        mem=values.get("HPCT_MEM", Site.mem),
        hours=_positive(values, "HPCT_HOURS", Site.hours, 72),
        max_alive=_positive(values, "HPCT_MAX_ALIVE", Site.max_alive, 4),
    )
    memory_mib(site.mem)
    return site


def login_env(site: Site) -> str:
    """The login node's settings for alloc.sh: what sbatch needs, sent over ssh's stdin, never on a command line."""
    if not site.account or not site.partition:
        raise HpcTestsError("site.env gives no HPCT_ACCOUNT or HPCT_PARTITION, which a submit needs")
    pairs = {
        "HPCT_ACCOUNT": site.account,
        "HPCT_PARTITION": site.partition,
        "HPCT_JOB_NAME": site.job_name,
        "HPCT_CPUS": str(site.cpus),
        "HPCT_MEM": site.mem,
        "HPCT_HOURS": str(site.hours),
        "HPCT_MAX_ALIVE": str(site.max_alive),
    }
    return "".join(f"{k}={v}\n" for k, v in pairs.items())


def allowlist(pairs: Mapping[str, str]) -> tuple[str, ...]:
    """The whole environment of a step as `KEY=value` words for `env -i`; a key or a value outside the alphabet is
    refused, so no word can carry a quote, a space, a `$` or a secret's usual characters onto the command line."""
    for key, value in pairs.items():
        if not ENV_KEY.fullmatch(key) or not ENV_VALUE.fullmatch(value):
            raise HpcTestsError(f"the step environment's {key} holds a character a step's argv must not carry")
    return tuple(f"{key}={value}" for key, value in pairs.items())


BOOT_PATH = "/usr/local/bin:/usr/bin:/bin"  # the boot's PATH; the site's own (HPCT_STEP_PATH) travels in the payload


def step_environment(site: Site) -> dict[str, str]:
    """A fixed PATH, the locale and TMPDIR on the node's tmpfs (never Slurm's per-job TMPDIR, a link into a shared
    filesystem on some sites). Nothing else of the login node's environment, and no site value, reaches a step."""
    return {
        "PATH": BOOT_PATH,
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TMPDIR": site.node_root.rsplit("/", 1)[0],
    }


@dataclass(frozen=True)
class Step:
    """One step of a held job: its size, its name and its command behind the environment allowlist."""

    job: str
    name: str
    cpus: int
    memory_mib: int
    minutes: int
    env: tuple[str, ...]
    command: tuple[str, ...]

    def argv(self) -> list[str]:
        checked(JOB_ID, self.job, "the job id")
        checked(NAME, self.name, "the step name")
        if not 1 <= self.cpus <= 192 or not 64 <= self.memory_mib <= 393216 or not 1 <= self.minutes <= 4320:
            raise HpcTestsError("a step takes 1 to 192 cores, 64M to 384G and 1 to 4320 minutes")
        return [
            "srun",
            f"--jobid={self.job}",
            "--exact",
            "-N1",
            "-n1",
            "-c",
            str(self.cpus),
            f"--mem={self.memory_mib}M",
            f"--time={self.minutes}",
            "-J",
            self.name,
            "--chdir=/",
            "--export=PATH",
            ENV,
            "-i",
            *self.env,
            *self.command,
        ]


USE_LOCK = "use.lock"  # in the login directory: every run holds it shared; the idle release needs it exclusive


def remote_command(argv: Sequence[str], login_dir: str | None = None, bound_s: int | None = None) -> str:
    """The line the login node's shell runs: every word quoted (the boot's newlines stay inside its quotes), and
    `exec`. With `login_dir`, the step runs under a shared lock on its USE_LOCK, so the idle release never cancels an
    allocation a run is using or about to use; with `bound_s`, under `timeout` on the login node, so an srun client
    that never returns (seen on a site) cannot hold that lock past the run's own bound."""
    if any("\0" in word for word in argv):
        raise HpcTestsError("a remote word holds a NUL")
    words: list[str] = []
    if login_dir is not None:
        words += ["flock", "-s", "-w", "600", f"{checked(LOGIN_DIR, login_dir, 'HPCT_LOGIN_DIR')}/{USE_LOCK}"]
    if bound_s is not None:
        words += ["timeout", "-k", "30", str(int(bound_s))]
    return "exec " + shlex.join([*words, *argv])


# The step's first program: read the payload from stdin into a fresh 0700 run directory under the node's tmpfs root,
# check its SHA-256, unpack it, and hand over to the node.sh it carries. Every refusal prints one HPCT-FAIL line.
BOOT = r"""set -eu
umask 077
fail() { echo "HPCT-FAIL boot: $*"; exit 72; }
prefix=$1 run=$2 sum=$3
shift 3
case "$prefix" in /tmp/?* | /dev/shm/?*) ;; *) fail "the node root must be under /tmp or /dev/shm";; esac
case "$run" in "" | . | .. | *[!A-Za-z0-9._-]*) fail "bad run id";; esac
case "$sum" in *[!0-9a-f]* | "") fail "bad sha256";; esac
uid=$(id -u)
[ "$uid" != 0 ] || fail "refusing to run as root"
[ "$(stat -f -c %T "${prefix%/*}")" = tmpfs ] || fail "the node root is not on a tmpfs"
root=$prefix-$uid
mkdir -p -m 0700 "$root"
[ -d "$root" ] && [ ! -L "$root" ] || fail "the node root is not a directory"
[ "$(stat -c %u "$root")" = "$uid" ] || fail "the node root is not ours"
chmod 0700 "$root"
[ "$(readlink -f "$root")" = "$root" ] || fail "the node root goes through a link"
[ "$(stat -f -c %T "$root")" = tmpfs ] || fail "the node root is not on a tmpfs"
mkdir -p -m 0700 "$root/runs"
[ -d "$root/runs" ] && [ ! -L "$root/runs" ] || fail "runs is not a directory"
[ "$(stat -c %u "$root/runs")" = "$uid" ] || fail "runs is not ours"
dir=$root/runs/$run
mkdir -m 0700 "$dir" || fail "the run directory exists already"
mkdir -m 0700 "$dir/kit"
cat > "$dir/payload.tar"
got=$(sha256sum "$dir/payload.tar" | cut -d' ' -f1)
[ "$got" = "$sum" ] || { rm -rf "$dir"; fail "the payload arrived as sha256 $got, not $sum"; }
tar -xf "$dir/payload.tar" -C "$dir/kit"
rm -f "$dir/payload.tar"
exec /bin/bash "$dir/kit/node.sh" "$root" "$run" "$@"
"""


def boot_command(node_root: str, run_id: str, payload_sha256: str, *node_args: str) -> tuple[str, ...]:
    """The step's command: bash with BOOT, then the node root, the run id, the payload's SHA-256 and node.sh's words."""
    checked(NODE_ROOT, node_root, "the node root")
    checked(NAME, run_id, "the run id")
    checked(SHA256, payload_sha256, "the payload sha256")
    for word in node_args:
        checked(NAME, word, "a node.sh argument")
    return (BASH, "-c", BOOT, "hpct-boot", node_root, run_id, payload_sha256, *node_args)


def node_settings(
    *, sha: str, uv: str, uv_version: str, python: str, timeout_s: int, cores: int, keep: bool, step_path: str = ""
) -> str:
    """The `settings` file of the payload, which node.sh reads line by line (it is never sourced)."""
    checked(SHA1, sha, "the commit")
    checked(ABS_PATH, uv, "HPCT_UV")
    if uv_version:
        checked(UV_VERSION, uv_version, "HPCT_UV_VERSION")
    checked(PYTHON, python, "HPCT_PYTHON")
    if step_path:
        checked(STEP_PATH, step_path, "HPCT_STEP_PATH")
    if not 60 <= timeout_s <= 259200 or not 1 <= cores <= 192:
        raise HpcTestsError("the timeout is 60 s to 72 h and the cores 1 to 192")
    pairs = {
        "HPCT_SHA": sha,
        "HPCT_UV": uv,
        "HPCT_UV_VERSION": uv_version,
        "HPCT_PYTHON": python,
        "HPCT_TIMEOUT_S": str(timeout_s),
        "HPCT_CORES": str(cores),
        "HPCT_KEEP": "1" if keep else "0",
        "HPCT_STEP_PATH": step_path,
    }
    return "".join(f"{k}={v}\n" for k, v in pairs.items())


# The pytest arguments a run may pass: an ALLOWLIST, since pytest also reads clustered short options (`-qo ...`) and
# argument files (`@file`), which no list of refused tokens can see into. Anything else is refused before a step
# starts: an option that could write outside the run directory, or move where the step's results go, or run no test.
FLAGS = frozenset(
    {"-q", "-qq", "-v", "-vv", "-vvv", "-x", "-s", "-l", "--exitfirst", "--showlocals", "--strict-markers",
     "--runxfail", "--no-header", "--no-summary", "--disable-warnings"}
)  # fmt: skip
WITH_VALUE = frozenset(
    {"-k", "-m", "-W", "--deselect", "--ignore", "--ignore-glob", "--maxfail", "--durations", "--tb"}
)
REPORT_CHARS = re.compile(r"-r[fEsxXpPaAN]+")
TEST_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*(?:::[A-Za-z0-9_.\[\]\-:, ]+)?")


def check_pytest_args(args: Sequence[str]) -> list[str]:
    """The arguments, refused unless each is a FLAGS flag, a `-r<chars>` report option, a WITH_VALUE option with its
    value (next, or after `=`), or a test path or node id relative to the clone (no `..`; no leading `/`, `-`, `@`)."""
    out, pending = list(args), None
    for arg in out:
        if pending is not None:
            pending = None
            continue
        name = arg.split("=", 1)[0]
        if arg in FLAGS or REPORT_CHARS.fullmatch(arg):
            continue
        if name in WITH_VALUE:
            pending = None if "=" in arg else name
            continue
        if TEST_PATH.fullmatch(arg) and ".." not in arg.split("::", 1)[0].split("/"):
            continue
        raise HpcTestsError(
            f"the pytest argument {arg!r} is not one a run takes: test paths, {', '.join(sorted(FLAGS))}, -r<chars>,"
            f" and {', '.join(sorted(WITH_VALUE))} with a value (README, 'pytest arguments')"
        )
    if pending is not None:
        raise HpcTestsError(f"the pytest option {pending} needs a value")
    return out
