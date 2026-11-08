# Provenance: new
"""The neutral turn that follows from a native message: what the backend that wrote it would have parsed from it (R03).

A record keeps each reply twice, as the loop saw it (the neutral turn: text, reasoning, tool calls) and as the provider
sent it and the backend put it into the conversation (the native message). Replay serves the neutral turn to the loop
and the native message to the conversation, so a record whose two disagree would replay a run that never happened. This
module derives the neutral turn from the native message with the rules of the backend that wrote it, which are the
adapter's: a tool call whose arguments were not a JSON object is kept in the history as `{"_raw": <the text>}`
(`expcore.models.adapters`), and the text is parsed again with the adapter's own `_parse_args`.

Whether a call was malformed cannot be read from the native message: a valid call whose arguments are the object
`{"_raw": "[1]"}` and a malformed call whose raw text was `[1]` are the same bytes in the history. The recorded neutral
call says which it was (its `parse_error`), and the parser is told: it derives the call that way, and the result must
still agree with everything else the native message holds.

`openai_compat` and `anthropic` are the two vendor shapes; the stub backend writes the OpenAI shape. A backend of
another shape registers its parser with `register_native_parser`; a record of a backend with none is not replayable.
"""

import json
from collections.abc import Callable, Sequence
from typing import Any

from expcore.models.adapters import _parse_args

# A parser takes a native assistant message and, for each tool call in it, whether the record says it was malformed.
NativeParser = Callable[[dict[str, Any], Sequence[bool]], dict[str, Any]]
NOT_AN_OBJECT = "arguments are not a JSON object"


def _call(call_id: Any, name: Any, arguments: Any, parse_error: str | None, raw: str | None) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": arguments, "parse_error": parse_error, "raw_arguments": raw}


def _only_raw(value: Any) -> str | None:
    """The text of a history entry `{"_raw": <text>}` that stands for arguments that were not an object, else None."""
    if isinstance(value, dict) and set(value) == {"_raw"} and isinstance(value["_raw"], str):
        return value["_raw"]
    return None


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _flag(flags: Sequence[bool], index: int) -> bool:
    """Whether the record says the call at this position was malformed; a call the record does not have is not."""
    return index < len(flags) and flags[index]


def _openai_call(tc: dict[str, Any], malformed: bool) -> dict[str, Any]:
    """The neutral call that one tool call of a chat-completions assistant message stands for."""
    function = tc.get("function") or {}
    call_id, name = tc.get("id"), function.get("name")
    sent = function.get("arguments") or "{}"
    if not isinstance(sent, str):
        return _call(call_id, name, {}, "the native arguments are not text", None)
    if malformed:  # the history keeps the raw text of arguments that were not an object as {"_raw": <text>}
        raw = _only_raw(_loads(sent))
        if raw is None:
            return _call(
                call_id, name, {}, "the native arguments of a call recorded as malformed hold no raw text", None
            )
        arguments, error = _parse_args(raw)
        return _call(call_id, name, arguments, error, raw if error else None)
    arguments, error = _parse_args(sent)
    return _call(call_id, name, arguments, error, sent if error else None)


def openai_neutral(native: dict[str, Any], malformed: Sequence[bool] = ()) -> dict[str, Any]:
    """Derive the neutral turn from a native assistant message of the chat-completions shape."""
    calls = [_openai_call(tc, _flag(malformed, i)) for i, tc in enumerate(native.get("tool_calls") or [])]
    return {"text": native.get("content") or "", "reasoning": native.get("_reasoning") or None, "tool_calls": calls}


def anthropic_neutral(native: dict[str, Any], malformed: Sequence[bool] = ()) -> dict[str, Any]:
    """Derive the neutral turn from a native assistant message of the Messages API shape."""
    texts, reasoning, calls = [], [], []
    for block in native.get("content") or []:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            texts.append(block.get("text", ""))
        elif kind == "tool_use":
            value = block.get("input")
            if _flag(
                malformed, len(calls)
            ):  # the history keeps an input that was not an object as {"_raw": <its JSON>}
                raw = _only_raw(value)
                broken = raw is None or isinstance(_loads(raw), dict)
                error = (
                    "the native input of a call recorded as malformed holds no raw text" if broken else NOT_AN_OBJECT
                )
                calls.append(_call(block.get("id"), block.get("name"), {}, error, raw))
            else:
                calls.append(
                    _call(block.get("id"), block.get("name"), value if isinstance(value, dict) else {}, None, None)
                )
        elif kind in ("thinking", "redacted_thinking"):
            reasoning.append(block.get("thinking") or "")
    return {"text": "".join(texts), "reasoning": "\n".join(r for r in reasoning if r) or None, "tool_calls": calls}


_PARSERS: dict[str, NativeParser] = {
    "openai_compat": openai_neutral,
    "stub": openai_neutral,
    "anthropic": anthropic_neutral,
}


def register_native_parser(backend: str, parser: NativeParser) -> None:
    """Add the parser for the native messages of a backend, by the name its `info()` gives (`route.backend`)."""
    _PARSERS[backend] = parser


def neutral_of(backend: object, native: dict[str, Any], malformed: Sequence[bool] = ()) -> dict[str, Any] | None:
    """Return the neutral turn a native message of the named backend stands for, or None if no parser is registered.

    `malformed` says, for each tool call in the message, whether the record holds that call as malformed.
    """
    parser = _PARSERS.get(backend) if isinstance(backend, str) else None
    return None if parser is None else parser(native, malformed)


def malformed_flags(neutral: dict[str, Any]) -> list[bool]:
    """Return, for each call of a recorded neutral turn, whether the record holds it as malformed."""
    return [c.get("parse_error") is not None for c in neutral.get("tool_calls") or []]


def normalised(neutral: dict[str, Any]) -> dict[str, Any]:
    """Return a recorded neutral turn in the form the parsers give: no reasoning is None, whatever empty it was."""
    return {**neutral, "reasoning": neutral.get("reasoning") or None}
