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
  patterns (`package.spec.LOCAL_ONLY`). A `.gitignore` the author put there is kept as it is, and gains the line
  `/harness-errors.log` at its end unless that is its last line already: the last matching rule wins, so this ignores
  the log whatever the earlier rules say, without reading them. It is outside `out/runs`, so the replay check neither
  hashes nor replays it.
- Entries are appended under a lock, since the campaign's workers are threads.
"""

import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError
from expcore.loop.outcomes import HarnessDefect
from expcore.records.redact import Redactor

LOG_DIR = "logs"
LOG_NAME = "harness-errors.log"
HARNESS_LOG = f"{LOG_DIR}/{LOG_NAME}"
LOG_RULE = f"/{LOG_NAME}"  # the line that ignores the log in its directory's ignore file
IGNORE_ALL = "# written by expcore: local logs, never committed or packaged\n*\n"

_lock = threading.Lock()


class Chained:
    """Several redactors in one: what is redacted loses every one's secrets, so it is never redacted against the
    shorter list of any of them (the attempt's own context, which `record_attempt` narrows to the environment
    `record_run` was given, and the runner's base: the process, the models, the registry). It is one pass over the
    union of their secrets (`Redactor.union`), longest first, and not one redactor after the other, which would replace
    a shorter secret inside a longer one and leave the rest of the longer one in what is shown (C40)."""

    def __init__(self, *redactors: "Redactor | Chained"):
        ones = [r._one if isinstance(r, Chained) else r for r in redactors]  # a chain may hold a chain
        self._one = ones[0] if len(ones) == 1 else Redactor.union(*ones)

    def with_lines(self) -> "Chained":
        return Chained(self._one.with_lines())

    def text(self, value: str) -> str:
        return self._one.text(value)

    def structure(self, obj: Any) -> tuple[Any, list[str]]:
        return self._one.structure(obj)


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
    redactor: Callable[[], Redactor | Chained],
) -> Path:
    """Append the redacted traceback of a harness defect to the campaign's log; return the log's path.

    `redactor` builds the redactor when it is needed; a ConfigError from it withholds the traceback, with its reason.
    """
    title = f"{run_key} attempt {attempt}: {defect.exception}, stack {defect.stack_sha256}"
    return write_failure(root, error, title=title, stamp=stamp, redactor=redactor)


def write_failure(
    root: Path, error: BaseException, *, title: str, stamp: str, redactor: Callable[[], Redactor | Chained]
) -> Path:
    """Append the redacted traceback of any failure of the harness to the campaign's log, headed by the time and
    `title`; return the log's path. A study's plugin that fails on import is one (`study.plugin`)."""
    try:
        text = redactor().with_lines().text("".join(traceback.format_exception(error)))  # plain text, never sealed
    except ConfigError as refusal:
        text = f"the traceback is withheld, since it cannot be redacted: {refusal}\n"
    return _append(root, f"=== {stamp} {title}\n", text)


def write_note(root: Path, *, title: str, stamp: str, text: str, redactor: Callable[[], Redactor | Chained]) -> Path:
    """Append a note to the campaign's local log, redacted as a traceback is (each line of a multi-line secret too);
    return the log's path. What a diagnostic that is not shown elsewhere keeps (C40): the log is never merged,
    committed or packaged (`package.spec.LOCAL_ONLY`), which the stores, their snapshots and the markers are not."""
    try:
        text = redactor().with_lines().text(text)
    except ConfigError as refusal:
        text = f"the note is withheld, since it cannot be redacted: {refusal}"
    return _append(root, f"=== {stamp} {title}\n", text)


def _append(root: Path, head: str, text: str) -> Path:
    """Append an entry to the campaign's local log; return the log's path."""
    path = log_path(root)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_ignored(path.parent / ".gitignore")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(head + text + ("" if text.endswith("\n") else "\n"))
    return path


def _ensure_ignored(ignore: Path) -> None:
    """Make the log's directory ignore the log: write an ignore file that ignores everything, or end the author's with
    the log's own rule. No earlier rule is read: the last matching rule wins, and this one matches the log only."""
    if not ignore.exists():
        ignore.write_text(IGNORE_ALL, encoding="utf-8")
        return
    text = ignore.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or lines[-1] != LOG_RULE:
        with ignore.open("a", encoding="utf-8") as handle:
            handle.write(("\n" if text and not text.endswith("\n") else "") + f"{LOG_RULE}\n")


class DefectLog:
    """The harness-defect log of one attempt: each entry carries its run key and attempt number.

    The redactor starts as the one the attempt runner can build (the backend's model and the model table);
    `record_attempt` replaces it with the attempt's whole redaction context (`redact_with`), so an entry written after
    that, for a crash the loop caught or one that escaped the executor, is redacted with every secret the record is.

    Writing an entry never raises, since it happens inside the loop and before the result is stored: a log that cannot
    be written must not cost the record or the marker. The first failure is kept, and the attempt raises it with
    `raise_failure` once the result is stored, which stops dispatch loudly (C17).
    """

    def __init__(
        self,
        root: Path,
        run_key: str,
        attempt: int,
        stamp: Callable[[], str],
        redactor: Callable[[], Redactor | Chained],
    ):
        self.root, self.run_key, self.attempt, self._stamp = Path(root), run_key, attempt, stamp
        self._base = redactor  # the runner's own: the process, the models and the registry, whatever is installed after
        self._redactor = redactor
        self.note_unwritten: str | None = None  # the class of what stopped a note being written, if one did (C40)
        self.failure: Exception | None = None  # the first entry that could not be written, if any
        self._where = f"the harness-error log {log_path(self.root)}"

    def redact_with(self, redactor: Callable[[], Redactor]) -> None:
        """Use this redactor for the attempt's entries from now on."""
        self._redactor = redactor

    def build_redactor(self) -> Chained:
        """The redactor of the attempt's redaction context as it stands now: the runner's own base and the one
        installed after it (the executor's `declare_secrets`, or the whole context `record_attempt` gave it), in one
        pass over both lists, so it is never the shorter of the two: `record_attempt` builds its own from the
        environment `record_run` was given. What else of the attempt is kept redacted (the diagnostic of a failed
        instrument start, C40) uses it too, and knows what the log does at that moment."""
        return Chained(self._base(), self._redactor()) if self._redactor is not self._base else Chained(self._base())

    def __call__(self, error: BaseException, defect: HarnessDefect) -> Path | None:
        """Write the redacted traceback of a harness defect of this attempt; return the log's path, or None when the
        entry could not be written (the failure is kept for `raise_failure`)."""
        try:
            return write_defect(
                self.root,
                error,
                defect,
                stamp=self._stamp(),
                run_key=self.run_key,
                attempt=self.attempt,
                redactor=self.build_redactor,
            )
        except Exception as failure:  # kept, and raised by the attempt once its result is stored
            if self.failure is None:
                self.failure = failure
            return None

    def keep_private(self, title: str, text: str) -> bool:
        """Write a note of this attempt to the campaign's local log, redacted as an entry is (`write_note`);
        tell whether it was written. It never raises and never stops dispatch: the note is a courtesy copy of
        what was withheld, so a log that cannot be written is recorded in `note_unwritten` (the exception's
        class, never its text) for the attempt to say in an event, and not as the failure `raise_failure`
        raises, which an entry's is (C17)."""
        try:
            write_note(
                self.root,
                title=f"{self.run_key} attempt {self.attempt}: {title}",
                stamp=self._stamp(),
                text=text,
                redactor=self.build_redactor,
            )
        except Exception as failure:
            self.note_unwritten = type(failure).__name__
            return False
        return True

    def keep(self, failure: Exception) -> None:
        """Keep what an executor's own defect sink raised, to be raised by `raise_failure` once the result is stored."""
        if self.failure is None:
            self.failure, self._where = failure, "the executor's defect sink"

    def raise_failure(self) -> None:
        """Raise LogUnwritable, naming the log or the executor's sink and why, when an entry of this attempt could not
        be written."""
        if self.failure is not None:
            why = f"{type(self.failure).__name__}: {self.failure}"
            raise LogUnwritable(
                f"{self._where} could not be written ({why}); the attempt's result is stored, and dispatch stops until "
                "it can be written"
            ) from self.failure


class LogUnwritable(RuntimeError):
    """The harness-error log could not be written; raised once the attempt's result is stored."""
