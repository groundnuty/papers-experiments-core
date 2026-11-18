"""The outputs of today's merge, and of one driver, with what a clock or a host writes in them masked (C45).

A merge by run (`merge --ledger`, `expcore-merge/2`) is a new form beside the merge of shards (`expcore-merge/1`) and
the unmerged campaign. Its code sits beside theirs in the check, the analysis, the export and the report, so this
module runs each old form for real (two shards of the example study merged, and one driver of the whole) through
`check`, `analyze` and `report`, and gives the bytes of `merge.json`, `export.json`, `analysis.json` and `report.md`
with every value a clock, a host or a SHA-256 of clock-bearing bytes decides replaced by a mark. What is left is what
the code decides, and `tests/data/merged-forms-pins.json` holds it as the core made it before the merge by run existed.
"""

import json
import re
from pathlib import Path
from typing import Any

from shard_support import make_study, merge_into, run_shard
from study_support import Campaign, cli, finish

FORMS = ("merged-shards", "unsharded")
OUTPUTS = ("merge.json", "export.json", "analysis.json", "report.md")
MASK = "<volatile>"
# keys whose values a clock decides, or the hash of bytes that hold a clock reading (a snapshot of a store)
VOLATILE_KEYS = frozenset(
    {
        "started_utc",
        "started_at",
        "ended_at",
        "ended_utc",
        "wall_s",
        "turn_latency_s",
        "lease_wait_s",
        "snapshot_sha256",
        "core_version",
        "window",
        # the host's installed distributions, its Python and the core's commit, and every hash taken over them: the
        # campaign fingerprint's `environment` and the fingerprint's SHA-256 (merge.json's `fingerprint`, the
        # export's `campaign_fingerprint.sha256`)
        "environment",
        "sha256",
    }
)
_HASHED_WHOLE = "fingerprint"  # merge.json's: the SHA-256 of a fingerprint that holds the environment
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?")
_SHARD_ROW = re.compile(r"\| shard-\d+ \| ")


def _masked(value: Any, key: str | None = None) -> Any:
    """A JSON value with every volatile leaf replaced by the mark, recursively."""
    if key in VOLATILE_KEYS or (key == _HASHED_WHOLE and isinstance(value, str)):
        return MASK
    if isinstance(value, dict):
        return {name: _masked(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_masked(item) for item in value]
    if isinstance(value, str):
        return _TIME.sub(MASK, value)
    return value


def normalized(path: Path) -> str | None:
    """The file's text with its volatile values masked; None when it is not there (merge.json of one driver)."""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.dumps(_masked(json.loads(text)), indent=1, sort_keys=False)
    lines = [_shard_timing_masked(line) for line in text.split("\n")]
    return _TIME.sub(MASK, "\n".join(lines))


def _shard_timing_masked(line: str) -> str:
    """A row of the report's shards table with its two wall-time cells (the shard's median and longest run) masked."""
    if not _SHARD_ROW.match(line):
        return line
    cells = line.split(" | ")
    return " | ".join(MASK if position in (2, 3) else cell for position, cell in enumerate(cells))


def outputs_of(campaign: Campaign) -> dict[str, str | None]:
    """The normalized outputs of a finished, checked, analysed and reported campaign, by file name."""
    return {name: normalized(campaign.root / "out" / name) for name in OUTPUTS}


def run_form(form: str, base: Path, env: dict[str, str]) -> Campaign:
    """One old form of the example study, run, merged where it is merged, checked, analysed and reported."""
    if form == "merged-shards":
        shards = [run_shard(base, f"{form}-{index}", env, index) for index in (1, 2)]
        campaign = Campaign(make_study(base, form))
        campaign.codes["merge"], errors = merge_into(env, campaign, *shards)
        assert campaign.codes["merge"] == 0, errors
    elif form == "unsharded":
        campaign = Campaign(make_study(base, form))
        campaign.codes["run"], errors = cli(env, "run", str(campaign.root))
        assert campaign.codes["run"] == 0, errors
    else:
        raise ValueError(form)
    finish(env, campaign)
    assert set(campaign.codes.values()) == {0}, campaign.codes
    return campaign


def collect(base: Path, env: dict[str, str]) -> dict[str, dict[str, str | None]]:
    """The normalized outputs of every old form, by form name."""
    return {form: outputs_of(run_form(form, base, env)) for form in FORMS}
