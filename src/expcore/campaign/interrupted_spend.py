# Provenance: new
"""What an interrupted attempt is charged: the spend its call log recorded, never less (C05).

An attempt killed before its result was stored left no counts, so it used to be charged its whole bound R. R is the
most its caps let it cost, while a kill mid-run has usually spent a fraction of that: one attempt killed in flight
could then hold every later run of its model on the budget. So, with a budget, each attempt keeps a **call log** as it
runs (`Store.note_call_sent`, `Store.note_call_answered`, written by the attempt's spend guard,
`campaign.topup.AttemptSpend`): `calls_sent`, the requests it sent, each counted before it goes out, and
`calls_usage`, the counts each reply reported (`[input, output, cache writes, cache reads]`, or `null` for a reply the
endpoint billed and the backend could not read), or `"failed"` for a request whose last send failed after it may have
been billed, with nothing sent after it: that send is charged a top-up of one request's bound (C26) in the same
transaction that closes the request here (`Store.add_topup(close_call=True)`), so it is never charged again as a
request in flight. The spend journal has a `call` line for each request sent and each request closed, with the
attempt's id (O62), and its `attempt` lines name how many of each it holds so far; a reader sums the call lines by
attempt id (`lost_spend`), and a set that disagrees with its attempt lines is no log. So the log outlives a host
whose store a kill took, and the journal grows linearly in the calls.

An interrupted attempt is charged on one of three **bases**:

- `measured`: every request it sent was answered: what the replies' counts cost at the model's price, each request
  at the tier its own prompt fell in;
- `measured_plus_inflight`: one request was sent and not answered, so it may have been billed in full: the measured
  part plus that request's bound (`attempt_bounds.request_bound_usd`, the most one request can cost: its prompt within
  the context window and its reply within the reply maximum, or both within the window, at the dearest rates);
- `bound`: its whole bound R, as before, whenever the log cannot be read or trusted: none kept (no budget, or an
  attempt from before the log), no request in it, a reply that left a count out or could not be read, more than one
  request unanswered, more replies than requests, a count that is no count, or a store whose log a journal has not
  confirmed (below).

The log sees what the guard sees: every request the core's recording makes (`record_run`, `record_attempt`), in the
tasks and threads the executor starts with the attempt's context. An executor that sends requests another way leaves
them out of its record too, and so out of a finished attempt's cost; one that sends every request another way leaves
a log with no request, which is why such a log charges the bound: it cannot be told from a kill before the first
request. (An attempt that raised before its loop ended, rather than one killed, and whose backend does not say it sent a
request, is charged nothing for such a log: `campaign.no_request`.)

Nothing recorded is ever charged less: the measured part is the recorded counts at the model's price, and the
in-flight request is added at its bound whatever the measured part is, never capped at R. The attempt's top-ups (C26)
are added on top as for any attempt.

**Which log is trusted.** A store that was never journaled is the store that ran the attempt, so recovery decides the
basis from its log when it interrupts the attempt (`recovery.recover`). A store whose ledger names a spend journal may
be a snapshot copied back after a kill, its log older than what the attempt went on to spend: recovery records such
an attempt at `bound`, and only the start's count of its own journal (`lost_spend.reconcile`) gives it the journal's
log, the longer of the two when one extends the other, and the basis that log says. An attempt the journal lists and
the store lacks is charged from the journal's log directly, which is complete up to the kill: each line is written
before the send it allows. The basis recorded on the row (`spend_basis`) is what the budget gate charges by; a row
whose log no longer says the basis it records is charged its bound. A store copied back to run again (a snapshot's
restore, the shard driver's resume from its archive) has its attempts' directories pointed at its new `runs/`
(`snapshot.rebase_run_dirs`), which marks it copied back (`COPIED_BACK_KEY` in its meta): its own log is never
trusted then, journal or none. A store never journaled and copied back by hand from a backup, with neither, is outside
this, as it is outside every guarantee about what was spent after the copy.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from expcore.campaign.store import FAILED, column_for  # FAILED: a request closed by a failed last send (C26)
from expcore.config.models import ModelSpec

MEASURED, INFLIGHT, BOUND = "measured", "measured_plus_inflight", "bound"
BASES = (MEASURED, INFLIGHT, BOUND)
COPIED_BACK_KEY = "copied_back_utc"  # meta: the store was copied back to run again (`snapshot.rebase_run_dirs`)
Counts = tuple[int | None, int | None, int | None, int | None]
Entry = Counts | str | None  # a reply's counts, FAILED, or None for a reply billed and not read


@dataclass(frozen=True)
class CallLog:
    """An attempt's call log as read: the requests it sent and how each closed one ended: its reply's counts, FAILED,
    or None for a reply that was not read."""

    sent: int
    answered: tuple[Entry, ...]

    @property
    def in_flight(self) -> int:
        """The requests sent and not closed."""
        return self.sent - len(self.answered)

    def as_columns(self) -> tuple[int, str]:
        """The log as the store's `calls_sent` and `calls_usage` hold it."""
        return self.sent, json.dumps([c if c is None or isinstance(c, str) else list(c) for c in self.answered])

    def extends(self, other: "CallLog") -> bool:
        """Whether this log is `other` with nothing changed and perhaps more after it."""
        prefix = self.answered[: len(other.answered)]
        return self.sent >= other.sent and len(self.answered) >= len(other.answered) and prefix == other.answered


def read_log(sent: Any, usage: Any) -> CallLog | None:
    """The call log in the store's columns (or a lost attempt's in the ledger); None when none was kept, or it cannot
    be read or is not consistent: a count that is no whole number, a cache count above its prompt, more replies than
    requests, or more than one request unanswered."""
    if not isinstance(usage, str):
        return None
    try:
        entries = json.loads(usage)
    except ValueError:
        return None
    return log_of(sent, entries)


