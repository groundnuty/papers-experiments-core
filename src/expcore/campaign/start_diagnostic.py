# Provenance: new
"""What a failed instrument start leaves on the attempt: a diagnostic snapshot, redacted, in the set-aside record (C40).

An instrument whose `start` verifies something (a state inducer whose start-state check fails) raises an `InfraError`
and the attempt is set aside and run again. What was checked and what was seen lived only on the exception, so the
record of the set-aside attempt held a kind and a detail and no evidence of why. The instrument can now give the error a
`diagnostic` (a JSON object), `start_instruments` names the instrument and the phase on the error it lets through
(`InfraError.origin`), and the attempt keeps the **snapshot**:

    {"phase": "start", "instrument": "<namespace>", "diagnostic": {...}}

`diagnostic` is left out when the instrument gave none, so a failed start of any instrument is still counted by
instrument; an infrastructure error raised anywhere else that carries a diagnostic keeps `{"diagnostic": {...}}`, with
no phase or instrument. An error with neither has no snapshot, and the attempt is what it always was.

The snapshot goes through the attempt's redactor, as an instrument's snapshot goes through the record's (R12): every
string, a key among them, loses the secrets of the environment, the models and the registry, and those the executor
declared (`RunRequest.declare_secrets`: an executor starts its instruments before `record_run`, so a secret it names
only to `record_run` is not known when a start fails, and it declares it first, or declares none to say there is
nothing more). **Never a diagnostic redacted against a partial list of the run's secrets**: until the executor has
declared (or `record_attempt` has given the attempt's whole context) a diagnostic is withheld: the snapshot keeps the
phase and the instrument and is `{"ok": false, "error": "Withheld", "why": ...}`. It is kept on the
attempt's row (`attempts.diagnostic`, canonical JSON), in its outcome marker (so recovery commits it after a kill) and
in the export (`attempt_diagnostics`, only when an attempt kept one). A snapshot that cannot be kept (not a JSON
object, a number JSON cannot hold, more than `LIMIT_BYTES`, a redaction that raised) is `{"ok": false, "error":
<class>}` with the phase and the instrument when known, as an instrument whose `finish` failed is (O08): it is never
dropped, never guessed, and never an exception, since one that escaped the attempt runner would stop dispatch (C17).
"""

import json
from collections.abc import Callable, Mapping
from typing import Any

from expcore.errors import InfraError
from expcore.records.redact import Redactor

EXPORT_KEY = "attempt_diagnostics"
LIMIT_BYTES = 16 * 1024  # the canonical JSON of a snapshot, before redaction
WITHHELD = (
    "the executor did not tell the core its secrets (RunRequest.declare_secrets) before it started its instruments, so "
    "the diagnostic cannot be shown redacted with all of them; declare them, or declare none, and it is kept"
)


class DiagnosticTooLarge(ValueError):
    """A diagnostic is more than `LIMIT_BYTES` of canonical JSON."""


class DiagnosticUnreadable(ValueError):
    """A stored diagnostic is not the JSON object the core writes; the message names the attempt."""


def snapshot_of(
    error: BaseException, redactor: Callable[[], Redactor], *, complete: bool = True
) -> dict[str, Any] | None:
    """The snapshot an attempt keeps of an error, redacted with `redactor()`; None when the error has nothing to keep
    (it is no infrastructure error, or it names no instrument and carries no diagnostic). Never raises.

    `complete` says the redaction context is known to be whole: the executor declared its secrets, or `record_attempt`
    gave the attempt's whole context. Never a diagnostic redacted against a partial list of the run's secrets: without
    it, a diagnostic is withheld (`{"ok": false, "error": "Withheld", "why": WITHHELD}`, with the phase and the
    instrument), since a secret that only the executor knows would be kept on the row, in the marker and the export.
    A failed start that gave no diagnostic has nothing to redact and is kept either way."""
    given, origin = getattr(error, "diagnostic", None), getattr(error, "origin", None)  # a subclass may skip the base
    if not isinstance(error, InfraError) or (given is None and origin is None):
        return None
    try:
        if given is not None and not complete:
            return {**_head(origin, redactor), "ok": False, "error": "Withheld", "why": WITHHELD}
        if given is not None and not isinstance(given, Mapping):
            raise TypeError("a diagnostic is a JSON object")
        text = json.dumps(
            dict(origin or {}) if given is None else {**dict(origin or {}), "diagnostic": given},
            sort_keys=True,
            allow_nan=False,
        )
        if len(text.encode("utf-8")) > LIMIT_BYTES:
            raise DiagnosticTooLarge(f"a diagnostic is at most {LIMIT_BYTES} bytes")
        redacted, _ = redactor().structure(json.loads(text))
        return dict(redacted)
    except Exception as problem:  # whatever is wrong with it, the attempt keeps its result and says the snapshot failed
        return {**_head(origin, redactor), "ok": False, "error": type(problem).__name__}


def _head(origin: Any, redactor: Callable[[], Redactor]) -> dict[str, Any]:
    """The phase and the instrument a snapshot that holds no evidence keeps, redacted like the rest (an instrument's
    name can be any word, a secret's value among them); empty when they cannot be redacted: none is kept unredacted."""
    try:
        redacted, _ = redactor().structure(dict(origin or {}))
        return dict(redacted)
    except Exception:
        return {}


def exported(store: Any, conditions: Mapping[str, str], models: Mapping[str, str]) -> list[dict[str, Any]] | None:
    """The export's `attempt_diagnostics`: every attempt that kept a snapshot, in attempt order, with its run, its
    condition and model (`conditions` and `models` by run key), its number, state, infrastructure kind and whether it
    was charged. None when none kept one, so a campaign without any exports what it did before; a snapshot made before
    the column existed has none. Raises `DiagnosticUnreadable` for a stored snapshot that is not a JSON object."""
    columns = {row["name"] for row in store.q("PRAGMA table_info(attempts)")}
    if "diagnostic" not in columns:
        return None
    rows = store.q(
        "SELECT id, run_key, attempt, state, infra_kind, charged, diagnostic FROM attempts "
        "WHERE diagnostic IS NOT NULL ORDER BY id"
    )
    entries = []
    for row in rows:
        try:
            snapshot = json.loads(row["diagnostic"])
        except ValueError:
            snapshot = None
        if not isinstance(snapshot, dict):
            raise DiagnosticUnreadable(f"the store's attempt {row['id']} has a diagnostic that is not a JSON object")
        entries.append(
            {
                "attempt_id": row["id"],
                "run_key": row["run_key"],
                "condition": conditions[row["run_key"]],
                "model": models[row["run_key"]],
                "attempt": row["attempt"],
                "state": row["state"],
                "infra_kind": row["infra_kind"],
                "charged": bool(row["charged"]),
                "diagnostic": snapshot,
            }
        )
    return entries or None
