# Provenance: new
"""Build the context of a day-0 run from a platform.toml, run the registered checks, and write the report."""

import os
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError, load_toml
from expcore_onedata.admission import fault_settings_of
from expcore_onedata.client import OnezoneClient, tcp_connect
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.day0.registry import REGISTRY, Context, Result, failed
from expcore_onedata.day0.report import Entry, build_report, stamp, write_report
from expcore_onedata.fault_recovery import FaultDriver
from expcore_onedata.fault_settings import compose_driver
from expcore_onedata.provision import resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.sandbox.hostops import DockerHost, HostOps
from expcore_onedata.tokens import TokenIssuer

MUTATE_VARIABLE = "EXPCORE_LIVE_MUTATE"
FAULTS_VARIABLE = "EXPCORE_LIVE_FAULTS"
DOCKER_VARIABLE = "EXPCORE_LIVE_DOCKER"
EXIT_PASS, EXIT_FAIL, EXIT_UNKNOWN, EXIT_USAGE = 0, 1, 3, 2


def mutation_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """The caller has allowed the checks that change state when EXPCORE_LIVE_MUTATE is exactly "1"."""
    return (os.environ if environ is None else environ).get(MUTATE_VARIABLE) == "1"


def faults_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """The caller has allowed taking a provider down when EXPCORE_LIVE_FAULTS is exactly "1" (O10)."""
    return (os.environ if environ is None else environ).get(FAULTS_VARIABLE) == "1"


def containers_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """The caller has allowed the outage check to start containers when EXPCORE_LIVE_DOCKER is exactly "1"."""
    return (os.environ if environ is None else environ).get(DOCKER_VARIABLE) == "1"


def build_context(
    platform: str | Path,
    out_dir: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    allow_mutation: bool | None = None,
    client: OnezoneClient | None = None,
    ceiling_s: float = 300.0,
    samples: int = 5,
    tcp: Callable[[str, int, float], float] | None = tcp_connect,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    allow_faults: bool | None = None,
    driver: FaultDriver | None = None,
    outage_bound_s: float = 60.0,
    agent_host: HostOps | None = None,
    agent_options: Mapping[str, Any] | None = None,
    tool_condition: str | None = None,
) -> Context:
    """Load the target configuration, which must be named, and build the client and the issuer for it.

    The configuration is validated with its secrets present. `[placement]` is read when the file has it. A client may be
    handed in (a test's fake); otherwise one is built that talks to the configured federation.

    A platform.toml with a `[faults]` table gives the outage check the fault injector's settings
    (`fault_settings.load_faults`) and its Compose driver (`compose_driver`), unless a `driver` is handed in. Taking a
    provider down also needs `allow_faults` (`EXPCORE_LIVE_FAULTS=1` by default). The outage check starts a sandbox
    and the tool server on `agent_host`, by default a `DockerHost` when `EXPCORE_LIVE_DOCKER=1` and platform.toml has
    a `[sandbox]` table, and on none otherwise.
    """
    path = Path(platform)
    config = load_platform(path, environ=environ)
    placement = load_placement(path, config) if "placement" in load_toml(path) else None
    client = client or OnezoneClient(config, environ=environ)
    settings = fault_settings_of(config)
    ctx = Context(
        config=config,
        client=client,
        issuer=TokenIssuer(client, config, clock=now),
        out_dir=Path(out_dir),
        placement=placement,
        allow_mutation=mutation_allowed(environ) if allow_mutation is None else allow_mutation,
        ceiling_s=ceiling_s,
        samples=samples,
        tcp_connect=tcp,
        clock=clock,
        sleep=sleep,
        now=now,
        allow_faults=faults_allowed(environ) if allow_faults is None else allow_faults,
        fault_settings=settings,
        fault_driver=driver or (compose_driver(settings) if settings else None),
        outage_bound_s=outage_bound_s,
        agent_host=agent_host or _default_host(config, environ),
        agent_options=dict(agent_options or {}),
        tool_condition=tool_condition,
    )
    ctx.provider_ids = resolve_provider_ids(config)
    return ctx


def _default_host(config: PlatformConfig, environ: Mapping[str, str] | None) -> HostOps | None:
    """The real host, when the caller allowed containers and the platform has a sandbox to start; else none."""
    if config.sandbox is None or not containers_allowed(environ):
        return None
    return DockerHost(sudo_command=config.sandbox.sudo)


def run_checks(ctx: Context, names: Iterable[str] | None = None) -> list[Entry]:
    """Run the named checks (all, in registration order, by default) and return their results.

    A check that raises is recorded as failed, with the exception's class and no message; one that is not registered
    is a usage error.
    """
    wanted = list(names) if names else list(REGISTRY)
    unknown_names = [n for n in wanted if n not in REGISTRY]
    if unknown_names:
        raise ConfigError(f"unknown day-0 check(s): {', '.join(unknown_names)}; known: {', '.join(REGISTRY)}")
    entries = []
    for name in wanted:
        check = REGISTRY[name]
        began = ctx.clock()
        try:
            result: Result = check.run(ctx)
        except Exception as error:  # a check that crashes is a failed check, never a pass
            result = failed(f"the check raised {type(error).__name__}")
        entries.append(
            Entry(
                name, check.title, check.mutates, result.ok, result.summary, result.facts, round(ctx.clock() - began, 3)
            )
        )
    return entries


def exit_code(entries: list[Entry]) -> int:
    """0 when every check passed, 1 when any failed, 3 when none failed but some are unknown."""
    if any(e.ok is False for e in entries):
        return EXIT_FAIL
    return EXIT_UNKNOWN if any(e.ok is None for e in entries) else EXIT_PASS


def run(
    platform: str | Path,
    out_dir: str | Path,
    *,
    names: Iterable[str] | None = None,
    **options,
) -> tuple[int, Path, Path]:
    """Run the checks against a platform.toml and write the dated report; return the exit code and the two paths."""
    ctx = build_context(platform, out_dir, **options)
    entries = run_checks(ctx, names)
    code = exit_code(entries)
    report = build_report(
        entries, ctx.config, utc=stamp(ctx.now()), mutation_allowed=ctx.allow_mutation, exit_code=code
    )
    json_path, md_path = write_report(Path(out_dir), report)
    return code, json_path, md_path
