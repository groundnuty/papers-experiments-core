# Provenance: new
"""What can be checked about a tool call whose rendering was redacted (R03, R12).

When redaction changed the characters of a call's raw or shown text, the loop's cap cannot be recomputed from them,
and replay serves the recorded rendering (`ReplayToolBox`). That is a hole a fabricated record could use, so the flag
that opens it, `rendering_verified: false`, is checked before the run:

- it is honoured only if `redacted_paths` shows that the call's raw or shown text was redacted; otherwise the flag
  is a divergence that names the call;
- and what can still be checked is: a call that was not truncated shows exactly its raw text; a truncated call shows a
  cut of its raw text followed by the cap's notice, so what precedes the notice must be a prefix of the raw text. A cut
  that fell inside a secret leaves a marker where redaction marked the fragment (one trailing marker is set aside
  before comparing, whatever the raw text has there, and the raw text must then run at least four characters past what
  was compared, since the marker stands for four or more) or fewer than four characters of it where the raw text has
  its marker. Replay never sees a secret, so none of this depends on one.

Each call also records `raw_chars`, the length of its raw result before redaction, which is not a secret. The cut and
the notice follow from that length and the cap alone, so for every call: the notice must be the one the cap writes for
`raw_chars`; a call that was not cut must fit the cap; and a raw text that redaction did not touch must have exactly
`raw_chars` characters. Without it, a record whose raw text was shortened would still show a cut that cannot have
happened.
"""

import re
from typing import Any

from expcore.loop.capping import TRUNCATION_NOTICE, cap_plan
from expcore.records.record import RunRecord
from expcore.records.redact import MIN_SECRET_LEN, REDACTED
from expcore.replay.divergence import ReplayDivergence

_NOTICE = re.compile(re.escape(TRUNCATION_NOTICE).replace(r"\{dropped\}", r"\d+").replace(r"\{total\}", r"\d+") + r"\Z")


def _redacted_under(paths: list[str], field: str) -> bool:
    """Tell whether some redacted path is the field or lies inside it (a key of the JSON text it holds)."""
    return any(p == field or p.startswith((field + ".", field + "[")) for p in paths)


def shown_follows_raw(text: str, raw: str, truncated: bool) -> bool:
    """Tell whether a call's shown text can be what the loop made of its raw text, both redacted the same way."""
    if not truncated:
        return text == raw
    notice = _NOTICE.search(text)
    head = text[: notice.start()] if notice else text  # a cap smaller than its notice is a plain cut
    if raw.startswith(head):
        return True
    if head.endswith(REDACTED):
        # redaction marked the fragment the cut left, and the redacted raw text has the whole of it or none; the marker
        # stands for at least MIN_SECRET_LEN characters, so the raw text runs at least that far past what was compared
        marked = len(head) - len(REDACTED)
        if raw.startswith(head[:marked]) and len(raw) - marked >= MIN_SECRET_LEN:
            return True
    # a cut that fell inside a secret and left fewer than four characters of it, where the raw text has its marker
    return any(raw.startswith(head[: len(head) - j] + REDACTED) for j in range(1, min(MIN_SECRET_LEN, len(head) + 1)))


def _divergence(j: int, field: str, call: dict[str, Any], what: str) -> ReplayDivergence:
    """The divergence of a call at one of its fields, naming the call."""
    return ReplayDivergence(f"tool_calls[{j}].{field}", f"call {call['call_id']!r} {what}")


def check_counts(j: int, call: dict[str, Any], cap_chars: int, raw_redacted: bool) -> None:
    """Raise ReplayDivergence when what the call says of the length of its raw result disagrees with what it shows.

    The cut and the notice follow from `raw_chars` and the cap alone; the length of the raw text is checked where
    redaction did not change it.
    """
    raw_chars = call["raw_chars"]
    if not raw_redacted and len(call["raw_text"]) != raw_chars:
        raise _divergence(
            j,
            "raw_chars",
            call,
            f"records {raw_chars} characters of raw result, but its raw text, which redaction did not touch, "
            f"has {len(call['raw_text'])}",
        )
    if not call["truncated"]:
        if raw_chars > cap_chars:
            raise _divergence(
                j, "raw_chars", call, f"was not cut, but its raw result of {raw_chars} characters exceeds the cap"
            )
        return
    if raw_chars <= cap_chars:
        raise _divergence(j, "raw_chars", call, f"was cut, but its raw result of {raw_chars} characters fits the cap")
    if not call["text"].endswith(cap_plan(raw_chars, cap_chars)[1]):
        raise _divergence(j, "text", call, f"ends with a notice other than the cap writes for {raw_chars} characters")


def check_rendering(record: RunRecord, cap_chars: int) -> None:
    """Raise ReplayDivergence for a call whose lengths disagree with its text, for a call marked as not verifiable that
    redaction did not touch, and for one whose shown text cannot follow from its raw text."""
    for j, call in enumerate(record.tool_calls):
        where = f"tool_calls[{j}]"
        flagged = not call.get("rendering_verified", True)
        raw_redacted = _redacted_under(record.redacted_paths, f"{where}.raw_text")
        if flagged and not (raw_redacted or _redacted_under(record.redacted_paths, f"{where}.text")):
            raise ReplayDivergence(
                f"{where}.rendering_verified",
                f"call {call['call_id']!r} is marked as not verifiable, but the record shows no redaction of its text",
            )
        check_counts(j, call, cap_chars, raw_redacted)
        if flagged and not shown_follows_raw(call["text"], call["raw_text"], call["truncated"]):
            raise _divergence(j, "text", call, "has a shown text that cannot follow from its raw text")
