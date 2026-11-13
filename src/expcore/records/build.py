# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/runner.py:413-431
"""Turn a finished attempt into a record.

`record_attempt` runs one attempt through the loop with the backend wrapped for recording and returns the record.
`build_record` is the same step for a caller that ran the loop itself. The record is built the same way whatever ended
the attempt, so an infrastructure failure writes a partial record: the turns so far, the request that failed, the usage
seen, and no score (R09). Replay builds its own record through this function, so the two are compared like with like.

Exception text is stable (R07): a class name and, only for the core's own exceptions, their fixed fields. The text a
library puts in an exception differs between interpreter versions and would stop a record replaying elsewhere.
"""

import asyncio
import dataclasses
import time
import warnings
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from expcore.config.loading import ConfigError
from expcore.config.models import ModelTable
from expcore.errors import TARGET_LABEL, infra_kinds
from expcore.loop.agent_loop import run_agent
from expcore.loop.config import AgentConfig
from expcore.loop.outcomes import HarnessDefect, Outcome
from expcore.loop.records import LoopResult, TurnRecord
from expcore.loop.timer import AgentTimer
from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import ModelBackend, ModelTurn
from expcore.models.route import sdk_version
from expcore.plugin import ToolProvider, ToolSpec
from expcore.prompt.templates import RenderedPrompt, sha256_of
from expcore.provenance.environment import CORE_DISTRIBUTION
from expcore.records.digest import digest, plain
from expcore.records.injections import injected_targets
from expcore.records.record import RunRecord
from expcore.records.recording import Captured, RecordingBackend
from expcore.records.redact import Redactor
from expcore.records.schema import (
    CURRENT,
    SCHEMAS,
    RecordError,
    SchemaVersion,
    fields_of,
    first_turn_rule,
    optional_in,
    reply_count_rule,
    target_rule,
    unread_replies,
)
from expcore.records.seal import mark_unverified, seal

CONTEXT_OVERFLOW = "ContextOverflow"
INFRA_PREFIX = "infrastructure: "


@dataclass(frozen=True)
class RunIdentity:
    """Who the attempt is: the run key, the factor values it was derived from, and the attempt number."""

    run_key: str
    factors: Mapping[str, Any]
    attempt: int


@dataclass(frozen=True)
class PromptText:
    """A prompt as given to the model, with the template hash and slot values when it was rendered from a template."""

    text: str
    template_sha256: str | None = None
    slots: Mapping[str, str] | None = None

    @classmethod
    def of(cls, prompt: "str | RenderedPrompt | PromptText") -> "PromptText":
        """Accept a plain string, a rendered template or a prompt text."""
        if isinstance(prompt, PromptText):
            return prompt
        if isinstance(prompt, RenderedPrompt):
            return cls(prompt.text, prompt.template_sha256, dict(prompt.slots))
        return cls(prompt)

    def entry(self) -> dict[str, Any]:
        """Return the prompt as the record keeps it."""
        slots = None if self.slots is None else dict(self.slots)
        return {
            "text": self.text,
            "sha256": sha256_of(self.text),
            "template_sha256": self.template_sha256,
            "slots": slots,
        }


def stable_detail(outcome: Outcome) -> str:
    """Return the outcome's detail line without library exception text (R07).

    Caps, malformed endings and infrastructure details are written by the core and kept. For an error the model
    endpoint or a library raised, and for a defect, the detail is the exception class; the message is not stored. A
    context overflow keeps its detail, which holds only the status and the matched pattern.
    """
    if outcome.name == "error":
        return outcome.detail if outcome.error_type == CONTEXT_OVERFLOW else str(outcome.error_type)
    if outcome.name == "harness_defect" and outcome.defect is not None:
        return outcome.defect.exception
    return outcome.detail


def stable_call_detail(status: str, detail: str) -> str:
    """Return a tool call's detail line without library exception text: a crash keeps its class name only (R07)."""
    if status == "failed" and not detail.startswith(INFRA_PREFIX):
        return detail.split(":", 1)[0]
    return detail


