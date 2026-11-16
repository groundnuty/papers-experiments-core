# Provenance: new
"""Hook (c): the slot a mount data path plugs into on the Apptainer backend (O56).

A mount run needs a host-side helper (oneclient, holding the mount's token) that the sandbox starts before the run's
first cell and stops after its last. The mount's own lane builds it, with the detector of a cell's access to its
process and its token file (`hostmount.host_mount_factory`); without one, a mount run is refused at its start
(`MOUNT_REFUSED`).

The sandbox calls, in this order: the factory `(node, settings, spec, run_dir)` once the run's directory exists and
before the gate, the collector's canary or any cell; `token_paths()`, the files of the run's directory that will hold
the token while the helper starts, which the sandbox lists in the run's record before they exist, so a reaper of a
driver killed outright removes them before it unmounts or removes anything (O60); `start()`, which returns the node
directory the sandbox binds read-only into every cell at `[sandbox.mount] path`; `metadata()` for the run's record
(never a secret); and `stop()` in the teardown, after every process of the cells is shown gone and before the run's
directory is removed. `stop` shows the helper and its mount gone or raises `SandboxError`; it is called on every path
that ends a sandbox the factory was called for, a failed start included, and may be called again after a stop that
raised.

Two methods a helper may add, which the sandbox looks for and reads as unknown when they are missing: `infra_state()`,
its processes and its mount by role for `infra_check` (`apptainer.infra`; a helper without it is a death of state
unknown), and `oneclient_log()`, oneclient's INFO log as bytes or None, for `mount_log` and `mount_errors` (O17, O18).
`hostmount.HostMount` has both.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

MOUNT_REFUSED = (
    "a mount data path is not available on the Apptainer backend yet: a cell would share the uid of the oneclient "
    "that holds the mount's token, and the mount's helper and its detector are still to land (hook c)"
)


class MountHelper(Protocol):
    def token_paths(self) -> list[Path]:
        """The files of the run's directory that hold the token at some point of the start: listed in the run's record
        before they exist, so the teardown and a reaper remove them first."""
        ...

    def start(self) -> Path:
        """Start the helper and its mount; the node directory to bind read-only at the mount path in every cell."""
        ...

    def stop(self) -> None:
        """Stop it and show it and its mount gone, or raise `SandboxError`. Safe to call again."""
        ...

    def metadata(self) -> dict[str, Any]:
        """The helper's facts for the run's record: never a secret, never a configured name or path."""
        ...


MountHelperFactory = Callable[[Any, Any, Any, Path], MountHelper]  # (node, settings, spec, run_dir) -> helper

__all__ = ["MOUNT_REFUSED", "MountHelper", "MountHelperFactory"]
