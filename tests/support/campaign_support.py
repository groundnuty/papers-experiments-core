"""Shared helpers for the campaign tests: a small schema and plans over it."""

import hashlib
from pathlib import Path

from expcore.campaign.factors import FactorSchema, RunKey
from expcore.campaign.manifest import Manifest, build_fingerprint, build_manifest

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


def make_keys(schema: FactorSchema, tasks=("task-a", "task-b"), models=("m1",), seeds=(1, 2)) -> list[RunKey]:
    """Expand the plan of a schema over the given tasks, models and seeds."""
    return schema.expand(tasks, models, seeds)


def make_manifest(schema: FactorSchema, order_seed: int = 7, **plan) -> Manifest:
    """Build the manifest of a plan with a seeded order."""
    return build_manifest(make_keys(schema, **plan), order_seed)


def make_fingerprint(schema: FactorSchema, task_ids=("task-a", "task-b"), model_keys=("m1",), **changes) -> dict:
    """Return the fingerprint of a campaign over a schema; keyword arguments replace one section whole."""
    inputs = {
        "tasks": {t: sha(f"task {t}") for t in task_ids},
        "models": {m: sha(f"model {m}") for m in model_keys},
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
