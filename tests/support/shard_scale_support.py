"""The scale study: a campaign the size of a main study's, made of stubs, for the rehearsal of #154 at N = 21 and 27.

`make_scale_study` writes a stamped study of four stub models, two arms (`plain`, `guided`), `TASKS` tasks and `SEEDS`
seeds, with `[shards] per_model` when a layout is given; its plugin is `scale_study_plugin.py`. Every copy made with
the same arguments is the same campaign, so they share one fingerprint wherever they lie.

**The size.** A main study has about 150 pairs of task and seed per model; `EXPCORE_SCALE_TASKS=75` gives them
(1200 runs per campaign; the rehearsal then takes over ten minutes on a shared host). The default, 12 tasks, gives 24
pairs per model (192 runs per campaign, 3 or 4 pairs per shard at N = 27), so that the rehearsal takes minutes, most of
them the start of its 49 driver processes and the package's scan of every byte it holds. What the shard path does with
a pair does not depend on how many a shard holds; the partition and the shares at 150 pairs per model are tested
without running (`tests/campaign/test_shard_budget.py`).

A `Host` is one driver's machine: its own copy of the study (so its own store and `out/`), the shard it runs, and what
its environment adds (the faults its platform plants). `in_pool` runs jobs at most `PARALLEL` at once: the host is
shared, so the drivers of a rehearsal take turns three at a time, each a process of its own. `measured` wraps a command
of the core so that its process reports its peak memory and its wall time.
"""

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from study_support import EXAMPLE, HEAD

from expcore.campaign.shard_budget import reservation_usd
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study

SUPPORT = Path(__file__).resolve().parent
CRASH = SUPPORT / "study_crash.py"
TASKS = int(os.environ.get("EXPCORE_SCALE_TASKS", "12"))
SEEDS = (1, 2)
MODELS = ("alpha-stub", "beta-stub", "gamma-stub", "delta-stub")
PRICES = {"alpha-stub": (1.0, 2.0), "beta-stub": (0.5, 1.5), "gamma-stub": (3.0, 6.0), "delta-stub": (0.25, 1.0)}
ARMS = ("plain", "guided")
REGIONS = 10
UNIT_CAP = 20  # concurrent sessions of each model over all its shards
PARALLEL = 3  # processes at once: the host is shared with other work
LAYOUT_21 = {"alpha-stub": 6, "beta-stub": 5, "gamma-stub": 5, "delta-stub": 5}
LAYOUT_27 = {"alpha-stub": 7, "beta-stub": 7, "gamma-stub": 7, "delta-stub": 6}
MEASURED = re.compile(r"^MEASURED (\d+) ([0-9.]+)$", re.MULTILINE)


def task_names(count: int = TASKS) -> list[str]:
    return [f"t{n:03d}" for n in range(1, count + 1)]


def _study_toml(tasks: Sequence[str]) -> str:
    lines = [
        "[study]",
        'name = "scale-study"',
        'plugin = "plugin.py"',
        'code = ["study.toml", "models.toml", "campaign.toml", "preregistration.toml", "plugin.py", "prompt.txt"]',
        "",
        "[factors.arm]",
        f"levels = {json.dumps(list(ARMS))}",
        "",
        "[identity]",
        'order = ["task", "arm", "model", "seed"]',
        "",
    ]
    for arm in ARMS:
        lines += [f"[conditions.{arm}]", f'arm = "{arm}"', ""]
    for n, task in enumerate(tasks):
        lines += [f"[tasks.{task}]", f'region = "r{n % REGIONS:02d}"', ""]
    return "\n".join(lines)


def _models_toml() -> str:
    lines = []
    for model in MODELS:
        given, taken = PRICES[model]
        lines += [f"[models.{model}]", 'backend = "stub"', 'status = "frozen"', "context_tokens = 8192", ""]
        lines += [f"[models.{model}.price]", f"input_per_mtok = {given}", f"output_per_mtok = {taken}", ""]
    return "\n".join(lines)


