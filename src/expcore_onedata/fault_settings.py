# Provenance: new
"""`[faults]` of platform.toml: the fault injector's sanction, journal, Compose settings and services (O10, O12).

`load_faults` reads the table against the loaded configuration and names a mistake by the file, the key and, for a
service entry, its position, never by a provider's or a service's name. The service names live here and nowhere in code.

    [faults]
    sanctioned = true                # the configuration's half of the sanction; false or absent refuses
    journal = "fault-journal"        # directory of the injection journal and the targets' lock files, in the root
    compose_file = "compose.yaml"    # the Compose file that declares the providers' services
    project = "..."                  # the Compose project name, which with a service names the container
    command = ["docker", "compose"]  # optional: the Compose command
    docker = ["docker"]              # optional: the command that inspects a container
    poll_s = 2                       # optional: between two readings of a service
    settle_timeout_s = 180           # optional: the longest a service may take to go down or to come back; see below
    command_timeout_s = 120          # optional: the longest one command may take
    ready_timeout_s = 180            # optional: the longest a restored provider may take to serve; see below
    clear_attempts = 3               # optional: how many times a restore is tried
    admission_timeout_s = 3600       # optional: the longest a run waits for the federation's lock
    health_wait_s = 30               # optional: the longest the health check waits for it; below health_timeout_s
    gate_timeout_s = 240             # optional: the longest a restored provider's peers may take to reach it; see below
    heal = "off"                     # optional: "off", "all_links", "link_then_container" or "link"; see below
    heal_timeout_s = 4200            # optional, provisional: the longest the heal of a stuck link may take in all
                                     # (left out, it is derived from the providers and the mode; see below)
    heal_settle_s = 180              # optional, provisional: the no-fetch wait after a heal's restarts
    session_drain_s = 320            # optional: how long an outage run's admission outlives its sessions; see below
    heal_container_fallback = true   # optional: false, and "all_links" restarts links only, never a container

    [faults.services]
    <provider name> = "<service>"    # each provider that may be taken down, by its name; no two share a service

`journal` names the federation: every platform file of one federation, the root's own and any derived from it, names the
same directory, which is how they share its lock. It lies directly in the federation's root, as `up` makes it
(`<root>/fault-journal`), so the root is its parent (`linkrecord.federation_root`), and the link-check record, which
describes the federation's links and not a file's, is written there whichever file a check ran through (O35,
`expcore_onedata.linkrecord`). A table whose journal's parent holds no `platform.toml` is refused, naming the key,
rather than guess where the record goes: a platform file derived in another directory states the root's journal, by
an absolute path or one relative to itself. A platform file that lies in a study directory, one that holds
`study.toml`, as a copy of the federation's kept with the study does, states it by an absolute path only: a relative one
resolves into the study, whose parent holds that very copy, so the check above would pass vacuously and the lock and
the record would land in the study; such a table is refused, naming the key and the absolute form to write.

`settle_timeout_s` must exceed the providers' `stop_grace_period` (the Compose file's, 120 s in the federation of
deploy/compose-federation) by a margin. A stop that hits `command_timeout_s` kills the CLI, not the Docker daemon's
stop, which runs on for up to the grace period; the restore then waits, for at most `settle_timeout_s`, for the service
to show not running, and a service not seen down in that time is an unverified restore (`faults.py`, "A stop of unknown
outcome"). The grace period is in the Compose file, which platform.toml does not read, so nothing here can refuse a
value below it: whoever raises the grace, or lowers `settle_timeout_s`, keeps this bound. The default is the grace
period plus `SETTLE_MARGIN_S`.

`ready_timeout_s` is the longest a restored provider may take to serve before its restore counts as unverified. A
restore whose identity changed is not yet a provider that serves: in the federation of deploy/compose-federation the
restarted container's REST endpoint refused connections right after its restart and its health check passed 48 to 52 s
after its start, so the default, `DEFAULT_READY_TIMEOUT_S`, is 180 s. The wait is counted from the verified identity
change and ends when the provider answers an authenticated REST read and its container, if it has a health check, is
healthy (`expcore_onedata.fault_serves`, `expcore_onedata.compose`). A provider that does not serve within it leaves the
injection open and the federation's exclusive admission held, as a failed restore does.

`gate_timeout_s` bounds the session gate a restore passes once its provider serves and before its links are checked
(`expcore_onedata.fault_sessions`): Onezone shows every provider of the probe space online, and a probe file made before
the injection, its block map read through each peer, has no error entry for the restored provider. Its default, 240 s,
is the 180 s cap of a peer's session renewal backoff (op-worker f118ed9) and a margin.

`session_drain_s` is how long the federation admits no run after an outage run's mount disconnected and its REST
gate stopped, whichever is later (`expcore_onedata.fault_drain`). During an outage an agent's
read of a file only the stopped provider holds leaves an on-demand sync job on the local provider that nothing calls
back, and a later read of the same file and range, from any session, joins it and hangs. The job is cancelled when the
run's callers end: a REST read's seconds after its client closes the connection, a mount's about 300 s after oneclient's
last connection closes (op-worker's `fuse_session_grace_period_seconds`), plus about 15 s when the removal retries
(`DEFAULT_SESSION_DRAIN_S`, 320 s; source-read of op-worker f118ed9, not measured). Eviction, a transfer cancel,
deleting the file and a link restart do not cancel it. The wait is the remainder after whatever the restore already
took, so it overlaps the heal's settle; times that are missing mean the whole drain from the restore's start, never
zero. It applies only after an outage run: every other run is let go at once. `0` declares no drain, which only a
federation whose agents never read a file a stopped provider alone holds may choose.

`heal` says what the restore does when a link is stuck after it (`links_stuck`, O35; `expcore_onedata.fault_heal`):
`"off"` (the default) fails the restore closed, as before. `"all_links"`, the primary mode, restarts the rtransfer link
of every provider in quick succession, settles with no fetch, checks every pair, and falls back to restarting a stuck
pair's reader's container. `"link_then_container"` and `"link"` are the per-reader design, kept: each stuck pair's
reader's link restarted in turn, then (in the first) its container. A container restart is itself an outage of that
provider, so declare a mode that can make one knowingly. The fault gate's health check heals the stuck pairs its own
link check finds with the same mode and bounds, between runs (`fault_gate_heal`). `heal_settle_s` is the enforced settle
after every link or container restart: no probe, no gate read, no check and no run until it has passed, since a fetch
through a provider soon after its link restarts can leave the pair stale (seen live 23 s after a restart; a clean
3-minute settle left every pair healthy). `heal_timeout_s` bounds the heal: no step starts whose declared bounds would
carry the heal past it, so a declared heal must leave room for one round of its steps, or it is refused at load, naming
the key, the least it may be and the value declared: for `"all_links"`, `heal_all_links_reserve` (per provider of
`[faults.services]`, a ping and a link restart: three commands at `command_timeout_s`, the RPC's bound and its margin;
and one settle) and one container restart (`heal_container_reserve`: four commands and a margin of three,
`settle_timeout_s` twice, `ready_timeout_s` and one settle); for `"link_then_container"`, `heal_link_reserve` (one
provider's ping and link restart, and one settle) and one container restart; for `"link"`, `heal_link_reserve`. With the
other defaults, four providers in `"all_links"` need 3,780 s and five need 4,290 s.

**An undeclared `heal_timeout_s` is derived, never fixed.** A fixed default would be refused by the loader's own check
for some common size (it was: 4,200 s against the 4,290 s five providers need), so a declared heal that leaves the key
out gets `default_heal_timeout_s`: `heal_round_s`, the very least the load check holds a declared value to, counted
from the declared providers, mode, `heal_container_fallback`, `command_timeout_s`, `settle_timeout_s`,
`ready_timeout_s` and `heal_settle_s`, plus `HEAL_TIMEOUT_HEADROOM_PCT` (10 %), rounded up to a whole minute
(`HEAL_TIMEOUT_STEP_S`). With the other defaults: `"all_links"` takes 4,200 s for four providers and 4,740 s for five;
without the container fallback 2,460 s and 3,060 s; `"link_then_container"` 2,520 s and `"link"` 780 s. With the heal
off there is no round and no bound, so `heal_timeout_s` is 0 and nothing reads it. A declared value is never derived or
raised: it is held to the least and refused below it. `heal_container_fallback = false` keeps `"all_links"` to link
restarts: a pair they do not mend fails the restore closed, for an operator to decide, and no container is restarted;
its least `heal_timeout_s` then holds no container restart (2,220 s for four providers, 2,730 s for five, with the
other defaults). `"link_then_container"` with it false is refused. **Both durations are provisional,** to be tuned as
live heals are measured.

**The restore must fit the run's token margin.** The restore runs inside the outage run's attempt, at the injector's
`running` exit, which the executor's pattern places inside the tool server's launch (`faults.py`, "Use"): after the
restore come the collection, `finish_instruments` and the tool server's stop, whose re-verification of the tool plane
examines the run's token and fails the run as `tool_plane_integrity` once it has expired (`toolserver._recheck`). A
run's token outlives its whole-time ceiling by `[tokens] margin_s` only, counted from its mint. The restore always runs
(its container start and its session gate use no run token), and a token's lifetime is the study's choice, declared
and never implied, so a platform.toml with `[faults]` is refused at load, heal declared or not, unless it declares
`[tokens] margin_s` and that is at least `restore_token_margin_s`: a missing margin and a short one are refused alike,
naming the least, its derivation term by term (`restore_margin_derivation`) and the line to write, and no default is
derived. The least is the restore's bound (`restore_bound_s`: a stop of unknown outcome waited out,
`settle_timeout_s`; `clear_attempts` starts, each with its command and its wait for the new identity,
`command_timeout_s` and `settle_timeout_s`, and a pause between two; the wait to serve, `ready_timeout_s`; each of
those waits one pause, `poll_s`, past its bound, which it checks only after a reading; and the session gate,
`gate_timeout_s`), 1,514 s with the defaults, plus `TOKEN_SLACK_S`, 300 s, for what that bound does not hold (the
readings' own time, the injector's start after the mint, the link check after the gate, the steps before the tool
server's check), plus the session drain a clear may wait (`restore_drain_s`), rounded up to a whole second: 2,134 s
with the defaults, above the 900 s a platform without `[faults]` still gets when it declares nothing. The drain is
counted because the injector can always hold an exclusive admission of its own, whatever its run holds: a clear that
finds none held takes one (`FaultInjector._restoring`), and that wait passes a drain record still standing
(`federation.drain`), at most `session_drain_s` after the release that wrote it, before it restores; and an injector
that holds an admission of its own, taken at its start or for the clear, waits out its own drain before it lets go
(`FaultInjector._drain`). That drain runs from the restore's start, so only what outlasts the restore's bound is added
(nothing with the defaults). `session_drain_s = 0` adds nothing: a clear then takes no admission and waits for no
drain. Not counted: a start's recovery of an injection an earlier run left, which the health gate settles before a run
is dispatched; a start's waits for the federation (its admission, the drain record and the drain a recovery owes,
`FaultInjector._wait_owed`), which a run's admission has passed before its attempt starts (`FederationLock.held`), and
which an injector that takes an admission of its own waits before any token of its run exists, since a step that uses
a token (the tool server's checks, the sandbox's mint and start) is refused without the run's admission
(`admission.begin_token_step`); in the clear's own admission, the wait for the lock itself and a drain record another
holder raises during it, bounded by `admission_timeout_s` and brief while an injection is open (a shared admission
refuses an unsettled journal, and another outage run's start fails on a target a live injector holds); and the session
drain of the run's own admission, which it publishes after the post-run check. A second restore never falls inside the
window: a failed one is tried again only once no step of the run that uses its tokens runs, and is otherwise left open
for the next start's recovery, the run set aside (`FaultInjector._retry_now`), so the derivation's one restore is
exact.

**A heal is skipped while a step of the run that uses its tokens runs.** A heal may take `heal_timeout_s` more, so at
its start the injector reads the steps of the run that use its tokens (the tool server's launch and the sandbox's,
counted on the run's admission) and skips the heal while one still runs (`faults.py`, "The token's window"), unless the
study declares the long margin: `margin_s` of at least `heal_token_margin_s`, the restore's margin above, two heals of
`heal_timeout_s` (the restore's and the start's recovery's, as the injector's `cancel_wait_s` counts them) and one
session drain of `session_drain_s`, which that start waits out after it restored a provider an earlier run left down
(`FaultInjector._wait_owed`). It counts the effective `heal_timeout_s`, declared or derived, so it follows the derived
bound: with the other defaults, four providers in `"all_links"` (4,200 s) need 10,854 s, and five (4,740 s) 11,934 s. A
heal is never refused at load for the margin.

**A declared heal needs a campaign `max_defer_s` that outlives it.** While the fault gate heals, every check answers the
uncharged deferral, for up to `heal_timeout_s` plus `heal_settle_s` from the heal's start (`heal_wait_s`), and after an
outage run the session drain comes first, with no check between them that passes. The heal begins at a check, so one
wait between two checks (`recheck_s`, 30 s, and up to `poll_interval_s`, 1 s) and that check (up to `health_timeout_s`,
60 s) sit between the drain and the heal, and after the heal, whose last deferred answer can come just before it ends,
another such wait and the check that makes the link check afresh (a call that meets it waits for it). The core turns a
deferral past the campaign's `max_defer_s` (3,600 s by default) into a blocking hold, on which a campaign that exits
when blocked stops. So the check declares the sum of its own times (`heal_deferral_s`: `session_drain_s`,
`heal_timeout_s` and `heal_settle_s`; none with the heal off) as its longest deferral (`FaultGate.max_deferral_s`,
`PlatformHealth.max_deferral_s`), with how many of the core's waits it includes, and the core, which alone knows its own
settings, adds them: the campaign's `max_defer_s` must be at least that total, or it is refused at its start. With the
defaults (`recheck_s` 30 s, `poll_interval_s` 1 s, `health_timeout_s` 60 s) that is 4,700 s and 182 s, 4,882 s, for four
providers, and 4,790 s and 182 s, 4,972 s, for five at the 4,290 s they need (5,240 s and 182 s, 5,422 s, at the 4,740 s
derived when `heal_timeout_s` is left out); with `session_drain_s = 0` the first deferral is the check that starts the
heal, and only the waits after the heal are added (the 4,470 s of five providers and 91 s, 4,561 s). A
`health_timeout_s` of 150 s, as the link check needs, makes them 5,062 s, 5,152 s and 4,651 s.

The figure assumes the heal ends within its own bound (a heal that overruns it closes the gate, and the settle's hold it
leaves can defer a check later still), and every check ends within `health_timeout_s`, as a check must (the link check
too: keep it above `visible_timeout_s + timeout_s + evict_timeout_s`): the core gives up on one that outlives it, but
its thread runs on and can still start the heal later than the figure counts. A deferral that began before the drain
(an outage run still running, and another worker's check deferred on it) or for another reason (a run on an ingest
baseline, or a run's reset holding the reset lock), a heal that waits for another holder of the federation
(the hold `federation_busy`), a heal cut short by its process's end, which the next process settles and heals once
more, and a heal that fails with a container restart left open, which the next check recovers, owing a session drain
and holding a settle when none is journalled, before the gate closes, count from the deferral's first answer, so its
age holds that wait too, which no setting here bounds.
"""

