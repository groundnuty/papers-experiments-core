# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/record.py:189-304
"""Secret redaction: no record is written with the value of a secret in it (R12).

A secret comes from three places, and redaction works on parsed values, so its output is still valid JSON:

1. **Environment variables.** The value of every variable the caller declares (`secret_env` on the record builder; the
   `key_env` of every model in use is added by the builder), and of every variable whose name ends in `_API_KEY`,
   `_APIKEY`, `_SECRET`, `_SECRET_KEY`, `_PASSWORD`, `_PASSWD`, `_ACCESS_TOKEN`, `_AUTH_TOKEN` or `_TOKEN`, in any case.
   The suffix ends the name, so `MAX_TOKENS`, `SSH_AUTH_SOCK` and `KEYBOARD_LAYOUT` are not secrets.
2. **A registered secret.** `register_secret(value)` adds a value for the rest of the process, and nothing removes
   it: the registry only grows. A token minted at run time (the platform layer mints the tokens of both planes) is in
   no environment variable, so the code that mints it must register it, or it is not redacted. Redaction reads the
   registry when it runs, at the build of a record and again at its write, so a value registered during a run, by a
   provider that minted it, is caught. `build_record(secrets=...)` takes values for one record only.
3. **A key that names a secret.** The value of a mapping key called token, api_key, apikey, password, secret,
   authorization or bearer (or ending in `_` and one of them, such as `x-api-key` or `client_secret`), in any case, is
   replaced whatever it is, so a credential nothing told us about is still caught. A JSON-schema definition is
   exempt, since it names things and holds no value: the tools of a record, and any mapping under a `properties` key.
   The rule reaches inside a string that is itself JSON, such as the arguments of a tool call in a native
   message, and replaces only the span of the secret-keyed value; every other character of the string, a number
   such as 1e400 included, stays as it was (`jsontext.py`). A string that cannot be scanned as strict JSON is
   redacted by value only.

Every secret value of 4 or more characters is replaced wherever it occurs inside a string, in values and in keys,
and so is the fragment of one that the output cap leaves where it cut a result: 4 or more characters just before the
cap's notice that begin a secret (a cap that cuts a secret after six characters leaves six characters of it). That is
the only place a fragment is looked for, and it becomes one marker.
A value under 4 characters would match ordinary text, so it is refused instead, with a `ConfigError` that names the
variable and never shows the value. A number whose string form is a secret is replaced too.

A secret is redacted in three base forms, raw, as Python's repr shows it and as the repr of its UTF-8 bytes shows
it, since an exception's text often holds a repr (`KeyError(secret)` escapes a quote when the secret holds both
kinds), and in the forms derived from each of them: JSON-escaped up to three levels of escaping (text inside a JSON
string inside a JSON string) and percent-encoded, since a native message carries text in JSON strings and a URL
carries it percent-encoded. A secret that was replaced is gone: neither the text nor a decoding of it holds the
value.

In plain text that is never sealed (the harness-error log, a message printed to the author: `Redactor.with_lines`,
`redact_text`), each line of a multi-line secret that is at least the minimum length is a secret of its own as well,
since a formatter can put something between the lines (an exception group prefixes each with `| `). Never in a
record: a record holds a multi-line secret whole or JSON-escaped, which the whole-secret forms match, and a line that
is a common word would rename a key and break the record's seal.

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

from expcore.config.loading import ConfigError
from expcore.loop.capping import TRUNCATION_NOTICE
from expcore.records.jsontext import redact_keys

SECRET_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|APIKEY|SECRET_KEY|SECRET|PASSWORD|PASSWD|ACCESS_TOKEN|AUTH_TOKEN|TOKEN)$", re.IGNORECASE
)
SECRET_KEYS = ("token", "api_key", "apikey", "password", "secret", "authorization", "bearer")
MIN_SECRET_LEN = 4
ESCAPING_LEVELS = 3
REDACTED = "[redacted]"
# The line the output cap puts after what it kept: the text before it is where a cut secret ends.
_CAP_NOTICE = re.compile(
    re.escape(TRUNCATION_NOTICE).replace(r"\{dropped\}", r"\d+").replace(r"\{total\}", r"\d+") + r"\Z"
)
SCHEMA_ROOT = "tools"  # the top-level record field that holds the JSON-schema definitions of the tools

_registered: set[str] = set()
_lock = threading.Lock()


def _refuse_short(value: str, what: str) -> None:
    """Refuse a secret too short to redact without damaging ordinary text, naming where it came from, not what it is."""
    if len(value) < MIN_SECRET_LEN:
        raise ConfigError(
            f"{what} holds a secret of fewer than {MIN_SECRET_LEN} characters, which cannot be redacted without "
            "redacting ordinary text; use a longer value"
        )


def register_secret(value: str) -> None:
    """Add a secret minted at run time to the values redacted from every record written from now on.

    The platform layer calls this for each token it mints, before the token is used anywhere a record could see it.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("a secret to register is a non-empty string")
    _refuse_short(value, "a registered secret")
    with _lock:
        _registered.add(value)


