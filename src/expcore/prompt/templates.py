# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/prompt.py, blob 99abd8fb6e233cd5096f217be3b05c283e50938c
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Prompt rendering from the science agent's protocol texts.

`protocol/task-prompt.md` (the text between the two `====` lines) is the prompt; `{ACCESS}` is the
only option-dependent slot, filled from `protocol/access/{T,D,M}.md` (condition_a uses T.md,
M+ uses M.md). The protocol is read at run time from `protocol/` (the registered texts) and from nowhere else: a
missing `protocol/` is an error, never a silent fallback to another copy.

The test in tests/harness/test_prompt.py renders every option and fails if any difference falls
outside the access block.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from dataset_76.records import Instance, Option

from .config import Limits

REPO = Path(__file__).resolve().parents[2]
ACCESS_FILE = {Option.T: "T", Option.condition_c: "T", Option.D: "D", Option.M: "M", Option.condition_d: "M"}
_SLOT = re.compile(r"\{([A-Z][A-Z_]*)\}")


@dataclasses.dataclass(frozen=True)
class PromptTexts:
    template: str  # the prompt between the two ==== lines
    access: dict[str, str]  # "T" | "D" | "M" -> access text
    source: str  # directory the texts were read from
    draft_fallback: bool  # True while protocol/ is absent and the snapshot is used


def load_prompt_texts(protocol_dir: Path | None = None) -> PromptTexts:
    d = protocol_dir or REPO / "protocol"
    fallback = False  # kept in the record: there is no fallback copy any more
    if not (d / "task-prompt.md").exists():
        raise FileNotFoundError(f"{d}/task-prompt.md: the registered protocol texts are required")
    parts = (d / "task-prompt.md").read_text().split("\n====\n")
    if len(parts) != 3:
        raise ValueError(f"{d}/task-prompt.md: expected exactly two ==== lines")
    template = parts[1].strip("\n")
    if template.count("{ACCESS}") != 1:
        raise ValueError("the template must contain exactly one {ACCESS} slot")
    access = {k: (d / "access" / f"{k}.md").read_text().strip("\n") for k in sorted(set(ACCESS_FILE.values()))}
    return PromptTexts(template, access, str(d), fallback)


def slot_values(instance: Instance, limits: Limits, config_slots: dict[str, str]) -> dict[str, str]:
    """Every slot except {ACCESS}: identical across options by construction."""
    v = dict(config_slots)
    v.update(
        LAT_MIN=instance.lat_min,
        LAT_MAX=instance.lat_max,
        LON_MIN=instance.lon_min,
        LON_MAX=instance.lon_max,
        START=instance.target.start.isoformat(),
        END=instance.target.end.isoformat(),
        OUTPUT_CAP=limits.output_cap_chars,
        TURN_CAP=limits.max_tool_rounds,
        TOKEN_BUDGET=limits.token_budget,
        TIME_CAP_MIN=int(limits.time_cap_s // 60),
    )
    return {k: str(x) for k, x in v.items()}


def _fill(text: str, values: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in values:
            raise KeyError(f"no value for prompt slot {{{m.group(1)}}}")
        return values[m.group(1)]

    return _SLOT.sub(sub, text)


def render_access(texts: PromptTexts, option: Option, values: dict[str, str]) -> str:
    return _fill(texts.access[ACCESS_FILE[option]], values)


def render_prompt(
    texts: PromptTexts, instance: Instance, option: Option, limits: Limits, config_slots: dict[str, str]
) -> tuple[str, str]:
    """(system, user). The protocol prompt is a single user message, so the system part is empty.
    `config_slots` is HarnessConfig.slots_for(layout): the location phrases depend on the layout only."""
    values = slot_values(instance, limits, config_slots)
    access = render_access(texts, option, values)
    user = _fill(texts.template.replace("{ACCESS}", "\x00ACCESS\x00"), values).replace("\x00ACCESS\x00", access)
    return "", user
