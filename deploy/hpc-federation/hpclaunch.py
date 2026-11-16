"""The state store and the step launcher every command of the HPC federation shares: `up` and `down` here, and the
service verbs (`hpcfed_control.py`, M4) through the hook in `hpcfed.py` (DESIGN.md, sections 3, 4 and 7).

- `StateStore`: `read()` the state file; `update(fn)` a locked, atomic read-modify-write of it (`fn` takes the state
  and returns the new one), under `hpc-federation.lock` in the root, so two commands never interleave their writes.
- `Launcher`: steps of the held job the state names, on its own nodes only. `control(node, argv, *, name, timeout_s)`
  runs a bounded control step (`--exact`, the control reserve's size, the environment allowlist, stdin /dev/null)
  and raises `TimeoutError` at its bound after cancelling that step by its id; `service(node, argv, *, kind, name,
  log)` launches a detached service step of the kind's size; `listed(name)` is the step id `squeue -s` lists under a
  name; `cancel(step_id)` cancels that one step.
"""

import contextlib
import copy
import fcntl
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple

import hpcstate
from hpcsettings import HpcSettings, Site
from hpcslurm import Completed, Slurm, Step, allowlist

LOCK = "hpc-federation.lock"
STEP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


class StateStore:
    """The federation's state file, read whole and changed only under its lock."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def exists(self) -> bool:
        return hpcstate.path(self.root).exists()

    def read(self) -> Dict[str, Any]:
        state = hpcstate.load(self.root)
        if state is None:
            raise hpcstate.StateError(f"the root holds no {hpcstate.STATE}: the federation is not up (run up)")
        return state

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        descriptor = os.open(self.root / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def create(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Write the first state; refused when one exists."""
        with self.locked():
            if self.exists():
                raise hpcstate.StateError(f"the root holds a {hpcstate.STATE} already: run down first")
            hpcstate.save(self.root, state)
        return state

    def update(self, change: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        """Read, change and write the state as one step under the lock; return what was written."""
        with self.locked():
            state = change(copy.deepcopy(self.read()))
            if not isinstance(state, dict) or state.get("format") != hpcstate.FORMAT:
                raise hpcstate.StateError("a change of the state returned no state")
            hpcstate.save(self.root, state)
        return state


class Launcher:
    """Steps of the federation's held job, on its own nodes, sized by the layout's `[hpc]` table."""

    def __init__(self, slurm: Slurm, store: StateStore, settings: HpcSettings) -> None:
        self.slurm = slurm
        self.store = store
        self.settings = settings

    def _where(self, node: str, name: str) -> Tuple[str, Site]:
        state = self.store.read()
        job = state["allocation"].get("job")
        if job is None:
            raise hpcstate.StateError("the state names no held job")
        if node not in (state["allocation"].get("nodes") or []):
            raise hpcstate.StateError("refusing a step on a node that is not one of the held job's")
        if not STEP_NAME.fullmatch(name):
            raise hpcstate.StateError("a step name must be letters, digits, '_', '.' and '-'")
        return job, Site.from_json(state["site"])

    def _step(self, node: str, argv: Sequence[str], name: str, kind: str, output: Optional[str] = None) -> Step:
        job, site = self._where(node, name)
        size = self.settings.control if kind == "control" else self.settings.size(kind)
        env = allowlist(site.step_path, site.node_tmp)
        return Step(job, node, name, size.cpus, size.memory_mib, env, tuple(argv), output)

    def control(self, node: str, argv: Sequence[str], *, name: str, timeout_s: float) -> Completed:
        """A bounded control step, stdin /dev/null; `TimeoutError` (`StepTimeout`) at its bound, the step cancelled."""
        return self.slurm.control(self._step(node, argv, name, "control"), timeout_s)

    def control_pipe(
        self, first: Tuple[str, Sequence[str], str], second: Tuple[str, Sequence[str], str], timeout_s: float
    ) -> Tuple[Completed, Completed]:
        """Two control steps, `(node, argv, name)` each, the first's stdout into the second's stdin."""
        left = self._step(first[0], first[1], first[2], "control")
        right = self._step(second[0], second[1], second[2], "control")
        return self.slurm.control_pipe(left, right, timeout_s)

    def service(self, node: str, argv: Sequence[str], *, kind: str, name: str, log: str) -> None:
        """A detached service step of the kind's size (`oz` or `op`), its output in `log`; its srun client is recorded
        in the state, by the step's name, for `down` to end once the step is gone."""
        if kind not in ("oz", "op"):
            raise ValueError("a service step is of kind oz or op")
        client = self.slurm.service(self._step(node, argv, name, kind, output=log), log + ".client")

        def record(state: Dict[str, Any]) -> Dict[str, Any]:
            state.setdefault("clients", {})[name] = {"pid": client.pid, "start": client.start}
            return state

        self.store.update(record)

    def listed(self, name: str) -> Optional[str]:
        state = self.store.read()
        job = state["allocation"].get("job")
        return None if job is None else self.slurm.steps(job).get(name)

    def cancel(self, step_id: str) -> None:
        state = self.store.read()
        if not step_id.startswith(f"{state['allocation'].get('job')}."):
            raise hpcstate.StateError("refusing to cancel a step of another job")
        self.slurm.cancel_step(step_id)
