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


@dataclasses.dataclass
class ExecResult:
    """What the agent's code did: its output, its exit code, how long it ran, and, when it was killed, why."""

    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool = False
    kill: dict[str, Any] | None = None  # the cause of a kill or timeout, from the kernel's counters (killcause.py)


@runtime_checkable
class Sandbox(Protocol):
    """One sandboxed run. `start` is the implementation's constructor; the rest is this protocol.

    The guarantees are the implementer's: `exec` bounds itself with `timeout_s` and never raises for what the agent's
    code did; `stop` is idempotent and raises when anything is not verifiably gone; `metadata` holds no secret.
    """

    def exec(self, code: str, timeout_s: float) -> ExecResult:
        """Run Python source as the unprivileged user."""
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
