"""Bring a throwaway Onedata federation up on HPC batch nodes, and take it down, from the Compose federation's layout
file (DESIGN.md is the process model; README.md says what has run).

    uv run python -I -B deploy/hpc-federation/hpcfed.py --layout PATH --root PATH up | down | describe | verify-data
    uv run python -I -B deploy/hpc-federation/hpcfed.py --layout PATH --root PATH stop | start | status SERVICE
    uv run python -I -B deploy/hpc-federation/hpcfed.py --layout PATH --root PATH exec SERVICE -- ARGV...
    (the fault relay's own: `--step-tag TAG` before a verb, and `late-steps SERVICE --for-verb VERB --request ID
    --window-s N --poll-s N --since T`; see hpclate.py)

Run on the login node. `up` reads the site's values from the environment (`set -a; . site.env; set +a`) and records
them in the state file in the root, so `down` and the service verbs need no environment: the login node's fault relay
(M4), which `up` starts last, runs the verbs through this script's argv prefix, with `--layout` and `--root`.

- up: one held job of `1 + providers` nodes, submitted and read back, with the agent's node when the layout declares
  one (`[hpc] sandbox`, or the agent's limits in its template) as the job's component 1 of its own shape, or as a held
  job of its own (`[hpc] sandbox_placement`); the zone and each provider a service step on its own node (the bundle's
  `zone.sbatch` and `provider.sbatch`), each provider registered by a token minted on the zone and piped into its
  delivery step; the zone's passphrase and CA into the root; then the bootstrap, the baseline, facts.json,
  platform.toml and the platform layer's link check, which writes link-check.json in the root
  (`hpcboot.core_phase`). The layout's `[hpc] core` says where that phase runs: "login" (in this process) or "step"
  (a control step on the zone's node, the core's Python).
- down: every service torn down (the providers, then the zone), every step gone, each node checked, the held job
  (and the agent's own job, when it has one) cancelled by its id, then the root removed; on whatever up reached.
- describe: the plan: the nodes and their shape, every step's size, and what the job bills per hour.
- verify-data: after the study's ingest, read each ingested space (a space that is neither a baseline space nor a
  shared import, whose data is the study's own) back through its owner and compare every file's path, size and SHA-256
  and every directory with the manifest it declares (`data_manifest` on the space, rendered by `up`); it exits 1,
  naming the space and how many files differ, on any difference, and records a verified marker in the state file on a
  pass, which `shards.py plan` requires (O57). It reads the root and needs no site environment; it never touches the
  job.
- stop, start, status, exec: the service verbs, `hpcfed_control.run` (M4), through the hook in `main`. With
  `--step-tag TAG` (the fault relay's: 12 hex digits) every control step a verb launches is named by the tag, so the
  relay can find its steps by their exact names.
- late-steps: the fault relay's watch for the steps of a verb it answered `timed_out` (`hpclate.py`): it cancels, by
  exact id and in the federation's own job alone, a step of the verb's exact names that is made late, and journals what
  it saw in the relay's directory. No request can name it.
- core: the core's phase alone, as the step of `[hpc] core = "step"` runs it.
"""

import argparse
import math
import os
import pwd
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Mapping, Optional, Sequence

sys.dont_write_bytecode = True  # nothing of the checkout or the venv is written on the login node or a node
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _path in (HERE, REPO / "deploy" / "compose-federation"):  # python -I leaves the script's directory out of sys.path
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fedlayout import Layout, checked_root  # noqa: E402
from fedspec import DEFAULT_LAYOUT, LayoutError, Spec, load_spec  # noqa: E402
from hpcdriver import CORE_MISMATCH, CoreMismatch, Driver  # noqa: E402
from hpclaunch import Launcher, StateStore  # noqa: E402
from hpcsettings import SettingsError, agent_limits, load_settings, site_from_environment  # noqa: E402
from hpcslurm import Runner, Slurm  # noqa: E402

CONTROL_VERBS = ("stop", "start", "status", "exec")  # hpcfed_control.VERBS (M4)
COMMANDS = ("up", "down", "describe", "core", "verify-data", *CONTROL_VERBS, "late-steps")
STEP_TAG = re.compile(r"[0-9a-f]{12}")  # hpcfed_control.STEP_TAG
REQUEST_ID = re.compile(r"[0-9a-f]{32}")  # a fault relay request's id
LATE_BOUNDS = (3600.0, 60.0)  # the most `--window-s` and `--poll-s` the watch accepts


def core_paths() -> Dict[str, str]:
    """The files of the core and of the deploy code this interpreter really imports."""
    import fedlayout

    import expcore_onedata

    return {
        "expcore_onedata": str(Path(expcore_onedata.__file__).resolve().parent),
        "fedlayout": str(Path(fedlayout.__file__).resolve()),
        "hpcfed": str(Path(__file__).resolve()),
    }


