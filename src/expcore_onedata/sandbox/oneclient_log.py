# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/oneclient_log.py, blob 40f08eced5e22b3c17a5d6db07d96ff5b923aa75
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""What the oneclient sidecar's own log says went wrong, per run: the M counterpart of the gate's 5xx counts.

Read from outside the agent (the sidecar's log file and its container output), so it adds nothing to the agent's
interpreter and nothing that one option has and the others lack. Format of a line (glog):

    [IWEF]mmdd hh:mm:ss.uuuuuu threadid file:line] message

Only warnings, errors and fatals are counted. Each is classified by ORIGIN where the line itself allows it:

  sidecar      the client side: the connection to the provider (connectionPool.cc, handshake, TLS) and the storage
               helper the client builds for direct IO (fuseFileHandle.cc "Could not create storage helper");
  platform     the line says the Oneprovider answered with an error (none seen in the logs of 31 Oct - 1 Nov);
  unattributed everything else, notably `fsOperations.cc: Fuse error exception: <strerror>`: the FUSE layer reports
               the errno it returns to the application, but the line does not say whether the cause was local or a
               provider reply, so it is not guessed.

`agent_saw_sidecar_error` is NOT derived here: whether the agent's process received the errno is a fact of the
agent's file operations (the #9 tracer's errno records), and stays None (unknown) until that is read.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter

LINE = re.compile(r"^([IEWF])(\d{4}) (\d\d:\d\d:\d\d\.\d+)\s+(\d+) ([\w./-]+):(\d+)\] (.*)$")
SIDECAR_FILES = ("connectionPool.cc", "handshake", "tls", "ssl", "fuseFileHandle.cc", "storageHelper", "helpers")
CONTEXT_WINDOW_S = 5.0  # how far back a FUSE exception looks for the sidecar error that may explain it
FUSE_LINES_KEPT = 50
# UNOBSERVED so far: no log of 31 Oct - 1 Nov contains a line in which the provider replies with an error, so these
# markers are guesses at oneclient's wording and the platform bucket has never been filled by a real line.
PLATFORM_MARKERS = (
    "oneprovider returned",
    "provider returned",
    "received error from oneprovider",
    "oneprovider responded with",
    "error response from oneprovider",
)
# errnos that the agent's OWN request causes, whatever the operation: it asked for a path that is not there, wrote to the
# read-only mount, opened a directory as a file. The mount did what it should; this is not a failure of the data path, and
# no "the agent saw an error" fallback may count it.
AGENT_CAUSED = {"No such file or directory": "ENOENT", "Read-only file system": "EROFS", "Is a directory": "EISDIR"}
# NOT in AGENT_CAUSED on purpose: EACCES and EPERM. On a write, or a path outside the study space, the agent caused them;
# on a read inside the study space they mean our read-only token failed or expired, an infrastructure fault. The FUSE
# line (`fsOperations.cc:116] Fuse error exception: <strerror>`) names neither the operation nor the path, and the lines
# next to it (`fuseFileHandle.cc: Could not create storage helper for file <id> ...`) give a file id, not the operation, so
# a read cannot be told from a write here. Not guessing: they count as NOT agent-caused, the direction that surfaces
# infrastructure faults. They are marked `ambiguous_permission` so the analysis can see them.
AMBIGUOUS_PERMISSION = {"Permission denied": "EACCES", "Operation not permitted": "EPERM"}
# errno texts the FUSE layer reports (strerror) for the transient failures that matter to a reader
TRANSIENT = {
    "Input/output error": "EIO",
    "Resource temporarily unavailable": "EAGAIN",
    "Transport endpoint is not connected": "ENOTCONN",
    "Stale file handle": "ESTALE",
    "Connection timed out": "ETIMEDOUT",
    "Host is down": "EHOSTDOWN",
    "Connection refused": "ECONNREFUSED",
}


def signature(msg: str) -> str:
    """The message with ids, numbers and file handles removed, so equal failures count together."""
    s = re.sub(r"\b[A-Za-z0-9+/=_-]{24,}\b", "<id>", msg)
    return re.sub(r"\d+", "N", s)[:160]


def ambiguous_permission(kind: str) -> bool:
    """EACCES / EPERM: the agent's own doing on a write, an expired token on a read; the log cannot say which."""
    return kind.startswith("fuse_error:") and kind.split(":", 1)[1] in AMBIGUOUS_PERMISSION.values()


