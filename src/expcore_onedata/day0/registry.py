# Provenance: new
"""The registry of day-0 checks, and what a check is given and returns.

A check is a function from a `Context` to a `Result`. `Result.ok` is `True` (passed), `False` (failed) or `None`
(unknown: it could not be measured, or it was not allowed to run). **Unknown is not a pass**: the report lists it apart
and the exit code says so. A check that needs to change state says `mutates=True` and runs only when the caller has
allowed it; without that it returns unknown and does nothing.

No check runs without an explicit target configuration: a `Context` is built from a loaded platform.toml and there is no
default one.

A check about a space runs in each space it is about (`per_space`). With one declared space its result is the one
space's, as it was before several spaces could be declared; with several (`[[spaces]]`) it is the worst over the spaces,
failed before unknown before passed, and each space is named by its position, `spaces[i]`, never by its label or name.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import PlatformConfig, StudySpace
from expcore_onedata.day0.report import status
from expcore_onedata.fault_recovery import FaultDriver
from expcore_onedata.fault_settings import FaultSettings
from expcore_onedata.provision import Provisioner
from expcore_onedata.rangemap import Placement
from expcore_onedata.sandbox.hostops import HostOps
from expcore_onedata.tokens import TokenIssuer


@dataclass(frozen=True)
class Result:
    """What one check found: pass, fail or unknown, a one-line summary and the facts behind it."""

    ok: bool | None
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)


def passed(summary: str, **facts: Any) -> Result:
    """A check that passed."""
    return Result(True, summary, facts)


def failed(summary: str, **facts: Any) -> Result:
    """A check that failed."""
    return Result(False, summary, facts)


def unknown(summary: str, **facts: Any) -> Result:
    """A check that could not be measured; it is not a pass."""
    return Result(None, summary, facts)


@dataclass
class Context:
    """Everything a check may use: the loaded configuration, the client, and the permission to change state.

    Taking a provider down is a second permission, `allow_faults` (`EXPCORE_LIVE_FAULTS=1`), beside `allow_mutation`
    and beside `sanctioned = true` in `[faults]` of platform.toml (`fault_settings`, `fault_settings.load_faults`).
    `fault_driver` is the fault injector's driver (`compose.ComposeDriver` by default), and `outage_bound_s` the
    longest a read or a call of the outage check waits for an answer. `agent_host` is where the outage check may start
    a sandbox and the tool server (`DockerHost` with `EXPCORE_LIVE_DOCKER=1`; None starts nothing), `agent_options` the
    keywords its `SandboxPlatform` gets (a registry, the sandbox's options), `tool_condition` the tool-server condition
    whose tools it calls (else the first that allows the four knowledge tools), and `outage_ceiling_s` the ceiling the
    agent's tokens are minted for (None, the default, mints them for the scenario's worst case,
    `outage_budget.scenario_s`).
    """

    config: PlatformConfig
    client: OnezoneClient
    issuer: TokenIssuer
    out_dir: Path
    placement: Placement | None = None
    allow_mutation: bool = False
    ceiling_s: float = 300.0
    samples: int = 5
    tcp_connect: Callable[[str, int, float], float] | None = None
    clock: Callable[[], float] = lambda: 0.0
    sleep: Callable[[float], None] = lambda seconds: None
    now: Callable[[], float] = lambda: 0.0
    provider_ids: dict[str, str] = field(default_factory=dict)
    allow_faults: bool = False
    fault_settings: FaultSettings | None = None
    fault_driver: FaultDriver | None = None
    outage_bound_s: float = 60.0
    agent_host: HostOps | None = None
    agent_options: dict[str, Any] = field(default_factory=dict)
    tool_condition: str | None = None
    outage_ceiling_s: float | None = None
    _provisioners: dict[str, Provisioner] = field(default_factory=dict, repr=False)  # by the label of their space

    def provisioner(self, space: str | None = None) -> Provisioner:
        """Return this run's provisioner in a space, named by its label (the only space by default), with its change
        log in the output directory: `day0-changes.jsonl` with one space, and with several one log per space, named by
        its position (`day0-changes-spaces-1.jsonl`), so that no space's teardown reads another space's creations."""
        if self.placement is None:
            raise LookupError("platform.toml has no [placement] table")
        entry = self.config.space_named(space)
        if entry.label not in self._provisioners:
            log = "day0-changes.jsonl"
            if len(self.config.spaces) > 1:
                log = f"day0-changes-{entry.position.replace('[', '-').rstrip(']')}.jsonl"
            self._provisioners[entry.label] = Provisioner(
                self.client,
                self.config,
                self.placement.settings,
                provider_ids=self.provider_ids,
                log=self.out_dir / log,
                space=entry.label,
                clock=self.clock,
                sleep=self.sleep,
            )
        return self._provisioners[entry.label]


def holder_of(ctx: Context, entry: StudySpace) -> str | None:
    """A space's data provider, as its provisioner takes it: a `[[spaces]]` entry's own, else `[placement] holder`."""
    if ctx.config.spaces_table:
        return entry.holder
    return ctx.placement.settings.holder if ctx.placement else None


def worst(verdicts: Iterable[bool | None]) -> bool | None:
    """The worst of several verdicts: failed beats unknown, and unknown beats passed."""
    seen = list(verdicts)
    if any(ok is False for ok in seen):
        return False
    return None if any(ok is None for ok in seen) else True


def per_space(ctx: Context, entries: Iterable[StudySpace], check: Callable[[StudySpace], Result]) -> Result:
    """Run a check's part about one space in each of `entries` and return one result, the worst over the spaces.

    With one declared space it is that space's result as it is, so a one-space report reads as it did before several
    spaces could be declared. With several, the facts hold each space's result, summary and facts under its position,
    and the summary names, with its own summary, each space that did not pass, or on a pass each space. Spaces are
    named by their position alone (O12). No space to run in measures nothing, so it is unknown, never a pass."""
    if len(ctx.config.spaces) == 1:
        return check(ctx.config.spaces[0])
    results = {entry.position: check(entry) for entry in entries}
    if not results:
        return unknown("no declared space is one this check is about, so nothing was measured")
    ok = worst(result.ok for result in results.values())
    facts = {
        position: {**result.facts, "result": status(result.ok), "summary": result.summary}
        for position, result in results.items()
    }
    if ok is True:
        shown = "; ".join(f"{position}: {result.summary}" for position, result in results.items())
        return passed(f"passed in each of the {len(results)} spaces: {shown}", spaces=facts)
    behind = [(position, result) for position, result in results.items() if result.ok is not True]
    shown = "; ".join(f"{position} ({status(result.ok)}): {result.summary}" for position, result in behind)
    return Result(ok, f"{len(behind)} of {len(results)} spaces did not pass: {shown}", {"spaces": facts})


@dataclass(frozen=True)
class Check:
    """A registered check."""

    name: str
    title: str
    mutates: bool
    run: Callable[[Context], Result]


REGISTRY: dict[str, Check] = {}


def register(
    name: str, title: str, *, mutates: bool = False
) -> Callable[[Callable[[Context], Result]], Callable[[Context], Result]]:
    """Register a check under a name; a name is registered once."""

    def add(run: Callable[[Context], Result]) -> Callable[[Context], Result]:
        if name in REGISTRY:
            raise ValueError(f"the day-0 check {name!r} is registered twice")
        REGISTRY[name] = Check(name, title, mutates, run)
        return run

    return add
