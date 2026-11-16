"""`down` of the HPC federation (DESIGN.md, section 5): whatever `up` reached, every service torn down (the providers,
then the zone), every step gone, each node checked, the held job cancelled by its own id, the srun clients ended, and
the root removed. Mixed into `hpcdriver.Driver`, whose state store, launcher and Slurm it uses.
"""

import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import hpcrelay
from fedlayout import ROOT_MARKER
from hpcsettings import Site
from hpcslurm import Client

BUNDLE = Path(__file__).resolve().parent / "bundle"
SHARDS = BUNDLE.parent / "shards.py"  # its `sweep`, on the agent's node at down
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
        with self.store.lifecycle("down"):  # never beside an up that may be between its state and its sbatch
            return self.take_down()

    def take_down(self) -> str:
        """Whatever fails on the way, the held job is cancelled and every client ended before down returns or raises:
        a failure is reported as unverified, never a reason to leave the job billing."""
        if not self.store.exists():
            remove_tree(self.root)
            return "down: no state file, so up never submitted a job; the root removed"
        state = self.set(lambda s: s.update(phase="stopping"))
        unverified: List[str] = []
        if self.relay_available():  # first: no verb may launch a step while the federation goes down
            try:
                if not hpcrelay.stop(self.slurm.runner, sys.executable, self.root):
                    unverified.append("the fault relay")
            except (RuntimeError, OSError) as error:
                self.say(f"the fault relay's stop failed: {error}")
                unverified.append("the fault relay")
        lost: List[str] = []  # a job whose lookup failed: the other is still cancelled, then down fails
        job, info = self.ours(state, "allocation", lost)
        agent, agent_info = self.ours(state, "sandbox", lost) if self.separate(state) else (None, None)
        if info is not None and info.get("JobState") == "RUNNING":
            try:
                unverified += self.stop_services(job)
            except (RuntimeError, OSError, ValueError) as error:
                self.say(f"stopping the services failed ({error}); the held job is cancelled all the same")
                unverified.append("the services")
        elif any(r.get("workdir") and r.get("state") != "removed" for r in state["services"].values()):
            self.say("the held job had ended: its nodes could not be checked (the next job's sweep removes leftovers)")
        swept = ""
        if ((agent_info if self.separate(state) else info) or {}).get("JobState") == "RUNNING":
            try:
                swept = self.sweep_agent_node(state)
            except (RuntimeError, OSError, TimeoutError) as error:
                self.say(f"the agent's node could not be swept ({error}); the held job is cancelled all the same")
                unverified.append("the agent's node")
        self.cancel_all([j for j, i in ((job, info), (agent, agent_info)) if i is not None], lost)
        unverified += self.end_clients([j for j in (job, agent) if j is not None])
        if unverified:
            self.set(lambda s: s.update(phase="down"))
            raise RuntimeError(
                f"the held job is gone, but {', '.join(unverified)} could not be verified clean; the root and its "
                "state are kept: run down again to remove them"
            )
        remove_tree(self.root)
        gone = " and ".join(j for j in (job, agent) if j) or "(never submitted, or no longer ours)"
        return f"down: the held job {gone} is gone, and so is the root" + (f"\n{swept}" if swept else "")

    def sweep_agent_node(self, state: Mapping[str, Any]) -> str:
        """`shards.py sweep` in a control step on the agent's node: every shard step's directory of this federation on
        its tmpfs whose step is shown gone is removed, and one holding an out/ that could not be archived is kept and
        named (the node's tmpfs is the node's, not the job's: no job end removes it). What the sweep printed; nothing
        without an agent's node. RuntimeError when the sweep fails."""
        record = state.get("sandbox") or {}
        if not record.get("node"):
            return ""
        site = Site.from_json(state["site"])
        argv = [site.node_python, "-I", "-B", str(SHARDS), "sweep", "--tmpfs", site.node_tmp, "--root", str(self.root)]
        name = f"{state['allocation']['name']}-sandbox-sweep"
        done = self.launcher.control(record["node"], argv, name=name, timeout_s=self.timeouts["step"])
        if done.returncode != 0:
            raise RuntimeError(f"its sweep ended with status {done.returncode}")
        return done.stdout.strip()

    @staticmethod
    def separate(state: Mapping[str, Any]) -> bool:
        """Whether the agent's node is a held job of its own (`sandbox_placement = "separate"`); under `het` it is a
        component of the held job, and the leader's cancel ends it."""
        return (state.get("sandbox") or {}).get("placement") == "separate"

    def ours(
        self, state: Mapping[str, Any], key: str, lost: List[str]
    ) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
        """One of up's held jobs (`key`: "allocation" or "sandbox"), by its recorded id or else its exact name, and what
        Slurm records of it; (id, None) when Slurm no longer knows it, and (None, None) when its id now names another
        job (Slurm reuses ids): that one is never touched. A lookup that fails is added to `lost`, never cancelled."""
        try:
            job = state[key].get("job") or self.job_by_name(state, key)
            info = self.slurm.job(job) if job is not None else None
        except RuntimeError as error:
            lost.append(f"the {key} job could not be looked up ({error})")
            return None, None
        if info is not None and info.get("JobName") != state[key]["name"]:
            self.say(f"job {job} now names another job: ours ended long ago, and that one is not touched")
            return None, None
        return job, info

    def cancel_all(self, jobs: List[str], failed: List[str]) -> None:
        """Cancel each job by its own id, then wait for each to leave the queue; a failure with one (or a job `failed`
        names already, whose lookup failed) never spares another, and is raised once every job was tried, the srun
        clients left as they are, since their steps may still run."""
        cancelled = []
        for job in jobs:
            try:
                self.slurm.cancel(job)
                cancelled.append(job)
            except RuntimeError as error:
                failed.append(f"the cancel of job {job} failed ({error})")
        for job in cancelled:
            try:
                self.wait_gone(job)
            except RuntimeError as error:
                failed.append(str(error))
        if failed:
            raise RuntimeError(f"{'; '.join(failed)}; the root and its state are kept: run down again")

    def job_by_name(self, state: Mapping[str, Any], key: str = "allocation") -> Optional[str]:
        """One of up's held jobs by its name, when its id never reached the state: an exact name holding up's random
        part (the agent's own job's ends in -sandbox), among this user's jobs. Never more than one."""
        found = self.slurm.jobs_named(self.user, state[key]["name"])
        if len(found) > 1:
            raise RuntimeError("more than one job carries this federation's allocation name; nothing cancelled")
        if found:
            self.set(lambda s: s[key].update(job=found[0]))
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
                try:
                    self.record_workdir(service, text)
                except RuntimeError as error:  # a directory not as the bundle makes one: never removed by name
                    self.say(f"{service}: {error}; its step is cancelled instead")
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

    def end_clients(self, jobs: List[str]) -> List[str]:
        """End every srun client of the jobs still running on this host, verified: the recorded ones, the relay's when
        its stop failed, and any other `srun --jobid=<job>` of ours (one spawned just before a crash). Their steps are
        gone by now. Returns what could not be ended."""
        state = self.store.read()
        recorded = [*state.get("clients", {}).values(), *([state["relay"]] if state.get("relay") else [])]
        clients = {Client(int(c["pid"]), int(c["start"])) for c in recorded}
        for job in jobs:
            clients |= set(self.slurm.runner.clients_of(job))
        return [f"the client {c.pid}" for c in sorted(clients) if not self.slurm.runner.end_client(c)]
