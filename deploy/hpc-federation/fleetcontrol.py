"""fleetcontrol.py: the elastic fleet's provisioning controller (#462 step 5, C43; README.md, "The elastic fleet's
controller"). `expcore.campaign.elastic.control` decides; this reads the sites, and runs or prints what it decided.

    <python> -I -B deploy/hpc-federation/fleetcontrol.py observe --fleet DIR    # on a site's login node, read only
    <python> -I -B deploy/hpc-federation/fleetcontrol.py control --ledger L --sites S.toml [--once] [--dry-run]
                                                                 [--observed SITE=FILE ...] [--log FILE]
    <python> -I -B deploy/hpc-federation/fleetcontrol.py drain --ledger L --label LABEL
    <python> -I -B deploy/hpc-federation/fleetcontrol.py ended --ledger L --label LABEL --proof cancelled|terminal
    <python> -I -B deploy/hpc-federation/fleetcontrol.py join --ledger L --label LABEL --site S --jobs J[,J]
                                                       --job-end EPOCH --kill-wait S --fingerprint F --environment E

- observe: one read of the site (`fleetobserve`), as JSON on stdout; nothing is written, submitted or cancelled.
- control: every `interval_s` (60 by default), each site is read (`observe` through the site's transport, or the JSON
  file `--observed SITE=FILE` names), the ledger's demand is read, `decide` gives each site's target and the actions,
  and the tick is logged as one JSON line (targets and their limits, each up's queue wait, each action with its reason
  and its commands). `--dry-run` reads the ledger read-only, without its lock, prints each action's commands, and runs
  nothing; otherwise the controller holds the ledger (one process drives it) and runs them (`fleetexecute`). `--once`
  is one tick, waiting for what it started.
- drain, ended, join: the ledger's half of an action, by hand, exactly as the controller calls it.

The controller only adds to a fleet that exists: `fleet.py --fleet F --layout L up N` makes it (at least two). The
sites file (`--sites`, TOML) holds a `[controller]` table (`evidence`, the absolute directory each retired member's
root evidence is collected into; `interval_s`; `log`, the JSON log's absolute path) and one `[[site]]` table per site:
its `label` (an operator's label, `[a-z0-9-]`, never a site's name), `fleet` (its fleet's absolute directory),
`transport` (`local` only: the commands run on this host, the site's login node; a remote site is step 6's),
optionally `interpreter` and `deploy` (the Python and this directory on its login node), and the settings of
`SiteSettings`: `max_federations`, `allowance_units` and `rate_units_per_hour` (required), `kill_wait_s`,
`parallel_ups`, `warm_pool`, `retire_idle_s`, `queue_stall_s`, `lease_bound_s`, `collect_s`, `margin_s`.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
for _path in (HERE, HERE.parents[1] / "deploy" / "compose-federation"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fleetexecute import Executor, LocalTransport, Log, SiteConfig, described, last_line, printed, render  # noqa: E402

COMMANDS = ("observe", "control", "drain", "ended", "join")
CONTROLLER_KEYS = ("evidence", "interval_s", "log")
SITE_KEYS = ("fleet", "transport", "interpreter", "deploy")
TRANSPORTS = ("local",)
OBSERVE_TIMEOUT_S = 600.0


class ConfigError(ValueError):
    """The sites file cannot be used; the message names the key."""


@dataclass(frozen=True)
class Controller:
    evidence: Path
    interval_s: float = 60.0
    log: Optional[Path] = None


@dataclass
class Hooks:
    """What the controller reads and runs through: this host's Slurm and commands by default, the tests' fakes."""

    runner: Callable[[], Any]  # the login node's Runner, for observe
    transport: Callable[[Mapping[str, str]], Any] = LocalTransport
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    identity: Callable[[str, str], Optional[Tuple[str, str]]] = lambda site, member: None
    say: Callable[[str], None] = field(default=print)


def _absolute(value: Any, what: str) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ConfigError(f"{what} must be an absolute path")
    return Path(os.path.normpath(value))


def load_config(path: Path) -> Tuple[Controller, List[SiteConfig]]:
    """The sites file: the controller's table and each site's, every key checked."""
    import tomllib

    from expcore.campaign.elastic.control import SiteSettings

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"the sites file cannot be read ({type(error).__name__})") from None
    table = data.get("controller")
    if not isinstance(table, dict) or set(table) - set(CONTROLLER_KEYS) or "evidence" not in table:
        raise ConfigError(f"[controller] takes {', '.join(CONTROLLER_KEYS)}, and evidence is required")
    interval = table.get("interval_s", 60.0)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 3600:
        raise ConfigError("[controller] interval_s must be from 1 to 3600 seconds")
    log = _absolute(table["log"], "[controller] log") if "log" in table else None
    controller = Controller(_absolute(table["evidence"], "[controller] evidence"), float(interval), log)
    sites = []
    for entry in data.get("site") or []:
        own = {key: entry.pop(key) for key in SITE_KEYS if key in entry}
        if own.get("transport", "local") not in TRANSPORTS:
            raise ConfigError(f"site {entry.get('label')}: transport must be one of {', '.join(TRANSPORTS)}")
        try:
            settings = SiteSettings(**entry)
        except TypeError as error:
            raise ConfigError(f"site {entry.get('label')}: {error}") from None
        fleet = _absolute(own.get("fleet"), f"site {settings.label}: fleet")
        deploy = _absolute(own["deploy"], f"site {settings.label}: deploy") if "deploy" in own else HERE
        sites.append(SiteConfig(settings, fleet, own.get("transport", "local"), own.get("interpreter", sys.executable),
                                deploy))  # fmt: skip
    if not sites or len({s.label for s in sites}) != len(sites):
        raise ConfigError("the sites file needs at least one [[site]], each label once")
    return controller, sites


def site_view(label: str, text: str) -> Any:
    """A site's read, as `observe` printed it."""
    from expcore.campaign.elastic.control import Member, SiteView

    data = json.loads(text)
    members = tuple(Member(**{**m, "jobs": tuple(m["jobs"])}) for m in data.pop("members"))
    return SiteView(label=label, error=None, members=members, **data)


