# Provenance: new
"""What a sandbox is to the rest of the platform layer: its interface, its result type, its errors and the data paths.

The interface is the architecture's `Sandbox` (`start`, `exec`, `collect`, `stop`, `metadata`) with one `ExecResult`
type. The platform layer implements it over containers (`expcore_onedata.sandbox.sandbox.DockerSandbox`); a test, or a
platform on other infrastructure, implements the same protocol without inheriting from anything.

The data path of a run is one of three kinds. The study's `data_path` factor names its levels in the study's own words;
`platform.toml` maps each level to one of these kinds, so no study name is written in code.
"""

import dataclasses
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from expcore_onedata.addresses import AddressMap
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
    """A host the sandbox reaches through the gate or the mount: name, port and whether it speaks TLS.

    `address`, when the platform's `[addresses]` map holds the name (O73), is where to connect: the name is a layout's
    declared one, which resolves nowhere, and stays what the handshake asks for and the certificate is checked
    against. It is never the cell's to see: the gate and the mount dial it, no file or variable of a cell holds it."""

    host: str
    port: int
    tls: bool = True
    address: str | None = dataclasses.field(default=None, repr=False)

    @property
    def dial(self) -> str:
        """Where a connection to this endpoint goes: its mapped address, or else its host."""
        return self.address or self.host

    @classmethod
    def from_url(cls, url: str, addresses: AddressMap | None = None) -> "Endpoint":
        """The endpoint of a configured URL (scheme, host and perhaps a port: `config.py` guarantees nothing else), and
        with the platform's address map the address its host is dialled at, when the map holds the host."""
        parts = urlsplit(url)
        tls = parts.scheme == "https"
        host = parts.hostname or ""
        address = addresses.address_of(host) if addresses is not None else None
        return cls(host, parts.port or (443 if tls else 80), tls, address)


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
    `inputs` are the run's read-only inputs, in the order they are mounted and recorded (O20). The read-log channel
    (`expcore_onedata.readlog`) is not asked for here: a platform that declares `readlog_collector_sha256` has it for
    every run.
    """

    run_key: str
    mode: DataPathMode
    token: SecretLike | None = None
    zone: Endpoint | None = None
    provider: Endpoint | None = None
    lease_key: str | None = None
    listing: str = PAGED
    inputs: tuple[ReadOnlyInput, ...] = ()
    # the real names and addresses of the federation's nodes (O73), which the gate scans the answers it relays for:
    # empty where the names are already the declared ones (Compose). Never shown, never in anything a cell can read.
    node_names: tuple[str, ...] = dataclasses.field(default=(), repr=False)


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
    cell gave itself (`os.kill(os.getpid(), 9)`) before that limit as its own exit, not a timeout (`bounds`, O39).

    `output_cut` is what the sandbox cut of the cell's output, which it keeps up to a cap per stream: for each stream it
    cut, the bytes the cell wrote (`produced`) and the bytes kept (`kept`); None when nothing was cut (O43).

    `exec_check` is what the cell's completion record showed (O55, `completion.py`): `source_whole`, whether its program
    arrived whole in the container; `exit_status`, the status the record saw; `nonce_matched`, whether a record of this
    exec came on both streams. An exec not known to have completed that no timeout or kill explains is never a result:
    `exec` raises `completion.ExecIncomplete`. None for a cell that was not started."""

    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool = False
    kill: dict[str, Any] | None = None  # the cause of a kill or timeout, from the kernel's counters (killcause.py)
    status: str = RAN
    gate_refusals: tuple[str, ...] = ()  # the data gate's own refusals during the cell (gatelog.refusals)
    bound: str | None = None  # the sandbox's bound that ended the cell, when `timed_out` (bounds.py)
    # the streams of the cell the sandbox cut (O43): `{"stdout": {"produced": n, "kept": m}, ...}`, only those it cut
    output_cut: dict[str, dict[str, int]] | None = None
    # what the cell's completion record showed (O55): `{"source_whole": ..., "exit_status": ..., "nonce_matched": ...}`
    exec_check: dict[str, Any] | None = None
    # the cell's deadline aborted the run's mount and the survivors were the agent's: the mount is to be restored
    # (`MountRestoring.restore_mount`) before anything else runs, and the kill stays the agent's time limit (O34)
    restore_pending: bool = False
    # the record of the cell's deadline when it fired with a process that outlived the kill, however it ended (the
    # live dict of `cells.CellRunner`: a restore updates it, so read it after the restore); None for any other cell
    deadline: dict[str, Any] | None = None


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


class MountRestoring(Protocol):
    """A sandbox that can bring a run's mount back after a cell's deadline aborted it (O34): `restore_mount()` restores
    it as the start made it, and answers `{"ok": True, "generation": n, ...}`, `{"ok": False, "why": ...}` when it could
    not (the run is then ended and every later call raises `sandbox`), or `{"ok": None, "why": ...}` when nothing was
    pending. It never raises for what the restore does. The code tool calls it, inside the agent's paused clock, when a
    cell's result says `restore_pending`. Optional beside `Sandbox`: a sandbox that has none never reports a restore."""

    def restore_mount(self) -> dict[str, Any]: ...


@runtime_checkable
class InfraReadings(Protocol):
    """What a study reads of a run's infrastructure after its loop, by the same names and in the same shapes on every
    backend (`DockerSandbox`, `ApptainerSandbox`). Read them while the sandbox is up, after its collection or before.

    `infra_check()` is `{"containers": {role: {"start", "end", "problems", ...}}, "deaths": [...], "death": [...]}`:
    the state of each of the run's other components by role (Docker's gate and mount sidecar; on an HPC node the
    gate, the egress collector, the mount's oneclient with its mount and the mount guard's watcher) against what it was
    when it came up, each death with `role`, `condition`, `cause`, `category`, `basis` and `problems`
    (`infradeath`), and the problems of the deaths that are infrastructure. A component the harness stopped itself is
    judged by its state just before that stop. `start` and `end` hold each backend's own evidence (a container's state,
    or a process's and a mount's), and a state that cannot be read is a problem, never a clean one (O17).

    `mount_log()` is oneclient's INFO log, `{"text": ...}` with the data-path token replaced by `<token>`, or
    `{"unavailable": reason, ...}` when it could not be read; None without a mount. `mount_errors()` is the summary of
    that same text (`oneclient_log.summarize`, which `oneclient_log.platform_errors` counts), the same `unavailable`
    answer, or None (O18). Both may be read during the run: on an HPC node the read is the driver's, of a file in a
    directory no cell reaches, which the mount guard does not watch (O60).
    """

    def infra_check(self) -> dict[str, Any]: ...

    def mount_log(self) -> dict[str, Any] | None: ...

    def mount_errors(self) -> dict[str, Any] | None: ...


@dataclasses.dataclass(frozen=True)
class ServerProcess:
    """Where a backend runs a run's tool server, when it names it (`SandboxPlatform.tool_server` asks the sandbox's
    `tool_server_process()`; the Apptainer backend's, O56): `home`, a directory of the run's own, which the server
    takes as its `HOME` and starts in, and `env`, the settings beside the launcher's (the run's tag). With one, the
    launcher names the server's whole environment (`toolserver.ToolServerLauncher.launch`). Docker's sandbox names
    none, and its server's process is as it always was."""

    home: Path
    env: Mapping[str, str]
