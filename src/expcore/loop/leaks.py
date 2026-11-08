# Provenance: new
"""Leaked tool calls: a call the model wrote as text, or in its reasoning, instead of making it (L10).

Some models, some of the time, print a call as JSON in their reply and expect it to run. The loop does not run it and
does not ask the model to try again: a reply with no tool call is an answer, and one whose text or reasoning holds a
call to an offered tool is labelled as a leak, so the analysis can count them.

A leak is a JSON object that names an offered tool (under `name`, `tool`, `tool_name` or `function`) and carries an
`arguments` or `parameters` object, as a JSON object or as a string holding one. That covers a bare object, one in a
code fence or a tag, the chat API's `{"type": "function", "function": {...}}` and a `tool_calls` list. JSON that names
no offered tool is domain content and is not a leak. The scan is bounded: the first 64 KiB of each text, a few
occurrences of each tool name, and a fixed number of decode attempts, so a hostile or huge reply costs a fixed amount.
"""

import json
from collections.abc import Collection
from typing import Any

SCAN_LIMIT = 64 * 1024  # characters of a reply that are looked at
MAX_OCCURRENCES = 32  # places a tool name is looked for, per name
MAX_STARTS = 16  # opening braces tried to the left of one occurrence
MAX_DECODES = 256  # JSON decode attempts per text
_MAX_DEPTH = 24  # how deep a decoded value is searched for a call
_NAME_KEYS = ("name", "tool", "tool_name", "function")
_ARGUMENT_KEYS = ("arguments", "parameters")


def leaked_tool(text: str | None, tool_names: Collection[str]) -> str | None:
    """Return the name of an offered tool that a JSON object in the first 64 KiB of `text` calls, else None."""
    if not text or not tool_names:
        return None
    window = text[:SCAN_LIMIT]
    decoder = json.JSONDecoder()
    decodes = 0
    for name in sorted(tool_names):
        needle = json.dumps(name)
        position, seen = window.find(needle), 0
        while position != -1 and seen < MAX_OCCURRENCES:
            seen += 1
            start = position
            for _ in range(MAX_STARTS):  # the object that holds the name opens somewhere to its left
                start = window.rfind("{", 0, start)
                if start == -1 or decodes >= MAX_DECODES:
                    break
                decodes += 1
                called = _called_in(_decode(decoder, window, start), tool_names, 0)
                if called is not None:
                    return called
            position = window.find(needle, position + len(needle))
    return None


def _decode(decoder: json.JSONDecoder, text: str, start: int) -> Any:
    """Decode the JSON value that starts at `start`; None when it is not valid JSON (a brace in prose)."""
    try:
        value, _end = decoder.raw_decode(text, start)
    except (ValueError, RecursionError):
        return None
    return value


def _called_in(value: Any, tool_names: Collection[str], depth: int) -> str | None:
    """Find a call to an offered tool in a decoded value, looking inside objects and lists."""
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, dict):
        named = _call(value, tool_names)
        if named is not None:
            return named
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            found = _called_in(item, tool_names, depth + 1)
            if found is not None:
                return found
    return None


def _call(obj: dict[str, Any], tool_names: Collection[str]) -> str | None:
    """Return the tool an object calls: it names an offered tool and carries an arguments or parameters object."""
    name = next((obj[k] for k in _NAME_KEYS if isinstance(obj.get(k), str) and obj[k] in tool_names), None)
    if name is None:
        return None
    return name if any(_is_object(obj.get(k)) for k in _ARGUMENT_KEYS) else None


def _is_object(value: Any) -> bool:
    """Tell whether a value is a JSON object, or a string that holds one (the chat API sends arguments as a string)."""
    if isinstance(value, dict):
        return True
    if isinstance(value, str) and value.lstrip().startswith("{"):
        return isinstance(_decode(json.JSONDecoder(), value.lstrip(), 0), dict)
    return False
