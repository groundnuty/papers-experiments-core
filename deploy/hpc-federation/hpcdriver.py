"""`up`, `down` and `describe` of the HPC federation, over one layout and one root (DESIGN.md, sections 1 to 6).

Every step goes through the shared launcher (`hpclaunch.Launcher`) and every change of the state file through its
store (`hpclaunch.StateStore`), written before the action it enables, so `down` finds whatever `up` reached.
"""

import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import hpcdata
import hpcexclude
import hpcidentity
import hpcimport
import hpclaunchproof
import hpcnames
import hpcpins
import hpcplacement
import hpcquota
import hpcrelay
import hpcstate
import hpcstorage
from fedlayout import ROOT_MARKER, Layout
from fedpki import private_directory, write_secret
from fedspec import Spec
from fedtools import check_artifacts, check_launchable
from hpcboot import core_phase, hpc_spec, hpc_values, load_owner, load_readings, verify_ingested
from hpcca import ca_certificates
from hpcdown import BUNDLE, WORKDIR_LINE, Down
from hpclaunch import Launcher, StateStore, client_log
from hpcsettings import HpcSettings, Site
from hpcslurm import READBACK, Allocation, Client, Shape, Slurm, SlurmError

HERE = Path(__file__).resolve().parent
CORE_MISMATCH = 97  # the exit status of a core step whose interpreter imports another core
TIMEOUTS = {
    "queue": 1800.0,  # the held job's start
    "service": 1800.0,  # a service's verdict, from its step's launch
    "step": 600.0,  # a control step: node facts, a token's mint and delivery, an export, a check
    "core": 5400.0,  # the core's phase as a step: the bootstrap, the baseline and the link check
    "cancel": 300.0,  # the held job leaving the queue after its cancel
}
VERDICT = {"oz": "hpcop: OK zone", "op": "hpcop: OK provider"}
FAILED = ("FINISH FAILED", "hpcop: ERROR")
TOKEN_WAIT = "waiting for the registration token"
FQDN = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
IPV4 = re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}")
PASSPHRASE = re.compile(r"[A-Za-z0-9]{16,128}")
GONE_POLLS = 3
STDERR_TAIL = 300  # the most of a step's stderr a message repeats
LAUNCH_STAGGER_S = 1.0  # declared bound: the pause between two provider launches, so five do not start in one second
LAUNCH_RETRY_BACKOFF_S = 60.0  # declared bound: the wait between a launch proved failed and its one retry (the live
# nodes resolved again "a minute later")


class CoreMismatch(RuntimeError):
    """The core an interpreter imports is not this checkout's."""


class StepGone(RuntimeError):
    """A service's step ended before its log said what `up` waited for: the one failure `await_start` weighs a retry
    for, and the same failure `up` has always raised (its message is unchanged)."""


def stderr_tail(text: str, limit: int = STDERR_TAIL) -> str:
    """What a failed step said on stderr, for a message: its last `limit` characters on one line (its lines joined by
    " | "), each character that does not print replaced; a step that said nothing is named so. Only for a step whose
    stderr carries no secret (the node facts' does not: nodefacts.py prints none, and srun's own errors name none)."""
    said = " | ".join(line.strip() for line in text.splitlines() if line.strip())[-limit:]
    said = "".join(c if c.isprintable() else "?" for c in said)
    return f"its stderr: {said}" if said else "nothing on its stderr"


def _say(line: str) -> None:
    print(f"hpcfed: {line}", file=sys.stderr, flush=True)


