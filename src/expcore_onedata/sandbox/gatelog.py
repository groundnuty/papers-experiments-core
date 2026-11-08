# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:104-170 (the gate log's summary and error counts)
#   changes: the whole-file and spool counters belonged to modes the gate no longer has; cut transfers are counted
"""What the data gate's log says happened, read from outside the agent (O16).

One JSON line per request or event. The summary separates three things that must never be added together: a policy
refusal (4xx, the enforcement working), an error the gate made itself (origin `gate`) and an error the platform made
(origin `upstream`). A transfer the gate cut, because the client went away or an upstream failed mid-body, carries
`cut` and `unread_bytes`. A log that cannot be read is unknown, never zero errors.
"""

import json
from typing import Any


def unknown(why: str) -> dict[str, Any]:
    """No gate log to read: every count is None, so the analysis reads "not recorded" and never "no errors"."""
    return {
        "unavailable": why,
        "content_requests": None,
        "upstream_bytes": None,
        "bytes_to_sandbox": None,
        "refused_requests": None,
        "cut_transfers": None,
        "unread_bytes": None,
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
        "unread_bytes": sum(r.get("unread_bytes", 0) for r in cut),
        "errors": counts,
        # an error the gate made itself is one only a gate option can see; an upstream one is the platform's, which
        # every data path meets in some form
        "agent_saw_gate_error": counts["gate_5xx"] + counts["gate_resets"] > 0,
        "agent_saw_upstream_error": counts["upstream_5xx"] + counts["upstream_resets"] > 0,
    }