import hashlib
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError, Section, load_toml
from expcore.config.platform_loads import record_platform_load
from expcore_onedata.compose import RESTART_LINK_MARGIN_S, RPC_TIMEOUT_S, CommandRunner, ComposeDriver, run_command
from expcore_onedata.config import PLATFORM_MODULE, PlatformConfig
from expcore_onedata.fault_hpc import HPC_EXEC_MARGIN_S, HPC_POLL_S, HPC_STOP_BOUND_S, HpcDriver
from expcore_onedata.fault_relay import RelayClient
from expcore_onedata.linkrecord import ROOT_FILE, federation_root

# What marks a study directory: `expcore.study.config.STUDY_FILE`, which this layer does not import (a test pins both).
STUDY_FILE = "study.toml"
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# A Compose project name as Compose accepts it. Required, because Compose would otherwise take the name from the
# environment, the Compose file or its directory, and a target must name the one container its outage stops.
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
STOP_GRACE_S = 120.0  # the federation's `stop_grace_period` for a provider (deploy/compose-federation/fedcompose.py)
SETTLE_MARGIN_S = 60.0  # what the default settle timeout adds to it, for the daemon's kill and the readings' lag
DEFAULT_SETTLE_TIMEOUT_S = STOP_GRACE_S + SETTLE_MARGIN_S
DEFAULT_READY_TIMEOUT_S = 180.0  # the federation's restored providers served 48 to 52 s after their restart (measured)
DEFAULT_GATE_TIMEOUT_S = 240.0  # a peer session's renewal backs off to 180 s (inference from source), plus a margin
HEAL_TIMEOUT_HEADROOM_PCT = 10  # what an undeclared heal_timeout_s adds to the least a round needs (provisional)
HEAL_TIMEOUT_STEP_S = 60.0  # and it is rounded up to a whole multiple of this: a minute
DEFAULT_HEAL_SETTLE_S = 180.0  # provisional, until the other study's measurement tunes it
DEFAULT_SESSION_DRAIN_S = 320.0  # op-worker's 300 s mount-session grace, 15 s for a removal that retries, a margin
TOKEN_SLACK_S = 300.0  # what the token margin keeps beyond the restore's bound: the start, the link check, the teardown
HEAL_MODES = ("off", "all_links", "link_then_container", "link")
BACKENDS = ("compose", "hpc")
DEFAULT_RELAY_WAIT_S = 30.0  # what a request through the fault relay may wait beyond its command's own bound
_COMPOSE_KEYS = ("compose_file", "project", "command", "docker")  # an HPC backend reads none of them
_HPC_KEYS = ("stop_bound_s", "relay_wait_s")  # a Compose backend reads neither
_KEYS = (
    "sanctioned",
    "journal",
    "compose_file",
    "project",
    "command",
    "docker",
    "poll_s",
    "settle_timeout_s",
    "command_timeout_s",
    "ready_timeout_s",
    "clear_attempts",
    "admission_timeout_s",
    "health_wait_s",
    "gate_timeout_s",
    "heal",
    "heal_timeout_s",
    "heal_settle_s",
    "session_drain_s",
    "heal_container_fallback",
    "services",
    "backend",
    *_HPC_KEYS,
)