def caused_by_agent(kind: str) -> bool:
    """True for a FUSE exception whose errno the agent's own request causes whatever the operation (see AGENT_CAUSED)."""
    return kind.startswith("fuse_error:") and kind.split(":", 1)[1] in AGENT_CAUSED.values()


def classify(file: str, msg: str) -> tuple[str, str]:
    """(origin, kind) of one warning/error line."""
    low = msg.lower()
    if any(m in low for m in PLATFORM_MARKERS):
        return "platform", "provider_error_reply"
    if file == "connectionPool.cc" or "failed to establish connection" in low:
        return "sidecar", "provider_connection"
    if "could not create storage helper" in low:
        return "sidecar", "storage_helper"
    m = re.search(r"fuse error exception: (.*)$", low)
    if m:
        text = m.group(1).strip()
        errno = next(
            (v for k, v in {**TRANSIENT, **AGENT_CAUSED, **AMBIGUOUS_PERMISSION}.items() if k.lower() == text), "other"
        )
        return "unattributed", f"fuse_error:{errno}"
    if any(f.lower() in file.lower() for f in SIDECAR_FILES):
        return "sidecar", "client_internal"
    return "unattributed", "other"


HEADER = re.compile(r"^Log file created at: (\d{4})/(\d\d)/(\d\d) (\d\d:\d\d:\d\d)")
FALLBACK_YEAR = 2024  # a leap year: used only for a log without its creation header
REF_YEAR = 2000  # a leap year: orders month-days (Feb 29 included) to find a year end


def _t(year: int, md: str, ts: str) -> float | None:
    """Seconds of a log line's timestamp on a naive calendar (no time zone, no DST: datetime.timestamp() would apply the
    host's). The line has no year; the caller supplies it (from the log's creation header) and advances it at a year
    end. None for a timestamp that is not a date in that year (Feb 29 in a non-leap year, 99 seconds): such a line has no
    context, and nothing raises."""
    try:
        h, m, sec = ts.split(":")
        sec_f = float(sec)
        if not 0 <= sec_f < 60:  # datetime would accept the minutes and hours but not the seconds: reject them here
            return None
        return (
            dt.datetime(year, int(md[:2]), int(md[2:]), int(h), int(m)) - dt.datetime(2000, 1, 1)
        ).total_seconds() + sec_f
    except (ValueError, OverflowError):
        return None


def summarize(log_text: str) -> dict:
    """Counts of the warnings and errors in oneclient's log: by origin, by kind and by message signature.

    For each unattributed `Fuse error exception` the kind of the nearest preceding `sidecar` error line within
    CONTEXT_WINDOW_S is recorded as `preceded_by` (null if none): context for the analysis, NOT a classification, so
    "EAGAIN right after a storage-helper failure" is visible without an origin being guessed."""
    by_origin, by_kind, sigs, ctx = Counter(), Counter(), Counter(), Counter()
    fuse_lines: list[dict] = []
    first = last = None
    last_sidecar: tuple[float, str] | None = None
    n_lines = n_bad = fuse_total = fuse_agent = 0
    year, prev_ref = (
        FALLBACK_YEAR,
        None,
    )  # year end: a month-day more than half a year before the previous one is next year
    for raw in log_text.splitlines():
        n_lines += 1
        h = HEADER.match(raw)
        if h:
            year = int(h.group(1))
            prev_ref = _t(
                REF_YEAR, h.group(2) + h.group(3), h.group(4)
            )  # the creation date seeds the year-end tracking
            continue
        m = LINE.match(raw)
        if not m:
            continue
        level, md, ts, _tid, file, _ln, msg = m.groups()
        # calendar order on a leap reference year (every month-day exists there) decides a year end; only then is the line
        # checked against its real year, so Feb 29 right after a Dec 31 is next year's, not an invalid date of this one
        ref = _t(REF_YEAR, md, ts)
        if ref is not None:
            if prev_ref is not None and ref < prev_ref - 183 * 86400:
                year += 1
            prev_ref = ref
        t = _t(year, md, ts)
        if level == "I":
            continue
        n_bad += 1
        origin, kind = classify(file, msg)
        by_origin[origin] += 1
        by_kind[kind] += 1
        sigs[f"{level} {file}: {signature(msg)}"] += 1
        first, last = first or f"{md} {ts}", f"{md} {ts}"
        if origin == "sidecar":
            last_sidecar = (t, kind) if t is not None else None
        elif kind.startswith("fuse_error:"):
            near = (
                last_sidecar
                if (t is not None and last_sidecar and 0 <= t - last_sidecar[0] <= CONTEXT_WINDOW_S)
                else None
            )
            ctx[near[1] if near else "none"] += 1
            agent = caused_by_agent(kind)
            fuse_total += 1
            fuse_agent += agent
            if len(fuse_lines) < FUSE_LINES_KEPT:
                fuse_lines.append(
                    {
                        "t": f"{md} {ts}",
                        "errno": kind.split(":", 1)[1],
                        "preceded_by": near[1] if near else None,
                        "gap_s": round(t - near[0], 6) if near else None,
                        "caused_by_agent": agent,
                        "ambiguous_permission": ambiguous_permission(kind),
                    }
                )
    return {
        "total": n_bad,
        "by_origin": dict(by_origin),
        "by_kind": dict(by_kind),
        "signatures": dict(sigs.most_common(20)),
        "fuse_preceded_by": dict(ctx),
        "fuse_lines": fuse_lines,
        "fuse_total": fuse_total,
        "fuse_caused_by_agent": fuse_agent,
        "fuse_not_caused_by_agent": fuse_total - fuse_agent,
        "first": first,
        "last": last,
        "log_lines": n_lines,
    }


