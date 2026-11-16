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

import hpcrelay
import hpcstate
from fedlayout import ROOT_MARKER, Layout
from fedpki import private_directory, write_secret
from fedspec import Spec
from hpcboot import core_phase, hpc_spec, hpc_values, load_owner
from hpcdown import BUNDLE, WORKDIR_LINE, Down
from hpclaunch import Launcher, StateStore
from hpcsettings import HpcSettings, Site
from hpcslurm import Allocation, Client, Slurm, SlurmError

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


class CoreMismatch(RuntimeError):
    """The core an interpreter imports is not this checkout's."""


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
        node, s = self.settings.node(), self.settings
        lines = [
            f"layout: {self.spec.source} (project {self.spec.project}); root {self.root}",
            f"held job: {len(roles)} node(s) of {node.cpus} cores and {node.memory_mib} MiB, --time {s.time}, "
            f"--no-requeue; {len(roles) * node.cpus} cores and {len(roles) * node.memory_mib} MiB in all",
        ]
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

    def preflight(self, site: Site) -> None:
        if any(space.imported for space in self.spec.spaces):
            raise RuntimeError("the layout declares an imported space, which the HPC driver does not hold yet (M3)")
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
        if self.settings.sandbox is not None:
            self.say("the layout declares the agent's node: it is held and placed, and nothing runs on it yet (M5)")

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
        return {
            "format": hpcstate.FORMAT,
            "project": self.spec.project,
            "layout": {"source": self.spec.source, "digest": self.spec.digest, "path": str(self.layout_path)},
            "core": dict(self.core_paths),
            "command": list(self.command),
            "site": site.to_json(),
            "allocation": {
                "name": f"hpcop-{self.spec.project[:40]}-{secrets.token_hex(4)}",
                "job": None,
                "nodes": [],
                "cpus_per_node": node.cpus,
                "memory_mib_per_node": node.memory_mib,
                "time": self.settings.time,
                "readback": None,
                "started": None,
                "ends": None,
            },
            "roles": [role.name for role in roles],
            "services": {role.name: hpcstate.new_service(role.kind) for role in roles},
            "clients": {},
            "owner": None,
            "phase": "preflight",
        }

    def allocation(self, state: Mapping[str, Any], site: Site) -> Allocation:
        node = self.settings.node()
        return Allocation(
            name=state["allocation"]["name"],
            account=site.account,
            partition=site.partition,
            nodes=len(state["roles"]),
            cpus_per_node=node.cpus,
            memory_mib=node.memory_mib,
            time=self.settings.time,
            time_s=self.settings.time_s,
        )

    # -- up -----------------------------------------------------------------------------------------------------------

    def up(self, site: Site) -> str:
        self.preflight(site)
        self.prepare_root()
        state = self.store.create(self.initial_state(site))
        asked = self.allocation(state, site)
        job = self.slurm.submit(asked)
        self.set(lambda s: (s["allocation"].update(job=job), s.update(phase="submitted")))
        self.say(f"held job {job} submitted ({asked.nodes} nodes); reading back what Slurm recorded")
        readback = self.slurm.readback(job, asked)
        self.set(lambda s: s["allocation"].update(readback=readback))
        info = self.slurm.wait_running(job, self.timeouts["queue"])
        nodes = self.slurm.hostnames(info.get("NodeList", ""))
        if len(nodes) != asked.nodes:
            raise SlurmError(f"the held job runs on {len(nodes)} node(s), not the {asked.nodes} asked for")
        self.set(lambda s: self.place(s, nodes, info))
        self.node_facts()
        self.zone_up()
        self.providers_up()
        self.export_zone()
        out = self.run_core()
        self.set(lambda s: s.update(phase="linked"))
        return f"{out}\n{self.start_relay()}"

    def start_relay(self) -> str:
        """The login node's fault relay, for a campaign on a compute node (M4); none in a core without it."""
        if not self.relay_available():
            return f"fault relay: none, this core has no {hpcrelay.RELAY}"
        state = self.store.read()
        log = Path(state["site"]["logs"]) / f"{state['allocation']['name']}-fault-relay.log"
        client = hpcrelay.start(
            self.slurm.runner, state["command"][0], self.root, state["command"], log, self.environ,
            clock=self.clock, sleep=self.sleep,
        )  # fmt: skip
        self.set(lambda s: s.update(relay={"pid": client.pid, "start": client.start, "log": str(log)}))
        return "fault relay: serving"

    def place(self, state: Dict[str, Any], nodes: List[str], info: Mapping[str, str]) -> None:
        state["allocation"].update(nodes=nodes, started=info.get("StartTime"), ends=info.get("EndTime"))
        for role, node in zip(state["roles"], nodes):
            state["services"][role]["node"] = node
        state["phase"] = "placed"

    def node_facts(self) -> None:
        """Each node's FQDN, its IPv4 address and the job's uid and gid, from a control step on it."""
        state = self.store.read()
        site, prefix = Site.from_json(state["site"]), state["allocation"]["name"]
        for role in state["roles"]:
            argv = [site.node_python, "-I", str(BUNDLE / "nodefacts.py")]
            node = state["services"][role]["node"]
            done = self.launcher.control(node, argv, name=f"{prefix}-{role}-facts", timeout_s=self.timeouts["step"])
            owner = load_owner(done.stdout) if done.returncode == 0 else None
            facts = json.loads(done.stdout) if owner else {}
            if not FQDN.fullmatch(str(facts.get("fqdn"))) or not IPV4.fullmatch(str(facts.get("address"))):
                raise RuntimeError(f"the node facts of {role} are not a name, an IPv4 address, a uid and a gid")
            self.set(
                lambda s, r=role, f=facts, o=owner: (
                    s["services"][r].update(fqdn=f["fqdn"], address=f["address"]),
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
            self.launch(name, [*argv, zone["address"], *peers])
        self.say(f"{len(names)} provider(s) launched; registering each")
        for name in names:
            self.register(name, self.record_workdir(name, self.wait_log(name, TOKEN_WAIT)))
        for name in names:
            self.wait_log(name, VERDICT["op"])
            self.set(lambda s, n=name: s["services"][n].update(state="running"))
            self.say(f"{name} is up")
        self.set(lambda s: s.update(phase="providers"))

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
        """The zone's passphrase into the root's secrets (0600), where the Compose root holds it, and its CA."""
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
        ca = export("ca")
        if "-----BEGIN CERTIFICATE-----" not in ca:
            raise RuntimeError("the zone's exported CA holds no certificate")
        self.layout.ca_pem.write_text(ca, encoding="utf-8")

    def placed_layout(self, state: Mapping[str, Any]) -> Layout:
        hosts = {name: record["fqdn"] for name, record in state["services"].items() if record["kind"] in ("oz", "op")}
        return Layout(self.root, self.layout.parent, hpc_spec(self.spec, hosts))

    def core_here(self) -> str:
        """The core's phase in this process: the bootstrap, the baseline, the facts, platform.toml, the link check."""
        state = self.store.read()
        owner = tuple(state["owner"] or ())
        if len(owner) != 2:
            raise RuntimeError("the state names no owner of the providers' storage: the node facts did not run")
        s = self.settings
        values = hpc_values(state["command"], s.stop_bound_s, s.settle_timeout_s)
        out = self.core(self.placed_layout(state), owner, values)
        self.set(lambda s: s.update(phase="bootstrapped"))
        return out

    def run_core(self) -> str:
        """The core's phase where `[hpc] core` says: here, or in a control step on the zone's node."""
        if self.settings.core == "login":
            return self.core_here()
        state = self.store.read()
        zone, site = state["services"]["zone"], Site.from_json(state["site"])
        argv = [site.core_python, "-I", str(HERE / "hpcfed.py"), "--layout", str(self.layout_path)]
        argv += ["--root", str(self.root), "core"]
        name = f"{state['allocation']['name']}-core"
        done = self.launcher.control(zone["node"], argv, name=name, timeout_s=self.timeouts["core"])
        if done.returncode == CORE_MISMATCH:
            raise CoreMismatch("the core's step imports a core that is not this checkout's")
        if done.returncode != 0:
            raise RuntimeError(f"the core's step failed with status {done.returncode}: {done.stderr.strip()[-600:]}")
        return done.stdout.strip()
