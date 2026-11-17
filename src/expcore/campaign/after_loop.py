# Provenance: new
"""What an attempt is charged when its executor raises after the model loop has ended (C05).

An executor that raises is recorded with no usage: the record `record_run` built is lost with the exception, so the
attempt is charged its bound, the most its caps let it cost. When the raise comes after the loop, a post-run guard that
found a leak in a tool answer for one, every request the attempt made is closed, and the call log its spend guard kept
(`campaign.interrupted_spend`: in the attempt's row, and line by line in the spend journal) holds what each reply
reported. `settle_usage` gives such an attempt that measured usage, and the basis it was charged on (`spend_basis`).

**The loop has ended** only when the recording says so: `expcore.records.build` tells the attempt's spend guard
(`spend.after_loop`) as `run_agent` returns, and the guard keeps `AttemptSpend.loop_ended`. Nothing else tells an
exception after the loop from one during it: a tool that raises between two requests also leaves every request closed.
An executor that runs the loop another way never says so, a raise before the loop or during it never does, and the
attempt keeps today's usage: unknown, so charged its bound, with no basis recorded, unless its log shows that no request
was sent (`campaign.no_request`, which decides that for an attempt whose loop did not end).

**The log is complete** (basis `measured`) when it holds at least one request and every request in it is closed with
its counts: each reply's input and output counts known, or the request closed by a failed last send, whose top-up is
what it cost (C26). Anything else is incomplete, and the attempt is charged its **bound** and records the basis
`bound`:

- no log was kept, or it cannot be read or is not consistent (a count that is no whole number, a cache count above its
  prompt, more replies than requests, more than one request unanswered);
- no request is in it: a log that shows nothing cannot be told from an executor whose requests bypass the recording;
- a request was sent and has no end entry: it may have been billed in full, and an attempt that raises after its loop
  is not charged a request in flight at its bound on top of what was measured (`measured_plus_inflight`, which only an
  interrupted attempt is charged), but its whole bound, the figure it was reserved at;
- a reply that was billed and could not be read, or one that left its input or output count out.

Every cause the loop has for incomplete usage leaves such a log: a request given up or abandoned is one with no end
entry, and a reply without a count or one that could not be read is a count missing or a reply not read. The log sees
what the guard sees, the requests the core's recording makes; an executor that sends requests another way leaves them
out of it, as of an interrupted attempt's. Measured is never reported where the log cannot support it, and an attempt
without a budget keeps no log, so it has no guard and never reaches here.
"""

import dataclasses
from typing import Any

from expcore.campaign.interrupted_spend import BOUND, MEASURED, basis_of, read_log
from expcore.campaign.markers import AttemptResult


def settle_usage(result: AttemptResult, calls_sent: Any, calls_usage: Any) -> AttemptResult:
    """Return `result`, the failure of an executor that raised after its loop, with the usage its call log measured
    (the row's `calls_sent` and `calls_usage`) and `spend_basis` `measured`, or with its usage left unknown and the
    basis `bound` when the log is unreadable or incomplete (see the module docstring). A result whose usage is
    already known is returned as it is."""
    if result.usage_complete:
        return result
    log = read_log(calls_sent, calls_usage)
    if basis_of(log) != MEASURED or log is None:
        return dataclasses.replace(result, spend_basis=BOUND)
    replies = tuple(counts for counts in log.answered if isinstance(counts, tuple))  # the rest closed by a failed send
    return dataclasses.replace(
        result,
        input_tokens=sum(counts[0] or 0 for counts in replies),
        output_tokens=sum(counts[1] or 0 for counts in replies),
        cache_creation_tokens=_total(replies, 2),
        cache_read_tokens=_total(replies, 3),
        usage_complete=True,
        replies_checked=True,
        turn_usage=replies,
        spend_basis=MEASURED,
    )


def _total(replies: tuple[tuple[int | None, ...], ...], index: int) -> int | None:
    """A cache count summed over the replies that reported it; None when none did (unknown, never 0)."""
    counts = [reply[index] for reply in replies if reply[index] is not None]
    return sum(counts) if counts else None
