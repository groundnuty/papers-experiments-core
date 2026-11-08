# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/record.py:189-304
"""Secret redaction: no record is written with the value of a secret in it (R12).

A secret comes from three places, and redaction works on parsed values, so its output is still valid JSON:

1. **The environment.** The value of every variable whose name contains KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL
   or AUTH, whatever the value looks like. A value of 8 or more characters is replaced wherever it occurs inside a
   string, in values and in keys. A shorter value would match ordinary text, so it is replaced only where an entire
   string equals it, or where a number's string form does (a numeric value 4096 is compared as "4096").
2. **A key that names a secret.** The value of a mapping key called token, api_key, apikey, password, secret,
   authorization or bearer (or ending in `_` and one of them, such as `x-api-key` or `client_secret`), in any case, is
   replaced whatever it is. This is how a credential the process was never told about, a header or an argument,
   is still caught.
3. **A registered secret.** `register_secret(value)` adds a value for the rest of the process. A token minted at run
   time (the platform layer mints the tokens of both planes) is in no environment variable and sits in no key the
   rules above know: the code that mints it must register it, or it is not redacted. `build_record(secrets=...)`
   takes values for one record only.

Every secret is also redacted in its JSON-escaped and its percent-encoded forms, since a native message carries
text inside JSON strings and a URL carries it percent-encoded. A secret that was replaced is gone: neither the text nor
a decoding of it holds the value.

Redaction records the paths of what it changed, never values. It is a fixed point: redacting a redacted structure
changes nothing. A number that is replaced becomes a string, so a record whose redaction would break a field that must
be numeric is refused when it is written, naming the path (see `RunRecord.dumps`).
"""

import json
import os
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, quote_plus

SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.IGNORECASE)
SECRET_KEYS = ("token", "api_key", "apikey", "password", "secret", "authorization", "bearer")
MIN_SUBSTRING_LEN = 8
REDACTED = "[redacted]"

_registered: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Add a secret minted at run time to the values redacted from every record written from now on.

    The platform layer calls this for each token it mints, before the token is used anywhere a record could see it.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("a secret to register is a non-empty string")
    with _lock:
        _registered.add(value)


def forget_secrets() -> None:
    """Forget every registered secret, for example at the end of a run or between tests."""
    with _lock:
        _registered.clear()


def _env_secrets(environ: Mapping[str, str] | None) -> dict[str, str]:
    """Return the secret-looking variables of an environment (the process's by default) that have a value."""
    env = os.environ if environ is None else environ
    return {k: v for k, v in env.items() if SECRET_NAME.search(k) and v and v.strip()}


def secret_names(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return the names, never the values, of the environment variables treated as secrets."""
    return sorted(_env_secrets(environ))


def secret_values(environ: Mapping[str, str] | None = None, extra: Iterable[str] = ()) -> list[str]:
    """Return the distinct secret values (environment, registered, and `extra`), longest first."""
    with _lock:
        registered = set(_registered)
    values = {*_env_secrets(environ).values(), *registered, *(v for v in extra if v)}
    return sorted((v for v in values if v not in REDACTED), key=len, reverse=True)


def forms_of(value: str) -> set[str]:
    """Return the ways a secret can appear in text: raw, JSON-escaped (both ways) and percent-encoded (both ways)."""
    return {
        value,
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        quote(value, safe=""),
        quote_plus(value),
    }


def is_secret_key(key: str) -> bool:
    """Tell whether a mapping key names a secret: a known name, or one ending in an underscore and a known name."""
    name = key.strip().lower().replace("-", "_")
    return any(name == known or name.endswith("_" + known) for known in SECRET_KEYS)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


class Redactor:
    """Replaces a fixed set of secrets, and the values of keys that name one, in text and in JSON-like structures."""

    def __init__(self, secrets: Iterable[str] = (), *, keys: bool = True):
        values = [s for s in secrets if s and s not in REDACTED]
        self._whole: frozenset[str] = frozenset(f for s in values for f in forms_of(s))
        self._inside: tuple[str, ...] = tuple(
            sorted({f for s in values if len(s) >= MIN_SUBSTRING_LEN for f in forms_of(s)}, key=len, reverse=True)
        )
        self._numbers: frozenset[str] = frozenset(values)
        self._keys = keys

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, extra: Iterable[str] = ()) -> "Redactor":
        """Build a redactor from the environment (the process's by default), the registered secrets and `extra`."""
        return cls(secret_values(environ, extra))

    @classmethod
    def none(cls) -> "Redactor":
        """A redactor that changes nothing, for data that was redacted already (a replay reads a stored record)."""
        return cls((), keys=False)

    def text(self, value: str) -> str:
        """Return the string with every secret replaced: all of it if it equals one, else each long one inside it."""
        if value in self._whole:
            return REDACTED
        for form in self._inside:
            if form in value:
                value = value.replace(form, REDACTED)
        return value

    def _number_is_secret(self, number: int | float) -> bool:
        return bool(self._numbers) and (repr(number) if isinstance(number, float) else str(number)) in self._numbers

    def structure(self, obj: Any) -> tuple[Any, list[str]]:
        """Return a copy of a JSON-like structure with every secret replaced, and the paths of what was changed.

        Raises ValueError when two keys become the same once redacted, because merging them would lose a value.
        """
        paths: list[str] = []

        def walk(value: Any, path: str) -> Any:
            if isinstance(value, str):
                new = self.text(value)
                if new != value:
                    paths.append(path)
                return new
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if self._number_is_secret(value):
                    paths.append(path)
                    return REDACTED
                return value
            if isinstance(value, dict):
                out: dict = {}
                for index, (k, v) in enumerate(value.items()):
                    key = self.text(k) if isinstance(k, str) else k
                    here = _join(path, k if key == k else f"{{key {index}}}")
                    if key != k:
                        paths.append(here)
                    if key in out:
                        raise ValueError("two keys are the same once secret values are redacted")
                    if self._keys and isinstance(k, str) and is_secret_key(k):
                        out[key] = REDACTED
                        if v != REDACTED:
                            paths.append(here)
                    else:
                        out[key] = walk(v, here)
                return out
            if isinstance(value, (list, tuple)):
                return [walk(v, f"{path}[{i}]") for i, v in enumerate(value)]
            return value

        return walk(obj, ""), sorted(set(paths))


def redact_structure(
    obj: Any, environ: Mapping[str, str] | None = None, extra: Iterable[str] = ()
) -> tuple[Any, list[str]]:
    """Redact a JSON-like structure with the environment's, the registered and the `extra` secrets; see `Redactor`."""
    return Redactor.from_environment(environ, extra).structure(obj)


def redact_text(text: str, environ: Mapping[str, str] | None = None, extra: Iterable[str] = ()) -> tuple[str, int]:
    """Redact plain text such as a log line or an error message; return the text and how many places changed."""
    redactor = Redactor.from_environment(environ, extra)
    new = redactor.text(text)
    return new, int(new != text)
