# Provenance: new
"""The day-0 check of what reads and the knowledge plane do while a provider is down (O28, issue #108).

A study's outage condition cannot be designed without these facts, so the check takes a declared provider down, records
what each way of reaching the data answers, brings the provider back, and fails if it cannot show that it did.

**The scenario, per space.** A space has a *target* (its data provider, whose service is taken down), a *mirror* (a
second provider that holds a full replica of some of its files) and the agent's *local* provider (through which every
read is made, as the agent's is). Three different providers are needed; the target being the local one is no outage
the agent's reads could route around, so that, or a support set of two, makes the check unknown with the reason. The
scratch files come in pairs: a file *replicated* at the mirror and a file held by the *target alone*, proved by the
local provider's own block map before anything is stopped. Reads change what a provider holds (a range read through a
provider that lacks the bytes places them there), so each way of reaching the data gets a pair of its own, and a
baseline pair is read with everything up.

**Before the outage** (all providers up): the REST baseline reads through the target, the mirror and, cold, the local
provider, and the scratch directory's listing; the same through the mount; and the knowledge tools. A baseline that
does not serve means nothing that follows could be attributed, so the check is unknown and nothing is stopped.

**During it**, through the local provider: a read of a replicated file, a read of a file held by the target alone and a
listing of the scratch directory, over REST (`outage_cells`) and, when the platform declares a mount data path,
through the mount of a real sandbox in mount mode, inside the agent's container (`outage_mount`); and the knowledge
plane as the agent meets it: the condition's tool server, started with the leased slot's tool-server user and token,
its tools called through MCP (`outage_tools`: `get_file_distribution`, `list_space_providers`,
`schedule_file_replication` to the local provider, `get_transfer` until it ends or the bound). The sandbox and the tool
server are started for the space's scenario (`outage_agent`), and each is measured with everything up first: a
baseline that does not serve stops nothing. Every cell has an outcome (`served`, `partial`, `differs`, `refused`, `no
answer`, `blocked`) and its seconds against `Context.outage_bound_s`; a mount read also its errno and whether it
blocked, a tool call whether it timed out, its attempts and its wording as a digest with the error terms it holds.

**After it**: the restore, by the fault injector, verified by the identity of the service having changed and the
provider serving, and the target answering again, then the reads again, which must return what was written. The
restore runs in a `finally`; the scratch is torn down after it, since teardown deletes through the target, and its
absence is verified (O11).

**The injector (O10, O11).** The provider is taken down and brought back by the platform's fault injector
(`outage_fault`, over `expcore_onedata.faults.FaultInjector`): journalled before the stop, recovered if an earlier
process left it open, restored only with a changed identity and once the provider serves, a stop of unknown outcome
waited out. The check holds the federation's lock exclusively (`admission.FederationLock`) for a whole space's
scenario, the scratch's teardown included, and the injector takes that admission as its own.

**Verdict.** Passed: the target was seen down and restored, every cell measured, the re-reads right, the scratch gone.
Failed: the target answered while it was down, it could not be restored or did not answer or read back after the
restore, the scratch was not shown gone, a measurement raised, or the sandbox's teardown or the tool plane's check at
its stop failed. A space whose provider is not shown back (not
cleared, or not answering REST again) halts the spaces after it. Unknown: anything the check could not do or measure,
with the reason: a mount the platform declares and that was not read, a tool not called, or the sandbox or the tool
server not started (starting them needs `Context.agent_host`, which `EXPCORE_LIVE_DOCKER=1` gives the runner). What a
cell answered is a fact, never a failure.

**Consent (O10).** Nothing is stopped without `EXPCORE_LIVE_MUTATE=1`, `EXPCORE_LIVE_FAULTS=1` and `sanctioned = true`
in `[faults]` of platform.toml. **Names (O12).** Providers are `providers[i]` in every key and message, spaces
`spaces[i]`, files the pair and kind they are, and nothing identifying is recorded. Only structured fields are kept
(outcomes, statuses, error ids and errnos of a plain word's shape, counts, seconds, tool names, held bytes by label, a
transfer's state from a known set); a tool's answer or wording, which can name what the check does not know, is kept
only as a digest of it with every known name and id-like token replaced, its length and its error terms
(`outage_tools.Scrubber`).
"""

import asyncio
import secrets

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.admission import OUTAGE_RUN, FederationLock
from expcore_onedata.config import StudySpace
from expcore_onedata.day0.mutating import clean_up
from expcore_onedata.day0.outage_agent import AgentPlane, unallowed_tools
from expcore_onedata.day0.outage_agent import refusal as agent_refusal
from expcore_onedata.day0.outage_cells import Roles
from expcore_onedata.day0.outage_run import Run
from expcore_onedata.day0.registry import Context, Result, holder_of, per_space, register, unknown


