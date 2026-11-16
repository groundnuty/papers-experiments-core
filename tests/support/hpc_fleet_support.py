"""A bench for the fleet launcher's tests (deploy/hpc-federation/fleet.py): the pilot's layout as committed, one
site.env for every federation (as on the login node), and each federation over a fake cluster of its own
(`hpc_fed_support.FakeSlurm`, its own job ids), its core's phase the real one over the rehearsal's bootstrap stand-in,
so every root holds the platform.toml its up rendered and its link check's record.

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

from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, data_stand_in, hpc_on_path
from hpc_rehearsal_support import BootStandIn, link_check_stand_in, make_ca

from expcore_onedata.config import load_platform


def job_ids(label: str) -> tuple[str, str]:
    """Each federation's held job's id (and a second sbatch's), distinct across the fleet: fed-k gets 4k00."""
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

    def client_alive(self, client: Any) -> bool:
        return client.pid in self.bench.alive


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
    guard: Any = None  # the inode guard's stand-in: (count, layout, site, environ) -> its words

    def __post_init__(self) -> None:
        self.node_tmp = self.base / "node-tmpfs"
        for folder in ("images", "logs", "node-tmpfs"):
            (self.base / folder).mkdir()
        for name in ("onezone.tar", "oneprovider.tar"):
            (self.base / "images" / name).write_bytes(b"tar")
            (self.base / "images" / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
        agent = agent_site(self.base / "agent", work_root=self.node_tmp / "agent-work")
        self.environ = {**SITE, **agent, "SH": str(self.base / "images"), "L": str(self.base / "logs"),
                        "HPC_NODE_TMP": str(self.node_tmp), "HPC_FLEET_MAX": "5"}  # fmt: skip

    @property
    def fleet(self) -> Path:
        return self.base / "fleet"

    def root(self, label: str) -> Path:
        return self.fleet / label / "root"

    def fake(self, label: str) -> FakeSlurm:
        if label not in self.fakes:
            job, second = job_ids(label)
            self.fakes[label] = FakeSlurm(job_id=job, second_id=second, ca_pem=self.cas.get(label, self.ca_pem),
                                          node_tmp=str(self.node_tmp))  # fmt: skip
        return self.fakes[label]

    def driver(self, label: str, layout: Path, root: Path, agent: bool) -> Any:
        """`hpcfed.make_driver` over this federation's fake cluster, its waits not slept on, the core's phase the real
        one over the bootstrap's stand-in (as the rehearsal's `driver_maker`)."""
        made = self.deploy.hpc.cli.make_driver(layout, str(root), self.fake(label), self.environ, agent=agent)
        made.sleep = made.slurm.sleep = lambda seconds: None
        made.poll_s = made.slurm.poll_s = 0.0
        made.core = functools.partial(
            self.deploy.hpc.boot.core_phase,
            rest=lambda ca: SimpleNamespace(close=lambda: None),
            bootstrap=BootStandIn(label),
            steps=lambda b: None,
            content=lambda b: SimpleNamespace(reset=lambda timeout: None),
            data=self.data.get(label) or data_stand_in(),
        )
        return made

    def hooks(self, **given: Any) -> Any:
        fleet = self.deploy.fleet
        guard = self.guard or (lambda count, layout, site, environ: f"inode check: stand-in for {count}")
        return fleet.Hooks(driver=given.get("driver", self.driver), runner=lambda label: StepRunner(self, label),
                           inode_guard=guard)  # fmt: skip

    def run(self, *argv: str, environ: dict | None = None, **given: Any) -> tuple[int, str]:
        """`fleet.py --fleet <fleet> ARGV`, through its own `main`: its exit status and all it printed."""
        printed = io.StringIO()
        given_environ = self.environ if environ is None else environ
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            code = self.deploy.fleet.main(["--fleet", str(self.fleet), *argv], given_environ, self.hooks(**given))
        return code, printed.getvalue()

    def up(self, count: int, **given: Any) -> tuple[int, str]:
        return self.run("--layout", str(LAYOUT), "up", str(count), **given)

    def settings(self) -> Path:
        """The shard settings an operator gives plan: shards.py's keys, no [[shards]]."""
        path = self.base / "shard-settings.toml"
        lines = [f'{key} = "{self.base / key}"' for key in ("study", "results")]
        lines += ['python = "/usr/bin/python3"', f'core_src = "{self.base / "src"}"', f'tmpfs = "{self.node_tmp}"']
        lines += ['step_path = "/usr/bin:/bin"', "cpus = 2", 'memory = "4g"']
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def fingerprint(self, label: str) -> dict:
        return load_platform(self.root(label) / "platform.toml", require_secrets=False).fingerprint()


@contextlib.contextmanager
def fleet_modules():
    """The driver's modules and the fleet's, by their plain names, with both deploy directories on `sys.path` only while
    the context lasts (`hpc_on_path`): `fleet.py` adds them itself only when they are not there."""
    with hpc_on_path() as hpc:
        import fedlinks
        import fleet

        yield SimpleNamespace(hpc=hpc, fleet=fleet, fedlinks=fedlinks)


def make_bench(deploy: Any, base: Path, ca_pem: str, patch: Any) -> Bench:
    """A bench in `base`, the link check's transfers stood in (`link_check_stand_in`) while `patch` lasts."""
    patch.setattr(deploy.fedlinks, "check_links", link_check_stand_in([]))
    return Bench(deploy, base, ca_pem)


def another_ca(directory: Path) -> str:
    """The CA of another zone image's build: its platform fingerprint's TLS record differs."""
    return make_ca(directory)


__all__ = ["Bench", "StepRunner", "another_ca", "fleet_modules", "job_ids", "make_bench", "make_ca"]
