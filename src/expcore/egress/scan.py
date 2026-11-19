# Provenance: new
"""The static scan of an agent's code cells for URLs and download or network calls: the second line of detection.

The shim that logs connections (`expcore.egress.instrument`) cannot see a statically linked program, raw system calls
or io_uring, so the code the agent ran is scanned too, from the record alone. A code cell is the `code` argument of a
call to a tool whose input schema has a string `code` property (the platform's code tool, whatever the study named
it); every such call the record holds is scanned, run or not. The scan finds:

- `url`: a URL of a network scheme (http, https, ftp, ws, git, ssh, s3, ...) whose host is not the platform's (a name
  the run declared, a gate address or a loopback address), recorded by its host only, never its path;
- `download`: `curl`, `wget`, `pip install` (also `python -m pip install` and the argument-list form), `git clone`,
  `conda`/`mamba install`;
- `network_call`: the use of a network module (`requests`, `urllib`, `http.client`, `httpx`, `aiohttp`, `socket`,
  `ftplib`, `smtplib`, `paramiko`, `pycurl`), by import or by a call.

Each hit names its cell (the index among the record's code cells) and what it found. A `url` or `download` hit always
flags the run; a `network_call` hit flags it unless the run declared that its data path is the agent's own network
calls to the gate (`Declaration.network_calls`, a REST data path), when it is kept as evidence without a flag. The scan
reads text: code that builds a name or a call at run time, or decodes it, passes it, and that is why it is the second
line and not the first. Neither line prevents egress.
"""

import re
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from expcore.egress.declare import Declaration
from expcore.egress.package_index import PACKAGE_INDEX, PIP_INSTALL, names_setting, plain_installs

SCAN_VERSION = 1
MAX_HITS = 200  # per run; `hits_cut` says when more were found
URL, DOWNLOAD, NETWORK_CALL = "url", "download", "network_call"
_WEB = ("http://", "https://")  # the schemes a URL on a declared package index may have
_TAIL = re.compile(r"\S*")  # what may follow a URL's match up to the next space
_AUTHORITY = re.compile(r"[^/?#]*")  # a URL's authority, after its scheme

_SCHEMES = "https?|ftps?|wss?|git|ssh|sftp|rsync|s3|gs"
_URL = re.compile(rf"\b(?:{_SCHEMES})://[^\s'\"<>`\\)\]}}]+", re.IGNORECASE)
_DOWNLOADS = {
    "curl": re.compile(r"\bcurl\b"),
    "wget": re.compile(r"\bwget\b"),
    "pip install": PIP_INSTALL,
    "git clone": re.compile(r"\bgit\b[\s'\",]*\bclone\b"),
    "conda install": re.compile(r"\b(?:conda|mamba|micromamba)\b[\s'\",]*\binstall\b"),
}
_MODULES = ("requests", "urllib", "urllib3", "http.client", "httpx", "aiohttp", "socket", "ftplib", "smtplib")
_MODULES += ("paramiko", "pycurl")
_NETWORK = {
    name: re.compile(
        rf"(?:^|\n)\s*(?:import|from)\s+(?:[\w.]+\s*,\s*)*{re.escape(name)}\b|\b{re.escape(name)}\.\w+\s*\(",
    )
    for name in _MODULES
}


def code_cells(record: Any) -> list[str]:
    """The code the agent's code tool was given, in the order of the record's tool calls."""
    names = set()
    for tool in record.tools:
        properties = tool.get("input_schema", {}).get("properties", {})
        code = properties.get("code") if isinstance(properties, Mapping) else None
        if isinstance(code, Mapping) and code.get("type") == "string":
            names.add(tool.get("name"))
    cells = []
    for call in record.tool_calls:
        code = call.get("arguments", {}).get("code") if call.get("name") in names else None
        if isinstance(code, str):
            cells.append(code)
    return cells


def _host(url: str) -> str | None:
    try:
        return urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None


def _on_index(match: re.Match[str], code: str) -> bool:
    """Whether a URL may be a declared index's: http(s), and no user part in its authority, read on past the match
    to the next space (`https://pypi.org)@elsewhere/` and `https://pypi.org'@elsewhere/` are elsewhere's)."""
    whole = match.group() + _TAIL.match(code, match.end()).group()
    authority = _AUTHORITY.match(whole.split("://", 1)[1]).group()
    return match.group().lower().startswith(_WEB) and "@" not in authority


def _hits_of(
    index: int, code: str, declared: Declaration, indexes: tuple[str, ...], installs: bool
) -> list[dict[str, Any]]:
    """Every hit in one cell, in a fixed order: URLs by host, then downloads, then network modules. Under declared
    package indexes, a URL on one of their hosts and, when `installs`, a cell's plain `pip install` are
    `package_index` hits that do not flag (`expcore.egress.package_index`)."""
    hits = []
    matches = list(_URL.finditer(code))
    hosts = [_host(m.group()) or "?" for m in matches]
    urls = [(h, bool(indexes) and _on_index(m, code)) for m, h in zip(matches, hosts, strict=True)]
    for host in dict.fromkeys(host for host, _ in urls):
        if host in indexes and all(web for other, web in urls if other == host):  # http(s) only
            hits.append({"cell": index, "kind": PACKAGE_INDEX, "what": host, "flag": False})
        elif not declared.is_platform_host(host):
            hits.append({"cell": index, "kind": URL, "what": host, "flag": True})
    for name, pattern in _DOWNLOADS.items():
        if not pattern.search(code):
            continue
        if installs and name == "pip install" and plain_installs(code):
            hits.append({"cell": index, "kind": PACKAGE_INDEX, "what": name, "flag": False})
        else:
            hits.append({"cell": index, "kind": DOWNLOAD, "what": name, "flag": True})
    calls = [n for n, p in _NETWORK.items() if p.search(code)]
    hits += [{"cell": index, "kind": NETWORK_CALL, "what": n, "flag": not declared.network_calls} for n in calls]
    return hits


def scan_cells(cells: Sequence[str], declared: Declaration, *, package_indexes: Sequence[str] = ()) -> dict[str, Any]:
    """The scan of a run's code cells under its declaration: its hits, and whether any flags the run. Under declared
    package indexes it also says which, and how many `package_index` hits it found (the cut list may show fewer)."""
    indexes = tuple(package_indexes)
    installs = bool(indexes) and not any(names_setting(code) for code in cells)  # a setting holds across cells
    found = [hit for index, code in enumerate(cells) for hit in _hits_of(index, code, declared, indexes, installs)]
    scanned = {
        "version": SCAN_VERSION,
        "cells": len(cells),
        "hits": found[:MAX_HITS],
        "hits_cut": len(found) > MAX_HITS,
        "flagged": any(hit["flag"] for hit in found),
    }
    if indexes:
        scanned |= {"package_indexes": list(indexes), PACKAGE_INDEX: sum(h["kind"] == PACKAGE_INDEX for h in found)}
    return scanned
