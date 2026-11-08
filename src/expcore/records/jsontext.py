# Provenance: new
"""Redact the values of secret-named keys inside a string that holds JSON, changing nothing else in it (R12).

A native message carries the arguments of a tool call as a JSON string. A value under a key that names a secret has to
go from that string, but re-writing the string from the parsed value would change the rest: `1.234567890123456789`
would become the nearest float and `1e400` would overflow. So the text is scanned with positions, and only the span of
each secret-keyed value is replaced; every other character stays as it was. A string that is itself JSON text inside a
value is handled the same way, and written back only if something in it changed.

The scan is strict JSON (RFC 8259) and nothing more, and numbers are read as text, never as values. It keeps its own
stack instead of calling itself, so the depth of the JSON is bounded by memory and not by the interpreter, and every
strict-JSON string is scanned in full. Text that is not strict JSON cannot be scanned unambiguously: `redact_keys`
returns None for it, and the caller falls back to redacting by value only. Only the depth to which a string holds
JSON text inside a string, each level of which doubles the escapes, takes a recursion, and that is bounded by the
logarithm of the text's size.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

MARKER = '"[redacted]"'
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_LITERALS = ("true", "false", "null")
_WHITESPACE = " \t\n\r"
_ESCAPES = '"\\/bfnrt'
_HEX = "0123456789abcdefABCDEF"

Edit = tuple[int, int, str]


class _Unscannable(Exception):
    """The text is not JSON that the scan can read with certainty."""


@dataclass
class _Frame:
    """An open object or array. For an object, the member being read: its key, where its value starts, and how many
    edits there were before it, so the edits inside a value that is replaced whole can be dropped."""

    is_object: bool
    property_names: bool = False  # the members of this object name things (it is the value of `properties`)
    key: str = ""
    value_start: int = 0
    mark: int = 0


class _Scanner:
    def __init__(self, text: str, is_secret_key: Callable[[str], bool]):
        self.text, self.is_secret_key, self.edits = text, is_secret_key, []

    def skip(self, i: int) -> int:
        while i < len(self.text) and self.text[i] in _WHITESPACE:
            i += 1
        return i

    def string(self, i: int) -> int:
        """Return the index after the string literal that starts at `i`."""
        if self.text[i : i + 1] != '"':
            raise _Unscannable
        i += 1
        while i < len(self.text):
            c = self.text[i]
            if c == '"':
                return i + 1
            if c < " ":
                raise _Unscannable
            if c == "\\":
                nxt = self.text[i + 1 : i + 2]
                digits = self.text[i + 2 : i + 6]
                if nxt and nxt in _ESCAPES:
                    i += 2
                elif nxt == "u" and len(digits) == 4 and all(h in _HEX for h in digits):
                    i += 6
                else:
                    raise _Unscannable
            else:
                i += 1
        raise _Unscannable

    def member(self, i: int, frame: _Frame) -> int:
        """Read an object member's key and colon from `i`, note them in the frame, and return where its value starts."""
        start = self.skip(i)
        end = self.string(start)
        frame.key = json.loads(self.text[start:end])
        colon = self.skip(end)
        if self.text[colon : colon + 1] != ":":
            raise _Unscannable
        frame.value_start = self.skip(colon + 1)
        frame.mark = len(self.edits)
        return frame.value_start

    def scalar(self, i: int) -> int:
        """Return the index after the string, number or literal that starts at `i`; a string may hold JSON text."""
        if self.text[i : i + 1] == '"':
            end = self.string(i)
            self.nested(i, end)
            return end
        for literal in _LITERALS:
            if self.text.startswith(literal, i):
                return i + len(literal)
        number = _NUMBER.match(self.text, i)
        if number:
            return number.end()
        raise _Unscannable

    def nested(self, start: int, end: int) -> None:
        """A string value that is JSON text is scanned in turn and, if it changed, written back as a string."""
        inner = json.loads(self.text[start:end])
        if inner.lstrip()[:1] in ("{", "["):
            changed = redact_keys(inner, self.is_secret_key)
            if changed is not None and changed != inner:
                self.edits.append((start, end, json.dumps(changed, ensure_ascii=False)))

    def finish_member(self, frame: _Frame, end: int) -> None:
        """A member's value ended at `end`: if its key names a secret the whole value goes, and what was found in it."""
        if self.is_secret_key(frame.key) and not frame.property_names:
            del self.edits[frame.mark :]
            if self.text[frame.value_start : end] != MARKER:
                self.edits.append((frame.value_start, end, MARKER))

    def scan(self) -> None:
        """Scan the whole text with an explicit stack; raise _Unscannable unless it is exactly one strict JSON value."""
        stack: list[_Frame] = []
        i = self.skip(0)
        while True:
            # begin a value at i: open a container (and go on into it), or read a scalar and fall through to unwind
            opener = self.text[i : i + 1]
            end = -1
            if opener in ("{", "["):
                parent = stack[-1] if stack else None
                frame = _Frame(opener == "{", property_names=parent is not None and parent.key == "properties")
                i = self.skip(i + 1)
                if self.text[i : i + 1] == ("}" if frame.is_object else "]"):
                    end = i + 1
                else:
                    stack.append(frame)
                    i = self.member(i, frame) if frame.is_object else i
                    continue
            else:
                end = self.scalar(i)
            # a value ended at `end`: close the members and containers it completes, or go on to the next value
            while True:
                if not stack:
                    if self.skip(end) != len(self.text):
                        raise _Unscannable
                    return
                frame = stack[-1]
                if frame.is_object:
                    self.finish_member(frame, end)
                i = self.skip(end)
                separator = self.text[i : i + 1]
                if separator == ("}" if frame.is_object else "]"):
                    stack.pop()
                    end = i + 1
                    continue
                if separator != ",":
                    raise _Unscannable
                i = self.member(i + 1, frame) if frame.is_object else self.skip(i + 1)
                break


def redact_keys(text: str, is_secret_key: Callable[[str], bool]) -> str | None:
    """Return the text with the value of every secret-named key replaced by the marker, all else unchanged.

    The same text comes back when nothing needed replacing. None means the text could not be scanned unambiguously
    (it is not strict JSON), and the caller redacts by value only.
    """
    scanner = _Scanner(text, is_secret_key)
    try:
        scanner.scan()
    except (_Unscannable, ValueError, IndexError, RecursionError):
        return None
    for begin, stop, replacement in sorted(scanner.edits, reverse=True):
        text = text[:begin] + replacement + text[stop:]
    return text