def secret_env_names(environ: Mapping[str, str] | None = None, declared: Iterable[str] = ()) -> list[str]:
    """Return the names, never the values, of the environment variables treated as secrets that have a value.

    A variable is one when its name ends as the pattern says, or the caller declared it.
    """
    env = os.environ if environ is None else environ
    named = set(declared)
    return sorted(n for n, v in env.items() if v and v.strip() and (n in named or SECRET_NAME.search(n)))


def secret_values(
    environ: Mapping[str, str] | None = None,
    extra: Iterable[str] = (),
    declared: Iterable[str] = (),
    *,
    skip_short: bool = False,
) -> list[str]:
    """Return the distinct secret values (environment, registered and `extra`), longest first.

    Raises ConfigError, naming the variable, for a value under the minimum length; with `skip_short` such a value is
    left out of the list instead (`short_secret_values` gives them, for a redactor that replaces them as tokens).
    """
    env = os.environ if environ is None else environ

    def keep(value: str, what: str) -> bool:
        if skip_short and len(value) < MIN_SECRET_LEN:
            return False
        _refuse_short(value, what)
        return True

    values = {env[name] for name in secret_env_names(env, declared) if keep(env[name], f"environment variable {name}")}
    with _lock:
        registered = {value for value in _registered if keep(value, "a registered secret")}
    values |= {value for value in extra if value and keep(value, "a secret given for this record")}
    values |= registered
    return sorted((v for v in values if v not in REDACTED), key=len, reverse=True)


def _base_forms(value: str) -> set[str]:
    """A secret as it is, and inside Python's repr of it and of its UTF-8 bytes (without the repr's quotes)."""
    return {value, repr(value)[1:-1], repr(value.encode("utf-8", "surrogatepass"))[2:-1]}


def short_secret_values(
    environ: Mapping[str, str] | None = None, extra: Iterable[str] = (), declared: Iterable[str] = ()
) -> list[str]:
    """Return the secret values `secret_values` refuses as too short (environment, registered and `extra`)."""
    env = os.environ if environ is None else environ
    with _lock:
        registered = set(_registered)
    named = {env[name] for name in secret_env_names(env, declared)}
    short = {v for v in (*named, *registered, *extra) if v and len(v) < MIN_SECRET_LEN}
    return sorted(short, key=len, reverse=True)


def forms_of(value: str, *, lines: bool = False) -> set[str]:
    """Return the ways a secret can appear in text: each base form (raw, the repr of the string, the repr of its
    bytes), and with `lines` (plain text only, never a record) of each of its lines of the minimum length or more too,
    and each of those JSON-escaped up to three levels and percent-encoded."""
    bases = _base_forms(value)
    for line in value.splitlines() if lines else ():
        if len(line) >= MIN_SECRET_LEN:
            bases |= _base_forms(line)
    forms, level = set(bases), set(bases)
    for _ in range(ESCAPING_LEVELS):
        level = {json.dumps(v)[1:-1] for v in level} | {json.dumps(v, ensure_ascii=False)[1:-1] for v in level}
        forms |= level
    return forms | {quote(v, safe="") for v in bases} | {quote_plus(v) for v in bases}


def is_secret_key(key: str) -> bool:
    """Tell whether a mapping key names a secret: a known name, or one ending in an underscore and a known name."""
    name = key.strip().lower().replace("-", "_")
    return any(name == known or name.endswith("_" + known) for known in SECRET_KEYS)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


