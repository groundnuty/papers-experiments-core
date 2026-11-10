# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:104-170 (the gate log's summary and error counts)
#   changes: the whole-file and spool counters belonged to modes the gate no longer has; cut transfers are counted
"""What the data gate's log says happened, read from outside the agent (O16).

One JSON line per request or event. The summary separates three things that must never be added together: a policy
refusal (4xx, the enforcement working), an error the gate made itself (origin `gate`) and an error the platform made
(origin `upstream`). A transfer the gate cut, because the client went away or an upstream failed mid-body, carries
`cut` and `unread_bytes`; one it cut because the body held its token carries `cut_reason` too. An answer the gate
refused for its own integrity (a redirect, an encoded body, its token in a header or a body, an answer or a complete
listing too large to check or serve whole) carries `gate_refusal`, its reason (`refusals`): the gate cut the
condition's read, the agent did not read less. A log that cannot be read is unknown, never zero errors, and never no
refusal (`UNREADABLE`).

**The bytes served in a run (O20, `served`).** The log of a gate that was stopped (the collection's own SIGTERM, after
the agent's processes were stopped) holds one line per answer, each with the bytes of its body that the gate's socket
accepted (`body_bytes`), never fewer than the agent received, and what that body was (`body`). `served` adds them up:

  * `content_bytes`: the bodies of the platform's 200 and 206 answers to reads of a file's content, which is the file
    data the agent read; a transfer the gate cut counts the part it sent;
  * `listing_bytes`: the bodies of the platform's 200 answers to listings, each page in the paged mode and the merged
    answer in the complete one, so the count is of what the agent was served, never of the pages the gate read;
  * `other_bytes`: every other answer of the platform's the gate passed on (the token's scope, a file id looked up, a
    file's attributes, the platform's own errors);
  * `served_bytes`: the three together, the bytes of the platform's answers the agent read through the gate;
  * `gate_answer_bytes`: the gate's own answers (a refusal, an error), kept out of `served_bytes`.

It counts body bytes, never headers or TLS framing, never what the gate read upstream, and never the plain TCP
forwards of the knowledge plane. It is a count of what was served, so it can exceed what the agent's code consumed by
what sat unread in its socket when the agent stopped reading (a cut transfer's line and its connection's close line
show that). Every count is None, with its reasons in `unknown`, when any line may be missing or cannot be read: the
gate was not shown stopped (`gate_not_stopped`), its log could not be read or written whole (`gate_log_unreadable`,
`gate_log_malformed`, `gate_log_write_failed`), it could not write the close line of every connection
(`gate_teardown_incomplete`), or a request was still being answered when it stopped (`request_in_flight_at_stop`).
Unknown is never zero.
"""

import json
from collections.abc import Mapping
from typing import Any


def unknown(why: str) -> dict[str, Any]:
    """No gate log to read: every count is None, so the analysis reads "not recorded" and never "no errors"."""
    return {
        "unavailable": why,
        "content_requests": None,
        "bytes_to_sandbox": None,
        "refused_requests": None,
        "cut_transfers": None,
        "cut_reasons": None,
        "unread_bytes": None,
        "gate_refusals": None,
        "errors": None,
        "agent_saw_gate_error": None,
        "agent_saw_upstream_error": None,
    }


def errors(records: list[dict]) -> dict[str, int]:
    """Every 5xx and every reset the client got, by origin (the gate's own, or the platform's), and the gate's retries.

    From the `error_origin` and `client_saw` fields: a 5xx is an HTTP status the client received, a reset is a
    connection the gate ended mid-response (a body already begun cannot carry an error status). Policy refusals are 4xx
    and counted apart; they are the enforcement, not errors."""
    out = {
        "gate_5xx": 0,
        "gate_resets": 0,
        "upstream_5xx": 0,
        "upstream_resets": 0,
        "connection_errors_gate": 0,
        "connection_errors_client": 0,
        "client_request_errors": 0,
        "upstream_retries": 0,
        "upstream_stale": 0,
        "policy_refusals": 0,
    }
    for r in records:
        if r.get("event") in ("connection_error", "tcp_forward_error"):
            origin, seen = r.get("error_origin"), r.get("client_saw") in ("5xx", "reset")
            if origin == "upstream":
                out["upstream_resets"] += seen  # the platform side failed: the client lost its connection
            elif origin == "gate":
                out["connection_errors_gate"] += 1  # diagnostic, whether or not a client noticed
                out["gate_resets"] += seen  # the flag counts only what a client saw
            else:
                out["connection_errors_client"] += 1
            continue
        if r.get("event") == "request_error":  # a request the parser refused: the client's own
            out["client_request_errors"] += 1
            continue
        origin, saw = r.get("error_origin"), r.get("client_saw")
        if origin in ("gate", "upstream") and saw in ("5xx", "reset"):
            out[f"{origin}_{'5xx' if saw == '5xx' else 'resets'}"] += 1
        out["upstream_retries"] += int(r.get("upstream_retries") or 0)
        out["upstream_stale"] += int(r.get("upstream_stale") or 0)
        if r.get("decision") == "deny":
            out["policy_refusals"] += 1
    return out


