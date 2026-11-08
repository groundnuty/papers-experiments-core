# Provenance: new
"""Checks on the values of one model entry that need more than a key lookup: the URL, sampling types, thinking budget.

Each function returns the problems it finds, as sentences that name the key, and never raises on a bad value. The
model table collects them, so one load reports everything wrong with an entry and names the file and the model.
No message echoes a value it was given: a base URL can carry a secret in its userinfo.
"""

import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

MAX_PORT = 65535
# Sampling values the adapters pass on verbatim, so a wrong type would travel to the endpoint and fail there.
SAMPLING_NUMBERS = ("temperature", "top_p")
SAMPLING_INTEGERS = ("top_k", "seed")


def _is_integer(value: object) -> bool:
    """Tell whether a value is an integer; a boolean is not one, though Python counts it as such."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    """Tell whether a value is an integer or a float; a boolean is neither."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def base_url_problems(base_url: str | None) -> list[str]:
    """Check that a base URL parses, has an http or https scheme and a host, a valid port and no credentials."""
    try:
        parts = urlsplit(base_url or "")
        host = parts.hostname
    except ValueError:
        return ["base_url cannot be parsed as a URL"]
    try:
        port = parts.port
    except ValueError:
        port = 0
    if port == 0:  # a port is None when absent; 0 is never a port a server listens on
        return [f"base_url has an invalid port (it must be a number from 1 to {MAX_PORT})"]
    if parts.scheme not in ("http", "https") or not host:
        return ["base_url must be an http or https URL with a host"]
    if parts.username or parts.password:
        return ["base_url must not carry credentials; the key goes in key_env or key_file"]
    return []


def sampling_problems(sampling: Mapping[str, Any]) -> list[str]:
    """Check the types of the sampling values: temperature and top_p are numbers, top_k and seed integers."""
    problems = []
    for name in SAMPLING_NUMBERS:
        if name in sampling:
            value = sampling[name]
            if not _is_number(value):
                problems.append(f"sampling.{name} must be a number, got {type(value).__name__}")
            elif not math.isfinite(value):
                problems.append(f"sampling.{name} must be a finite number")
    for name in SAMPLING_INTEGERS:
        if name in sampling and not _is_integer(sampling[name]):
            problems.append(f"sampling.{name} must be an integer, got {type(sampling[name]).__name__}")
    return problems


def thinking_problems(backend: str, extra_body: Mapping[str, Any], max_output_tokens: object) -> list[str]:
    """Check that an Anthropic thinking budget leaves room under the model's output maximum (B16).

    The Messages API refuses a request whose `thinking.budget_tokens` is not below `max_tokens`. The model's own
    maximum is the largest `max_tokens` a request can carry, so a budget at or above it can never be sent. Without a
    maximum on the entry there is nothing to compare at load.
    """
    thinking = extra_body.get("thinking") if backend == "anthropic" else None
    if not isinstance(thinking, Mapping) or "budget_tokens" not in thinking:
        return []
    budget = thinking["budget_tokens"]
    if not _is_integer(budget):
        return [f"extra_body.thinking.budget_tokens must be an integer, got {type(budget).__name__}"]
    if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool) and budget >= max_output_tokens:
        return [
            f"extra_body.thinking.budget_tokens ({budget}) must be smaller than max_output_tokens "
            f"({max_output_tokens}): the Messages API needs room for the answer after the thinking"
        ]
    return []
