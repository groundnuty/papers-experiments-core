"""A bench for the fleet launcher's tests (deploy/hpc-federation/fleet.py): the pilot's layout as committed, one
site.env for every federation (as on the login node), and each federation over a fake cluster of its own
(`hpc_fed_support.FakeSlurm`, its own job ids), its core's phase the real one over the rehearsal's bootstrap stand-in,
so every root holds the platform.toml its up rendered and its link check's record.

The fleet runs each federation's `hpcfed.py up` and `down` in a process of its own; here they are `hpcfed.main` in
this process (`Bench.federation`), with `hpcfed.make_driver` patched once for the bench's life to put each federation
on its own fake cluster, chosen by its root (`<fleet>/<label>/root`).

The fleet's step launches each shard's srun line detached on the login node; here `StepRunner` stands for that
spawn, and checks each line against its own federation's fake cluster (`FakeSlurm._step`: the job and the node must
be that federation's, the component its het group). Nothing reaches a cluster, a node or a container.
"""

from __future__ import annotations

import contextlib
import functools
import io
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fingerprint_support import standin_python
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, data_stand_in, hpc_on_path
from hpc_rehearsal_support import BootStandIn, link_check_stand_in, make_ca

from expcore_onedata.config import load_platform


def job_ids(label: str) -> tuple[str, str]:
    """Each federation's held jobs' ids, distinct across the fleet: fed-k's allocation 4k00, its agent's own job
    (`sandbox_placement = "separate"`, a second sbatch) 4k50."""
    k = int(label.split("-")[1])
    return str(4000 + 100 * k), str(4050 + 100 * k)


@dataclass
class StepRunner:
    """The login node's Runner for one federation's shard steps: `spawn` checks the line on that federation's fake
    cluster and records it; a client runs while its pid is in `bench.alive`."""

    bench: Bench
    label: str

    def spawn(self, argv: list, client_log: str) -> Any:
        from hpcslurm import Client

        srun = self.bench.fake(self.label)._step(list(argv))
        assert not srun.refused, "a step of a heterogeneous job names its component"
        pid = 30000 + len(self.bench.spawned)
        launched = SimpleNamespace(label=self.label, argv=list(argv), log=client_log, srun=srun, pid=pid)
        self.bench.spawned.append(launched)
        self.bench.alive.add(pid)
        return Client(pid, 1)

    def client_state(self, client: Any) -> str:
        """What a recorded client is now, in the real `Runner.client_state`'s words: a pid not in the bench's table is
        gone, one whose start tick is not the recorded one (`bench.starts`, 1 unless a test says another process holds
        the pid now) is another process."""
        from hpcslurm import ALIVE, ANOTHER, GONE

        if client.pid not in self.bench.alive:
            return GONE
        return ALIVE if self.bench.starts.get(client.pid, 1) == client.start else ANOTHER

    def client_alive(self, client: Any) -> bool:
        from hpcslurm import ALIVE

        return self.client_state(client) == ALIVE

    def end_client(self, client: Any, grace_s: float = 10.0) -> bool:
        """Ends the client it was asked to end and records which: the real one signals while the pid is still the
        recorded process, and never another's."""
        self.bench.ended.append(client)
        if self.client_alive(client):
            self.bench.alive.discard(client.pid)
        return True

    def run(self, argv: list, timeout: float) -> Any:
        """What Slurm answers on the login node (the fleet asks it of a step's job before the launch)."""
        return self.bench.fake(self.label).run(argv, timeout)


