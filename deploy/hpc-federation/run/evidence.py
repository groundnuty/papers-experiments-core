"""evidence.py: the run's summary, derived from the evidence files and from nothing else.

  python3 -I evidence.py <evidence dir>     print the summary as JSON; exit 0 on PASS, 1 on FAIL, 2 on a usage error

The last live run's driver printed a literal False as the result of its scan of the gate's log for the token, so the
scan never ran and the summary said it had passed. The rule here, in place of that:

1. Every step writes raw evidence to files under the run's evidence directory (run/README.md lists them).
2. Every summary item is built by `build_item`, from the name of the item, the evidence file(s) it reads and a parser,
   and by nothing else: `Item` refuses to be built any other way. `build_item` reads the files first. A file that is
   missing, empty or not JSON fails the item, and the parser is not called. A parser that raises, or that returns
   anything but a `verdict(...)`, fails the item. A parser sees only what the files hold, so the verdict is a
   function of the evidence: there is no way to put a literal True into the summary.
3. The verdict is PASS only when every item of the catalog was parsed and every item's own check passed. An item
   the catalog lists is always built, so a step that was skipped shows as a failed item, never as an absent one.

Stdlib only, Python 3.8 (the login node's), no import of the core: classifying the egress log's destinations (gate,
loopback, outside) is `expcore.egress`'s job, not this file's. The summary names no host, node or address, only roles
and counts.
"""

from __future__ import annotations

import glob
import hashlib
import ipaddress
import json
import os
import sys

SCHEMA = "hpcop-evidence/1"
PROVIDERS = tuple("P%d" % n for n in (1, 2, 3))
SERVICES = ("Z", *PROVIDERS)
READERS = PROVIDERS[1:]  # the providers that read what the first one wrote
NODES = (*SERVICES, "S")
INTERNAL_PORTS = ("8091", "11210", "9443", "4369")  # Couchbase, Couchbase's data port, onepanel, the default epmd
GUARD_WHENS = ("after onepanel's Couchbase setup", "after the rotation")
GUARD_REQUESTS = {"GET /settings/web (administrator)": 200, "GET /settings/web (anonymous)": 401}
GUARD_REQUIRED = {
    (check, when, request)
    for check, whens in (("cb_watch", ("start of the service",)), ("cb_guard", GUARD_WHENS))
    for when in whens
    for request in GUARD_REQUESTS
}
# What sandbox.sh's scan() greps for the token: the evidence copy of each log, which the summary hashes and compares
# with the scan's own record of what it scanned.
SCANNED_FILES = {
    "gate-requests": "sandbox/gate-requests.jsonl",
    "gate-stdout": "sandbox/gate-stdout.txt",
    "gate-stderr": "sandbox/gate-stderr.txt",
    "agent-stdout": "sandbox/agent.json",
    "agent-stderr": "sandbox/agent.err",
    "egress-log": "sandbox/egress.jsonl",
}
SCANNED = tuple(SCANNED_FILES)
RAW = "raw:"  # a slot of a log no item parses: read whole, for its SHA-256 and size
HTTP_LOGS = 4  # roundtrip/http-*.jsonl: the zone's prepare-n, and one per provider's work directory
HEX = "0123456789abcdef"
# The collector's log, as egress-collector.py writes it and expcore.egress.log reads it.
EGRESS_FORMAT = "hpcop-egress/1"
EGRESS_EVENTS = ("start", "record", "malformed", "end")
EGRESS_OUTCOMES = ("result", "async")
EGRESS_TYPES = ("tcp", "udp", "raw", "other")
EGRESS_FAMILIES = {"inet": 4, "inet6": 6}
EGRESS_REC_KEYS = {"op", "pid", "fd", "time", "type", "family", "address", "port", "result", "dropped"}
CELL_FILES = ["agent.py", "ca.pem", "hosts.txt"]
OTHER_STEPS = ("batch", "extern")  # the allocation's own steps, which a teardown leaves
OURS = "hpcop-"  # the name every job of ours starts with
# The only POSTs the gate's policy allows, both lookups that read (gatepolicy.py); every other POST is a write.
READ_POSTS = ("/api/v3/oneprovider/lookup-file-id/", "/api/v3/onezone/tokens/infer_access_token_scope")
ATTEMPTS = ("connect", "sendto", "sendmsg", "sendmmsg")  # the shim's records that name a destination asked for
NODE_COUNTS = ("leftover_tmp", "tmp_top", "jobtmp", "listeners", "procs_other")
NODE_COMMANDS = ("ps_rc", "ss_rc")  # the node check's listings: a count from one that failed is no count


