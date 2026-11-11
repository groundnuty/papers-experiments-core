# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/config.py, blob 4bf53fb3c2a01cb49ec4b77c7e3651c35128fb76
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The model table: every model a study may use, named once, with the settings its requests carry.

`models.toml` holds one `[models.<key>]` table per model. The key names the model everywhere else in
configuration (caps and concurrency refer to keys, never to API ids); an API id or a name the server reports
resolves to the same entry. Nothing is read lazily: `load_models` validates every entry, refuses a name that two
entries share (P06), refuses settings that cannot work together (B16), and names the file and the key of every
problem. The model's status gates study runs: only `frozen` settings may start one (B08).

`backend` names one of the core's adapters (`BACKENDS`) or a backend of the study's own, a plain lower-case name. The
core's rules for its own adapters apply only to them. The core cannot build a study's own backend: the study's plugin
supplies a `backend_factory` that serves it, and a plugin without one is refused at load (`expcore.study.plugin`). An
entry with such a backend and a problem of its own (a missing `api_id`, a rule it breaks) says first that the core does
not build its backend, so a misspelt core backend is never reported as a missing key, and a name close to a core
backend is named with it.

`accepted_fingerprints` declares the serving fingerprints (`system_fingerprint`) a load-balanced endpoint serves the
model under, each a deployment the study decided is the same model (B18, `expcore.campaign.identity`). It is a set,
kept sorted; an empty list, a repeat or a value that joins several fingerprints is refused, and so is the key on an
`anthropic` model, whose adapter records no fingerprint to check it against.

Secrets never appear here. An entry points at an environment variable (`key_env`) or a file (`key_file`), and the
key is read when a request is built, not when the table is loaded.

A price is `[models.<key>.price]`, in dollars per million tokens, flat or in tiers by the prompt's length, a request
priced, its reply included, at the tier its prompt fell in (`expcore.config.price_table`, C24).
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError, Section, close_match, load_toml
from expcore.config.model_checks import base_url_problems, sampling_problems, thinking_problems
from expcore.config.price_table import read_price
from expcore.config.prices import Price, TieredPrice
from expcore.models.served import MIXED_FINGERPRINTS

BACKENDS = ("openai_compat", "anthropic", "stub")  # the backends the core builds; a plugin's factory serves others
STATUSES = ("frozen", "draft")
CACHE_TTLS = ("5m", "1h")

_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKEND_RE = re.compile(r"[a-z][a-z0-9_]*")
# Request fields each adapter sets itself; a model entry that names one would be overridden or would override it.
_OWNED_FIELDS = {
    "openai_compat": frozenset(
        {"model", "messages", "tools", "tool_choice", "max_tokens", "stream", "stream_options", "seed"}
    ),
    "anthropic": frozenset({"model", "system", "messages", "tools", "max_tokens", "stream", "cache_control"}),
    "stub": frozenset(),
}
_ANTHROPIC_SAMPLING = frozenset({"temperature", "top_p", "top_k"})
_MESSAGE_FIELDS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name", "_reasoning"})


class RuleViolation(ConfigError):
    """A model entry whose settings cannot work together; `problems` lists each broken rule."""

    def __init__(self, key: str, problems: list[str]):
        super().__init__(f"model {key!r}: " + "; ".join(problems))
        self.problems = problems


@dataclasses.dataclass(frozen=True)
class Limits:
    """The study's per-run caps that the backends need: the time cap and the output-token limit."""

    max_tool_rounds: int
    time_cap_s: float
    token_budget: int
    output_cap_chars: int
    max_output_tokens: int


@dataclasses.dataclass(frozen=True)
class ClientSettings:
    """How a backend talks to its endpoint: timeouts, and the retry policy for transient errors."""

    request_timeout_s: float = 600.0
    connect_timeout_s: float = 10.0
    max_retries: int = 8
    retry_base_s: float = 5.0
    retry_max_s: float = 120.0
    retry_jitter: float = 0.25

    def __post_init__(self) -> None:
        """Refuse settings that cannot work: non-positive times, negative retries, jitter outside 0..1."""
        problems = []
        if self.request_timeout_s < 0 or self.connect_timeout_s <= 0:
            problems.append("timeouts must be positive (a request timeout of 0 means the run's time cap)")
        if self.max_retries < 0:
            problems.append("max_retries must not be negative")
        if self.retry_base_s <= 0 or self.retry_max_s < self.retry_base_s:
            problems.append("retry_base_s must be positive and no larger than retry_max_s")
        if not 0 <= self.retry_jitter <= 1:
            problems.append("retry_jitter must be between 0 and 1")
        if problems:
            raise ConfigError("client settings: " + "; ".join(problems))


