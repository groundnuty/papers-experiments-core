# Provenance: new
"""What a sandbox is to the rest of the platform layer: its interface, its result type, its errors and the data paths.

The interface is the architecture's `Sandbox` (`start`, `exec`, `collect`, `stop`, `metadata`) with one `ExecResult`
type. The platform layer implements it over containers (`expcore_onedata.sandbox.sandbox.DockerSandbox`); a test, or a
platform on other infrastructure, implements the same protocol without inheriting from anything.

The data path of a run is one of three kinds. The study's `data_path` factor names its levels in the study's own words;
`platform.toml` maps each level to one of these kinds, so no study name is written in code.
"""

import dataclasses
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from expcore_onedata.sandbox.gatepolicy import PAGED


class SandboxError(RuntimeError):
    """The sandbox failed: something did not come up, did not go away, or was not as declared."""


class StartRefused(SandboxError, ValueError):
    """`start` refused before any container, network or mount existed: a rejected setting or a moved image tag (O13).

    Nothing was created, so the caller loses no attempt. A start that fails after it created something raises
    `SandboxError` instead, and its cleanup has run (or is registered to run again).
    """


class DataPathMode(StrEnum):
    """How the agent reaches the study space: the three data paths a condition can have."""

    MOUNT = "mount"  # a read-only FUSE mount made by a oneclient sidecar, with the data-path token
    REST = "rest"  # the REST interface, only through the data gate, with the data-path token
    NONE = "none"  # no data path: no mount, no gate peer, no token

    @property
    def interface(self) -> str | None:
        """The token's interface caveat for this path (the mounting client or REST); None when there is no token."""
        return {"mount": "oneclient", "rest": "rest"}.get(self.value)


class SecretLike(Protocol):
    """A secret that does not show itself (`tokens.MintedToken`): read with `reveal` where it is used."""

    def reveal(self) -> str: ...


@dataclasses.dataclass(frozen=True)
class Endpoint:
    """A host the sandbox reaches through the gate or the mount: name, port and whether it speaks TLS."""

    host: str
    port: int
    tls: bool = True

    @classmethod
    def from_url(cls, url: str) -> "Endpoint":
        """The endpoint of a configured URL (scheme, host and perhaps a port: `config.py` guarantees nothing else)."""
        parts = urlsplit(url)
        tls = parts.scheme == "https"
        return cls(parts.hostname or "", parts.port or (443 if tls else 80), tls)


@dataclasses.dataclass(frozen=True)
class ReadOnlyInput:
    """Files a study supplies to the agent, such as scripts it may run but not change (O20).

    `source` is a directory on the host holding regular files and directories only; `target` is the absolute path the
    agent sees it at; `sha256` is the hash of its content when it was declared for the campaign (`inputs.declare`), and
    every start refuses an input whose content no longer has it. The sandbox copies it into the run's own directory,
    records the copy's hash, mounts the copy read-only in the agent's container and proves from inside the agent that it
    refuses every change (`expcore_onedata.sandbox.inputs`).
    """

    source: Path
    target: str
    sha256: str | None = None


@dataclasses.dataclass(frozen=True)
class SandboxSpec:
    """What one run asks of a sandbox: its data path and, for a path, the token, the zone and the pinned provider.

    The agent is pinned to its local provider (O03): `provider` is that one, and no other host is reachable. A run with
    no data path has no token and no endpoint. `lease_key` names the leased resource the sandbox's containers, network
    and directories are labelled with, so a probe can prove them gone (C19). `listing` is a REST data path's listing
    mode, `paged` or `complete` (`gatepolicy`, O21); a start refuses another, and any but `paged` on another data path.
    `inputs` are the run's read-only inputs, in the order they are mounted and recorded (O20).
    """

    run_key: str
    mode: DataPathMode
    token: SecretLike | None = None
    zone: Endpoint | None = None
    provider: Endpoint | None = None
    lease_key: str | None = None
    listing: str = PAGED
    inputs: tuple[ReadOnlyInput, ...] = ()


RAN = "ran"  # the cell was started: its exit code, timeout and kill say how it ended
NOT_STARTED = "not_started"  # too little of its time was left to start it: no exit, no timer fired, no timeout (O17)


@dataclasses.dataclass
class ExecResult:
    """What the agent's code did: its output, its exit code, how long it ran, and, when it was killed, why.

    `status` is `RAN`, or `NOT_STARTED` for a cell that too little time was left to start: it ran nothing, so it has no
    timeout and no kill, and what the time running out means is the loop's own rule.

    `timed_out` says that a bound of the sandbox's own ended the cell, with `record=False` as with a cell of the
    agent's, and `bound` says which (`bounds.CELL_TIMER`, `CONTAINER_TIMEOUT` or `HOST_CALL`); it is None exactly when
    `timed_out` is False. An exit 137 or 124 from the container's `timeout` at its limit reads as the bound's, and one a
    cell gave itself (`os.kill(os.getpid(), 9)`) before that limit as its own exit, not a timeout (`bounds`, O39)."""

    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool = False
    kill: dict[str, Any] | None = None  # the cause of a kill or timeout, from the kernel's counters (killcause.py)
    status: str = RAN
    gate_refusals: tuple[str, ...] = ()  # the data gate's own refusals during the cell (gatelog.refusals)
    bound: str | None = None  # the sandbox's bound that ended the cell, when `timed_out` (bounds.py)


@runtime_checkable
class Sandbox(Protocol):
    """One sandboxed run. `start` is the implementation's constructor; the rest is this protocol.

    The guarantees are the implementer's: `exec` bounds itself with `timeout_s` and never raises for what the agent's
    code did; `stop` is idempotent and raises when anything is not verifiably gone; `metadata` holds no secret.
    """

    def exec(self, code: str, timeout_s: float, *, record: bool = True) -> ExecResult:
        """Run Python source as the unprivileged user; `record=False` makes it the harness's own step, never a cell of
        the agent's (O24)."""
        ...

    def collect(self, dest_dir: str | Path) -> dict[str, Any]:
        """Stop what the agent left running and copy the run's evidence to `dest_dir`."""
        ...

    def stop(self) -> None:
        """Tear everything down, verify it is gone, and raise `SandboxError` when it is not."""
        ...

    def metadata(self) -> dict[str, Any]:
        """The facts of the run for its record: images by id, limits, the data path, kills, deaths. Never a secret."""
        ...