class _Verdict:
    """What a parser returns: whether its check held, and the numbers and strings it read from the files."""

    __slots__ = ("detail", "passed")

    def __init__(self, passed, detail):
        if not isinstance(passed, bool):
            raise TypeError("a verdict's check must be a bool computed from the files, not %r" % (passed,))
        self.passed, self.detail = passed, detail


def verdict(passed, **detail):
    """The only thing a parser may return. `passed` is the parser's own check of what it read."""
    return _Verdict(passed, detail)


_SEAL = object()


class Item:
    """One line of the summary. Built only by `build_item`: the constructor wants its private seal."""

    __slots__ = ("detail", "error", "evidence", "files", "kind", "name", "parsed", "passed")

    def __init__(self, seal, name, kind, evidence, files, parsed, passed, detail, error):
        if seal is not _SEAL:
            raise TypeError(
                "a summary item is built by build_item, from evidence files and a parser, and by nothing else"
            )
        if not evidence:
            raise ValueError("%s: an item must name the evidence file(s) it reads" % name)
        if passed and not (parsed and files):
            raise ValueError("%s: an item cannot pass without a parsed evidence file" % name)
        self.name, self.kind, self.evidence, self.files = name, kind, tuple(evidence), list(files)
        self.parsed, self.passed, self.detail, self.error = parsed, passed, detail, error

    def as_dict(self):
        return {
            "name": self.name,
            "kind": self.kind,
            "parsed": self.parsed,
            "passed": self.passed,
            "evidence": list(self.evidence),
            "files": self.files,
            "detail": self.detail,
            "error": self.error,
        }


