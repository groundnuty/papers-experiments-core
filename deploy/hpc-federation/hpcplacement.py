"""The HPC render's `[placement]` table (O79): always in the rendered `platform.toml`, and complete.

The platform layer requires the table (`rangemap.load_placement`), and a reader that found the render without one
patched in a default of its own: defaults supplied separately by each tool are how a setting drifts unseen. So:

- **The template declares `holder`**, the one setting the core has no default for (`PlacementSettings.holder`): a
  provider the file declares. `check_template` is the preflight (`up` refuses a template without it before anything is
  submitted), and `complete` refuses the same at the render.
- **The render writes every other setting at the core's default** beside what the template declared: the fields of
  `PlacementSettings` are the one source of the numbers (`defaults`), none is held here, so a field added there is in
  the next render. A setting the template declares keeps its value; a table that declares every setting is returned
  as it came.

The table is completed in the template's own text, by adding the missing `key = value` pairs where the template
declares it: inside the braces of one `placement = { ... }` line, or after the header of one `[placement]` table.
(TOML cannot add to an inline table from elsewhere, nor reopen a table, so a spelling that is neither is refused.) The
result is read back as TOML and must be the declared document plus exactly the added settings, or the render fails:
no other table of the file is touched, and no setting is written twice.
"""

import json
import math
import re
from dataclasses import MISSING, fields
from typing import Any, Dict, List, Mapping, Optional

import hpcpins
import tomllib

from expcore_onedata.rangemap import PlacementSettings

HEADER = re.compile(r"\[[ \t]*placement[ \t]*\][ \t]*(?:#.*)?")  # the whole line of a `[placement]` table's header
INLINE = re.compile(r"(placement[ \t]*=[ \t]*\{)(.*?)(\}[ \t]*(?:#.*)?)")  # a root `placement = { ... }` line


def toml_scalar(value: Any) -> str:
    """One setting as TOML text: a boolean as `true` or `false` (tested before the integers, which it is one of), an
    integer and a finite float as Python spells them (TOML's spelling too) and a string as a basic string (JSON's).
    A value of another type, or a float TOML cannot hold, is refused: a setting that a later core adds with a type
    this cannot spell fails the render, and is never written as text the loader would read another way."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"a [placement] setting of {value} cannot be written as TOML")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"a [placement] setting of type {type(value).__name__} cannot be written as TOML")


def defaults() -> Dict[str, Any]:
    """The core's own default of every `[placement]` setting but the holder, which has none: `PlacementSettings`'
    fields, in their order. The one place the numbers are read from; none is written here."""
    table: Dict[str, Any] = {}
    for item in fields(PlacementSettings):
        if item.name == "holder":
            continue
        if item.default is MISSING:
            raise RuntimeError(f"PlacementSettings.{item.name} has no default for the render to write")
        table[item.name] = item.default
    return table


def declared_placement(document: Mapping[str, Any]) -> Dict[str, Any]:
    """The `[placement]` table a document declares, checked: it is a table with a `holder` that names a provider the
    document declares. Names the key and never a value."""
    if "placement" not in document:
        raise RuntimeError(
            "the platform template declares no [placement]: the core has no default for its 'holder', so the template "
            'declares it (placement = { holder = "<a provider of the file>" })'
        )
    table = document["placement"]
    if not isinstance(table, dict):
        raise RuntimeError("the platform template's [placement] must be a table")
    if "holder" not in table:
        raise RuntimeError(
            "the platform template's [placement] declares no 'holder': the core has no default for it, so the "
            "template declares it (the data provider of the spaces that name none)"
        )
    holder = table["holder"]
    if not isinstance(holder, str) or not holder:
        raise RuntimeError("the platform template's [placement]: 'holder' must be the name of a provider")
    if holder not in (document.get("providers") or {}):
        raise RuntimeError("the platform template's [placement]: 'holder' must be a provider the file declares")
    return table


def check_template(template: Optional[str]) -> None:
    """The refusal before anything is submitted: the template, read as TOML with its placeholders filled in as
    `hpcpins` fills them for its own check, declares a `[placement]` holder (`declared_placement`). A layout with no
    template renders no platform file and is not asked; a template that is no TOML once filled in is left to the
    renderer, which reads the file it writes."""
    if template is None:
        return
    try:
        document = tomllib.loads(hpcpins.ANY_PLACEHOLDER.sub("0", template))
    except tomllib.TOMLDecodeError:
        return
    declared_placement(document)


def insertion(lines: List[str], pairs: List[str]) -> List[str]:
    """The lines of a document with `pairs` added where it declares `[placement]`: after the header of the one table,
    or inside the braces of the one inline line. A document that declares it any other way is refused."""
    stripped = [line.rstrip("\r\n") for line in lines]
    headers = [i for i, line in enumerate(stripped) if HEADER.fullmatch(line)]
    inline = [i for i, line in enumerate(stripped) if INLINE.fullmatch(line)]
    if len(headers) + len(inline) != 1:
        raise RuntimeError(
            "the platform template declares [placement] in a form the render cannot add the core's defaults to: "
            "declare it as one `placement = { ... }` line of the root, or as one `[placement]` table"
        )
    out = list(lines)
    if headers:
        at = headers[0]
        if not out[at].endswith("\n"):
            out[at] += "\n"
        out[at + 1 : at + 1] = [f"{pair}\n" for pair in pairs]
        return out
    at = inline[0]
    before, inside, after = INLINE.fullmatch(stripped[at]).groups()
    kept = inside.rstrip()
    out[at] = f"{before}{kept + ', ' if kept else ' '}{', '.join(pairs)} {after}" + lines[at][len(stripped[at]) :]
    return out


def same(left: Any, right: Any) -> bool:
    """Whether two parsed documents are one, a boolean never taken for a number."""
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)


def complete(text: str) -> str:
    """The rendered platform file with its `[placement]` complete: what the template declared, and the core's default
    for every setting it did not (`defaults`). The text is returned as it came when nothing is missing. Refused, naming
    the key, when the template declares no holder (`declared_placement`) or declares the table in a spelling that cannot
    be added to; and when the completed file would not be exactly the declared document plus the added settings."""
    document = tomllib.loads(text)
    declared = declared_placement(document)
    missing = {key: value for key, value in defaults().items() if key not in declared}
    if not missing:
        return text
    pairs = [f"{key} = {toml_scalar(value)}" for key, value in missing.items()]
    completed = "".join(insertion(text.splitlines(keepends=True), pairs))
    expected = {**document, "placement": {**declared, **missing}}
    try:
        read_back = tomllib.loads(completed)
    except tomllib.TOMLDecodeError:
        read_back = None
    if read_back is None or not same(read_back, expected):
        raise RuntimeError("the render could not add the core's defaults to the template's [placement] as declared")
    return completed
