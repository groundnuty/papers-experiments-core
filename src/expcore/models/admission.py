# Provenance: new
"""The admission check before a campaign dispatches a model: B07 and the admission half of B11.

A listed model is not a servable model: inactive models and models outside the grant pass a `/models` listing and
fail the first completion. A model is admitted only after a one-token completion succeeds, and only when its output
limit fits the endpoint's context window.
"""

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import ModelBackend
from expcore.models.retry import classify, describe


async def admit(backend: ModelBackend) -> str | None:
    """Return None when the backend may be dispatched, else a one-line reason it may not.

    The check never reads a model listing. Errors that are not the provider's (a bug in the harness) are raised.
    """
    limit, context = backend.output_limit, backend.context_tokens
    if limit is not None and context is not None and limit >= context:
        return f"the output limit of {limit} tokens does not fit the endpoint's context of {context} tokens"
    try:
        await backend.probe()
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
    return None
