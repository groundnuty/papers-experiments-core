# Provenance: new
"""The neutral turn that follows from a native message: what the backend that wrote it would have parsed from it (R03).

A record keeps each reply twice, as the loop saw it (the neutral turn: text, reasoning, tool calls) and as the provider
sent it and the backend put it into the conversation (the native message). Replay serves the neutral turn to the loop
and the native message to the conversation, so a record whose two disagree would replay a run that never happened. This
module derives the neutral turn from the native message with the rules of the backend that wrote it, which are the
adapter's: a tool call whose arguments were not a JSON object is kept in the history as `{"_raw": <the text>}`
(`expcore.models.adapters`), and the text is parsed again with the adapter's own `_parse_args`.

`openai_compat` and `anthropic` are the two vendor shapes; the stub backend writes the OpenAI shape. A backend of
another shape registers its parser with `register_native_parser`; a record of a backend with none is not replayable.
"""

import json
from collections.abc import Callable
from typing import Any

from expcore.models.adapters import _parse_args

NativeParser = Callable[[dict[str, Any]], dict[str, Any]]
NOT_AN_OBJECT = "arguments are not a JSON object"


def _call(call_id: Any, name: Any, arguments: Any, parse_error: str | None, raw: str | None) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": arguments, "parse_error": parse_error, "raw_arguments": raw}


def _only_raw(value: Any) -> str | None:
    """The text of a history entry `{"_raw": <text>}` that stands for arguments that were not an object, else None."""
    if isinstance(value, dict) and set(value) == {"_raw"} and isinstance(value["_raw"], str):
        return value["_raw"]
    return None


def _openai_call(tc: dict[str, Any]) -> dict[str, Any]:
    """The neutral call that one tool call of a chat-completions assistant message stands for."""
    function = tc.get("function") or {}
    sent = function.get("arguments") or "{}"
    if not isinstance(sent, str):
        return _call(tc.get("id"), function.get("name"), {}, "the native arguments are not text", None)
    raw = _only_raw(_loads(sent))
    if raw is not None:  # the history keeps arguments that were not an object as {"_raw": <text>}
        arguments, error = _parse_args(raw)
        if error is not None:
            return _call(tc.get("id"), function.get("name"), {}, error, raw)
    arguments, error = _parse_args(sent)
    return _call(tc.get("id"), function.get("name"), arguments, error, sent if error else None)


def openai_neutral(native: dict[str, Any]) -> dict[str, Any]:
    """Derive the neutral turn from a native assistant message of the chat-completions shape."""
    calls = [_openai_call(tc) for tc in native.get("tool_calls") or []]
    return {"text": native.get("content") or "", "reasoning": native.get("_reasoning") or None, "tool_calls": calls}


def anthropic_neutral(native: dict[str, Any]) -> dict[str, Any]:
    """Derive the neutral turn from a native assistant message of the Messages API shape."""
    texts, reasoning, calls = [], [], []
    for block in native.get("content") or []:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            texts.append(block.get("text", ""))
        elif kind == "tool_use":
            value = block.get("input")
            raw = _only_raw(value)
            not_an_object = raw is not None and not isinstance(_loads(raw), dict)
            if not_an_object:
                calls.append(_call(block.get("id"), block.get("name"), {}, NOT_AN_OBJECT, raw))
            else:
                calls.append(
                    _call(block.get("id"), block.get("name"), value if isinstance(value, dict) else {}, None, None)
                )
        elif kind in ("thinking", "redacted_thinking"):
            reasoning.append(block.get("thinking") or "")
    return {"text": "".join(texts), "reasoning": "\n".join(r for r in reasoning if r) or None, "tool_calls": calls}


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


_PARSERS: dict[str, NativeParser] = {
    "openai_compat": openai_neutral,
    "stub": openai_neutral,
    "anthropic": anthropic_neutral,
}


def register_native_parser(backend: str, parser: NativeParser) -> None:
    """Add the parser for the native messages of a backend, by the name its `info()` gives (`route.backend`)."""
    _PARSERS[backend] = parser


def neutral_of(backend: object, native: dict[str, Any]) -> dict[str, Any] | None:
    """Return the neutral turn a native message of the named backend stands for, or None if no parser is registered."""
    parser = _PARSERS.get(backend) if isinstance(backend, str) else None
    return None if parser is None else parser(native)


def normalised(neutral: dict[str, Any]) -> dict[str, Any]:
    """Return a recorded neutral turn in the form the parsers give: no reasoning is None, whatever empty it was."""
    return {**neutral, "reasoning": neutral.get("reasoning") or None}
