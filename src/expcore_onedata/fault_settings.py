# Provenance: new
"""`[faults]` of platform.toml: the fault injector's sanction, journal, Compose settings and services (O10, O12).

`load_faults` reads the table against the loaded configuration and names a mistake by the file, the key and, for a
service entry, its position, never by a provider's or a service's name. The service names live here and nowhere in code.

    [faults]
    sanctioned = true                # the configuration's half of the sanction; false or absent refuses
    journal = "fault-journal"        # directory of the injection journal and the targets' lock files
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
    heal_timeout_s = 3600            # optional, provisional: the longest the heal of a stuck link may take in all
    heal_settle_s = 180              # optional, provisional: the no-fetch wait after a heal's restarts

    [faults.services]
    <provider name> = "<service>"    # each provider that may be taken down, by its name; no two share a service

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

`heal` says what the restore does when a link is stuck after it (`links_stuck`, O35; `expcore_onedata.fault_heal`):
`"off"` (the default) fails the restore closed, as before. `"all_links"`, the primary mode, restarts the rtransfer link
of every provider in quick succession, settles with no fetch, checks every pair, and falls back to restarting a stuck
pair's reader's container. `"link_then_container"` and `"link"` are the per-reader design, kept: each stuck pair's
reader's link restarted in turn, then (in the first) its container. A container restart is itself an outage of that
provider, so declare a mode that can make one knowingly. `heal_settle_s` is the wait after the restarts, with no
probe, no fetch and no run. `heal_timeout_s` bounds the heal: no step starts whose declared bounds would carry the heal
past it, so a declared heal must leave room for one round of its steps, or it is refused at load, naming the key and
the least it may be: for `"all_links"`, `heal_all_links_reserve` (per provider of `[faults.services]`, a ping and a
link restart: three commands at `command_timeout_s`, the RPC's bound and its margin; and one settle) and one container
restart
(`heal_container_reserve`: four commands, `settle_timeout_s` twice, `ready_timeout_s` and one settle); for
`"link_then_container"`, `heal_link_reserve` (one provider's ping and link restart, and one settle) and one container
restart; for `"link"`, `heal_link_reserve`. With the other defaults, four providers in `"all_links"` need 3,420 s, which
the default 3,600 s covers; five need 3,930 s, which must raise it. **Both durations are provisional:** the settle and
the idle period that leaves a peer's connection stale are being measured, and the defaults will be tuned from it.
"""

import hashlib
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.config.loading import Section, load_toml
from expcore_onedata.compose import RESTART_LINK_MARGIN_S, RPC_TIMEOUT_S, CommandRunner, ComposeDriver, run_command
from expcore_onedata.config import PlatformConfig

SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# A Compose project name as Compose accepts it. Required, because Compose would otherwise take the name from the
# environment, the Compose file or its directory, and a target must name the one container its outage stops.
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
STOP_GRACE_S = 120.0  # the federation's `stop_grace_period` for a provider (deploy/compose-federation/fedcompose.py)
SETTLE_MARGIN_S = 60.0  # what the default settle timeout adds to it, for the daemon's kill and the readings' lag
DEFAULT_SETTLE_TIMEOUT_S = STOP_GRACE_S + SETTLE_MARGIN_S
DEFAULT_READY_TIMEOUT_S = 180.0  # the federation's restored providers served 48 to 52 s after their restart (measured)
DEFAULT_GATE_TIMEOUT_S = 240.0  # a peer session's renewal backs off to 180 s (inference from source), plus a margin
DEFAULT_HEAL_TIMEOUT_S = 3600.0  # provisional: four providers' all-links sequence and one container restart fit it
DEFAULT_HEAL_SETTLE_S = 180.0  # provisional, until the other study's measurement tunes it
HEAL_MODES = ("off", "all_links", "link_then_container", "link")
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
    "services",
)


@dataclass(frozen=True)
class FaultSettings:
    """`[faults]` of platform.toml, validated: the sanction, the journal, the Compose settings and the services."""

    file: Path
    sanctioned: bool
    journal: Path
    compose_file: Path
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
    heal_timeout_s: float = DEFAULT_HEAL_TIMEOUT_S
    heal_settle_s: float = DEFAULT_HEAL_SETTLE_S
    config_sha256: str = ""  # the digest of the platform.toml it was read from: part of the federation's identity


def _number(table: Section, key: str, default: float, minimum: float, *, integer: bool = False) -> float:
    """Read an optional finite number at or above a minimum."""
    value = table.data.get(key, default)
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value < minimum:
        raise table.fail(key, f"must be {'an integer' if integer else 'a number'} of at least {minimum:g}")
    return value