def outcome_entry(
    outcome: Outcome,
    treatment_kinds: Collection[str] = (),
    *,
    completed_turns: int,
    first_turn_rule: bool = True,
    target_rule: bool = True,
    treatment_targets: Mapping[str, str] | None = None,
    injected: Collection[str] = (),
    with_target: bool = True,
) -> dict[str, Any]:
    """Return the outcome as the record keeps it.

    `treatment` is `Outcome.is_treatment`: true exactly when the outcome is infrastructure of a kind the attempt's
    condition declares as its treatment, the kind can be one, the model completed at least one turn
    (`completed_turns`, the record's transcript) and, where the declaration names a target, the failure names it and
    the fault injector recorded it as injected (`injected`). Then it is a scored final attempt and the record is not
    set aside. A failure before the first turn is never treatment, except under `first_turn_rule=False`, the rule of
    schema version 1, which a replay of a version-1 record keeps. `with_target` is False for a version before 3, whose
    outcome has no target field. The target is optional in version 3: an outcome that names none has no `infra_target`,
    never a null.
    """
    defect = outcome.defect
    entry = {
        "name": outcome.name,
        "detail": stable_detail(outcome),
        "infra_kind": outcome.infra_kind,
        "error_type": outcome.error_type,
        "defect": None if defect is None else {"exception": defect.exception, "stack_sha256": defect.stack_sha256},
        "treatment": outcome.is_treatment(
            treatment_kinds,
            completed_turns,
            first_turn_rule=first_turn_rule,
            target_rule=target_rule,
            treatment_targets=treatment_targets,
            injected=injected,
        ),
    }
    if outcome.infra_target is not None:
        if not with_target:
            raise RecordError("the outcome names a target, which a record of a version before 3 cannot hold")
        entry["infra_target"] = outcome.infra_target
    return entry


def treatment_list(treatment_kinds: Iterable[str]) -> list[str]:
    """Return the declared treatment kinds sorted, each once; refuse a kind outside the registry (R02)."""
    kinds = sorted(set(treatment_kinds))
    unknown = [k for k in kinds if k not in infra_kinds()]
    if unknown:
        raise RecordError(
            f"unknown infrastructure kind {unknown[0]!r} in treatment_kinds; known: {', '.join(infra_kinds())}"
        )
    return kinds


def target_table(treatment_targets: Mapping[str, str] | None, kinds: Collection[str]) -> dict[str, str]:
    """Return the declared targets sorted by kind; refuse a kind that is not declared and a target that is no label."""
    targets = dict(sorted((treatment_targets or {}).items()))
    stray = [k for k in targets if k not in kinds]
    if stray:
        raise RecordError(f"treatment_targets: {stray[0]!r} is not one of the declared treatment kinds")
    bad = [k for k, t in targets.items() if not isinstance(t, str) or not TARGET_LABEL.fullmatch(t)]
    if bad:
        raise RecordError(f"treatment_targets.{bad[0]}: a target is a position label such as 'providers[1]'")
    return targets


def capped_totals(
    reserved_capped: bool | None,
    reply_cap_unheld: bool | None,
    schema_version: str = CURRENT,
    schemas: Sequence[SchemaVersion] = SCHEMAS,
) -> dict[str, bool]:
    """Return the optional totals that say whether the attempt was held to the reservation R (C27), the ones the run
    supplied, in the order the schema lists them.

    `reserved_capped` is the bound the budget gate reserved the run by (True: the reply maximum, False: the window) and
    `reply_cap_unheld` whether the campaign had taken the model off the reply maximum when the loop ended. None is
    "no gate decided it": the field is left out, never written as a null. Refuse a value that is not a boolean, and one
    for a version that predates the fields, which would be dropped without a word.
    """
    given = {"reserved_capped": reserved_capped, "reply_cap_unheld": reply_cap_unheld}
    for name, value in given.items():
        if value is not None and not isinstance(value, bool):
            raise RecordError(f"totals.{name} is True, False or None, not {value!r}")
        if value is not None and name not in fields_of("totals", schema_version, schemas):
            raise RecordError(f"totals.{name}: {schema_version} predates the field")
    return {name: value for name, value in given.items() if value is not None}


