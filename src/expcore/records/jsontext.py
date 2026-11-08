# Provenance: new
"""Redact the values of secret-named keys inside a string that holds JSON, changing nothing else in it (R12).

A native message carries the arguments of a tool call as a JSON string. A value under a key that names a secret has to
go from that string, but re-writing the string from the parsed value would change the rest: `1.234567890123456789`
would become the nearest float and `1e400` would overflow. So the text is scanned with positions, and only the span of
each secret-keyed value is replaced; every other character stays as it was. A string that is itself JSON text inside a
value is handled the same way, and written back only if something in it changed.

The scan is strict JSON (RFC 8259) and nothing more, and numbers are read as text, never as values. Text that is not
valid JSON in that sense, or nests deeper than `MAX_DEPTH`, cannot be scanned unambiguously: `redact_keys` returns None
for it, and the caller falls back to redacting by value only.
"""

import json
import re
from collections.abc import Callable

MAX_DEPTH = 64
MARKER = '"[redacted]"'
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_LITERALS = ("true", "false", "null")
_WHITESPACE = " \t\n\r"
_ESCAPES = '"\\/bfnrt'
_HEX = "0123456789abcdefABCDEF"

Edit = tuple[int, int, str]


class _Unscannable(Exception):
    """The text is not JSON that the scan can read with certainty."""


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

    def value(self, i: int, depth: int, own_key: str | None = None) -> int:
        """Scan the value that starts at `i`, recording edits; return the index after it.

        `own_key` is the key the value is under; the members of the object under `properties` name things."""
        if depth > MAX_DEPTH:
            raise _Unscannable
        c = self.text[i : i + 1]
        if c == "{":
            return self.obj(i, depth, property_names=own_key == "properties")
        if c == "[":
            return self.array(i, depth)
        if c == '"':
            end = self.string(i)
            self.nested(i, end, depth)
            return end
        for literal in _LITERALS:
            if self.text.startswith(literal, i):
                return i + len(literal)
        number = _NUMBER.match(self.text, i)
        if number:
            return number.end()
        raise _Unscannable

    def nested(self, start: int, end: int, depth: int) -> None:
        """A string value that is JSON text is scanned in turn and, if it changed, written back as a string."""
        inner = json.loads(self.text[start:end])
        if inner.lstrip()[:1] in ("{", "["):
            changed = redact_keys(inner, self.is_secret_key, depth + 1)
            if changed is not None and changed != inner:
                self.edits.append((start, end, json.dumps(changed, ensure_ascii=False)))

    def obj(self, i: int, depth: int, property_names: bool) -> int:
        i = self.skip(i + 1)
        if self.text[i : i + 1] == "}":
            return i + 1
        while True:
            key_end = self.string(self.skip(i))
            key = json.loads(self.text[self.skip(i) : key_end])
            i = self.skip(key_end)
            if self.text[i : i + 1] != ":":
                raise _Unscannable
            start = self.skip(i + 1)
            if self.is_secret_key(key) and not property_names:
                before = len(self.edits)
                end = self.value(start, depth + 1, own_key=key)
                del self.edits[before:]  # the whole value goes, so nothing inside it matters
                if self.text[start:end] != MARKER:
                    self.edits.append((start, end, MARKER))
            else:
                end = self.value(start, depth + 1, own_key=key)
            i = self.skip(end)
            separator = self.text[i : i + 1]
            if separator == "}":
                return i + 1
            if separator != ",":
                raise _Unscannable
            i += 1

    def array(self, i: int, depth: int) -> int:
        i = self.skip(i + 1)
        if self.text[i : i + 1] == "]":
            return i + 1
        while True:
            i = self.skip(self.value(self.skip(i), depth + 1))
            separator = self.text[i : i + 1]
            if separator == "]":
                return i + 1
            if separator != ",":
                raise _Unscannable
            i += 1


def redact_keys(text: str, is_secret_key: Callable[[str], bool], depth: int = 0) -> str | None:
    """Return the text with the value of every secret-named key replaced by the marker, all else unchanged.

    The same text comes back when nothing needed replacing. None means the text could not be scanned unambiguously
    (it is not strict JSON, or it nests too deeply), and the caller redacts by value only.
    """
    scanner = _Scanner(text, is_secret_key)
    try:
        start = scanner.skip(0)
        end = scanner.skip(scanner.value(start, depth))
        if end != len(text):
            return None
    except (_Unscannable, ValueError, IndexError):
        return None
    for begin, stop, replacement in sorted(scanner.edits, reverse=True):
        text = text[:begin] + replacement + text[stop:]
    return text
