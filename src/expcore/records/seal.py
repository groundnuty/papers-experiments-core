# Provenance: new
"""Seal a record: take every digest it pins over its data as it now stands, after redaction (R06, R12).

A record pins its prompt texts, the conversation each request carried, the settings each request sent and its
configuration by SHA-256, and replay checks the data against those digests. Secrets are replaced before the record is
stored, and what is stored is what is replayed, so the digests must be those of the redacted data: a digest of the
text as the model saw it would never match the text as stored. `seal` recomputes them all from the data it is given.
"""

from typing import Any

from expcore.prompt.templates import sha256_of
from expcore.records.digest import digest


def seal(data: dict[str, Any]) -> dict[str, Any]:
    """Recompute, in place, every digest of a record's data (as a dict of the record's fields), and return it.

    The data is changed in place because it is the freshly built, already redacted copy that the caller owns.
    The settings sets are stored under their digests, so they are keyed again, and the turns that name them with them.
    """
    for part in ("system", "user"):
        entry = data["prompt"][part]
        entry["sha256"] = sha256_of(entry["text"])
    data["agent_config_sha256"] = digest(data["agent_config"])
    route = data["route"]
    renamed = {old: digest(settings) for old, settings in route["settings"].items()}
    route["settings"] = {renamed[old]: settings for old, settings in route["settings"].items()}
    route["settings_sent_sha256"] = renamed.get(route["settings_sent_sha256"], route["settings_sent_sha256"])
    system = data["prompt"]["system"]["text"]
    conversation: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    conversation.append({"role": "user", "content": data["prompt"]["user"]["text"]})
    for turn in data["transcript"]:
        turn["request_sha256"] = digest(conversation)
        turn["settings_sha256"] = renamed.get(turn["settings_sha256"], turn["settings_sha256"])
        conversation = [*conversation, turn["native"], *turn["tool_messages"]]
    failed = data["failed_request"]
    if failed is not None:
        failed["request_sha256"] = digest(conversation)
        failed["settings_sha256"] = renamed.get(failed["settings_sha256"], failed["settings_sha256"])
    return data
