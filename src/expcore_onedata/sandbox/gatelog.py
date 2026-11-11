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

**The bytes served in a run (O22, `served`).** The log of a gate that was stopped (the collection's own SIGTERM, after
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
show that). A log is whole only with a positive mark: the `teardown_complete` line the gate writes last, once every
connection's close line is written; an exit code alone proves nothing, since a gate killed by its memory limit exits
too. Every count is None, with its reasons in `unknown`, when any line may be missing or cannot be read: the gate was
not shown stopped (`gate_not_stopped`), its log holds no `teardown_complete` (`gate_teardown_unconfirmed`), its log
could not be read or written whole (`gate_log_unreadable`, `gate_log_malformed`, `gate_log_write_failed`), its own
output could not be read to show neither went wrong (`gate_output_unreadable`), it could not write the close line of
every connection (`gate_teardown_incomplete`), or a request was still being answered when it stopped
(`request_in_flight_at_stop`). Unknown is never zero.

**The bytes served per file and per directory (O22, `served`).** The same lines, each answer of a file's content or a
listing put under the file or directory its path names, so a study can check an agent's claimed reads file by file,
which a run's total cannot (a read log that moves reads between files keeps the total):

  * `files`: every file whose content the platform served (a `content` answer), keyed by its file id as the request's
    path names it (`/api/v3/oneprovider/data/<file id>/content`, never decoded), with the path as the gate logged it
    (`path`, which names the file by its id, never by a name), the number of its answers (`requests`), the byte ranges
    they served merged where they overlap or touch (`ranges`, each `[start, stop]` with `stop` excluded, lowest
    first), the bytes in those ranges, each once (`range_bytes`), and every body byte of its answers (`body_bytes`). A
    range begins where the platform's answer says: a 200 at the file's start, a 206 at the first byte of its
    `Content-Range` (`content_range` on the line), and runs for the bytes the gate's socket accepted. An answer the
    log cannot place (a 206 whose `Content-Range` is not one range that holds its bytes, or a line with no status of
    the platform's) leaves that file's `ranges`, `range_bytes` and `ranges_cut` None, never a part of them; a file
    whose attributes alone were read, or whose content read was refused or failed, served no content and has no row;
  * `listings`: every directory whose listing the platform served (a `listing` answer), keyed by its id as the path
    names it (`/api/v3/oneprovider/data/<directory id>/children`), with `path`, `requests` and `body_bytes`.

Each table is bounded by the run's `Caps`, which `[sandbox.served_bytes]` of platform.toml declares (`settings.py`): at
most `max_files` files and `max_directories` directories, the first the log shows, and at most `max_ranges_per_file`
ranges a file, the lowest; `files_total` and `listings_total` count every one read, `files_cut`, `listings_cut` and a
file's `ranges_cut` say a bound cut the list, and `range_bytes` counts every range whatever the bound. The caps in
effect are in the result (`caps`), whatever else is known. Whenever the run's count is unknown, so is every table
(None, with the same reasons), and an answer of a file's content or a listing whose path names no file or directory
makes the log malformed. How a reader tells a row the cap left out from a file never served is `served.lookup_file`.
"""

import dataclasses
import json
import re
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


# Why the bytes served are unknown (O22): each makes every count None, never a partial sum taken for the whole
GATE_NOT_STOPPED = "gate_not_stopped"
GATE_LOG_MALFORMED = "gate_log_malformed"
GATE_LOG_WRITE_FAILED = "gate_log_write_failed"
GATE_TEARDOWN_INCOMPLETE = "gate_teardown_incomplete"
GATE_TEARDOWN_UNCONFIRMED = "gate_teardown_unconfirmed"
GATE_OUTPUT_UNREADABLE = "gate_output_unreadable"
REQUEST_IN_FLIGHT = "request_in_flight_at_stop"
TEARDOWN_COMPLETE = "teardown_complete"  # the event the gate logs last, once every close line is written (gate.py)
SERVED_FIELDS = ("served_bytes", "content_bytes", "listing_bytes", "other_bytes", "gate_answer_bytes")
_BODY_FIELD = {
    "content": "content_bytes",
    "listing": "listing_bytes",
    "other": "other_bytes",
    "gate": "gate_answer_bytes",
}


TABLE_FIELDS = ("files", "files_total", "files_cut", "listings", "listings_total", "listings_cut")


@dataclasses.dataclass(frozen=True)
class Caps:
    """How much a run's tables of the bytes served may list (`[sandbox.served_bytes]` of platform.toml, O22): the
    files, the first read (`files_cut` says when there were more), the directories, the first listed (`listings_cut`),
    and the ranges of one file, the lowest (`ranges_cut`). Each is a whole number of at least 1."""

    max_files: int = 1000
    max_directories: int = 1000
    max_ranges_per_file: int = 64

    def record(self) -> dict[str, int]:
        """The caps as the snapshot records them, by the keys platform.toml declares them under."""
        return dataclasses.asdict(self)


DEFAULT_CAPS = Caps()
_DATA_PATH = "/api/v3/oneprovider/data/"  # gatepolicy's routes of a file's content and of a listing
_CONTENT_PATH = re.compile(re.escape(_DATA_PATH) + r"([^/]+)/content")
_LISTING_PATH = re.compile(re.escape(_DATA_PATH) + r"([^/]+)/children")
_CONTENT_RANGE = re.compile(r"bytes ([0-9]{1,18})-([0-9]{1,18})/(?:[0-9]{1,18}|\*)")


def served_unknown(*reasons: str, caps: Caps = DEFAULT_CAPS) -> dict[str, Any]:
    """The bytes served, unknown for these reasons: every count None, and every table; the caps are still the run's."""
    tables = dict.fromkeys(TABLE_FIELDS)
    return {**dict.fromkeys(SERVED_FIELDS), **tables, "caps": caps.record(), "unknown": sorted(set(reasons))}


def served_nothing(caps: Caps = DEFAULT_CAPS) -> dict[str, Any]:
    """The bytes served through no data path: every count zero, and every table empty."""
    return {**dict.fromkeys(SERVED_FIELDS, 0), **_Tables(caps).snapshot(), "unknown": []}


def _span(record: Mapping[str, Any], size: int) -> tuple[int, int] | None:
    """Where in its file one answer's `size` bytes of content lie, as [start, stop) with stop excluded: from the start
    for the platform's 200, from the first byte of its `Content-Range` for its 206. None when the line cannot place
    them: a 206 whose `Content-Range` is not one range holding `size` bytes, or no status of the platform's (a cut
    transfer's line carries it as `upstream_status`)."""
    status = record.get("upstream_status", record.get("status"))
    if status == 200:
        return 0, size
    value = record.get("content_range")
    found = _CONTENT_RANGE.fullmatch(value) if status == 206 and isinstance(value, str) else None
    if found is None or size > int(found[2]) - int(found[1]) + 1:
        return None
    return int(found[1]), int(found[1]) + size


def _merged(spans: list[tuple[int, int]]) -> list[list[int]]:
    """The intervals joined where they overlap or touch, lowest first."""
    out: list[list[int]] = []
    for start, stop in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], stop)
        else:
            out.append([start, stop])
    return out


