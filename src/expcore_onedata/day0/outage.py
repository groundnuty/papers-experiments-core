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

**Before the outage** (all providers up): the baseline reads, through the target, the mirror and, cold, the local
provider (REST, and the mount if the run has one), and the scratch directory's listing. A baseline that does not return
what was written means nothing that follows could be attributed, so the check is unknown and nothing is stopped.

**During it**, through the local provider: a read of a replicated file, a read of a file held by the target alone, a
listing of the scratch directory; the same through the mount when the platform declares one and the run has a reader;
and the knowledge plane, as the REST calls its tools make (`get_file_distribution`, `list_space_providers` as Onezone's
online flag of each provider, `schedule_file_replication` to the local provider, `get_transfer` read until it ends or
the bound). The tool server itself is not launched: its wording of an error is not measured here. Every cell has the
shape of `outage_cells`: status and error id, seconds against the bound, and an outcome (`served`, `partial`,
`differs`, `refused`, `no answer`, `blocked`). Blocking is bounded by `Context.outage_bound_s`.

**After it**: the restore, by the fault injector, verified by the identity of the service having changed, and the target
answering again, then the reads again, which must return what was written. The restore runs in a `finally`; the
scratch is torn down after it, since teardown deletes through the target, and its absence is verified (O11).

**The injector (O10, O11).** The provider is taken down and brought back by the platform's fault injector
(`outage_fault`, over `expcore_onedata.faults.FaultInjector`): journalled before the stop, recovered if an earlier
process left it open, restored only with a changed identity, a stop of unknown outcome waited out. The check holds the
federation's lock exclusively (`admission.FederationLock`) for a whole space's scenario, the scratch's teardown
included, and the injector takes that admission as its own.

**Verdict.** Passed: the target was seen down and restored, every cell measured, the re-reads right, the scratch gone.
Failed: the target answered while it was down, it could not be restored or did not answer or read back after the
restore, the scratch was not shown gone, or a measurement raised. A space whose provider is not shown back (not
cleared, or not answering REST again) halts the spaces after it. Unknown: anything the check could not do or measure,
with the reason. What a cell answered is a fact, never a failure.