UNREADABLE = "gate_log_unreadable"  # the refusal an unreadable log stands for: its refusals are unknown, never none


def refusals(lines: list[str]) -> list[str]:
    """The reason of every answer the gate refused for its own integrity, in the order of the log. A line still being
    written when the log is read (the last, cut short) is no refusal yet."""
    out = []
    for line in lines:
        try:
            record = json.loads(line) if line.strip() else {}
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("gate_refusal"):
            out.append(str(record["gate_refusal"]))
    return out


def _counted(values: list[str]) -> dict[str, int]:
    return {v: values.count(v) for v in sorted(set(values))}


def summarize(lines: list[str]) -> dict[str, Any]:
    """Content transfers and errors seen by the gate so far."""
    records = [json.loads(x) for x in lines if x.strip()]
    content = [r for r in records if r.get("tag") == "content" and r.get("decision") == "allow"]
    ok = [r for r in content if r.get("status") in (200, 206)]  # 206: range reads
    counts = errors(records)
    # the bytes whatever the request's final status: 10 bytes of a failed transfer moved
    cut = [r for r in records if r.get("cut") is True]
    return {
        "content_requests": len(ok),
        "bytes_to_sandbox": sum(r.get("bytes", 0) for r in content),
        "refused_requests": sum(1 for r in records if r.get("decision") == "deny"),
        "cut_transfers": len(cut),
        "cut_reasons": _counted([str(r["cut_reason"]) for r in cut if r.get("cut_reason")]),  # the gate's own cuts
        "unread_bytes": sum(r.get("unread_bytes", 0) for r in cut),
        "gate_refusals": _counted([str(r["gate_refusal"]) for r in records if r.get("gate_refusal")]),
        "errors": counts,
        # an error the gate made itself is one only a gate option can see; an upstream one is the platform's, which
        # every data path meets in some form
        "agent_saw_gate_error": counts["gate_5xx"] + counts["gate_resets"] > 0,
        "agent_saw_upstream_error": counts["upstream_5xx"] + counts["upstream_resets"] > 0,
    }


# Why the bytes served are unknown (O20): each makes every count None, never a partial sum taken for the whole
GATE_NOT_STOPPED = "gate_not_stopped"
GATE_LOG_MALFORMED = "gate_log_malformed"
GATE_LOG_WRITE_FAILED = "gate_log_write_failed"
GATE_TEARDOWN_INCOMPLETE = "gate_teardown_incomplete"
REQUEST_IN_FLIGHT = "request_in_flight_at_stop"
SERVED_FIELDS = ("served_bytes", "content_bytes", "listing_bytes", "other_bytes", "gate_answer_bytes")
_BODY_FIELD = {
    "content": "content_bytes",
    "listing": "listing_bytes",
    "other": "other_bytes",
    "gate": "gate_answer_bytes",
}


def served_unknown(*reasons: str) -> dict[str, Any]:
    """The bytes served, unknown for these reasons: every count None."""
    return {**dict.fromkeys(SERVED_FIELDS), "unknown": sorted(set(reasons))}


def served(lines: list[str] | None, stop: Mapping[str, Any] | None) -> dict[str, Any]:
    """The bytes the gate served the agent in a run, from its log once it was stopped (O20; the module docstring says
    what each count holds). `lines` are the log's lines, None when it could not be read; `stop` is what the stop of the
    gate found (`gate_stop` of the sandbox's metadata): it must have been signalled and shown exited, with no failed
    write of its log and every connection's close line written."""
    reasons = set() if lines is not None else {UNREADABLE}
    if not isinstance(stop, Mapping) or stop.get("signalled") is not True or "exit_code" not in stop:
        reasons.add(GATE_NOT_STOPPED)
    else:
        if stop.get("log_write_failed") is not False:
            reasons.add(GATE_LOG_WRITE_FAILED)
        if stop.get("teardown_incomplete") is not False:
            reasons.add(GATE_TEARDOWN_INCOMPLETE)
    counts = dict.fromkeys(_BODY_FIELD.values(), 0)
    for text in lines or ():
        if not text.strip():
            continue
        try:
            record = json.loads(text)
        except ValueError:
            reasons.add(GATE_LOG_MALFORMED)
            continue
        if not isinstance(record, dict):
            reasons.add(GATE_LOG_MALFORMED)
            continue
        if record.get("event") == "conn_close" and record.get("in_request") is True:
            reasons.add(REQUEST_IN_FLIGHT)  # its answer may have begun, and has no line
        if "body_bytes" not in record:
            if "method" in record:  # a request's line always has its answer's body bytes
                reasons.add(GATE_LOG_MALFORMED)
            continue
        size, field = record["body_bytes"], _BODY_FIELD.get(record.get("body"))
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or field is None:
            reasons.add(GATE_LOG_MALFORMED)  # a line that does not say what it served is no count
            continue
        counts[field] += size
    if reasons:
        return served_unknown(*reasons)
    total = counts["content_bytes"] + counts["listing_bytes"] + counts["other_bytes"]
    return {"served_bytes": total, **counts, "unknown": []}
