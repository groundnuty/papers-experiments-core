# Provenance: new
"""Leaked tool calls: a call the model wrote as text, or in its reasoning, instead of making it (L10).

Some models, some of the time, print a call as JSON and expect it to run. The loop does not run it and does not ask
the model to try again: a reply with no tool call is an answer, and one that is a call to an offered tool written out
is labelled as a leak, so the analysis can count them.

Only a top-level call envelope counts. The reply's text, or its reasoning, with an optional single fenced code block
taken off, must be exactly one JSON value, and that value must be one of:

- an object `{"name": <an offered tool>, "arguments" or "parameters": {...}}`;
- that object wrapped once, as `{"tool_call" or "function" or "tool": {...}}` with no other key;
- a non-empty list of such objects, wrapped or not.

Anything else is the model's content, however much it looks like a call: prose around the JSON, a tool name nested
deeper inside a document (an algorithm described as `{"algorithm": {"name": "search", ...}}`), a wrapper with other
keys. The text is only looked at when it is within 64 KiB, so a huge or hostile reply costs a fixed amount.
"""

import json
import re
from collections.abc import Collection
from typing import Any

SCAN_LIMIT = 64 * 1024  # characters; a longer text is not looked at
_WRAPPERS = ("tool_call", "function", "tool")
_ARGUMENT_KEYS = ("arguments", "parameters")
# One fenced block that is the whole text: an opening fence with an optional language, the body, a closing fence.
_FENCE = re.compile(r"\A```[A-Za-z0-9_+.-]*[ \t]*\n(.*?)\n?[ \t]*```\Z", re.DOTALL)


def leaked_tool(text: str | None, tool_names: Collection[str]) -> str | None:
    """Return the name of the offered tool that `text` is a written-out call to, else None."""
    if not text or not tool_names or len(text) > SCAN_LIMIT:
        return None
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    if not body or body[0] not in "{[":
        return None
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):  # not JSON, or nested past what the parser takes
        return None
    items = value if isinstance(value, list) else [value]
    called = [_envelope(item, tool_names) for item in items]
    return called[0] if called and all(name is not None for name in called) else None


def _envelope(item: Any, tool_names: Collection[str]) -> str | None:
    """The tool an object calls, as itself or wrapped once; None for anything that is not such an envelope."""
    if not isinstance(item, dict):
        return None
    if len(item) == 1:
        (key, inner), *_ = item.items()
        if key in _WRAPPERS:
            return _call(inner, tool_names)
    return _call(item, tool_names)


def _call(obj: Any, tool_names: Collection[str]) -> str | None:
    """The offered tool an object names, when it also carries an arguments or parameters object."""
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if isinstance(name, str) and name in tool_names and any(isinstance(obj.get(k), dict) for k in _ARGUMENT_KEYS):
        return name
    return None
