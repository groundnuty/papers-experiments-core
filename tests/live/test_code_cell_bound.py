"""The per-cell kill (L18) against a real Docker daemon, skipped unless every gate below is set.

    EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_CELL_BOUND=1 \\
    EXPCORE_LIVE_SANDBOX_POOL=<a private IPv4 range the host routes nowhere> \\
    [EXPCORE_LIVE_AGENT_IMAGE=<an image on the host with python, timeout and sleep>] \\
    [EXPCORE_LIVE_WORK_ROOT=<a directory the run's files go under>] \\
    [EXPCORE_LIVE_FACTS_OUT=<file>] [EXPCORE_LIVE_RECORD_OUT=<file>] \\
    uv run pytest tests/live/test_code_cell_bound.py

One scripted attempt over a real sandbox with no data path (no federation, no token, no mount, no gate): the study
declares `code_cell_bound_s = 60`, and the agent's first cell starts a child and a detached grandchild and sleeps 180 s.
The cell must be killed at the bound with every process gone, the agent must be told the limit, and the run must go on
to a second cell and the final answer (`tests/support/cell_bound_live.py` says what is asserted, and the offline test
of the scenario runs it over a fake host). The image is never pulled. The sandbox's privileged steps run as
`sudo -n` through the host object, which logs each one (its program words only) into the facts: the kill of the
cell's processes, the reads of their state, the teardown's removals. `EXPCORE_LIVE_FACTS_OUT` names the file the facts
go to as JSON, and `EXPCORE_LIVE_RECORD_OUT` the attempt's record: outcomes, counts and seconds, never a token, an
address, a name or a path.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

GATES = ("EXPCORE_LIVE_DOCKER", "EXPCORE_LIVE_CELL_BOUND")
if any(os.environ.get(gate) != "1" for gate in GATES) or not os.environ.get("EXPCORE_LIVE_SANDBOX_POOL"):
    pytest.skip(
        "EXPCORE_LIVE_DOCKER=1, EXPCORE_LIVE_CELL_BOUND=1 and EXPCORE_LIVE_SANDBOX_POOL are needed: this test starts "
        "a container and runs a cell for a minute",
        allow_module_level=True,
    )

from cell_bound_live import BOUND_S, GRACE_S, problems, run_scenario  # noqa: E402 -- only once the module runs

from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.sandbox.api import DataPathMode, SandboxError, SandboxSpec  # noqa: E402
from expcore_onedata.sandbox.hostops import DockerHost  # noqa: E402
from expcore_onedata.sandbox.sandbox import DockerSandbox  # noqa: E402
from expcore_onedata.testing import write_platform  # noqa: E402

AGENT_IMAGE = os.environ.get("EXPCORE_LIVE_AGENT_IMAGE") or "python:3.12-slim"
LEASE_KEY = "live-cell-bound"  # labels the sandbox's containers, network and directories, so a probe can find them
LABEL_NAMESPACE = "org.expcore.live"


class LoggedHost(DockerHost):
    """The real host, noting the program of every privileged step it runs: the facts name the sudo steps by it."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.privileged: list[str] = []

    def run(self, argv, timeout_s, *, sudo=False, input=None):
        if sudo:
            self.privileged.append(" ".join(str(word) for word in argv[:2] if not str(word).startswith("/")))
        return super().run(argv, timeout_s, sudo=sudo, input=input)


def sandbox_table(work_root: Path, image: str, image_id: str) -> dict:
    """The `[sandbox]` table of one level with no data path, on the pool the host routes nowhere."""
    return {
        "work_root": str(work_root),
        "name_prefix": "cbk",
        "label_namespace": LABEL_NAMESPACE,
        "agent_uid": 10001,
        "python": "python",
        "sudo": ["sudo", "-n"],
        "images": {"agent": {"name": image, "id": image_id}},
        "network": {"pools": [os.environ["EXPCORE_LIVE_SANDBOX_POOL"]], "prefix": 28, "max_attempts": 64},
        "limits": {
            "cpus": 1.0,
            "memory_bytes": 2 << 30,
            "pids": 256,
            "nofile": 1024,
            "tmp_bytes": 256 << 20,
            "shm_bytes": 64 << 20,
            "work_disk_bytes": 256 << 20,
        },
        "data_paths": {"none": "none"},
        "kill_grace_s": GRACE_S,
    }


def top_look(host: DockerHost, container: str, marker: str):
    """The independent look: the container's own process list, from the docker daemon, filtered for the marker."""

    def look() -> list[str]:
        listed = host.docker("top", container, "-eo", "pid,args", check=False, timeout=30)
        return [line for line in listed.stdout.splitlines() if marker in line]

    return look


@pytest.fixture(scope="module")
def facts(tmp_path_factory):
    """The whole attempt, once; the facts each test reads."""
    base = tmp_path_factory.mktemp("cell-bound-live")
    try:
        image_id = DockerHost().image_id(AGENT_IMAGE)
    except SandboxError:
        pytest.skip("the agent image is not on the host; it is never pulled here")
    work_root = Path(os.environ.get("EXPCORE_LIVE_WORK_ROOT") or base / "work")
    config = load_platform(
        write_platform(base, lambda data: data.update(sandbox=sandbox_table(work_root, AGENT_IMAGE, image_id))),
        require_secrets=False,
    )
    spec = SandboxSpec(run_key="expcore-live-cell-bound", mode=DataPathMode.NONE, lease_key=LEASE_KEY)
    host = LoggedHost(sudo_command=config.sandbox.sudo)
    sandbox = DockerSandbox.start(host, config.sandbox, spec)
    marker = "cellbound-live"
    record_out = os.environ.get("EXPCORE_LIVE_RECORD_OUT")
    left: dict = {}
    try:
        found = run_scenario(
            sandbox,
            top_look(host, sandbox.names.agent, marker),
            marker,
            record_out=Path(record_out) if record_out else None,
        )
    finally:
        try:
            sandbox.stop()
            left["stopped"] = True
        except SandboxError as error:
            left["stop_error"] = type(error).__name__
        labelled = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label={LABEL_NAMESPACE}"], capture_output=True, text=True, check=False
        )
        left["containers_left"] = len(labelled.stdout.split())
    found["sudo_steps"] = sorted(set(host.privileged))
    found["after_stop"] = left
    if os.environ.get("EXPCORE_LIVE_FACTS_OUT"):
        Path(os.environ["EXPCORE_LIVE_FACTS_OUT"]).write_text(json.dumps(found, indent=1, default=str), "utf-8")
    return found


def test_l18_the_cell_is_killed_at_the_bound_the_agent_is_told_and_the_run_goes_on(facts):
    """L18, O34: the eight points of the proof: the cell's processes seen, the call back at the bound, a clean deadline,
    none of them left, the limit named to the agent, the next cell run, the run finished, the record replayed."""
    assert problems(facts, grace_s=GRACE_S) == []
    assert facts["call_1"]["elapsed_s"] < facts["sleep_s"]


def test_l18_nothing_of_the_sandbox_is_left_after_its_stop(facts):
    """L18: the sandbox stopped, and no container of the run's label is left."""
    assert facts["after_stop"] == {"stopped": True, "containers_left": 0}


def test_l18_the_bound_is_the_declared_one(facts):
    assert facts["bound_s"] == BOUND_S
