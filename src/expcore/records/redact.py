# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/record.py:189-304
"""Secret redaction: no record is written with the value of a secret in it (R12).

A secret is the value of an environment variable whose name contains KEY, TOKEN, SECRET or PASSWORD, when the value
is at least 16 characters long and not only digits: real keys are long random strings, while a short or numeric value
would match ordinary data. Redaction works on parsed values, in strings and in dictionary keys only, so the output is
still valid JSON; numbers, booleans and null are never touched, and token counts survive. It is a fixed point:
redacting a redacted structure changes nothing, because a value with a bracket in it is never treated as a secret.
Callers warn about the count; no message here ever shows a value.
"""

import json
import os
import re
from collections.abc import Mapping
from typing import Any

SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
MIN_SECRET_LEN = 16
REDACTED = "[redacted]"


def _is_secret_value(value: str) -> bool:
    """Tell whether a value is worth redacting. A value with a bracket in it, or inside the marker, is skipped so
    that redacting twice changes nothing."""
    return (
        len(value) >= MIN_SECRET_LEN
        and not value.isdigit()
        and "[" not in value
        and "]" not in value
        and value not in REDACTED
    )


def _secrets(environ: Mapping[str, str] | None) -> dict[str, str]:
    """Return the secret-looking variables of an environment (the process's by default)."""
    env = os.environ if environ is None else environ
    return {k: v for k, v in env.items() if SECRET_NAME.search(k) and v and _is_secret_value(v)}


def secret_names(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return the names, never the values, of the environment variables treated as secrets."""
    return sorted(_secrets(environ))


def secret_values(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return the distinct secret values, longest first so a secret that contains another is replaced whole."""
    return sorted(set(_secrets(environ).values()), key=len, reverse=True)


def redact_structure(obj: Any, environ: Mapping[str, str] | None = None) -> tuple[Any, int]:
    """Return a copy of a JSON-like structure with every secret in its strings and keys replaced, and the count.

    Raises ValueError when two keys become the same once redacted, because merging them would lose a value.
    """
    secrets = secret_values(environ)
    count = 0

    def text(value: str) -> str:
        nonlocal count
        for secret in secrets:
            if secret in value:
                count += value.count(secret)
                value = value.replace(secret, REDACTED)
        return value

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return text(value)
        if isinstance(value, dict):
            out: dict = {}
            for k, v in value.items():
                key = text(k) if isinstance(k, str) else k
                if key in out:
                    raise ValueError("two keys are the same once secret values are redacted")
                out[key] = walk(v)
            return out
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return value

    return walk(obj), count


def redact_text(text: str, environ: Mapping[str, str] | None = None) -> tuple[str, int]:
    """Replace secrets, raw or JSON-escaped, in plain text such as a log line or an error message; return the count.

    For a JSON document use `redact_structure`.
    """
    count = 0
    for value in secret_values(environ):
        for form in {value, json.dumps(value)[1:-1]}:
            if form in text:
                count += text.count(form)
                text = text.replace(form, REDACTED)
    return text, count