@dataclass
class Driver(Down):
    """up, down and describe over one layout and one root."""

    layout_path: Path
    layout: Layout
    settings: HpcSettings
    slurm: Slurm
    store: StateStore
    launcher: Launcher
    user: str
    command: List[str]  # this driver's argv prefix for this federation: what the fault relay runs a verb through
    say: Callable[[str], None] = _say
    core: Callable[..., str] = core_phase
    verify: Callable[..., Dict[str, str]] = verify_ingested  # verify-data's read of each ingested space (O57)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    poll_s: float = 5.0
    launch_stagger_s: float = LAUNCH_STAGGER_S  # recorded in the state file by `initial_state`
    launch_retry_backoff_s: float = LAUNCH_RETRY_BACKOFF_S  # recorded there too
    wall: Callable[[], float] = time.time  # the login node's wall clock, to read a step's End against (a stand-in's)
    launched_wall: Dict[str, float] = field(default_factory=dict)  # service -> the wall time of its last launch here
    core_paths: Dict[str, str] = field(default_factory=dict)
    relay_available: Callable[[], bool] = hpcrelay.available
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    timeouts: Dict[str, float] = field(default_factory=lambda: dict(TIMEOUTS))
    removal: Dict[str, Any] = field(default_factory=dict)  # the root's removal's file calls (hpcdown), for a stand-in

    @property
    def spec(self) -> Spec:
        return self.layout.spec

    @property
    def root(self) -> Path:
        return self.layout.root

    def set(self, change: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
        """Change the state under its lock; `change` edits the dict in place."""

        def apply(state: Dict[str, Any]) -> Dict[str, Any]:
            change(state)
            return state

        return self.store.update(apply)

    # -- describe ---------------------------------------------------------------------------------------------------

    def describe(self, weight: Optional[float] = None) -> str:
        roles = self.settings.roles(tuple(p.name for p in self.spec.providers))
        node, agent, s = self.settings.node(), self.settings.sandbox_node(), self.settings
        services = len(roles) - (1 if agent is not None else 0)
        lines = [
            f"layout: {self.spec.source} (project {self.spec.project}); root {self.root}",
            f"held job: {services} node(s) of {node.cpus} cores and {node.memory_mib} MiB, --time {s.time}, "
            f"--no-requeue; {services * node.cpus} cores and {services * node.memory_mib} MiB in all",
        ]
        if agent is not None:
            where = {
                "het": 'component 1 of the held job (a heterogeneous job: sandbox_placement "het")',
                "separate": 'a held job of its own (sandbox_placement "separate")',
            }[s.sandbox_placement]
            lines.append(f"the agent's node: 1 node of {agent.cpus} cores and {agent.memory_mib} MiB, {where}")
        for index, role in enumerate(roles):
            size = s.size(role.kind)
            lines.append(f"node {index}: {role.name} ({role.kind}), its step {size.cpus} cores, {size.memory_mib} MiB")
        lines.append(f"control reserve on every node: {s.control.cpus} core(s), {s.control.memory_mib} MiB")
        lines.append(f"stop bound {s.stop_bound_s} s; settle_timeout_s {s.settle_timeout_s} s; core phase: {s.core}")
        if weight is not None:
            billing = s.billing_per_hour(len(roles), weight)
            most = billing * s.time_s / 3600
            lines.append(f"billing: {billing:g} units per hour, idle or busy, until down; {most:g} at most for --time")
        return "\n".join(lines)

    # -- up: what is checked and recorded before the cluster is touched -----------------------------------------------

    def check_imports(self, site: Site) -> None:
        """The shared imported trees (O59, DESIGN.md section 8), all checked before the cluster is touched.

        Every import a provider owns must be a shared one (`shared_imports_of` refuses the rest: a writable one copies
        its tree into the root, and one not marked shared would leave the sharing out of the platform file the core
        checks). Each shared import's tree and manifest are read from this driver's environment and checked, and no
        manifest may hold more bytes than the support the layout gives its space, no tree may overlap another, and none
        may be, hold or contain the root or the log directory, which this driver writes. The core's step runs on a node
        whose environment is an allowlist that carries none of the variables, so a shared import needs `[hpc] core =
        "login"`. Every message names a space or a variable, never a path."""
        for provider in self.spec.providers:
            hpcimport.shared_imports_of(self.layout, provider.name)
        shared = self.layout.shared_imports()
        if not shared:
            return
        if self.settings.core != "login":
            raise RuntimeError(
                'a layout with a shared import needs [hpc] core = "login": the core\'s step runs on a node whose '
                "environment is an allowlist that carries neither the tree's variable nor the manifest's"
            )
        for space in shared:
            self.layout.import_source(space.name, self.environ)
            self.layout.import_manifest(space.name, self.environ)
        hpcimport.check_support(self.layout, self.environ)
        places = {"the federation's root": self.root, "the log directory (L)": Path(site.logs)}
        if (
            site.storage_root
        ):  # the provider storage's scratch root: `down` removes below it, and nothing may reach a tree
            places["the provider storage root (HPC_STORAGE_ROOT)"] = Path(site.storage_root)
        hpcimport.check_separate(self.layout, self.environ, places)

    def preflight(self, site: Site) -> None:
        self.settings.roles(tuple(p.name for p in self.spec.providers))  # a provider named like the agent's role
        self.check_imports(site)
        if self.layout.outer_roots or self.layout.inner_roots:
            raise RuntimeError("the root lies inside another federation's root, or holds one; refused")
        if self.relay_available() and hpcrelay.linked(self.root):  # refused now, not once the federation is up
            raise RuntimeError(
                "the root is reached through a symbolic link, where the fault relay does not start: name it by its "
                "resolved path"
            )
        if self.root.exists() and not self.layout.owned and any(self.root.iterdir()):
            raise RuntimeError(f"the root holds files and is not marked as this federation's ({ROOT_MARKER}); refused")
        if self.store.exists():
            raise RuntimeError(f"the root holds a {hpcstate.STATE}: the federation is up or half up; run down first")
        for name in ("onezone.tar", "oneprovider.tar"):
            for path in (Path(site.images) / name, Path(site.images) / f"{name}.provenance"):
                if not path.is_file():
                    raise RuntimeError(f"no {path.name} in SH: build the rootfs tarballs first (bundle README, step 0)")
        if not Path(site.logs).is_dir():
            raise RuntimeError("L is not a directory")
        if self.settings.core == "step" and not site.core_python:
            raise RuntimeError('[hpc] core = "step" needs HPC_CORE_PYTHON, the core\'s Python on the nodes')
        if self.settings.sandbox is not None and not site.core_python:  # the reaper of the sandbox's runs is the core's
            raise RuntimeError(
                "the agent's node needs HPC_CORE_PYTHON, the core's Python on the nodes: down's sweep of a node a "
                "shard step ran on reaps the sandbox's killed runs with the core's reaper"
            )
        template = self.spec.platform_template
        text = template.read_text(encoding="utf-8") if template is not None else None
        declared = [space.data_manifest for space in self.spec.spaces if space.data_manifest]
        hpcpins.check(site, text, declared)
        hpcplacement.check_template(
            text
        )  # a template declares [placement] holder (O79), which the core has no default for
        # O83: a template that names @study_artifacts@ has its tool server's files checked, and the frozen manifests
        check_launchable(self.spec, site.study_artifacts)  # the names resolve through the launcher here (O73)
        check_artifacts(self.spec, site.study_artifacts, self.root)
        # every declared manifest loads now, so a mistake in one fails before a job is held
        hpcdata.ingested_manifests(self.layout, hpcdata.study_values(site.study_artifacts))
        self.check_home(site)
        self.check_scratch(site)
        if self.settings.sandbox is not None:
            self.say(
                f"the layout declares the agent's node ({self.settings.sandbox_placement}): it is held and placed, and "
                "only its node facts step runs on it here (M5's shard step runs the agent)"
            )

    def check_home(self, site: Site) -> None:
        """The account's home guard (hpcquota.home_guard; DESIGN.md section 11): when the site sets its command or its
        pattern, the home's files and bytes are read and the federation refused when either is past 95% of its limit,
        or when they cannot be read, before anything is submitted; `HPC_HOME_QUOTA_CHECK=off` skips it, and `up` says
        so. It does not depend on a scratch root, and says nothing when no key is set."""
        said = hpcquota.home_guard(self.slurm.runner, site)
        if said:
            self.say(said)

    def check_scratch(self, site: Site) -> None:
        """The provider storage's scratch root, and the inode guard (hpcquota; DESIGN.md section 11): with a scratch
        root, the account's inode usage and quota are read through the site's command and the federation refused when
        its estimate would pass 95% of the quota, or when the quota cannot be read, before anything is submitted. The
        check is mandatory there: the providers' storage is bound onto scratch, and no override skips it. Nothing
        without a scratch root: nothing of the federation lands on scratch."""
        if not site.storage_root:
            return
        hpcstorage.check_root(site, self.root)
        self.say(hpcquota.guard(self.slurm.runner, site, self.layout, self.environ))

    def prepare_root(self) -> None:
        private_directory(self.root)
        if not self.layout.owned:
            self.layout.marker.write_text(self.spec.project + "\n", encoding="utf-8")
        private_directory(self.layout.secrets)
        private_directory(self.layout.users)
        self.layout.ca_pem.parent.mkdir(exist_ok=True)
        self.layout.journal.mkdir(exist_ok=True)

    def initial_state(self, site: Site, tree: Optional[str] = None, excluded: int = 0) -> Dict[str, Any]:
        """The state file as `up` first writes it. `tree` is the digest of the study's artifacts directory
        (`study_artifacts_tree`), kept under `hpcidentity.ARTIFACTS` so the core's phase, here or in a step on a node
        whose environment is an allowlist, is handed the digest `up` took and never reads the directory again; the
        key is left out when there is none. `excluded` is how many nodes of the account's other live federations the
        held job excludes (`other_federations_nodes`), a count and never a name."""
        roles = self.settings.roles(tuple(p.name for p in self.spec.providers))
        node = self.settings.node()
        name = f"{hpcexclude.JOB_PREFIX}{self.spec.project[:40]}-{secrets.token_hex(4)}"
        state = {
            "format": hpcstate.FORMAT,
            "project": self.spec.project,
            "layout": {"source": self.spec.source, "digest": self.spec.digest, "path": str(self.layout_path)},
            "core": dict(self.core_paths),
            "command": list(self.command),
            "site": site.to_json(),
            "allocation": {
                "name": name,
                "job": None,
                "nodes": [],
                "cpus_per_node": node.cpus,
                "memory_mib_per_node": node.memory_mib,
                "time": self.settings.time,
                "readback": None,
                "started": None,
                "ends": None,
                "excluded_nodes": excluded,
            },
            "sandbox": self.sandbox_record(name),
            "storage": hpcstorage.plan(site, name, [p.name for p in self.spec.providers]),
            "roles": [role.name for role in roles],
            "services": {role.name: hpcstate.new_service(role.kind) for role in roles},
            "clients": {},
            "launch": {
                "stagger_s": self.launch_stagger_s,
                "retry_backoff_s": self.launch_retry_backoff_s,
                "retry_limit": hpclaunchproof.RETRY_LIMIT,
            },
            "owner": None,
            # the spaces the study ingests into, each to be verified after its ingest (`verify_data`); plan reads it
            "data": {"ingested": hpcdata.ingested_record(self.layout, hpcdata.study_values(site.study_artifacts))},
            "phase": "preflight",
        }
        if tree is not None:
            state[hpcidentity.ARTIFACTS] = tree
        return state

    def sandbox_record(self, name: str) -> Optional[Dict[str, Any]]:
        """Where the agent's node is held (DESIGN.md, section 4), None without one: M5's shard driver reads `node`, and
        launches its step with `--jobid=<job>`, plus `--het-group=<het_group>` when that is not None. Its own job, under
        `separate`, is named apart (`<name>-sandbox`), so each job is found by its own exact name."""
        shape = self.settings.sandbox_node()
        if shape is None:
            return None
        het = self.settings.sandbox_placement == "het"
        return {
            "placement": self.settings.sandbox_placement,
            "name": name if het else f"{name}-sandbox",
            "job": None,
            "het_group": 1 if het else None,
            "component_job": None,  # het: the component's own id, under which squeue -s and sacct list its steps
            "node": None,
            "cpus_per_node": shape.cpus,
            "memory_mib_per_node": shape.memory_mib,
            "time": self.settings.time,
            "readback": None,
            "started": None,
            "ends": None,
        }

    def allocation(self, state: Mapping[str, Any], site: Site, exclude: Sequence[str] = ()) -> Allocation:
        """The held job: the service nodes, and the agent's node as its component 1 under `het`. `exclude` are the
        nodes of other federations' live jobs, which each of its components keeps off."""
        node, agent = self.settings.node(), self.settings.sandbox_node()
        het = agent is not None and self.settings.sandbox_placement == "het"
        return Allocation(
            name=state["allocation"]["name"],
            account=site.account,
            partition=site.partition,
            nodes=sum(1 for role in state["roles"] if state["services"][role]["kind"] != "sandbox"),
            cpus_per_node=node.cpus,
            memory_mib=node.memory_mib,
            time=self.settings.time,
            time_s=self.settings.time_s,
            het=(Shape(1, agent.cpus, agent.memory_mib),) if het and agent is not None else (),
            exclude=tuple(exclude),
        )

    def sandbox_allocation(
        self, state: Mapping[str, Any], site: Site, exclude: Sequence[str] = ()
    ) -> Optional[Allocation]:
        """The agent's own held job under `separate` (one node of its own shape), else None. It keeps off the same
        nodes as the services' job: an agent's sandbox never runs on a node that hosts an Onedata service."""
        record, agent = state.get("sandbox"), self.settings.sandbox_node()
        if record is None or agent is None or record["placement"] != "separate":
            return None
        return Allocation(
            name=record["name"],
            account=site.account,
            partition=site.partition,
            nodes=1,
            cpus_per_node=agent.cpus,
            memory_mib=agent.memory_mib,
            time=self.settings.time,
            time_s=self.settings.time_s,
            exclude=tuple(exclude),
        )

    # -- up -----------------------------------------------------------------------------------------------------------

    def up(self, site: Site) -> str:
        self.preflight(site)
        tree = self.study_artifacts_tree(site)
        self.prepare_root()
        with self.store.lifecycle("up"):  # a down cannot run between the state and the sbatch
            return self.bring_up(site, tree)

    def study_artifacts_tree(self, site: Site) -> Optional[str]:
        """The digest of the study's artifacts directory (`HPC_STUDY_ARTIFACTS`, O74), taken here, on the login node
        and before anything is made or submitted, so a tree that cannot be digested (a special file, a directory that
        is not one, a link whose target leaves the directory: it must be self-contained) fails `up` at once, naming
        the variable and a path relative to the directory, never an absolute one. None when the site declares no
        directory, or the layout names no platform template, which writes no render identity to hold it. The tree is
        only read: it may be read-only."""
        if not site.study_artifacts:
            return None
        if self.spec.platform_template is None:
            self.say(
                "HPC_STUDY_ARTIFACTS is set and the layout names no platform template: it writes no render identity"
            )
            return None
        try:
            digest = hpcidentity.tree_sha256(Path(site.study_artifacts))
        except hpcidentity.TreeError as error:
            raise RuntimeError(f"HPC_STUDY_ARTIFACTS: {error}") from None
        self.say("the study's artifacts directory (HPC_STUDY_ARTIFACTS) is digested for the render identity")
        return digest

    def bring_up(self, site: Site, tree: Optional[str] = None) -> str:
        excluded = self.other_federations_nodes()  # before anything is made: an unreadable read refuses here
        state = self.store.create(self.initial_state(site, tree, len(excluded)))
        self.make_storage(state)  # recorded above, made before any job exists to bill for it
        asked, alone = self.allocation(state, site, excluded), self.sandbox_allocation(state, site, excluded)
        held = [(self.submit(asked, "allocation"), asked)]
        if alone is not None:  # sandbox_placement = "separate": the agent's own held job, exactly as the first
            held.append((self.submit(alone, "sandbox"), alone))
        for job, wanted in held:
            self.slurm.wait_running(job, self.timeouts["queue"], name=wanted.name, note=self.queue_note(wanted))
        # Once more on the running jobs: the scheduler may allocate more than a pending request showed.
        records = [(job, shape, record) for job, wanted in held for shape, record in zip(
            wanted.shapes(), self.slurm.checked(job, wanted))]  # fmt: skip
        placed = self.placed_nodes(records)
        self.keep_off_other_federations([job for job, _ in held], placed)
        self.set(lambda s: self.place(s, placed, [record for _, _, record in records]))
        self.node_facts()
        self.zone_up()
        self.providers_up()
        self.export_zone()
        out = self.run_core()
        self.set(lambda s: s.update(phase="linked"))
        retried = self.retried()
        return f"{out}\n{retried}\n{self.start_relay()}" if retried else f"{out}\n{self.start_relay()}"

    def other_federations_nodes(self) -> List[str]:
        """The nodes of every live job of the account that carries the federation prefix (`hpcexclude`, DESIGN.md
        section 1), read before the held job is submitted, to be excluded from it: two federations' services of one
        account on one node would share Couchbase's fixed port. A read that fails refuses, and nothing is submitted:
        the held job never goes without the exclusion. The line says how many, never which."""
        try:
            nodes = hpcexclude.nodes_of(hpcexclude.live_jobs(self.slurm))
        except SlurmError as error:
            raise SlurmError(f"{error}; refusing to submit without keeping off the other federations' nodes") from None
        if nodes:
            self.say(f"{len(nodes)} node(s) of the account's other live federation jobs are excluded from the held job")
        else:
            self.say("no other live federation job of the account holds a node: nothing is excluded")
        return nodes

    @staticmethod
    def queue_note(wanted: Allocation) -> str:
        """What the queue bound's failure adds for a job that excluded nodes: Slurm keeps a job pending when what is
        left does not fit it, and the bound is the one `up` always had."""
        if not wanted.exclude:
            return ""
        return (
            f" (it excluded {len(wanted.exclude)} node(s) of other live federations of this account: Slurm may be "
            "keeping it pending for lack of others)"
        )

    def keep_off_other_federations(self, jobs: Sequence[str], placed: Sequence[Sequence[str]]) -> None:
        """Once the held jobs run and before any step, none of this federation's nodes may hold another live
        federation's job (a service's, or an agent's: an agent never runs beside a service): two ups that read the
        account's jobs at once both excluded nothing. One that does is refused, every held job of this up cancelled
        by its own id, and the roles named (the zone, providers[i], the agent), never a host. `placed` is each
        component's nodes, the services' first. Fail closed: with the jobs unreadable nothing proves the nodes are
        this federation's own, and the same release follows."""
        try:
            others = set(hpcexclude.nodes_of(hpcexclude.live_jobs(self.slurm), own=jobs))
        except SlurmError as error:
            raise SlurmError(f"{error}; {self.release(jobs)}") from None
        held = [(hpcexclude.role(index), node) for index, node in enumerate(placed[0])]
        held += [(hpcexclude.AGENT, node) for nodes in placed[1:] for node in nodes]
        shared = [role for role, node in held if node in others]
        if shared:
            raise SlurmError(
                f"{', '.join(shared)} would run on a node another live federation of this account holds, and two "
                f"federations never share one; no service started, and {self.release(jobs)}"
            )

    def release(self, jobs: Sequence[str]) -> str:
        """Cancel each held job of this up by its own id, and say so; a cancel that fails is said, and `down` finishes
        it."""
        failed = []
        for job in jobs:
            try:
                self.slurm.cancel(job)
            except SlurmError:
                failed.append(job)
        if failed:
            return f"cancelling job(s) {', '.join(failed)} failed: run down to release them"
        return f"the held job(s) {', '.join(jobs)} were cancelled"

    def make_storage(self, state: Mapping[str, Any]) -> None:
        """The providers' storage directories on scratch, as the state records them (hpcstorage.create); none without a
        scratch root. `down` removes exactly these, and only these."""
        record = state.get("storage")
        if record is None:
            return
        hpcstorage.create(record)
        made = len(record["providers"])
        self.say(f"provider storage: {made} director(ies) made on scratch and recorded; down removes them")

    def start_relay(self) -> str:
        """The login node's fault relay, for a campaign on a compute node (M4); none in a core without it."""
        if not self.relay_available():
            return f"fault relay: none, this core has no {hpcrelay.RELAY}"
        state = self.store.read()
        log = Path(state["site"]["logs"]) / f"{state['allocation']['name']}-fault-relay.log"

        def record(client: Client) -> None:
            self.set(lambda s: s.update(relay={"pid": client.pid, "start": client.start, "log": str(log)}))

        hpcrelay.start(
            self.slurm.runner, state["command"][0], self.root, state["command"], log, self.environ,
            record=record, clock=self.clock, sleep=self.sleep,
        )  # fmt: skip
        return "fault relay: serving"

    def submit(self, asked: Allocation, key: str) -> str:
        """Submit one held job (`key`: "allocation", the services' job, or "sandbox", the agent's own); its id is
        recorded as soon as sbatch prints it, before the read-back, which cancels that one job unless Slurm recorded
        every component as asked."""
        job = self.slurm.submit(asked)

        def submitted(state: Dict[str, Any]) -> None:
            state[key]["job"] = job
            if asked.het:  # the agent's node is this job's component 1
                state["sandbox"]["job"] = job
            state["phase"] = "submitted"

        self.set(submitted)
        self.say(f"held job {job} submitted ({sum(s.nodes for s in asked.shapes())} nodes); reading back what Slurm "
                 "recorded")  # fmt: skip
        records = self.slurm.checked(job, asked)
        self.set(lambda s: self.record_readback(s, key, records))
        return job

    @staticmethod
    def record_readback(state: Dict[str, Any], key: str, records: Sequence[Mapping[str, str]]) -> None:
        state[key]["readback"] = {field: records[0][field] for field in READBACK}
        if len(records) > 1:  # the heterogeneous job's component 1: the agent's node, with its own job id
            readback = {field: records[1][field] for field in READBACK}
            state["sandbox"].update(readback=readback, component_job=records[1]["JobId"])

    def placed_nodes(self, records: Sequence[Any]) -> List[List[str]]:
        """Each component's nodes, the services' first and the agent's last, as Slurm placed them. A component on
        another count of nodes than asked, or on a node another holds (no node runs two services, nor the agent beside
        one), fails up, its job cancelled by its id before any step."""
        found: List[List[str]] = []
        seen: set = set()
        for job, shape, record in records:
            nodes = self.slurm.hostnames(record.get("NodeList", ""))
            twice = sorted((set(nodes) & seen) | {n for n in nodes if nodes.count(n) > 1})
            if len(nodes) != shape.nodes or twice:
                self.slurm.cancel(job)
                why = f"on {', '.join(twice)}, held twice" if twice else f"on {len(nodes)} node(s)"
                raise SlurmError(f"job {job} was placed {why}, not on the {shape.nodes} asked for; it was cancelled")
            seen.update(nodes)
            found.append(nodes)
        return found

    def place(self, state: Dict[str, Any], placed: List[List[str]], records: Sequence[Mapping[str, str]]) -> None:
        """Each role on its node: the services on the held job's (component 0's) in the layout's order, the agent's
        node on its own component's or job's; the read-back of each, taken on the running jobs."""
        roles = [role for role in state["roles"] if state["services"][role]["kind"] != "sandbox"]
        first = records[0]
        state["allocation"].update(nodes=placed[0], started=first.get("StartTime"), ends=first.get("EndTime"))
        state["allocation"]["readback"] = {field: first[field] for field in READBACK}
        for role, node in zip(roles, placed[0]):
            state["services"][role]["node"] = node
        if state.get("sandbox") is not None:
            node, agent = placed[1][0], records[1]
            readback = {field: agent[field] for field in READBACK}
            state["sandbox"].update(node=node, readback=readback, started=agent.get("StartTime"))
            state["sandbox"]["ends"] = agent.get("EndTime")
            state["services"]["sandbox"]["node"] = node
        state["phase"] = "placed"

    def node_facts(self) -> None:
        """Each node's FQDN, its IPv4 address and the job's uid and gid, from a control step on it, and its OS release,
        glibc and kernel (`readings`, O87: recorded only, a node that gives none is unknown, never refused); then each
        service's declared name, and the address map the login node's clients dial them through
        (`write_address_map`)."""
        state = self.store.read()
        site, prefix = Site.from_json(state["site"]), state["allocation"]["name"]
        declared = hpcnames.declared_names(self.spec)
        for role in state["roles"]:
            argv = [site.node_python, "-I", "-B", str(BUNDLE / "nodefacts.py")]
            node = state["services"][role]["node"]
            done = self.launcher.control(node, argv, name=f"{prefix}-{role}-facts", timeout_s=self.timeouts["step"])
            try:
                owner = load_owner(done.stdout) if done.returncode == 0 else None
            except (ValueError, AttributeError):  # not a JSON object: refused below, with what the step said
                owner = None
            facts = json.loads(done.stdout) if owner else {}
            if not FQDN.fullmatch(str(facts.get("fqdn"))) or not IPV4.fullmatch(str(facts.get("address"))):
                raise RuntimeError(
                    f"the node facts of {role} are not a name, an IPv4 address, a uid and a gid (the step's status "
                    f"{done.returncode}; {stderr_tail(done.stderr)})"
                )
            readings = load_readings(facts.get("readings"))  # O87: recorded, never refused: unknown, with why
            self.set(
                lambda s, r=role, f=facts, o=owner, g=readings: (
                    s["services"][r].update(fqdn=f["fqdn"], address=f["address"], declared=declared.get(r), readings=g),
                    s.update(owner=list(o)),
                )
            )
        self.write_address_map()

    def write_address_map(self) -> None:
        """The map from each declared name to its node's address and the node's own names, into the root's secrets
        (0600), before any service is launched or any client of the login node dials one (O73). It is the root's own
        file, never in a cell, the gate's file or the facts. A service without an address stops up here."""
        state = self.store.read()
        found = hpcnames.address_map(hpcnames.declared_names(self.spec), state["services"])
        found.write(self.layout.address_map)
        hpcnames.install_toolshim(self.layout.toolshim)

    def host_args(self, state: Mapping[str, Any]) -> List[str]:
        """The `--host` arguments every service is launched with: each service's declared name and its node's address,
        so its hosts file resolves every peer. A service the state has no address for stops up."""
        return hpcnames.host_args(hpcnames.address_map(hpcnames.declared_names(self.spec), state["services"]))

    def launch(self, service: str, argv: Sequence[str]) -> None:
        """Launch a service step; its name, its log and its launch count are recorded first."""
        state = self.store.read()
        launch = state["services"][service]["launch"] + 1
        name = f"{state['allocation']['name']}-{service}-run-{launch}"
        log = str(Path(state["site"]["logs"]) / f"{name}.log")
        step = {"name": name, "id": None}
        self.launched_wall[service] = self.wall()
        self.set(lambda s: s["services"][service].update(launch=launch, step=step, log=log, state="starting"))
        record = state["services"][service]
        self.launcher.service(record["node"], argv, kind=record["kind"], name=name, log=log)

    def wait_log(self, service: str, wanted: str) -> str:
        """The service's log once it holds `wanted`; fail on a failure line, when its step (or its client, before the
        step was ever listed) is gone, or at the bound."""
        state = self.store.read()
        record = state["services"][service]
        name, log = record["step"]["name"], Path(record["log"])
        client = state.get("clients", {}).get(name)
        deadline, seen, absent = self.clock() + self.timeouts["service"], False, 0
        while True:
            text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
            if wanted in text:
                return text
            failed = [line for line in FAILED if line in text]
            if failed:
                self.set(lambda s: s["services"][service].update(state="failed"))
                raise RuntimeError(f"{service} failed to start ({failed[0]} in its log)")
            listed = self.launcher.listed(name)
            if listed is not None and not seen:
                seen = True
                self.set(lambda s, i=listed: s["services"][service]["step"].update(id=i))
            dead = client is not None and not self.slurm.runner.client_alive(Client(client["pid"], client["start"]))
            absent = absent + 1 if listed is None and (seen or dead) else 0
            if absent >= GONE_POLLS:
                self.set(lambda s: s["services"][service].update(state="failed"))
                raise StepGone(f"{service}'s step ended before its log said '{wanted}'")
            if self.clock() >= deadline:
                raise RuntimeError(f"{service} did not say '{wanted}' within {self.timeouts['service']:.0f} s")
            self.sleep(self.poll_s)

    def await_start(self, service: str, wanted: str, argv: Sequence[str]) -> str:
        """The service's log once it holds `wanted` (`wait_log`), after a launch that failed at launch is repeated ONCE.

        The only failure considered is `StepGone`: the step ended before its log said anything it waited for. It is
        launched again only when `launch_proof` shows, from three sources read now, that nothing of it ran (both
        proofs, `hpclaunchproof`). Then what is left of `launch_retry_backoff_s` passes, counted from the step's own End
        (the cause was transient: live, the nodes resolved again a minute later; `backoff`), the run log is read once
        more, and only if it is still empty is the retry written to the state, with its proofs, and the service
        launched. Every other failure, a second failure of the
        same service, and a deadline that ran out (the step may be running) are raised as they always were."""
        try:
            return self.wait_log(service, wanted)
        except StepGone:
            retry = self.launch_proof(service)
            if retry is None:
                raise
            retry["waited_s"] = self.backoff(service, retry)
            if not self.record_retry(service, retry):
                raise
        self.launch(service, argv)
        return self.wait_log(service, wanted)

    def backoff(self, service: str, retry: Dict[str, Any]) -> float:
        """Wait out what is left of `launch_retry_backoff_s` counted from the step's own End, read from the sacct row
        of the proofs; the whole of it from now when that End is unreadable or not to be believed (`failure_time`).
        Providers that failed together then wait one backoff in all, not one each. Returns the seconds waited."""
        now = self.wall()
        failed_at = hpclaunchproof.failure_time(retry["step_end"], self.launched_wall.get(service), now)
        wait = hpclaunchproof.backoff_wait(self.launch_retry_backoff_s, failed_at, now)
        how = "from its End" if failed_at is not None else "from now: its End is not to be read"
        self.say(f"{service}: its launch failed before anything ran; waiting {wait:g} s ({how})")
        if wait > 0:
            self.sleep(wait)
        return wait

    def launch_proof(self, service: str) -> Optional[Dict[str, Any]]:
        """The proofs that this service's last launch failed before anything of it ran, or None. Never a second
        retry: that is read from the state file, which outlives this process. The run log is read last."""
        record = self.store.read()["services"][service]
        if len(record.get("launch_retries") or []) >= hpclaunchproof.RETRY_LIMIT or not record["step"]:
            return None
        name, log = record["step"]["name"], str(record["log"])
        client = hpclaunchproof.read_client_log(Path(client_log(log)))
        try:
            rows = self.launcher.accounted_steps(record["node"], name)
        except hpcstate.StateError:
            rows = None
        found = hpclaunchproof.prove(
            name, record["step"].get("id"), client, rows, hpclaunchproof.run_log_size(Path(log))
        )
        return None if found is None else {"launch": record["launch"], **found}

    def record_retry(self, service: str, retry: Dict[str, Any]) -> bool:
        """Write the retry and its proofs to the state, once the run log is seen empty again (proof (b), after the
        backoff and immediately before the launch); False (nothing written) when it holds a byte now, or is gone."""
        log = Path(self.store.read()["services"][service]["log"])
        if hpclaunchproof.run_log_size(log) != 0:
            self.say(f"{service}: its run log is not empty now; not launched again")
            return False
        self.set(lambda s: s["services"][service].setdefault("launch_retries", []).append(retry))
        self.say(hpclaunchproof.evidence(service, retry) + "; launching it once more")
        return True

    def retried(self) -> str:
        """`up`'s evidence of its retries: one line per retry, as the state records it; nothing when there was none."""
        services = self.store.read()["services"]
        return "\n".join(
            hpclaunchproof.evidence(name, entry)
            for name, record in services.items()
            for entry in record.get("launch_retries") or []
        )

    def record_workdir(self, service: str, text: str) -> str:
        state = self.store.read()
        found = WORKDIR_LINE.findall(text)
        if not found:
            raise RuntimeError(f"{service}'s log names no work directory")
        kind = state["services"][service]["kind"]
        workdir = hpcstate.checked_workdir(found[0], state["site"]["node_tmp"], kind)
        self.set(lambda s: s["services"][service].update(workdir=workdir, rootfs=f"{workdir}/rootfs"))
        return workdir

    def zone_up(self) -> None:
        """The zone, its peers every provider's address (the shim reads them once, before the service starts)."""
        state = self.store.read()
        site, services = Site.from_json(state["site"]), state["services"]
        tar = str(Path(site.images) / "onezone.tar")
        peers = [services[p.name]["address"] for p in self.spec.providers]
        argv = ["bash", str(BUNDLE / "zone.sbatch"), str(BUNDLE), tar, self.spec.project[:40], *peers]
        # the one command, with its O73 names, that is launched and, if it fails at launch, launched again (O57)
        launched = [*argv, f"--declared={services['zone']['declared']}", *self.host_args(state)]
        self.launch("zone", launched)
        self.say("zone launched; waiting for its verdict")
        self.record_workdir("zone", self.await_start("zone", VERDICT["oz"], launched))
        self.set(lambda s: (s["services"]["zone"].update(state="running"), s.update(phase="zone")))

    def providers_up(self) -> None:
        """Each provider, its peers the zone and every other provider; each registered by a token piped in."""
        state = self.store.read()
        site, services = Site.from_json(state["site"]), state["services"]
        zone, tar = services["zone"], str(Path(site.images) / "oneprovider.tar")
        names = [p.name for p in self.spec.providers]
        argvs: Dict[str, List[str]] = {}
        for index, name in enumerate(names):
            peers = [services[p]["address"] for p in names if p != name]
            argv = ["bash", str(BUNDLE / "provider.sbatch"), str(BUNDLE), tar, name, zone["declared"], site.admin_mail]
            # --import=SPACE=PATH for each tree it owns, none for a mirror
            imports = hpcimport.provider_import_args(self.layout, name, self.environ)
            # O73: the name it registers under, and every service's name with its address, for its hosts file
            naming = [f"--declared={services[name]['declared']}", *self.host_args(state)]
            # --storage=PATH, its own directory on scratch, bound onto /volumes/posix; none without a scratch root
            storage = hpcstorage.provider_args(state, name)
            # the one command a provider is launched with and, if it fails at launch, launched again with (O57)
            argvs[name] = [*argv, zone["address"], *peers, *naming, *imports, *storage]
            if (
                index and self.launch_stagger_s > 0
            ):  # between two launches: five srun fan-outs in one second failed live
                self.sleep(self.launch_stagger_s)
            self.launch(name, argvs[name])
        self.say(f"{len(names)} provider(s) launched, {self.launch_stagger_s:g} s apart; registering each")
        for name in names:
            self.register(name, self.record_workdir(name, self.await_start(name, TOKEN_WAIT, argvs[name])))
        for name in names:
            self.wait_log(name, VERDICT["op"])
            self.set(lambda s, n=name: s["services"][n].update(state="running"))
            self.say(f"{name} is up")
        for name in names:
            self.check_mounts(name)
        self.set(lambda s: s.update(phase="providers"))

    def check_mounts(self, provider: str) -> None:
        """For an owner of shared imported trees, the proof that each is read-only (O59): a control step on its node
        that reads the mounts its service holds and exits 0 only when every listed tree, and every mount below it, is
        held with `ro`. Nothing is written to a tree to find out. The message names the provider and the spaces, and
        repeats only the check's own FAIL lines, which name a space and never a path."""
        owned = hpcimport.shared_imports_of(self.layout, provider)
        if not owned:
            return
        state = self.store.read()
        record, site = state["services"][provider], Site.from_json(state["site"])
        argv = hpcimport.mount_check_argv(str(BUNDLE), record["workdir"], python=site.node_python)
        name = f"{state['allocation']['name']}-{provider}-mounts"
        done = self.launcher.control(record["node"], argv, name=name, timeout_s=self.timeouts["step"])
        spaces = ", ".join(space.name for space in owned)
        if done.returncode != 0:
            failed = [line for line in done.stderr.splitlines() if line.startswith("FAIL: shared import ")]
            raise RuntimeError(
                f"{provider}'s shared imported tree(s) of {spaces} are not proven read-only (status {done.returncode})"
                + "".join(f"; {line}" for line in failed)
            )
        # the script exits 0 for a work directory with no list of imports: read its report against what was asked
        problems = hpcimport.mount_check_problems(done.stdout, [space.name for space in owned])
        if problems:
            raise RuntimeError(
                f"{provider}'s shared imported tree(s) of {spaces} are not proven read-only: " + "; ".join(problems)
            )
        self.set(lambda s: s["services"][provider].update(mounts_proven=True))
        self.say(f"{provider}: the shared imported tree(s) of {spaces} are held read-only")

    def require_mount_proofs(self, state: Mapping[str, Any]) -> None:
        """The core's phase starts a scan of every shared tree, so it never starts for an owner whose tree this up has
        not proved read-only (`hpcfed.py core` run by hand over a partial up, for one)."""
        for provider in self.spec.providers:
            if hpcimport.shared_imports_of(self.layout, provider.name) and not state["services"][provider.name].get(
                "mounts_proven"
            ):
                raise RuntimeError(
                    f"{provider.name}'s shared imported tree(s) have no read-only proof recorded by this up: "
                    "the core's phase does not start"
                )

    def register(self, provider: str, workdir: str) -> None:
        """A one-hour single-use registration token, minted on the zone's node and piped into the provider's delivery
        step, which runs in the control reserve (a live run's delivery was killed under 100 MB)."""
        state = self.store.read()
        zone, node, prefix = state["services"]["zone"], state["services"][provider]["node"], state["allocation"]["name"]
        mint = ["bash", str(BUNDLE / "mint-registration-token.sh"), provider, "--workdir", zone["workdir"]]
        deliver = ["bash", str(BUNDLE / "deliver-token.sh"), "--workdir", workdir]
        left, right = self.launcher.control_pipe(
            (zone["node"], mint, f"{prefix}-{provider}-mint"),
            (node, deliver, f"{prefix}-{provider}-deliver"),
            self.timeouts["step"],
        )
        if left.returncode != 0 or right.returncode != 0:
            raise RuntimeError(
                f"{provider}'s registration failed (mint {left.returncode}, delivery {right.returncode})"
            )

    def export_zone(self) -> None:
        """The zone's passphrase into the root's secrets (0600), where the Compose root holds it, and its CA: the
        certificates alone (`hpcca`), since the image's CA file holds the CA's private key before its certificate."""
        state = self.store.read()
        zone, prefix = state["services"]["zone"], state["allocation"]["name"]

        def export(what: str) -> str:
            argv = ["bash", str(BUNDLE / "zone-export.sh"), what, "--workdir", zone["workdir"]]
            name = f"{prefix}-zone-{what}"
            done = self.launcher.control(zone["node"], argv, name=name, timeout_s=self.timeouts["step"])
            if done.returncode != 0:
                raise RuntimeError(f"the zone's {what} could not be exported (status {done.returncode})")
            return done.stdout

        passphrase = export("passphrase").strip()
        if not PASSPHRASE.fullmatch(passphrase):
            raise RuntimeError("the zone's exported passphrase is not one line of letters and digits")
        write_secret(self.layout.passphrase(self.spec.zone.name), passphrase)
        self.layout.ca_pem.write_text(ca_certificates(export("ca")), encoding="utf-8")

    def placed_layout(self, state: Mapping[str, Any]) -> Layout:
        """The layout with every service at its declared name (O73): its URLs, and the facts and the platform file built
        from them, hold the name the service registered under, never its node's. The root's address map says where each
        is dialled."""
        hosts = {
            name: record["declared"] for name, record in state["services"].items() if record["kind"] in ("oz", "op")
        }
        return Layout(self.root, self.layout.parent, hpc_spec(self.spec, hosts))

    def move_phase(self, was: str, to: str) -> bool:
        """Set the state's phase to `to` only if it is still `was`, under the state's lock; whether it was. A `down`
        that wrote `stopping` meanwhile is never overwritten."""
        moved: List[bool] = []

        def change(state: Dict[str, Any]) -> None:
            if state.get("phase") == was:
                state["phase"] = to
                moved.append(True)

        self.set(change)
        return bool(moved)

    def recorded_manifests(self, state: Mapping[str, Any], recorded: Mapping[str, Any]) -> Dict[str, Any]:
        """The manifests of the layout's ingested spaces, once they are the ones `up` recorded (`recorded`, the state's
        `data.ingested`): the same spaces, each manifest the digest `up` took; RuntimeError naming the space if not."""
        values = hpcdata.study_values(Site.from_json(state["site"]).study_artifacts)
        now = hpcdata.ingested(self.layout)
        if set(now) != set(recorded):
            raise RuntimeError("the layout's ingested spaces are not the ones this up recorded: it changed since up")
        manifests = hpcdata.ingested_manifests(self.layout, values)
        for space, manifest in manifests.items():
            if manifest.digest() != recorded[space]["manifest"]:
                raise RuntimeError(f"{space}: its manifest is not the one up recorded (its file changed since up)")
        return manifests

    def relink_refusal(self, state: Mapping[str, Any]) -> Optional[str]:
        """Why a rerun on a `linked` federation cannot say `linked` again, whatever its own checks find, or None. The
        rerun never reads an ingested space, and `shards.py plan` takes the markers `verify-data` left as read. So a
        rerun that found no facts file (the core's phase then resets every space's content, an ingested one's too), or
        one over a layout whose ingested spaces or manifests are not what `up` recorded, does not vouch for them."""
        if not self.layout.facts.exists():
            return "its facts file was missing, so the core's phase reset every space's content"
        recorded = (state.get("data") or {}).get("ingested")
        if recorded is None:
            return "its state holds no record of the ingested spaces (its up predates it)"
        try:
            self.recorded_manifests(state, recorded)
        except RuntimeError as error:
            return str(error)
        return None

    def core_here(self) -> str:
        """The core's phase in this process: the bootstrap, the baseline, the facts, platform.toml, the link check.
        It holds the root's core-phase lock (`StateStore.core_phase`) from its first read of the state to its last write
        of the phase: a second one is refused while it runs.

        On a federation that is `linked` (`hpcfed.py core` run again on a finished root) the phase is `bootstrapped`
        from before the core's phase runs, and `linked` again only if it returns, which it does only when its link
        check passed: `up`'s own condition for `linked`, and `relink_refusal` finds nothing against it. A rerun that
        fails, or dies, leaves `bootstrapped`, which `shards.py plan` refuses, and no `linked` it did not earn. On any
        other state the phase ends `bootstrapped` and only `up` says `linked`. Either way a phase another command
        wrote meanwhile (a `down`'s `stopping`) is never overwritten."""
        with self.store.core_phase():
            return self.core_phase_locked()

    def core_phase_locked(self) -> str:
        """`core_here` with the core-phase lock held."""
        state = self.store.read()
        owner = tuple(state["owner"] or ())
        if len(owner) != 2:
            raise RuntimeError("the state names no owner of the providers' storage: the node facts did not run")
        self.require_mount_proofs(state)
        s, site = self.settings, Site.from_json(state["site"])
        # the agent's sandbox's values, as up recorded the site (its files' pins checked in the preflight)
        values = {**hpc_values(state["command"], s.stop_bound_s, s.settle_timeout_s, self.root), **hpcpins.values(site)}
        # the study's artifacts digest, as up took it: a step on a node has no site variable and never reads the tree
        if state.get(hpcidentity.ARTIFACTS) is not None:
            values[hpcidentity.ARTIFACTS] = state[hpcidentity.ARTIFACTS]
        placed = self.placed_layout(state)
        before = state["phase"]
        was_linked = before == "linked"
        why = self.relink_refusal(state) if was_linked else None  # before the core's phase changes what it reads
        relink = was_linked and self.move_phase("linked", "bootstrapped")
        if relink:
            self.say("the federation was linked: it is bootstrapped unless this core phase's link check passes")
        try:
            out = self.core(placed, owner, values, environ=self.environ)
        except Exception:
            if relink:
                self.say("the core's phase failed: the federation stays bootstrapped, which shards plan refuses")
            raise
        if not was_linked:
            self.move_phase(before, "bootstrapped")  # a `down`'s `stopping`, written meanwhile, is kept
        elif relink and why is not None:
            self.say(f"the core's phase passed, but the federation stays bootstrapped: {why}")
        elif relink and self.move_phase("bootstrapped", "linked"):
            self.say("the core's phase and its link check passed: the federation is linked again")
        return out

    def verify_data(self) -> str:
        """After the study's ingest, show that each ingested space holds exactly the data its manifest declares (O57,
        C06): every file's path, size and SHA-256, and every directory, read through the space's owner and compared with
        the manifest `up` recorded. A pass is recorded in the state file as a marker bound to the manifest's digest,
        which `shards.py plan` requires; a space that differs fails loudly, naming it and the counts, and has no marker
        (one an earlier pass left is cleared). A federation whose `up` has not finished is refused."""
        state = self.store.read()
        if state.get("phase") != "linked":
            raise RuntimeError(f"the federation's up has not finished (phase {state.get('phase')}): nothing to verify")
        recorded = (state.get("data") or {}).get("ingested")
        if recorded is None:
            raise RuntimeError("the state holds no record of the ingested spaces (its up predates it): down, then up")
        # a verification that fails, or dies mid-read, leaves no marker an earlier pass gave
        self.set(lambda s: [record.update(verified=None) for record in s["data"]["ingested"].values()])
        manifests = self.recorded_manifests(state, recorded)
        if not manifests:
            return "data: no ingested space: nothing to verify"
        owner = tuple(state["owner"] or ())
        if len(owner) != 2:
            raise RuntimeError("the state names no owner of the providers' storage: the node facts did not run")
        found = self.verify(self.placed_layout(state), owner, manifests)
        passed = {space: manifests[space] for space, said in found.items() if not said}

        def mark(state: Dict[str, Any]) -> None:
            for space, record in state["data"]["ingested"].items():
                manifest = passed.get(space)
                record["verified"] = None if manifest is None else {
                    "manifest": manifest.digest(), "files": len(manifest.files), "bytes": manifest.bytes}  # fmt: skip

        self.set(mark)
        failed = [hpcdata.failure(space, said, recorded[space]["declared"]) for space, said in found.items() if said]
        if failed:
            raise RuntimeError("; ".join(failed) + ": not verified")
        files = sum(len(m.files) for m in manifests.values())
        return (
            f"data: every ingested space holds the data its manifest declares ({len(manifests)} space(s), "
            f"{files} file(s))"
        )

    def run_core(self) -> str:
        """The core's phase where `[hpc] core` says: here, or in a control step on the zone's node."""
        if self.settings.core == "login":
            return self.core_here()
        state = self.store.read()
        zone, site = state["services"]["zone"], Site.from_json(state["site"])
        argv = [site.core_python, "-I", "-B", str(HERE / "hpcfed.py"), "--layout", str(self.layout_path)]
        argv += ["--root", str(self.root), "core"]
        name = f"{state['allocation']['name']}-core"
        done = self.launcher.control(zone["node"], argv, name=name, timeout_s=self.timeouts["core"])
        if done.returncode == CORE_MISMATCH:
            raise CoreMismatch("the core's step imports a core that is not this checkout's")
        if done.returncode != 0:
            raise RuntimeError(f"the core's step failed with status {done.returncode}: {done.stderr.strip()[-600:]}")
        return done.stdout.strip()
