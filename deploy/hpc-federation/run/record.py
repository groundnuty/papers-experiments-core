"""record.py: how the run's steps write their raw evidence (python3 -I, stdlib only, Python 3.8).

Every file is written 0600 into a 0700 directory, atomically, and holds one JSON object (or, for `raw`, the bytes it
was given). run/evidence.py later derives the run's summary from these files and from nothing else, so a step records
what it saw here and never a verdict of its own.

  record.py rc OUT NAME RC                    {"name": NAME, "rc": RC}: a command's exit status
  record.py lines OUT NAME RC                 stdin's lines: {"name", "rc", "lines": [...]}
  record.py logscan OUT LOG [--rc N] K=REGEX...   counts of the lines of LOG that match each regex, with the log's
                                              line count, size and SHA-256: {"log_lines", "log_bytes", "log_sha256",
                                              "counts": {K: n}, "rc"?}
  record.py guard-lines LOG DIR               one file per `hpcop-evidence {...}` line of LOG: DIR/cb-guard-NNN.json
                                              (a line that is not one JSON object is kept as its raw text, which the
                                              summary then fails as unparsable)
  record.py scan OUT NAME PATTERN FILE GREP_RC COUNT   one token scan: what the pattern file held (sizes only, never
                                              its content), the file's size, grep's exit status and its count
  record.py canary OUT ADDRESS PORT RC FILE   the egress canary: its endpoint, the cell's exit status and
                                              the connect result the cell printed (FILE's one JSON line; "unparsable"
                                              when it is not): {"address", "port", "rc", "result"}
  record.py kv OUT K=V...                     one object of integers and strings: K=int:5 K=str:text
  record.py raw OUT                           stdin, verbatim
  record.py nonce                             a fresh random hex string on stdout
  record.py field FILE KEY                    one string or integer field of a JSON object file, on stdout
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys

GUARD_LINE = re.compile(r"^hpcop-evidence (\{.*\})\s*$")


def write(path, data):
    """0600, atomically, in a directory made 0700 if it is new."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def write_json(path, obj):
    write(path, (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8"))


def integer(text, what):
    if not re.fullmatch(r"-?[0-9]{1,18}", text):
        sys.exit("record.py: %s is not an integer: %r" % (what, text))
    return int(text)


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def logscan(out, log, rest):
    rc, counters = None, []
    while rest:
        arg = rest.pop(0)
        if arg == "--rc":
            rc = integer(rest.pop(0), "--rc")
        else:
            key, sep, regex = arg.partition("=")
            if not sep or not key:
                sys.exit("record.py: logscan wants K=REGEX, got %r" % arg)
            counters.append((key, re.compile(regex)))
    data = read_bytes(log)
    lines = data.decode("utf-8", "replace").splitlines()
    doc = {
        "log_lines": len(lines),
        "log_bytes": len(data),
        "log_sha256": hashlib.sha256(data).hexdigest(),
        "counts": {key: sum(1 for line in lines if rx.search(line)) for key, rx in counters},
    }
    if rc is not None:
        doc["rc"] = rc
    write_json(out, doc)


def guard_lines(log, directory):
    n = 0
    for line in read_bytes(log).decode("utf-8", "replace").splitlines():
        if not line.startswith("hpcop-evidence "):
            continue
        found = GUARD_LINE.match(line)
        target = os.path.join(directory, "cb-guard-%03d.json" % n)
        try:
            doc = json.loads(found.group(1)) if found else None
        except ValueError:
            doc = None
        if isinstance(doc, dict):
            write_json(target, doc)
        else:
            write(target, line.encode("utf-8", "replace") + b"\n")
        n += 1
    print(n)


def scan(out, name, pattern, scanned, grep_rc, count):
    """The pattern file is read for its shape only: no byte of it is printed or kept."""
    text = read_bytes(pattern).decode("utf-8", "replace")
    lines = text.splitlines()
    write_json(
        out,
        {
            "name": name,
            "pattern_bytes": len(text.encode("utf-8")),
            "pattern_lines": len(lines),
            "pattern_blank_lines": sum(1 for line in lines if not line.strip()),
            "scanned_bytes": os.path.getsize(scanned),
            "grep_rc": integer(grep_rc, "grep's exit status"),
            "matches": integer(count, "the count") if re.fullmatch(r"[0-9]{1,18}", count) else None,
        },
    )


def canary(out, address, port, rc, path):
    """The canary's evidence: the endpoint this script asked the cell to dial, and what the cell printed."""
    result = "unparsable"
    try:
        lines = [line for line in read_bytes(path).decode("utf-8", "replace").splitlines() if line.strip()]
        said = json.loads(lines[0]) if len(lines) == 1 else None
        if isinstance(said, dict) and isinstance(said.get("connect"), str):
            result = said["connect"]
    except (OSError, ValueError):
        pass
    write_json(
        out,
        {"address": address, "port": integer(port, "the port"), "rc": integer(rc, "the exit status"), "result": result},
    )


def kv(out, pairs):
    doc = {}
    for pair in pairs:
        key, sep, typed = pair.partition("=")
        kind, sep2, value = typed.partition(":")
        if not sep or not sep2 or kind not in ("int", "str"):
            sys.exit("record.py: kv wants K=int:5 or K=str:text, got %r" % key)
        doc[key] = integer(value, key) if kind == "int" else value
    write_json(out, doc)


def field(path, key):
    doc = json.loads(read_bytes(path).decode("utf-8"))
    value = doc[key]
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        sys.exit("record.py: %s is not a string or an integer" % key)
    print(value)


def main(argv):
    command, args = (argv[0] if argv else ""), argv[1:]
    if command == "rc" and len(args) == 3:
        write_json(args[0], {"name": args[1], "rc": integer(args[2], "the exit status")})
    elif command == "lines" and len(args) == 3:
        rows = [line.rstrip("\n") for line in sys.stdin.read().splitlines()]
        write_json(args[0], {"name": args[1], "rc": integer(args[2], "the exit status"), "lines": rows})
    elif command == "logscan" and len(args) >= 3:
        logscan(args[0], args[1], list(args[2:]))
    elif command == "guard-lines" and len(args) == 2:
        guard_lines(args[0], args[1])
    elif command == "scan" and len(args) == 6:
        scan(*args)
    elif command == "canary" and len(args) == 5:
        canary(*args)
    elif command == "kv" and len(args) >= 2:
        kv(args[0], args[1:])
    elif command == "raw" and len(args) == 1:
        write(args[0], sys.stdin.buffer.read())
    elif command == "nonce" and not args:
        print(secrets.token_hex(16))
    elif command == "field" and len(args) == 2:
        field(args[0], args[1])
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
