# Provenance: new
"""TOML loading whose errors name the file and the key.

Configuration is validated when it is loaded and never read lazily at first use. `Section` wraps one table with
typed access, so a wrong type, a missing key or a misspelt key fails with a message that says where to look. A name
that is not one of a closed set of choices is refused with the choices, and `close_match` adds the one it was likely
meant to be.
"""

import difflib
import os
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """A configuration file or value is unusable; the message names the file and, where known, the key."""


def load_toml(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and parse a TOML file; raise ConfigError naming the file when it is missing, unreadable or malformed."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{file}: file not found") from None
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"{file}: cannot be read ({type(error).__name__})") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{file}: not valid TOML: {error}") from None


def load_toml_bytes(path: str | os.PathLike[str]) -> tuple[bytes, dict[str, Any]]:
    """Read a TOML file's bytes once and parse them as they are, UTF-8 with no newline translation; return both.

    For a file whose bytes are also rewritten (models.toml, `key_locations`): what is parsed is exactly what is
    rewritten and hashed, so a bare carriage return, which reading as text would turn into a newline, is refused here.
    """
    file = Path(path)
    try:
        data = file.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"{file}: file not found") from None
    except OSError as error:
        raise ConfigError(f"{file}: cannot be read ({type(error).__name__})") from None
    try:
        return data, tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ConfigError(f"{file}: cannot be read ({type(error).__name__})") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{file}: not valid TOML: {error}") from None


def close_match(name: object, choices: Iterable[str]) -> str:
    """A hint to put right after a name that is none of `choices`: ` (did you mean 'x'?)` for the closest one,
    ignoring case, or "" when none is close."""
    if not isinstance(name, str):
        return ""
    by_lower = {choice.lower(): choice for choice in choices}
    found = difflib.get_close_matches(name.lower(), list(by_lower), n=1)
    return f" (did you mean {by_lower[found[0]]!r}?)" if found else ""


class Section:
    """One TOML table with typed access; every failure names the file, the table and the key."""

    def __init__(self, data: Mapping[str, Any], file: str | os.PathLike[str], where: str) -> None:
        """Wrap a table read from `file`; `where` is how messages name it, for example `[models.alpha]`."""
        self.data = data
        self.file = str(file)
        self.where = where

    def fail(self, key: str | None, message: str) -> ConfigError:
        """Build the error for a problem with a key of this table (or the table itself when key is None)."""
        subject = f"key {key!r} " if key is not None else ""
        return ConfigError(f"{self.file}: {self.where}: {subject}{message}")

    def reject_unknown(self, known: Iterable[str]) -> None:
        """Refuse any key not in `known`, so a misspelt key cannot silently do nothing."""
        allowed = set(known)
        for key in self.data:
            if key not in allowed:
                raise self.fail(key, f"is not a known key; known keys: {', '.join(sorted(allowed))}")

    def string(self, key: str) -> str:
        """Return a required, non-empty string."""
        value = self.optional_string(key)
        if value is None:
            raise self.fail(key, "is required")
        return value

    def optional_string(self, key: str) -> str | None:
        """Return a non-empty string, or None when the key is absent."""
        if key not in self.data:
            return None
        value = self.data[key]
        if not isinstance(value, str) or not value.strip():
            raise self.fail(key, f"must be a non-empty string, got {_describe(value)}")
        return value

    def optional_integer(self, key: str) -> int | None:
        """Return an integer (not a boolean), or None when the key is absent."""
        if key not in self.data:
            return None
        value = self.data[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.fail(key, f"must be an integer, got {_describe(value)}")
        return value

    def strings(self, key: str) -> tuple[str, ...]:
        """Return an array of non-empty strings; an absent key is the empty tuple."""
        if key not in self.data:
            return ()
        value = self.data[key]
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise self.fail(key, f"must be an array of non-empty strings, got {_describe(value)}")
        return tuple(value)

    def table(self, key: str) -> dict[str, Any]:
        """Return a sub-table as a copy; an absent key is the empty table."""
        if key not in self.data:
            return {}
        value = self.data[key]
        if not isinstance(value, Mapping):
            raise self.fail(key, f"must be a table, got {_describe(value)}")
        return dict(value)

    def section(self, key: str) -> "Section":
        """Return a required sub-table wrapped as a Section named `where.key`."""
        if key not in self.data:
            raise self.fail(key, "is required")
        value = self.data[key]
        if not isinstance(value, Mapping):
            raise self.fail(key, f"must be a table, got {_describe(value)}")
        where = f"{self.where[:-1]}.{key}]" if self.where.endswith("]") else f"{self.where}.{key}"
        return Section(value, self.file, where)


def _describe(value: object) -> str:
    """Name a value's type for a message without echoing the value, which may be a secret."""
    return type(value).__name__
