"""Helpers for the tests of side files end to end: a copy of the example whose runs write one beside each record.

`side_study` copies the example and gives the copy a small instrument, `RangeLog`, which writes `extent_ranges.json.gz`
beside each record and records its SHA-256 at `instruments.extent.side_sha256`; the runs of task `dune` make none. The
copy's study.toml declares that side file, and a second one, `surplus_ranges.json.gz`, that no run writes. The example
itself is unchanged.
"""

import gzip
import json
import subprocess
import sys
import tarfile
from pathlib import Path

from study_support import HEAD, make_copy

from expcore.provenance.codestamp import write_stamp
from expcore.replay.check import CHECK_FILE, input_files, input_hashes, inputs_sha256
from expcore.study.config import load_study

SIDE = "extent_ranges.json.gz"
SURPLUS = "surplus_ranges.json.gz"
FIELD = "instruments.extent.side_sha256"
SURPLUS_FIELD = "instruments.surplus.side_sha256"
DECLARED = [{"pattern": SIDE, "hash_field": FIELD}, {"pattern": SURPLUS, "hash_field": SURPLUS_FIELD}]
NAME = "tiny-study-package"  # the package's name, from the example's package.toml

INSTRUMENT = '''

class RangeLog:
    """A test instrument: writes the run's log of byte ranges beside its record and names it by its SHA-256."""

    namespace = "extent"

    def __init__(self, run_dir: Path, shelf: Shelf) -> None:
        self.run_dir = Path(run_dir)
        self.shelf = shelf
        self.identity: dict[str, Any] = {}

    def start(self, identity: Mapping[str, Any]) -> None:
        self.identity = dict(identity)

    def finish(self) -> dict[str, Any]:
        if self.identity["factors"]["task"] == "dune":
            return {"ok": True}  # this run makes no side file
        text = json.dumps({"run_key": self.identity["run_key"], "served": self.shelf.served}, sort_keys=True)
        data = gzip.compress(text.encode("utf-8"), mtime=0)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "extent_ranges.json.gz").write_bytes(data)
        return {"ok": True, "side_sha256": hashlib.sha256(data).hexdigest()}
'''

DECLARATION = f"""
[[study.side_files]]
pattern = "{SIDE}"
hash_field = "{FIELD}"

[[study.side_files]]
pattern = "{SURPLUS}"
hash_field = "{SURPLUS_FIELD}"
"""


def edit(path: Path, old: str, new: str) -> None:
    """Replace the first occurrence of a text that must be in the file."""
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def side_study(base: Path) -> Path:
    """A stamped copy of the example whose runs write a side file, and whose study.toml declares it."""
    root = make_copy(base, "study")
    plugin = root / "plugin.py"
    edit(plugin, "import asyncio\n", "import asyncio\nimport gzip\nimport hashlib\nimport json\n")
    edit(plugin, "\n\n# --- the stub model's script", f"{INSTRUMENT}\n\n# --- the stub model's script")
    edit(plugin, "[FactsServed(shelf)]", "[FactsServed(shelf), RangeLog(request.run_dir, shelf)]")
    (root / "study.toml").write_text((root / "study.toml").read_text(encoding="utf-8") + DECLARATION, "utf-8")
    write_stamp(root, HEAD, load_study(root).scope)  # the plugin and study.toml changed: stamp the copy again
    return root


def side_files(runs: Path) -> list[Path]:
    """Every file of the first declared name in a batch, sorted."""
    return sorted(runs.rglob(SIDE))


def record_beside(path: Path) -> Path:
    """The one record in the directory of a side file."""
    [record] = [p for p in path.parent.glob("*.json") if not p.name.endswith("attempt-marker.json")]
    return record


def stored_check(runs: Path) -> dict:
    return json.loads((runs / CHECK_FILE).read_text(encoding="utf-8"))


def rel(runs: Path, path: Path) -> str:
    return path.relative_to(runs).as_posix()


def changed_log(path: Path) -> None:
    """Replace a side file with another log, as a hand-edited one would be."""
    path.write_bytes(gzip.compress(b'{"run_key": "edited", "served": 99}', mtime=0))


def doctor(runs: Path) -> None:
    """Edit the check file to vouch for the batch as it is now, as someone covering up a change would."""
    check_file = stored_check(runs)
    patterns = check_file["hashed_only_patterns"]
    check_file["replayed"], check_file["hashed_only"] = input_files(runs, patterns)
    check_file["n_records"] = check_file["n_ok"] = len(check_file["replayed"])
    check_file["inputs"] = input_hashes(runs, patterns)
    check_file["inputs_sha256"] = inputs_sha256(check_file["inputs"])
    (runs / CHECK_FILE).write_text(json.dumps(check_file, indent=1) + "\n", encoding="utf-8")


def unpack(archive: Path, where: Path) -> Path:
    """Unpack a replication package and return its root."""
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def make_check(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """The package's own `make check`, with the interpreter that already holds its environment."""
    return subprocess.run(
        ["make", "-C", str(root), "-f", "Makefile", "check-installed", f"PYTHON={sys.executable}"],
        capture_output=True,
        text=True,
        env=env,
    )
