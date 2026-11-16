"""R04: what a study's plugin writes into `request.run_dir` beside its record, the case the offline HPC rehearsal met
(PR #328): a plugin that collects its sandbox's metadata into the run's directory.

The study is a copy of the example whose instrument `SandboxEvidence` writes `sandbox.json` (a JSON object, no record)
beside each record and names it by its SHA-256 in its snapshot. Undeclared, the file is no record and nothing accounts
for it: `check` fails each one as unexpected, never as a record that does not load, every record still replays, and
`analyze` refuses the batch. Declared as a side file in study.toml, the same file is hashed into the check and paired
with its record, and the batch passes.
"""

import json
from pathlib import Path

import pytest
from side_file_support import edit
from study_support import HEAD, Campaign, clean_environment, cli, finish, make_copy

from expcore.provenance.codestamp import write_stamp
from expcore.replay.check import CHECK_FILE, UNEXPECTED_STAGE
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

EVIDENCE = "sandbox.json"
FIELD = "instruments.sandbox.side_sha256"

INSTRUMENT = '''

class SandboxEvidence:
    """A test instrument: writes what it saw of the run's sandbox into the run's directory and names it by its hash."""

    namespace = "sandbox"

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.identity: dict[str, Any] = {}

    def start(self, identity: Mapping[str, Any]) -> None:
        self.identity = dict(identity)

    def finish(self) -> dict[str, Any]:
        seen = {"backend": "apptainer", "image": "agent.sif", "run_key": self.identity["run_key"]}
        data = json.dumps(seen, sort_keys=True).encode("utf-8")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "sandbox.json").write_bytes(data)
        return {"ok": True, "side_sha256": hashlib.sha256(data).hexdigest()}
'''

DECLARATION = f"""
[[study.side_files]]
pattern = "{EVIDENCE}"
hash_field = "{FIELD}"
"""


def evidence_study(base: Path, *, declared: bool) -> Path:
    """A stamped copy of the example whose runs write sandbox.json into their directory, declared or not."""
    root = make_copy(base, "study")
    plugin = root / "plugin.py"
    edit(plugin, "import asyncio\n", "import asyncio\nimport hashlib\nimport json\n")
    edit(plugin, "\n\n# --- the stub model's script", f"{INSTRUMENT}\n\n# --- the stub model's script")
    edit(plugin, "[FactsServed(shelf)]", "[FactsServed(shelf), SandboxEvidence(request.run_dir)]")
    if declared:
        (root / "study.toml").write_text((root / "study.toml").read_text(encoding="utf-8") + DECLARATION, "utf-8")
    write_stamp(root, HEAD, load_study(root).scope)  # the plugin (and study.toml) changed: stamp the copy again
    return root


def run(base: Path, env: dict[str, str], *, declared: bool) -> Campaign:
    campaign = Campaign(evidence_study(base, declared=declared))
    campaign.codes["run"], _ = cli(env, "run", str(campaign.root))
    return campaign


@pytest.fixture(scope="module")
def undeclared(tmp_path_factory, clean_environment) -> Campaign:
    """Run and checked; the check is expected to fail, so nothing is analysed."""
    campaign = run(tmp_path_factory.mktemp("undeclared"), clean_environment, declared=False)
    campaign.codes["check"], _ = cli(clean_environment, "check", str(campaign.runs))
    return campaign


@pytest.fixture(scope="module")
def declared(tmp_path_factory, clean_environment) -> Campaign:
    campaign = run(tmp_path_factory.mktemp("declared"), clean_environment, declared=True)
    finish(clean_environment, campaign)
    return campaign


def stored_check(campaign: Campaign) -> dict:
    return json.loads((campaign.runs / CHECK_FILE).read_text(encoding="utf-8"))


def evidence_files(campaign: Campaign) -> list[str]:
    return sorted(p.relative_to(campaign.runs).as_posix() for p in campaign.runs.rglob(EVIDENCE))


def test_R04_evidence_a_plugin_writes_into_run_dir_undeclared_is_reported_as_unexpected_never_read_as_a_record(
    undeclared, clean_environment
):
    """R04: the rehearsal's case. Every run wrote its evidence beside its record; the check lists each file as
    unexpected, fails no record at `load` for it, replays every record, and does not pass, so `analyze` refuses."""
    assert undeclared.codes["run"] == 0
    assert undeclared.codes["check"] == 1
    check = stored_check(undeclared)
    found = evidence_files(undeclared)
    assert found  # every attempt directory has one
    assert check["n_records"] == check["n_ok"] == len(found)
    assert [(f["file"], f["stage"]) for f in check["failures"]] == [(rel, UNEXPECTED_STAGE) for rel in found]
    assert not set(found) & set(check["inputs"])
    code, errors = cli(clean_environment, "analyze", str(undeclared.root))
    assert code != 0
    assert f"unexpected file{'s' if len(found) > 1 else ''} in the batch" in errors


def test_R04_the_same_evidence_declared_as_a_side_file_is_hashed_into_the_check_paired_and_accepted(declared):
    """R04 and R14: declared in study.toml, the file the plugin wrote is a side file: hashed, never replayed, paired
    with the record that names its hash, and the batch is checked, analysed and reported."""
    assert declared.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    check = stored_check(declared)
    found = evidence_files(declared)
    assert found
    assert set(found) <= set(check["hashed_only"])
    assert check["failures"] == []
    assert check["side_files"] == [{"pattern": EVIDENCE, "hash_field": FIELD}]