def self_check() -> Dict[str, str]:
    """The core must be this checkout's (`src/` beside `deploy/`): a step whose allowlist lost a variable, or a stale
    install, would otherwise run another core silently."""
    paths = core_paths()
    if Path(paths["expcore_onedata"]) != REPO / "src" / "expcore_onedata":
        raise CoreMismatch("the interpreter imports a core that is not this checkout's src/expcore_onedata")
    return paths


def root_layout(value: Optional[str], spec: Spec) -> Layout:
    """The federation's root from `--root` (or EXPCORE_FED_ROOT): an absolute path in an existing directory, reached
    through no symbolic link, since down removes it whole."""
    if not value:
        raise ValueError("name the federation's root with --root (or EXPCORE_FED_ROOT)")
    if not os.path.isabs(value):
        raise ValueError("--root must be an absolute path")
    parent = Path(os.path.normpath(value)).parent
    if not parent.is_dir():
        raise ValueError("--root must lie in an existing directory")
    return Layout(checked_root(value, parent, strict=True), parent, spec)


def make_driver(
    layout_path: Path,
    root: Optional[str],
    runner: Optional[Runner] = None,
    environ: Optional[Mapping[str, str]] = None,
    *,
    agent: bool = True,
) -> Driver:
    """The driver of one layout and one root; the runner is the tests' fake Slurm, or the real one. `environ` is the
    environment the driver reads a shared imported tree and its manifest from (O59), and gives the fault relay: this
    process's own when it is None. `agent`: whether the agent's limits in the layout's template size the agent's node,
    which only up and describe need; down and the verbs work from the state file, so a template edited since up never
    stops them."""
    layout_path = layout_path.resolve()
    spec = load_spec(layout_path)
    layout = root_layout(root, spec)
    settings = load_settings(spec.hpc, agent_limits(spec.platform_template) if agent else None)
    slurm = Slurm(runner or Runner())
    store = StateStore(layout.root)
    command = [sys.executable, "-I", "-B", str(HERE / "hpcfed.py"), "--layout", str(layout_path), "--root",
               str(layout.root)]  # fmt: skip
    given = {} if environ is None else {"environ": dict(environ)}
    return Driver(
        layout_path=layout_path,
        layout=layout,
        settings=settings,
        slurm=slurm,
        store=store,
        launcher=Launcher(slurm, store, settings),
        user=pwd.getpwuid(os.getuid()).pw_name,
        command=command,
        core_paths=core_paths(),
        **given,
    )


def mount_check(driver: Driver) -> Optional[SimpleNamespace]:
    """The read-only proof of an owner's shared imported trees (O59, `hpcimport`), as the service verbs take it: a
    restarted owner is ready again only once its trees are proven read-only for that launch (`hpcfed_control`). None
    in a core without the shared import, whose layouts declare none."""
    try:
        import hpcimport
    except ImportError:
        return None
    from hpcdown import BUNDLE

    return SimpleNamespace(
        spaces=lambda service: [space.name for space in hpcimport.shared_imports_of(driver.layout, service)],
        argv=lambda workdir, python: hpcimport.mount_check_argv(str(BUNDLE), workdir, python=python),
        problems=hpcimport.mount_check_problems,
    )


def control_hook(
    driver: Driver, verb: str, service: Optional[str], rest: List[str], step_tag: Optional[str] = None
) -> int:
    """The service verbs (DESIGN.md, section 7), by `hpcfed_control.run` (M4). The hook is stable: the verb, the
    service, what follows `--` (for exec), the launcher, the state store, the layout's stop bound and the read-only
    proof of a shared import's owner (`mount_check`)."""
    try:
        import hpcfed_control
    except ImportError:
        print(f"error: {verb}: the service verbs are not built yet (hpcfed_control.py, M4)", file=sys.stderr)
        return 2
    if verb not in hpcfed_control.VERBS:
        print(f"error: {verb}: not a verb of hpcfed_control", file=sys.stderr)
        return 2
    given = {} if step_tag is None else {"step_tag": step_tag}  # only the relay's verbs carry one
    return hpcfed_control.run(
        verb,
        service,
        rest,
        launcher=driver.launcher,
        state=driver.store,
        stop_seconds=driver.settings.stop_seconds,
        mounts=mount_check(driver),
        **given,
    )