@dataclass(frozen=True)
class FaultSettings:
    """`[faults]` of platform.toml, validated: the sanction, the journal, the Compose settings and the services."""

    file: Path
    sanctioned: bool
    journal: Path
    compose_file: Path | None  # None for the HPC backend, which has no Compose file
    services: Mapping[str, str]
    project: str = ""
    command: tuple[str, ...] = ("docker", "compose")
    docker: tuple[str, ...] = ("docker",)
    poll_s: float = 2.0
    settle_timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S
    command_timeout_s: float = 120.0
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S
    clear_attempts: int = 3
    admission_timeout_s: float = 3600.0
    health_wait_s: float = 30.0
    gate_timeout_s: float = DEFAULT_GATE_TIMEOUT_S
    heal: str = "off"
    heal_timeout_s: float = 0.0  # `load_faults` derives it (`default_heal_timeout_s`) when the table leaves it out
    heal_settle_s: float = DEFAULT_HEAL_SETTLE_S
    session_drain_s: float = DEFAULT_SESSION_DRAIN_S
    heal_container_fallback: bool = True
    config_sha256: str = ""  # the digest of the platform.toml it was read from: part of the federation's identity
    backend: str = "compose"  # "hpc": the providers are Apptainer services on batch nodes (`fault_hpc`)
    stop_bound_s: float = 0.0  # the HPC backend's: the longest a service's stop takes (0 for Compose: not read)
    relay_wait_s: float = 0.0  # the HPC backend's: a relayed request's wait beyond its command's bound (`fault_relay`)


