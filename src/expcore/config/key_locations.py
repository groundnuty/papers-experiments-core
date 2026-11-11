# Provenance: new
"""Where a model's key lives: never part of the experiment, and never published in a replication package (K13).

`key_env` and `key_file` of a `models.toml` entry say where a request finds its key. That is deployment: the key goes
into the request's authorisation, never into what the model receives, and the location never vouched for the key or
its account anyway (the same path with another key in it was the same configuration). A location can name what a
package must not show, though: a home directory, an earlier study's folder, an account.

`withhold_key_locations` gives models.toml with each such value replaced by a fixed placeholder, `KEY_FILE_WITHHELD` or
`KEY_ENV_WITHHELD`, and every other byte as it was. That form is the file's identity:

- the code identity hashes models.toml in that form (`CodeScope.canonical`, set by `study.config`), and so does the
  campaign fingerprint, in the file's hash and in each model's digest (`withheld_entry`), so moving a key never changes
  either. A table that names no key location is its own form, so its hashes are what they always were;
- every replication package ships models.toml in that form, so the identity a reader recomputes from the shipped file
  is the one the records were checked under, with no transformation to take on trust. The build refuses a package
  whose models.toml still holds any other key location (`location_findings`), a safety net behind the rewrite.

The rewrite is textual and checked. A value is replaced only on a line of its own in a model's table
(`key_file = "..."`, the key bare or quoted, the value a one-line string, a comment after it kept), and the file is
parsed before and after: every value but the key locations must be unchanged, and every key location must now be the
placeholder. A key location written any other way (in an inline table, under a dotted key, as a multi-line string)
cannot be withheld, so loading the table refuses it, naming the model and the key, and no such study runs. Hashing
never refuses: a file the rewrite cannot reach is hashed as it is, and only a table the load refuses can be one.
"""

import dataclasses
import re
import tomllib
from collections.abc import Mapping
from typing import Any

from expcore.config.loading import ConfigError

KEY_FILE_WITHHELD = "<withheld>"  # where the key file lives is never published
KEY_ENV_WITHHELD = "KEY_WITHHELD"  # a variable name, as key_env must be one, and none an author chose
PLACEHOLDERS = {"key_env": KEY_ENV_WITHHELD, "key_file": KEY_FILE_WITHHELD}
FOUND_KEY = "a key location, which a package never holds (the build writes a placeholder there)"

_ASSIGNMENT = re.compile(
    r"(?P<lead>[ \t]*)(?P<key>(?P<quote>[\"']?)(?P<name>key_env|key_file)(?P=quote))(?P<eq>[ \t]*=[ \t]*)"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\"|'[^']*')(?P<rest>[ \t]*(?:#.*)?)"
)
_LINES = re.compile(r"(?<=\n)")  # TOML ends a line at LF (CRLF included) and nowhere else


class KeyLocationUnreachable(ConfigError):
    """A key location the textual rewrite cannot replace, so a package could not withhold it."""


def _header(line: str) -> tuple[str, ...] | None:
    """The keys of a standard table header (`[models."a.b"]`), or None for any other line."""
    start = line.lstrip()
    if not start.startswith("[") or start.startswith("[["):
        return None
    try:
        parsed: Any = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None  # not a header: a line of a multi-line array, say
    path: list[str] = []
    while isinstance(parsed, dict) and len(parsed) == 1:
        key, parsed = next(iter(parsed.items()))
        path.append(key)
    return tuple(path) if path and parsed == {} else None


def _replaced(text: str) -> tuple[str, int]:
    """The text with each key location on a line of its own in a model's table replaced, and how many were."""
    table: tuple[str, ...] = ()
    out, count = [], 0
    for line in _LINES.split(text):
        content = line.rstrip("\n").removesuffix("\r")
        ending = line[len(content) :]
        table = _header(content) or table
        match = _ASSIGNMENT.fullmatch(content) if len(table) == 2 and table[0] == "models" else None
        if match is None:
            out.append(line)
            continue
        value = PLACEHOLDERS[match["name"]]
        out.append(f'{match["lead"]}{match["key"]}{match["eq"]}"{value}"{match["rest"]}{ending}')
        count += 1
    return "".join(out), count


def _entries(parsed: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The model entries of a parsed table that are tables."""
    models = parsed.get("models")
    if not isinstance(models, Mapping):
        return {}
    return {key: entry for key, entry in models.items() if isinstance(entry, Mapping)}


def _expected(parsed: dict[str, Any]) -> dict[str, Any]:
    """The parsed table with every key location of every model replaced by its placeholder, and nothing else changed."""
    entries = _entries(parsed)
    if not entries:
        return parsed
    models = {key: {f: PLACEHOLDERS.get(f, v) for f, v in entry.items()} for key, entry in entries.items()}
    return {**parsed, "models": {**parsed["models"], **models}}


def _unreached(got: dict[str, Any], wanted: dict[str, Any]) -> str:
    """Name the first key location the rewrite did not replace: `[models.<key>]: key '<field>'`."""
    now = _entries(got)
    for key, entry in _entries(wanted).items():
        for field in PLACEHOLDERS:
            if field in entry and now.get(key, {}).get(field) != entry[field]:
                return f"[models.{key}]: key {field!r}"
    return "a key location"


def withhold_key_locations(data: bytes) -> bytes:
    """models.toml with every key location replaced by its placeholder, and every other byte as it was.

    The bytes come back unchanged, the same object, when they name no key location or are not a TOML table at all
    (which loading the table refuses). A placeholder already there is written again in the one form, so two tables
    that differ only in where their keys live give the same bytes. Raises KeyLocationUnreachable, naming the model
    and the key, when a key location is written where the rewrite cannot reach it.
    """
    try:
        text = data.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return data
    if not any(field in entry for entry in _entries(parsed).values() for field in PLACEHOLDERS):
        return data
    rewritten, count = _replaced(text)
    got = tomllib.loads(rewritten) if count else parsed
    wanted = _expected(parsed)
    if got != wanted:
        raise KeyLocationUnreachable(
            f"{_unreached(got, wanted)} cannot be withheld from a replication package as it is written; write it on "
            'a line of its own in the model\'s table, as key_file = "..." or key_env = "..."'
        )
    return rewritten.encode("utf-8") if count else data


def hashed_form(data: bytes) -> bytes:
    """The bytes the identity hashes for models.toml: its withheld form, or the bytes as they are when it has none."""
    try:
        return withhold_key_locations(data)
    except KeyLocationUnreachable:
        return data  # only a table the load refuses, which never runs; hashed as it is, it counts more, not less


def withheld_entry(spec: Any) -> dict[str, Any]:
    """A model entry as the campaign fingerprint digests it: its settings, each key location as its placeholder.

    An entry that names no key location gives exactly its settings, so its digest is the one it always had.
    """
    entry = dataclasses.asdict(spec)
    return {**entry, **{field: value for field, value in PLACEHOLDERS.items() if entry.get(field) is not None}}


def location_findings(data: bytes) -> list[str]:
    """The key locations a package's models.toml still holds, each as `[models.<key>]: key '<field>': ...` and never
    with its value; a file that cannot be read as TOML is one finding."""
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ["cannot be read as TOML, so its key locations cannot be checked"]
    return [
        f"[models.{key}]: key {field!r}: {FOUND_KEY}"
        for key, entry in _entries(parsed).items()
        for field, placeholder in PLACEHOLDERS.items()
        if field in entry and entry[field] != placeholder
    ]