@dataclasses.dataclass(frozen=True)
class ModelSpec:
    """One model: how to reach it, what to send it, and whether its settings are frozen for study runs."""

    key: str
    backend: str  # one of BACKENDS, or a backend of the study's own that its plugin's factory serves
    api_id: str  # the id sent to the endpoint
    status: str  # "frozen" once the vendor settings are collected, else "draft"
    base_url: str | None = None
    aliases: tuple[str, ...] = ()  # other names the server reports for this model
    key_env: str | None = None  # name of the environment variable holding the key
    key_file: str | None = None  # or a file holding it
    sampling: dict[str, Any] = dataclasses.field(default_factory=dict)
    max_output_tokens: int | None = None  # the model's own maximum per call, when lower than the study's limit
    context_tokens: int | None = None  # the endpoint's context window, when known; checked at admission (B11)
    echo_reasoning_field: str | None = None  # openai_compat: assistant-message field the reasoning is sent back in
    extra_body: dict[str, Any] = dataclasses.field(default_factory=dict)  # vendor extras sent verbatim (B14)
    prompt_cache_ttl: str | None = None  # anthropic: cache lifetime ("5m" or "1h"); None sends no cache marker
    price: Price | TieredPrice | None = None  # dollars per million tokens, flat or by prompt tier (C24); None with
    # free=False is an unknown cost (C12)
    free: bool = False  # a model that costs nothing per token (hosted by the study): priced without a price
    # the deployments' fingerprints the study declares the same model (B18); () checks against a baseline (C16)
    accepted_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Keep the accepted fingerprints sorted, then refuse an entry whose settings break a rule, before any request
        can be built from it."""
        accepted = self.accepted_fingerprints
        if isinstance(accepted, list | tuple) and all(isinstance(f, str) for f in accepted):
            object.__setattr__(self, "accepted_fingerprints", tuple(sorted(accepted)))
        problems = _rule_problems(self)
        if problems:
            raise RuleViolation(self.key, problems)

    def output_tokens(self, limit: int) -> int:
        """Return the max_tokens to send: the study's value, or the model's own maximum if that is lower (B11)."""
        return limit if self.max_output_tokens is None else min(limit, self.max_output_tokens)

    @property
    def priced(self) -> bool:
        """Tell whether the cost of its tokens is known: it has a price, or its entry says it is free (C12)."""
        return self.free or self.price is not None

    @property
    def core_backend(self) -> bool:
        """Tell whether the core builds the entry's backend; a backend of the study's own is its plugin's to build."""
        return self.backend in BACKENDS

    @property
    def seed_supported(self) -> bool:
        """Tell whether the backend takes a sampling seed."""
        return self.backend == "openai_compat"

    @property
    def names(self) -> tuple[str, ...]:
        """Return every name that resolves to this entry: its key, its API id and its aliases."""
        return tuple(dict.fromkeys((self.key, self.api_id, *self.aliases)))

    def read_key(self, env: Mapping[str, str] | None = None) -> str | None:
        """Read the key from its environment variable or file now; None when the entry names neither.

        The message of a failure names the variable or file, never a value.
        """
        if self.key_env:
            value = (os.environ if env is None else env).get(self.key_env, "").strip()
            if not value:
                raise ConfigError(f"model {self.key!r}: environment variable {self.key_env} is not set or is empty")
            return value
        if self.key_file:
            path = Path(os.path.expanduser(self.key_file))
            try:
                value = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                raise ConfigError(f"model {self.key!r}: key file {self.key_file!r} cannot be read") from None
            if not value:
                raise ConfigError(f"model {self.key!r}: key file {self.key_file!r} is empty")
            return value
        return None


def _rule_problems(spec: ModelSpec) -> list[str]:
    """List every rule the entry breaks: its shape, its secrets, and the combinations that cannot work (B16)."""
    problems: list[str] = []
    if not _KEY_RE.fullmatch(spec.key):
        problems.append("key must be letters, digits, dot, dash and underscore, starting with a letter or digit")
    if not spec.core_backend and not _BACKEND_RE.fullmatch(spec.backend):
        return [_backend_problem(spec.backend), *problems]
    if spec.status not in STATUSES:
        problems.append(f"status must be one of {', '.join(STATUSES)}, got {spec.status!r}")
    if not spec.api_id.strip():
        problems.append("api_id must not be empty")
    problems += _endpoint_problems(spec)
    problems += _limit_problems(spec)
    problems += _request_problems(spec)
    problems += _fingerprint_problems(spec)
    return problems


