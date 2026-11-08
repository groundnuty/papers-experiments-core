# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/servedsettings.py, blob 2667d58552f5b08383df9f5d080bcc861ed1299f
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""What the server says it ran (science, #2, 2 Nov 05:03Z): the same silent-default bug as Codex's, for the subject models. The frozen
settings (#8) are what the harness SENDS; this records what the server REPORTS, per run, and compares the two.

A server reports little: every OpenAI-compatible reply carries the served model id (and often a system fingerprint), almost none echo the
temperature or the reasoning switch. A field the reply does not carry is recorded as "not reported", never as the requested value. A field
it does carry is compared with what was requested; a value the harness never requested is a mismatch too (that is the silent default).

    match       the served model id is the expected one, and every reported setting equals the requested one
    mismatch    another model id, or a reported setting that differs from (or was never in) the request: dispatch of that model STOPS
    unreported  no model id came back at all: the run proves nothing; alarmed, nothing stops
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

NOT_REPORTED = "not reported"
SETTINGS_NOTE = (
    "requested; the servers don't report them. reasoning_content_seen=true shows a reasoning/thinking mode took effect; "
    "false does not show it did not"
)
SETTINGS = ("temperature", "top_p", "reasoning_effort", "enable_thinking", "thinking")


class Served:
    """Collects what the replies of one run reported."""

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.fields: dict[
            str, list[Any]
        ] = {}  # every distinct value reported, in order: a later reply must not be forgotten
        self.reasoning_calls = 0  # replies that carried reasoning/thinking content (or blocks)
        self.reasoning_tokens: int | None = (
            None  # summed where the API reports them (usage.completion_tokens_details.reasoning_tokens); else None
        )
        self.calls = 0
        self.reasoning_seen = False  # a reply carried reasoning/thinking content: proof that a mode took effect (its absence proves nothing)

    def note_thinking(self, present: bool, reasoning_tokens: int | None = None) -> None:
        """Once per reply, after its content was read."""
        self.calls += 1
        if present:
            self.reasoning_calls += 1
            self.reasoning_seen = True
        if reasoning_tokens is not None:
            self.reasoning_tokens = (self.reasoning_tokens or 0) + int(reasoning_tokens)

    def note(self, resp: Any) -> None:
        mid = getattr(resp, "model", None)
        if mid:
            self.ids.add(str(mid))
        extra = getattr(resp, "model_extra", None) or {}
        for k in SETTINGS:
            v = extra.get(k, getattr(resp, k, None) if k in ("temperature", "top_p") else None)
            if v is not None and v not in self.fields.setdefault(k, []):
                self.fields[k].append(v)

    def info(self) -> dict[str, Any]:
        return {
            "served_model_ids": sorted(self.ids),
            "served_settings": {
                k: (
                    NOT_REPORTED
                    if not self.fields.get(k)
                    else self.fields[k][0]
                    if len(self.fields[k]) == 1
                    else {"varied": list(self.fields[k])}
                )
                for k in SETTINGS
            },
            "reasoning_content_seen": self.reasoning_seen,
            "settings_note": SETTINGS_NOTE,
            "thinking": {
                "calls": self.calls,
                "calls_with_thinking": self.reasoning_calls,
                "thinking_tokens": self.reasoning_tokens,
                "thinking_tokens_note": "None = the API does not report them separately (they are inside output tokens)",
            },
        }


def requested_settings(sampling: dict | None, request_extra: dict | None) -> dict[str, Any]:
    """The reportable settings the harness REQUESTED: top-level sampling and request_extra keys, and those inside `chat_template_kwargs`
    (model_61's `enable_thinking`), where a server that echoes them would report them from."""
    out: dict[str, Any] = {}
    for src in (sampling or {}, request_extra or {}, (request_extra or {}).get("chat_template_kwargs") or {}):
        out.update({k: v for k, v in src.items() if k in SETTINGS})
    return out


def _same(a: Any, b: Any) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def judge(
    served_ids: Iterable[str],
    served_settings: dict[str, Any] | None,
    expected_id: str,
    requested: dict[str, Any],
    accepted_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """{"state": match|mismatch|unreported, "reasons": [...], "not_reported": [settings the server did not report]}."""
    ids = sorted({str(x) for x in served_ids if x})
    ok_ids = {expected_id, *accepted_ids}
    reasons: list[str] = []
    bad = [i for i in ids if i not in ok_ids]
    if bad:
        reasons.append(f"served model id {bad} is not the expected {expected_id!r}")
    not_rep = []
    for k, vv in (served_settings or {}).items():
        if vv == NOT_REPORTED or vv is None:
            not_rep.append(k)
            continue
        vals = (
            vv["varied"] if isinstance(vv, dict) and "varied" in vv else [vv]
        )  # replies that disagreed with each other
        if len(vals) > 1:
            reasons.append(f"the server reported different {k} in different replies: {vals!r}")
        for v in vals:
            if k not in requested:
                reasons.append(f"the server reports {k}={v!r} but none was requested (a server default)")
            elif not _same(v, requested[k]):
                reasons.append(f"the server reports {k}={v!r}, requested {requested[k]!r}")
    if reasons:  # a wrong setting stops the model even when the reply named no model id
        return {"state": "mismatch", "reasons": sorted(set(reasons)), "not_reported": not_rep}
    if not ids:
        return {"state": "unreported", "reasons": ["the server reported no model id"], "not_reported": list(SETTINGS)}
    return {"state": "match", "reasons": [], "not_reported": not_rep}
