"""fleetcontrol.py: the elastic fleet's provisioning controller (#462 step 5, C43; README.md, "The elastic fleet's
controller"). `expcore.campaign.elastic.control` decides; this reads the sites, and runs or prints what it decided.

    <python> -I -B deploy/hpc-federation/fleetcontrol.py observe --fleet DIR    # on a site's login node, read only
    <python> -I -B deploy/hpc-federation/fleetcontrol.py control --ledger L --sites S.toml [--once] [--dry-run]
                                                                 [--observed SITE=FILE ...] [--log FILE]
    <python> -I -B deploy/hpc-federation/fleetcontrol.py drain --ledger L --label LABEL
    <python> -I -B deploy/hpc-federation/fleetcontrol.py ended --ledger L --label LABEL --proof cancelled|terminal
    <python> -I -B deploy/hpc-federation/fleetcontrol.py join --ledger L --label LABEL --site S --jobs J[,J]
                                                       --job-end EPOCH --kill-wait S --fingerprint F
                                                       --environment-file IDENTITY.json [--platform P]
    <python> -I -B deploy/hpc-federation/fleetcontrol.py settle --ledger L --lease N --spend-micro S
                                                       --source root|pulled --attempts N

- observe: one read of the site (`fleetobserve`), as JSON on stdout; nothing is written, submitted or cancelled.
- control: every `interval_s` (60 by default), each site is read (`observe` through the site's transport, or the JSON
  file `--observed SITE=FILE` names), the ledger's demand is read, `decide` gives each site's target and the actions,
  and the tick is logged as one JSON line (targets and their limits, each up's queue wait, each action with its reason
  and its commands). `--dry-run` reads the ledger read-only, without its lock, prints each action's commands, and runs
  nothing; otherwise the controller holds the ledger (one process drives it) and runs them (`fleetexecute`). `--once`
  is one tick, waiting for what it started.
- drain, ended, join, settle: the ledger's half of an action, by hand, exactly as the controller calls it.

Each tick also prints one status line and logs it: what the ledger wants, its money (`status()`: what is left, and the
dollars locked in unsettled leases), and each relay found gone.

The controller only adds to a fleet that exists: `fleet.py --fleet F --layout L up N` makes it (at least two). With
`archives` in `[controller]` (the absolute directory the dispatcher keeps the archives it pulls in) and a `channel` in
every site (that site's channel root on its shared file system), this process also runs the elastic dispatcher
(`expcore.campaign.elastic.dispatch`) every 30 s on the ledger it holds (the ledger's lock admits one holder), each
site through its local transport; `settings` names the shard settings (the fleet's `plan --manifest` file, with
`home_files` when the lease runs need the operator's key files): each joined federation's worker is launched
(`fleetworker.py launch`) from it, and the identity a federation joins with and a dead store's spend are read with its
study. The sites file (`--sites`, TOML) holds a `[controller]` table (`evidence`, the
absolute directory each retired member's root evidence is collected into; `interval_s`; `log`, the JSON log's absolute
path) and one `[[site]]` table per site:
its `label` (an operator's label, `[a-z0-9-]`, never a site's name), `fleet` (its fleet's absolute directory),
`transport` (`local`: the commands run on this host, the site's login node; or `ssh`: every command runs on the
login node of a remote site over ssh, the ledger staying on this host's local disk), optionally `interpreter` and
`deploy` (the Python and this directory on its login node), and the settings of `SiteSettings`: `max_federations`,
`allowance_units` and `rate_units_per_hour` (required), `kill_wait_s`, `parallel_ups`, `warm_pool`, `retire_idle_s`,
`queue_stall_s`, `lease_bound_s`, `collect_s`, `margin_s`. An `ssh` site also names `host` (an ssh destination or alias
that the operator's own ssh config resolves: never logged), `interpreter` and `deploy` (absolute, on the login node; the
core is importable by that interpreter), `env_file` (its site.env on the login node, required: sourced before each
command, which fails without it, never a run on the login shell's defaults) and `settings` (the shard settings file on
the login node; required when `[controller]` names `settings`).
A site may also name `prepare`, the study's own command for each member that has come up (an argv list: its ingest and
`hpcfed.py verify-data`; `{root}` in a word is the member's federation root and `{layout}` the fleet's layout file), and
then `prepare_timeout_s` (required with it, refused without): the controller runs it once per member on the site's login
node, through the site's transport, from its deploy directory (`fleetprepare.py`), and joins the member only when it
exits 0 and the member then reads verified; any other end retires the member unjoined. Its output stays in a log file
at the site; the controller's log keeps the outcome, the exit status and the duration.
One campaign drives one site: the ledger and `archives` are this host's; each archive is pulled over ssh, hashed here.
"""

