"""What comes back to the HPC test runner (README.md): the stream a step prints (its log lines, a boot refusal, and the
results archive between a header with its SHA-256 and an end marker carrying pytest's exit status), what pytest wrote
(its summary line, its `-ra` section, its JUnit XML), and the login node's listing of our allocations, from which a
step's job is picked. Pure functions; `hpctests.py` feeds them."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field

from hpctestlib import FAIL, FRAME_END, FRAME_START, JOB_ID, LOG, SHA256, HpcTestsError, NoAllocation, checked


@dataclass
class Streamed:
    """What a step sent back: its log lines, a boot refusal, and the results archive with pytest's exit status."""

    logs: list[str] = field(default_factory=list)
    failure: str | None = None
    archive: bytes | None = None
    rc: int | None = None


class StreamParser:
    """Reads a step's stdout line by line. `done` turns true at the end marker; `result()` checks the archive's
    SHA-256 and size against its header and raises when they differ."""

    def __init__(self) -> None:
        self.out = Streamed()
        self.done = False
        self._header: tuple[str, int] | None = None
        self._chunks: list[str] = []

    def feed(self, line: str) -> None:
        line = line.rstrip("\n")
        if self.done:
            return
        if self._header is not None:
            if line.startswith(FRAME_END + " "):
                rc = line.split()[1] if len(line.split()) == 2 else ""
                if not rc.isdigit():
                    raise HpcTestsError(f"the end marker carries no exit status: {line!r}")
                self.out.rc = int(rc)
                self.done = True
            else:
                self._chunks.append(line)
        elif line.startswith(FRAME_START + " "):
            words = line.split()
            if len(words) != 3 or not SHA256.fullmatch(words[1]) or not words[2].isdigit():
                raise HpcTestsError(f"a malformed results header: {line!r}")
            self._header = (words[1], int(words[2]))
        elif line.startswith(FAIL + " "):
            self.out.failure = line[len(FAIL) + 1 :]
            self.done = True
        elif line.startswith(LOG + " "):
            self.out.logs.append(line[len(LOG) + 1 :])
        elif line:
            self.out.logs.append(line)

    def result(self) -> Streamed:
        if self._header is not None and self.done:
            try:
                data = base64.b64decode("".join(self._chunks), validate=True)
            except binascii.Error as error:
                raise HpcTestsError(f"the results archive is not base64: {error}") from None
            want, size = self._header
            got = hashlib.sha256(data).hexdigest()
            if got != want or len(data) != size:
                raise HpcTestsError(f"the results arrived as sha256 {got} ({len(data)} bytes), not {want} ({size})")
            self.out.archive = data
        return self.out


SUMMARY = re.compile(
    r"^(?:=+ )?((?:[0-9]+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?|rerun)(?:, )?)+"
    r" in [0-9.]+s(?: \([0-9:]+\))?|no tests ran in [0-9.]+s)(?: =+)?$"
)


def summary_line(log: str) -> str | None:
    """pytest's last summary line ("3 passed, 1 skipped in 2.10s"), without its rule of `=`; None when there is none."""
    found = None
    for line in log.splitlines():
        match = SUMMARY.match(line.strip())
        if match:
            found = match.group(1)
    return found


def short_summary(log: str) -> list[str]:
    """The lines of pytest's `-ra` "short test summary info" section, without its header and the summary line."""
    lines = log.splitlines()
    for index, line in enumerate(lines):
        if "short test summary info" in line and line.startswith("="):
            return [x for x in lines[index + 1 :] if x.strip() and not SUMMARY.match(x.strip())]
    return []


def junit_counts(xml: bytes) -> dict[str, float]:
    """The totals of a pytest JUnit XML file (every <testsuite> summed): tests, failures, errors, skipped, time."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise HpcTestsError(f"the JUnit XML does not parse: {error}") from None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0.0, "failures": 0.0, "errors": 0.0, "skipped": 0.0, "time": 0.0}
    for suite in suites:
        for key in totals:
            try:
                totals[key] += float(suite.get(key, "0"))
            except ValueError:
                raise HpcTestsError(f"the JUnit XML's {key} is not a number") from None
    return totals


@dataclass(frozen=True)
class Job:
    """An allocation of ours, as alloc.sh lists it (times in seconds since the epoch, read on the login node)."""

    id: str
    name: str
    state: str
    start: int | None
    end: int | None
    cpus: int
    mem: str


def parse_listing(text: str) -> tuple[int, list[Job], dict[str, str]]:
    """alloc.sh list's output: `NOW <epoch>`, a `JOB` line per allocation of ours that is alive, and `RENEWER` with
    `key=value` words."""
    now: int | None = None
    jobs: list[Job] = []
    renewer: dict[str, str] = {}
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0] == "NOW" and len(words) == 2 and words[1].isdigit():
            now = int(words[1])
        elif words[0] == "JOB" and len(words) == 8:
            _, job_id, name, state, start, end, cpus, mem = words
            checked(JOB_ID, job_id, "a listed job id")
            jobs.append(
                Job(
                    job_id,
                    name,
                    state,
                    int(start) if start.isdigit() else None,
                    int(end) if end.isdigit() else None,
                    int(cpus) if cpus.isdigit() else 0,
                    mem,
                )
            )
        elif words[0] == "RENEWER":
            renewer = dict(w.split("=", 1) for w in words[1:] if "=" in w)
    if now is None:
        raise HpcTestsError("the login node's listing has no NOW line (is alloc.sh installed? run install)")
    return now, jobs, renewer


def pick_job(jobs: Iterable[Job], now: int, need_s: int, given: str | None = None) -> Job:
    """The RUNNING allocation of ours a step of `need_s` seconds fits in before its end: the given one, else the one
    that ends last. Only jobs alloc.sh listed (its ledger's, under our name) are candidates."""
    running = [j for j in jobs if j.state == "RUNNING" and j.end is not None]
    if given is not None:
        running = [j for j in running if j.id == given]
        if not running:
            raise NoAllocation(f"job {given} is not a RUNNING allocation of ours")
    if not running:
        raise NoAllocation("no RUNNING allocation of ours (hpctests.py jobs lists them; submit makes one)")
    best = max(running, key=lambda j: j.end or 0)
    left = (best.end or 0) - now
    if left < need_s:
        raise NoAllocation(f"job {best.id} ends in {left} s, before a step of {need_s} s could finish")
    return best