def _number(table: Section, key: str, default: float, minimum: float, *, integer: bool = False) -> float:
    """Read an optional finite number at or above a minimum."""
    value = table.data.get(key, default)
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value < minimum:
        raise table.fail(key, f"must be {'an integer' if integer else 'a number'} of at least {minimum:g}")
    return value


def load_faults(path: str | os.PathLike[str], config: PlatformConfig) -> FaultSettings:
    """Read `[faults]` of a platform.toml against its loaded configuration; raise ConfigError naming the file and key.

    A message names a service entry by its position, never by the provider's or the service's name. While a study's
    build is open it records that it ran, with the platform's module and the file (`platform_loads`, C11, O35).
    """
    file = Path(path).resolve()
    record_platform_load(PLATFORM_MODULE, file)  # at the entry: a load that then refuses its file still ran
    table = Section(load_toml(file), file, "platform.toml").section("faults")
    table.reject_unknown(_KEYS)
    sanctioned = table.data.get("sanctioned", False)
    if not isinstance(sanctioned, bool):
        raise table.fail("sanctioned", "must be true or false")
    services = Section(table.table("services"), file, "[faults.services]")
    if not services.data:
        raise services.fail(None, "must name the service of at least one provider")
    backend = table.data.get("backend", "compose")
    if backend not in BACKENDS:
        raise table.fail("backend", "must be one of " + ", ".join(f'"{name}"' for name in BACKENDS))
    hpc = backend == "hpc"
    for key in _COMPOSE_KEYS if hpc else _HPC_KEYS:
        if key in table.data:
            other = "compose" if hpc else "hpc"
            raise table.fail(key, f'is read only with backend = "{other}", and this table declares "{backend}"')
    project = "" if hpc else table.string("project")
    if not hpc and not PROJECT.fullmatch(project):
        raise table.fail("project", "must be a Compose project name: lower-case letters, digits, '_' and '-'")
    named: dict[str, int] = {}  # each service by the position of the entry that names it
    for index, (name, service) in enumerate(services.data.items()):
        if name not in config.providers:
            raise services.fail(None, f"entry {index} is not a provider declared in [providers]")
        if not isinstance(service, str) or not SERVICE.fullmatch(service):
            raise services.fail(None, f"entry {index} must be a service name: letters, digits, '_', '.' and '-'")
        if service in named:  # one container is one target: the probe maps a service back to exactly one provider
            raise services.fail(None, f"entry {index} names the same service as entry {named[service]}")
        named[service] = index
    heal = table.data.get("heal", "off")
    if heal not in HEAL_MODES:
        raise table.fail("heal", "must be one of " + ", ".join(f'"{mode}"' for mode in HEAL_MODES))
    fallback = table.data.get("heal_container_fallback", True)
    if not isinstance(fallback, bool):
        raise table.fail("heal_container_fallback", "must be true or false")
    words = {}
    for key, default in (("command", ("docker", "compose")), ("docker", ("docker",))):
        words[key] = tuple(table.strings(key)) or default
    declared_journal = table.string("journal")
    if not Path(declared_journal).is_absolute() and (file.parent / STUDY_FILE).is_file():
        raise table.fail(
            "journal",
            f"is relative, and this platform file lies in a study directory (it is beside {STUDY_FILE}): the journal "
            f"would resolve into the study, and the federation's lock and link-check record would land there instead "
            f"of in the federation's root. State the federation's journal in full: "
            f'journal = "<absolute path of the federation\'s root>/fault-journal" (`up` makes it there)',
        )
    journal = (file.parent / declared_journal).resolve()
    if not (federation_root(journal) / ROOT_FILE).is_file():  # the link-check record's place is the root (`linkrecord`)
        raise table.fail(
            "journal",
            f"must lie directly in the federation's root, the directory that holds its {ROOT_FILE} (`up` makes it as "
            f"<root>/fault-journal), where the link-check record is written; its parent holds no {ROOT_FILE}",
        )
    stop_bound_s = _number(table, "stop_bound_s", HPC_STOP_BOUND_S, 1) if hpc else 0.0
    settings = FaultSettings(
        file=file,
        sanctioned=sanctioned,
        journal=journal,
        compose_file=None if hpc else (file.parent / table.string("compose_file")).resolve(),
        services=dict(services.data),
        project=project,
        command=words["command"],
        docker=words["docker"],
        poll_s=_number(table, "poll_s", HPC_POLL_S if hpc else 2.0, 0.001),
        settle_timeout_s=_number(
            table, "settle_timeout_s", stop_bound_s + SETTLE_MARGIN_S if hpc else DEFAULT_SETTLE_TIMEOUT_S, 0
        ),
        command_timeout_s=_number(table, "command_timeout_s", 120.0, 1),
        ready_timeout_s=_number(table, "ready_timeout_s", DEFAULT_READY_TIMEOUT_S, 0),
        clear_attempts=_number(table, "clear_attempts", 3, 1, integer=True),
        admission_timeout_s=_number(table, "admission_timeout_s", 3600.0, 0),
        health_wait_s=_number(table, "health_wait_s", 30.0, 0),
        gate_timeout_s=_number(table, "gate_timeout_s", DEFAULT_GATE_TIMEOUT_S, 0),
        heal=heal,
        heal_timeout_s=_number(table, "heal_timeout_s", 0.0, 0),  # derived below when undeclared
        heal_settle_s=_number(table, "heal_settle_s", DEFAULT_HEAL_SETTLE_S, 0),
        session_drain_s=_number(table, "session_drain_s", DEFAULT_SESSION_DRAIN_S, 0),
        heal_container_fallback=fallback,
        config_sha256=config.sha256,
        backend=backend,
        stop_bound_s=stop_bound_s,
        relay_wait_s=_number(table, "relay_wait_s", DEFAULT_RELAY_WAIT_S, 0) if hpc else 0.0,
    )
    if hpc and settings.settle_timeout_s <= stop_bound_s:  # a stop's waits must outlast the stop itself
        raise table.fail(
            "settle_timeout_s",
            f"must exceed stop_bound_s, {stop_bound_s:g} s, the longest a service's stop takes on an HPC federation "
            f"(by default the layout's stop_seconds 600, supervise.py's kill 30 s and the step's end 60 s), not "
            f"{settings.settle_timeout_s:g}: a stop waited for less is never seen to end; leave it out for "
            f"{stop_bound_s + SETTLE_MARGIN_S:g}",
        )
    declared = "heal_timeout_s" in table.data
    if not declared:  # never a fixed number: one the loader's own check would refuse for some size is wrong
        settings = replace(settings, heal_timeout_s=default_heal_timeout_s(settings))
    if heal == "link_then_container" and not fallback:  # a mode of container restarts, declared to make none
        raise table.fail("heal_container_fallback", 'is false, which "link_then_container" contradicts: use "link"')
    uncovered = sorted(heal_participants(config) - set(settings.services))
    if heal == "all_links" and uncovered:  # a partial sequence is what leaves pairs stale
        shown = config.provider_labels(uncovered)
        raise table.fail("heal", f'"all_links" restarts every provider\'s link, and no service is declared for {shown}')
    least = heal_round_s(settings)
    if settings.heal_timeout_s < least:  # only a declared value can be: the derived one is above the least
        raise table.fail(
            "heal_timeout_s",
            f'must be at least {least:g} for heal = "{heal}", not {settings.heal_timeout_s:g}: one round of its '
            f"actions at their declared bounds; leave it out for the default, {default_heal_timeout_s(settings):g}",
        )
    _require_token_margin(table, load_toml(file), settings)
    return settings