import argparse
import json
import os
import sqlite3
import stat
import subprocess
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

import fleetcompose  # noqa: E402
from fleetexecute import (  # noqa: E402
    Executor,
    Log,
    SiteConfig,
    SiteTransport,
    dead_store_reader,
    described,
    drain_refused,
    last_line,
    printed,
    render,
)

COMMANDS = ("observe", "control", "drain", "ended", "join", "settle")
CONTROLLER_KEYS = ("evidence", "interval_s", "log", "archives", "settings", "study")
SITE_KEYS = ("fleet", "transport", "interpreter", "deploy", "channel", "host", "env_file", "settings", "prepare",
             "kind", "ssh_config", *fleetcompose.KEYS)  # fmt: skip
KINDS = ("hpc", fleetcompose.KIND)
SSH_ONLY_KEYS = ("host", "env_file", "settings", "ssh_config")
DISPATCH_S = 30.0  # `elastic.dispatch.TICK_S`: with a dispatcher the loop goes round this often
TRANSPORTS = ("local", "ssh")
OBSERVE_TIMEOUT_S = 600.0


class ConfigError(ValueError):
    """The sites file cannot be used; the message names the key."""


@dataclass(frozen=True)
class Controller:
    evidence: Path
    interval_s: float = 60.0
    log: Optional[Path] = None
    archives: Optional[Path] = None  # where the dispatcher keeps the archives it pulls: set, it runs in this loop
    settings: Optional[Path] = None  # the shard settings (`fleetplan.read_settings`): the study, python and home_files
    study: Optional[Path] = None  # the study here, where no shard settings are named (a Compose site's campaign)


@dataclass
class Hooks:
    """What the controller reads and runs through: this host's Slurm and commands by default, the tests' fakes."""

    runner: Callable[[], Any]  # the login node's Runner, for observe
    transport: Callable[[Mapping[str, str]], Any] = SiteTransport
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    # None: read from the sites file's `settings` (`fleetidentity`), or nothing when it names none
    identity: Optional[Callable[[str, str], Optional[Tuple[str, Mapping[str, str]]]]] = None
    spend: Optional[Callable[[Any, Any], Optional[Tuple[int, str, Optional[int]]]]] = None
    say: Callable[[str], None] = field(default=print)
    # the dispatcher's transport, given each site's directory for its calls (`RoutedTransport` by default: ssh for a
    # site with a host, else here)
    dispatch: Optional[Callable[[Mapping[str, str]], Any]] = None


def _absolute(value: Any, what: str) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ConfigError(f"{what} must be an absolute path")
    return Path(os.path.normpath(value))


