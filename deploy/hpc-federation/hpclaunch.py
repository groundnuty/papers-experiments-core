"""The state store and the step launcher every command of the HPC federation shares: `up` and `down` here, and the
service verbs (`hpcfed_control.py`, M4) through the hook in `hpcfed.py` (DESIGN.md, sections 3, 4 and 7).

- `StateStore`: `read()` the state file; `update(fn)` a locked, atomic read-modify-write of it (`fn` takes the state
  and returns the new one), under `hpc-federation.lock` in the root, so two commands never interleave their writes.
- `Launcher`: steps of the held job the state names, on its own nodes only (every step of a heterogeneous job names
  its component, `--het-group=0` on a service node and the sandbox's group on the agent's node; or the agent's node is
  its own held job, as the state's `sandbox` says). `control(node, argv, *, name, timeout_s)`
  runs a bounded control step (`--exact`, the control reserve's size, the environment allowlist, stdin /dev/null)
  and raises `TimeoutError` at its bound after cancelling that step by its id; `service(node, argv, *, kind, name,
  log)` launches a detached service step of the kind's size; `listed(name)` is the step id `squeue -s` lists under a
  name, asked of every id the federation's steps are listed under (`listings`: the held job's, and its agent's
  component's own or the agent's own job's); `cancel(step_id)` cancels that one step.
"""

import contextlib
import copy
import fcntl
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import hpcstate
from hpcsettings import HpcSettings, Site
from hpcslurm import Completed, Slurm, Step, allowlist

LOCK = "hpc-federation.lock"  # held around each change of the state file
LIFECYCLE = "hpc-federation.lifecycle.lock"  # held for a whole up or down
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

    @contextlib.contextmanager
    def lifecycle(self, what: str) -> Iterator[None]:
        """Hold the root's lifecycle lock for a whole `up` or `down`, never waiting for it: an `up` between its state
        and its `sbatch` must not meet a `down` that finds no job yet and removes the root. The kernel lets it go with a
        process that dies. The service verbs do not take it."""
        descriptor = os.open(self.root / LIFECYCLE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise hpcstate.StateError(f"another up or down of this root is running; {what} refused") from None
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


def listings(state: Dict[str, Any]) -> List[str]:
    """Every job id `squeue -s` lists the federation's steps under, the held job's first: under `het`, also its agent's
    component's own id (`sandbox.component_job`), under which alone a step on the agent's node is listed; under
    `separate`, the agent's own job's id. None of an id the state does not hold yet."""
    sandbox = state.get("sandbox") or {}
    found = [state["allocation"].get("job")]
    found.append(sandbox.get("component_job") if sandbox.get("placement") == "het" else sandbox.get("job"))
    return [job for index, job in enumerate(found) if job is not None and job not in found[:index]]


class Launcher:
    """Steps of the federation's held job, on its own nodes, sized by the layout's `[hpc]` table."""

    def __init__(self, slurm: Slurm, store: StateStore, settings: HpcSettings) -> None:
        self.slurm = slurm
        self.store = store
        self.settings = settings

    def _where(self, node: str, name: str) -> Tuple[str, Optional[int], Optional[str], Site]:
        """The job a step on this node belongs to, the heterogeneous job's component it runs in (None for a job of one
        component, where no step names one) and that component's own id (None: the job's): a service node is the
        services' job's, component 0 of a heterogeneous job; the agent's node is the last component of the same job
        (`het`) or a held job of its own (`separate`). Every step of a heterogeneous job names its component, 0
        included: on the site's Slurm a step of one with no `--het-group` is refused ("No directives to start
        application on any available hetjob components")."""
        state = self.store.read()
        job = state["allocation"].get("job")
        if job is None:
            raise hpcstate.StateError("the state names no held job")
        sandbox = state.get("sandbox") or {}
        if node in (state["allocation"].get("nodes") or []):
            group = 0 if sandbox.get("placement") == "het" else None  # the services' component of a het job
            where: Tuple[str, Optional[int], Optional[str]] = (job, group, None)
        elif sandbox.get("node") is not None and node == sandbox["node"] and sandbox.get("job") is not None:
            where = (sandbox["job"], sandbox.get("het_group"), sandbox.get("component_job"))
        else:
            raise hpcstate.StateError("refusing a step on a node that is not one of the held job's")
        if not STEP_NAME.fullmatch(name):
            raise hpcstate.StateError("a step name must be letters, digits, '_', '.' and '-'")
        return (*where, Site.from_json(state["site"]))

    def _step(self, node: str, argv: Sequence[str], name: str, kind: str, output: Optional[str] = None) -> Step:
        job, group, component, site = self._where(node, name)
        size = self.settings.control if kind == "control" else self.settings.size(kind)
        env = allowlist(site.step_path, site.node_tmp)
        return Step(job, node, name, size.cpus, size.memory_mib, env, tuple(argv), output, group, component)

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
        """The id `squeue -s` lists a step of the federation's under, by its name, or None: asked of every id its
        steps are listed under (`listings`), one `squeue -s -j` each, since a step of a heterogeneous job's further
        component is listed under that component's own id alone, never the leader's (seen live)."""
        for job in listings(self.store.read()):
            step = self.slurm.steps(job).get(name)
            if step is not None:
                return step
        return None

    def cancel(self, step_id: str) -> None:
        """Cancel one step of the federation's own jobs: the held job, a component of it, or the agent's own job."""
        state = self.store.read()
        sandbox = state.get("sandbox") or {}
        ours = {state["allocation"].get("job"), sandbox.get("job"), sandbox.get("component_job")} - {None}
        if "." not in step_id or step_id.split(".")[0].split("+")[0] not in ours:
            raise hpcstate.StateError("refusing to cancel a step of another job")
        self.slurm.cancel_step(step_id)
