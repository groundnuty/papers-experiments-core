# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, day0/REPORT.md:1-10,540-556
"""The dated day-0 report, as JSON and as Markdown.

The report states facts and where something could not be measured it says **unknown** and why, as the earlier study's
day-0 report did; unknown is listed in a table of its own and is never counted as a pass. Both files are written new
into a directory the caller names and never over an earlier report.

Nothing identifying a deployment is written. Every string is scrubbed of the configured URLs, hosts, identifiers and
the space's name before it is written, and of any token the process has registered as a secret; providers are named by
the label platform.toml gives them.
"""

import hashlib
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.records.redact import redact_structure
from expcore_onedata.config import PlatformConfig

MARK = "<withheld>"
MIN_SENSITIVE_LEN = 4
SCHEMA = 1


@dataclass(frozen=True)
class Entry:
    """One check's result as the report holds it."""

    name: str
    title: str
    mutates: bool
    ok: bool | None
    summary: str
    facts: Mapping[str, Any]
    seconds: float


def status(ok: bool | None) -> str:
    """Name a result: `pass`, `fail` or `unknown`."""
    return "unknown" if ok is None else ("pass" if ok else "fail")


def sensitive_strings(config: PlatformConfig) -> list[str]:
    """Return every string of the configuration that identifies the deployment, longest first."""
    found = {config.zone.url, config.space.name, config.space.id}
    for provider in config.providers.values():
        found.update({provider.url, provider.host, provider.id or ""})
    found.add(config.zone.url.split("://")[-1])
    return sorted((s for s in found if len(s) >= MIN_SENSITIVE_LEN), key=len, reverse=True)


def scrub(value: Any, sensitive: Iterable[str]) -> Any:
    """Return the value with every sensitive string replaced, and registered secrets redacted."""
    needles = list(sensitive)

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            for needle in needles:
                item = item.replace(needle, MARK)
            return item
        if isinstance(item, Mapping):
            return {str(walk(str(k))): walk(v) for k, v in item.items()}
        if isinstance(item, list | tuple):
            return [walk(v) for v in item]
        return item

    cleaned, _ = redact_structure(walk(value), environ={})
    return cleaned


def build_report(
    entries: list[Entry], config: PlatformConfig, *, utc: str, mutation_allowed: bool, exit_code: int
) -> dict[str, Any]:
    """Assemble the report: what ran, the counts, and each result, scrubbed of the deployment's identifiers."""
    body = {
        "schema": SCHEMA,
        "utc": utc,
        "platform_file_sha256": hashlib.sha256(config.file.read_bytes()).hexdigest(),
        "providers": sorted(config.providers),
        "mutation_allowed": mutation_allowed,
        "counts": {s: sum(1 for e in entries if status(e.ok) == s) for s in ("pass", "fail", "unknown")},
        "exit_code": exit_code,
        "checks": [
            {
                "name": e.name,
                "title": e.title,
                "mutates": e.mutates,
                "status": status(e.ok),
                "summary": e.summary,
                "seconds": e.seconds,
                "facts": dict(e.facts),
            }
            for e in entries
        ],
    }
    return scrub(body, sensitive_strings(config))


def _facts(facts: Mapping[str, Any], indent: int = 0) -> list[str]:
    """Render facts as a nested bullet list."""
    pad = "  " * indent
    lines = []
    for key, value in facts.items():
        if isinstance(value, Mapping):
            lines.append(f"{pad}- {key}:")
            lines.extend(_facts(value, indent + 1))
        else:
            lines.append(f"{pad}- {key}: {json.dumps(value)}")
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the report for a person: a table, the unknowns with what they wait on, then each check's facts."""
    counts = report["counts"]
    lines = [
        f"# Day-0 report {report['utc']}",
        "",
        f"{counts['pass']} passed, {counts['fail']} failed, {counts['unknown']} unknown. "
        "Unknown is not a pass: it means the fact could not be measured.",
        f"Providers (by their label in platform.toml): {', '.join(report['providers'])}. "
        f"Mutation allowed: {'yes' if report['mutation_allowed'] else 'no'}. "
        f"Platform file SHA-256: {report['platform_file_sha256']}.",
        "",
        "| check | result | summary |",
        "|---|---|---|",
    ]
    for check in report["checks"]:
        lines.append(f"| {check['name']} | {check['status']} | {check['summary']} |")
    unknown = [c for c in report["checks"] if c["status"] == "unknown"]
    if unknown:
        lines += ["", "## Blocked or unknown", "", "| check | waits on |", "|---|---|"]
        lines += [f"| {c['name']} | {c['summary']} |" for c in unknown]
    for check in report["checks"]:
        lines += ["", f"## {check['title']} ({check['status']})", "", check["summary"], ""]
        lines += _facts(check["facts"])
    return "\n".join(lines) + "\n"


def stamp(now: float | None = None) -> str:
    """Return a UTC time stamp fit for a file name and a heading."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def write_report(out_dir: Path, report: Mapping[str, Any]) -> tuple[Path, Path]:
    """Write the JSON and the Markdown into a new pair of files named by the report's time; never overwrite one."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"day0-{report['utc']}"
    json_path, md_path = out_dir / f"{base}.json", out_dir / f"{base}.md"
    with open(json_path, "x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    with open(md_path, "x", encoding="utf-8") as handle:
        handle.write(render_markdown(report))
    return json_path, md_path
