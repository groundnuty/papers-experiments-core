"""What the HPC rehearsal (tests/e2e/test_hpc_rehearsal.py) wires together: two federations brought up by the driver's
own `up` over the fake Slurm, the study whose plugin runs every run in the Apptainer sandbox on a fake node, the shard
manifest over the roots `up` wrote, the steps run as the node would run them, the holds that time a kill, and `down`.

Every stage reads only what the previous one wrote: `up` writes the state file, `platform.toml` and `link-check.json`
in each root; `shards.py plan` reads the state files; each step runs the line `plan` printed, after its `srun` prefix,
in that line's environment and nothing else; `collect` reads the archives the steps wrote; `down` reads the state file.

The federations run the pilot's layout and template as committed, the site's values those `site.env` holds. What
stands in is what cannot run offline, each named where it is made: the cluster (`hpc_fed_support.FakeSlurm`, its zone
exporting a real CA), the zone's and the providers' answers to the bootstrap (`BootStandIn`), the link check's
transfers (`link_check_stand_in`), and each run's node (`fake_mount_node.FakeMountNode`, its driver the dispatcher
itself). The seams the rehearsal found, and how each was closed, are in its pull request.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
import shard_support
from hpc_fed_support import HPC, LAYOUT, SITE, FakeSlurm, agent_site, data_stand_in, hpc_on_path
from study_support import HEAD
from tls_support import make_federation_ca

from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study

REPO = HPC.parents[1]
SUPPORT = Path(__file__).resolve().parent
SHARDS_SCRIPT = HPC / "shards.py"
LABELS = ("fed-a", "fed-b")  # one shard per federation, in the manifest's order
# each fake cluster's held job (and a second's) ids
JOBS = {"fed-a": ("4242", "4343"), "fed-b": ("5252", "5353"), "fed-c": ("6262", "6363")}
PLUGIN = "hpc_plugin.py"
PLUGIN_SETTINGS = "hpc_rehearsal.json"
SNAPSHOT_EVERY_S = 3600  # the manifest's: no clock snapshot within the rehearsal, so only a run's end takes one


# ------------------------------------------------------------------------------------------------ the deploy modules
@dataclass
class Deploy:
    """The driver's modules (by their plain names, as hpcfed.py imports them) and shards.py."""

    hpc: Any
    shards: Any
    fedlinks: Any


@contextlib.contextmanager
def deploy_modules() -> Iterator[Deploy]:
    """The driver's modules with both deploy directories on `sys.path` only while the context lasts, and shards.py
    under a module name of the rehearsal's own."""
    with hpc_on_path() as hpc:
        import fedlinks

        spec = importlib.util.spec_from_file_location("hpc_shards_rehearsal", SHARDS_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules["hpc_shards_rehearsal"] = module  # its dataclasses look their module up by name
        try:
            spec.loader.exec_module(module)
            yield Deploy(hpc, module, fedlinks)
        finally:
            sys.modules.pop("hpc_shards_rehearsal", None)


# ------------------------------------------------------------------------------------------------ one federation
def pinned_file(path: Path, content: bytes) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()}