def load_faults(path: str | os.PathLike[str], config: PlatformConfig) -> FaultSettings:
    """Read `[faults]` of a platform.toml against its loaded configuration; raise ConfigError naming the file and key.

    A message names a service entry by its position, never by the provider's or the service's name.
    """
    file = Path(path).resolve()
    table = Section(load_toml(file), file, "platform.toml").section("faults")
    table.reject_unknown(_KEYS)
    sanctioned = table.data.get("sanctioned", False)
    if not isinstance(sanctioned, bool):
        raise table.fail("sanctioned", "must be true or false")
    services = Section(table.table("services"), file, "[faults.services]")
    if not services.data:
        raise services.fail(None, "must name the service of at least one provider")
    project = table.string("project")
    if not PROJECT.fullmatch(project):
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
    words = {}
    for key, default in (("command", ("docker", "compose")), ("docker", ("docker",))):
        words[key] = tuple(table.strings(key)) or default
    settings = FaultSettings(
        file=file,
        sanctioned=sanctioned,
        journal=(file.parent / table.string("journal")).resolve(),
        compose_file=(file.parent / table.string("compose_file")).resolve(),
        services=dict(services.data),
        project=project,
        command=words["command"],
        docker=words["docker"],
        poll_s=_number(table, "poll_s", 2.0, 0.001),
        settle_timeout_s=_number(table, "settle_timeout_s", DEFAULT_SETTLE_TIMEOUT_S, 0),
        command_timeout_s=_number(table, "command_timeout_s", 120.0, 1),
        ready_timeout_s=_number(table, "ready_timeout_s", DEFAULT_READY_TIMEOUT_S, 0),
        clear_attempts=_number(table, "clear_attempts", 3, 1, integer=True),
        admission_timeout_s=_number(table, "admission_timeout_s", 3600.0, 0),
        health_wait_s=_number(table, "health_wait_s", 30.0, 0),
        gate_timeout_s=_number(table, "gate_timeout_s", DEFAULT_GATE_TIMEOUT_S, 0),
        heal=heal,
        heal_timeout_s=_number(table, "heal_timeout_s", DEFAULT_HEAL_TIMEOUT_S, 0),
        heal_settle_s=_number(table, "heal_settle_s", DEFAULT_HEAL_SETTLE_S, 0),
        config_sha256=config.sha256,
    )
    least = heal_round_s(settings)
    if settings.heal_timeout_s < least:
        raise table.fail(
            "heal_timeout_s",
            f'must be at least {least:g} for heal = "{heal}": one round of its actions at their declared bounds',
        )
    return settings


def heal_link_reserve(settings: FaultSettings) -> float:
    """The declared bounds of a ping and a link restart: two container lookups and the ping, the RPC and its margin,
    and one settle."""
    return 3 * settings.command_timeout_s + RPC_TIMEOUT_S + RESTART_LINK_MARGIN_S + settings.heal_settle_s


def heal_container_reserve(settings: FaultSettings) -> float:
    """The declared bounds of a reader's container restart: its lookup and inspection, its stop and its start, the
    waits for it to go down and to come back, the wait for it to serve, and one settle."""
    return (
        4 * settings.command_timeout_s
        + 2 * settings.settle_timeout_s
        + settings.ready_timeout_s
        + (settings.heal_settle_s)
    )


def heal_all_links_reserve(settings: FaultSettings) -> float:
    """The declared bounds of the all-links sequence: a ping and a link restart for each provider of
    `[faults.services]`, and one settle."""
    each = heal_link_reserve(settings) - settings.heal_settle_s
    return len(settings.services) * each + settings.heal_settle_s


def heal_round_s(settings: FaultSettings) -> float:
    """The least `heal_timeout_s` a declared heal needs for one round of its steps: the all-links sequence and one
    container restart; one link restart and one container restart; one link restart; 0 with the heal off."""
    if settings.heal == "off":
        return 0.0
    if settings.heal == "all_links":
        return heal_all_links_reserve(settings) + heal_container_reserve(settings)
    container = heal_container_reserve(settings) if settings.heal == "link_then_container" else 0.0
    return heal_link_reserve(settings) + container


def faults_fingerprint(config: PlatformConfig) -> dict[str, Any] | None:
    """`[faults]`'s half of the platform's fingerprint (`PlatformConfig.fingerprint`); None when the file has none.

    What a resume must not change about the fault injector: that it is configured at all (every run is then admitted
    to the federation, an outage run exclusively), whether it is sanctioned, and which container each target takes
    down, by the target's label and the SHA-256 of its Compose project and service names, never a name or a path
    (O12). The bounds of its waits and where its journal lies are how it runs, not what the runs measure, so they are
    left out.
    """
    if "faults" not in load_toml(config.file):
        return None
    settings = load_faults(config.file, config)
    targets = {
        config.provider_label(name): hashlib.sha256(f"{settings.project}/{service}".encode()).hexdigest()
        for name, service in settings.services.items()
    }
    return {"sanctioned": settings.sanctioned, "targets": dict(sorted(targets.items()))}


def compose_driver(
    settings: FaultSettings, runner: CommandRunner = run_command, *, probe: Callable[[str], bool]
) -> ComposeDriver:
    """The Compose driver `[faults]` describes, running its commands through `runner`; `probe(service)` says whether
    the provider behind a service answers its REST API (`expcore_onedata.fault_serves.provider_serves`), which a
    restore is not verified without."""
    return ComposeDriver(
        settings.compose_file,
        project=settings.project,
        command=settings.command,
        docker=settings.docker,
        runner=runner,
        timeout_s=settings.command_timeout_s,
        probe=probe,
    )