def _require_token_margin(table: Section, document: Mapping[str, Any], settings: FaultSettings) -> None:
    """Refuse a platform with `[faults]` whose `[tokens] margin_s` is not declared, or is below the least the restore,
    its gate and the session drain need (`restore_token_margin_s`); the refusal names the least, its derivation and the
    line to write. A token's lifetime is the study's choice: none is derived for it (see the module docstring)."""
    tokens = document.get("tokens")
    declared = tokens.get("margin_s") if isinstance(tokens, Mapping) else None
    needed = restore_token_margin_s(settings)
    least = math.ceil(needed)  # margin_s is a whole number of seconds: an integer, written out in full
    what = "the restore, its gate and the session drain" if settings.session_drain_s > 0 else "the restore and its gate"
    why = f"the least is {least} s, {what} before the tool server's stop examines the run's token: "
    how = f"{restore_margin_derivation(settings)}, so"
    if declared is None:
        raise table.fail(
            None,
            f"[tokens] margin_s is not declared, and a platform with [faults] must declare it: {why}{how} add: "
            f"[tokens] margin_s = {least}",
        )
    if declared < needed:
        raise table.fail(
            None, f"[tokens] margin_s = {_seconds(declared)} is too small: {why}{how} set: [tokens] margin_s = {least}"
        )