def fleet_identity(fleet: Path, host: Optional[str] = None) -> Any:
    """One fleet directory however it is named: its device and inode (a link or a bind mount of it is the same), or its
    resolved path while it does not exist. A remote site's is its host and path: its directory is not on this host."""
    if host is not None:
        return ("ssh", host, os.path.normpath(fleet))
    try:
        info = os.stat(fleet)
    except OSError:
        return os.path.realpath(fleet)
    return (info.st_dev, info.st_ino)


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
    archives = _absolute(table["archives"], "[controller] archives") if "archives" in table else None
    manifest = _absolute(table["settings"], "[controller] settings") if "settings" in table else None
    study = _absolute(table["study"], "[controller] study") if "study" in table else None
    controller = Controller(
        _absolute(table["evidence"], "[controller] evidence"), float(interval), log, archives, manifest, study
    )
    sites = []
    for entry in data.get("site") or []:
        own = {key: entry.pop(key) for key in SITE_KEYS if key in entry}
        prepare = prepare_of(entry.get("label"), own.get("prepare"), "prepare_timeout_s" in entry)
        if own.get("transport", "local") not in TRANSPORTS:
            raise ConfigError(f"site {entry.get('label')}: transport must be one of {', '.join(TRANSPORTS)}")
        remote = own.get("transport", "local") == "ssh"
        compose = compose_of(entry.get("label"), own, remote, archives is not None)
        if compose is not None:
            fleetcompose.settings_defaults(entry, compose)
        if not remote:
            entry.setdefault("login_node", "local")  # every local site's commands run here: one login node, one cgroup
        try:
            settings = SiteSettings(**entry)
        except TypeError as error:
            raise ConfigError(f"site {entry.get('label')}: {error}") from None
        fleet = _absolute(own.get("fleet"), f"site {settings.label}: fleet")
        deploy = _absolute(own["deploy"], f"site {settings.label}: deploy") if "deploy" in own else HERE
        channel = _absolute(own["channel"], f"site {settings.label}: channel") if "channel" in own else None
        python = own.get("interpreter", sys.executable)
        host = env_file = shard_settings = None
        if remote:
            host, env_file, shard_settings = ssh_site(settings.label, own, manifest is not None and compose is None)
            python = str(_absolute(python, f"site {settings.label}: interpreter"))
        elif any(key in own for key in SSH_ONLY_KEYS):
            raise ConfigError(f"site {settings.label}: {', '.join(SSH_ONLY_KEYS)} belong to an ssh site")
        config = private_file(own["ssh_config"], f"site {settings.label}: ssh_config") if "ssh_config" in own else None
        sites.append(SiteConfig(settings, fleet, own.get("transport", "local"), python, deploy, channel, host,
                                env_file, shard_settings, prepare, compose, config))  # fmt: skip
    if not sites or len({s.label for s in sites}) != len(sites):
        raise ConfigError("the sites file needs at least one [[site]], each label once")
    labels = [s.label for s in sites]
    if any(other.startswith(f"{label}-") for label in labels for other in labels):
        # a member's ledger label is `<site>-<member>`: two sites one of which begins with the other and a dash could
        # name one federation twice (a-b's c and a's b-c), and one's read prove the other's end
        raise ConfigError("a site's label may not begin with another site's label and a dash")
    channels = [s.channel is not None for s in sites]
    if (archives is None and any(channels)) or (archives is not None and not all(channels)):
        raise ConfigError("[controller] archives and every site's channel go together: the dispatcher serves each "
                          "site's channel and keeps the archives it pulls")  # fmt: skip
    if len({fleet_identity(s.fleet, s.host) for s in sites}) != len(sites):
        raise ConfigError("two sites name one fleet directory: each fleet is one site's, read and acted on once")
    return controller, sites


def private_file(value: Any, what: str) -> str:
    """An absolute path to a regular file of this user that no group or other can read or write (an ssh config file:
    it names the key and the known hosts), never a link."""
    path = _absolute(value, what)
    try:
        info = os.lstat(path)
    except OSError:
        raise ConfigError(f"{what} is not there") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError(f"{what} must be a regular file of this user of mode 0600, not a link")
    return str(path)


def compose_of(label: Any, own: Mapping[str, Any], remote: bool, dispatched: bool) -> Optional[Any]:
    """A `kind = "compose"` site's own keys (`fleetcompose.compose_site`), or None for an HPC site, which may name none
    of them. A Compose site is an ssh site with a channel (its workers take leases through it)."""
    kind = own.get("kind", "hpc")
    if kind not in KINDS:
        raise ConfigError(f"site {label}: kind must be one of {', '.join(KINDS)}")
    if kind != fleetcompose.KIND:
        if any(key in own for key in fleetcompose.KEYS):
            raise ConfigError(f"site {label}: {', '.join(fleetcompose.KEYS)} belong to a Compose site")
        return None
    if not remote or "channel" not in own or not dispatched:
        raise ConfigError(f"site {label}: a Compose site is an ssh site with a channel, and [controller] archives")
    if "prepare" in own:  # its members read verified once linked, so no prepare would ever be decided: never ignored
        raise ConfigError(f"site {label}: a Compose site takes no prepare yet (its members have no data to verify)")
    try:
        return fleetcompose.compose_site(str(label), own)
    except ValueError as error:
        raise ConfigError(str(error)) from None


