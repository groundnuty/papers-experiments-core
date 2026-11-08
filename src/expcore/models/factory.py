# Provenance: new
"""Build the backend a model entry names. Keys are read here, at use time, from the variable or file the entry names."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from expcore.config.loading import ConfigError
from expcore.config.model_checks import thinking_problems
from expcore.config.models import ClientSettings, Limits, ModelSpec, assert_frozen
from expcore.models.adapters import AnthropicAdapter, ModelBackend, OpenAICompatAdapter, StubAdapter
from expcore.models.retry import SupportsUniform


def refuse_unsendable_thinking(spec: ModelSpec, limits: Limits) -> None:
    """Refuse a thinking budget that is not below the output limit a request will carry: the lower of two caps (B16).

    The entry alone cannot say, when it sets no maximum of its own: the study's limit may be the lower one.
    """
    effective = spec.output_tokens(limits.max_output_tokens)
    model_cap = "unset" if spec.max_output_tokens is None else spec.max_output_tokens
    problems = thinking_problems(
        spec.backend,
        spec.extra_body,
        effective,
        "the effective output limit",
        f", the lower of the study's max_output_tokens ({limits.max_output_tokens}) and the model's ({model_cap})",
    )
    if problems:
        raise ConfigError(f"model {spec.key!r}: " + "; ".join(problems))


def build_backend(
    spec: ModelSpec,
    *,
    seed: int,
    limits: Limits,
    client_cfg: ClientSettings | None = None,
    env: Mapping[str, str] | None = None,
    base_url: str | None = None,
    require_frozen: bool = True,
    script: Iterable[Any] = (),
    client: Any = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rng: SupportsUniform | None = None,
    time_left: Callable[[], float | None] | None = None,
) -> ModelBackend:
    """Return the backend for a model entry.

    A study run needs frozen settings: with `require_frozen` (the default) an entry that is not frozen raises
    before anything is built, let alone sent (B08). A pilot passes False. `base_url` overrides the entry's, for a
    campaign whose admitted endpoint is the one a run must use (C14). `script` is the stub's list of turns.
    An Anthropic thinking budget that is not below the effective output limit, the lower of the study's cap and
    the entry's own, raises before anything is built (B16).
    """
    if require_frozen:
        assert_frozen(spec)
    refuse_unsendable_thinking(spec, limits)
    if spec.backend == "stub":
        return StubAdapter(
            list(script),
            name=spec.key,
            seed=seed,
            output_limit=spec.output_tokens(limits.max_output_tokens),
            context_tokens=spec.context_tokens,
        )
    shared: dict[str, Any] = {
        "api_key": spec.read_key(env),
        "base_url": base_url,
        "client_cfg": client_cfg,
        "client": client,
        "sleep": sleep,
        "rng": rng,
        "time_left": time_left,
    }
    adapter = AnthropicAdapter if spec.backend == "anthropic" else OpenAICompatAdapter
    return adapter(spec, seed, limits, **shared)
