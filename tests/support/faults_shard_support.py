"""Helpers for the faults-in-shards rehearsal (tests/study/test_faults_in_model_shards.py): a campaign split by model,
each shard run against a fake federation of its own, with a real provider outage injected through the Onedata layer's
`FaultInjector`.

A `Federation` is a root with its own `platform.toml`, fault journal and fake Compose services (`fault_support`), and
its own Compose project name. `make_study` copies the example study, puts `faults_shard_plugin.py` in it as
`fault_plugin.py`, makes the `relay-down` condition's treatment `provider_outage` on `providers[1]`, and, given a
federation, writes the platform file beside `study.toml` with `[faults]` naming that federation's root (the journal in
full, as a platform file in a study directory must). Nothing of a shard is written by hand: `run_shard` runs it with
`run --shard i/N`, as a host would.
"""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fault_support import SERVICES, FakeServices, write_faults
from shard_support import make_study as make_example
from study_support import HEAD, Campaign, cli

from expcore.provenance.codestamp import write_stamp
from expcore.records.injections import FAULT_NAMESPACE
from expcore.study.config import load_study
from expcore_onedata.fault_journal import JOURNAL

SOURCE = Path(__file__).resolve().parent / "faults_shard_plugin.py"
PLUGIN = "fault_plugin.py"
JOURNAL_DIR = "fault-journal"
STATE_FILE = "services.json"  # the plugin's: the federation's fake Compose services
TARGET_SERVICE = SERVICES["beta"]  # what the example's `providers[1]` is
OTHER_SERVICE = SERVICES["alpha"]
EDITS = (  # study.toml: the plugin, the platform module whose kinds the study names, and the treatment's own kind
    ('plugin = "plugin.py"', f'plugin = "{PLUGIN}"\nplatforms = ["expcore_onedata.sandbox.kinds"]'),
    ('"plugin.py", "prompt.txt"]', f'"plugin.py", "{PLUGIN}", "prompt.txt"]'),
    (
        'infrastructure_is_treatment = ["knowledge"]',
        'infrastructure_is_treatment = [{ kind = "provider_outage", target = "providers[1]" }]',
    ),
)


@dataclass(frozen=True)
class Federation:
    """One fake federation: its root, its Compose project and the `[faults]` settings it has beyond the fixture's."""

    root: Path
    project: str
    faults: dict[str, Any] = field(default_factory=dict)

    @property
    def journal(self) -> Path:
        return self.root / JOURNAL_DIR

    def make(self) -> "Federation":
        """Make the root, holding its own `platform.toml` (the file `up` renders), and return it."""
        self.root.mkdir(parents=True, exist_ok=True)
        write_faults(self.root, project=self.project, **self.faults)
        return self

    def services(self) -> FakeServices:
        """The federation's Compose services as they stand, read from its root."""
        return FakeServices(path=self.root / STATE_FILE)

    def lines(self) -> list[dict[str, Any]]:
        """Every line of the federation's fault journal, in order."""
        path = self.journal / JOURNAL
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def injections(self) -> list[dict[str, Any]]:
        """Each injection of the journal: its `injecting` line (the target, the owner) and whether it was seen down
        (`injected`) and brought back and verified (`cleared`)."""
        lines = self.lines()
        seen = {kind: {line["id"] for line in lines if line["event"] == kind} for kind in ("injected", "cleared")}
        return [
            {**line, "down": line["id"] in seen["injected"], "cleared": line["id"] in seen["cleared"]}
            for line in lines
            if line["event"] == "injecting"
        ]


def make_study(base: Path, name: str, extra: str = "", federation: Federation | None = None) -> Path:
    """A stamped copy of the example whose `relay-down` condition is a real outage of `providers[1]`.

    With a `federation` the study has its `platform.toml`, beside `study.toml`; without one (the merge target, whose
    commands run no attempt) it has none, and its plugin is the example's. Every copy has the same plugin file and the
    same `study.toml`, so they plan one campaign; only the platform file differs between federations, and it is in no
    `[study] code` list.
    """
    root = make_example(base, name, extra)
    text = (root / "study.toml").read_text(encoding="utf-8")
    for old, new in EDITS:
        assert text.count(old) == 1, f"the example's study.toml no longer holds {old!r}"
        text = text.replace(old, new)
    (root / "study.toml").write_text(text, encoding="utf-8")
    shutil.copyfile(SOURCE, root / PLUGIN)
    if federation is not None:
        federation.make()
        write_faults(
            root,
            project=federation.project,
            journal=str(federation.journal),
            compose_file=str(federation.root / "compose.yaml"),
            **federation.faults,
        )
    write_stamp(root, HEAD, load_study(root).scope)  # the study's files changed: stamp the copy again
    return root


def run_whole(base: Path, name: str, env: dict[str, str], federation: Federation, extra: str = "") -> Campaign:
    """One driver of the whole campaign against one federation; the campaign is run and not yet checked."""
    campaign = Campaign(make_study(base, name, extra, federation))
    campaign.codes["run"], errors = cli(env, "run", str(campaign.root))
    assert campaign.codes["run"] == 0, errors
    return campaign


def run_shard(
    base: Path, name: str, env: dict[str, str], index: int, federation: Federation, *, count: int = 3, extra: str = ""
) -> Campaign:
    """Shard `index` of `count`, run by the command line against `federation`, to the end; not checked."""
    root = make_study(base, name, extra, federation)
    code, errors = cli(env, "run", str(root), "--shard", f"{index}/{count}", "--shard-label", f"shard-{index}")
    assert code == 0, errors
    return Campaign(root)


def attempts_of(campaign: Campaign) -> list[dict[str, Any]]:
    """Every attempt a store holds: its row id, run key, state, whether it was a treatment attempt, its kind of
    infrastructure failure and the admission it held."""
    query = "SELECT id, run_key, state, treatment, infra_kind, admission FROM attempts ORDER BY id"
    return [dict(row) for row in campaign.sql(query)]


def outage_attempts(campaign: Campaign) -> list[dict[str, Any]]:
    """The attempts that held the federation exclusively: the outage runs', since no other condition declares it."""
    return [row for row in attempts_of(campaign) if row["admission"] == "exclusive"]


def injector_readings(campaign: Campaign) -> list[dict[str, Any]]:
    """The fault injector's reading in every record of the campaign's batch that holds one: an outage run's record, with
    the injector's own account of whether its restore was verified."""
    found = []
    for path in sorted(campaign.runs.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        instruments = data.get("instruments") if isinstance(data, dict) else None
        if isinstance(instruments, dict) and FAULT_NAMESPACE in instruments:
            found.append(instruments[FAULT_NAMESPACE])
    return found