def observe_site(transport: Any, site: SiteConfig, observed: Optional[Path]) -> Any:
    """One read of a site: its `observe` run through its transport, or the file an operator pulled; a site whose
    read fails, or cannot be parsed, is dark for this tick."""
    from expcore.campaign.elastic.control import SiteView

    try:
        if observed is not None:
            text = observed.read_text(encoding="utf-8")
        else:
            argv = site.script("fleetcontrol.py", "observe", "--fleet", str(site.fleet))
            code, text, err = transport.run(site, argv, OBSERVE_TIMEOUT_S)
            if code != 0:
                return SiteView(label=site.label, error=f"its read failed with status {code}: {last_line(err)}")
        return site_view(site.label, text)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        return SiteView(label=site.label, error=f"its read could not be taken or parsed ({type(error).__name__})")


def tick(views: Any, ledger_path: Path, controller: Controller, sites: Sequence[SiteConfig],
         observed: Mapping[str, Path], transport: Any, executor: Optional[Executor], log: Log,
         hooks: Hooks) -> Any:  # fmt: skip
    """One tick: read, decide, log, then run (or, with no executor, print)."""
    from expcore.campaign.elastic.control import decide, ledger_view

    by_label = {site.label: site for site in sites}
    reads = [(site.settings, observe_site(transport, site, observed.get(site.label))) for site in sites]
    busy = executor.busy() if executor is not None else frozenset()
    decision = decide(hooks.clock(), ledger_view(views), reads, busy)
    entries = [described(a, render(a, by_label[a.site], controller.evidence, ledger_path)) for a in decision.actions]
    log.write("tick", dry_run=executor is None, wanted=decision.wanted, targets=decision.targets,
              waits=decision.waits, actions=entries)  # fmt: skip
    if executor is None:
        for entry in entries:
            hooks.say(printed(entry))
    else:
        executor.run(decision)
    return decision


