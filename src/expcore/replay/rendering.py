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
  before comparing, whatever the raw text has there) or fewer than four characters of it where the raw text has its
  marker. Replay never sees a secret, so none of this depends on one.
"""

import re

from expcore.loop.capping import TRUNCATION_NOTICE
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
    if head.endswith(REDACTED) and raw.startswith(head[: -len(REDACTED)]):
        return True  # redaction marked the fragment the cut left, and the redacted raw text has the whole of it or none
    # a cut that fell inside a secret and left fewer than four characters of it, where the raw text has its marker
    return any(raw.startswith(head[: len(head) - j] + REDACTED) for j in range(1, min(MIN_SECRET_LEN, len(head) + 1)))


def check_rendering(record: RunRecord) -> None:
    """Raise ReplayDivergence for a call marked as not verifiable that redaction did not touch, or whose shown text
    cannot follow from its raw text."""
    for j, call in enumerate(record.tool_calls):
        if call.get("rendering_verified", True):
            continue
        where = f"tool_calls[{j}]"
        if not any(_redacted_under(record.redacted_paths, f"{where}.{f}") for f in ("raw_text", "text")):
            raise ReplayDivergence(
                f"{where}.rendering_verified",
                f"call {call['call_id']!r} is marked as not verifiable, but the record shows no redaction of its text",
            )
        if not shown_follows_raw(call["text"], call["raw_text"], call["truncated"]):
            raise ReplayDivergence(
                f"{where}.text", f"the shown text of call {call['call_id']!r} cannot follow from its raw text"
            )
