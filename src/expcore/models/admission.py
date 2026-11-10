# Provenance: new
"""The admission check before a campaign dispatches a model: B07 and the admission half of B11.

A listed model is not a servable model: inactive models and models outside the grant pass a `/models` listing and
fail the first completion. A model is admitted only after a one-token completion succeeds, and only when its output
limit fits the endpoint's context window.

The completion's reply must also report its input and its output tokens (B07). Every campaign declares a token budget
(`token_budget` is required), and the loop holds a run to it by the counts its replies report: an endpoint that omits
them leaves the cap unenforced for that model alone, which would confound every comparison of models, so it is not
admitted. A backend whose probe returns no reply to read (`None`) cannot show the counts, and is refused the same way.
An occasional reply without usage on an admitted model is another matter: the record keeps it, and with a budget the
requests after it are topped up and it is an event (`expcore.models.spend`, C26).

With a budget, a model of the core's own adapters is also asked whether its endpoint holds a reply to the max_tokens it
is sent (`reply_cap_unshown`, C27): a request for a long reply under a small max_tokens must come back cut at that
limit, its output count within it. What it shows decides only which bound the budget reserves
(`expcore.campaign.reply_cap`); a model it does not show is admitted all the same.
"""

from typing import Protocol

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import OUTPUT_LIMIT_STOPS, ModelBackend, ProbeReply
from expcore.models.retry import classify, describe


class ReplyCapProbe(Protocol):
    """A backend that can send the probe of the reply maximum: the core's OpenAI-compatible and Anthropic adapters."""

    async def probe_reply_cap(self) -> ProbeReply:
        """Ask for a long reply under a small max_tokens and return what came back."""
        ...


async def admit(backend: ModelBackend) -> str | None:
    """Return None when the backend may be dispatched, else a one-line reason it may not.

    The check never reads a model listing. Errors that are not the provider's (a bug in the harness) are raised.
    """
    limit, context = backend.output_limit, backend.context_tokens
    if limit is not None and context is not None and limit >= context:
        return f"the output limit of {limit} tokens does not fit the endpoint's context of {context} tokens"
    try:
        reply = await backend.probe()
    except InfraError as error:
        return f"the one-token completion failed: {error.kind}: {error.detail}"
    except ContextOverflow as error:
        return f"the one-token completion overflowed the context ({error.pattern})"
    except RequestAbandoned:
        return "the one-token completion was sent and not answered in time"
    except Exception as error:
        info = classify(error)
        if info is None:
            raise
        return f"the endpoint refused the one-token completion: {describe(info)}"
    return unreported_usage(reply)


async def reply_cap_unshown(backend: ReplyCapProbe) -> str | None:
    """Return why the probe did not show that the endpoint holds a reply to the max_tokens it is sent, or None when it
    did: the reply stopped at the limit with an output count within it. A reply that stopped by itself shows nothing.
    Errors that are not the provider's (a bug in the harness) are raised."""
    try:
        reply = await backend.probe_reply_cap()
    except (InfraError, ContextOverflow, RequestAbandoned) as error:
        return f"the probe of the reply maximum failed: {type(error).__name__}: {error}"
    except Exception as error:
        info = classify(error)
        if info is None:
            raise
        return f"the endpoint refused the probe of the reply maximum: {describe(info)}"
    out = reply.output_tokens
    if out is None:
        return "the probe of the reply maximum reported no output token count"
    if out > reply.max_tokens:
        return f"the probe of the reply maximum reported {out} output tokens for a max_tokens of {reply.max_tokens}"
    if reply.stop_reason not in OUTPUT_LIMIT_STOPS:
        return (
            f"the probe of the reply maximum stopped by itself ({reply.stop_reason}) after {out} tokens, so it shows "
            f"nothing of the max_tokens of {reply.max_tokens} it was sent"
        )
    return None


def unreported_usage(reply: ProbeReply | None) -> str | None:
    """Return why a probe's reply does not show that the endpoint reports usage, or None when it does."""
    if reply is None:
        return "the backend's probe returned no reply to read, so it cannot show that the endpoint reports usage"
    missing = [
        name for name, count in (("input", reply.input_tokens), ("output", reply.output_tokens)) if count is None
    ]
    if not missing:
        return None
    return (
        f"the one-token completion reported no {' and no '.join(missing)} token count: the token budget is enforced "
        "from the counts each reply reports, so an endpoint that omits them cannot be held to it"
    )