def _seconds(value: float) -> str:
    """A number of seconds written in full, never in exponent form or rounded: a whole number as an integer, any other
    as Python writes it exactly."""
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def command_bound_s(settings: FaultSettings) -> float:
    """The longest one driver command may take: `command_timeout_s`, and for the HPC backend the relay's wait too
    (`relay_wait_s`: a request's wait beyond its command's bound, `fault_relay`)."""
    if settings.backend == "hpc":
        return settings.command_timeout_s + settings.relay_wait_s
    return settings.command_timeout_s  # Compose: exactly as before


def stop_command_s(settings: FaultSettings) -> float:
    """The longest a stop command may take: a command's on Compose; for the HPC backend its own bound,
    `settle_timeout_s`, which outlasts a service's stop (`fault_hpc.HpcDriver`), and the relay's wait."""
    if settings.backend == "hpc":
        return settings.settle_timeout_s + settings.relay_wait_s
    return command_bound_s(settings)


def link_command_s(settings: FaultSettings) -> float:
    """The longest a link restart's command may take: the RPC's bound and its margin, and for the HPC backend the
    exec's step (`fault_hpc.HPC_EXEC_MARGIN_S`) and the relay's wait."""
    if settings.backend == "hpc":
        return RPC_TIMEOUT_S + RESTART_LINK_MARGIN_S + HPC_EXEC_MARGIN_S + settings.relay_wait_s
    return RPC_TIMEOUT_S + RESTART_LINK_MARGIN_S


def _command_words(settings: FaultSettings) -> str:
    """`command_timeout_s` in words, with the relay's wait for the HPC backend."""
    n = _seconds
    words = f"command_timeout_s {n(settings.command_timeout_s)} s"
    return f"{words} + relay_wait_s {n(settings.relay_wait_s)} s" if settings.backend == "hpc" else words


def _bound_terms(settings: FaultSettings) -> tuple[tuple[str, float], ...]:
    """The terms of `restore_bound_s`, each with its declared values in words."""
    s, attempts, n = settings, settings.clear_attempts, _seconds
    identity_wait = s.settle_timeout_s + s.poll_s
    return (
        (f"a stop of unknown outcome waited out (settle_timeout_s {n(s.settle_timeout_s)} s + poll_s {n(s.poll_s)} s)",
         identity_wait),
        (f"{attempts} starts each with its wait ({attempts} x ({_command_words(s)} + "
         f"settle_timeout_s {n(s.settle_timeout_s)} s + poll_s {n(s.poll_s)} s))",
         attempts * (command_bound_s(s) + identity_wait)),
        (f"{attempts - 1} pause(s) between them ({attempts - 1} x poll_s {n(s.poll_s)} s)", (attempts - 1) * s.poll_s),
        (f"the wait to serve (ready_timeout_s {n(s.ready_timeout_s)} s + poll_s {n(s.poll_s)} s)",
         s.ready_timeout_s + s.poll_s),
        (f"the session gate (gate_timeout_s {n(s.gate_timeout_s)} s)", s.gate_timeout_s),
    )  # fmt: skip


def _drain_terms(settings: FaultSettings, bound: float) -> tuple[tuple[str, float], ...]:
    """The terms of `restore_drain_s`, each with its declared values in words; none with `session_drain_s` 0. `bound`
    is the restore's (`restore_bound_s`)."""
    drain, n = settings.session_drain_s, _seconds
    if drain <= 0:
        return ()
    terms = [
        (f"a drain record still standing, which a clear that takes the federation itself waits out before it "
         f"restores (session_drain_s {n(drain)} s)", drain),
    ]  # fmt: skip
    if drain > bound:  # its own drain runs from the restore's start: only what outlasts the restore's bound is added
        past = f"its own drain past the restore's bound (session_drain_s {n(drain)} s - {n(bound)} s)"
        terms.append((past, drain - bound))
    return tuple(terms)


def _start_terms(settings: FaultSettings) -> tuple[tuple[str, float], ...]:
    """The HPC backend's term of `restore_token_margin_s` for the injector's start after the run's mint: its stop, which
    may run to its own bound (`stop_command_s`), past what the slack holds; none on Compose, whose stop the slack
    holds."""
    if settings.backend != "hpc":
        return ()
    n = _seconds
    words = (
        f"the injector's start's stop (settle_timeout_s {n(settings.settle_timeout_s)} s + relay_wait_s "
        f"{n(settings.relay_wait_s)} s)"
    )
    return ((words, stop_command_s(settings)),)