def prepare_of(label: Any, argv: Any, timed: bool) -> Optional[Tuple[str, ...]]:
    """A site's `prepare` command, checked: an argv of non-empty words, with `prepare_timeout_s` beside it and never
    without it (a command with no bound, or a bound for no command, is a mistake in the file)."""
    if argv is None:
        if timed:
            raise ConfigError(f"site {label}: prepare_timeout_s belongs with prepare")
        return None
    if not isinstance(argv, list) or not argv or not all(isinstance(word, str) and word for word in argv):
        raise ConfigError(f"site {label}: prepare must be a list of words, the command's argv, none empty")
    if not timed:
        raise ConfigError(f"site {label}: prepare needs prepare_timeout_s, the bound of the command")
    return tuple(argv)


def ssh_site(label: str, own: Mapping[str, Any], needs_settings: bool) -> Tuple[str, Optional[str], Optional[Path]]:
    """An ssh site's own keys, checked: its host, and (absolute, on its login node) its site.env and shard settings. The
    defaults of a local site (this host's interpreter and directory) would be wrong there, so they are named."""
    from expcore.campaign.elastic.transport import ssh_argv

    named = ("host", "interpreter", "deploy", "env_file") + (("settings",) if needs_settings else ())
    missing = [key for key in named if key not in own]
    if missing:
        raise ConfigError(f"site {label}: an ssh site names {', '.join(missing)}")
    try:
        ssh_argv(own["host"])
    except ValueError:
        raise ConfigError(f"site {label}: host is not an ssh destination or alias") from None
    env_file = str(
        _absolute(own["env_file"], f"site {label}: env_file")
    )  # required: never a run on the login shell's defaults
    settings = _absolute(own["settings"], f"site {label}: settings") if "settings" in own else None
    return own["host"], env_file, settings


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
            argv = (fleetcompose.observe_argv(site) if fleetcompose.is_compose(site)
                    else site.script("fleetcontrol.py", "observe", "--fleet", str(site.fleet)))  # fmt: skip
            code, text, err = transport.run(site, argv, OBSERVE_TIMEOUT_S)
            if code != 0:
                return SiteView(label=site.label, error=f"its read failed with status {code}: {last_line(err)}")
        return site_view(site.label, text)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as error:
        return SiteView(label=site.label, error=f"its read could not be taken or parsed ({type(error).__name__})")


def tick(views: Any, ledger_path: Path, controller: Controller, sites: Sequence[SiteConfig],
         observed: Mapping[str, Path], transport: Any, executor: Optional[Executor], log: Log,
         hooks: Hooks) -> Any:  # fmt: skip
    """One tick: read, decide, log, then run (or, with no executor, print)."""
    from expcore.campaign.elastic.control import decide, ledger_view

    by_label = {site.label: site for site in sites}
    busy = executor.busy() if executor is not None else frozenset()  # before the reads: what ends during them is busy
    reads = [(site.settings, observe_site(transport, site, observed.get(site.label))) for site in sites]
    view = ledger_view(views)
    decision = decide(hooks.clock(), view, reads, busy)
    rendered = [render(a, by_label[a.site], controller.evidence, ledger_path, settings=controller.settings)
                for a in decision.actions]  # fmt: skip
    entries = [described(a, r, by_label[a.site]) for a, r in zip(decision.actions, rendered)]  # logged: no host in it
    plain = [described(a, r) for a, r in zip(decision.actions, rendered)]  # printed: exactly what a live tick runs
    money = dict(view.money)
    log.write("tick", dry_run=executor is None, wanted=decision.wanted, targets=decision.targets,
              waits=decision.waits, relays=decision.relays, money=money, actions=entries)  # fmt: skip
    hooks.say(status_line(decision, money))
    if executor is None:
        for entry in plain:
            hooks.say(printed(entry))
    else:
        executor.run(decision)
    return decision


def usd(micro: Optional[int]) -> str:
    return "none" if micro is None else f"{micro / 1_000_000:.6f} USD"


