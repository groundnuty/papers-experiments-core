# Provenance: new
"""Loading campaign.toml: the plan's models and seeds, the loop's caps, the scheduler's limits and the budget.

The file is validated when it is loaded, with messages that name the file and the key; a misspelt key is refused.

    order_seed = 20260101         # the seed of the run order (C08)
    seeds = [1, 2]                # the integer seeds of the plan
    models = ["model-a", "model-b"]   # model keys of models.toml

    [agent]                       # the loop's caps for every run
    max_tool_rounds = 8
    time_cap_s = 120
    token_budget = 50000
    output_cap_chars = 4000
    max_output_tokens = 1024
    malformed_policy = "report"   # optional: "report" (the default) or "end"

    [scheduler]                   # optional: every key of `SchedulerConfig` that is a number, a flag or a kind
    max_attempts = 3
    workers = 1
    lease_timeout_s = 600

    [scheduler.unit_caps]         # optional: runs in flight at once per unit
    pool-a = 2

    [budget]                      # optional: without it runs are not held for cost
    max_usd = 20.0
    per_model_usd = { model-a = 10.0 }

    [endpoints.model-a]           # optional: the unit a model's runs count in (default: the model's own key)
    unit = "pool-a"
    base_url = "https://llm.example.test/v1"
"""

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from expcore.campaign.executor import Endpoint
from expcore.campaign.gate import Budget
from expcore.campaign.settings import SchedulerConfig
from expcore.config.loading import ConfigError, Section, load_toml
from expcore.config.models import Limits
from expcore.loop.config import MALFORMED_POLICIES, AgentConfig

_TOP_KEYS = ("order_seed", "seeds", "models", "agent", "scheduler", "budget", "endpoints")
_AGENT_KEYS = ("max_tool_rounds", "time_cap_s", "token_budget", "output_cap_chars", "max_output_tokens")
_SCHEDULER_KINDS: dict[str, type] = {
    "max_attempts": int,
    "workers": int,
    "retry_delay_s": float,
    "poll_interval_s": float,
    "unit_fail_threshold": int,
    "unit_pause_s": float,
    "health_kind": str,
    "identity_kind": str,
    "recheck_s": float,
    "health_timeout_s": float,
    "max_defer_s": float,
    "lease_timeout_s": float,
    "exit_when_blocked": bool,
    "allow_growth": bool,
    "refuse_unpriced": bool,
}


@dataclass(frozen=True)
class CampaignConfig:
    """What campaign.toml holds, as values: the plan's models and seeds, the loop's caps, the scheduler's limits."""

    source: str
    order_seed: int
    seeds: tuple[int, ...]
    models: tuple[str, ...]
    agent: AgentConfig
    scheduler: SchedulerConfig
    endpoints: Mapping[str, Endpoint] = field(default_factory=dict)

    def caps(self) -> dict[str, Any]:
        """The campaign-wide caps as plain data, for the fingerprint: the loop's, the attempt limit and the budget."""
        budget = self.scheduler.budget
        return {
            "agent": self.agent.as_dict(),
            "max_attempts": self.scheduler.max_attempts,
            "budget": None if budget is None else dataclasses.asdict(budget) | {"release_file": None},
        }


def _typed(section: Section, key: str, kind: type) -> Any:
    """Read an optional value of one scalar type; a boolean is never a number, and an integer may stand for a float."""
    value = section.data[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, bool) != (kind is bool) or not isinstance(value, kind):
        raise section.fail(key, f"must be a {kind.__name__}, got {type(value).__name__}")
    return value


def _table(root: Section, key: str) -> Section:
    """Return a required sub-table named `[key]` in messages, as TOML writes the table."""
    return Section(root.section(key).data, root.file, f"[{key}]")


def _number(section: Section, key: str) -> float:
    """Read a required number."""
    if key not in section.data:
        raise section.fail(key, "is required")
    return _typed(section, key, float)


