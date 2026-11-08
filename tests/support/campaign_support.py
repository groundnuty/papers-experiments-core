"""Shared helpers for the campaign tests: a small schema and plans over it."""

import dataclasses
import hashlib
import json
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


def chain_schema(tasks=("task-a", "task-b"), condition: str = "download-write") -> FactorSchema:
    """Return the standard schema in which one condition runs on a subset of the plan's tasks (a chain design)."""
    base = make_schema()
    return FactorSchema(base.declared, base.order, base.conditions, tasks={condition: tasks})


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


def open_in_rounds(base: str, rounds: int, barrier, results) -> None:
    """Run in a child process: in each round wait at the barrier, open the round's new database, report what it got.

    A report is `(journal mode, synchronous, None)` for an open that worked and `(None, None, repr(error))` for one that
    raised. The barrier is entered in every round, whatever happened in the last, so one failure never leaves the
    others waiting.
    """
    schema = make_schema()
    for index in range(rounds):
        barrier.wait(timeout=120)
        try:
            store = Store(Path(base) / f"round{index}" / "campaign.db", schema)
            report = (store.q("PRAGMA journal_mode")[0][0], store.q("PRAGMA synchronous")[0][0], None)
            store.close()
        except Exception as error:  # reported to the parent, which fails on it
            report = (None, None, repr(error))
        results.put((index, report))


_TEMPLATES: dict[str, bytes] = {}  # plan -> the bytes of a closed database holding it


def planned_store(tmp_path: Path, **plan):
    """Open a store holding the runs of a small plan; return the store, the schema and the manifest.

    Creating a database costs a dozen fsyncs, so the first call for a plan builds it and later calls copy the closed
    file: the store they open is the same, and a test that opens many does not wait on the disk each time.
    """
    schema = make_schema()
    manifest = make_manifest(schema, **plan)
    name = repr(sorted(plan.items()))
    if name not in _TEMPLATES:
        built = open_store(tmp_path, schema, name="template.db")
        built.sync_manifest(manifest.rows)
        built.close()
        _TEMPLATES[name] = (tmp_path / "template.db").read_bytes()
        (tmp_path / "template.db").unlink()
        for leftover in tmp_path.glob("template.db-*"):
            leftover.unlink()
    (tmp_path / "campaign.db").write_bytes(_TEMPLATES[name])
    return open_store(tmp_path, schema), schema, manifest


def run_one(store: Store, key: str, result: AttemptResult, max_attempts: int = 3, run_dir: str = "") -> str:
    """Begin an attempt of a run and finish it with a result; return the run's status.

    A runner passes the treatment kinds its record carried. For a result that names none, this passes the run's
    condition's own, as a runner that built its record from `treatment_kinds_of` would.
    """
    if not result.treatment_kinds:
        factors = json.loads(store.q("SELECT factors FROM runs WHERE run_key=?", (key,))[0]["factors"])
        kinds = store.schema.treatment_kinds[store.schema.condition_named(factors, key)]
        result = dataclasses.replace(result, treatment_kinds=kinds)
    begun = store.begin_attempt(key, NOW, 1234, run_dir)
    return store.finish_attempt(begun.id, result, max_attempts)