def _unique(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is given twice")
    return dict(pairs)


def _read_object(path, rel):
    """(object, file record) for one JSON file holding one object; ValueError names what is wrong."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        raise ValueError("missing: %s" % rel) from None
    except OSError as err:
        raise ValueError("unreadable: %s (%s)" % (rel, type(err).__name__)) from None
    record = {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    if not data.strip():
        raise ValueError("empty: %s" % rel)
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
    except ValueError:
        raise ValueError("unparsable: %s" % rel) from None
    if not isinstance(value, dict):
        raise ValueError("unparsable: %s (not one JSON object)" % rel)
    return value, record


def _read_lines(path, rel):
    """(list of objects, file record) for a JSON-lines file with at least one line."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        raise ValueError("missing: %s" % rel) from None
    except OSError as err:
        raise ValueError("unreadable: %s (%s)" % (rel, type(err).__name__)) from None
    record = {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    if not data.strip():
        raise ValueError("empty: %s" % rel)
    rows = []
    try:
        for line in data.decode("utf-8").splitlines():
            if line.strip():
                row = json.loads(line, object_pairs_hook=_unique)
                if not isinstance(row, dict):
                    raise ValueError("a line that is not an object")
                rows.append(row)
    except ValueError:
        raise ValueError("unparsable: %s" % rel) from None
    return rows, record


def _read_raw(path, rel):
    """({"sha256", "bytes"}, file record) for a log kept as it was written: present, and read whole; it may be empty."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        raise ValueError("missing: %s" % rel) from None
    except OSError as err:
        raise ValueError("unreadable: %s (%s)" % (rel, type(err).__name__)) from None
    record = {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return {"sha256": record["sha256"], "bytes": record["bytes"]}, record


def _read_one(path, rel):
    if rel.endswith(".jsonl"):
        return _read_lines(path, rel)
    return _read_object(path, rel)


def _load(base, pattern):
    """One slot: a path (one object), a path ending in .jsonl (a list of objects), a glob of either (a list of those),
    or "raw:" and a path (a log no item parses: its SHA-256 and size)."""
    raw = pattern.startswith(RAW)
    rel = pattern[len(RAW) :] if raw else pattern
    if ".." in rel.split("/") or rel.startswith("/"):
        raise ValueError("an evidence path stays under the evidence directory: %s" % rel)
    path = os.path.join(base, *rel.split("/"))
    if raw:
        doc, record = _read_raw(path, rel)
        return doc, [record]
    if "*" in rel:
        found = sorted(glob.glob(path))
        if not found:
            raise ValueError("missing: %s" % rel)
        docs, records = [], []
        for name in found:
            doc, record = _read_one(name, os.path.relpath(name, base).replace(os.sep, "/"))
            docs.append(doc)
            records.append(record)
        return docs, records
    doc, record = _read_one(path, rel)
    return doc, [record]


def build_item(base, name, kind, slots, parser):
    """The one constructor of a summary item: `slots` is ((key, evidence path), ...), `parser` takes {key: parsed}."""
    if not slots:
        raise ValueError("%s: an item must name the evidence file(s) it reads" % name)
    docs, files, problems = {}, [], []
    for key, pattern in slots:
        try:
            docs[key], records = _load(str(base), pattern)
            files.extend(records)
        except ValueError as err:
            problems.append(str(err))
    evidence = [pattern for _, pattern in slots]
    if problems:
        return Item(_SEAL, name, kind, evidence, files, False, False, {}, "; ".join(problems))
    try:
        result = parser(docs)
    except Exception as err:  # a file in a shape the parser does not know is an unparsed file, never a pass
        error = "unparsable evidence: %s: %s" % (type(err).__name__, err)
        return Item(_SEAL, name, kind, evidence, files, False, False, {}, error)
    if not isinstance(result, _Verdict):
        error = "the parser returned %r, not a verdict(...) made from the files" % (result,)
        return Item(_SEAL, name, kind, evidence, files, False, False, {}, error)
    return Item(_SEAL, name, kind, evidence, files, True, result.passed, result.detail, None)


# --- typed reads: a wrong type raises, and a raise fails the item ----------------------------------------------------


def _int(doc, key):
    value = doc[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s is not an integer" % key)
    return value


def _str(doc, key):
    value = doc[key]
    if not isinstance(value, str) or not value:
        raise ValueError("%s is not a non-empty string" % key)
    return value


def _hex64(doc, key):
    value = _str(doc, key)
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("%s is not a SHA-256 in lowercase hex" % key)
    return value


def _obj(doc, key):
    value = doc[key]
    if not isinstance(value, dict):
        raise ValueError("%s is not an object" % key)
    return value


def _list(doc, key):
    value = doc[key]
    if not isinstance(value, list):
        raise ValueError("%s is not a list" % key)
    return value


def _is_status(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _refused(value):
    """A refusal is an HTTP 4xx the platform or the gate answered; a transport error's text is no refusal."""
    return _is_status(value) and 400 <= value < 500


def _checked(checks, **detail):
    """A verdict that holds when every named check does, and says which did not."""
    failing = sorted(name for name, held in checks.items() if not held)
    return verdict(not failing, failing=failing, **detail)


def _rc(doc):
    return _int(doc, "rc")


# --- the parsers, one per catalog item ---------------------------------------------------------------------------


def _service_up(d):
    counts = _obj(d["up"], "counts")
    n = {key: _int(counts, key) for key in sorted(counts)}  # a count that is not an integer fails the item
    checks = {
        "its verdict line (OK zone / OK provider)": n["verdict_ok"] >= 1,
        "ok lines": n["ok_lines"] >= 1,
        "no FAIL line": n["fail_lines"] == 0,
        "no ERROR line": n["error_lines"] == 0,
        "no FINISH FAILED": n["finish_failed"] == 0,
        "the guard's verdict": n["guard_passed"] >= 1,
        "the live default check": n["defaults_ok"] >= 1,
        "the live port check": n["port_check_ok"] >= 1,
        "the live hashed check": n["live_hash_ok"] >= 1,
    }
    return _checked(checks, counts=n, log_lines=_int(d["up"], "log_lines"))


def _guard(d):
    seen, wrong = set(), []
    for line in d["lines"]:
        check, when, request = _str(line, "check"), _str(line, "when"), _str(line, "request")
        status, expected = _int(line, "status"), _int(line, "expected")
        if request not in GUARD_REQUESTS:
            raise ValueError("an unknown guard request")
        if status == expected == GUARD_REQUESTS[request]:
            seen.add((check, when, request))
        else:
            wrong.append([check, request, status, expected])
    missing = sorted(GUARD_REQUIRED - seen)
    return _checked(
        {"every status is the expected one": not wrong, "every stage and request ran": not missing},
        lines=len(d["lines"]),
        wrong=wrong,
        missing=[list(m) for m in missing],
    )


def _registration(d):
    mint, deliver = _int(d["reg"], "mint_rc"), _int(d["reg"], "deliver_rc")
    return _checked({"the mint step": mint == 0, "the delivery step": deliver == 0}, mint_rc=mint, deliver_rc=deliver)


def _providers_online(d):
    listed = list(_list(d["out"], "providers"))
    up = sum(1 for p in listed if p["online"] is True)
    reported = _int(d["out"], "online")
    checks = {
        "the step's exit status": _rc(d["rc"]) == 0,
        "three online": up >= 3,
        "its own count agrees": reported == up,
    }
    return _checked(checks, providers=len(listed), online=up)


def _space_prepared(d):
    rc, size = _int(d["prep"], "rc"), _int(d["prep"], "stdout_bytes")
    return _checked({"the step's exit status": rc == 0, "it printed its result": size > 0}, rc=rc, stdout_bytes=size)


def _support(d):
    onboard = _int(d["out"], "onboard_rc")
    checks = {
        "the step's exit status": _rc(d["rc"]) == 0,
        "onboarding's exit status": onboard == 0,
        "the supported space is the written one": _str(d["out"], "space_id") == _str(d["write"], "space_id"),
    }
    return _checked(checks, onboard_rc=onboard)


def _write(d):
    out = d["out"]
    written, back, size = _hex64(out, "sha256"), _hex64(out, "sha256_read_back"), _int(out, "bytes")
    checks = {
        "the step's exit status": _rc(d["rc"]) == 0,
        "the read-back SHA-256 is the written one": written == back,
        "bytes": size > 0,
        "ids": all(_str(out, key) for key in ("space_id", "root_id", "file_id")),
    }
    return _checked(checks, bytes=size, sha256=written, sha256_read_back=back)


def _read(d):
    out, write = d["out"], d["write"]
    got, want, written = _hex64(out, "sha256_read"), _hex64(out, "sha256_expected"), _hex64(write, "sha256")
    checks = {
        "the step's exit status": _rc(d["rc"]) == 0,
        "the read answered 200": _int(out, "read_status") == 200,
        "the SHA-256 read is the expected one": got == want,
        "the expected one is the written one": want == written,
        "the file is the written one": _str(out, "file_id") == _str(write, "file_id"),
    }
    return _checked(checks, sha256_read=got, sha256_expected=want)


def _sandbox_step(d):
    step, agent = _rc(d["step"]), _rc(d["agent"])
    return _checked({"the sandbox step": step == 0, "the cell": agent == 0}, step_rc=step, agent_rc=agent)


def _gate_read(d):
    read, write = _obj(d["agent"], "gate_read"), d["write"]
    got, want = _hex64(read, "sha256_read"), _hex64(read, "sha256_expected")
    checks = {
        "answered 200": _int(read, "status") == 200,
        "the SHA-256 read is the expected one": got == want,
        "the expected one is the written one": want == _hex64(write, "sha256"),
        "the byte count is the written one": _int(read, "bytes") == _int(write, "bytes"),
    }
    return _checked(checks, status=read["status"], bytes=read["bytes"], sha256_read=got, sha256_expected=want)


def _gate_listing(d):
    listing = _obj(d["agent"], "gate_listing")
    entries = _int(listing, "entries")
    return _checked({"answered 200": _int(listing, "status") == 200, "listed an entry": entries >= 1}, entries=entries)


def _gate_lookup(d):
    lookup = _obj(d["agent"], "gate_lookup")
    got, want = _str(lookup, "file_id"), _str(lookup, "file_id_expected")
    checks = {
        "answered 200": _int(lookup, "status") == 200,
        "the id is the space root's": got == want,
        "the expected id is the writer's root": want == _str(d["write"], "root_id"),
    }
    return _checked(checks, status=lookup["status"])


def _gate_write_refused(d):
    status = _obj(d["agent"], "gate_write_refused")["status"]
    return _checked({"refused with a 4xx": _refused(status)}, status=status)


def _gate_strips(d):
    probe = _obj(d["agent"], "gate_strips_client_token")
    bad, control = probe["probe_status"], probe["control_status"]
    checks = {"the route answers without a client token": control == 200, "and with a bogus one": bad == 200}
    return _checked(checks, probe_status=bad, control_status=control)


def _direct(d):
    direct = _obj(d["agent"], "direct_no_token")
    space, content = direct["space_status"], direct["content_status"]
    checks = {
        "the space request is refused": space in (401, 403),
        "the content request is refused": content in (401, 403),
    }
    return _checked(checks, space_status=space, content_status=content)


def _reach(d):
    reach = _obj(d["agent"], "reach")
    opened, closed = [], []
    for role in SERVICES:
        ports = _obj(reach, role)
        for port in INTERNAL_PORTS:
            if _str(_obj(ports, port), "connect") == "open":
                opened.append([role, port])
        for port in ("8443",) + (("6665",) if role in PROVIDERS else ()):
            if _str(_obj(ports, port), "connect") != "open":
                closed.append([role, port])
    checks = {"no internal port is reachable": not opened, "the public ports are (the control)": not closed}
    return _checked(checks, internal_open=opened, public_closed=closed)


def _internet(d):
    connect = _str(_obj(d["agent"], "internet_443"), "connect")
    return verdict(connect in ("open", "refused", "timeout") or connect.startswith("error: "), connect=connect)


def _cell_clean(d):
    agent = d["agent"]
    files = sorted(_list(agent, "cell_files"))
    checks = {
        "no token-like variable": _list(agent, "env_token_like") == [],
        "the cell held its three files": files == CELL_FILES,
        "no Couchbase login was possible": _obj(agent, "couchbase_login_attempts") == {},
        "no epmd port was reachable": _list(agent, "epmd_open_roles") == [],
    }
    return _checked(checks, cell_files=files)


def _reads(row):
    """A GET, or a POST to one of the two lookups the policy allows."""
    method, path = _str(row, "method"), row.get("path")
    if method == "GET":
        return True
    lookup, scope = READ_POSTS
    return method == "POST" and isinstance(path, str) and (path.startswith(lookup) or path == scope)


def _forwarded_write(row):
    """A request that does not read, unless the gate refused it itself: its decision "deny" and a 4xx. A write the
    gate let through counts even when the platform then refused it."""
    return not _reads(row) and not (row.get("decision") == "deny" and _refused(row.get("status")))


def _gate_log(d):
    rows, end = d["rows"], _rc(d["end"])
    complete = [r for r in rows if r.get("event") == "teardown_complete"]
    methods = [r for r in rows if r.get("event") is None and "method" in r]
    forwarded = [r["method"] for r in methods if _forwarded_write(r)]
    reads = [r for r in methods if r["method"] == "GET" and r.get("status") == 200 and r.get("body") == "content"]
    refused = [r for r in methods if _refused(r.get("status"))]
    checks = {
        "it logged requests": len(methods) >= 1,
        "its teardown_complete line": len(complete) == 1,
        "no write was forwarded": not forwarded,
        "it served a content read": len(reads) >= 1,
    }
    return _checked(
        checks, lines=len(rows), requests=len(methods), content_reads=len(reads), refused=len(refused), gate_rc=end
    )


def _token_scan(d):
    scans, matches, errors, problems = {}, {}, [], []
    for doc in d["scans"]:
        name = _str(doc, "name")
        scans[name] = doc
    for name in SCANNED:
        if name not in scans:
            problems.append("no scan of %s" % name)
            continue
        doc, seen = scans[name], d["file:" + name]
        rc, found = _int(doc, "grep_rc"), doc.get("matches")
        if _int(doc, "pattern_bytes") <= 0 or _int(doc, "pattern_lines") != 1 or _int(doc, "pattern_blank_lines") != 0:
            problems.append("%s: the pattern file was not one non-blank line" % name)
        if rc not in (0, 1) or not _is_status(found):
            errors.append(name)
        elif (rc == 1) != (found == 0):
            problems.append("%s: the count contradicts grep's exit status" % name)
        elif found > 0:
            matches[name] = found
        # A count holds for the bytes it was taken of: the evidence file must be the one the scan read.
        if doc.get("scanned_sha256") != seen["sha256"] or doc.get("scanned_bytes") != seen["bytes"]:
            problems.append("%s: the evidence file is not the one scanned (changed or replaced after it)" % name)
        if name == "gate-requests" and seen["bytes"] <= 0:
            problems.append("the gate's log was empty")
    checks = {"every file was scanned": not problems, "no scan errored": not errors, "no match": not matches}
    return _checked(checks, scanned=sorted(scans), matches=matches, errors=errors, problems=problems)


def _destinations(records):
    """How many distinct (address, port, type) destinations the attempts name, and how many of those are loopback.
    The summary is what gets pasted: no address goes into it; the log keeps them, for the core to classify."""
    distinct = set()
    for rec in records:
        if rec["op"] in ATTEMPTS:
            distinct.add((ipaddress.ip_address(rec["address"]), rec["port"], rec["type"]))
    loopback = sum(1 for address, _, _ in distinct if address.is_loopback)
    return {"distinct": len(distinct), "loopback": loopback, "other": len(distinct) - loopback}


def _whole(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _egress_record(rec):
    """True when `rec` is a record as the collector parses it, and as expcore.egress.log reads it."""
    if not isinstance(rec, dict) or set(rec) != EGRESS_REC_KEYS:
        return False
    if rec["op"] not in ATTEMPTS + EGRESS_OUTCOMES or rec["type"] not in EGRESS_TYPES:
        return False
    if rec["family"] not in EGRESS_FAMILIES or not _whole(rec["port"], 0, 65535):
        return False
    if not all(_whole(rec[key], 0, 2**63) for key in ("pid", "fd", "dropped")) or not isinstance(rec["time"], str):
        return False
    if (rec["result"] is None) != (rec["op"] in ATTEMPTS):
        return False  # an attempt's result is "-", an outcome's a number
    if rec["result"] is not None and not _whole(rec["result"], 0, 4095):
        return False
    if rec["address"] == "-":
        return rec["op"] == "async"
    if not isinstance(rec["address"], str):
        return False
    try:
        return ipaddress.ip_address(rec["address"]).version == EGRESS_FAMILIES[rec["family"]]
    except ValueError:
        return False


def _egress_lines(rows):
    """Every line of a kind the collector writes, numbered 0, 1, 2, ... with a clock that never goes back, and every
    record in the collector's shape, as the core reads them. A ValueError, which fails the item, names the first line
    that is not."""
    last = None
    for index, row in enumerate(rows):
        mono = row.get("mono")
        if row.get("event") not in EGRESS_EVENTS:
            raise ValueError("line %d is of no kind the collector writes" % index)
        if not _whole(row.get("seq"), index, index) or isinstance(mono, bool) or not isinstance(mono, (int, float)):
            raise ValueError("line %d is not numbered %d with a clock reading" % (index, index))
        if last is not None and not mono >= last:
            raise ValueError("the clock goes back at line %d" % index)
        if row["event"] == "record" and not _egress_record(row.get("rec")):
            raise ValueError("line %d is not a record as the collector writes one" % index)
        last = mono


def _egress(d):
    rows, collector, canary = d["rows"], d["collector"], d["canary"]
    _egress_lines(rows)
    first, last = rows[0], rows[-1]
    address, port = _str(canary, "address"), _int(canary, "port")
    records = [row for row in rows if row["event"] == "record"]
    recs = [row["rec"] for row in records]
    malformed = sum(1 for row in rows if row["event"] == "malformed")
    started = first["event"] == "start" and first.get("format") == EGRESS_FORMAT and _whole(first.get("uid"), 0, 2**32)
    uid = first["uid"] if started else None
    # As the core reads the log (expcore.egress.log): a record from another uid than the collector's is not this cell's,
    # and a record whose sender dropped some says the log misses destinations.
    foreign = sum(1 for row in records if not isinstance(row.get("peer"), dict) or row["peer"].get("uid") != uid)
    dropped = sum(rec["dropped"] for rec in recs)
    # Nothing but the canary may connect through the shim between the collector's start and the canary: the first line
    # after the start is its record. A log that opens with anything else is unknown, not clean (the shim may not have
    # loaded).
    opened = rows[1]["rec"] if len(rows) > 1 and rows[1]["event"] == "record" else {}
    ends = [index for index, row in enumerate(rows) if row["event"] == "end"]
    checks = {
        "it opens with the collector's start line": started,
        "it is this launch's (its nonce)": first.get("nonce") == _str(collector, "nonce"),
        "one start line": sum(1 for row in rows if row["event"] == "start") == 1,
        "its end line is its last and only one": ends == [len(rows) - 1],
        "its end line counts the records": last.get("records") == len(records),
        "its end line counts the malformed lines": last.get("malformed") == malformed,
        "no datagram went unwritten": last.get("unwritten") == 0,
        "no malformed datagram": malformed == 0,
        "no sender dropped a record": dropped == 0,
        "every record came from the collector's uid": uid is not None and foreign == 0,
        "its first record is the canary's connect to the saved endpoint": (
            opened.get("op") == "connect" and opened.get("address") == address and opened.get("port") == port
        ),
        "the canary connect succeeded in the cell": _rc(canary) == 0 and _str(canary, "result") == "open",
        "the collector was ready": _int(collector, "ready_lines") >= 1,
        "the collector exited 0": _rc(collector) == 0,
    }
    return _checked(
        checks,
        records=len(records),
        malformed=malformed,
        dropped=dropped,
        foreign=foreign,
        destinations=_destinations(recs),
        canary={"port": port, "loopback": ipaddress.ip_address(address).is_loopback},
    )


def _run_identity(d):
    """run.json, written by the run's first step in an empty directory; the job is checked by every step against the
    allocation it runs in, and never printed."""
    run = d["run"]
    run_id, job, started = run.get("run_id"), run.get("job"), run.get("started")
    checks = {
        "its id is a nonce": isinstance(run_id, str) and len(run_id) == 32 and all(c in HEX for c in run_id),
        "it names its allocation": isinstance(job, str) and bool(job),
        "it says when it started": isinstance(started, str) and bool(started),
    }
    return _checked(checks, run_id=run_id if checks["its id is a nonce"] else None, started=started)


def _http_calls(d):
    """The helpers' call logs: each call's method, path, status and body size and SHA-256. Raw evidence of the round
    trip that no check reads further; a log that is missing, or a line in another shape, fails the run."""
    logs = d["round"]
    calls = [row for rows in logs for row in rows] + list(d["online"])
    for row in calls:
        _str(row, "method"), _str(row, "path"), _int(row, "status"), _int(row, "body_bytes"), _hex64(row, "body_sha256")
    return _checked({"the zone's log and one per provider": len(logs) >= HTTP_LOGS}, logs=len(logs), calls=len(calls))


def _listeners(d):
    total, ours = _int(d["node"], "tcp_listeners_total"), _int(d["node"], "tcp_listeners_ours")
    return verdict(total >= 0 and ours >= 0, tcp_listeners_total=total, tcp_listeners_ours=ours)


def _teardown(d):
    stopped, ended = _obj(d["teardown"], "counts"), _obj(d["end"], "counts")
    checks = {
        "teardown.sh's exit status": _int(d["teardown"], "rc") == 0,
        "the service stopped": _int(stopped, "service_stopped") >= 1,
        "the work directory was removed": _int(stopped, "workdir_removed") >= 1,
        "no error in the teardown": _int(stopped, "errors") == 0,
        "the service ended at the stop requested": _int(ended, "stopped_at_request") == 1,
        "it did not end by itself": _int(ended, "exited_by_itself") == 0,
        "the service step's exit status": _rc(d["step"]) == 0,
    }
    return _checked(checks, step_rc=d["step"]["rc"])


def _steps_left(d):
    names = [str(line).strip() for line in _list(d["steps"], "lines") if str(line).strip()]
    left = [n for n in names if n not in OTHER_STEPS]
    return _checked({"the listing succeeded": _rc(d["steps"]) == 0, "no step is left": not left}, steps_left=len(left))


def _nodes_clean(d):
    roles, dirty, failing = {}, [], []
    for doc in d["nodes"]:
        role = _str(doc, "role")
        roles[role] = doc
        dirty.extend([role, key] for key in NODE_COUNTS if _int(doc, key) != 0)
        failing.extend([role, key] for key in NODE_COMMANDS if _int(doc, key) != 0)
    missing = [role for role in NODES if role not in roles]
    checks = {"every node was checked": not missing, "its listings ran": not failing, "no leftover": not dirty}
    return _checked(checks, missing=missing, dirty=dirty, commands_failed=failing)


def _scancel(d):
    rc = _rc(d["rc"])
    return _checked({"scancel's exit status": rc == 0}, rc=rc)


def _squeue(d):
    rc = _rc(d["squeue"])
    listed = []
    for line in _list(d["squeue"], "lines"):
        if not isinstance(line, str):
            raise ValueError("a squeue line is not a string")
        if line.strip():
            fields = line.split(None, 1)
            if len(fields) != 2:
                raise ValueError("a squeue line is not '<id> <name>'")
            listed.append(fields)
    ours = sum(1 for _, name in listed if name.startswith(OURS))
    checks = {"squeue succeeded": rc == 0, "no job of ours is left": ours == 0}
    return _checked(checks, jobs_listed=len(listed), ours=ours)


def catalog():
    """Every item of the summary: (name, kind, slots, parser). The summary builds all of them, every time."""
    items = [("run_identity", "check", (("run", "run.json"),), _run_identity)]
    for role in SERVICES:
        items.append((role + "_up", "check", (("up", role + "/up.json"),), _service_up))
        items.append((role + "_couchbase_guard", "check", (("lines", role + "/cb-guard-*.json"),), _guard))
    for role in PROVIDERS:
        items.append((role + "_registration", "check", (("reg", role + "/registration.json"),), _registration))
    online = (("out", "providers/online.out.json"), ("rc", "providers/online.rc.json"))
    items.append(("providers_online", "check", online, _providers_online))
    items.append(("space_prepared", "check", (("prep", "roundtrip/prepare.json"),), _space_prepared))
    write = ("write", "roundtrip/write.out.json")
    for role in PROVIDERS:
        slots = (("out", "roundtrip/support-%s.out.json" % role), ("rc", "roundtrip/support-%s.rc.json" % role), write)
        items.append(("support_" + role, "check", slots, _support))
    items.append(("write_P1", "check", (("out", write[1]), ("rc", "roundtrip/write.rc.json")), _write))
    for role in READERS:
        slots = (("out", "roundtrip/read-%s.out.json" % role), ("rc", "roundtrip/read-%s.rc.json" % role), write)
        items.append(("read_" + role, "check", slots, _read))
    agent = ("agent", "sandbox/agent.json")
    items.append(
        ("sandbox_step", "check", (("step", "sandbox/step.rc.json"), ("agent", "sandbox/agent.rc.json")), _sandbox_step)
    )
    items.append(("gate_read", "check", (agent, write), _gate_read))
    items.append(("gate_listing", "check", (agent,), _gate_listing))
    items.append(("gate_lookup", "check", (agent, write), _gate_lookup))
    items.append(("gate_write_refused", "check", (agent,), _gate_write_refused))
    items.append(("gate_strips_client_token", "check", (agent,), _gate_strips))
    items.append(("direct_no_token", "check", (agent,), _direct))
    items.append(("reach_matrix", "check", (agent,), _reach))
    items.append(("fact_internet_443", "fact", (agent,), _internet))
    items.append(("cell_clean", "check", (agent,), _cell_clean))
    gate = (("rows", "sandbox/gate-requests.jsonl"), ("end", "sandbox/gate-end.json"))
    items.append(("gate_log", "check", gate, _gate_log))
    scanned = tuple(("file:" + name, RAW + rel) for name, rel in SCANNED_FILES.items())
    items.append(("gate_token_scan", "check", (("scans", "sandbox/token-scan-*.json"), *scanned), _token_scan))
    egress = (
        ("rows", "sandbox/egress.jsonl"),
        ("collector", "sandbox/egress-collector.json"),
        ("canary", "sandbox/canary.json"),
    )
    items.append(("egress", "check", egress, _egress))
    items.append(("fact_sandbox_listeners", "fact", (("node", "sandbox/node.json"),), _listeners))
    calls = (("round", "roundtrip/http-*.jsonl"), ("online", "providers/http-zone.jsonl"))
    items.append(("fact_http_calls", "fact", calls, _http_calls))
    for role in SERVICES:
        slots = (
            ("teardown", role + "/teardown.json"),
            ("end", role + "/service-end.json"),
            ("step", role + "/service-step.rc.json"),
        )
        items.append((role + "_teardown", "check", slots, _teardown))
    items.append(("steps_left", "check", (("steps", "teardown/steps-left.json"),), _steps_left))
    items.append(("nodes_clean", "check", (("nodes", "teardown/nodecheck-*.json"),), _nodes_clean))
    items.append(("release_scancel", "check", (("rc", "release/scancel.rc.json"),), _scancel))
    items.append(("release_squeue", "check", (("squeue", "release/squeue-final.json"),), _squeue))
    return items


def summary(evidence_dir):
    """The run's summary: every catalog item built from its files, and PASS only if every one passed."""
    base = os.fspath(evidence_dir)
    items = [build_item(base, name, kind, slots, parser) for name, kind, slots, parser in catalog()]
    names = [item.name for item in items]
    if not items or len(set(names)) != len(names):
        raise ValueError("the catalog is empty or names an item twice")
    failed = [item.name for item in items if not item.passed]
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not failed else "FAIL",
        "items": [item.as_dict() for item in items],
        "failed": failed,
    }


def main(argv):
    if len(argv) != 1:
        print("usage: python3 -I evidence.py <evidence dir>", file=sys.stderr)
        return 2
    result = summary(argv[0])
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