def control(args: argparse.Namespace, environ: Mapping[str, str], hooks: Hooks) -> int:
    from expcore.campaign.elastic import Ledger
    from expcore.campaign.elastic.control import read_only

    controller, sites = load_config(args.sites)
    known = {site.label for site in sites}
    observed: Dict[str, Path] = {}
    for given in args.observed or []:
        label, _, file = given.partition("=")
        if label not in known or not file:
            raise ConfigError(f"--observed takes SITE=FILE for a site of the sites file, not {given!r}")
        observed[label] = Path(file)
    log = Log(args.log or controller.log, hooks.clock)
    transport = hooks.transport(environ)
    if args.dry_run:
        while True:
            views = read_only(args.ledger)
            try:
                tick(views, args.ledger, controller, sites, observed, transport, None, log, hooks)
            finally:
                views.close()
            if args.once:
                return 0
            hooks.sleep(controller.interval_s)
    ledger = Ledger.open(args.ledger, clock=hooks.clock)
    executor = Executor(ledger, args.ledger, {s.label: s for s in sites}, transport, controller.evidence, log,
                        hooks.identity)  # fmt: skip
    try:
        while True:
            tick(ledger, args.ledger, controller, sites, observed, transport, executor, log, hooks)
            if args.once:
                return 0
            hooks.sleep(controller.interval_s)
    finally:
        executor.wait()
        executor.pool.shutdown(wait=True)
        ledger.close()


def by_hand(args: argparse.Namespace, hooks: Hooks) -> int:
    """`drain`, `ended` or `join`: the ledger's half of an action, as the controller calls it."""
    from expcore.campaign.elastic import Ledger

    ledger = Ledger.open(args.ledger, clock=hooks.clock)
    try:
        if args.command == "drain":
            ledger.drain(args.label)
        elif args.command == "ended":
            ledger.prove_ended(args.label, args.proof)
        else:
            jobs = tuple(job for job in args.jobs.split(",") if job)
            ledger.join(args.label, site=args.site, job_ids=jobs, job_end_at=args.job_end, fingerprint=args.fingerprint,
                        environment=args.environment, kill_wait_s=args.kill_wait)  # fmt: skip
    finally:
        ledger.close()
    hooks.say(f"fleetcontrol: {args.command}: {args.label}: done")
    return 0


def observe(args: argparse.Namespace, environ: Mapping[str, str], hooks: Hooks) -> int:
    import fleetobserve

    if not os.path.isabs(args.fleet):
        raise ConfigError("--fleet must be an absolute path")
    hooks.say(json.dumps(fleetobserve.observe(Path(args.fleet), environ, hooks.runner()), sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(prog="fleetcontrol.py", description=__doc__.split("\n\n")[0])
    sub = found.add_subparsers(dest="command", required=True)
    sub.add_parser("observe").add_argument("--fleet", required=True)
    loop = sub.add_parser("control")
    loop.add_argument("--ledger", type=Path, required=True)
    loop.add_argument("--sites", type=Path, required=True)
    loop.add_argument("--once", action="store_true")
    loop.add_argument("--dry-run", dest="dry_run", action="store_true")
    loop.add_argument("--observed", action="append", help="SITE=FILE: that site's read, as observe printed it")
    loop.add_argument("--log", type=Path)
    for name in ("drain", "ended", "join"):
        verb = sub.add_parser(name)
        verb.add_argument("--ledger", type=Path, required=True)
        verb.add_argument("--label", required=True)
        if name == "ended":
            verb.add_argument("--proof", choices=("cancelled", "terminal"), required=True)
        if name == "join":
            verb.add_argument("--site", required=True)
            verb.add_argument("--jobs", required=True)
            verb.add_argument("--job-end", dest="job_end", type=float, required=True)
            verb.add_argument("--kill-wait", dest="kill_wait", type=float, required=True)
            verb.add_argument("--fingerprint", required=True)
            verb.add_argument("--environment", required=True)
    return found


def default_hooks() -> Hooks:
    from hpcslurm import Runner

    return Hooks(runner=Runner)


def main(argv: Optional[Sequence[str]] = None, environ: Optional[Mapping[str, str]] = None,
         hooks: Optional[Hooks] = None) -> int:  # fmt: skip
    args = parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    hooks = hooks or default_hooks()
    try:
        if args.command == "observe":
            return observe(args, environ, hooks)
        if args.command == "control":
            return control(args, environ, hooks)
        return by_hand(args, hooks)
    except (ConfigError, RuntimeError, OSError, ValueError) as error:
        print(f"error: {args.command}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
