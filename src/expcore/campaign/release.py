# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/release.py, blob e4ff196acefcdef558343611ba476ab2396f5019
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Release files: someone's word that a stop may end. The word is used once: a file left in place would silently end the next stop as well (the lead, 2
Nov), and one written before the stop says nothing about it. So a release counts only if written after the stop, is removed when it ends it, and is
recorded by who wrote it and when. If it cannot be removed it releases nothing: the stop holds and the alarm stays.

The model_66 gate's file (gate.py) is the third kind: a standing decision on one latched outcome, which removing would hold again. It is kept, and it too
counts only if written after that outcome (`written_after`): nobody had seen the figure before.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import pwd
from pathlib import Path

log = logging.getLogger(__name__)
SLACK_S = 1.0      # file times come from a coarser clock than time.time(): a file written just after a stop can read a moment before it


def _signed(st: os.stat_result) -> str:
    try:
        who = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        who = f"uid {st.st_uid}"
    return f"{who} at {dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def epoch_of(utc: object) -> float:
    """A stored time; one that cannot be read lets any release count (the stop itself is not in doubt)."""
    try:
        return dt.datetime.fromisoformat(str(utc).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def written_after(path: str | Path, since: float) -> str | None:
    """Who wrote the file and when, if it exists and was written at or after `since`; it stays where it is."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return _signed(st) if st.st_mtime >= since - SLACK_S else None


def take(path: str | Path, stopped_at: float) -> str | None:
    """A file that ends one stop: its existence is the word. Returns who and when (with its first line, if it says why) and removes it, or None."""
    p = Path(path)
    try:
        st = p.stat()
        if st.st_mtime < stopped_at - SLACK_S:
            return None
        said = _signed(st)
        first = next(iter(p.read_text(errors="replace").splitlines()), "").strip()
        p.unlink()
    except FileNotFoundError:
        return None
    except OSError:
        log.exception("could not use up the release file %s: the stop holds", p)
        return None
    return f"{said}: {first[:200]}" if first else said


def take_listed(path: str | Path, stops: dict[str, float]) -> dict[str, str]:
    """A file that lists names, one per line, each ending that name's stop. Returns {name: who and when} for the names stopped before the file was written,
    and removes those lines, keeping the file's time for the rest; the file goes with its last line."""
    p = Path(path)
    if not stops:
        return {}
    try:
        st = p.stat()
        lines = [x.strip() for x in p.read_text(errors="replace").splitlines() if x.strip()]
        taken = {m for m, t in stops.items() if m in lines and st.st_mtime >= t - SLACK_S}
        if not taken:
            return {}
        left = [x for x in lines if x not in taken]
        if left:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text("\n".join(left) + "\n")
            os.utime(tmp, (st.st_atime, st.st_mtime))      # a name still listed stays as old as it was
            os.replace(tmp, p)
        else:
            p.unlink()
    except FileNotFoundError:
        return {}
    except OSError:
        log.exception("could not use up the release file %s: the stops hold", p)
        return {}
    said = _signed(st)
    return {m: said for m in taken}