class _Tables:
    """The rows of the files and directories one log's answers served (the module docstring says what each holds)."""

    def __init__(self, caps: Caps = DEFAULT_CAPS) -> None:
        self._caps = caps
        self._files: dict[str, dict[str, Any]] = {}
        self._file_ids: set[str] = set()  # every file read, listed or not
        self._listings: dict[str, dict[str, Any]] = {}
        self._dir_ids: set[str] = set()

    def add(self, record: Mapping[str, Any], field: str, size: int) -> bool:
        """Put one answer's line under its file or directory; False when its path names none (a malformed line)."""
        if field not in ("content_bytes", "listing_bytes"):
            return True
        content = field == "content_bytes"
        path = record.get("path")
        found = (_CONTENT_PATH if content else _LISTING_PATH).fullmatch(path) if isinstance(path, str) else None
        if found is None:
            return False
        rows, seen, bound = (
            (self._files, self._file_ids, self._caps.max_files)
            if content
            else (self._listings, self._dir_ids, self._caps.max_directories)
        )
        seen.add(found[1])
        row = rows.get(found[1])
        if row is None:
            if len(rows) >= bound:
                return True  # counted in the total, and the table says it was cut
            row = rows[found[1]] = {"path": path, "requests": 0, "body_bytes": 0} | ({"spans": []} if content else {})
        row["requests"] += 1
        row["body_bytes"] += size
        if content and size and row["spans"] is not None:
            span = _span(record, size)
            if span is None:
                row["spans"] = None  # one answer it cannot place: the file's ranges are unknown, never a part
            else:
                row["spans"].append(span)  # the row is this table's own accumulator
        return True

    def snapshot(self) -> dict[str, Any]:
        files, most = {}, self._caps.max_ranges_per_file
        for file_id, row in self._files.items():
            spans = row["spans"]
            merged = None if spans is None else _merged(spans)
            files[file_id] = {
                "path": row["path"],
                "requests": row["requests"],
                "ranges": None if merged is None else merged[:most],
                "range_bytes": None if merged is None else sum(stop - start for start, stop in merged),
                "ranges_cut": None if merged is None else len(merged) > most,
                "body_bytes": row["body_bytes"],
            }
        return {
            "files": files,
            "files_total": len(self._file_ids),
            "files_cut": len(self._file_ids) > len(files),
            "listings": {dir_id: dict(row) for dir_id, row in self._listings.items()},
            "listings_total": len(self._dir_ids),
            "listings_cut": len(self._dir_ids) > len(self._listings),
            "caps": self._caps.record(),
        }


