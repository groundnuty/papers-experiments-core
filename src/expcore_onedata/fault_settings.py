# Provenance: new
"""`[faults]` of platform.toml: the fault injector's sanction, journal, Compose settings and services (O10, O12).

`load_faults` reads the table against the loaded configuration and names a mistake by the file, the key and, for a
service entry, its position, never by a provider's or a service's name. The service names live here and nowhere in code.

    [faults]
    sanctioned = true                # the configuration's half of the sanction; false or absent refuses
    journal = "fault-journal"        # directory of the injection journal and the targets' lock files
    compose_file = "compose.yaml"    # the Compose file that declares the providers' services
    project = "..."                  # optional: the Compose project name
    command = ["docker", "compose"]  # optional: the Compose command
    docker = ["docker"]              # optional: the command that inspects a container
    poll_s = 2                       # optional: between two readings of a service
    settle_timeout_s = 180           # optional: the longest a service may take to go down or to come back
    command_timeout_s = 120          # optional: the longest one command may take
    clear_attempts = 3               # optional: how many times a restore is tried
    admission_timeout_s = 3600       # optional: the longest a run waits for the federation's lock

    [faults.services]
    <provider name> = "<service>"    # each provider that may be taken down, by its name in [providers]
"""

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from expcore.config.loading import Section, load_toml
from expcore_onedata.compose import CommandRunner, ComposeDriver, run_command
from expcore_onedata.config import PlatformConfig

SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
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
    "clear_attempts",
    "admission_timeout_s",
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
    project: str | None = None
    command: tuple[str, ...] = ("docker", "compose")
    docker: tuple[str, ...] = ("docker",)
    poll_s: float = 2.0
    settle_timeout_s: float = 180.0
    command_timeout_s: float = 120.0
    clear_attempts: int = 3
    admission_timeout_s: float = 3600.0


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
    for index, (name, service) in enumerate(services.data.items()):
        if name not in config.providers:
            raise services.fail(None, f"entry {index} is not a provider declared in [providers]")
        if not isinstance(service, str) or not SERVICE.fullmatch(service):
            raise services.fail(None, f"entry {index} must be a service name: letters, digits, '_', '.' and '-'")
    words = {}
    for key, default in (("command", ("docker", "compose")), ("docker", ("docker",))):
        words[key] = tuple(table.strings(key)) or default
    return FaultSettings(
        file=file,
        sanctioned=sanctioned,
        journal=(file.parent / table.string("journal")).resolve(),
        compose_file=(file.parent / table.string("compose_file")).resolve(),
        services=dict(services.data),
        project=table.optional_string("project"),
        command=words["command"],
        docker=words["docker"],
        poll_s=_number(table, "poll_s", 2.0, 0.001),
        settle_timeout_s=_number(table, "settle_timeout_s", 180.0, 0),
        command_timeout_s=_number(table, "command_timeout_s", 120.0, 1),
        clear_attempts=_number(table, "clear_attempts", 3, 1, integer=True),
        admission_timeout_s=_number(table, "admission_timeout_s", 3600.0, 0),
    )


def compose_driver(settings: FaultSettings, runner: CommandRunner = run_command) -> ComposeDriver:
    """The Compose driver `[faults]` describes, running its commands through `runner`."""
    return ComposeDriver(
        settings.compose_file,
        project=settings.project,
        command=settings.command,
        docker=settings.docker,
        runner=runner,
        timeout_s=settings.command_timeout_s,
    )