def _campaign_toml(
    per_model: Mapping[str, int] | None, max_usd: float, per_model_usd: Mapping[str, float], workers: int
) -> str:
    lines = [
        "order_seed = 20261114",
        f"seeds = {json.dumps(list(SEEDS))}",
        f"models = {json.dumps(list(MODELS))}",
        "",
        "[agent]",
        "max_tool_rounds = 4",
        "time_cap_s = 30",
        "token_budget = 20000",
        "output_cap_chars = 500",
        "max_output_tokens = 256",
        "",
        "[scheduler]",
        "max_attempts = 3",
        f"workers = {workers}",
        "retry_delay_s = 0",
        "poll_interval_s = 0.02",
        "recheck_s = 1",
        "health_timeout_s = 5",
        "lease_timeout_s = 30",
        "unit_pause_s = 0.5",
        "",
        "[scheduler.unit_caps]",
        *(f"{model} = {UNIT_CAP}" for model in MODELS),
        "",
        "[budget]",
        f"max_usd = {max_usd!r}",
        "",
    ]
    if per_model_usd:
        lines += ["[budget.per_model_usd]", *(f"{model} = {usd!r}" for model, usd in per_model_usd.items()), ""]
    if per_model is not None:
        counts = ", ".join(f"{model} = {count}" for model, count in per_model.items())
        lines += ["[shards]", f"per_model = {{ {counts} }}", ""]
    return "\n".join(lines)


PREREGISTRATION = """alpha = 0.05
holm_family = ["guided-vs-plain"]

[bootstrap]
replicates = 200
seed = 20261114
ci_level = 0.95

[power]
seed = 1
n_simulations = 20
target_clusters = 10
target_difference = 0.2
sd_grid = [0.2]

[estimators.wilcoxon_cluster_bootstrap]
method = "auto"

[contrasts.guided-vs-plain]
conditions = ["guided", "plain"]
outcome = "accuracy"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "region"

[contrasts.guided-rounds]
conditions = ["guided", "plain"]
outcome = "rounds"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "region"
"""

PACKAGE = """[package]
name = "scale-study-package"
include = [
    "README.md",
    "package.toml",
    "study.toml",
    "models.toml",
    "campaign.toml",
    "preregistration.toml",
    "plugin.py",
    "prompt.txt",
    "out/runs",
    "out/merge.json",
    "out/shards",
    "out/export.json",
    "out/scores.json",
    "out/analysis.json",
    "out/report.md",
]
"""


def make_scale_study(
    base: Path,
    name: str,
    *,
    per_model: Mapping[str, int] | None = None,
    max_usd: float = 1000.0,
    per_model_usd: Mapping[str, float] | None = None,
    workers: int = 1,
    tasks: int = TASKS,
) -> Path:
    """Write a stamped copy of the scale study under `base/name` and return its root: `per_model` is its `[shards]`
    layout, `max_usd` and `per_model_usd` its budget."""
    root = base / name
    root.mkdir(parents=True)
    files = {
        "study.toml": _study_toml(task_names(tasks)),
        "models.toml": _models_toml(),
        "campaign.toml": _campaign_toml(per_model, max_usd, per_model_usd or {}, workers),
        "preregistration.toml": PREREGISTRATION,
        "package.toml": PACKAGE,
        "README.md": "# Scale study\n\nFour stub models, two arms, many tasks: the rehearsal of a sharded campaign.\n",
    }
    for file, text in files.items():
        (root / file).write_text(text, encoding="utf-8")
    shutil.copyfile(EXAMPLE / "prompt.txt", root / "prompt.txt")
    shutil.copyfile(SUPPORT / "scale_study_plugin.py", root / "plugin.py")
    write_stamp(root, HEAD, load_study(root).scope)
    return root


def run_reservation_usd(root: Path, model: str) -> float:
    """What the budget gate reserves for one run of `model` in the study at `root`."""
    study = load_study(root)
    return reservation_usd(study.models.resolve(model), study.campaign.agent.limits)