def _stop_reasons(stop: Mapping[str, Any] | None) -> set[str]:
    """Why what the gate's stop found leaves its log possibly short: it must have been signalled and shown exited, with
    its own output read and showing no failed write of its log and no close line it could not write."""
    if not isinstance(stop, Mapping) or stop.get("signalled") is not True or "exit_code" not in stop:
        return {GATE_NOT_STOPPED}
    if stop.get("output_read") is not True:  # the gate's output not read: whether a write failed is not known
        return {GATE_OUTPUT_UNREADABLE}
    reasons = set()
    if stop.get("log_write_failed") is not False:
        reasons.add(GATE_LOG_WRITE_FAILED)
    if stop.get("teardown_incomplete") is not False:
        reasons.add(GATE_TEARDOWN_INCOMPLETE)
    return reasons


def served(lines: list[str] | None, stop: Mapping[str, Any] | None, caps: Caps = DEFAULT_CAPS) -> dict[str, Any]:
    """The bytes the gate served the agent in a run, in all and per file and directory, from its log once it was
    stopped (O22; the module docstring says what each count holds). `lines` are the log's lines, None when it could not
    be read; `stop` is what the stop of the gate found (`gate_stop` of the sandbox's metadata, `_stop_reasons`); `caps`
    bound the tables. The log must hold the gate's `teardown_complete` line, whatever its exit code."""
    reasons = _stop_reasons(stop) | (set() if lines is not None else {UNREADABLE})
    counts, tables = dict.fromkeys(_BODY_FIELD.values(), 0), _Tables(caps)
    confirmed = False
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
        if confirmed and "method" in record:  # a request's line after the mark: the mark did not cover it
            reasons.add(GATE_TEARDOWN_UNCONFIRMED)
        confirmed = confirmed or record.get("event") == TEARDOWN_COMPLETE
        if "body_bytes" not in record:
            if "method" in record:  # a request's line always has its answer's body bytes
                reasons.add(GATE_LOG_MALFORMED)
            continue
        size, field = record["body_bytes"], _BODY_FIELD.get(record.get("body"))
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or field is None:
            reasons.add(GATE_LOG_MALFORMED)  # a line that does not say what it served is no count
            continue
        counts[field] += size
        if not tables.add(record, field, size):
            reasons.add(GATE_LOG_MALFORMED)  # content or a listing of no file or directory the log can name
    if lines is not None and not confirmed:  # an exit is no proof: a gate killed by its memory limit exits as well
        reasons.add(GATE_TEARDOWN_UNCONFIRMED)
    if reasons:
        return served_unknown(*reasons, caps=caps)
    total = counts["content_bytes"] + counts["listing_bytes"] + counts["other_bytes"]
    return {"served_bytes": total, **counts, **tables.snapshot(), "unknown": []}