def status_line(decision: Any, money: Mapping[str, Any]) -> str:
    """One tick's status: leases wanted, the money left and locked in unsettled leases, relays gone."""
    locked = ", ".join(f"{state} {usd(amount)}" for state, amount in sorted(money["locked_by_state"].items()))
    gone = sum(len(found) for found in decision.relays.values())
    return (f"fleetcontrol: {decision.wanted} lease(s) wanted; remaining {usd(money['remaining_micro'])}; locked in "
            f"unsettled leases {usd(money['locked_micro'])}" + (f" ({locked})" if locked else "")
            + f"; {len(decision.actions)} action(s); {gone} relay(s) gone")  # fmt: skip


def hooks_read(controller: Controller, sites: Sequence[SiteConfig], hooks: Hooks,
               transport: Any = None) -> Tuple[Any, Any]:  # fmt: skip
    """What a federation joins with and what a lost lease spent: the hooks' own (the tests'), else read from the sites
    file's `settings` (`fleetidentity.identity_reader` and `dead_store_reader` over `fleetidentity.locator`, with the
    settings' study), else nothing is read: a join waits and a lost lease stays charged its grant."""
    identity, spend = hooks.identity, hooks.spend
    named = controller.settings is not None or controller.study is not None
    if named and (identity is None or spend is None):
        import fleetidentity
        import fleetplan

        try:
            study = controller.study or fleetplan.read_settings(controller.settings)["study"]
            from expcore.study.config import load_study

            loaded = load_study(study)
        except (OSError, ValueError, RuntimeError, KeyError, ImportError) as error:
            raise ConfigError(f"the study of the shard settings cannot be loaded ({type(error).__name__})") from None
        identity = identity or fleetidentity.identity_reader(controller.settings, {s.label: s for s in sites},
                                                             transport=transport)  # fmt: skip
        spend = spend or dead_store_reader(fleetidentity.locator(controller.evidence, transport), loaded)
    return identity or (lambda site, member: None), spend or (lambda site, action: None)


def dispatcher_of(ledger: Any, controller: Controller, sites: Sequence[SiteConfig], hooks: Hooks) -> Any:
    """The dispatcher over the ledger this process holds (the ledger's lock admits one holder, so it ticks in this
    loop), for each site's channel root; None when the sites file names no archives directory. A local site is read
    through the local transport (its login node is this host), a remote one over ssh."""
    if controller.archives is None:
        return None
    from expcore.campaign.elastic.dispatch import Dispatcher
    from expcore.campaign.elastic.transport import RoutedTransport, Site, ssh_argv

    named = [Site(s.label, str(s.channel), ssh_argv(s.host, s.ssh_config) if s.remote else (), (s.python, "-I", "-B"),
                  str(s.fleet) if s.remote else None, s.env_file) for s in sites]  # fmt: skip
    cwds = {s.label: str(s.fleet) for s in sites if not s.remote}
    transport = hooks.dispatch(cwds) if hooks.dispatch is not None else RoutedTransport(cwds)
    return Dispatcher(ledger, named, transport, controller.archives)


def dispatch_tick(dispatcher: Any, log: Log) -> None:
    """One dispatcher tick, logged when it did anything or read nothing; a failure of the site or the ledger is logged
    and the loop goes on (the next tick reads again)."""
    from expcore.campaign.elastic import LedgerError

    try:
        done = dispatcher.tick()
    except (OSError, LedgerError, ValueError, sqlite3.Error) as error:
        log.write("dispatch", ok=False, said=f"{type(error).__name__}: {last_line(str(error))}")
        return
    seen = {name: {key: value for key, value in vars(t).items() if value} for name, t in done.items()}
    busy = {
        name: found for name, found in seen.items() if set(found) != {"read"}
    }  # a site read with nothing to do: quiet
    if busy:
        log.write("dispatch", ok=True, sites=busy)


