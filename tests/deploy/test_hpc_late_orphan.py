"""O58: the fault relay's hole, end to end over real processes.

A real relay runs the real `hpcfed.py status` for a federation that is up (its state file from `hpcfed.py up` over the
fake Slurm), with stand-in `srun`, `squeue`, `scancel` and `sacct` programs first on its PATH. The stand-in `srun`
waits forever, as a client does while the Slurm controller stalls; the relay's bound passes, the verb is answered
`timed_out`, and the verb's whole process group is gone, the `srun` with it. Then the controller catches up: a step
appears under the exact name the verb gave it. The relay's watch cancels that step by its exact `<job>.<step>` id and
journals it, and touches no other step.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path

from expcore_onedata.fault_hpc import Request
from expcore_onedata.fault_relay import RelayClient, beat
from expcore_onedata.fault_relay_late import journal_path

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
BOUND_S = 15.0  # the verb's bound: long enough for hpcfed to start and launch its srun on a cold node
WINDOW_S, POLL_S = 12.0, 0.5

# Stand-in Slurm programs, first on the relay's PATH. Each finds its files beside itself.
STUBS = {
    # the srun a stalled controller leaves waiting: it writes its pid and arguments, then waits
    "srun": """\
        #!/bin/sh
        d=$(dirname "$0")
        echo "$$" > "$d/srun.pid"
        printf "%s\\n" "$@" > "$d/srun.argv"
        exec sleep 300
        """,
    # squeue: the job's steps as a file lists them; any other listing shows the job still exists
    "squeue": """\
        #!/bin/sh
        d=$(dirname "$0")
        case " $* " in
          *" -s "*) cat "$d/steps.txt" 2>/dev/null;;
          *) echo 4242;;
        esac
        exit 0
        """,
    # scancel: every id it is given is logged and leaves the list
    "scancel": """\
        #!/bin/sh
        d=$(dirname "$0")
        echo "$@" >> "$d/scancel.log"
        for id in "$@"; do
          grep -v "^$id " "$d/steps.txt" > "$d/steps.tmp" 2>/dev/null
          mv "$d/steps.tmp" "$d/steps.txt"
        done
        exit 0
        """,
    # sacct: the accounting as files hold it (the times form when asked for End, the steps form otherwise)
    "sacct": """\
        #!/bin/sh
        d=$(dirname "$0")
        case "$*" in
          *End*) cat "$d/sacct-times.txt" 2>/dev/null;;
          *) cat "$d/sacct.txt" 2>/dev/null;;
        esac
        exit 0
        """,
}


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def stubs(tmp_path):
    directory = tmp_path / "stubs"
    directory.mkdir()
    for name, text in STUBS.items():
        (directory / name).write_text(textwrap.dedent(text), encoding="utf-8")
        (directory / name).chmod(0o755)
    (directory / "steps.txt").write_text("", encoding="utf-8")
    return directory


@pytest.fixture
def federation(hpc, tmp_path):
    """A federation `up` over the fake Slurm: its root, a provider's service name and the allocation's name and job."""
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for file in ("onezone.tar", "oneprovider.tar"):
        (images / file).write_bytes(b"tar")
        (images / f"{file}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    site = {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}
    fake, root = FakeSlurm(), tmp_path / "fed" / "root"
    driver = hpc.cli.make_driver(LAYOUT, str(root), runner=fake)
    driver.sleep, driver.poll_s = (lambda seconds: None), 0.0
    driver.slurm.sleep, driver.slurm.poll_s = (lambda seconds: None), 0.0
    driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
    driver.relay_available = lambda: False
    driver.up(hpc.settings.site_from_environment(site))
    state = driver.store.read()
    service = next(name for name, entry in state["services"].items() if entry["kind"] == "op")
    return type(
        "Federation",
        (),
        {
            "root": root,
            "service": service,
            "prefix_name": state["allocation"]["name"],
            "job": state["allocation"]["job"],
        },
    )


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    return stat[stat.rfind(")") + 2 :].split()[0] not in ("Z", "X")


def lines_of(root) -> list[dict]:
    path = journal_path(root)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def wait_for(test, seconds=60.0, what="the condition"):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        found = test()
        if found:
            return found
        time.sleep(0.1)
    raise AssertionError(f"{what} did not come within {seconds:g} s")


def test_o58_an_srun_that_waits_past_the_bound_is_gone_and_the_step_it_would_make_is_cancelled_by_its_exact_id(
    federation, stubs, tmp_path
):
    """O58: the verb is answered `timed_out`; its srun, which was still waiting, does not survive the bound; a step that
    appears afterwards under the verb's exact name (and the tag the relay gave it) is cancelled by its exact id and
    recorded; a step of another tag or another word is never touched."""
    root, service, job = federation.root, federation.service, federation.job
    prefix = [sys.executable, "-I", "-B", str(HPC / "hpcfed.py"), "--layout", str(LAYOUT), "--root", str(root)]
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(tmp_path), "LC_ALL": "C"}
    log = tmp_path / "relay.log"
    with log.open("wb") as sink:
        relay = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "expcore_onedata.fault_relay",
                "serve",
                "--root",
                str(root),
                "--late-window-s",
                str(WINDOW_S),
                "--late-poll-s",
                str(POLL_S),
                "--",
                *prefix,
            ],
            env=env,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
        )
    try:
        wait_for(lambda: beat(root) is not None or relay.poll() is not None, 30, "the relay's heartbeat")
        assert relay.poll() is None, log.read_text(encoding="utf-8")

        # the verb: its srun waits on a controller that does not answer, past the relay's bound
        with pytest.raises(subprocess.TimeoutExpired) as answered:
            RelayClient(root, wait_s=60)(Request("status", service), timeout_s=BOUND_S)  # answered `timed_out`
        pid = int((stubs / "srun.pid").read_text(encoding="utf-8"))
        assert not alive(pid), "the verb's srun outlived the relay's bound"
        words = (stubs / "srun.argv").read_text(encoding="utf-8").split()
        name = words[words.index("-J") + 1]
        (opened,) = [line for line in lines_of(root) if line["event"] == "watch_opened"]
        tag = opened["tag"]
        assert name == f"{federation.prefix_name}-{service}-status-{tag}", "the verb did not name its step by the tag"
        assert opened["why"] == "timed_out"
        assert opened["window_s"] == WINDOW_S
        assert answered.value.timing["accounting"] in (
            "read",
            "unreadable",
            "skipped",
        )  # the answer says where time went

        # the controller catches up: the step is made, under the verb's exact name; others are listed too
        other = f"{federation.prefix_name}-{service}-status-{'0' * 12}"
        exec_word = f"{federation.prefix_name}-{service}-exec-{tag}"
        (stubs / "steps.txt").write_text(f"{job}.9 {name}\n{job}.8 {other}\n{job}.7 {exec_word}\n", encoding="utf-8")
        closed = wait_for(lambda: [e for e in lines_of(root) if e["event"] == "watch_closed"], 60, "the watch's end")
        assert (stubs / "scancel.log").read_text(encoding="utf-8").split() == [f"{job}.9"]
        late = [line for line in lines_of(root) if line["event"] == "late_step"]
        assert [(e["step"], e["id"], e["cancelled"]) for e in late] == [(name, f"{job}.9", True)]
        assert closed[-1]["outcome"] == "late_steps"
        assert (stubs / "steps.txt").read_text(encoding="utf-8").split("\n")[:-1] == [
            f"{job}.8 {other}",
            f"{job}.7 {exec_word}",
        ]
    finally:
        relay.terminate()
        relay.wait(timeout=30)
