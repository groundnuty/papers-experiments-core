# Provenance: new
"""Seal a record: take every digest it pins over its data as it now stands, after redaction (R06, R12).

A record pins its prompt texts, the conversation each request carried, the settings each request sent, its
configuration and its tools by SHA-256, and replay checks the data against those digests. Secrets are replaced before
the record is stored, and what is stored is what is replayed, so the digests must be those of the redacted data: a
digest of the text as the model saw it would never match the text as stored. `seal` recomputes them all from the data
it is given. The builder seals what it redacted, and the writer seals again whatever it redacts, so a secret that
is found only when the record is written cannot leave a stale digest behind.

A call whose shown text or raw result was redacted can no longer be re-rendered by the loop's cap, which counts
characters of text that has changed; `mark_unverified` records that on the call (`rendering_verified: false`).
"""

import re
from typing import Any

from expcore.loop.toolbox import ToolBox
from expcore.plugin import ToolSpec
from expcore.prompt.templates import sha256_of
from expcore.records.digest import digest

# The fields of a tool-call entry that the cap and the error rendering are computed from, as redaction paths.
_RENDERED = re.compile(r"tool_calls\[(\d+)\]\.(?:raw_text|text|error_class)(?:[.\[].*)?")


class _Specs:
    """A provider that offers fixed specs and runs nothing, to compute the manifest hash of a tool box."""

    def __init__(self, specs: list[ToolSpec]):
        self._specs = specs

    def tools(self) -> list[ToolSpec]:
        return self._specs


def manifest_sha256_of(tools: list[dict[str, Any]], cap_chars: int) -> str | None:
    """Return the manifest hash a tool box gives these tool entries under this cap, or None if none can be built."""
    try:
        return ToolBox([_Specs([ToolSpec(**t) for t in tools])], cap_chars=cap_chars).manifest_sha256  # type: ignore[list-item]
    except (TypeError, ValueError):
        return None


def mark_unverified(data: dict[str, Any], paths: list[str]) -> None:
    """Mark, in place, each tool call whose shown text, raw result or error class was redacted as not verifiable."""
    for path in paths:
        found = _RENDERED.fullmatch(path)
        if found:
            data["tool_calls"][int(found[1])]["rendering_verified"] = False


def seal(data: dict[str, Any]) -> dict[str, Any]:
    """Recompute, in place, every digest of a record's data (as a dict of the record's fields), and return it.

    The data is changed in place because it is the freshly built, already redacted copy that the caller owns. The
    settings sets are stored under their digests, so they are keyed again, and the turns that name them with them. A
    digest the record's schema version does not have is not added.
    """
    for part in ("system", "user"):
        entry = data["prompt"][part]
        entry["sha256"] = sha256_of(entry["text"])
    data["agent_config_sha256"] = digest(data["agent_config"])
    manifest = manifest_sha256_of(data["tools"], data["agent_config"]["limits"]["output_cap_chars"])
    if manifest is not None:
        data["prompt"]["tools_sha256"] = manifest
    route = data["route"]
    renamed = {old: digest(settings) for old, settings in route["settings"].items()}
    route["settings"] = {renamed[old]: settings for old, settings in route["settings"].items()}
    route["settings_sent_sha256"] = renamed.get(route["settings_sent_sha256"], route["settings_sent_sha256"])
    system = data["prompt"]["system"]["text"]
    conversation: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    conversation.append({"role": "user", "content": data["prompt"]["user"]["text"]})
    for turn in data["transcript"]:
        if "request_sha256" in turn:
            turn["request_sha256"] = digest(conversation)
        if "settings_sha256" in turn:
            turn["settings_sha256"] = renamed.get(turn["settings_sha256"], turn["settings_sha256"])
        conversation = [*conversation, turn["native"], *turn["tool_messages"]]
    failed = data["failed_request"]
    if failed is not None:
        if "request_sha256" in failed:
            failed["request_sha256"] = digest(conversation)
        if "settings_sha256" in failed:
            failed["settings_sha256"] = renamed.get(failed["settings_sha256"], failed["settings_sha256"])
    return data
