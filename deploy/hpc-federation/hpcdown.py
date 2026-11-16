"""`down` of the HPC federation (DESIGN.md, section 5): whatever `up` reached, every service torn down (the providers,
then the zone), every step gone, each node checked, the held job cancelled by its own id, the srun clients ended, and
the root removed. Mixed into `hpcdriver.Driver`, whose state store, launcher and Slurm it uses.
"""

import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, List, Mapping, Optional

from fedlayout import ROOT_MARKER
from hpcslurm import Client

BUNDLE = Path(__file__).resolve().parent / "bundle"
WORKDIR_LINE = re.compile(r"^hpcop: work directory (/\S+)$", re.M)  # the batch scripts' own line (zone.sbatch)


def remove_tree(root: Path) -> None:
    """Remove a tree that may hold read-only directories: each directory (never a link, never through one) is made
    writable by its owner first; `chmod -R` would follow a link given as an argument."""
    for directory, subdirectories, _ in os.walk(root, followlinks=False):
        for name in subdirectories:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chmod(path, stat.S_IMODE(path.lstat().st_mode) | stat.S_IRWXU)
    os.chmod(root, stat.S_IRWXU)
    shutil.rmtree(root)


class Down:
    """down, and what it is made of; `hpcdriver.Driver` provides the rest."""

    def down(self) -> str:
        if not self.root.exists():
            return "down: the root is gone already"
        if not self.layout.owned:
            raise RuntimeError(f"the root is not marked as this federation's ({ROOT_MARKER}); nothing stopped")
        if not self.store.exists():
            remove_tree(self.root)
            return "down: no state file, so up never submitted a job; the root removed"
        state = self.set(lambda s: s.update(phase="stopping"))
        job = state["allocation"].get("job") or self.job_by_name(state)
        unverified: List[str] = []
        if job is not None:
            info = self.slurm.job(job)
            if info is not None and info.get("JobState") == "RUNNING":
                unverified = self.stop_services(job)
            elif any(r["workdir"] and r["state"] != "removed" for r in state["services"].values()):
                self.say(
                    "the held job had ended: its nodes could not be checked (the next job's sweep removes leftovers)"
                )
            if info is not None:
                self.slurm.cancel(job)
                self.wait_gone(job)
        self.end_clients()
        if unverified:
            self.set(lambda s: s.update(phase="down"))
            raise RuntimeError(
                f"the held job is cancelled, but {', '.join(unverified)} could not be verified clean; the root and its "
                "state are kept: run down again to remove them"
            )
        remove_tree(self.root)
        return f"down: the held job {job or '(never submitted)'} is gone, and so is the root"

    def job_by_name(self, state: Mapping[str, Any]) -> Optional[str]:
        """The held job by its allocation's name, when its id never reached the state: an exact name holding up's
        random part, among this user's jobs. Never more than one."""
        found = self.slurm.jobs_named(self.user, state["allocation"]["name"])
        if len(found) > 1:
            raise RuntimeError("more than one job carries this federation's allocation name; nothing cancelled")
        if found:
            self.set(lambda s: s["allocation"].update(job=found[0]))
        return found[0] if found else None

    def stop_services(self, job: str) -> List[str]:
        """Tear down every service (the providers, then the zone), wait for every step to leave, then check each
        node; return the services that could not be verified."""
        order = [p.name for p in self.spec.providers] + ["zone"]
        unverified = [service for service in order if not self.teardown(service)]
        self.wait_steps(job)
        return unverified + [s for s in order if s not in unverified and not self.node_clean(s)]

    def teardown(self, service: str) -> bool:
        """teardown.sh in a control step on the service's node; a service whose work directory never reached the state
        or its log has its step cancelled (its batch script's trap stops it and removes the directory), unverified."""
        state = self.store.read()
        record, prefix = state["services"][service], state["allocation"]["name"]
        if record["workdir"] is None and record["log"] and Path(record["log"]).is_file():
            text = Path(record["log"]).read_text(encoding="utf-8", errors="replace")
            if WORKDIR_LINE.search(text):
                self.record_workdir(service, text)
                record = self.store.read()["services"][service]
        if record["workdir"] is None:
            listed = self.launcher.listed(record["step"]["name"]) if record["step"] else None
            if listed is not None:
                self.launcher.cancel(listed)
            return record["step"] is None
        argv = ["bash", str(BUNDLE / "teardown.sh"), record["kind"], "--workdir", record["workdir"]]
        argv += ["--timeout", str(self.settings.stop_seconds)]
        name = f"{prefix}-{service}-teardown-{record['launch']}"
        try:
            done = self.launcher.control(record["node"], argv, name=name, timeout_s=self.settings.stop_seconds + 120.0)
        except TimeoutError:
            return False
        ok = done.returncode == 0
        self.set(lambda s: s["services"][service].update(state="removed" if ok else "failed"))
        return ok

    def wait_steps(self, job: str) -> None:
        """Until no step but the batch and extern steps is listed, within the stop's whole bound; a step still listed
        then is cancelled by its own id."""
        deadline = self.clock() + self.settings.stop_bound_s
        while self.slurm.steps(job) and self.clock() < deadline:
            self.sleep(self.poll_s)
        for step_id in self.slurm.steps(job).values():
            self.slurm.cancel_step(step_id)

    def node_clean(self, service: str) -> bool:
        """A read-only control step: the service's work directory is gone from its node."""
        state = self.store.read()
        record = state["services"][service]
        if record["workdir"] is None:
            return True
        name = f"{state['allocation']['name']}-{service}-check"
        try:
            done = self.launcher.control(
                record["node"], ["test", "!", "-e", record["workdir"]], name=name, timeout_s=self.timeouts["step"]
            )
        except TimeoutError:
            return False
        return done.returncode == 0

    def wait_gone(self, job: str) -> None:
        deadline = self.clock() + self.timeouts["cancel"]
        while not self.slurm.gone(job):
            if self.clock() >= deadline:
                raise RuntimeError(
                    f"the held job {job} is still listed {self.timeouts['cancel']:.0f} s after its cancel"
                )
            self.sleep(self.poll_s)

    def end_clients(self) -> None:
        """End every recorded srun client still running: their steps are gone by now."""
        for client in self.store.read().get("clients", {}).values():
            self.slurm.runner.end_client(Client(int(client["pid"]), int(client["start"])))
