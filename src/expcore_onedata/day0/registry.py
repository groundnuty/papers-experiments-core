# Provenance: new
"""The registry of day-0 checks, and what a check is given and returns.

A check is a function from a `Context` to a `Result`. `Result.ok` is `True` (passed), `False` (failed) or `None`
(unknown: it could not be measured, or it was not allowed to run). **Unknown is not a pass**: the report lists it apart
and the exit code says so. A check that needs to change state says `mutates=True` and runs only when the caller has
allowed it; without that it returns unknown and does nothing.

No check runs without an explicit target configuration: a `Context` is built from a loaded platform.toml and there is no
default one.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import PlatformConfig
from expcore_onedata.provision import Provisioner
from expcore_onedata.rangemap import Placement
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
    """Everything a check may use: the loaded configuration, the client, and the permission to change state."""

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
    _provisioner: Provisioner | None = field(default=None, repr=False)

    def provisioner(self) -> Provisioner:
        """Return the provisioner of this run, with its change log in the output directory."""
        if self.placement is None:
            raise LookupError("platform.toml has no [placement] table")
        if self._provisioner is None:
            self._provisioner = Provisioner(
                self.client,
                self.config,
                self.placement.settings,
                provider_ids=self.provider_ids,
                log=self.out_dir / "day0-changes.jsonl",
                clock=self.clock,
                sleep=self.sleep,
            )
        return self._provisioner


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
