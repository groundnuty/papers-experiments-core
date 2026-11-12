# Provenance: new
"""How long one space's outage scenario can need the agent's tokens: the lifetime they are minted for (O28, #108).

The sandbox's data-path token and the tool server's token are minted when `outage_agent` starts them, and must outlive
everything after: the mount and the tools with everything up, the stop, the measurements, the restore, the wait for the
target, the re-reads, the transfers' reading and the tool plane's check at its stop. A token that ended before a
re-read through the mount would fail it, and the failure would be blamed on the provider. So unless the caller names a
ceiling (`Context.outage_ceiling_s`), the tokens are minted for `scenario_s`: the longest all of that can take with
every bound of the check and of `[faults]` spent, plus `MARGIN_S` for the steps that have no bound here (starting the
sandbox and the tool server, their stop). A platform whose `[tokens]` limit is below it refuses the mint, and the check
is then unknown with the tool server's reason, never run on tokens that may end midway.

Each bound below is the longest its step may take in `outage_run`, `outage_tools` and `outage_mount`; the injector's
start and its clear are each bounded as `FaultInjector.cancel_wait_s` bounds a start (every settling round's settle
and command timeouts, and two waits for a provider to serve).
"""

from expcore_onedata.day0.outage_cells import KINDS
from expcore_onedata.day0.outage_mount import cell_timeout_s
from expcore_onedata.day0.outage_tools import READ_ATTEMPTS
from expcore_onedata.day0.registry import Context

MARGIN_S = 600.0
DOWN_PROBE_S = 10.0  # the longest the check waits to see that the target does not answer
REST_READS = 3  # the reads of the outage over REST, and again after it: two files and the listing


def tool_call_s(bound_s: float, poll_s: float) -> float:
    """One tool call with its retries: every attempt to the bound, and the polls between them."""
    return READ_ATTEMPTS * bound_s + (READ_ATTEMPTS - 1) * poll_s


def replication_s(bound_s: float, poll_s: float) -> float:
    """A schedule (one attempt) and the following of its transfer: polled until the bound, the last call whole."""
    return bound_s + bound_s + poll_s + tool_call_s(bound_s, poll_s)


def scenario_s(ctx: Context, *, mount: bool) -> float:
    """The longest one space's scenario can need the agent's tokens, from the start of the sandbox, with `MARGIN_S`."""
    faults, bound = ctx.fault_settings, ctx.outage_bound_s
    if faults is None or ctx.placement is None:
        raise LookupError("the outage scenario is bounded only with [faults] and [placement] in platform.toml")
    poll, settle = ctx.placement.settings.poll_s, faults.settle_timeout_s
    call, replicate = tool_call_s(bound, poll), replication_s(bound, poll)
    injector = (settle + faults.command_timeout_s) * (faults.clear_attempts + 2) + 2 * faults.ready_timeout_s
    knowledge = (
        (2 * call + replicate)  # with everything up: a distribution, the providers, a replication
        + (len(KINDS) * call + call + len(KINDS) * replicate)  # during the outage, for the knowledge pair
        + 2 * len(KINDS) * call  # where each file read during the outage is held (REST and the mount)
        + len(KINDS) * call  # the transfers after the restore
    )
    reads = (2 * cell_timeout_s(3, bound) + settle + cell_timeout_s(len(KINDS), bound) + poll) if mount else 0.0
    rest = REST_READS * 2 * bound + DOWN_PROBE_S  # during the outage (a listing may take two pages), and the probe
    after = (settle + DOWN_PROBE_S + poll) + REST_READS * (settle + 2 * bound + poll)  # the wait, and the re-reads
    return 2 * injector + knowledge + reads + rest + after + MARGIN_S


__all__ = ["DOWN_PROBE_S", "MARGIN_S", "scenario_s"]