def log_of(sent: Any, entries: Any) -> CallLog | None:
    """The call log of a count of requests sent and how each closed one ended, in order (the store's columns read,
    or a journal's call lines summed by attempt); None as `read_log` says."""
    if isinstance(sent, bool) or not isinstance(sent, int) or sent < 0 or not isinstance(entries, list | tuple):
        return None
    answered: list[Entry] = []
    for entry in entries:
        if entry is None or entry == FAILED:
            answered.append(entry)
            continue
        counts = _counts(entry)
        if counts is None:
            return None
        answered.append(counts)
    log = CallLog(sent, tuple(answered))
    return log if 0 <= log.in_flight <= 1 else None


def _counts(entry: Any) -> Counts | None:
    """One reply's counts, each a whole number of at least 0 or None, the cache counts within the prompt."""
    if not isinstance(entry, list) or len(entry) != 4:
        return None
    if any(v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0) for v in entry):
        return None
    prompt, reply, written, read = entry
    if prompt is not None and (written or 0) + (read or 0) > prompt:
        return None
    return prompt, reply, written, read


def _unknown(entry: Entry) -> bool:
    """Whether a closed request's cost is unknown: a reply not read, or one whose prompt or reply count is missing."""
    return entry is None or (not isinstance(entry, str) and (entry[0] is None or entry[1] is None))


def basis_of(log: CallLog | None) -> str:
    """The basis a log charges on: `bound` without a log, with no request in it (a kill before the first request and
    an executor whose requests bypass the recording look the same), or with a reply whose prompt or reply count is
    unknown."""
    if log is None or log.sent == 0 or any(_unknown(c) for c in log.answered):
        return BOUND
    return MEASURED if log.in_flight == 0 else INFLIGHT


def measured_usd(spec: ModelSpec, answered: Sequence[Entry]) -> float | None:
    """What the answered requests cost at the model's price, each at its own tier; None when unpriced or a count is
    unknown. A cache count left out is none, as for a finished attempt. A request closed by a failed send costs
    nothing here: its top-up, charged with the attempt's, is what it cost."""
    if spec.free:
        return 0.0
    if spec.price is None or any(_unknown(c) for c in answered):
        return None
    replies = [c for c in answered if c is not None and not isinstance(c, str)]
    return sum(spec.price.request_usd(c[0], c[1], c[2] or 0, c[3] or 0) for c in replies)


def logged_cost(
    spec: ModelSpec, log: CallLog | None, *, bound: float | None, per_request: float | None
) -> tuple[float | None, str]:
    """An interrupted attempt's cost from its log without its top-ups, and the basis: measured, plus `per_request`
    (one request's bound) for the request in flight; `bound` (its bound R) when the log says `bound`. None for an
    unpriced model. The caller gives both bounds, by the reply maximum or the window as the attempt was reserved."""
    measured = None if basis_of(log) == BOUND or log is None else measured_usd(spec, log.answered)
    if measured is None or log is None:
        return bound, BOUND
    if log.in_flight == 0:
        return measured, MEASURED
    return (None if per_request is None else measured + log.in_flight * per_request), INFLIGHT


def trusted_log(row: Any) -> CallLog | None:
    """The call log of an interrupted row when its recorded basis is measured and the log still says so; None when it
    is charged its bound (recorded so, none recorded, or a log that no longer says what was recorded)."""
    log = read_log(row["calls_sent"], row["calls_usage"])
    recorded = row["spend_basis"]
    return log if recorded in (MEASURED, INFLIGHT) and basis_of(log) == recorded else None


EXPORT_KEY = "interrupted_spend"


def exported(store: Any) -> list[dict[str, Any]] | None:
    """The export's `interrupted_spend`: each interrupted attempt, in attempt order, with the basis it is charged on
    (`bound` for one that kept no log or recorded none), the requests it sent and the replies its log holds (None
    without a log); None when no interrupted attempt records a basis, so a campaign without call logs exports what it
    did before. `store` answers `q`; a snapshot made before the log has no such column and exports none."""
    columns = {row["name"] for row in store.q("PRAGMA table_info(attempts)")}
    if not {"calls_sent", "calls_usage", "spend_basis"} <= columns:
        return None
    rows = store.q(
        f"SELECT a.id, a.run_key, r.{column_for('model')} AS model, a.calls_sent, a.calls_usage, a.spend_basis "
        "FROM attempts a JOIN runs r USING(run_key) WHERE a.state='interrupted' ORDER BY a.id"
    )
    if not any(row["spend_basis"] is not None for row in rows):
        return None
    entries = []
    for row in rows:
        log = read_log(row["calls_sent"], row["calls_usage"])
        entries.append(
            {
                "attempt_id": row["id"],
                "run_key": row["run_key"],
                "model": row["model"],
                "basis": _basis(row),
                "calls_sent": None if log is None else log.sent,
                "calls_answered": None if log is None else len(log.answered),
            }
        )
    return entries


def _basis(row: Any) -> str:
    log = trusted_log(row)
    return BOUND if log is None else basis_of(log)


def merged(store_log: CallLog | None, journal_log: CallLog | None) -> CallLog | None:
    """The log of an attempt that both the store and its own journal hold: the longer, when one extends the other (a
    snapshot's store lags its journal; a store can lead it by the one line a failed write never journaled, after which
    nothing was sent); None when neither does or either cannot be read."""
    if store_log is None or journal_log is None:
        return None
    if journal_log.extends(store_log):
        return journal_log
    return store_log if store_log.extends(journal_log) else None