def _restore_terms(settings: FaultSettings) -> tuple[tuple[str, float], ...]:
    """The terms of `restore_token_margin_s`, each with its declared values in words: the restore's bound, the slack,
    the session drain and, for the HPC backend, the start's stop."""
    bound = _bound_terms(settings)
    drain = _drain_terms(settings, sum(value for _, value in bound))
    return (*bound, (f"slack {_seconds(TOKEN_SLACK_S)} s", TOKEN_SLACK_S), *drain, *_start_terms(settings))


def restore_margin_derivation(settings: FaultSettings) -> str:
    """`restore_token_margin_s` term by term, in words and then as a sum, every number in full: what a refusal at load
    spells out."""
    terms = _restore_terms(settings)
    words = " + ".join(word for word, _ in terms)
    total = sum(value for _, value in terms)
    return f"{words}: {' + '.join(_seconds(value) for _, value in terms)} = {_seconds(total)}"


def restore_bound_s(settings: FaultSettings) -> float:
    """The longest the run's own restore may take without a heal, by its declared bounds and its pauses: a stop of
    unknown outcome waited out (`settle_timeout_s`), `clear_attempts` starts, each with its command and its wait for
    the new identity (`command_timeout_s` and `settle_timeout_s`) and a pause between two (`poll_s`), the wait to
    serve (`ready_timeout_s`), each of those waits one pause (`poll_s`) past its bound, since it checks the bound only
    after a reading, and the session gate (`gate_timeout_s`, whose pauses end at its bound). 1,514 s with the
    defaults. A reading's own time and the link check after the gate are the slack's (`TOKEN_SLACK_S`)."""
    return sum(value for _, value in _bound_terms(settings))


def restore_drain_s(settings: FaultSettings) -> float:
    """What the session drain adds to the restore's bound when the injector holds an exclusive admission of its own,
    which it can always do (see the module docstring): a clear that holds no exclusive admission takes one
    (`FaultInjector._restoring`), and that wait passes a drain record still standing (`federation.drain`), at most
    `session_drain_s` after the release that wrote it, before anything is restored; and an injector that holds an
    admission of its own, taken at its start or for the clear, waits out its own drain before it lets go
    (`FaultInjector._drain`), which runs from the restore's start, so only what outlasts the restore's bound is added.
    320 s with the defaults; 0 with `session_drain_s` 0, which takes no admission for a clear and waits for none."""
    return sum(value for _, value in _drain_terms(settings, restore_bound_s(settings)))


def restore_token_margin_s(settings: FaultSettings) -> float:
    """The least `[tokens] margin_s` of a platform with `[faults]`, which must declare it and is refused at load below
    it: the restore's bound, `TOKEN_SLACK_S` and the session drain a clear may wait (`restore_drain_s`; see the module
    docstring), and for the HPC backend the start's stop (`_start_terms`). 2,134 s with the defaults on Compose."""
    started = sum(value for _, value in _start_terms(settings))
    return restore_bound_s(settings) + TOKEN_SLACK_S + restore_drain_s(settings) + started


def heal_token_margin_s(settings: FaultSettings) -> float:
    """The long token margin: the least `[tokens] margin_s` with which the injector heals while a step of the run that
    uses its tokens still runs (see the module docstring): the restore's margin, two heals of `heal_timeout_s` and one
    session drain of `session_drain_s`, 10,854 s with the defaults; 0 with the heal off, which asks nothing of the
    margin."""
    if settings.heal == "off":
        return 0.0
    return restore_token_margin_s(settings) + 2 * settings.heal_timeout_s + settings.session_drain_s


def heal_wait_s(settings: FaultSettings) -> float:
    """The longest the fault gate waits for its heal, from the heal's start (`fault_gate.FaultGate`): `heal_timeout_s`
    and the settle after its last restart, `heal_settle_s`."""
    return settings.heal_timeout_s + settings.heal_settle_s


def heal_deferral_s(settings: FaultSettings) -> float:
    """The platform's own time of the longest deferral of its health check with a declared heal (see the module
    docstring): the session drain after an outage run (`session_drain_s`; 0 declares none), then the fault gate's heal
    of the stuck pairs that run left (`heal_wait_s`); 0 with the heal off. The core's waits between and around them
    (`recheck_s`, `poll_interval_s`, `health_timeout_s`) are not here: the gate declares how many of each
    (`FaultGate.max_deferral_s`) and the core adds its own figures."""
    if settings.heal == "off":
        return 0.0
    return settings.session_drain_s + heal_wait_s(settings)


def heal_link_reserve(settings: FaultSettings) -> float:
    """The declared bounds of a ping and a link restart: two container lookups and the ping, the RPC and its margin,
    and one settle."""
    return 3 * command_bound_s(settings) + link_command_s(settings) + settings.heal_settle_s


def heal_container_reserve(settings: FaultSettings) -> float:
    """The declared bounds of a reader's container restart: its lookup and inspection, its stop and its start, the
    waits for it to go down and to come back, the wait for it to serve, a margin of three commands (a start in flight
    and one reading past the restart's own deadline, `Recovery.restart_container`), and one settle."""
    return (
        6 * command_bound_s(settings)
        + stop_command_s(settings)
        + 2 * settings.settle_timeout_s
        + settings.ready_timeout_s
        + (settings.heal_settle_s)
    )


def heal_participants(config: PlatformConfig) -> set[str]:
    """The providers whose links the link check tests: those of `[link_check]` and of every declared space's support.
    `all_links` restarts every one of them, so each needs a service in `[faults.services]`."""
    found = set(config.link_check.providers) if config.link_check is not None else set()
    for entry in config.spaces:
        found |= set(entry.support.providers)
    return found