**Consent (O10).** Nothing is stopped without `EXPCORE_LIVE_MUTATE=1`, `EXPCORE_LIVE_FAULTS=1` and `sanctioned = true`
in `[faults]` of platform.toml. **Names (O12).** Providers are `providers[i]` in every key and message, spaces
`spaces[i]`, files the pair and kind they are, and nothing identifying is recorded.
"""

import hashlib
import secrets
from typing import Any

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata import consistency
from expcore_onedata.admission import OUTAGE_RUN, FederationLock
from expcore_onedata.client import PlatformError
from expcore_onedata.config import StudySpace
from expcore_onedata.day0.mutating import clean_up
from expcore_onedata.day0.outage_cells import ROLES, Cells, Roles, ScratchFile
from expcore_onedata.day0.outage_fault import RESTORED, Outage
from expcore_onedata.day0.registry import Context, Result, failed, holder_of, passed, per_space, register, unknown
from expcore_onedata.faults import FaultInjector
from expcore_onedata.placement import held_bytes
from expcore_onedata.provision import ProvisionError
from expcore_onedata.sandbox.settings import DataPathMode

FILE_BYTES = 16384
KINDS = ("replicated", "owner_only")
BASE_PAIRS = ("baseline", "rest", "knowledge")
MOUNT_PAIRS = ("baseline_mount", "mount")
DOWN_PROBE_S = 10.0  # the longest the check waits to see that the target does not answer


class Unprepared(RuntimeError):
    """The scenario could not be set up or proved, so nothing was taken down."""


def payload(label: str) -> bytes:
    """Deterministic bytes, different for every file, so a read of another file never has the hash of this one."""
    out = bytearray()
    block = 0
    while len(out) < FILE_BYTES:
        out += hashlib.sha256(f"{label}:{block}".encode()).digest()
        block += 1
    return bytes(out[:FILE_BYTES])


def mount_declared(ctx: Context) -> bool:
    """Whether platform.toml declares a data path that is a mount."""
    sandbox = ctx.config.sandbox
    return sandbox is not None and DataPathMode.MOUNT in sandbox.data_paths.values()


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
    """What a platform.toml and a run still lack for the check to start, or None: the refusals the check makes, and
    for each space the three providers it needs. A live test skips with this reason, which names no value."""
    refusal = _refusal(ctx)
    if refusal is not None:
        return refusal
    reasons = [roles for entry in ctx.config.spaces if isinstance(roles := _roles(ctx, entry), str)]
    return reasons[0] if reasons else None


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
            run = Run(ctx, entry, roles)
            try:
                run.execute()
            finally:
                run.facts["cleanup_verified"] = clean_up(run.provisioner, run.directory)
            run.facts["admission"] = {"exclusive": admission.exclusive}
            return run.verdict()
    except (InfraError, ConfigError) as error:
        if entered:
            raise
        reason = error.detail if isinstance(error, InfraError) else str(error)
        return unknown(f"the federation could not be held exclusively, so nothing was stopped ({reason})")


class Run:
    """One space's outage: the scenario's files, the facts gathered, and what went wrong or could not be measured."""

    def __init__(self, ctx: Context, entry: StudySpace, roles: Roles) -> None:
        self.ctx, self.entry, self.roles = ctx, entry, roles
        self.provisioner = ctx.provisioner(entry.label)
        self.bound_s = ctx.outage_bound_s
        self.directory = f"day0-outage-{secrets.token_hex(6)}"
        self.directory_id = ""
        self.files: dict[tuple[str, str], ScratchFile] = {}
        self.pairs = list(BASE_PAIRS)
        self.problems: list[str] = []  # what failed
        self.gaps: list[str] = []  # what could not be measured
        self.facts: dict[str, Any] = {"roles": dict(roles.labels), "bound_s": self.bound_s, "bytes": FILE_BYTES}
        self.cells = Cells(
            ctx.client,
            entry,
            self.provisioner,
            roles,
            bound_s=self.bound_s,
            clock=ctx.clock,
            sleep=ctx.sleep,
            mount=ctx.mount,
        )
        self._mount_on = False

    # -- the run ------------------------------------------------------------------------------------------------------
    def execute(self) -> None:
        """Prepare and baseline, take the target down, measure, restore it, then check the restore and read again."""
        self.facts["mount"] = self._mount_plan()
        try:
            self.prepare()
        except (ProvisionError, PlatformError, consistency.WaitTimeout, KeyError, Unprepared) as error:
            reason = str(error) if isinstance(error, Unprepared) else type(error).__name__
            self.gaps.append(f"nothing was taken down: the scenario could not be set up or proved ({reason})")
            return
        ctx = self.ctx
        assert ctx.fault_settings is not None
        assert ctx.fault_driver is not None
        injector = FaultInjector(
            ctx.config,
            ctx.fault_settings,
            self.roles.labels["target"],
            sanctioned=ctx.allow_faults and ctx.allow_mutation,  # the caller's half of the sanction (O10)
            driver=ctx.fault_driver,
            clock=ctx.clock,
            sleep=ctx.sleep,
        )
        outage = Outage(injector, self.directory)
        went_down = False
        try:
            went_down = self.take_down(outage)
            if went_down:
                self.during()
        except Exception as error:  # a measurement that breaks is a failure, never an escape past the restore
            self.problems.append(f"a measurement raised {type(error).__name__}")
        finally:
            self.facts["restore"] = outage.restore()
        self.after_restore(went_down)

    def _mount_plan(self) -> dict[str, Any]:
        if not mount_declared(self.ctx):
            return {"declared": False}
        if self.ctx.mount is None:
            self.gaps.append("the platform declares a mount but this run has no reader of it, so it was not measured")
            return {"declared": True, "measured": False, "reason": "this run has no reader of the mount"}
        self._mount_on = True
        self.pairs += MOUNT_PAIRS
        return {"declared": True, "measured": True}

    # -- before ---------------------------------------------------------------------------------------------------
    def prepare(self) -> None:
        """Make the files, place the replicas, prove who holds what and read the baseline."""
        roles, prov = self.roles, self.provisioner
        prov.verify_space()
        for pair in self.pairs:
            for kind in KINDS:
                path = f"{self.directory}/{pair}-{kind}.bin"
                data = payload(f"{pair}:{kind}")
                self.files[(pair, kind)] = ScratchFile(pair, kind, path, prov.put_file(path, data), data)
        self.directory_id = prov.resolve(self.directory).file_id
        prov.await_visible(roles.local, [f.file_id for f in self.files.values()])
        for pair in self.pairs:
            prov.replicate(self.files[(pair, "replicated")].path, roles.mirror)
        self.prove_holdings()
        self.baseline()

    @property
    def expected(self) -> dict[str, int]:
        """The entries of the scratch directory: every file's name and size."""
        return {f.name: len(f.data) for f in self.files.values()}

    def prove_holdings(self) -> None:
        """Read, through the local provider, who holds how much of each file; refuse a scenario that is not as built."""
        roles, rows, wrong = self.roles, {}, []
        for file in self.files.values():
            held = {
                role: held_bytes(self.provisioner.blocks(file.file_id, roles.name(role), reader=roles.local))
                for role in ROLES
            }
            size = len(file.data)
            if held != {"target": size, "mirror": size if file.kind == "replicated" else 0, "local": 0}:
                wrong.append(file.key)
            rows[file.key] = {roles.labels[role]: held[role] for role in ROLES}
        self.facts["held_before"] = rows
        if wrong:
            raise Unprepared(f"{len(wrong)} file(s) are not held as the scenario needs before the outage")

    def baseline(self) -> None:
        """Read the baseline pair with everything up, through each provider that may hold it, and list the directory."""
        cells, files, roles = self.cells, self.files, self.roles
        owner_only, replicated = files[("baseline", "owner_only")], files[("baseline", "replicated")]
        rows = {
            "rest": {
                "target_owner_only": cells.read(owner_only, roles.target),
                "mirror_replicated": cells.read(replicated, roles.mirror),
                "local_owner_only": cells.read(owner_only),
                "local_replicated": cells.read(replicated),
                "listing": cells.listing(self.directory_id, self.expected),
            }
        }
        if self._mount_on:
            rows["mount"] = {
                "local_owner_only": cells.read_mount(files[("baseline_mount", "owner_only")]),
                "local_replicated": cells.read_mount(files[("baseline_mount", "replicated")]),
                "listing": cells.listing_mount(self.directory, self.expected),
            }
        self.facts["baseline"] = rows
        short = [
            f"{path}/{name}" for path, cell in rows.items() for name, row in cell.items() if row["outcome"] != "served"
        ]
        if short:
            raise Unprepared(f"the baseline did not read back everything it was given ({len(short)} cell(s))")

    # -- during ------------------------------------------------------------------------------------
    def take_down(self, outage: Outage) -> bool:
        """Stop the target through the injector and see it down; True when it was seen down. The target must not
        answer REST either."""
        down = outage.take_down()
        if not down["started"]:
            self.facts["outage"] = down
            self.gaps.append(f"the provider could not be taken down and seen down ({down['reason']})")
            return False
        probe = self.cells.target_answers(min(self.bound_s, DOWN_PROBE_S))
        self.facts["outage"] = {**down, "rest_answers_while_down": probe["answers"], "rest": probe}
        if probe["answers"]:
            self.problems.append(
                "the provider answered REST while its service was seen down, so no outage was observed"
            )
        return not probe["answers"]

    def during(self) -> None:
        """Measure, through the local provider, each way of reaching the data and the knowledge plane."""
        cells, files = self.cells, self.files
        first_read = cells.providers_online(until_target_offline=False)
        rest = {
            "replicated": cells.read(files[("rest", "replicated")]),
            "owner_only": cells.read(files[("rest", "owner_only")]),
            "listing": cells.listing(self.directory_id, self.expected),
        }
        during: dict[str, Any] = {"rest": rest}
        if self._mount_on:
            during["mount"] = {
                "replicated": cells.read_mount(files[("mount", "replicated")]),
                "owner_only": cells.read_mount(files[("mount", "owner_only")]),
                "listing": cells.listing_mount(self.directory, self.expected),
            }
        knowledge = {
            "get_file_distribution": {
                file.key: cells.distribution(file)
                for file in files.values()
                if file.pair in ("rest", "mount", "knowledge")
            },
            "list_space_providers": {"first": first_read},
        }
        for kind in KINDS:
            knowledge.setdefault("schedule_file_replication", {})[kind] = cells.replicate_to_local(
                files[("knowledge", kind)]
            )
        knowledge["list_space_providers"]["until_target_offline"] = cells.providers_online(until_target_offline=True)
        during["knowledge"] = knowledge
        self.facts["during"] = during

    # -- after -------------------------------------------------------------------------------------
    def after_restore(self, went_down: bool) -> None:
        """Check the restore: cleared and verified by the injector, the target answering, and the reads returning what
        was written. A provider never seen down needs only to be found as it was (`never_down`)."""
        restore = self.facts["restore"]
        if restore["how"] == "not_injected":
            return
        if not restore["cleared"]:
            self.problems.append(f"the provider could not be brought back ({restore['how']})")
            return
        if went_down and restore["how"] not in RESTORED:
            self.problems.append(f"the provider could not be restored and verified ({restore['how']})")
            return
        if not self.wait_for_target():
            self.problems.append("the provider ran again but did not answer REST within the settle timeout")
            return
        if not went_down:
            return
        after = {
            "owner_only": self.reread(lambda: self.cells.read(self.files[("rest", "owner_only")])),
            "replicated": self.reread(lambda: self.cells.read(self.files[("rest", "replicated")])),
            "listing": self.reread(lambda: self.cells.listing(self.directory_id, self.expected)),
        }
        if self._mount_on:
            after["mount_owner_only"] = self.reread(lambda: self.cells.read_mount(self.files[("mount", "owner_only")]))
        after["transfers"] = {kind: self.cells.transfer_now(self.files[("knowledge", kind)]) for kind in KINDS}
        self.facts["after"] = after
        wrong = [name for name, row in after.items() if name != "transfers" and row["outcome"] != "served"]
        if wrong:
            self.problems.append(f"after the restore {len(wrong)} read(s) did not return what was written")

    def wait_for_target(self) -> bool:
        """Poll the target's REST until it answers, within the settle timeout; record how long it took."""
        settings = self.ctx.fault_settings
        assert settings is not None
        began, tries = self.ctx.clock(), 0
        while True:
            tries += 1
            if self.cells.target_answers(min(self.bound_s, DOWN_PROBE_S))["answers"]:
                self.facts["restore"].update(
                    rest_answers_again=True, rest_answered_after_s=round(self.ctx.clock() - began, 3), polls=tries
                )
                return True
            if self.ctx.clock() - began >= settings.settle_timeout_s:
                self.facts["restore"].update(rest_answers_again=False, polls=tries)
                return False
            self.ctx.sleep(settings.poll_s)

    def reread(self, read: Any) -> dict[str, Any]:
        """Read again until the read is served, within the settle timeout (a provider is slow to take up its peers)."""
        settings = self.ctx.fault_settings
        assert settings is not None
        began, tries = self.ctx.clock(), 0
        while True:
            tries += 1
            row = read()
            if row["outcome"] == "served" or self.ctx.clock() - began >= settings.settle_timeout_s:
                return {**row, "attempts": tries}
            self.ctx.sleep(settings.poll_s)

    # -- the verdict -------------------------------------------------------------------------------
    def verdict(self) -> Result:
        """The check's result: failed for a problem, unknown for a gap, else passed with what was observed."""
        if self.facts.get("cleanup_verified") is not True:
            self.problems.append("the scratch it made was not shown gone (O11)")
        if self.problems:
            return failed("; ".join(self.problems), **self.facts)
        if self.gaps:
            return unknown("; ".join(self.gaps), **self.facts)
        return passed(self.summary(), **self.facts)

    def summary(self) -> str:
        """One line of what the outage did, in the vocabulary of the cells."""
        rest = self.facts["during"]["rest"]
        target = self.roles.labels["target"]
        return (
            f"{target} was taken down, seen down and restored; through the local provider a file held only there was "
            f"{rest['owner_only']['outcome']}, a replicated file {rest['replicated']['outcome']}, the directory "
            f"listing {rest['listing']['outcome']}; the scratch was removed"
        )


__all__ = ["Run", "provider_outage"]
