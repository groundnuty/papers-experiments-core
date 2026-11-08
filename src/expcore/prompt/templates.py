# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/prompt.py, blob 99abd8fb6e233cd5096f217be3b05c283e50938c
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Prompt templates with any number of named slots, rendered with the template and the result both hashed.

A template is text with slots written `{NAME}` (capital letters, digits and underscores). The study plugin supplies a
value for every slot. Rendering is strict, because a prompt that silently differs between conditions is a confound:

- a slot without a value, or with a blank one, raises;
- a value for a name the template does not have raises, so a dropped slot or a typo is not ignored;
- a value containing `PLACEHOLDER` is refused for a study run, and so is a template that contains it;
- a value is inserted as it is and never searched for slots again.

The record keeps the SHA-256 of the template and of the rendered text, so two runs that claim the same prompt can be
compared, and a template file can be pinned to the hash its protocol froze.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLACEHOLDER = "PLACEHOLDER"
_SLOT = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_SLOT_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


class PromptError(ValueError):
    """A template could not be loaded, or its slots could not be filled."""


def sha256_of(text: str) -> str:
    """Return the SHA-256 of a text, encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RenderedPrompt:
    """A rendered prompt with what the record keeps: both hashes and the values that were inserted."""

    text: str
    template_sha256: str
    rendered_sha256: str
    slots: dict[str, str]


def _fill(text: str, values: dict[str, str]) -> str:
    """Replace each slot with its value, in one pass."""

    def sub(m: re.Match) -> str:
        return values[m.group(1)]

    return _SLOT.sub(sub, text)


@dataclass(frozen=True)
class PromptTemplate:
    """Template text with named slots."""

    text: str

    @property
    def slot_names(self) -> tuple[str, ...]:
        """The slots the template has, sorted, each once."""
        return tuple(sorted(set(_SLOT.findall(self.text))))

    @property
    def sha256(self) -> str:
        """The SHA-256 of the template text."""
        return sha256_of(self.text)

    @classmethod
    def from_file(cls, path: Path, *, expected_sha256: str | None = None) -> "PromptTemplate":
        """Read a template from a file. With `expected_sha256` the file is refused when its content differs."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise PromptError(f"{path}: cannot read the prompt template: {exc}") from exc
        template = cls(text)
        if expected_sha256 is not None and template.sha256 != expected_sha256:
            raise PromptError(f"{path}: the template hashes to {template.sha256}, not the pinned {expected_sha256}")
        return template

    def render(self, values: Mapping[str, Any], *, study_run: bool = False) -> RenderedPrompt:
        """Fill every slot and return the prompt with its hashes. `study_run` refuses any PLACEHOLDER."""
        slots = self._text_values(values)
        names = set(self.slot_names)
        if missing := sorted(names - slots.keys()):
            raise PromptError(f"no value for prompt slot {', '.join('{' + n + '}' for n in missing)}")
        if extra := sorted(slots.keys() - names):
            raise PromptError(f"values for slots the template does not have: {', '.join(extra)}")
        if study_run:
            self._refuse_placeholders(slots)
        text = _fill(self.text, slots)
        return RenderedPrompt(text, self.sha256, sha256_of(text), slots)

    def _text_values(self, values: Mapping[str, Any]) -> dict[str, str]:
        """Turn the values into text; refuse a name that is not a slot name and a value that is blank."""
        slots: dict[str, str] = {}
        for name, value in values.items():
            if not _SLOT_NAME.fullmatch(name):
                raise PromptError(f"{name!r} is not a slot name: capital letters, digits and underscores")
            text = "" if value is None else str(value)
            if not text.strip():
                raise PromptError(f"prompt slot {{{name}}} is unfilled: its value is empty")
            slots[name] = text
        return slots

    def _refuse_placeholders(self, slots: dict[str, str]) -> None:
        """A study run never shows the model a draft marker."""
        bad = sorted(name for name, value in slots.items() if PLACEHOLDER in value.upper())
        if bad:
            raise PromptError(f"a study run refuses {PLACEHOLDER} in the values of: {', '.join(bad)}")
        if PLACEHOLDER in self.text.upper():
            raise PromptError(f"a study run refuses a template that contains {PLACEHOLDER}")