def late_steps(driver: Driver, args: argparse.Namespace) -> int:
    """`late-steps`: the fault relay's watch for a timed-out verb's steps (`hpclate.py`). Every argument is checked
    before Slurm is looked at: the tag, the request id, the verb (one of the four), the service and the numbers. It
    journals to the relay's own file in this federation's root, which it derives from the root, and nowhere else."""
    import hpcfed_control
    import hpclate

    from expcore_onedata.fault_relay_late import JOURNAL

    journal = driver.layout.root / "fault-relay" / JOURNAL
    window, poll = args.window_s, args.poll_s
    numbers = all(
        isinstance(n, float) and math.isfinite(n) and 0 < n <= top for n, top in zip((window, poll), LATE_BOUNDS)
    )
    problems = [
        text
        for ok, text in (
            (args.step_tag is not None and STEP_TAG.fullmatch(args.step_tag), "--step-tag is 12 hex digits"),
            (args.service, "name the service"),
            (args.for_verb in hpcfed_control.VERBS, "--for-verb is one of %s" % ", ".join(hpcfed_control.VERBS)),
            (args.request is not None and REQUEST_ID.fullmatch(args.request), "--request is a request id"),
            (numbers, "--window-s and --poll-s are positive numbers within bounds"),
            (
                args.since is None or (math.isfinite(args.since) and args.since > 0),
                "--since is a time in epoch seconds",
            ),
        )
        if not ok
    ]
    if problems:
        print("error: late-steps: %s" % "; ".join(problems), file=sys.stderr)
        return 2
    return hpclate.watch(
        driver.launcher,
        driver.store,
        args.service,
        args.for_verb,
        args.step_tag,
        request=args.request,
        journal=journal,
        window_s=window,
        poll_s=poll,
        since=args.since,
    )


def main(
    argv: Optional[Sequence[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
    runner: Optional[Runner] = None,
) -> int:
    words = list(sys.argv[1:] if argv is None else argv)
    rest: List[str] = []
    if "--" in words:
        cut = words.index("--")
        words, rest = words[:cut], words[cut + 1 :]
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--layout", type=Path, default=DEFAULT_LAYOUT, help="the layout file")
    parser.add_argument("--root", help="the federation's root (default: EXPCORE_FED_ROOT)")
    parser.add_argument("--step-tag", help=argparse.SUPPRESS)  # the fault relay's tag for a verb's control steps
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("service", nargs="?", help="the service a verb acts on, by its layout name")
    for late in ("--for-verb", "--request"):  # late-steps only: the fault relay's own watch
        parser.add_argument(late, help=argparse.SUPPRESS)
    parser.add_argument("--window-s", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--poll-s", type=float, help=argparse.SUPPRESS)
    parser.add_argument(
        "--since", type=float, help=argparse.SUPPRESS
    )  # late-steps: the epoch second the verb was answered
    args = parser.parse_args(words)
    environ = os.environ if environ is None else environ
    try:
        root = args.root or environ.get("EXPCORE_FED_ROOT")
        driver = make_driver(args.layout, root, runner, environ, agent=args.command in ("up", "describe"))
    except (LayoutError, SettingsError, ValueError) as error:
        print(f"error: {args.command}: {error}", file=sys.stderr)
        return 2
    if args.step_tag is not None and not STEP_TAG.fullmatch(args.step_tag):
        print("error: %s: --step-tag is 12 hex digits" % args.command, file=sys.stderr)
        return 2
    if args.step_tag is not None and args.command not in (*CONTROL_VERBS, "late-steps"):
        print("error: %s: --step-tag belongs to a verb or to late-steps" % args.command, file=sys.stderr)
        return 2
    if args.command != "late-steps" and any(
        value is not None for value in (args.for_verb, args.request, args.window_s, args.poll_s, args.since)
    ):
        print("error: %s: the late-steps options belong to late-steps" % args.command, file=sys.stderr)
        return 2
    if args.command == "late-steps":
        return late_steps(driver, args)
    if args.command in CONTROL_VERBS:
        return control_hook(driver, args.command, args.service, rest, args.step_tag)
    try:
        if args.command == "describe":
            weight = environ.get("HPC_BILLING_MEM_WEIGHT")
            print(driver.describe(float(weight) if weight else None))
        elif args.command == "core":
            self_check()
            print(driver.core_here())
        elif args.command == "verify-data":
            self_check()
            print(driver.verify_data())
        elif args.command == "up":
            self_check()
            print(driver.up(site_from_environment(environ)))
        elif args.command == "down":  # never an `else`: a command added without a branch would tear the federation down
            print(driver.down())
        else:
            raise AssertionError(f"{args.command}: no branch")  # pragma: no cover
    except CoreMismatch as error:
        print(f"error: {args.command}: {error}", file=sys.stderr)
        return CORE_MISMATCH
    except (RuntimeError, TimeoutError, OSError, SettingsError, ValueError) as error:
        print(f"error: {args.command}: {error}", file=sys.stderr)
        if args.command == "up":
            print("error: up: what it placed is recorded in the root; run down to release it", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
