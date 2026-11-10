# Provenance: new
"""The local log of harness defects: the traceback a record, a marker and the store never hold (L08).

A crash in the study's executor that is not an `InfraError` is a harness defect, and the attempt keeps only the
exception's class and a line-free stack hash: the exception's text may carry a secret, and a record, a marker and the
store are shared. That is too little to find the bug, so the traceback goes to one local file instead,
`<campaign root>/logs/harness-errors.log` (`out/logs/` of a study), one entry per defect, headed by the time, the run
key, the attempt number, the class and the stack hash that the marker and the store hold, so an entry is found from
either.

- **Redacted as a record is.** The text passes through the redactor a record of the attempt would get
  (`records.build.redactor_for`: the environment's secrets, the keys of every model in the table, the registered
  secrets), by value, in every form `records.redact` knows. A redactor that cannot be built (a secret too short to
  redact) withholds the traceback: the entry says why, naming the variable, never a value.
- **Never shared.** The directory holds a `.gitignore` that ignores everything in it, so a study whose own
  `.gitignore` misses `out/` still does not commit it, and a replication package never selects it, whatever its
  patterns (`package.spec.LOCAL_ONLY`). It is outside `out/runs`, so the replay check neither hashes nor replays it.
- Entries are appended under a lock, since the campaign's workers are threads.
"""

import threading
import traceback
from collections.abc import Callable
from pathlib import Path

from expcore.config.loading import ConfigError
from expcore.loop.outcomes import HarnessDefect
from expcore.records.redact import Redactor

LOG_DIR = "logs"
HARNESS_LOG = f"{LOG_DIR}/harness-errors.log"
IGNORE_ALL = "# written by expcore: local logs, never committed or packaged\n*\n"

_lock = threading.Lock()


def log_path(root: Path) -> Path:
    """The harness-error log of the campaign whose root (a study's `out/`) is `root`."""
    return Path(root) / HARNESS_LOG


def write_defect(
    root: Path,
    error: BaseException,
    defect: HarnessDefect,
    *,
    stamp: str,
    run_key: str,
    attempt: int,
    redactor: Callable[[], Redactor],
) -> Path:
    """Append the redacted traceback of a harness defect to the campaign's log; return the log's path.

    `redactor` builds the redactor when it is needed; a ConfigError from it withholds the traceback, with its reason.
    """
    try:
        text = redactor().text("".join(traceback.format_exception(error)))
    except ConfigError as refusal:
        text = f"the traceback is withheld, since it cannot be redacted: {refusal}\n"
    head = f"=== {stamp} {run_key} attempt {attempt}: {defect.exception}, stack {defect.stack_sha256}\n"
    path = log_path(root)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        ignore = path.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text(IGNORE_ALL, encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(head + text + ("" if text.endswith("\n") else "\n"))
    return path
