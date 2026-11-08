# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/config.py, blob 4fc48e52df947cbc31c674d79a74bece2ca2c9d7
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Loads the one pre-registered config file (preregistered.toml)."""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import tomllib
from typing import Any

DEFAULT_PATH = pathlib.Path(__file__).with_name("preregistered.toml")


@dataclasses.dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    sha256: str

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def alpha(self) -> float:
        return float(self.raw["inference"]["alpha"])

    @property
    def options(self) -> list[str]:
        return list(self.raw["inference"]["main_options"])

    @property
    def layouts(self) -> list[str]:
        return list(self.raw["inference"]["layouts"])

    @property
    def contrasts(self) -> list[tuple[str, str]]:
        return [(a, b) for a, b in self.raw["inference"]["contrasts"]]

    def replace_section(self, section: str, **kw: Any) -> Config:
        """A copy with some keys overridden (tests, never production)."""
        raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.raw.items()}
        raw[section].update(kw)
        return Config(raw, self.sha256 + "+override")


def _validate(raw: dict[str, Any]) -> None:
    """Block lists that contradict each other would let a row through one list and out the other."""
    b = raw["blocks"]
    clash = (set(b["primary"]) & set(b["never"])) | ({b["pilot"]} & set(b["primary"]))
    if clash:
        raise ValueError(f"[blocks]: {sorted(clash)} are both analysed (primary) and left out (never/pilot)")


def load(path: str | pathlib.Path | None = None) -> Config:
    p = pathlib.Path(path) if path else DEFAULT_PATH
    data = p.read_bytes()
    raw = tomllib.loads(data.decode())
    _validate(raw)
    return Config(raw, hashlib.sha256(data).hexdigest())
