# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/servedsettings.py, blob 2667d58552f5b08383df9f5d080bcc861ed1299f
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: www-2027-experiments @ 46a5828d, scripts/run_pilot.py:2175-2283
#   merged: www-2027-experiments @ 46a5828d, harness/agent/providers.py:1149-1152, 1292-1293
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:488-500
#   changes: see git history of this file from its import commit
"""What the server says it ran, set against what was requested (B06).

The frozen settings are what a backend SENDS; this records what the server REPORTS, per run, and compares the two. A
server reports little: every OpenAI-compatible reply carries the served model id (and often a system fingerprint),
almost none echo the temperature or the reasoning switch. A field the reply does not carry is recorded as "not
reported", never as the requested value. A field it does carry is compared with what was requested; a value that was
never requested is a mismatch too (that is a silent server default).

    match       the served model id is the expected one, and every reported setting equals the requested one
    mismatch    another model id, a changed or mixed system fingerprint, or a reported setting that differs from
                (or was never in) the request: dispatch of that model STOPS
    unreported  no model id came back at all: the run proves nothing; alarmed, nothing stops

Model ids. A dated snapshot of the expected id (`<model>-YYYYMMDD`) is the same model.

System fingerprints. The first fingerprint a model is served with is its baseline; a later reply with another one is
a change. A reply that carries several fingerprints (joined with `MIXED_FINGERPRINTS`, as a reply spliced from
servers that differ is) is a change by itself and never becomes a baseline. A reply without a fingerprint is no
change and sets nothing.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from typing import Any

NOT_REPORTED = "not reported"
SETTINGS_NOTE = (
    "requested; most servers don't report them. reasoning_content_seen=true shows a reasoning/thinking mode "
    "took effect; false does not show it did not"
)
SETTINGS = ("temperature", "top_p", "reasoning_effort", "enable_thinking", "thinking")
MIXED_FINGERPRINTS = " | "
_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}")


def same_model(expected: str, served: str) -> bool:
    """Tell whether a served id is the expected model: the same id, or its dated snapshot `<expected>-YYYYMMDD`."""
    if served == expected:
        return True
    return served.startswith(expected) and _SNAPSHOT_SUFFIX.fullmatch(served[len(expected) :]) is not None


def is_mixed(fingerprint: str | None) -> bool:
    """Tell whether a fingerprint is really several, joined because one reply carried more than one."""
    return bool(fingerprint) and MIXED_FINGERPRINTS in (fingerprint or "")


def _fingerprint_of(resp: Any) -> str | None:
    """Read a reply's system fingerprint; several (a list) are joined into one mixed value."""
    found = getattr(resp, "system_fingerprint", None)
    if isinstance(found, (list, tuple)):
        distinct = list(dict.fromkeys(str(f) for f in found if f))
        return MIXED_FINGERPRINTS.join(distinct) if len(distinct) > 1 else (distinct[0] if distinct else None)
    return str(found) if found else None


class Served:
    """Collects what the replies of one run reported."""

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.fingerprints: list[str] = []  # distinct, in the order met; a mixed one is kept whole
        # every distinct value reported, in order: a later reply must not be forgotten
        self.fields: dict[str, list[Any]] = {}
        self.reasoning_calls = 0  # replies that carried reasoning/thinking content (or blocks)
        # summed where the API reports them (usage.completion_tokens_details.reasoning_tokens); else None
        self.reasoning_tokens: int | None = None
        self.calls = 0
        # a reply carried reasoning/thinking content: proof that a mode took effect (its absence proves nothing)
        self.reasoning_seen = False

    def note_thinking(self, present: bool, reasoning_tokens: int | None = None) -> None:
        """Once per reply, after its content was read."""
        self.calls += 1
        if present:
            self.reasoning_calls += 1
            self.reasoning_seen = True
        if reasoning_tokens is not None:
            self.reasoning_tokens = (self.reasoning_tokens or 0) + int(reasoning_tokens)

    def note(self, resp: Any) -> None:
        """Take the served model id, the system fingerprint and every echoed setting from one reply."""
        mid = getattr(resp, "model", None)
        if mid:
            self.ids.add(str(mid))
        fingerprint = _fingerprint_of(resp)
        if fingerprint and fingerprint not in self.fingerprints:
            self.fingerprints.append(fingerprint)
        extra = getattr(resp, "model_extra", None) or {}
        for k in SETTINGS:
            v = extra.get(k, getattr(resp, k, None) if k in ("temperature", "top_p") else None)
            if v is not None and v not in self.fields.setdefault(k, []):
                self.fields[k].append(v)

    def info(self) -> dict[str, Any]:
        """Return what was reported, for the record: a setting no reply carried reads "not reported"."""
        return {
            "served_model_ids": sorted(self.ids),
            "system_fingerprints": list(self.fingerprints),
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
                "thinking_tokens_note": "None = the API does not report them separately (inside output tokens)",
            },
        }