class Redactor:
    """Replaces a fixed set of secrets, and the values of keys that name one, in text and in JSON-like structures."""

    def __init__(
        self, secrets: Iterable[str] = (), *, keys: bool = True, short: Iterable[str] = (), lines: bool = False
    ):
        values = [s for s in secrets if s and s not in REDACTED]
        for value in values:
            _refuse_short(value, "a secret")
        short = [s for s in short if s and s not in REDACTED]
        self._values, self._short, self._keys, self._lines = tuple(values), tuple(short), keys, lines
        # A secret too short to replace wherever it occurs is replaced only where it stands as a token of its own
        # (no letter, digit or underscore on either side): for a diagnostic, never for a record, which refuses it.
        tokens = sorted({f for s in short for f in forms_of(s, lines=lines)}, key=len, reverse=True)
        self._tokens = tuple(re.compile(rf"(?<!\w){re.escape(form)}(?!\w)") for form in tokens)
        inside = {f for s in values for f in forms_of(s, lines=lines)}
        self._inside: tuple[str, ...] = tuple(sorted(inside, key=len, reverse=True))
        self._numbers: frozenset[str] = frozenset(values)

    def with_lines(self) -> "Redactor":
        """The same redactor for plain text that is never sealed (a log, a printed message): each line of a
        multi-line secret is replaced on its own too."""
        return Redactor(self._values, keys=self._keys, short=self._short, lines=True)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        extra: Iterable[str] = (),
        declared: Iterable[str] = (),
        *,
        short_tokens: bool = False,
        lines: bool = False,
    ) -> "Redactor":
        """Build a redactor from the environment (the process's by default), the registered secrets, `extra` values and
        the variables named in `declared`. A secret under the minimum length is refused, or with `short_tokens`, for a
        diagnostic on the author's terminal, replaced where it stands as a token of its own. `extra` is read once,
        whatever iterable it is. `lines`: see `with_lines`."""
        extra = tuple(extra)
        long = secret_values(environ, extra, declared, skip_short=short_tokens)
        short = short_secret_values(environ, extra, declared) if short_tokens else ()
        return cls(long, short=short, lines=lines)

    @classmethod
    def none(cls) -> "Redactor":
        """A redactor that changes nothing, for data that was redacted already (a replay reads a stored record)."""
        return cls((), keys=False)

    def text(self, value: str) -> str:
        """Return the string with every secret replaced, and every fragment of one that a cut left at either end."""
        for form in self._inside:
            if form in value:
                value = value.replace(form, REDACTED)
        for token in self._tokens:
            value = token.sub(REDACTED, value)
        return self._fragments(value) if self._inside else value

    def _fragments(self, value: str) -> str:
        """Replace, with one marker, the characters just before the cap's notice that begin a secret: the cut point.

        The output cap keeps the start of a result and writes its notice after it, so a secret the limit fell inside is
        cut, and what is left of it ends the kept text. That is the only place a fragment is looked for: four or more
        characters there that start a secret in any of its forms. A text with no notice is not a cut text, and ordinary
        text that happens to begin like a secret, anywhere else, is left alone.
        """
        notice = _CAP_NOTICE.search(value)
        if notice is None:
            return value
        head, tail = value[: notice.start()], value[notice.start() :]
        if head.endswith(REDACTED):
            return value
        cut = max((self._overlap(head, form) for form in self._inside), default=0)
        return head[:-cut] + REDACTED + tail if cut else value

    @staticmethod
    def _overlap(head: str, form: str) -> int:
        """The length of the longest fragment, at least 4 characters and shorter than the form, with which `head` ends
        and the form begins."""
        for k in range(min(len(head), len(form) - 1), MIN_SECRET_LEN - 1, -1):
            if head.endswith(form[:k]):
                return k
        return 0

    def _number_is_secret(self, number: int | float) -> bool:
        return bool(self._numbers) and (repr(number) if isinstance(number, float) else str(number)) in self._numbers

    def structure(self, obj: Any) -> tuple[Any, list[str]]:
        """Return a copy of a JSON-like structure with every secret replaced, and the paths of what was changed.

        Raises ValueError when two keys become the same once redacted, because merging them would lose a value.
        """
        paths: list[str] = []

        def in_json_text(text: str, path: str) -> str:
            """Apply the rule about secret-named keys inside a string that holds a JSON object or array (a tool call's
            arguments are one in a native message), replacing only the spans of their values. A string that cannot be
            scanned is left as the redaction by value made it."""
            if text.lstrip()[:1] not in ("{", "["):
                return text
            result = redact_keys(text, is_secret_key)
            return text if result is None else result

        def walk(value: Any, path: str, schema: bool = False, property_names: bool = False) -> Any:
            if isinstance(value, str):
                new = self.text(value)
                if self._keys and not schema:
                    new = in_json_text(new, path)
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
                    named_secret = isinstance(k, str) and is_secret_key(k)
                    if self._keys and named_secret and not (schema or property_names):
                        out[key] = REDACTED
                        if v != REDACTED:
                            paths.append(here)
                    else:
                        in_schema = schema or (path == "" and k == SCHEMA_ROOT)
                        out[key] = walk(v, here, in_schema, k == "properties")
                return out
            if isinstance(value, (list, tuple)):
                return [walk(v, f"{path}[{i}]", schema) for i, v in enumerate(value)]
            return value

        return walk(obj, ""), sorted(set(paths))


def redact_structure(
    obj: Any, environ: Mapping[str, str] | None = None, extra: Iterable[str] = (), declared: Iterable[str] = ()
) -> tuple[Any, list[str]]:
    """Redact a JSON-like structure with the environment's, the registered and the `extra` secrets; see `Redactor`."""
    return Redactor.from_environment(environ, extra, declared).structure(obj)


def redact_text(
    text: str, environ: Mapping[str, str] | None = None, extra: Iterable[str] = (), declared: Iterable[str] = ()
) -> tuple[str, int]:
    """Redact plain text such as a log line or an error message, each line of a multi-line secret too (`with_lines`);
    return the text and how many places changed."""
    new = Redactor.from_environment(environ, extra, declared, lines=True).text(text)
    return new, int(new != text)
