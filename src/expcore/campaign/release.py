# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/release.py, blob e4ff196acefcdef558343611ba476ab2396f5019
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Release files: someone's word that a stop may end (C13).

The word is used once. A file left in place would silently end the next stop as well, and one written before the stop
says nothing about it, because nobody had seen the stop yet. So a release counts only if its modification time is
**strictly newer** than the stop it ends, with no allowance for coarse file clocks: a file that cannot be told from the
stop's own moment is not shown to have been written after it, and the person writes it again. A release is removed when
it ends a stop and is recorded by who wrote it and when. A file that cannot be removed releases nothing: the stop holds
and the alarm stays.

Two forms. `take` is a file whose existence is the word, for one stop. `take_listed` is a file that lists names, one
per line, each ending that name's stop; a name with no stop in force is left alone, and the file goes with its last
line. `written_after` only looks: it serves a standing decision that removing the file would hold again.
"""

import datetime as dt
import logging
import os
import pwd
from pathlib import Path

log = logging.getLogger(__name__)


def _signed(st: os.stat_result) -> str:
    """Return who owns a file and when it was written, for the record of a release."""
    try:
        who = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        who = f"uid {st.st_uid}"
    return f"{who} at {dt.datetime.fromtimestamp(st.st_mtime, dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def epoch_of(utc: object) -> float:
    """Return a stored time as epoch seconds; one that cannot be read lets any release count (the stop is not in doubt)."""
    try:
        return dt.datetime.fromisoformat(str(utc).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def written_after(path: str | Path, since: float) -> str | None:
    """Return who wrote the file and when, if it exists and was written strictly after `since`; it stays where it is."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return _signed(st) if st.st_mtime > since else None


def take(path: str | Path, stopped_at: float) -> str | None:
    """Use up a file that ends one stop: its existence is the word.

    Return who and when (with its first line, if it says why) and remove it; None when there is no such file, it is
    not newer than the stop, or it cannot be removed.
    """
    p = Path(path)
    try:
        st = p.stat()
        if st.st_mtime <= stopped_at:
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
    """Use up the lines of a file that names the stops it ends, one name per line.

    `stops` maps each name with a stop in force to the epoch time of that stop. Return {name: who and when} for the names
    the file lists and that were stopped strictly before the file was written, and remove those lines, keeping the file's time
    for the rest. Nothing is released if the file cannot be rewritten.
    """
    p = Path(path)
    if not stops:
        return {}
    try:
        st = p.stat()
        lines = [x.strip() for x in p.read_text(errors="replace").splitlines() if x.strip()]
        taken = {name for name, t in stops.items() if name in lines and st.st_mtime > t}
        if not taken:
            return {}
        left = [x for x in lines if x not in taken]
        if left:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text("\n".join(left) + "\n")
            os.utime(tmp, (st.st_atime, st.st_mtime))  # a name still listed stays as old as it was
            os.replace(tmp, p)
        else:
            p.unlink()
    except FileNotFoundError:
        return {}
    except OSError:
        log.exception("could not use up the release file %s: the stops hold", p)
        return {}
    said = _signed(st)
    return dict.fromkeys(taken, said)
