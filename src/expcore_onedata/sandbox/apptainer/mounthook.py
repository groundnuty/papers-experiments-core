# Provenance: new
"""Hook (c): the slot a mount data path plugs into on the Apptainer backend (O56).

A mount run needs a host-side helper (oneclient, holding the mount's token) that the sandbox starts before the run's
first cell and stops after its last. The mount's own lane builds it, with the detector of a cell's access to its
process and its token file; until it lands, a mount run is refused at its start (`MOUNT_REFUSED`).

The sandbox calls, in this order: the factory `(node, settings, spec, run_dir)` once the run's directory exists and
before the gate, the collector's canary or any cell; `start()`, which returns the node directory the sandbox binds
read-only into every cell at `[sandbox.mount] path`; `metadata()` for the run's record (never a secret); and `stop()` in
the teardown, after every process of the cells is shown gone and before the run's directory is removed. `stop` shows
the helper and its mount gone or raises `SandboxError`; it is called on every path that ends a sandbox the factory was
called for, a failed start included, and may be called again after a stop that raised.
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
