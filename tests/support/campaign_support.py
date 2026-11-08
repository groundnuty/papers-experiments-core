"""Shared helpers for the campaign tests: a small schema and plans over it."""

import hashlib
from pathlib import Path

from expcore.campaign.factors import FactorSchema, RunKey
from expcore.campaign.manifest import Manifest, build_fingerprint, build_manifest
from expcore.campaign.markers import AttemptResult
from expcore.campaign.store import Store
from expcore.loop.outcomes import HarnessDefect, Outcome

NOW = "2026-01-01T00:00:00Z"
LATER = "2026-01-01T00:10:00Z"

STUDY_TOML = """\
[factors.data_path]
levels = ["mount", "api", "download"]

[factors.tools]
levels = ["none", "read", "write"]

[identity]
order = ["task", "data_path", "tools", "model", "seed"]

[conditions.mount-read]
data_path = "mount"
tools = "read"

[conditions.api-none]
data_path = "api"
tools = "none"

[conditions.download-write]
data_path = "download"
tools = "write"
"""


def sha(text: str) -> str:
    """Return a 64-character hex digest of a text, for fingerprint inputs."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_schema() -> FactorSchema:
    """Return a schema with two declared factors and three conditions."""
    return FactorSchema(
        {"data_path": ["mount", "api", "download"], "tools": ["none", "read", "write"]},
        ["task", "data_path", "tools", "model", "seed"],
        {
            "mount-read": {"data_path": "mount", "tools": "read"},
            "api-none": {"data_path": "api", "tools": "none"},
            "download-write": {"data_path": "download", "tools": "write"},
        },
    )


def treatment_schema(kinds=("endpoint",), condition: str = "api-none") -> FactorSchema:
    """Return the standard schema with one condition declaring infrastructure kinds as its treatment."""
    base = make_schema()
    return FactorSchema(base.declared, base.order, base.conditions, treatment={condition: kinds})


def keys_under(schema: FactorSchema, manifest: Manifest, condition: str) -> list[str]:
    """Return the run keys of a condition, in manifest order."""
    return [row.key.text for row in manifest.rows if schema.condition_of(row.key) == condition]


def make_keys(schema: FactorSchema, tasks=("task-a", "task-b"), models=("m1",), seeds=(1, 2)) -> list[RunKey]:
    """Expand the plan of a schema over the given tasks, models and seeds."""
    return schema.expand(tasks, models, seeds)


def make_manifest(schema: FactorSchema, order_seed: int = 7, **plan) -> Manifest:
    """Build the manifest of a plan with a seeded order."""
    return build_manifest(make_keys(schema, **plan), order_seed)


def make_fingerprint(
    schema: FactorSchema, task_ids=("task-a", "task-b"), model_keys=("m1",), seed_ids=(1, 2), **changes
) -> dict:
    """Return the fingerprint of a campaign over a schema; keyword arguments replace one section whole."""
    inputs = {
        "tasks": {t: sha(f"task {t}") for t in task_ids},
        "models": {m: sha(f"model {m}") for m in model_keys},
        "seeds": list(seed_ids),
        "caps": {"attempt_limit": 3, "time_cap_s": 600},
        "prompts": {c: sha(f"prompt {c}") for c in schema.conditions},
        "tools": {c: sha(f"tools {c}") for c in schema.conditions},
        "scorer_digest": sha("scorer"),
        "configs": {"study.toml": sha("study"), "models.toml": sha("models"), "campaign.toml": sha("campaign")},
    }
    inputs.update(changes)
    return build_fingerprint(schema, **inputs)


def write_study(tmp_path: Path, text: str = STUDY_TOML) -> Path:
    """Write a study file and return its path."""
    path = tmp_path / "study.toml"
    path.write_text(text, encoding="utf-8")
    return path


def finished(**fields) -> AttemptResult:
    """Return the result of an attempt the agent finished."""
    return AttemptResult(Outcome("finished"), LATER, fields.pop("wall_s", 12.5), **fields)


def infra(kind: str = "endpoint", detail: str = "HTTP 503 after 5 retries", **fields) -> AttemptResult:
    """Return the result of an attempt that ended on an infrastructure failure of a kind."""
    return AttemptResult(Outcome("infrastructure", detail, infra_kind=kind), LATER, 3.0, **fields)


def defect(**fields) -> AttemptResult:
    """Return the result of an attempt that crashed in our own code."""
    outcome = Outcome("harness_defect", "boom", defect=HarnessDefect("ValueError", "a" * 64))
    return AttemptResult(outcome, LATER, 1.0, **fields)


def open_store(tmp_path: Path, schema: FactorSchema | None = None, name: str = "campaign.db") -> Store:
    """Open a store in a temporary directory."""
    return Store(tmp_path / name, schema or make_schema())


def planned_store(tmp_path: Path, **plan):
    """Open a store holding the runs of a small plan; return the store, the schema and the manifest."""
    schema = make_schema()
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema, **plan)
    store.sync_manifest(manifest.rows)
    return store, schema, manifest


def run_one(store: Store, key: str, result: AttemptResult, max_attempts: int = 3, run_dir: str = "") -> str:
    """Begin an attempt of a run and finish it with a result; return the run's status."""
    begun = store.begin_attempt(key, NOW, 1234, run_dir)
    return store.finish_attempt(begun.id, result, max_attempts)