def control(args: argparse.Namespace, environ: Mapping[str, str], hooks: Hooks) -> int:
    from expcore.campaign.elastic import Ledger
    from expcore.campaign.elastic.control import read_only

    controller, sites = load_config(args.sites)
    known = {site.label for site in sites}
    observed: Dict[str, Path] = {}
    if args.observed and not args.dry_run:
        raise ConfigError("--observed takes a read pulled earlier: it is for --dry-run only, never acted on")
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
    identity, spend = hooks_read(controller, sites, hooks, transport)
    ledger = Ledger.open(args.ledger, clock=hooks.clock)
    executor = Executor(ledger, args.ledger, {s.label: s for s in sites}, transport, controller.evidence, log,
                        identity, spend, settings=controller.settings, clock=hooks.clock)  # fmt: skip
    dispatcher = dispatcher_of(ledger, controller, sites, hooks)
    # with a dispatcher the loop goes round every DISPATCH_S, and the controller decides every interval_s of them
    pause = controller.interval_s if dispatcher is None else min(DISPATCH_S, controller.interval_s)
    decide_at = hooks.clock()
    try:
        while True:
            if dispatcher is not None:
                dispatch_tick(dispatcher, log)
            if hooks.clock() >= decide_at:
                tick(ledger, args.ledger, controller, sites, observed, transport, executor, log, hooks)
                decide_at = hooks.clock() + controller.interval_s
            if args.once:
                return 0
            hooks.sleep(pause)
    finally:
        executor.wait()
        executor.pool.shutdown(wait=True)
        ledger.close()


def environment_of(path: Path) -> str:
    """The ledger's environment for a join by hand: the digest of the identity in the file (`environment_digest`)."""
    from expcore.campaign.elastic.control import environment_digest

    try:
        identity = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"--environment-file cannot be read as JSON ({type(error).__name__})") from None
    if not isinstance(identity, dict) or not all(isinstance(v, str) for v in identity.values()):
        raise ConfigError("--environment-file must hold a JSON object of names to text values")
    return environment_digest(identity)


def by_hand(args: argparse.Namespace, hooks: Hooks) -> int:
    """`drain`, `ended`, `join` or `settle`: the ledger's half of an action, as the controller calls it."""
    from expcore.campaign.elastic import Ledger

    ledger = Ledger.open(args.ledger, clock=hooks.clock)
    try:
        if args.command == "drain":
            late = drain_refused(ledger, args.label, hooks.clock)
            if late is not None:
                raise ConfigError(late)
            ledger.drain(args.label)
        elif args.command == "settle":
            ledger.settle(args.lease, args.spend_micro, args.source, attempts_charged=args.attempts)
        elif args.command == "ended":
            ledger.prove_ended(args.label, args.proof)
        else:
            jobs = tuple(job for job in args.jobs.split(",") if job)
            ledger.join(args.label, site=args.site, job_ids=jobs, job_end_at=args.job_end, fingerprint=args.fingerprint,
                        environment=environment_of(args.environment_file), kill_wait_s=args.kill_wait,
                        platform=args.platform)  # fmt: skip
    finally:
        ledger.close()
    hooks.say(f"fleetcontrol: {args.command}: {getattr(args, 'label', None) or args.lease}: done")
    return 0


def observe(args: argparse.Namespace, environ: Mapping[str, str], hooks: Hooks) -> int:
    import fleetobserve

    if not os.path.isabs(args.fleet):
        raise ConfigError("--fleet must be an absolute path")
    read = fleetobserve.observe(Path(args.fleet), environ, hooks.runner(), hooks.clock())
    hooks.say(json.dumps(read, sort_keys=True))
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
    settle = sub.add_parser("settle")
    settle.add_argument("--ledger", type=Path, required=True)
    settle.add_argument("--lease", type=int, required=True)
    settle.add_argument("--spend-micro", dest="spend_micro", type=int, required=True)
    settle.add_argument("--source", choices=("root", "pulled"), required=True)
    settle.add_argument("--attempts", type=int, required=True, help="the charged attempts the dead store shows")
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
            verb.add_argument(
                "--platform",
                default=None,
                help="the SHA-256 of the federation's own platform fingerprint: required when the campaign declares "
                "its platforms (C50), not read otherwise",
            )
            verb.add_argument(
                "--environment-file",
                dest="environment_file",
                type=Path,
                required=True,
                help="the federation's environment identity as a lease store pins it, a JSON object",
            )
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
