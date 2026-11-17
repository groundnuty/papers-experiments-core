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
declared (`RunRequest.declare_secrets`).

**Never a diagnostic redacted against a partial list of the run's secrets.** An executor starts its instruments before
`record_run`, so a secret it names only to `record_run` is not known when a start fails. Only an attempt whose
executor called `declare_secrets` (a call with no argument says it has none beyond the environment's) has its
diagnostic shown in the row, the marker, the export and the report. For any other the snapshot is a stub, `{"phase":
"start", "ok": false, "error": "Withheld", "why": ...}`: the phase alone, since the instrument's name is the study's
own word and can be a secret only the executor knows (a failed start that gave no diagnostic is `{"phase": "start"}`).
The text of a withheld diagnostic goes to the campaign's local log and nowhere else (`private_text`,
`DefectLog.keep_private`): a store, its snapshot and the markers are merged and packaged, and `out/logs` is the one
place that is not (`package.spec.LOCAL_ONLY`).

What is shown is kept on the attempt's row (`attempts.diagnostic`, canonical JSON), in its outcome marker (so recovery
commits it after a kill) and in the export (`attempt_diagnostics`, only when an attempt kept one). A snapshot that
cannot be kept (not a JSON object, a number JSON cannot hold, more than `LIMIT_BYTES`, a redaction that raised) is
`{"ok": false, "error": <class>}` with the phase and the instrument's name, redacted like the rest (only the phase,
when the redactor cannot be built), as an instrument whose `finish` failed is (O08): it is never dropped, never
guessed, and never an exception, since one that escaped the attempt runner would stop dispatch (C17).
"""

import json
from collections.abc import Callable, Mapping
from typing import Any

from expcore.campaign.defect_log import Chained
from expcore.errors import InfraError
from expcore.records.redact import Redactor

EXPORT_KEY = "attempt_diagnostics"
LIMIT_BYTES = 16 * 1024  # the canonical JSON of a snapshot, before redaction
WITHHELD_ERROR = (
    "Withheld"  # the `error` of a snapshot whose diagnostic is not shown: the stub of an undeclared executor
)
WITHHELD = (
    "withheld: the executor declared no secrets (RunRequest.declare_secrets) before it started its instruments, so the "
    "diagnostic cannot be shown redacted with all of the run's secrets; its text is in the campaign's local log only. "
    "Declare them, or declare none, and the diagnostic is shown"
)


class DiagnosticTooLarge(ValueError):
    """A diagnostic is more than `LIMIT_BYTES` of canonical JSON."""


class DiagnosticUnreadable(ValueError):
    """A stored diagnostic is not the JSON object the core writes; the message names the attempt."""


def snapshot_of(
    error: BaseException, redactor: Callable[[], Redactor | Chained], *, complete: bool = True
) -> dict[str, Any] | None:
    """The snapshot an attempt keeps of an error, redacted with `redactor()`; None when the error has nothing to keep
    (it is no infrastructure error, or it names no instrument and carries no diagnostic). Never raises.

    `complete` says the executor declared its secrets (`RunRequest.declare_secrets`; a call with none says it has
    none). Never a diagnostic redacted against a partial list of the run's secrets: without it a diagnostic is withheld
    (`{"phase": "start", "ok": false, "error": "Withheld", "why": WITHHELD}`: the phase alone, since the instrument's
    name can be a secret too), because a secret that only the executor knows would be kept on the row, in the marker
    and in the export, which ship. A failed start that gave no diagnostic has nothing to redact: it is the phase, and
    the instrument's name only when `complete`."""
    given, origin = getattr(error, "diagnostic", None), getattr(error, "origin", None)  # a subclass may skip the base
    if not isinstance(error, InfraError) or (given is None and origin is None):
        return None
    if not complete:  # only the literal phase: the instrument's name is the study's own word, a secret's value perhaps
        head = _phase(origin)
        return head if given is None else {**head, "ok": False, "error": WITHHELD_ERROR, "why": WITHHELD}
    try:
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


def _phase(origin: Any) -> dict[str, Any]:
    """The phase of an origin when it is the literal `start_instruments` sets; nothing otherwise, and never an error."""
    try:
        held = dict(origin or {})
    except Exception:
        return {}
    return {"phase": held["phase"]} if held.get("phase") == "start" else {}


def _head(origin: Any, redactor: Callable[[], Redactor | Chained]) -> dict[str, Any]:
    """The phase and the instrument a snapshot that failed to be kept keeps, redacted like the rest (an instrument's
    name can be any word, a secret's value among them). When they cannot be redacted only the phase is kept."""
    try:
        redacted, _ = redactor().structure(dict(origin or {}))
        return dict(redacted)
    except Exception:
        return _phase(origin)


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