def _refusal(ctx: Context) -> str | None:
    """What stands between the check and its start, or None: consent, the sanction, a driver and the placement."""
    if not ctx.allow_mutation:
        return "the outage check needs mutation consent (EXPCORE_LIVE_MUTATE=1), so nothing was sent"
    if not ctx.allow_faults:
        return "the outage check takes a provider down and needs EXPCORE_LIVE_FAULTS=1, so nothing was sent"
    if ctx.fault_settings is None or ctx.fault_settings.sanctioned is not True:
        return "the outage check needs 'sanctioned = true' in [faults] of platform.toml, so nothing was sent"
    if ctx.fault_driver is None:
        return "no driver for the deployment's services was given to this run, so no provider can be taken down"
    if ctx.placement is None:
        return "platform.toml has no [placement] table, so the data provider is not declared"
    return None


def prerequisite(ctx: Context) -> str | None:
    """What a platform.toml and a run still lack for the check to start and measure everything, or None: the
    refusals the check makes, for each space the three providers it needs, and what starting the sandbox and the tool
    server needs (`outage_agent.refusal`). A live test skips with this reason, which names no value."""
    refusal = _refusal(ctx)
    if refusal is not None:
        return refusal
    reasons = [roles for entry in ctx.config.spaces if isinstance(roles := _roles(ctx, entry), str)]
    if reasons:
        return reasons[0]
    refused = agent_refusal(ctx)
    if refused is None and unallowed_tools(ctx):
        return f"the tool-server condition does not allow {len(unallowed_tools(ctx))} of the four knowledge tools"
    return refused


@register("provider_outage", "Reads and the knowledge plane while a provider is down", mutates=True)
def provider_outage(ctx: Context) -> Result:
    """Take the data provider of each space down, record the answers, restore it and show it restored (see above).

    The spaces run one after the other, each with its provider brought back before the next starts; if a space's
    provider is not shown back (the injector could not clear the injection, or the provider does not answer REST
    again), no later space is run, since a provider may still be down. The verdict is the worst over the spaces
    (`per_space`)."""
    refusal = _refusal(ctx)
    if refusal is not None:
        return unknown(refusal)
    stuck: list[str] = []

    def one(entry: StudySpace) -> Result:
        if stuck:
            return unknown(f"not run: {stuck[0]} left a provider that could not be shown back")
        result = _outage_in(ctx, entry)
        restore = result.facts.get("restore", {})
        if restore.get("cleared") is False or restore.get("rest_answers_again") is False:
            stuck.append(entry.position)
        return result

    return per_space(ctx, ctx.config.spaces, one)


def _roles(ctx: Context, entry: StudySpace) -> Roles | str:
    """The target, the mirror and the local provider of a space, or why it has no such three."""
    config, target, local = ctx.config, holder_of(ctx, entry), entry.support.local
    if target is None:
        return "the space declares no data provider"
    shown = config.provider_label(target)
    if target == local:
        return (
            f"the space's data provider ({shown}) is the agent's local provider, so there is no outage to read through"
        )
    mirror = next((name for name in entry.support.providers if name not in (target, local)), None)
    if mirror is None:
        return "the support set has no provider besides the data provider and the local one to hold a replica"
    if ctx.fault_settings is None or target not in ctx.fault_settings.services:
        return f"[faults.services] names no service for the data provider ({shown})"
    if any(name not in ctx.provider_ids for name in (target, mirror, local)):
        return "a provider's id is neither declared in platform.toml nor read from the space"
    return Roles.of(config, ctx.provider_ids, target, mirror, local)


def _outage_in(ctx: Context, entry: StudySpace) -> Result:
    """The outage check in one declared space, with the federation held exclusively for the whole of it."""
    roles = _roles(ctx, entry)
    if isinstance(roles, str):
        return unknown(roles)
    settings = ctx.fault_settings
    assert settings is not None
    entered = False
    try:
        with FederationLock(settings).held(
            exclusive=True, timeout_s=settings.admission_timeout_s, target=roles.labels["target"], purpose=OUTAGE_RUN
        ) as admission:
            entered = True
            run = Run(ctx, entry, roles, AgentPlane(ctx, entry, f"day0-outage-{secrets.token_hex(6)}"))
            try:
                asyncio.run(run.main())
            finally:
                run.facts["cleanup_verified"] = clean_up(run.provisioner, run.directory)
            run.facts["admission"] = {"exclusive": admission.exclusive}
            return run.verdict()
    except (InfraError, ConfigError) as error:
        if entered:
            raise
        reason = error.detail if isinstance(error, InfraError) else str(error)
        return unknown(f"the federation could not be held exclusively, so nothing was stopped ({reason})")


__all__ = ["prerequisite", "provider_outage"]
