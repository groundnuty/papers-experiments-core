# Provenance: new
"""How a replay stops: a divergence from the record, named by the path of the first place that differs."""

from typing import NoReturn


class ReplayDivergence(Exception):
    """The replay asked for, or produced, something the record does not hold.

    `path` names the first place that differs (for example `transcript[2].request_sha256` or `tool_calls[0].text`);
    `message` says how. The text of the exception holds both.
    """

    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


class Halt(BaseException):
    """Carries a divergence out of the agent loop.

    The loop turns every `Exception` from a backend, a tool provider or a clock into an outcome of the attempt. A
    divergence found while the loop runs is not an outcome of the attempt, so it travels as a `BaseException`, which
    the loop does not catch, and the replay re-raises it as the `ReplayDivergence` it carries.
    """

    def __init__(self, divergence: ReplayDivergence):
        super().__init__(str(divergence))
        self.divergence = divergence


def stop(path: str, message: str) -> NoReturn:
    """End the replay here, from inside the loop, with a divergence at `path`."""
    raise Halt(ReplayDivergence(path, message))
