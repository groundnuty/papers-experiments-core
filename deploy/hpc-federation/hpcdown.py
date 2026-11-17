"""`down` of the HPC federation (DESIGN.md, section 5): whatever `up` reached, every service torn down (the providers,
then the zone), every step gone, each node checked, the held job cancelled by its own id, the srun clients ended, the
provider storage on scratch removed and shown gone (hpcstorage), and the root removed. Mixed into `hpcdriver.Driver`,
whose state store, launcher and Slurm it uses.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import hpcevidence
import hpcrelay
import hpcstate
import hpcstorage
from fedlayout import ROOT_MARKER
from hpcremove import SETTLE_S, only_silly, remove_tree, settle_rmdir
from hpcsettings import Site
from hpcslurm import Client, SlurmError

BUNDLE = Path(__file__).resolve().parent / "bundle"
SHARDS = BUNDLE.parent / "shards.py"  # its `sweep`, on the agent's node at down
WORKDIR_LINE = re.compile(r"^hpcop: work directory (/\S+)$", re.M)  # the batch scripts' own line (zone.sbatch)


class Down:
    """down, and what it is made of; `hpcdriver.Driver` provides the rest."""

    def down(self, discard_evidence: bool = False, discard_reason: Optional[str] = None) -> str:
        """The federation taken down and its root removed. A root that a shard step ran against and whose evidence was
        not collected (`evidence_gate`) is refused before anything is stopped, unless `discard_evidence`, which the
        output records."""
        if not self.root.exists():
            return "down: the root is gone already"
        if not self.layout.owned:
            unmarked = f"the root is not marked as this federation's ({ROOT_MARKER}); nothing stopped"
            if not self.removal_remains():
                raise RuntimeError(unmarked)
            with self.store.lifecycle("down"):
                if not self.removal_remains():  # looked at again under the lock: an up may have made it its own
                    raise RuntimeError(unmarked)
                ops = self.removal_ops()
                settle_rmdir(self.root, ops["rmdir"], ops["sleep"], ops["clock"]() + SETTLE_S, ops["clock"])
            return "down: the root an earlier down's removal left (cut short by files held open) is removed"
        with self.store.lifecycle("down"):  # never beside an up that may be between its state and its sbatch
            discarded = self.evidence_gate(discard_evidence, discard_reason)
            done = self.take_down()
        return f"{discarded}\n{done}" if discarded else done

    def evidence_gate(self, discard: bool, reason: Optional[str] = None) -> str:
        """O63: down removes the root, which holds the evidence the study's post-check and the facts read (the state,
        `platform.toml`, `facts.json`, the link check, the render identity, the fault journals), so it refuses, before
        it stops or cancels anything, a root that has shard results recorded against it (a step of a shard started
        there, `shards.py step`) unless `shards.py collect` marked it and its evidence is still exactly what the marked
        archive holds (`hpcevidence.uncollected`): a step that started or wrote since, or a changed file, makes it
        stale. A root where no step ever ran goes as before, and so does one whose down already began (its phase
        `stopping` or `down`: a down that failed halfway and is run again passed this gate before, and its own changes
        to the state are no change of the evidence). `discard` (`--discard-evidence`) goes on, and the line it
        returns, which the output and `say` carry, records what was discarded. Empty when there was nothing to
        refuse or discard."""
        if self.store.exists() and self.store.read().get("phase") in ("stopping", "down"):
            return ""
        found = hpcevidence.uncollected(self.root)
        if found is None:
            return ""
        labels, stale = found
        shown = ", ".join(labels)
        if not discard:
            if stale:
                why = (
                    f"the evidence of {shown} changed after it was collected (a step started or wrote since, or a file "
                    "was changed), so what was collected lacks it"
                )
                remedy = (
                    "end the shard's step, run `shards.py collect` (or `collect --roots-only`) again while it is up"
                )
            else:
                why = f"shard results are recorded in this root ({shown}) and its evidence was not collected"
                remedy = (
                    "run `shards.py collect` (or `collect --roots-only`, which needs no final shard) while it is up"
                )
            raise RuntimeError(
                f"{why}: down would remove the state, facts, link check, render identity and fault journals the "
                f"post-check needs. Nothing was stopped or removed: {remedy}, pull the results and `verify-copy` them; "
                "or give --discard-evidence to remove the root without it"
            )
        line = (
            f"down: --discard-evidence: the root's evidence of {shown} "
            f"{'changed after it was collected' if stale else 'was not collected'} and is removed with it"
            + (f" (reason given: {reason})" if reason else "")
        )
        if self.store.exists():  # the state goes with the root: this line, in down's output, is what outlives it
            entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "shards": labels,
                     "changed_since_collect": stale, "reason": reason}  # fmt: skip
            self.set(lambda state: state.setdefault("evidence_discarded", []).append(entry))
            line += "; recorded in the state file"
        self.say(line)
        return line

    def removal_remains(self) -> bool:
        """Whether the root is what a removal cut short after its marker leaves: empty, or holding nothing but NFS
        silly-rename files, never a link, with the lifecycle lock beside it (which only an `up` of a root it marked,
        or a `down` of a marked root, makes). Such a root is only ever `rmdir`ed, once its silly files are gone:
        nothing in it is removed."""
        root = self.root
        return self.store.lifecycle_path.is_file() and root.is_dir() and not root.is_symlink() and only_silly(root)

    def removal_ops(self) -> Dict[str, Any]:
        """The file calls the root's removal makes (`remove_tree`), the tests' NFS stand-in's in place of any."""
        return {"unlink": os.unlink, "rmdir": os.rmdir, "sleep": time.sleep, "clock": time.monotonic, **self.removal}

    def remove_root(self, gone: str) -> None:
        """The root removed (`remove_tree`); a removal cut short says the job is gone, and that down finishes it."""
        try:
            remove_tree(self.root, **self.removal_ops())
        except (RuntimeError, OSError) as error:
            raise RuntimeError(
                f"{gone}, but the root could not be removed ({error}); run down again to finish its removal"
            ) from None

    def take_down(self) -> str:
        """Whatever fails on the way, the held job is cancelled and every client ended before down returns or raises:
        a failure is reported as unverified, never a reason to leave the job billing."""
        if not self.store.exists():
            self.remove_root("no state file is left (up never submitted a job, or an earlier down removed it)")
            return "down: no state file, so no job of up's is left to cancel; the root removed"
        state = self.set(lambda s: s.update(phase="stopping"))
        unverified: List[str] = []
        relay_unstopped = self.relay_available() and not self.relay_stopped()  # first: no verb may launch a step now
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
        for entry in state.get("shard_nodes", []):  # W1: every other node a shard step of this federation ran on
            try:
                swept = "\n".join(filter(None, [swept, self.sweep_shard_node(state, entry)]))
            except (RuntimeError, OSError, TimeoutError) as error:
                self.say(f"the node shard {entry['label']} was resumed on could not be swept ({error})")
                unverified.append(f"the node shard {entry['label']} was resumed on")
        self.cancel_all([j for j, i in ((job, info), (agent, agent_info)) if i is not None], lost)
        unverified += self.end_clients([j for j in (job, agent) if j is not None])
        if relay_unstopped and not self.relay_stopped():  # its client is ended now: checked again, never read before
            unverified.append("the fault relay")
        if unverified:
            self.set(lambda s: s.update(phase="down"))
            raise RuntimeError(
                f"the held job is gone, but {', '.join(unverified)} could not be verified clean; the root and its "
                "state are kept: run down again to remove them"
            )
        gone = " and ".join(j for j in (job, agent) if j) or "(never submitted, or no longer ours)"
        stored = self.remove_storage(f"the held job {gone} is gone")
        self.remove_root(f"the held job {gone} is gone" + (f" ({swept})" if swept else ""))
        return f"down: the held job {gone} is gone, and so is the root" + "".join(
            f"\n{x}" for x in (swept, stored) if x
        )

    def remove_storage(self, gone: str) -> str:
        """The provider storage `up` made on scratch (hpcstorage), removed and shown gone, only here: the held job and
        every client are gone and every service verified clean, and the state that names the storage still exists. What
        it printed, empty when `up` recorded none (no scratch root, or a state older than the key). A failure keeps the
        root and the state for a second down, which finishes what is left: the removal is idempotent."""
        state = self.store.read()
        if state.get("storage") is None:
            if state.get("site", {}).get(
                "storage_root"
            ):  # up always records the storage it makes: this one lost its record
                self.set(lambda s: s.update(phase="down"))
                raise RuntimeError(
                    f"{gone}, but the state records a scratch root and no provider storage on it, which up always "
                    "records: nothing on scratch was removed, and the root and its state are kept; look at the "
                    "scratch root, then restore the record or remove the scratch root's key from the state"
                )
            return ""
        try:
            return hpcstorage.remove(state["storage"], state, **self.removal_ops())
        except (RuntimeError, OSError) as error:
            self.set(lambda s: s.update(phase="down"))
            raise RuntimeError(
                f"{gone}, but the provider storage on scratch could not be removed ({error}); the root and its state "
                "are kept: run down again to remove it"
            ) from None

    def relay_stopped(self) -> bool:
        """Whether the relay's stop shows no heartbeat beating; a stop that could not be run shows nothing."""
        try:
            return hpcrelay.stop(self.slurm.runner, sys.executable, self.root)
        except (RuntimeError, OSError) as error:
            self.say(f"the fault relay's stop failed: {error}")
            return False

    def sweep_agent_node(self, state: Mapping[str, Any]) -> str:
        """`shards.py sweep` in a control step on the agent's node: every shard step's directory of this federation on
        its tmpfs whose step is shown gone is removed, and one holding an out/ that could not be archived is kept and
        named (the node's tmpfs is the node's, not the job's: no job end removes it); then the sandbox's work root,
        its killed runs reaped (a dead FUSE mount among them) and itself removed once empty (W1, W2). What the sweep
        printed; nothing without an agent's node. RuntimeError when the sweep fails."""
        record = state.get("sandbox") or {}
        if not record.get("node"):
            return ""
        return self.sweep_node(state, record["node"], f"{state['allocation']['name']}-sandbox-sweep")

    def sweep_shard_node(self, state: Mapping[str, Any], entry: Mapping[str, str]) -> str:
        """The sweep on a node a shard was resumed on (`shard_nodes`, which `shards.py resume-line` records), in the job
        it was resumed in, which down never cancels: only while that job runs and is still the one recorded (Slurm
        reuses ids, `ours`); one that ended is said, and its node's leftovers wait for the next shard step there, which
        reaps them before it uses its work root. The agent's node in the agent's own job was swept already."""
        sandbox = state.get("sandbox") or {}
        if entry["node"] == sandbox.get("node") and entry["job"] in (sandbox.get("job"), state["allocation"]["job"]):
            return ""
        info = self.slurm.job(entry["job"])
        if info is None or info.get("JobName") != entry["name"] or info.get("JobState") != "RUNNING":
            self.say(f"shard {entry['label']} was resumed in job {entry['job']}, which has ended: its node is not "
                     "swept here (the next shard step there reaps what it left)")  # fmt: skip
            return ""
        self.end_shard_steps(state, entry["job"])
        name = f"{state['allocation']['name']}-shard-{entry['label']}-sweep"
        return self.sweep_node(state, entry["node"], name, job=entry["job"], live_fails=True)

    def end_shard_steps(self, state: Mapping[str, Any], job: str) -> None:
        """A step of this federation's shards still running in a job down does not cancel (an operator's, where a
        shard was resumed) would outlive its federation: each one listed under that job, by the name every step of a
        shard of this federation carries (`<allocation or agent's job name>-shard-<n>`), is cancelled by its own id,
        and must leave the list within the cancel's bound; RuntimeError otherwise, so the root and its state are kept
        for a second down. No other step of that job is touched."""
        prefixes = [n for n in (state["allocation"]["name"], (state.get("sandbox") or {}).get("name")) if n]
        shard_step = re.compile("(?:" + "|".join(re.escape(p) for p in prefixes) + r")-shard-[0-9]+")
        live = {name: step for name, step in self.slurm.steps(job).items() if shard_step.fullmatch(name)}
        for name, step in live.items():
            self.say(f"a shard step of this federation ({name}) still runs in job {job}: it is cancelled by its id")
            try:
                self.slurm.cancel_step(step)
            except SlurmError as error:  # its leaving the list decides (wait_steps)
                self.say(f"the cancel of step {step} reported a failure ({error})")
        deadline = self.clock() + self.timeouts["cancel"]
        while any(name in self.slurm.steps(job) for name in live) and self.clock() < deadline:
            self.sleep(self.poll_s)
        if any(name in self.slurm.steps(job) for name in live):
            raise RuntimeError(f"a shard step of this federation is still listed in job {job} after its cancel")

    def sweep_node(
        self, state: Mapping[str, Any], node: str, name: str, job: Optional[str] = None, live_fails: bool = False
    ) -> str:
        """`shards.py sweep` on `node` (in `job`, a recorded resume's, when given), by the core's Python on the nodes
        (`HPC_CORE_PYTHON`, which up asks for with an agent's node: the reaper of the sandbox's runs is the core's), or
        the nodes' own for a state an earlier up wrote without it, whose sweep then fails when it finds a work root.
        `live_fails`: a step of this federation still running there fails the sweep (`--live-fails`), never passes."""
        site = Site.from_json(state["site"])
        python = site.core_python or site.node_python
        argv = [python, "-I", "-B", str(SHARDS), "sweep", "--tmpfs", site.node_tmp, "--root", str(self.root)]
        argv += ["--live-fails"] if live_fails else []
        done = self.launcher.control(node, argv, name=name, timeout_s=self.timeouts["step"], job=job)
        if done.returncode != 0:
            tail = (done.stdout or "").strip().splitlines()[-1:]
            raise RuntimeError(f"its sweep ended with status {done.returncode}{': ' + tail[0] if tail else ''}")
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
        """Tear down every service (the providers, then the zone), wait for every step to leave, remove what a step
        cancelled at the bound left (`remove_after_cancel`), then check each node; return the services that could not
        be verified."""
        order = [p.name for p in self.spec.providers] + ["zone"]
        unverified = [service for service in order if not self.teardown(service)]
        cancelled = self.wait_steps(self.listed_under(job))
        swept = [service for service in order if self.remove_after_cancel(service, cancelled)]
        unverified = [service for service in unverified if service not in swept]
        return unverified + [s for s in order if s not in unverified and s not in swept and not self.node_clean(s)]

    def listed_under(self, job: str) -> List[str]:
        """The ids `squeue -s` lists the held job's steps under: its own, and under `het` its agent's component's own,
        under which alone a step on the agent's node is listed (seen live). The agent's own job under `separate` is
        cancelled whole, and its id is never asked here: it may name another job by now (`ours`)."""
        record = self.store.read().get("sandbox") or {}
        component = record.get("component_job") if record.get("placement") == "het" else None
        return [job] + ([component] if component and component != job else [])

    def teardown(self, service: str) -> bool:
        """teardown.sh in a control step on the service's node; a service whose work directory never reached the state
        or its log has its step cancelled, if it is still listed (its batch script's trap stops it and removes the
        directory), and its node is checked once every step has left (`node_clean`)."""
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
            if listed is not None:  # seen, so its end can be shown once it leaves
                self.set(lambda s: s["services"][service]["step"].update(id=listed))
                self.launcher.cancel(listed)
            return True
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

    def wait_steps(self, jobs: List[str]) -> List[str]:
        """Until no step but the batch and extern steps is listed under any of the ids, within the stop's whole bound
        (one deadline for them all); a step still listed then is cancelled by its own id. Returns the names of the
        steps it cancelled that have left the list since (within the cancel's bound).

        A cancel's own exit status is no verdict: on a heterogeneous job `scancel <leader>.<n>` printed "Invalid job id
        specified" and exited 225 while it did end the step (live slot 5, K1). Whether the step left the list is."""
        deadline = self.clock() + self.settings.stop_bound_s
        while any(self.slurm.steps(job) for job in jobs) and self.clock() < deadline:
            self.sleep(self.poll_s)
        cancelled: Dict[str, str] = {}
        for job in jobs:
            for name, step_id in self.slurm.steps(job).items():
                try:
                    self.slurm.cancel_step(step_id)
                except SlurmError as error:
                    self.say(f"the cancel of step {step_id} reported a failure ({error}); its leaving the list decides")
                cancelled[name] = job
        deadline = self.clock() + self.timeouts["cancel"]
        while any(name in self.slurm.steps(job) for name, job in cancelled.items()) and self.clock() < deadline:
            self.sleep(self.poll_s)
        return [name for name, job in cancelled.items() if name not in self.slurm.steps(job)]

    def remove_after_cancel(self, service: str, cancelled: List[str]) -> bool:
        """K1: a service whose step down cancelled (at the stop's bound) and that has left the list was ended outright
        on the site that showed it (the step's processes killed, no trap run), which leaves its work directory, 2 to
        2.6 GB of the node's tmpfs, on its node. Only the work directory the state recorded for it, accepted as the
        bundle makes one (`hpcstate.checked_workdir`), is removed, by teardown.sh in a control step on its node (no
        service of it runs by then: its step is gone), and shown gone by `node_clean`. Nothing it did not record is
        removed: a service whose log never named a work directory is left to `no_workdir_left`, which removes nothing.
        True once the directory is shown gone."""
        state = self.store.read()
        record, site = state["services"][service], Site.from_json(state["site"])
        if (record.get("step") or {}).get("name") not in cancelled or record.get("workdir") is None:
            return False
        try:
            hpcstate.checked_workdir(record["workdir"], site.node_tmp, record["kind"])
        except hpcstate.StateError as error:
            self.say(f"{service}: {error}; nothing is removed")
            return False
        argv = ["bash", str(BUNDLE / "teardown.sh"), record["kind"], "--workdir", record["workdir"], "--timeout", "0"]
        name = f"{state['allocation']['name']}-{service}-remove-{record['launch']}"
        try:
            self.launcher.control(record["node"], argv, name=name, timeout_s=self.timeouts["step"])
        except TimeoutError:
            return False
        if not self.node_clean(service):  # a removal that failed, or one teardown.sh refused: never shown clean
            self.say(f"{service}: its step was cancelled, and its work directory is still on its node")
            return False
        self.set(lambda s: s["services"][service].update(state="removed"))
        self.say(f"{service}: its step was cancelled, and the work directory it recorded is removed from its node")
        return True

    def node_clean(self, service: str) -> bool:
        """A read-only control step: the service's work directory is gone from its node. A service never launched is
        clean; one launched whose log never named a work directory is checked by `no_workdir_left`."""
        state = self.store.read()
        record = state["services"][service]
        if record["workdir"] is None:
            return record["step"] is None or self.no_workdir_left(service)
        name = f"{state['allocation']['name']}-{service}-check"
        try:
            done = self.launcher.control(
                record["node"], ["test", "!", "-e", record["workdir"]], name=name, timeout_s=self.timeouts["step"]
            )
        except TimeoutError:
            return False
        return done.returncode == 0

    def no_workdir_left(self, service: str) -> bool:
        """A service whose step was launched but whose log never named a work directory (seen live: a zone that failed
        in node-init before making one) is clean only when both are shown: its step has ended (listed no more, once
        it was seen listed or its srun client of this host has ended: a step never listed whose client still runs may
        yet start), and a read-only check on its node (`bundle/leftovers.py`) counts no work directory of ours of its
        kind there. A check that fails, or prints anything but such a count, is no proof."""
        state = self.store.read()
        record, site = state["services"][service], Site.from_json(state["site"])
        log = Path(record["log"]) if record.get("log") else None
        if log is None or not log.is_file() or WORKDIR_LINE.search(log.read_text(encoding="utf-8", errors="replace")):
            # its log is gone, or it named a directory the driver refused (outside the tmpfs, say): never shown clean
            self.say(f"{service}: its log cannot show that its step never made a work directory")
            return False
        name = record["step"]["name"]
        client = state.get("clients", {}).get(name)
        seen = record["step"].get("id") is not None
        recorded = Client(int(client["pid"]), int(client["start"])) if client is not None else None
        ended = recorded is not None and not self.slurm.runner.client_alive(recorded)
        if self.launcher.listed(name) is not None or not (seen or ended):
            self.say(f"{service}: its step named no work directory and its end cannot be shown")
            return False
        argv = [site.node_python, "-I", "-B", str(BUNDLE / "leftovers.py"), record["kind"], site.node_tmp]
        try:
            done = self.launcher.control(
                record["node"], argv, name=f"{state['allocation']['name']}-{service}-leftovers",
                timeout_s=self.timeouts["step"],
            )  # fmt: skip
        except TimeoutError:
            return False
        try:
            found = json.loads(done.stdout) if done.returncode == 0 else None
        except ValueError:
            found = None
        count = found.get("count") if isinstance(found, dict) and found.get("kind") == record["kind"] else None
        if isinstance(count, bool) or count != 0:
            self.say(f"{service}: its node's check found {count if isinstance(count, int) else 'no count of'} "
                     "work directories of ours")  # fmt: skip
            return False
        self.say(f"{service}: its step ended before naming a work directory, and its node holds none of ours")
        return True

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
