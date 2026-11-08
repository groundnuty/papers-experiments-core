# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/capping.py, blob 72916ddb1c5aa5b3c1c7ea9be1f1b66a90f615e2
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: www-2027-experiments @ 46a5828d, harness/agent/tools.py:121-141
#   changes: see git history of this file from its import commit
"""The output cap, applied identically to every tool output in every condition (L06).

The cap counts characters, and the truncation notice counts inside it. An error is capped like any other output,
with its class first. The cap is stated in every tool's description, so the hash of the tool manifest changes with
it, and the cap value is part of the run's configuration hash: two conditions cannot differ in it unnoticed.
The full raw result or error stays in the record; the cap only decides what the model is shown.
"""

from __future__ import annotations

TRUNCATION_NOTICE = "\n[output truncated: {dropped} of {total} characters dropped by the harness cap]"


def cap_text(text: str, cap_chars: int) -> tuple[str, bool]:
    """Cap `text` to at most `cap_chars` characters, notice included.

    Returns (delivered_text, was_truncated).
    """
    total = len(text)
    if total <= cap_chars:
        return text, False
    keep = cap_chars
    for _ in range(8):  # the notice's length depends on the digits of `dropped`; settle on a fixed point
        notice = TRUNCATION_NOTICE.format(dropped=total - keep, total=total)
        new_keep = cap_chars - len(notice)
        if new_keep < 0:
            return text[:cap_chars], True  # cap smaller than the notice: plain cut
        if new_keep == keep:
            break
        keep = new_keep
    notice = TRUNCATION_NOTICE.format(dropped=total - keep, total=total)
    out = text[:keep] + notice
    while len(out) > cap_chars and keep > 0:
        keep -= 1
        out = text[:keep] + TRUNCATION_NOTICE.format(dropped=total - keep, total=total)
    if len(out) > cap_chars:  # never deliver more than the cap, whatever the digits of the counts did
        return text[:cap_chars], True
    return out, True


def render_error(error_class: str, message: str) -> str:
    """Return the text the model sees for an error: the error class first, then the message."""
    return f"{error_class}: {message}" if message else error_class


def cap_error(error_class: str, message: str, cap_chars: int) -> tuple[str, bool]:
    """Cap an error like any output. The class leads, so it survives the cut. Returns (delivered_text, was_truncated)."""
    return cap_text(render_error(error_class, message), cap_chars)


def describe_cap(cap_chars: int) -> str:
    """Return the sentence appended to every tool description, so the manifest hash pins the cap and the notice."""
    notice = TRUNCATION_NOTICE.format(dropped="D", total="T").strip()
    return (
        f"Output limit: a result or an error longer than {cap_chars} characters is cut to {cap_chars} characters, "
        f"and a line {notice} ends it; the line counts inside the {cap_chars}."
    )