@dataclass
class Bench:
    """The fleet's directory, its site's environment, and a fake cluster per federation, made on first use."""

    deploy: Any  # the deploy modules: .hpc (hpc_fed_support's), .fleet, .boot
    base: Path
    ca_pem: str
    environ: dict = field(default_factory=dict)
    fakes: dict = field(default_factory=dict)
    cas: dict = field(default_factory=dict)  # a federation's zone's CA, when not the common one (another image)
    data: dict = field(default_factory=dict)  # a federation's data as read back, when not its manifest's
    spawned: list = field(default_factory=list)
    alive: set = field(default_factory=set)
    starts: dict = field(default_factory=dict)  # pid -> the start tick of the process that holds it now (1: the client's)
    ended: list = field(default_factory=list)  # each client `end_client` was asked to end, in order
    pool: Any = None  # the nodes of one cluster every federation shares (O57), else each has a cluster of its own
    guard: Any = None  # the inode guard's stand-in: (count, layout, site, environ) -> its words
    home_guard: Any = None  # the home guard's stand-in: (site) -> its words, "" for none
    real: Any = None  # hpcfed.make_driver as the module has it, before the bench patches it
    core: Any = None  # what the manifest's interpreter says of its core (`shards.identify_core`'s report), or the
    # error it raises; a core with a known commit that is not stale until a test says otherwise
    asked: list = field(default_factory=list)  # the plan of each time the core was asked
    no_relay: set = field(default_factory=set)  # the federations whose up finds no fault relay in its core (O68)
    needs: tuple = ()  # the variables the settings' study plugin refuses to be built without (`standin_python`)

    def __post_init__(self) -> None:
        self.real = self.deploy.hpc.cli.make_driver
        self.node_tmp = self.base / "node-tmpfs"
        for folder in ("images", "logs", "node-tmpfs"):
            (self.base / folder).mkdir()
        for name in ("onezone.tar", "oneprovider.tar"):
            (self.base / "images" / name).write_bytes(b"tar")
            (self.base / "images" / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
        self.layout = SEPARATE_LAYOUT
        agent = agent_site(self.base / "agent", work_root=self.node_tmp / "agent-work")
        self.environ = {**SITE, **agent, "SH": str(self.base / "images"), "L": str(self.base / "logs"),
                        "HPC_NODE_TMP": str(self.node_tmp), "HPC_FLEET_MAX": "5"}  # fmt: skip

    def identify_core(self, plan: Any) -> Any:
        """Stands in for `shards.identify_core`, which runs the manifest's interpreter: the bench has no core to ask.
        The probe itself is tested with real interpreters (`test_hpc_shards_core_identity.py`)."""
        self.asked.append(plan)
        if isinstance(self.core, Exception):
            raise self.core
        return self.core or {"known": True, "stale": False, "why": None}

    @property
    def fleet(self) -> Path:
        return self.base / "fleet"

    def root(self, label: str) -> Path:
        return self.fleet / label / "root"

    def fake(self, label: str) -> FakeSlurm:
        if label not in self.fakes:
            job, second = job_ids(label)
            shared = {"nodes": list(self.pool)} if self.pool else {}
            fake = FakeSlurm(job_id=job, second_id=second, ca_pem=self.cas.get(label, self.ca_pem),
                             node_tmp=str(self.node_tmp), **shared)  # fmt: skip
            for other in self.fakes.values() if self.pool else ():  # one cluster: each `squeue --me` sees the rest
                other.peers.append(fake)
                fake.peers.append(other)
            self.fakes[label] = fake
        return self.fakes[label]

    def driver(self, label: str, layout: Path, root: Path, agent: bool) -> Any:
        """`hpcfed.make_driver` over this federation's fake cluster, its waits not slept on, the core's phase the real
        one over the bootstrap's stand-in (as the rehearsal's `driver_maker`)."""
        made = self.real(layout, str(root), self.fake(label), self.environ, agent=agent)
        made.sleep = made.slurm.sleep = lambda seconds: None
        made.poll_s = made.slurm.poll_s = 0.0
        made.relay_available = lambda: label not in self.no_relay  # every federation serves one unless a test says not
        made.core = functools.partial(
            self.deploy.hpc.boot.core_phase,
            rest=lambda ca, **kwargs: SimpleNamespace(close=lambda: None),
            bootstrap=BootStandIn(label),
            steps=lambda b: None,
            content=lambda b: SimpleNamespace(reset=lambda timeout: None),
            data=self.data.get(label) or data_stand_in(),
        )
        made.verify = functools.partial(  # `verify-data`: each ingested space read back as the same stand-in holds it
            self.deploy.hpc.boot.verify_ingested,
            rest=lambda ca, **kwargs: SimpleNamespace(close=lambda: None),
            bootstrap=BootStandIn(label),
            data=self.data.get(label) or data_stand_in(),
        )
        return made

    def make_driver(
        self, layout: Path, root: str, runner: Any = None, environ: Any = None, *, agent: bool = True
    ) -> Any:
        """`hpcfed.make_driver` as `hpcfed.main` calls it: the federation is the one whose slot holds the root, and the
        runner `main` was given must be that federation's own fake cluster."""
        label = Path(root).parent.name
        assert runner is None or runner is self.fake(label), f"{root} driven over another federation's cluster"
        return self.driver(label, layout, Path(root), agent)

    def federation(self, label: str, command: str, layout: Path, root: Path, log: Path) -> tuple[int, str]:
        """`hpcfed.py --layout L --root R <command>` through its own `main`, in this process, on the federation's own
        fake cluster: what the fleet's process of its own would run."""
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            code = self.deploy.hpc.cli.main(["--layout", str(layout), "--root", str(root), command], self.environ,
                                            runner=self.fake(label))  # fmt: skip
        log.write_text(printed.getvalue(), encoding="utf-8")
        if code == 0 and command == "up":
            self.verify_data(label, layout, root)
        return code, printed.getvalue()

    def verify_data(self, label: str, layout: Path, root: Path) -> None:
        """What an operator runs on each federation once the study's ingest is done, before `plan` (O57): the layout's
        one ingested space holds nothing, as its manifest (the empty one) declares: the stand-in shows it verified."""
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            code = self.deploy.hpc.cli.main(["--layout", str(layout), "--root", str(root), "verify-data"], self.environ,
                                            runner=self.fake(label))  # fmt: skip
        assert code == 0, printed.getvalue()

    def hooks(self, **given: Any) -> Any:
        fleet = self.deploy.fleet
        guard = self.guard or (lambda count, layout, site, environ: f"inode check: stand-in for {count}")
        home = self.home_guard or (lambda site: "")
        operate = given.get("federation", self.federation)
        return fleet.Hooks(
            federation=operate, runner=lambda label: StepRunner(self, label), inode_guard=guard, home_guard=home
        )

    def run(self, *argv: str, environ: dict | None = None, **given: Any) -> tuple[int, str]:
        """`fleet.py --fleet <fleet> ARGV`, through its own `main`: its exit status and all it printed."""
        printed = io.StringIO()
        given_environ = self.environ if environ is None else environ
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            code = self.deploy.fleet.main(["--fleet", str(self.fleet), *argv], given_environ, self.hooks(**given))
        return code, printed.getvalue()

    def up(self, count: int, **given: Any) -> tuple[int, str]:
        return self.run("--layout", str(self.layout), "up", str(count), **given)

    def settings(self, *extra: str) -> Path:
        """The shard settings an operator gives plan: shards.py's keys, no [[shards]]; `extra` are more `key = value`
        lines (an optional key of shards.py's manifest)."""
        path = self.base / "shard-settings.toml"
        (self.base / "study").mkdir(exist_ok=True)  # the campaign fingerprint is taken on a copy of it (O63)
        python = standin_python(self.base / "venv", real_platform=True, needs=self.needs)
        lines = [f'{key} = "{self.base / key}"' for key in ("study", "results")]
        lines += [f'python = "{python}"', f'core_src = "{self.base / "src"}"', f'tmpfs = "{self.node_tmp}"']
        lines += ['step_path = "/usr/bin:/bin"', "cpus = 2", 'memory = "4g"', *extra]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def fingerprint(self, label: str) -> dict:
        return load_platform(self.root(label) / "platform.toml", require_secrets=False).fingerprint()


SEPARATE_LAYOUT = LAYOUT.with_name("pilot-shape-separate.toml")  # the pilot's, the agent's node a job of its own


@contextlib.contextmanager
def fleet_modules():
    """The driver's modules and the fleet's, by their plain names, with both deploy directories on `sys.path` only while
    the context lasts (`hpc_on_path`): `fleet.py` adds them itself only when they are not there."""
    with hpc_on_path() as hpc:
        import fedlinks
        import fleet
        import shards

        yield SimpleNamespace(hpc=hpc, fleet=fleet, fedlinks=fedlinks, shards=shards)


def make_bench(deploy: Any, base: Path, ca_pem: str, patch: Any) -> Bench:
    """A bench in `base`; while `patch` lasts, the link check's transfers are stood in (`link_check_stand_in`) and
    `hpcfed.make_driver` is the bench's, and so is the answer to which core the manifest's interpreter imports."""
    patch.setattr(deploy.fedlinks, "check_links", link_check_stand_in([]))
    bench = Bench(deploy, base, ca_pem)
    patch.setattr(deploy.hpc.cli, "make_driver", bench.make_driver)
    patch.setattr(deploy.shards, "identify_core", bench.identify_core)  # the fleet asks the core of its plan
    return bench


def another_ca(directory: Path) -> str:
    """The CA of another zone image's build: its platform fingerprint's TLS record differs."""
    return make_ca(directory)


__all__ = ["SEPARATE_LAYOUT", "Bench", "StepRunner", "another_ca", "fleet_modules", "job_ids", "make_bench", "make_ca"]
