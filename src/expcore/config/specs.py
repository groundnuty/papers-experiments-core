# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/config.py, blob 4bf53fb3c2a01cb49ec4b77c7e3651c35128fb76
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Harness configuration: frozen parameters, the model table, scheduler settings.

Values live in `config/harness.toml` and `config/models.toml` (placeholders until the science
agent's `protocol/` lands). Nothing is hard-coded here; `STUDY_VAR_56` points the
loader at another directory.

Derived from groundnuty/dataset-88 @ BASE_COMMIT (harness/panel.py,
runner.py, schema.py); see docs/harness.md.
"""
from __future__ import annotations

import dataclasses
import os
import tomllib
from pathlib import Path
from typing import Any

BASE_REPO = "user-10/repo-10"
BASE_COMMIT = "06223f16181d53681b17e274aa2ccaec04ffbf06"

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class ConfigError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Limits:
    max_tool_rounds: int
    time_cap_s: float
    token_budget: int
    output_cap_chars: int
    max_output_tokens: int


GiB = 1024 ** 3


@dataclasses.dataclass(frozen=True)
class SandboxLimits:
    """The agent sandbox's resource limits, identical in every option, by rule (science ruling on #9, 1 Nov).
    The container's memory limit is the agent's allowance plus the tracer's measured headroom; the two parts are stated apart."""
    agent_memory_gib: float
    tracer_headroom_gib: float | None      # a measurement (#9, at real size); None = not yet measured: the real factory refuses
    cpus: float
    pids: int
    nofile: int = 1048576

    @property
    def agent_memory_bytes(self) -> int:
        return int(self.agent_memory_gib * GiB)

    @property
    def tracer_headroom_bytes(self) -> int | None:
        return None if self.tracer_headroom_gib is None else int(self.tracer_headroom_gib * GiB)


@dataclasses.dataclass(frozen=True)
class ClientSettings:
    endpoints_file: str
    request_timeout_s: float
    connect_retries: int
    retry_base_s: float
    retry_max_s: float


@dataclasses.dataclass(frozen=True)
class ModelSpec:
    key: str                       # config key, also the slot model name in endpoints.json
    reported_name: str
    api_id: str
    checkpoint: str                # the real checkpoint behind the API id
    version_note: str
    kind: str                      # "openai_compat" | "anthropic"
    provider_id: str
    status: str                    # "frozen" once the vendor settings are collected
    slot_prefix: str | None = None
    sampling: dict[str, Any] = dataclasses.field(default_factory=dict)
    request_extra: dict[str, Any] = dataclasses.field(default_factory=dict)
    echo_reasoning_field: str | None = None  # openai_compat: assistant-message field to send back
    sources: list[str] = dataclasses.field(default_factory=list)
    notes: str = ""

    version_notes: dict[str, str] = dataclasses.field(default_factory=dict)   # per backend: hpc | provider_i | anthropic
    max_output_tokens: int | None = None      # the model's own maximum per call, when lower than limits.max_output_tokens

    def output_tokens(self, limit: int) -> int:
        """The max_tokens sent: the study's value, or the model's own maximum if that is lower."""
        return limit if self.max_output_tokens is None else min(limit, self.max_output_tokens)

    def version_for(self, backend: str) -> str:
        return self.version_notes.get(backend, "")

    @property
    def endpoint_name(self) -> str:
        """The model name used in endpoints.json (its `model` field): the single mapping for slots."""
        return self.slot_prefix or self.key

    @property
    def seed_supported(self) -> bool:
        return self.kind == "openai_compat"


@dataclasses.dataclass(frozen=True)
class HarnessConfig:
    status: str
    limits: Limits
    sandbox_limits: SandboxLimits
    seeds: tuple[int, ...]
    site_id: str
    provider_concurrency: dict[str, int]
    client: ClientSettings
    anthropic_key_file: str
    knowledge: dict[str, Any]
    models: dict[str, ModelSpec]
    provider_i: dict[str, Any] = dataclasses.field(default_factory=dict)   # [provider_i]: base_url, key_file, concurrency
    prompt_slots: dict[str, str] = dataclasses.field(default_factory=dict)
    prompt_slots_by_layout: dict[str, dict[str, str]] = dataclasses.field(default_factory=dict)

    def slots_for(self, layout) -> dict[str, str]:
        """Prompt slot values for a layout; identical across options by construction."""
        return {**self.prompt_slots, **self.prompt_slots_by_layout.get(getattr(layout, 'value', layout), {})}

    def concurrency_unit(self, spec: ModelSpec) -> str:
        """The key of the cap this model's runs count against: `anthropic:<api id>` for a Claude model with its own entry
        in provider_concurrency, else its provider. The one source of the caps is `[scheduler.provider_concurrency]`."""
        own = f"{spec.provider_id}:{spec.api_id}"
        return own if own in self.provider_concurrency else spec.provider_id

    def model(self, key_or_name: str) -> ModelSpec:
        if key_or_name in self.models:
            return self.models[key_or_name]
        for m in self.models.values():
            if key_or_name in (m.reported_name, m.api_id):
                return m
        low = key_or_name.lower()      # the campaign's reporting names ("model-1") differ in case from reported_name
        for m in self.models.values():
            if low in (m.key.lower(), m.reported_name.lower(), m.api_id.lower()):
                return m
        raise ConfigError(f"unknown model {key_or_name!r}")


def _read(path: Path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _sandbox_limits(h: dict) -> SandboxLimits:
    t = h.get("sandbox_limits")
    if not t:
        raise ConfigError("config/harness.toml has no [sandbox_limits]: the agent's memory allowance, the tracer headroom, cpus and pids are required")
    for k in ("agent_memory_gib", "cpus", "pids"):
        if k not in t:
            raise ConfigError(f"[sandbox_limits] needs {k}")
    return SandboxLimits(float(t["agent_memory_gib"]), None if t.get("tracer_headroom_gib") is None else float(t["tracer_headroom_gib"]),
                         float(t["cpus"]), int(t["pids"]), int(t.get("nofile", 1048576)))


def load_config(config_dir: str | os.PathLike | None = None) -> HarnessConfig:
    d = Path(config_dir or os.environ.get("STUDY_VAR_56") or DEFAULT_CONFIG_DIR)
    h = _read(d / "harness.toml")
    m = _read(d / "models.toml")
    models: dict[str, ModelSpec] = {}
    for key, t in m["models"].items():
        models[key] = ModelSpec(
            key=key,
            reported_name=t["reported_name"],
            api_id=t["api_id"],
            checkpoint=t["checkpoint"],
            version_note=t.get("version_note", ""),
            version_notes=dict(t.get("version_notes", {})) or (
                {"anthropic": t.get("version_note", "")} if t["kind"] == "anthropic" else {"hpc": t.get("version_note", "")}),
            kind=t["kind"],
            provider_id=t["provider_id"],
            status=t.get("status", "pending-research"),
            slot_prefix=t.get("slot_prefix"),
            sampling=dict(t.get("sampling", {})),
            request_extra=dict(t.get("request_extra", {})),
            echo_reasoning_field=t.get("echo_reasoning_field"),
            sources=list(t.get("sources", [])),
            notes=t.get("notes", ""),
            max_output_tokens=t.get("max_output_tokens"),
        )
    c = h["client"]
    return HarnessConfig(
        status=h["meta"]["status"],
        limits=Limits(**h["limits"]),
        sandbox_limits=_sandbox_limits(h),
        seeds=tuple(h["seeds"]["values"]),
        site_id=h["scheduler"]["site_id"],
        provider_concurrency=dict(h["scheduler"]["provider_concurrency"]),
        client=ClientSettings(
            endpoints_file=os.path.expanduser(c["endpoints_file"]),
            request_timeout_s=c["request_timeout_s"],
            connect_retries=c["connect_retries"],
            retry_base_s=c["retry_base_s"],
            retry_max_s=c["retry_max_s"],
        ),
        anthropic_key_file=h["anthropic"]["key_file"],
        knowledge=dict(h.get("knowledge", {})),
        provider_i=dict(h.get("provider_i", {})),
        models=models,
        prompt_slots={k: str(v) for k, v in h.get("prompt_slots", {}).items() if not isinstance(v, dict)},
        prompt_slots_by_layout={k: {a: str(b) for a, b in v.items()}
                                for k, v in h.get("prompt_slots", {}).items() if isinstance(v, dict)},
    )


def assert_frozen(cfg: HarnessConfig, model: ModelSpec, check_config: bool = True) -> None:
    """Refuse a live study run on unfrozen settings. The sampling/reasoning settings freeze before the pilot; the caps
    and the rest of the harness config freeze after it (`check_config=False` checks the model settings only)."""
    if model.status != "frozen":
        raise ConfigError(
            f"model {model.key}: sampling/reasoning settings are {model.status!r}, not 'frozen'"
        )
    if check_config and cfg.status != "frozen":
        raise ConfigError(f"harness config status is {cfg.status!r}, not 'frozen'")


TOKEN_MARGIN_S = 3600     # sandbox start-up, collection and teardown after the time cap


def required_token_ttl_s(time_cap_s: float, margin_s: float = TOKEN_MARGIN_S) -> int:
    """The one place a per-run credential's lifetime is derived: the run's time cap ({TIME_CAP_MIN}) plus a margin.
    Every token a run uses (sandbox REST token, oneclient token for M and M+, knowledge-plane token) must live at least this long."""
    return int(time_cap_s + margin_s)


def check_token_ttl(ttl_s: float, time_cap_s: float, what: str = "token") -> None:
    """Refuse a token that could expire before the run's time cap plus the margin."""
    need = required_token_ttl_s(time_cap_s)
    if ttl_s < need:
        raise ConfigError(f"{what} lifetime {int(ttl_s)} s is below the run's time cap plus margin ({need} s): it could expire mid-run")