@dataclass
class Federation:
    """One federation of the rehearsal, over the pilot's layout as committed: its root, its fake cluster, and its
    agent's node, whose tmpfs (`node_tmp`, the manifest's `tmpfs` and the site's `HPC_NODE_TMP`) is a directory of this
    host that both federations' agent nodes stand for here, each with a work root of its own in it."""

    label: str
    base: Path
    node_tmp: Path
    ca_pem: str
    fake: FakeSlurm = field(init=False)
    root: Path = field(init=False)
    printed: dict[str, str] = field(default_factory=dict)
    codes: dict[str, int] = field(default_factory=dict)
    pairs: list = field(default_factory=list)
    data: Any = field(default_factory=data_stand_in)  # what each space holds, read back (`hpcboot.core_phase`)
    sif: bytes = b"an invented SIF image\n"  # the agent's image this federation's site declares

    def __post_init__(self) -> None:
        self.root = self.base / "fed" / "root"
        job, second = JOBS[self.label]
        self.fake = FakeSlurm(job_id=job, second_id=second, ca_pem=self.ca_pem, node_tmp=str(self.node_tmp),
                              sweep_here=True)  # fmt: skip
        for folder in ("images", "logs", "fed"):
            (self.base / folder).mkdir(parents=True)
        for name in ("onezone.tar", "oneprovider.tar"):
            (self.base / "images" / name).write_bytes(b"tar")
            (self.base / "images" / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
        self.agent = agent_site(self.base / "agent", sif=self.sif, work_root=self.work_root)  # pinned files

    @property
    def work_root(self) -> Path:
        return self.node_tmp / f"agent-{self.label}"

    def site(self) -> dict[str, str]:
        """site.env, as `set -a; . site.env; set +a` exports it on the login node."""
        return {**SITE, **self.agent, "SH": str(self.base / "images"), "L": str(self.base / "logs"),
                "HPC_NODE_TMP": str(self.node_tmp)}  # fmt: skip


class BootStandIn:
    """The bootstrap of `hpcboot.core_phase`, standing in for the panel and REST calls a live zone answers: the ids a
    bootstrap makes, and the secrets it writes into the root (the admin's token, each user's password)."""

    def __init__(self, label: str) -> None:
        self.label = label

    def __call__(self, rest: Any, layout: Any, owner: Any) -> SimpleNamespace:
        spec = layout.spec
        ids = SimpleNamespace(
            admin=f"uid-admin-{self.label}",
            providers={p.name: f"pid-{p.name}-{self.label}" for p in spec.providers},
            spaces={s.name: f"sid-{s.name}-{self.label}" for s in spec.spaces},
            users={p.name: f"uid-{p.name}-{self.label}" for p in spec.principals},
            scans={},
            storages={},
        )
        secrets = {"admin.token": "an-invented-admin-token"}
        secrets |= {f"users/{p.name}.pass": "an-invented-password" for p in spec.principals}
        for name, value in secrets.items():
            path = layout.root / "secrets" / name
            path.write_text(value, encoding="utf-8")
            os.chmod(path, 0o600)
        return SimpleNamespace(layout=layout, ids=ids, spec=spec)


def link_check_stand_in(pairs_seen: list) -> Any:
    """`fedlinks.check_links`, standing in for the transfers of a live link check: every pair the rendered platform
    file declares passes. What it writes into the root is the platform layer's own record."""
    from expcore_onedata.linkcheck import LinkReport, PairResult
    from expcore_onedata.linksettings import link_pairs

    def check(config: Any, client: Any) -> Any:
        pairs = link_pairs(config.spaces, list(config.providers))
        pairs_seen.append(pairs)
        rows = tuple(PairResult(f"providers[{i}]", f"providers[{i + 1}]", True, 0.5, 65536) for i in range(len(pairs)))
        return LinkReport(True, "ok", rows, 1.0, 2.0)

    return check


def driver_maker(deploy: Deploy, fed: Federation) -> Any:
    """`hpcfed.make_driver` as `hpcfed.main` calls it, with the waits not slept on and the core's phase the real one
    over a bootstrap that answers what a live zone would (`BootStandIn`)."""
    real = deploy.hpc.cli.make_driver
    boot = deploy.hpc.boot

    def make(*args: Any, **kwargs: Any) -> Any:
        driver = real(*args, **kwargs)
        driver.sleep = driver.slurm.sleep = lambda seconds: None
        driver.poll_s = driver.slurm.poll_s = 0.0
        driver.core = functools.partial(
            boot.core_phase,
            rest=lambda ca: SimpleNamespace(close=lambda: None),
            bootstrap=BootStandIn(fed.label),
            steps=lambda b: None,
            content=lambda b: SimpleNamespace(reset=lambda timeout: None),
            data=fed.data,
        )
        return driver

    return make


def hpcfed(deploy: Deploy, fed: Federation, command: str, environ: Mapping[str, str]) -> int:
    """`hpcfed.py --layout L --root R <command>`, through its own `main`, over this federation's fake cluster."""
    printed = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(deploy.hpc.cli, "make_driver", driver_maker(deploy, fed))
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            code = deploy.hpc.cli.main(
                ["--layout", str(LAYOUT), "--root", str(fed.root), command], environ, runner=fed.fake
            )
    fed.codes[command], fed.printed[command] = code, printed.getvalue()
    return code


def make_ca(directory: Path) -> str:
    return make_federation_ca(directory).ca.read_text(encoding="ascii")


# ------------------------------------------------------------------------------------------------ the study
def make_study(base: Path, control: Path) -> Path:
    """The example study with caps two shards can share (`shard_support.make_study`), its plugin the rehearsal's
    (`hpc_rehearsal_plugin.py`), stamped: one copy on the filesystem the nodes see, which every step copies."""
    root = shard_support.make_study(base, "study")
    shutil.copyfile(SUPPORT / "hpc_rehearsal_plugin.py", root / PLUGIN)
    settings = {"support": str(SUPPORT), "control": str(control)}
    (root / PLUGIN_SETTINGS).write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text = (root / "study.toml").read_text(encoding="utf-8")
    for old, new in (
        # every study that builds the Onedata platform through the core's loaders lists its module (C11, #329)
        ('plugin = "plugin.py"', f'plugin = "{PLUGIN}"\nplatforms = ["expcore_onedata.sandbox.kinds"]'),
        ('"plugin.py", "prompt.txt"]', f'"plugin.py", "{PLUGIN}", "{PLUGIN_SETTINGS}", "prompt.txt"]'),
    ):
        assert text.count(old) == 1, f"study.toml: {old!r}"
        text = text.replace(old, new)  # fmt: skip
    (root / "study.toml").write_text(text, encoding="utf-8")
    write_stamp(root, HEAD, load_study(root).scope)
    return root


def write_manifest(path: Path, study: Path, results: Path, tmpfs: Path, feds: Mapping[str, Federation]) -> Path:
    """shards.toml, as an operator writes it: the study, the core's interpreter and source, where results come back,
    the node's tmpfs, and one shard per federation by the root its `up` wrote, in `feds`' order."""
    lines = [
        f'study = "{study}"',
        f'python = "{sys.executable}"',
        f'core_src = "{REPO / "src"}"',
        f'results = "{results}"',
        f'tmpfs = "{tmpfs}"',
        'step_path = "/usr/bin:/bin"',
        "cpus = 2",
        'memory = "4g"',
        f"snapshot_every_s = {SNAPSHOT_EVERY_S}",
    ]
    for label, fed in feds.items():
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{fed.root}"']
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------------------------------------ a step's line
@dataclass(frozen=True)
class StepLine:
    """One shard's line from `shards.py plan`: the `srun` prefix, the environment `env -i` gives, and the command."""

    shard: str
    srun: list[str]
    env: dict[str, str]
    command: list[str]

    @classmethod
    def parse(cls, shard: str, argv: list[str]) -> StepLine:
        cut = argv.index("/usr/bin/env")
        assert argv[cut + 1] == "-i", "the step's command runs behind env -i"
        rest = argv[cut + 2 :]
        pairs = [w for w in rest if "=" in w and w.split("=", 1)[0].isupper()]
        assert rest[: len(pairs)] == pairs, "the allowlist comes first"
        env = dict(w.split("=", 1) for w in pairs)
        return cls(shard, argv[:cut], env, rest[len(pairs) :])

    def option(self, name: str) -> str:
        return self.srun[self.srun.index(name) + 1] if name in self.srun else ""

    def start(self, log: Path, *extra: str) -> subprocess.Popen:
        """Run the line on this host as the node would: `/usr/bin/env -i <the allowlist> <command>`, in a session of its
        own (so a kill outright takes the step and its dispatcher together), its output to `log`."""
        argv = ["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in self.env.items()), *self.command, *extra]
        with open(log, "ab") as handle:
            return subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)  # fmt: skip


def wait_step(process: subprocess.Popen, timeout_s: float = 600.0) -> int:
    try:
        return process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        kill_outright(process)
        raise


def kill_outright(process: subprocess.Popen) -> None:
    """SIGKILL to the step's whole session: the step and its dispatcher, with no chance to snapshot or archive, as a
    walltime's end, a node's or an OOM kill of the step's cgroup leaves it."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=60)


# ------------------------------------------------------------------------------------------------ holds and snapshots
@dataclass(frozen=True)
class Control:
    """The control directory the plugin reads its holds from (`hpc_rehearsal_plugin`, "Holds")."""

    path: Path

    def hold(self, label: str, n: int) -> None:
        (self.path / f"hold-{label}-{n}").write_text("", encoding="utf-8")

    def release(self, label: str, n: int) -> None:
        (self.path / f"go-{label}-{n}").write_text("", encoding="utf-8")

    def clear(self) -> None:
        for path in self.path.iterdir():
            path.unlink()

    def wait_held(self, label: str, n: int, process: subprocess.Popen, timeout_s: float = 180.0) -> None:
        wait_for(lambda: (self.path / f"held-{label}-{n}").exists(), process, timeout_s, f"attempt {n} held")


def wait_for(ready: Any, process: subprocess.Popen, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not ready():
        if process.poll() is not None:
            raise AssertionError(f"the step ended (status {process.returncode}) before {what}")
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not within {timeout_s:.0f} s")
        time.sleep(0.05)


def attempts_in(snapshot: Path) -> list[tuple[str, str]]:
    """The (run key, state) of every attempt a snapshot's store copy holds, in attempt order."""
    connection = sqlite3.connect(f"file:{quote(str(snapshot / 'state.sqlite'))}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in connection.execute("SELECT run_key, state FROM attempts ORDER BY id")]
    finally:
        connection.close()


def attempt_starts_in(snapshot: Path) -> set[tuple[str, str]]:
    """The (run key, started_utc) of every attempt a snapshot's store copy holds: what names an attempt alike in the
    store, in the spend journal and among the export's lost entries."""
    connection = sqlite3.connect(f"file:{quote(str(snapshot / 'state.sqlite'))}?mode=ro", uri=True)
    try:
        return {(key, started) for key, started in connection.execute("SELECT run_key, started_utc FROM attempts")}
    finally:
        connection.close()


def journaled_starts(journal: Path) -> set[tuple[str, str]]:
    """The (run key, started_utc) of every attempt a spend journal has a line of. A kill can cut the file's last line
    short, which reads as not written (`spend_journal.read_journal`); every other line must be one JSON object."""
    whole = journal.read_text(encoding="utf-8").split("\n")[:-1]  # what follows the last newline is empty or cut short
    lines = [json.loads(line) for line in whole]
    return {(line["run_key"], line["started_utc"]) for line in lines if line["kind"] == "attempt"}


__all__ = [
    "LABELS",
    "Control",
    "Deploy",
    "Federation",
    "StepLine",
    "attempt_starts_in",
    "attempts_in",
    "deploy_modules",
    "hpcfed",
    "journaled_starts",
    "kill_outright",
    "link_check_stand_in",
    "make_ca",
    "make_study",
    "pinned_file",
    "wait_for",
    "wait_step",
    "write_manifest",
]
