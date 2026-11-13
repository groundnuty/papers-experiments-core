"""What the shard rehearsal adds to `shard_support` (tests/e2e/test_shard_rehearsal.py): a study copy whose plugin hands
the core an environment identity read from the driver's environment, so a driver can be a process of its own; the start
of such a driver; and the comparison of a merged batch with one driver's.

The in-process shards of `shard_support` (`run_shard`, `plant_run`, `without_run`, `merge_into`) serve the faults the
tests plant in a finished shard; this module serves only what needs a real process.
"""

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import shard_support
from study_support import HEAD

from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study

COUNT = 2
LABELS = {1: "shard-1", 2: "shard-2"}
IDENTITY_VARIABLE = "REHEARSAL_ENVIRONMENT_IDENTITY"
PLUGIN = "rehearsal_plugin.py"
RECORD = re.compile(r"[0-9a-f]{64}\.\d+\.json")
SHARD_FIELDS = ("shard", *shard_support.MERGED_ONLY)  # what a merged batch adds to an export, at any depth
ORDER_FIELDS = ("dispatch_seq", "started_at", "lease_wait_s")  # where and when a run was dispatched: per driver
CONDITIONS = "## Conditions"  # the report's table whose last cell is a median dispatch position, which a driver counts

WRAPPER = '''"""The example's plugin, and the environment identity its driver is given."""

import dataclasses
import json
import os

from .plugin import build as example_build


def read_identity():
    """The identity a test hands this driver: names mapped to SHA-256 digests."""
    return json.loads(os.environ.get("REHEARSAL_ENVIRONMENT_IDENTITY", "{}"))


def build(context):
    return dataclasses.replace(example_build(context), environment_identity=read_identity)
'''


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


IDENTITY = {**shard_support.IDENTITY, "ingest-space-1": digest("manifest-1")}
OTHER_IDENTITY = {**IDENTITY, "ingest-space-1": digest("manifest-1 with one file changed")}


def replace_once(path: Path, old: str, new: str) -> None:
    """Replace one exact piece of a file, which must occur exactly once."""
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{path.name}: {old!r} occurs {text.count(old)} times"
    path.write_text(text.replace(old, new), encoding="utf-8")


def make_driver_study(base: Path, name: str, *, time_cap_s: int | None = None) -> Path:
    """`shard_support.make_study` (caps that N = 2 can share), with a plugin that hands the core an identity.

    Every copy of one campaign is made the same way, so they share one fingerprint; another `time_cap_s` changes it.
    """
    root = shard_support.make_study(base, name)
    if time_cap_s is not None:  # a cap of the whole campaign: in the fingerprint, and the stub never reaches it
        replace_once(root / "campaign.toml", "time_cap_s = 30\n", f"time_cap_s = {time_cap_s}\n")
    (root / PLUGIN).write_text(WRAPPER, encoding="utf-8")
    replace_once(root / "study.toml", 'plugin = "plugin.py"', f'plugin = "{PLUGIN}"')
    replace_once(root / "study.toml", '"plugin.py", "prompt.txt"]', f'"plugin.py", "{PLUGIN}", "prompt.txt"]')
    write_stamp(root, HEAD, load_study(root).scope)
    return root


def start_driver(
    study: Path,
    env: Mapping[str, str],
    *,
    shard: tuple[int, int] | None = None,
    identity: Mapping[str, str] = IDENTITY,
) -> "subprocess.Popen[str]":
    """Start `python -m expcore run` as a process of its own and return it without waiting: a shard is one driver."""
    command = [sys.executable, "-m", "expcore", "run", str(study)]
    if shard is not None:
        index, count = shard
        command += ["--shard", f"{index}/{count}", "--shard-label", LABELS[index]]
    environment = {**env, IDENTITY_VARIABLE: json.dumps(identity)}
    return subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for(driver: "subprocess.Popen[str]") -> tuple[int, str]:
    """The exit status of a driver and what it printed, once it ends."""
    try:
        out, errors = driver.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        driver.kill()
        driver.communicate()
        raise
    return driver.returncode, out + errors


def record_files(runs: Path) -> list[Path]:
    """The records of a batch, never an attempt marker or the replay check."""
    return sorted(p for p in runs.rglob("*.json") if RECORD.fullmatch(p.name))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- what a merged batch must equal ----------------------------------------------------------------------------------


def apart_from_where_and_when(export: Mapping[str, Any]) -> Any:
    """An export without what is per driver: what a merged export adds (the shard tags, each shard's own set-asides,
    and the run times of each analysed entry, `shard_support.MERGE_TIMES`), and where and when a driver dispatched a
    run, at any depth. The run times go from the analysed entries only, since `discarded_usage` sums a `wall_s` that
    every export has and that must still agree."""
    times = shard_support.MERGE_TIMES
    analysed = [{k: v for k, v in entry.items() if k not in times} for entry in export["analysed_attempts"]]
    return _without_per_driver({**export, "analysed_attempts": analysed})


def _without_per_driver(value: Any) -> Any:
    dropped = SHARD_FIELDS + ORDER_FIELDS
    if isinstance(value, dict):
        return {k: _without_per_driver(v) for k, v in value.items() if k not in dropped}
    if isinstance(value, list):
        return [_without_per_driver(v) for v in value]
    return value


def without_dispatch_positions(report: str) -> str:
    """The report with the last cell of every row of the conditions table cut off: a condition's median dispatch
    position is a place in one driver's order, so two drivers count it differently."""
    assert CONDITIONS in report, "the report has no conditions table"
    before, _, after = report.partition(CONDITIONS)
    section, boundary, rest = after.partition("\n#")
    rows = [line.rsplit(" | ", 1)[0] if line.startswith("| ") else line for line in section.splitlines()]
    return before + CONDITIONS + "\n".join(rows) + boundary + rest