class Baseline:
    """A model's first served id and system fingerprint, against which every later reply is compared.

    Not tied to one run: a campaign keeps one per model, so a model that changes between runs is found.
    """

    def __init__(self, served_model: str | None = None, system_fingerprint: str | None = None) -> None:
        self.served_model = served_model
        self.system_fingerprint = system_fingerprint
        self._lock = threading.Lock()

    def check(self, served: str | None, fingerprint: str | None) -> dict[str, str | None] | None:
        """Compare one reply with the baseline and learn from it; None when nothing changed, else the change.

        The first reply that names a model sets the id, and the first with a single fingerprint sets that.
        A mixed fingerprint is a change whatever the baseline and never sets one.
        """
        with self._lock:
            mixed = is_mixed(fingerprint)
            if served and self.served_model is None:
                self.served_model = served
            if fingerprint and not mixed and self.system_fingerprint is None:
                self.system_fingerprint = fingerprint
            change: dict[str, str | None] = {}
            if served and self.served_model and not same_model(self.served_model, served):
                change.update(first_served_model=self.served_model, served_model=served)
            if fingerprint and (mixed or fingerprint != self.system_fingerprint):
                change.update(first_system_fingerprint=self.system_fingerprint, system_fingerprint=fingerprint)
            return change or None

    def to_dict(self) -> dict[str, str | None]:
        """Return the baseline for a campaign to keep."""
        return {"served_model": self.served_model, "system_fingerprint": self.system_fingerprint}

    @classmethod
    def from_dict(cls, data: dict[str, str | None]) -> Baseline:
        """Rebuild a baseline from what `to_dict` returned."""
        return cls(data.get("served_model"), data.get("system_fingerprint"))


def requested_settings(sampling: dict | None, extra_body: dict | None) -> dict[str, Any]:
    """The reportable settings that were REQUESTED, from sampling, extra_body and its `chat_template_kwargs`.

    A server that echoes a setting back reports it from one of these places, so all of them count as requested.
    """
    out: dict[str, Any] = {}
    for src in (sampling or {}, extra_body or {}, (extra_body or {}).get("chat_template_kwargs") or {}):
        out.update({k: v for k, v in src.items() if k in SETTINGS})
    return out


def _same(a: Any, b: Any) -> bool:
    """Compare two setting values as numbers when both are, else as given."""
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
    *,
    served_fingerprints: Iterable[str] = (),
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Return {"state": match|mismatch|unreported, "reasons": [...], "not_reported": [settings never reported]}.

    `accepted_ids` are other ids the server reports for the same model (the model table's aliases).
    """
    ids = sorted({str(x) for x in served_ids if x})
    ok_ids = {expected_id, *accepted_ids}
    reasons: list[str] = []
    bad = [i for i in ids if not any(same_model(ok, i) for ok in ok_ids)]
    if bad:
        reasons.append(f"served model id {bad} is not the expected {expected_id!r}")
    reasons += _fingerprint_reasons(served_fingerprints, expected_fingerprint)
    not_rep = []
    for k, vv in (served_settings or {}).items():
        if vv == NOT_REPORTED or vv is None:
            not_rep.append(k)
            continue
        # a "varied" entry lists replies that disagreed with each other
        vals = vv["varied"] if isinstance(vv, dict) and "varied" in vv else [vv]
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


def _fingerprint_reasons(fingerprints: Iterable[str], expected: str | None) -> list[str]:
    """List why the fingerprints of a run are a change: a mixed reply, different replies, or a moved baseline."""
    seen = list(dict.fromkeys(f for f in fingerprints if f))
    reasons = []
    if any(is_mixed(f) for f in seen):
        reasons.append("a reply carried several system fingerprints")
    single = [f for f in seen if not is_mixed(f)]
    if len(single) > 1:
        reasons.append(f"the server reported different system fingerprints in different replies: {single!r}")
    if expected is not None:
        reasons += [f"system fingerprint {f!r} is not the baseline {expected!r}" for f in single if f != expected]
    return reasons