def tool_entry(spec: ToolSpec) -> dict[str, Any]:
    """Return a tool spec as the record keeps it: as the provider gave it, before the cap sentence is added."""
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": plain(spec.input_schema),
        "terminal": spec.terminal,
    }


def call_entry(call: Any) -> dict[str, Any]:
    """Return a tool-call log entry as the record keeps it."""
    entry = plain(dataclasses.asdict(call))
    entry["detail"] = stable_call_detail(entry["status"], entry["detail"])
    entry["rendering_verified"] = True  # until redaction changes what the cap or the error text is computed from
    return entry


def usage_entry(turn: ModelTurn, schema_version: str) -> dict[str, Any]:
    """Return the usage of one turn as a record of `schema_version` keeps it: without a field a later version added,
    and without an optional field the turn did not report (None), which stays absent and is never written as a null or
    a zero."""
    usage = {
        "input_tokens": turn.input_tokens,
        "output_tokens": turn.output_tokens,
        "uncached_input_tokens": turn.uncached_input_tokens,
        "cache_creation_input_tokens": turn.cache_creation_input_tokens,
        "cache_read_input_tokens": turn.cache_read_input_tokens,
        "extra": None if turn.usage_extra is None else dict(turn.usage_extra),
        "extra_cut": turn.usage_extra_cut,
    }
    held, optional = fields_of("usage", schema_version), optional_in("usage", schema_version)
    return {name: v for name, v in usage.items() if name in held and not (v is None and name in optional)}


def turn_entry(
    index: int, captured: Captured, settings_sha256: str, loop_turn: TurnRecord, schema_version: str = CURRENT
) -> dict[str, Any]:
    """Return one model turn as the record keeps it: the neutral and the native message, and what surrounded them."""
    turn = captured.turn
    return {
        "index": index,
        "neutral": {
            "text": turn.text,
            "reasoning": turn.reasoning,
            "tool_calls": [plain(dataclasses.asdict(c)) for c in turn.tool_calls],
        },
        "native": captured.native,
        "tool_messages": captured.tool_messages,
        "request_sha256": "",  # filled in by `seal`, over the redacted conversation
        "settings_sha256": settings_sha256,
        "stop_reason": turn.stop_reason,
        "usage": usage_entry(turn, schema_version),
        "latency_s": turn.latency_s,
        "served_model": turn.served_model,
        "system_fingerprint": turn.system_fingerprint,
        "retries": plain(list(turn.retries)),
        "waits_left_open": loop_turn.waits_left_open,
        "infra_wait_by_kind": plain(loop_turn.infra_wait_by_kind),
    }