# --- hosts, processes and the pool ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Done:
    """How a process ended: its exit status and what it printed on stdout and stderr."""

    code: int
    out: str
    errors: str

    @property
    def measured(self) -> tuple[int, float]:
        """The peak resident memory (KiB) and wall time (s) a `measured` command reported."""
        found = MEASURED.findall(self.errors)
        assert found, f"the process reported no measurement: {self.errors[-500:]}"
        kib, seconds = found[-1]
        return int(kib), float(seconds)


def run_process(command: Sequence[str], env: Mapping[str, str], timeout: float = 900) -> Done:
    """Run a command to its end and return how it ended."""
    done = subprocess.run(list(command), env=dict(env), capture_output=True, text=True, timeout=timeout, check=False)
    return Done(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class Host:
    """One driver's machine: its copy of the study, the shard it runs (None: the whole campaign) and its faults."""

    root: Path
    shard: tuple[int, int] | None = None
    faults: Mapping[str, str] = field(default_factory=dict)

    @property
    def out(self) -> Path:
        return self.root / "out"

    def options(self) -> list[str]:
        if self.shard is None:
            return []
        index, count = self.shard
        return ["--shard", f"{index}/{count}", "--shard-label", f"shard-{index}"]

    def run(self, env: Mapping[str, str]) -> Done:
        """`python -m expcore run` of this host, to its end."""
        command = [sys.executable, "-m", "expcore", "run", str(self.root), *self.options()]
        return run_process(command, {**env, **self.faults})

    def crash(self, env: Mapping[str, str], point: str, nth: int) -> Done:
        """The same run, killed (exit status 9, no clean-up) when its `nth` attempt reaches `point`."""
        command = [sys.executable, str(CRASH), str(self.root), point, str(nth), *self.options()]
        return run_process(command, {**env, **self.faults})


MEASURE = (
    "import resource, sys, time\n"
    "from expcore.cli import main\n"
    "began = time.monotonic()\n"
    "code = main(sys.argv[1:])\n"
    "peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
    "print(f'MEASURED {peak} {time.monotonic() - began:.2f}', file=sys.stderr)\n"
    "sys.exit(code)\n"
)


def measured(env: Mapping[str, str], *argv: str) -> Done:
    """Run `python -m expcore ARGV...` in a process of its own that reports its peak memory and wall time."""
    return run_process([sys.executable, "-c", MEASURE, *argv], env)


def finish_batch(env: Mapping[str, str], root: Path) -> dict[str, Done]:
    """check, analyze and report of a study's batch, each measured, in that order; stops at the first that fails."""
    steps = {"check": str(root / "out" / "runs"), "analyze": str(root), "report": str(root)}
    done: dict[str, Done] = {}
    for name, target in steps.items():
        done[name] = measured(env, name, target)
        if done[name].code != 0:
            break
    return done


PACKAGE_BUILD = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from package_support import stand_in_vendor\n"
    "from expcore.package.build import build_package\n"
    "from expcore.provenance.hashfile import render_hash_file\n"
    "root, dist = Path(sys.argv[2]), Path(sys.argv[3])\n"
    "dist.mkdir(parents=True)\n"
    "hashes = dist / 'private.sha256'\n"
    "hashes.write_text(render_hash_file(['zarnothwick']), encoding='utf-8')  # an invented private name\n"
    "print(build_package(root, dist / 'archive', hashes=hashes, vendor=stand_in_vendor))\n"
)


def build_package_apart(env: Mapping[str, str], root: Path, dist: Path) -> Done:
    """Build the study's replication package into `dist/archive` in a process of its own, with stand-in wheels; it
    prints the archive's path. The build is refused unless the package passes its own check."""
    return run_process([sys.executable, "-c", PACKAGE_BUILD, str(SUPPORT), str(root), str(dist)], env)


def in_pool[T](jobs: Mapping[str, Callable[[], T]]) -> dict[str, T]:
    """Run every job, at most `PARALLEL` at once, in the order given, and return each one's result by name."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {name: pool.submit(job) for name, job in jobs.items()}
        return {name: future.result() for name, future in futures.items()}
