"""Across the two HPC lanes: the shard driver (deploy/hpc-federation/shards.py) reads the state file that the
federation driver's own `up` writes (hpcdriver.py), with the agent's node held as a heterogeneous job's component or
as a held job of its own. Slurm is the fake of hpc_fed_support; the core's phase is a stand-in; nothing runs srun."""

import importlib.util
import sys
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, hpc_on_path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
CONTROL = 'control = { cpus = 1, memory = "1g" }'
PLACEMENT = 'sandbox_placement = "het"'
AGENT = 'sandbox = { cpus = 8, memory = "17g" }'


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_up_state", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_up_state"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_up_state", None)


def up_federation(hpc, tmp_path: Path, placement: str) -> tuple[Path, FakeSlurm]:
    """The pilot-shape layout with the agent's node held as `placement`, brought up by the driver over the fake
    Slurm; the federation's root, and the fake."""
    images, logs, layouts = tmp_path / "images", tmp_path / "logs", tmp_path / "layout"
    for folder in (images, logs, layouts, tmp_path / "fed"):
        folder.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    text = LAYOUT.read_text(encoding="utf-8")
    assert CONTROL in text
    assert PLACEMENT in text
    text = text.replace(PLACEMENT, f'sandbox_placement = "{placement}"').replace(CONTROL, f"{CONTROL}\n{AGENT}")
    template = LAYOUT.parent / "pilot-shape.platform.toml.template"
    (layouts / LAYOUT.name).write_text(text, encoding="utf-8")
    (layouts / template.name).write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    fake, root = FakeSlurm(), tmp_path / "fed" / "root"
    driver = hpc.cli.make_driver(layouts / LAYOUT.name, str(root), runner=fake)
    driver.sleep = driver.slurm.sleep = lambda s: None
    driver.poll_s = driver.slurm.poll_s = 0.0
    driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
    driver.relay_available = lambda: False
    driver.up(hpc.settings.site_from_environment({**SITE, "SH": str(images), "L": str(logs)}))
    return root, fake


def manifest(tmp_path: Path, root: Path) -> Path:
    """A shard manifest whose first shard is the federation brought up here (the second is never read)."""
    lines = [f'{key} = "{tmp_path / key}"' for key in ("study", "python", "core_src", "results", "tmpfs")]
    lines += ['step_path = "/usr/bin:/bin"', "cpus = 6", 'memory = "18g"']
    lines += ["[[shards]]", 'label = "fed-a"', f'root = "{root}"']
    lines += ["[[shards]]", 'label = "fed-b"', f'root = "{tmp_path / "other" / "root"}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("placement", "job", "group", "suffix"),
    [("het", "4242", ["--het-group=1"], ""), ("separate", "4343", [], "-sandbox")],
)
def test_the_shard_step_lands_on_the_agents_node_where_up_held_it(hpc, shards, tmp_path, placement, job, group, suffix):
    """O57: the shard driver's step goes where the federation's own up held the agent's node: under "het" into the
    federation's job with --het-group=1, under "separate" into the agent's own held job with no het group; on the
    node the state file records for it, named after that job."""
    root, fake = up_federation(hpc, tmp_path, placement)
    plan = shards.load_plan(manifest(tmp_path, root))
    shard = plan.shards[0]
    fed = shards.federation(shard)
    name = fake.submitted["4242"][0]["--job-name"]
    assert fed == {
        "job": job,
        "name": name + suffix,
        "node": "node07",
        "placement": placement,
        "het_group": 1 if placement == "het" else None,
    }
    line = shards.step_argv(plan, shard, fed, SCRIPT)
    assert line[: 3 + len(group)] == ["srun", f"--jobid={job}", *group, "--exact"]
    assert "--overlap" not in line
    assert line[line.index("-w") + 1] == "node07"
    assert line[line.index("-J") + 1] == f"{name}{suffix}-shard-1"