DETECT_START = re.compile(
    r"Performing automatic storage access type detection for storage (\S+) for file \S+ "
    r"with forced proxy io mode: (\d)"
)
DETECT_END = re.compile(r"Storage '(\S+)' is not directly accessible to the client\.")
DETECT_RESULTS = {DETECT_END: "not_directly_accessible"}  # the only end line oneclient 25.0 has been seen to write
DETECT_NOTE = (
    "oneclient logs the start (helpersCache.h:467) and the end (helpersCache.h:752) of each storage access type "
    "detection; the first phase (auto mode) is storage_detection_s; a later phase (forced proxy io mode 1) follows "
    "at once in the same log and is listed in phases, not added: the log does not say whether the application waits for it"
)


def storage_detection(log_text: str) -> dict:
    """Oneclient's storage access type detection, from its own log (the M set-up cost that is not the mount itself).

    Each detection has a start line and an end line; a phase is the log-time difference of the two (container clock, so
    the host's load does not enter). `storage_detection_s` is the FIRST complete phase (the one the application's first read
    waits for: 5.1 s against a 5.59 s first read, #7). `None` with `none_reason` when there is no start line, or the first
    start has no end line (a detection that never finished is not a duration), or the stamps do not order. Never 0."""
    year, phases, open_ = FALLBACK_YEAR, [], {}
    for raw in log_text.splitlines():
        h = HEADER.match(raw)
        if h:
            year = int(h.group(1))
            continue
        m = LINE.match(raw)
        if not m or m.group(1) != "I":
            continue
        _lv, md, ts, _tid, file, _ln, msg = m.groups()
        if file != "helpersCache.h":
            continue
        t = _t(year, md, ts)
        st = DETECT_START.search(msg)
        if st:
            ph = {
                "storage": st.group(1),
                "forced_proxy_io_mode": int(st.group(2)),
                "start": f"{md} {ts}",
                "end": None,
                "seconds": None,
                "result": None,
                "_t0": t,
            }
            phases.append(ph)
            open_[st.group(1)] = ph
            continue
        for rx, result in DETECT_RESULTS.items():
            en = rx.search(msg)
            ph = open_.pop(en.group(1), None) if en else None
            if ph is not None:
                ph["end"], ph["result"] = f"{md} {ts}", result
                if t is not None and ph["_t0"] is not None and t > ph["_t0"]:
                    ph["seconds"] = round(t - ph["_t0"], 6)
    for ph in phases:
        del ph["_t0"]
    out = {
        "storage_detection_s": None,
        "none_reason": None,
        "start": None,
        "end": None,
        "result": None,
        "forced_proxy_io_mode": None,
        "phases": phases,
        "note": DETECT_NOTE,
    }
    if not phases:
        out["none_reason"] = (
            "no storage access type detection line in the log (no file read yet, or oneclient did not log one)"
        )
    elif phases[0]["seconds"] is None:
        out["none_reason"] = (
            "the first detection has no usable end line (not finished, an end line of unseen wording, "
            "or timestamps out of order)"
        )
    else:
        f = phases[0]
        out.update(
            storage_detection_s=f["seconds"],
            start=f["start"],
            end=f["end"],
            result=f["result"],
            forced_proxy_io_mode=f["forced_proxy_io_mode"],
        )
    return out