def route_entry(info: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    """Return the route section: the backend's description with each distinct settings set kept once, by digest.

    `info["settings_sent"]` (the settings of the latest request) is replaced by the digest of that set. Every turn
    names its own set by digest, so a setting that changes in the middle of a run stays visible.
    """
    rest = {k: v for k, v in plain(dict(info)).items() if k != "settings_sent"}
    latest = plain(info.get("settings_sent", {}))
    sets = {**plain(dict(settings))}
    sets.setdefault(digest(latest), latest)
    return {**rest, "settings_sent_sha256": digest(latest), "settings": sets}


def info_of_route(route: Mapping[str, Any]) -> dict[str, Any]:
    """Return what `backend.info()` returned for a route section: the inverse of `route_entry`."""
    rest = {k: v for k, v in route.items() if k not in ("settings", "settings_sent_sha256")}
    return {**rest, "settings_sent": route["settings"].get(route["settings_sent_sha256"], {})}


def model_secrets(
    backend: Any, models: ModelTable | None, environ: Mapping[str, str] | None
) -> tuple[list[str], list[str]]:
    """Return the names of the key variables of the models in use, and the keys themselves.

    The models in use are the backend's own (the adapters hold their entry as `spec`) and, when given, every entry of
    the model table. A key is read the way the backend reads it, so one kept in a file is found too.
    """
    specs = [spec] if (spec := getattr(backend, "spec", None)) is not None else []
    specs += [models.resolve(key) for key in models] if models is not None else []
    names = [s.key_env for s in specs if getattr(s, "key_env", None)]
    values = []
    for s in specs:
        try:
            value = s.read_key(environ)
        except ConfigError:
            continue  # no key is set for that entry: nothing of it to redact
        if value:
            values.append(value)
    return names, values


def redactor_for(
    backend: Any,
    *,
    environ: Mapping[str, str] | None = None,
    secrets: Iterable[str] = (),
    secret_env: Iterable[str] = (),
    models: ModelTable | None = None,
    short_tokens: bool = False,
) -> Redactor:
    """Build the redactor of a record: the environment's secrets, the variables named in `secret_env`, the key
    variables of the models in use (added automatically), the registered secrets and `secrets`.

    Raises ConfigError, naming the variable, when a secret is too short to redact (see `expcore.records.redact`);
    `short_tokens` replaces such a secret where it stands as a token of its own instead, for a diagnostic on the
    author's terminal (`Redactor.from_environment`).
    """
    secrets, secret_env = tuple(secrets), tuple(secret_env)  # whatever iterable the caller gave is read once
    names, keys = model_secrets(backend, models, environ)
    return Redactor.from_environment(environ, [*secrets, *keys], [*secret_env, *names], short_tokens=short_tokens)


def expected_conversation(system: str, user: str, captured: Sequence[Captured]) -> list[dict[str, Any]]:
    """Return the conversation the pieces add up to: system (when given), user, then each turn and its results."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": user})
    for c in captured:
        messages += [c.native, *c.tool_messages]
    return messages


def _build_record(
    result: LoopResult,
    recording: RecordingBackend,
    *,
    identity: RunIdentity,
    system: "str | RenderedPrompt | PromptText",
    user: "str | RenderedPrompt | PromptText",
    specs: Sequence[ToolSpec],
    code: Mapping[str, Any],
    core_version: str | None = None,
    instruments: Mapping[str, Any] | None = None,
    treatment_kinds: Iterable[str] = (),
    treatment_targets: Mapping[str, str] | None = None,
    schema_version: str = CURRENT,
    schemas: Sequence[SchemaVersion] = SCHEMAS,
    redactor: Redactor | None = None,
    secrets: Iterable[str] = (),
    secret_env: Iterable[str] = (),
    models: ModelTable | None = None,
    environ: Mapping[str, str] | None = None,
    instrument_refusal: Mapping[str, Any] | None = None,
    reserved_capped: bool | None = None,
    reply_cap_unheld: bool | None = None,
) -> RunRecord:
    """Build the record of a finished attempt from the loop's result and the recording wrapper it ran through.

    Raises RecordError when the pieces do not add up: a different number of replies than the loop counted, or a
    conversation that is not the one the pieces make, which means the backend's history is not the plain
    system, user, assistant, tool-results sequence the replay serves.

    `treatment_kinds` are the infrastructure kinds the attempt's condition declares as its treatment; the campaign
    passes `FactorSchema.treatment_kinds_of(key)`. They are kept in the record's identity, and the outcome's `treatment`
    flag is derived from them: an infrastructure failure of a listed kind, after the model completed a turn, is a scored
    final attempt, not set aside. `treatment_targets` map a declared kind to the target the condition manipulates
    (`FactorSchema.treatment_targets`): a failure of that kind is treatment only if it names that target and the fault
    injector's reading among `instruments` records the target as injected in this attempt.

    `reserved_capped` and `reply_cap_unheld` are the two optional totals that say whether the attempt was held to the
    reservation R (`capped_totals`); None leaves one out, and a replay serves back what the record holds.

    The record is written as `schema_version`, and its outcome's treatment flag follows the rules of that version in
    `schemas`. Private: only a replay of an older record (its own version) and tests building older records choose a
    version; a live run goes through `build_record`, which always writes the current one. The loop's record usually
    has no instruments yet; `RunRecord.with_instrument` derives the flag again when they are added.

    Secrets are redacted here, before any digest is taken: the digests the record pins (prompt texts, the
    conversation each request carried, the settings each sent, the configuration) are those of the redacted
    data, which is what is stored and replayed, and `redacted_paths` lists where something was replaced.
    The secrets are those of `redactor_for` (the environment's, `environ` being the process's by default, the
    variables in `secret_env`, the key variables of the models in use, the registered ones, and `secrets` for this
    record) unless a `redactor` is given. A replay passes `Redactor.none()`: what it reads is redacted already.
    """
    secrets, secret_env = tuple(secrets), tuple(secret_env)
    kinds = treatment_list(treatment_kinds)
    targets = target_table(treatment_targets, kinds)
    if targets and not target_rule(schema_version, schemas):
        raise RecordError(f"treatment_targets: {schema_version} predates declared targets")
    sys_prompt, user_prompt = PromptText.of(system), PromptText.of(user)
    captured = recording.captured
    if len(captured) != len(result.turns):
        raise RecordError(f"the loop counted {len(result.turns)} replies but the recording holds {len(captured)}")
    if expected_conversation(sys_prompt.text, user_prompt.text, captured) != result.transcript:
        raise RecordError(
            "the backend's conversation is not the system and user prompts followed by the recorded turns"
        )
    sets: dict[str, Any] = {}
    turns = []
    for index, c in enumerate(captured):
        turns.append(turn_entry(index, c, digest(c.settings), result.turns[index], schema_version))
        sets[digest(c.settings)] = c.settings
    failed = None
    if recording.failure is not None:
        failed = dict(recording.failure)
        sets[failed["settings_sha256"]] = recording.failure_settings
    config = plain(result.config)
    fields = {
        "run_key": identity.run_key,
        "factors": plain(dict(identity.factors)),
        "attempt": identity.attempt,
        "treatment_kinds": kinds,
        "route": route_entry(result.backend_info, sets),
        "prompt": {
            "system": sys_prompt.entry(),
            "user": user_prompt.entry(),
            "tools_sha256": result.tool_manifest_sha256,
        },
        "tools": [tool_entry(s) for s in specs],
        "agent_config": config,
        "agent_config_sha256": result.config_sha256,
        "code": plain(dict(code)),
        "core_version": core_version,
        "transcript": turns,
        "failed_request": failed,
        "tool_calls": [call_entry(c) for c in result.tool_calls],
        "clock": [[label, value] for label, value in result.clock_readings],
        "outcome": outcome_entry(
            result.outcome,
            kinds,
            completed_turns=len(turns),
            first_turn_rule=first_turn_rule(schema_version, schemas),
            target_rule=target_rule(schema_version, schemas),
            treatment_targets=targets,
            injected=injected_targets(instruments or {}, identity.run_key, identity.attempt),
            with_target="infra_target" in fields_of("outcome", schema_version, schemas),
        ),
        "totals": {
            "rounds": result.rounds,
            "malformed_calls": result.malformed_calls,
            "malformed_events": plain(result.malformed_events),
            "tokens_used": result.tokens_used,
            "usage_incomplete": result.usage_incomplete
            or (reply_count_rule(schema_version) and unread_replies(result.backend_info.get("usage_seen"), len(turns))),
            "agent_s": result.agent_s,
            "elapsed_s": result.elapsed_s,
            "infra_wait_s": result.infra_wait_s,
            "infra_wait_by_kind": plain(result.infra_wait_by_kind),
            "cleanup_s": result.cleanup_s,
            "cleanup_timed_out": result.cleanup_timed_out,
            "abandoned_threads": result.abandoned_threads,
            "final_text": result.final_text,
            **capped_totals(reserved_capped, reply_cap_unheld, schema_version, schemas),
        },
        "instruments": plain(dict(instruments or {})),
        "scores": {},
        "treatment_targets": targets,
    }
    if instrument_refusal is not None:  # optional: written only when there is one, served back by a replay
        fields["instrument_refusal"] = plain(dict(instrument_refusal))
    if redactor is None:
        redactor = redactor_for(recording.inner, environ=environ, secrets=secrets, secret_env=secret_env, models=models)
    redacted, paths = redactor.structure(fields)
    mark_unverified(redacted, paths)
    if paths:
        warnings.warn(
            f"record of attempt {identity.attempt}: redacted secrets at {len(paths)} place(s)",
            RuntimeWarning,
            stacklevel=2,
        )
    return RunRecord(**seal(redacted), redacted_paths=paths, schema_version=schema_version)


def build_record(result: LoopResult, recording: RecordingBackend, **options: Any) -> RunRecord:
    """Build the record of a finished attempt, always as the current schema version (see `_build_record`).

    A live run cannot choose an older record version: that would apply an older treatment rule to a new run. The
    keyword options are those of `_build_record` (`identity`, `system`, `user`, `specs`, `code`, `core_version`,
    `instruments`, `instrument_refusal`, `treatment_kinds`, `treatment_targets`, `reserved_capped`, `reply_cap_unheld`,
    `redactor`, `secrets`, `secret_env`, `models`, `environ`); `schema_version` is refused.
    """
    if "schema_version" in options:
        raise TypeError("build_record writes the current record version; a live run cannot choose another")
    return _build_record(result, recording, **options)


class DefectSink(Protocol):
    """Where the traceback of a harness defect goes (the campaign's `defect_log.DefectLog`). `record_attempt` hands it
    the attempt's whole redaction context first, and calls it with each crash the loop turns into a defect. Calling it
    should not raise, since the loop calls it before the record exists; if it does, the loop carries the exception
    forward and `record_attempt` hands it to `on_sink_failure` once the record is built."""

    def redact_with(self, redactor: Callable[[], Redactor]) -> None:
        """Use this redactor, built when an entry is written, for the attempt's entries from now on."""
        ...

    def __call__(self, error: BaseException, defect: HarnessDefect) -> object:
        """Write the redacted traceback of a crash the loop caught, with the defect its outcome carries."""
        ...


async def _record_attempt(
    backend: ModelBackend,
    providers: Sequence[ToolProvider],
    system: "str | RenderedPrompt | PromptText",
    user: "str | RenderedPrompt | PromptText",
    config: AgentConfig,
    identity: RunIdentity,
    *,
    code: Mapping[str, Any],
    core_version: str | None = None,
    treatment_kinds: Iterable[str] = (),
    treatment_targets: Mapping[str, str] | None = None,
    schema_version: str = CURRENT,
    timer: AgentTimer | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    secrets: Iterable[str] = (),
    secret_env: Iterable[str] = (),
    models: ModelTable | None = None,
    environ: Mapping[str, str] | None = None,
    defect_log: DefectSink | None = None,
    on_sink_failure: Callable[[Exception], object] | None = None,
    reserved_capped: bool | None = None,
    reply_cap_unheld: Callable[[], bool | None] | None = None,
) -> RunRecord:
    """Run one attempt through the loop, recording it as `schema_version`, and return its record.

    Private: live runs go through `record_attempt`, which always writes the current version. Only tests building
    records of an older version call this directly.

    The tool box takes its cap from the configuration, so the record's configuration describes what the model saw.
    `code` is the code identity, for example `expcore.provenance.codestamp.version(root, scope)`, and `core_version`
    defaults to the installed version of the core. `treatment_kinds` are the infrastructure kinds the condition declares
    as its treatment, and `treatment_targets` the target it declares for any of them (see `build_record`). The caller
    builds the backend (passing it `timer.time_left` when it uses the loop's time) and closes it.

    Secrets are redacted from the record (R12): those of environment variables named like one, the variables
    named in `secret_env`, the key variables of the backend's model and of the `models` table, the ones registered
    with `expcore.records.redact.register_secret` (a platform that mints a token registers it), and `secrets`.
    A secret too short to redact raises ConfigError before the run starts. The secrets are read again when the record
    is built, after the run, so one registered during the run (a token a provider minted) is redacted too.

    `defect_log` (the campaign passes one through `record_run`) gets the same redaction context before anything can
    crash, so a traceback in it is redacted with these secrets too, and each crash the loop turns into a harness defect.
    What the sink raises is handed to `on_sink_failure` once the record is built (`record_run` passes the attempt's
    log, which reports it once the result is stored), or, with no one to hand it to, warned of.

    `reserved_capped` is the bound the budget gate reserved the run by, known when it was admitted; `reply_cap_unheld`
    is read when the loop has ended, in a worker thread, since the campaign's mark of a model that is not held to the
    reply maximum can change while the attempt runs. Both come from the request (`record_run`), and None leaves a total
    out (C27).
    """
    secrets, secret_env = tuple(secrets), tuple(secret_env)  # read once: the early check would use up an iterator

    def redactor() -> Redactor:
        return redactor_for(backend, environ=environ, secrets=secrets, secret_env=secret_env, models=models)

    if defect_log is not None:
        defect_log.redact_with(redactor)
    redactor()  # fail early
    recording = RecordingBackend(backend)
    box = ToolBox(list(providers), cap_chars=config.limits.output_cap_chars)
    specs = [spec for provider in providers for spec in provider.tools()]
    prompts = (PromptText.of(system), PromptText.of(user))
    result = await run_agent(
        recording,
        box,
        prompts[0].text,
        prompts[1].text,
        config,
        timer=timer,
        clock=clock,
        sleep=sleep,
        on_defect=defect_log,
    )
    unheld = None if reply_cap_unheld is None else await asyncio.to_thread(reply_cap_unheld)
    record = _build_record(
        result,
        recording,
        identity=identity,
        system=prompts[0],
        user=prompts[1],
        specs=specs,
        code=code,
        core_version=core_version if core_version is not None else sdk_version(CORE_DISTRIBUTION),
        treatment_kinds=treatment_kinds,
        treatment_targets=treatment_targets,
        schema_version=schema_version,
        secrets=secrets,
        secret_env=secret_env,
        models=models,
        environ=environ,
        reserved_capped=reserved_capped,
        reply_cap_unheld=unheld,
    )
    if result.sink_failure is not None:  # the record exists: now the sink's failure can be reported
        if on_sink_failure is None:
            warnings.warn(f"the defect sink failed: {type(result.sink_failure).__name__}", RuntimeWarning, stacklevel=2)
        else:
            on_sink_failure(result.sink_failure)
    return record


async def record_attempt(
    backend: ModelBackend,
    providers: Sequence[ToolProvider],
    system: "str | RenderedPrompt | PromptText",
    user: "str | RenderedPrompt | PromptText",
    config: AgentConfig,
    identity: RunIdentity,
    **options: Any,
) -> RunRecord:
    """Run one attempt through the loop, recording it, and return its record, always as the current schema version.

    A live run cannot choose an older record version: that would apply an older treatment rule to a new run.
    The keyword options are those of the private `_record_attempt` (`code`, `core_version`, `treatment_kinds`,
    `treatment_targets`, `timer`, `clock`, `sleep`, `secrets`, `secret_env`, `models`, `environ`, `defect_log`,
    `on_sink_failure`, `reserved_capped`, `reply_cap_unheld`); `schema_version` is refused.
    """
    if "schema_version" in options:
        raise TypeError("record_attempt writes the current record version; a live run cannot choose another")
    return await _record_attempt(backend, providers, system, user, config, identity, **options)
