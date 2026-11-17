# Provenance: new
"""What an attempt is charged when it failed with no model request in its call log (C05).

An executor that raises is recorded with no usage, so the attempt used to be charged its bound, the most its caps let
it cost, unless it was a first-turn refusal, which knows it sent nothing (C31). With a bound of about half a model's
ceiling, two attempts that failed before they sent anything (a fault that could not be induced, a state file that could
not be read, a check that failed at start) spent that model's budget with no request sent. Each attempt keeps a call
log as it runs (`campaign.interrupted_spend`: its spend guard counts each request in its row before the request goes
out), and the log says whether any request was sent. `settle_unsent` reads it for an attempt that raised before the
loop ended, whatever raised.

**No request in a complete log** (`no_request_in`) is all of:

- the log is readable and consistent (`interrupted_spend.read_log`: a count that is no whole number, more replies than
  requests, or more than one request unanswered make it no log) and holds no request: no send begun, so no reply and no
  request closed by a failed send;
- the attempt was charged no top-up (a top-up is charged for a send that may have been billed);
- the backend's own count of its requests says none: `requests_sent`, an optional attribute that the core's adapters
  keep (each counts a request before it sends it, resends included), is exactly 0. The log sees only the requests the
  core's recording makes, so an executor that sends another way leaves them out of it, and an empty log alone cannot be
  told from the log of an attempt that sent none; the backend's count is what confirms it. A backend that keeps no
  count, or whose count is above 0, is no whole number or cannot be read, confirms nothing.

Such an attempt keeps zero tokens and no turn, complete, and is charged what they cost, 0 at any price (C24), with the
basis `measured` on its row and in its marker and the reason `REASON` in an event. Anything else keeps what it has
today: its usage unknown, its bound charged and no basis recorded, which is what an unreadable or incomplete log, a send
with no reply (it may have been billed in full), a top-up and a backend that counted a request, or kept no count to
confirm none, come to.

**What this does not decide.** An attempt whose loop ended is `after_loop`'s (an empty log there contradicts the loop
having run, and is charged its bound). An attempt killed before its result was stored has no result here; recovery
charges it from its log as before. Without a budget there is no guard and no log, so nothing changes. A study's own
backend that keeps no `requests_sent` is charged the bound for an attempt that failed at start, as before: it should
keep a count, as the core's adapters do, to have such an attempt charged nothing.
"""

import dataclasses
from typing import Any

from expcore.campaign.interrupted_spend import MEASURED, read_log
from expcore.campaign.markers import AttemptResult

REASON = "no model request in a complete journal"
EVENT = "usage_measured_zero"


def no_request_in(calls_sent: Any, calls_usage: Any, topup_usd: Any, backend: Any) -> bool:
    """Tell whether an attempt's call log (the row's `calls_sent` and `calls_usage`) is readable and complete and
    holds no request, the attempt took no top-up, and its backend's own count of its requests is 0. Anything that
    cannot be read or confirmed is not "no request". This is the one place that decides "complete and no request"."""
    log = read_log(calls_sent, calls_usage)
    if log is None or log.sent != 0 or log.answered:
        return False
    return _no_top_up(topup_usd) and _counted_none(backend)


def settle_unsent(
    result: AttemptResult, calls_sent: Any, calls_usage: Any, topup_usd: Any, backend: Any
) -> AttemptResult:
    """Return `result`, the failure of an executor that raised before its loop ended, with zero usage known and the
    basis `measured` when its call log shows no request (`no_request_in`); otherwise as it is. A result whose usage
    is already known (a first-turn refusal) is returned as it is."""
    if result.usage_complete or not no_request_in(calls_sent, calls_usage, topup_usd, backend):
        return result
    return dataclasses.replace(
        result,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=None,
        cache_read_tokens=None,
        usage_complete=True,
        replies_checked=True,
        turn_usage=(),
        spend_basis=MEASURED,
    )


def _no_top_up(topup_usd: Any) -> bool:
    """Whether the attempt's top-up total is the number 0; a total that is missing or no number is not none."""
    return isinstance(topup_usd, int | float) and not isinstance(topup_usd, bool) and topup_usd == 0


def _counted_none(backend: Any) -> bool:
    """Whether the backend says it started no request: its `requests_sent` is the whole number 0. A backend that keeps
    no count, or one that is None, above 0, no whole number or that cannot be read, confirms nothing."""
    try:
        count = getattr(backend, "requests_sent", None)
    except Exception:  # a counter that raises proves nothing: the attempt is charged its bound, never nothing
        return False
    return isinstance(count, int) and not isinstance(count, bool) and count == 0
