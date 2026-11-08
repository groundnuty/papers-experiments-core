# Provenance: new
"""Check a tool call's arguments against the tool's JSON schema, enough to turn a wrong call into an error result.

This is not a full JSON Schema validator. It covers what a model gets wrong: a missing required argument, a value of
the wrong type, a value outside an enum, an argument the schema does not list when it forbids extras. Anything else
in the schema is left to the tool, which stays authoritative (L02).
"""

from typing import Any

_PROBLEM_LIMIT = 8


def json_type(value: Any) -> str:
    """Return the JSON Schema type name of a decoded JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_type(value: Any, expected: str) -> bool:
    """Tell whether a value has the named JSON type; an integral float is an integer, an integer is a number."""
    actual = json_type(value)
    if expected == "number":
        return actual in ("integer", "number")
    if expected == "integer":
        return actual == "integer" or (actual == "number" and float(value).is_integer())
    return actual == expected


def _walk(value: Any, schema: Any, where: str, problems: list[str]) -> None:
    """Add the problems of one value against one schema, recursing into properties and items."""
    if not isinstance(schema, dict) or len(problems) >= _PROBLEM_LIMIT:
        return
    expected = schema.get("type")
    names = [expected] if isinstance(expected, str) else [t for t in expected or () if isinstance(t, str)]
    if names and not any(_is_type(value, name) for name in names):
        problems.append(f"{where}: expected {' or '.join(names)}, got {json_type(value)}")
        return
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        problems.append(f"{where}: must be one of {schema['enum']!r}")
    if isinstance(value, dict):
        _walk_object(value, schema, where, problems)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _walk(item, schema["items"], f"{where}[{index}]", problems)


def _walk_object(value: dict, schema: dict, where: str, problems: list[str]) -> None:
    """Check required keys, extra keys when forbidden, and each listed property."""
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for key in schema.get("required") or ():
        if key not in value:
            problems.append(f"{where}: missing required argument {key!r}")
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in properties:
                problems.append(f"{where}: unexpected argument {key!r}")
    for key, sub in properties.items():
        if key in value:
            _walk(value[key], sub, f"{where}.{key}" if where != "arguments" else f"argument {key!r}", problems)


def argument_problems(arguments: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Return what is wrong with the arguments, as short sentences; empty when they pass."""
    problems: list[str] = []
    _walk(arguments, schema, "arguments", problems)
    return problems