def heal_all_links_reserve(settings: FaultSettings) -> float:
    """The declared bounds of the all-links sequence: a ping and a link restart for each provider of
    `[faults.services]`, and one settle."""
    each = heal_link_reserve(settings) - settings.heal_settle_s
    return len(settings.services) * each + settings.heal_settle_s


def default_heal_timeout_s(settings: FaultSettings) -> float:
    """The `heal_timeout_s` a table that leaves it out gets: the least one round of the declared heal needs
    (`heal_round_s`, the figure the load check holds a declared value to, so the two cannot drift) plus
    `HEAL_TIMEOUT_HEADROOM_PCT` percent, rounded up to a multiple of `HEAL_TIMEOUT_STEP_S`; 0 with the heal off.
    Integer percentages keep an exact minute exact (a float tenth would round 3,000 s up to 3,360 s)."""
    least = heal_round_s(settings)
    scaled = least * (100 + HEAL_TIMEOUT_HEADROOM_PCT) / (100 * HEAL_TIMEOUT_STEP_S)
    return math.ceil(scaled) * HEAL_TIMEOUT_STEP_S


def heal_round_s(settings: FaultSettings) -> float:
    """The least `heal_timeout_s` a declared heal needs for one round of its steps: the all-links sequence and one
    container restart; one link restart and one container restart; one link restart; 0 with the heal off."""
    if settings.heal == "off":
        return 0.0
    if settings.heal == "all_links":
        container = heal_container_reserve(settings) if settings.heal_container_fallback else 0.0
        return heal_all_links_reserve(settings) + container
    container = heal_container_reserve(settings) if settings.heal == "link_then_container" else 0.0
    return heal_link_reserve(settings) + container


def faults_fingerprint(config: PlatformConfig) -> dict[str, Any] | None:
    """`[faults]`'s half of the platform's fingerprint (`PlatformConfig.fingerprint`); None when the file has none.

    What a resume must not change about the fault injector, and two shards must share (C06): that it is configured at
    all (every run is then admitted to the federation, an outage run exclusively), whether it is sanctioned, which
    service each target takes down, by the target's label and the SHA-256 of its backend and service name, never a
    name (O12), and every bound of its waits, its restore and its heal after its default (`settings`, the derived
    `heal_timeout_s` when the table leaves it out), each number of its field's type: they decide whether an outage
    run's restore is verified in time or the run is set aside, and how long the federation stays held. Not here: the
    journal and the Compose file (paths in the federation's root), the Compose project (each deployment's own name, so
    two federations of one layout differ in it) and the commands that run Compose and Docker (the host's).
    """
    if "faults" not in load_toml(config.file):
        return None
    settings = load_faults(config.file, config)
    targets = {
        config.provider_label(name): hashlib.sha256(f"{settings.backend}:{service}".encode()).hexdigest()
        for name, service in settings.services.items()
    }
    return {
        "sanctioned": settings.sanctioned,
        "targets": dict(sorted(targets.items())),
        "settings": _bounds_record(settings),
    }


def _bounds_record(settings: FaultSettings) -> dict[str, Any]:
    """The fault injector's bounds as the fingerprint holds them: every one after its default, a whole number of
    attempts as an integer, every other number as a float, so `2` and `2.0` are one value."""
    seconds = (
        "poll_s",
        "settle_timeout_s",
        "command_timeout_s",
        "ready_timeout_s",
        "admission_timeout_s",
        "health_wait_s",
        "gate_timeout_s",
        "heal_timeout_s",
        "heal_settle_s",
        "session_drain_s",
        "stop_bound_s",
        "relay_wait_s",
    )
    return {
        "backend": settings.backend,
        "heal": settings.heal,
        "heal_container_fallback": settings.heal_container_fallback,
        "clear_attempts": int(settings.clear_attempts),
        **{key: float(getattr(settings, key)) for key in seconds},
    }


def compose_driver(
    settings: FaultSettings, runner: CommandRunner = run_command, *, probe: Callable[[str], bool]
) -> ComposeDriver:
    """The Compose driver `[faults]` describes, running its commands through `runner`; `probe(service)` says whether
    the provider behind a service answers its REST API (`expcore_onedata.fault_serves.provider_serves`), which a
    restore is not verified without."""
    if settings.backend != "compose" or settings.compose_file is None:
        raise ConfigError(f'{settings.file}: [faults]: backend = "{settings.backend}" has no Compose driver')
    return ComposeDriver(
        settings.compose_file,
        project=settings.project,
        command=settings.command,
        docker=settings.docker,
        runner=runner,
        timeout_s=settings.command_timeout_s,
        probe=probe,
    )


def hpc_driver(settings: FaultSettings, *, probe: Callable[[str], bool], transport: Any = None) -> HpcDriver:
    """The HPC driver `[faults]` describes (`backend = "hpc"`): its requests go through the federation's fault relay
    (`fault_relay.RelayClient`, in the federation's root), or `transport` (a test's), each command within
    `command_timeout_s` and a stop within `settle_timeout_s`; `probe(service)` as for `compose_driver`."""
    if settings.backend != "hpc":
        raise ConfigError(f'{settings.file}: [faults]: backend = "{settings.backend}" has no HPC driver')
    sent = transport or RelayClient(federation_root(settings.journal), wait_s=settings.relay_wait_s)
    return HpcDriver(sent, timeout_s=settings.command_timeout_s, stop_timeout_s=settings.settle_timeout_s, probe=probe)
