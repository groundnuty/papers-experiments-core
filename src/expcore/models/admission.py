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
"""

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import ModelBackend, ProbeReply
from expcore.models.retry import classify, describe


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