def _fingerprint_problems(spec: ModelSpec) -> list[str]:
    """Rules for the declared set of accepted fingerprints (B18): distinct, single, non-empty fingerprints, on a
    backend whose replies carry one."""
    accepted = spec.accepted_fingerprints
    if not isinstance(accepted, tuple) or not all(isinstance(f, str) and f.strip() for f in accepted):
        return ["accepted_fingerprints must be a list of non-empty strings"]
    problems = [
        f"accepted_fingerprints lists {f!r} twice" for f in sorted({f for f in accepted if accepted.count(f) > 1})
    ]
    problems += [
        f"accepted_fingerprints entry {f!r} holds several fingerprints joined by {MIXED_FINGERPRINTS.strip()!r}; list "
        "each on its own"
        for f in accepted
        if MIXED_FINGERPRINTS in f
    ]
    if accepted and spec.backend == "anthropic":
        problems.append(
            "accepted_fingerprints cannot apply to an anthropic model: its adapter records no system fingerprint"
        )
    return problems


def _backend_problem(backend: str) -> str:
    """The problem of a backend name that is neither the core's nor a plain lower-case name a plugin could serve."""
    return (
        f"backend must be one of {', '.join(BACKENDS)}, or the plain lower-case name of a backend the study's "
        f"plugin serves, got {backend!r}{close_match(backend, BACKENDS)}"
    )


def not_built_by_core(backend: str) -> str:
    """Say that the core does not build a backend, and what serves it instead: the plugin's `backend_factory`.

    Said at load before any other problem of an entry that has one, and when a plugin without a factory is loaded.
    """
    return (
        f"names {backend!r}{close_match(backend, BACKENDS)}, a backend the core does not build ({', '.join(BACKENDS)}); "
        "the plugin must supply a backend_factory that serves it"
    )


def _endpoint_problems(spec: ModelSpec) -> list[str]:
    """Rules for the base URL and the place the key is read from."""
    problems = []
    if spec.backend == "stub":
        if spec.base_url or spec.key_env or spec.key_file:
            problems.append("a stub model takes no base_url, key_env or key_file")
        return problems
    if spec.core_backend or spec.base_url is not None:  # a study's own backend may need no URL; one it gives is checked
        problems += base_url_problems(spec.base_url)
    if spec.key_env and spec.key_file:
        problems.append("give key_env or key_file, not both")
    if spec.backend == "anthropic" and not (spec.key_env or spec.key_file):
        problems.append("an anthropic model needs key_env or key_file")
    if spec.key_env and not _IDENT_RE.fullmatch(spec.key_env):
        problems.append("key_env must be the name of an environment variable, never a key")
    return problems


