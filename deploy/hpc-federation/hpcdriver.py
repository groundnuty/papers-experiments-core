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

import hpcimport
import hpcpins
import hpcquota
import hpcrelay
import hpcstate
import hpcstorage
from fedlayout import ROOT_MARKER, Layout
from fedpki import private_directory, write_secret
from fedspec import Spec
from hpcboot import core_phase, hpc_spec, hpc_values, load_owner, load_readings
from hpcca import ca_certificates
from hpcdown import BUNDLE, WORKDIR_LINE, Down
from hpclaunch import Launcher, StateStore
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


class CoreMismatch(RuntimeError):
    """The core an interpreter imports is not this checkout's."""


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
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    poll_s: float = 5.0
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
        template = self.spec.platform_template
        hpcpins.check(site, template.read_text(encoding="utf-8") if template is not None else None)
        self.check_scratch(site)
        if self.settings.sandbox is not None:
            self.say(
                f"the layout declares the agent's node ({self.settings.sandbox_placement}): it is held and placed, and "
                "only its node facts step runs on it here (M5's shard step runs the agent)"
            )

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

    def initial_state(self, site: Site) -> Dict[str, Any]:
        roles = self.settings.roles(tuple(p.name for p in self.spec.providers))
        node = self.settings.node()
        name = f"hpcop-{self.spec.project[:40]}-{secrets.token_hex(4)}"
        return {
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
            },
            "sandbox": self.sandbox_record(name),
            "storage": hpcstorage.plan(site, name, [p.name for p in self.spec.providers]),
            "roles": [role.name for role in roles],
            "services": {role.name: hpcstate.new_service(role.kind) for role in roles},
            "clients": {},
            "owner": None,
            "phase": "preflight",
        }

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

    def allocation(self, state: Mapping[str, Any], site: Site) -> Allocation:
        """The held job: the service nodes, and the agent's node as its component 1 under `het`."""
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
        )

    def sandbox_allocation(self, state: Mapping[str, Any], site: Site) -> Optional[Allocation]:
        """The agent's own held job under `separate` (one node of its own shape), else None."""
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
        )

    # -- up -----------------------------------------------------------------------------------------------------------

    def up(self, site: Site) -> str:
        self.preflight(site)
        self.prepare_root()
        with self.store.lifecycle("up"):  # a down cannot run between the state and the sbatch
            return self.bring_up(site)

    def bring_up(self, site: Site) -> str:
        state = self.store.create(self.initial_state(site))
        self.make_storage(state)  # recorded above, made before any job exists to bill for it
        asked, alone = self.allocation(state, site), self.sandbox_allocation(state, site)
        held = [(self.submit(asked, "allocation"), asked)]
        if alone is not None:  # sandbox_placement = "separate": the agent's own held job, exactly as the first
            held.append((self.submit(alone, "sandbox"), alone))
        for job, wanted in held:
            self.slurm.wait_running(job, self.timeouts["queue"], name=wanted.name)
        # Once more on the running jobs: the scheduler may allocate more than a pending request showed.
        records = [(job, shape, record) for job, wanted in held for shape, record in zip(
            wanted.shapes(), self.slurm.checked(job, wanted))]  # fmt: skip
        placed = self.placed_nodes(records)
        self.set(lambda s: self.place(s, placed, [record for _, _, record in records]))
        self.node_facts()
        self.zone_up()
        self.providers_up()
        self.export_zone()
        out = self.run_core()
        self.set(lambda s: s.update(phase="linked"))
        return f"{out}\n{self.start_relay()}"

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
        glibc and kernel (`readings`, O87: recorded only, a node that gives none is unknown, never refused)."""
        state = self.store.read()
        site, prefix = Site.from_json(state["site"]), state["allocation"]["name"]
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
                    s["services"][r].update(fqdn=f["fqdn"], address=f["address"], readings=g),
                    s.update(owner=list(o)),
                )
            )

    def launch(self, service: str, argv: Sequence[str]) -> None:
        """Launch a service step; its name, its log and its launch count are recorded first."""
        state = self.store.read()
        launch = state["services"][service]["launch"] + 1
        name = f"{state['allocation']['name']}-{service}-run-{launch}"
        log = str(Path(state["site"]["logs"]) / f"{name}.log")
        step = {"name": name, "id": None}
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
                raise RuntimeError(f"{service}'s step ended before its log said '{wanted}'")
            if self.clock() >= deadline:
                raise RuntimeError(f"{service} did not say '{wanted}' within {self.timeouts['service']:.0f} s")
            self.sleep(self.poll_s)

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
        self.launch("zone", argv)
        self.say("zone launched; waiting for its verdict")
        self.record_workdir("zone", self.wait_log("zone", VERDICT["oz"]))
        self.set(lambda s: (s["services"]["zone"].update(state="running"), s.update(phase="zone")))

    def providers_up(self) -> None:
        """Each provider, its peers the zone and every other provider; each registered by a token piped in."""
        state = self.store.read()
        site, services = Site.from_json(state["site"]), state["services"]
        zone, tar = services["zone"], str(Path(site.images) / "oneprovider.tar")
        names = [p.name for p in self.spec.providers]
        for name in names:
            peers = [services[p]["address"] for p in names if p != name]
            argv = ["bash", str(BUNDLE / "provider.sbatch"), str(BUNDLE), tar, name, zone["fqdn"], site.admin_mail]
            # --import=SPACE=PATH for each tree it owns, none for a mirror
            imports = hpcimport.provider_import_args(self.layout, name, self.environ)
            # --storage=PATH, its own directory on scratch, bound onto /volumes/posix; none without a scratch root
            storage = hpcstorage.provider_args(state, name)
            self.launch(name, [*argv, zone["address"], *peers, *imports, *storage])
        self.say(f"{len(names)} provider(s) launched; registering each")
        for name in names:
            self.register(name, self.record_workdir(name, self.wait_log(name, TOKEN_WAIT)))
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
        hosts = {name: record["fqdn"] for name, record in state["services"].items() if record["kind"] in ("oz", "op")}
        return Layout(self.root, self.layout.parent, hpc_spec(self.spec, hosts))

    def core_here(self) -> str:
        """The core's phase in this process: the bootstrap, the baseline, the facts, platform.toml, the link check."""
        state = self.store.read()
        owner = tuple(state["owner"] or ())
        if len(owner) != 2:
            raise RuntimeError("the state names no owner of the providers' storage: the node facts did not run")
        self.require_mount_proofs(state)
        s, site = self.settings, Site.from_json(state["site"])
        # the agent's sandbox's values, as up recorded the site (its files' pins checked in the preflight)
        values = {**hpc_values(state["command"], s.stop_bound_s, s.settle_timeout_s, self.root), **hpcpins.values(site)}
        out = self.core(self.placed_layout(state), owner, values, environ=self.environ)
        self.set(lambda s: s.update(phase="bootstrapped"))
        return out

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
