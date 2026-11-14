"""scrub.py: find and redact this launch's secrets, plain and base64-encoded, in what the failure path keeps.

The failure path (zone.sbatch, provider.sbatch) deletes the secret files, then keeps the work directory's logs
for inspection. A log can still hold a secret in an encoded form: onepanel's error output put the deploy-time
Couchbase credentials into run.log as an HTTP Basic header, base64(user:password), broken after 76 characters
by couchbase-cli's base64.encodestring and printed as a Python repr, so with a literal backslash-n in it (live
run 3). Matching the plain values only, the scrub kept it.

For every secret value V this matches V itself, and V in base64 at each of the three byte offsets it can have
inside a longer encoded string, so base64(V), base64(user:V) and base64(anything V anything) all match,
whatever the user. Both alphabets (standard and URL-safe) count, and a line break may sit between any two
characters: LF, CRLF, or the escapes \\n and \\r\\n that a repr prints. A base64 match is widened to the whole
run of base64 characters around it, across one escaped line break each way.

  needles SECRETS OUT    write fixed strings for grep -F to OUT (mode 0600): each value, and every 12-character
                         piece of each of its base64 forms. However the form is wrapped, one whole piece of it
                         sits on one line, so `grep -rlF -f OUT` finds every candidate file quickly.
  redact SECRETS FILE... redact every form in each text FILE, in place; remove a binary FILE (one holding a NUL
                         byte) that holds one. One line per file on stdout.
  filter SECRETS         copy stdin to stdout with every form redacted (an excerpt of run.log for the batch log).

SECRETS is a 0600 file with one value per line: no value is ever on a command line. Values shorter than 12
characters are matched in plain text only (the bundle's secrets have 24 or more).
"""

import base64
import os
import re
import sys

MARK = b"[hpcop: redacted]"
PIECE = 12
MIN_ENCODED = 12
B64 = rb"[A-Za-z0-9+/=_-]"
ESCAPED_BREAK = rb"(?:\\r\\n|\\n)"  # the two characters \n (or four, \r\n), as a repr prints them
ANY_BREAK = rb"(?:\r?\n|\\r\\n|\\n)"  # also a real LF or CRLF
URLSAFE = bytes.maketrans(b"+/", b"-_")


def read_values(path):
    with open(path, "rb") as f:
        values = [line.rstrip(b"\r\n") for line in f]
    return [v for v in values if v]


def encoded_forms(value):
    """The base64 characters that carry only bits of value, for each offset (0, 1, 2) and both alphabets."""
    forms = []
    for pad in range(3):
        enc = base64.b64encode(b"\0" * pad + value)
        start = (8 * pad + 5) // 6  # the first character made only of the value's bits
        end = (8 * (pad + len(value))) // 6  # one past the last such character
        for form in (enc[start:end], enc[start:end].translate(URLSAFE)):
            if form not in forms:
                forms.append(form)
    return forms


def pieces(form):
    """Every 12-character piece at a multiple of 12, and the last 12 characters."""
    out = [form[i : i + PIECE] for i in range(0, len(form) - PIECE + 1, PIECE)]
    if form[-PIECE:] not in out:
        out.append(form[-PIECE:])
    return out


def widened(core):
    """core, plus the base64 run around it (across one escaped line break on each side)."""
    return (
        rb"(?:"
        + B64
        + rb"+"
        + ESCAPED_BREAK
        + rb")?"
        + B64
        + rb"*"
        + core
        + B64
        + rb"*(?:"
        + ESCAPED_BREAK
        + B64
        + rb"+)?"
    )


def pattern(values):
    alternatives = []
    for value in values:
        alternatives.append(re.escape(value))
        if len(value) < MIN_ENCODED:
            continue
        for form in encoded_forms(value):
            # The whole form, a line break allowed between any two characters; then any piece of it, so a form
            # cut short (a truncated log line) is caught as well.
            alternatives.append(widened((ANY_BREAK + b"?").join(re.escape(form[i : i + 1]) for i in range(len(form)))))
            alternatives.extend(widened(re.escape(p)) for p in pieces(form))
    return re.compile(b"|".join(alternatives))


def needles(values):
    out = []
    for value in values:
        out.append(value)
        if len(value) >= MIN_ENCODED:
            for form in encoded_forms(value):
                out.extend(p for p in pieces(form) if p not in out)
    return out


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def redact_file(rx, path):
    if os.path.islink(path) or not os.path.isfile(path):
        return "skipped %s: not a regular file" % path
    with open(path, "rb") as f:
        data = f.read()
    new, n = rx.subn(MARK, data)
    if n == 0:
        return None
    if b"\0" in data:
        os.remove(path)
        return "removed %s: a binary file holding a secret value" % path
    tmp = "%s.hpcop-scrub.%d" % (path, os.getpid())
    write_private(tmp, new)
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
    return "redacted %d occurrence(s) of a secret value in %s" % (n, path)


def main(argv):
    if len(argv) == 3 and argv[0] == "needles":
        write_private(argv[2], b"".join(n + b"\n" for n in needles(read_values(argv[1]))))
        return 0
    if len(argv) >= 2 and argv[0] == "redact":
        rx = pattern(read_values(argv[1]))
        for path in argv[2:]:
            line = redact_file(rx, path)
            if line:
                print("hpcop: " + line, flush=True)
        return 0
    if len(argv) == 2 and argv[0] == "filter":
        values = read_values(argv[1])
        data = sys.stdin.buffer.read()
        sys.stdout.buffer.write(pattern(values).sub(MARK, data) if values else data)
        return 0
    sys.stderr.write("usage: scrub.py needles SECRETS OUT | redact SECRETS FILE... | filter SECRETS\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
