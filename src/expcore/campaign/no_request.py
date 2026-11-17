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
- the backend does not say it started a request. A backend that counts them (`requests_sent`, as for a first-turn
  refusal) is a veto: a count above 0, or one that is no whole number or cannot be read, is evidence the log may have
  missed a request, which an executor that sends another way than through the core's recording leaves out of it.

Such an attempt keeps zero tokens and no turn, complete, and is charged what they cost, 0 at any price (C24), with the
basis `measured` on its row and in its marker and the reason `REASON` in an event. Anything else keeps what it has
today: its usage unknown, its bound charged and no basis recorded, which is what an unreadable or incomplete log, a send
with no reply (it may have been billed in full) and a backend that counted a request come to.

**What this does not decide.** An attempt whose loop ended is `after_loop`'s (an empty log there contradicts the loop
having run, and is charged its bound). An attempt killed before its result was stored has no result here; recovery
charges it from its log as before. Without a budget there is no guard and no log, so nothing changes. **The residual:**
an executor that sends every request another way than through the core's recording, on a backend that keeps no count of
its requests, leaves a log that cannot be told from one of an attempt that sent none, and is charged nothing. The core's
own adapters count their requests; a study whose backend does not should give it a `requests_sent`.
"""

import dataclasses
from typing import Any

from expcore.campaign.interrupted_spend import MEASURED, read_log
from expcore.campaign.markers import AttemptResult

REASON = "no model request in a complete journal"
EVENT = "usage_measured_zero"


def no_request_in(calls_sent: Any, calls_usage: Any, topup_usd: Any, backend: Any) -> bool:
    """Tell whether an attempt's call log (the row's `calls_sent` and `calls_usage`) is readable and complete and
    holds no request, the attempt took no top-up, and its backend does not say it started a request. This is the one
    place where "complete and no request" is decided."""
    log = read_log(calls_sent, calls_usage)
    if log is None or log.sent != 0 or log.answered:
        return False
    return _no_top_up(topup_usd) and not _started_a_request(backend)


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
    """Whether the attempt's top-up total is none (a row's column is a number; a missing one is none)."""
    if topup_usd is None:
        return True
    return isinstance(topup_usd, int | float) and not isinstance(topup_usd, bool) and topup_usd == 0


def _started_a_request(backend: Any) -> bool:
    """Whether the backend says it started a request. A backend that keeps no count says nothing (None); a count that is
    no whole number, or that cannot be read, is no evidence of none, so it counts as one that started a request."""
    try:
        count = getattr(backend, "requests_sent", None)
    except Exception:  # a counter that raises proves nothing: the attempt is charged its bound, never nothing
        return True
    if count is None:
        return False
    return isinstance(count, bool) or not isinstance(count, int) or count != 0