def _agent(root: Section) -> AgentConfig:
    section = _table(root, "agent")
    section.reject_unknown([*_AGENT_KEYS, "malformed_policy"])
    for key in _AGENT_KEYS:
        if key not in section.data:
            raise section.fail(key, "is required")
    limits = Limits(
        max_tool_rounds=_typed(section, "max_tool_rounds", int),
        time_cap_s=_number(section, "time_cap_s"),
        token_budget=_typed(section, "token_budget", int),
        output_cap_chars=_typed(section, "output_cap_chars", int),
        max_output_tokens=_typed(section, "max_output_tokens", int),
    )
    policy = section.optional_string("malformed_policy") or MALFORMED_POLICIES[0]
    try:
        return AgentConfig(limits, malformed_policy=policy)
    except ValueError as error:
        raise ConfigError(f"{root.file}: {section.where}: {error}") from None


def _budget(root: Section) -> Budget | None:
    if "budget" not in root.data:
        return None
    section = _table(root, "budget")
    section.reject_unknown(["max_usd", "per_model_usd"])
    per_model = {name: _typed(Section(section.table("per_model_usd"), root.file, "[budget.per_model_usd]"), name, float)
                 for name in section.table("per_model_usd")}  # fmt: skip
    try:
        return Budget(_number(section, "max_usd"), per_model)
    except ValueError as error:
        raise ConfigError(f"{root.file}: {section.where}: {error}") from None


def _scheduler(root: Section, budget: Budget | None) -> SchedulerConfig:
    section = Section(root.table("scheduler"), root.file, "[scheduler]")
    section.reject_unknown([*_SCHEDULER_KINDS, "unit_caps"])
    values: dict[str, Any] = {
        key: _typed(section, key, kind) for key, kind in _SCHEDULER_KINDS.items() if key in section.data
    }
    caps = Section(section.table("unit_caps"), root.file, "[scheduler.unit_caps]")
    values["unit_caps"] = {unit: _typed(caps, unit, int) for unit in caps.data}
    try:
        return SchedulerConfig(**values, budget=budget)
    except ValueError as error:
        raise ConfigError(f"{root.file}: {section.where}: {error}") from None


def _endpoints(root: Section, models: tuple[str, ...]) -> dict[str, Endpoint]:
    declared = Section(root.table("endpoints"), root.file, "[endpoints]")
    unknown = [name for name in declared.data if name not in models]
    if unknown:
        raise declared.fail(unknown[0], f"is not one of the plan's models: {', '.join(models)}")
    endpoints = {}
    for model in models:
        entry = declared.section(model) if model in declared.data else Section({}, root.file, f"[endpoints.{model}]")
        entry.reject_unknown(["unit", "base_url"])
        endpoints[model] = Endpoint(model, entry.optional_string("unit") or model, entry.optional_string("base_url"))
    return endpoints


def load_campaign(path: str | os.PathLike[str]) -> CampaignConfig:
    """Read and validate campaign.toml; raise ConfigError naming the file and the key of the first problem."""
    root = Section(load_toml(path), path, "top level")
    root.reject_unknown(_TOP_KEYS)
    if "order_seed" not in root.data:
        raise root.fail("order_seed", "is required")
    order_seed = _typed(root, "order_seed", int)
    seeds = root.data.get("seeds")
    if not isinstance(seeds, list) or not seeds or not all(type(s) is int for s in seeds):
        raise root.fail("seeds", "must be a non-empty array of integers")
    models = root.strings("models")
    if not models:
        raise root.fail("models", "must name at least one model of models.toml")
    if len(set(models)) != len(models):
        raise root.fail("models", "names a model more than once")
    budget = _budget(root)
    return CampaignConfig(
        source=str(path),
        order_seed=order_seed,
        seeds=tuple(seeds),
        models=models,
        agent=_agent(root),
        scheduler=_scheduler(root, budget),
        endpoints=_endpoints(root, models),
    )