def _limit_problems(spec: ModelSpec) -> list[str]:
    """Rules for the output and context limits."""
    problems = []
    for name in ("max_output_tokens", "context_tokens"):
        value = getattr(spec, name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            problems.append(f"{name} must be a positive integer")
    limits_ok = not problems
    if limits_ok and spec.max_output_tokens and spec.context_tokens and spec.max_output_tokens >= spec.context_tokens:
        problems.append("max_output_tokens must be smaller than the context window (context_tokens)")
    return problems


def _request_problems(spec: ModelSpec) -> list[str]:
    """Rules for what the request carries: sampling, vendor extras, the reasoning echo and the cache."""
    problems = [
        *sampling_problems(spec.sampling),
        *thinking_problems(spec.backend, spec.extra_body, spec.max_output_tokens),
    ]
    owned = _OWNED_FIELDS.get(spec.backend, frozenset())  # a study's own backend: its factory knows what it sets
    for where, table in (("sampling", spec.sampling), ("extra_body", spec.extra_body)):
        problems += [f"{where} sets '{name}', which the adapter sets itself" for name in sorted(owned & table.keys())]
    if (spec.sampling.keys() | spec.extra_body.keys()) & {"stream"}:
        problems.append("streaming is not supported")
    clash = sorted(spec.sampling.keys() & spec.extra_body.keys())
    if clash:
        problems.append(f"{', '.join(clash)} appear in both sampling and extra_body, so one would silently win")
    if spec.backend == "anthropic":
        bad = sorted(spec.sampling.keys() - _ANTHROPIC_SAMPLING - owned)
        if bad:
            problems.append(f"sampling keys {', '.join(bad)} are not accepted by the Messages API")
    if spec.backend == "stub" and (spec.sampling or spec.extra_body):
        problems.append("a stub model takes no sampling or extra_body")
    field = spec.echo_reasoning_field
    if field is not None:
        if spec.backend != "openai_compat":
            problems.append("echo_reasoning_field applies to openai_compat models only")
        elif field in _MESSAGE_FIELDS:
            problems.append(f"echo_reasoning_field {field!r} would overwrite a standard message field")
        elif not _IDENT_RE.fullmatch(field):
            problems.append("echo_reasoning_field must be a plain field name")
    if spec.free and spec.price is not None:
        problems.append("a model is free or has a price, not both")
    if spec.prompt_cache_ttl is not None:
        if spec.backend != "anthropic":
            problems.append("prompt_cache_ttl applies to anthropic models only")
        elif spec.prompt_cache_ttl not in CACHE_TTLS:
            problems.append(f"prompt_cache_ttl must be one of {', '.join(CACHE_TTLS)}")
    return problems


@dataclasses.dataclass(frozen=True)
class ModelTable:
    """The single model table. Every name a configuration uses resolves to exactly one entry (P06)."""

    models: Mapping[str, ModelSpec]
    source: str = "<memory>"

    def __post_init__(self) -> None:
        """Refuse a table in which two entries answer to the same name, ignoring case."""
        seen: dict[str, str] = {}
        for key, spec in self.models.items():
            for name in spec.names:
                other = seen.setdefault(name.lower(), key)
                if other != key:
                    raise ConfigError(
                        f"{self.source}: models {other!r} and {key!r} both answer to {name!r}; each model is named once"
                    )

    def __iter__(self) -> Iterator[str]:
        """Iterate over the keys in file order."""
        return iter(self.models)

    def __len__(self) -> int:
        """Return the number of models."""
        return len(self.models)

    def resolve(self, name: str) -> ModelSpec:
        """Return the entry a key, an API id or an alias names; raise ConfigError when none or several do."""
        if name in self.models:
            return self.models[name]
        low = name.lower()
        found = [spec for spec in self.models.values() if low in (n.lower() for n in spec.names)]
        if len(found) == 1:
            return found[0]
        known, hint = ", ".join(self.models), close_match(name, self.models)
        raise ConfigError(f"{self.source}: unknown model {name!r}{hint}; the table holds: {known}")

    def require_frozen(self, names: Iterable[str]) -> None:
        """Refuse a study run unless every named model is frozen (B08)."""
        for name in names:
            assert_frozen(self.resolve(name))

    def require_priced(self, names: Iterable[str]) -> None:
        """Refuse a study run unless every named model has a price or is explicitly marked free (C12)."""
        unpriced = [spec.key for spec in map(self.resolve, names) if not spec.priced]
        if unpriced:
            raise ConfigError(
                f"{self.source}: no price for model(s) {', '.join(unpriced)}: give each a [models.<key>.price] table, "
                "or mark it free = true if it costs nothing per token; a study run needs a known cost to enforce its budget"
            )

    def require_context_window(self, names: Iterable[str]) -> None:
        """Refuse a campaign with a budget whose models, other than free ones, do not all declare `context_tokens`.

        The budget reserves for each run the request it can make past its token cap, and only the context window
        bounds that request's prompt (C25).
        """
        missing = [spec.key for spec in map(self.resolve, names) if not spec.free and spec.context_tokens is None]
        if missing:
            raise ConfigError(
                f"{self.source}: no context window for model(s) {', '.join(map(repr, missing))}: with a budget "
                "(max_usd), each run "
                "is reserved what it can cost, including the one request it can send just under its token cap and end "
                "past it, whose prompt only the context window bounds; give each model context_tokens = <the "
                "endpoint's context window in tokens>, or mark it free = true if it costs nothing per token"
            )

    def require_keys(self, names: Iterable[str], env: Mapping[str, str] | None = None) -> None:
        """Check at startup that the key of every named model can be read; the values are discarded."""
        for name in names:
            self.resolve(name).read_key(env)


def assert_frozen(model: ModelSpec) -> None:
    """Refuse a study run on unfrozen settings: the sampling and reasoning settings freeze before the pilot (B08)."""
    if model.status != "frozen":
        raise ConfigError(f"model {model.key}: settings are {model.status!r}, not 'frozen'; a study run needs 'frozen'")


_ENTRY_KEYS = (
    "backend",
    "api_id",
    "status",
    "base_url",
    "aliases",
    "key_env",
    "key_file",
    "sampling",
    "max_output_tokens",
    "context_tokens",
    "echo_reasoning_field",
    "extra_body",
    "prompt_cache_ttl",
    "price",
    "free",
    "accepted_fingerprints",
)


def _flag(section: Section, key: str) -> bool:
    """Read an optional boolean; absent is False."""
    value = section.data.get(key, False)
    if not isinstance(value, bool):
        raise section.fail(key, f"must be true or false, got {type(value).__name__}")
    return value


def _backend_first(backend: str) -> list[str]:
    """What an entry's problems begin with when its backend is not the core's: that the core does not build it and
    the plugin's factory must (or that the name could not be a backend at all), so a missing key is never said first."""
    if backend in BACKENDS:
        return []
    if not _BACKEND_RE.fullmatch(backend):
        return [_backend_problem(backend)]
    return [f"key 'backend' {not_built_by_core(backend)}"]


def _entry(section: Section, key: str) -> ModelSpec:
    """Build one ModelSpec from its table; every failure names the file and the key.

    An entry whose backend the core does not build may still be valid (its plugin's factory serves it), so that is
    said only when the entry has a problem, and then before the others.
    """
    backend = section.string("backend")
    first = _backend_first(backend)
    try:
        section.reject_unknown(_ENTRY_KEYS)
        spec_args = _entry_args(section, key, backend)
    except ConfigError as error:  # a key that cannot be read: the backend still comes first
        if not first:
            raise
        own, file = f"{section.file}: {section.where}: ", f"{section.file}: "
        text = str(error)
        rest = text[len(own) :] if text.startswith(own) else text[len(file) :] if text.startswith(file) else text
        raise ConfigError(own + "; ".join([*first, rest])) from None
    if spec_args["api_id"] is None:
        raise ConfigError(f"{section.file}: {section.where}: " + "; ".join([*first, "key 'api_id' is required"]))
    try:
        return ModelSpec(**spec_args)
    except RuleViolation as error:
        problems = [*first, *(problem for problem in error.problems if problem not in first)]
        raise ConfigError(f"{section.file}: {section.where}: " + "; ".join(problems)) from None


def _entry_args(section: Section, key: str, backend: str) -> dict[str, Any]:
    """Read the keys of one entry into the arguments of its ModelSpec; a key of the wrong type raises ConfigError."""
    return {
        "key": key,
        "backend": backend,
        "api_id": section.optional_string("api_id") or (key if backend == "stub" else None),
        "status": section.optional_string("status") or "draft",
        "base_url": section.optional_string("base_url"),
        "aliases": section.strings("aliases"),
        "key_env": section.optional_string("key_env"),
        "key_file": section.optional_string("key_file"),
        "sampling": section.table("sampling"),
        "max_output_tokens": section.optional_integer("max_output_tokens"),
        "context_tokens": section.optional_integer("context_tokens"),
        "echo_reasoning_field": section.optional_string("echo_reasoning_field"),
        "extra_body": section.table("extra_body"),
        "prompt_cache_ttl": section.optional_string("prompt_cache_ttl"),
        "price": read_price(section),
        "free": _flag(section, "free"),
        "accepted_fingerprints": _accepted(section),
    }


def _accepted(section: Section) -> tuple[str, ...]:
    """Read the optional set of accepted fingerprints (B18); a key that lists none is refused, as it declares nothing."""
    accepted = section.strings("accepted_fingerprints")
    if "accepted_fingerprints" in section.data and not accepted:
        raise section.fail("accepted_fingerprints", "must list at least one fingerprint; leave it out to declare none")
    return accepted


def load_models(path: str | os.PathLike[str]) -> ModelTable:
    """Load and validate `models.toml`; fail at once, naming the file and the key, on the first bad entry."""
    return parse_models(load_toml(path), path)


def parse_models(data: Mapping[str, Any], path: str | os.PathLike[str]) -> ModelTable:
    """Validate the parsed TOML of a model table, whose messages name `path` as its file."""
    root = Section(data, path, "(file)")
    root.reject_unknown(["models"])
    table = root.table("models")
    if not table:
        raise root.fail("models", "is required and must hold at least one model")
    models = Section(table, path, "[models]")
    specs: dict[str, ModelSpec] = {}
    for key in models.data:
        specs[key] = _entry(models.section(key), key)
    return ModelTable(specs, source=str(path))
