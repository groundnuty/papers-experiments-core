# Provenance: new
"""What the scheduler hands a study's executor, and how an executor's record becomes the store's result (C14).

The scheduler owns everything around an attempt: the gates, the lease, the timer, the endpoint and the store. The study
or platform owns what happens inside it: the prompts, the tools and the sandbox. It supplies a `RunExecutor`, an async
callable that receives a `RunRequest` and returns the run's record, normally by calling `record_run`, which passes the
request's backend, timer and treatment kinds to `record_attempt`.

The backend in the request is built by the scheduler from the endpoint the campaign admitted (`Endpoint`), the same
one admission checked. The executor never builds a backend of its own and never resolves a URL again: a run whose
record names another endpoint than the one admitted is the fault C14 describes.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from expcore.campaign.lease import Lease
from expcore.campaign.markers import AttemptResult
from expcore.config.models import Limits, ModelSpec, ModelTable
from expcore.errors import FirstTurnRefusal, InfraError
from expcore.loop.config import AgentConfig
from expcore.loop.outcomes import Outcome, harness_defect_of
from expcore.loop.timer import AgentTimer
from expcore.models.adapters import ModelBackend
from expcore.models.factory import build_backend
from expcore.plugin import ToolProvider
from expcore.prompt.templates import RenderedPrompt
from expcore.records.build import DefectSink, PromptText, RunIdentity, record_attempt
from expcore.records.injections import injected_targets
from expcore.records.record import RunRecord
from expcore.records.schema import first_turn_rule, outcome_from_entry, target_rule, unread_replies


@dataclass(frozen=True)
class Endpoint:
    """The endpoint a campaign admitted for a model: where it is reached and which concurrency unit it counts in.

    `base_url` is the campaign's own choice and replaces the model entry's (None keeps the entry's). `unit` is any
    key a platform names (an endpoint, a provider): caps and health are kept per unit.
    """

    model: str
    unit: str
    base_url: str | None = None


class BackendFactory(Protocol):
    """Build the backend of one run from an admitted endpoint: the same call for admission and for every run."""

    def __call__(
        self,
        spec: ModelSpec,
        *,
        seed: int,
        limits: Limits,
        base_url: str | None,
        time_left: Callable[[], float | None] | None,
    ) -> ModelBackend:
        """Return a backend for the entry, sending to `base_url` when given."""
        ...


def default_backend_factory(
    spec: ModelSpec,
    *,
    seed: int,
    limits: Limits,
    base_url: str | None,
    time_left: Callable[[], float | None] | None,
) -> ModelBackend:
    """Build the backend a model entry names; refuses a model that is not frozen (B08), and honours the URL (C14)."""
    return build_backend(spec, seed=seed, limits=limits, base_url=base_url, time_left=time_left)


@dataclass(frozen=True)
class RunRequest:
    """One attempt, as the executor sees it.

    - `identity`: the run key, factor values and attempt number, for the record.
    - `backend`: built from the admitted endpoint for this run's seed; the scheduler closes it.
    - `timer`: the attempt's timer, not started; the loop starts it. Pass it to `record_attempt`.
    - `treatment_kinds`: the infrastructure kinds the run's condition declares as its treatment. The record must carry
      exactly these, or the store refuses the result.
    - `lease`: the lease the platform's resources require, held for the whole attempt; None without one.
    - `treatment_targets`: the target the condition declares for any of its treatment kinds (a position label such as
      `providers[1]`), which the run's fault injector takes down; the record must carry exactly these too.
    - `models`: the study's whole model table. `record_run` records with it, so the key of every model is redacted
      whatever the backend: the protocol does not require a backend to hold its model entry (R12).
    - `defect_log`: where the attempt's harness defects leave their redacted traceback (`defect_log.DefectLog`); it
      also keeps what an executor's own sink raised, which the attempt reports once the result is stored.
      `record_run` passes it on, so a crash the loop catches is logged too, redacted with every secret the executor
      hands `record_run`.
    """

    identity: RunIdentity
    run_dir: Path
    endpoint: Endpoint
    backend: ModelBackend
    timer: AgentTimer
    clock: Callable[[], float]
    treatment_kinds: tuple[str, ...]
    lease: Lease | None = None
    treatment_targets: Mapping[str, str] = field(default_factory=dict)
    models: ModelTable | None = None
    defect_log: DefectSink | None = None

    @property
    def run_key(self) -> str:
        """The run's key text."""
        return self.identity.run_key


class RunExecutor(Protocol):
    """What a study or platform supplies: run one attempt and return its record.

    Infrastructure that fails outside the loop (a sandbox that will not start) is raised as `InfraError`; the scheduler
    records it as such. Any other exception is a defect in our own code: the attempt is a harness error (C10).
    """

    async def __call__(self, request: RunRequest) -> RunRecord:
        """Run the attempt and return its record."""
        ...


async def record_run(
    request: RunRequest,
    providers: Sequence[ToolProvider],
    system: "str | RenderedPrompt | PromptText",
    user: "str | RenderedPrompt | PromptText",
    config: AgentConfig,
    *,
    code: Mapping[str, Any],
    **options: Any,
) -> RunRecord:
    """Run the attempt through the loop with the request's backend, timer and treatment kinds, and return the record.

    The way an executor calls `record_attempt`: it cannot forget the treatment kinds and targets, which the store
    checks, or the model table the keys are redacted from, or the attempt's harness-defect log, or build a backend of
    its own.
    """
    return await record_attempt(
        request.backend,
        providers,
        system,
        user,
        config,
        request.identity,
        code=code,
        treatment_kinds=request.treatment_kinds,
        treatment_targets=request.treatment_targets,
        timer=request.timer,
        clock=request.clock,
        **{"models": request.models, "defect_log": request.defect_log, **_sink_failure_to(request), **options},
    )


def _sink_failure_to(request: RunRequest) -> dict[str, Any]:
    """Where a failure of the attempt's defect sink goes (the request's own log, or an executor's sink passed in its
    place): the request's log, which the attempt reports once the result is stored."""
    return {} if request.defect_log is None else {"on_sink_failure": request.defect_log.keep}


def _total(turns: Sequence[Mapping[str, Any]], name: str) -> int | None:
    """Sum a usage count over the turns that reported it; None when no turn did (unknown, never zero)."""
    values = [t["usage"][name] for t in turns if t.get("usage") and t["usage"].get(name) is not None]
    return sum(values) if values else None


def turn_usage_of(turns: Sequence[Mapping[str, Any]]) -> tuple[tuple[int | None, ...], ...]:
    """Each turn's counts as its record reported them: input tokens (the whole prompt), output tokens, cache writes,
    cache reads; a count the turn did not report is None. A tiered price is applied to each request from these (C24)."""
    names = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return tuple(tuple((t.get("usage") or {}).get(name) for name in names) for t in turns)


def outcome_of(entry: Mapping[str, Any]) -> Outcome:
    """Rebuild an outcome from the record's outcome section, its target included (none before version 3)."""
    return outcome_from_entry(entry)


def result_from_record(record: RunRecord, *, ended_utc: str, wall_s: float | None = None) -> AttemptResult:
    """Return the store's result for a record: its outcome, tokens as the turns reported them (summed, and per turn),
    its treatment kinds and targets, the turns it completed, the targets its fault injector recorded as injected in this
    attempt, and the treatment flag it carries (the store checks that flag against its own decision).

    A count no turn reported is unknown, and totals the loop knows to be short are marked incomplete (C05), and so are
    totals that miss a billed reply the backend counted and could not read, whatever rule the record's version keeps
    in its own totals (C26): the campaign charges such an attempt its bound. The wall time is the record's elapsed time
    unless the caller gives one.
    """
    turns = record.transcript
    elapsed = record.totals.get("elapsed_s")
    unread = unread_replies(record.route.get("usage_seen"), len(turns))
    return AttemptResult(
        outcome=outcome_of(record.outcome),
        ended_utc=ended_utc,
        wall_s=float(wall_s if wall_s is not None else (elapsed or 0.0)),
        input_tokens=_total(turns, "input_tokens"),
        output_tokens=_total(turns, "output_tokens"),
        cache_creation_tokens=_total(turns, "cache_creation_input_tokens"),
        cache_read_tokens=_total(turns, "cache_read_input_tokens"),
        usage_complete=not record.totals.get("usage_incomplete", False) and not unread,
        treatment_kinds=tuple(record.treatment_kinds),
        model_turns=len(turns),
        recorded_treatment=bool(record.outcome["treatment"]),
        first_turn_rule=first_turn_rule(record.schema_version),
        turn_usage=turn_usage_of(turns),
        target_rule=target_rule(record.schema_version),
        treatment_targets=dict(record.treatment_targets),
        injected_targets=tuple(injected_targets(record.instruments, record.run_key, record.attempt)),
    )


def sent_no_request(error: BaseException, backend: Any) -> bool:
    """Tell whether an executor's failure is known to have sent no model request (C31).

    True for a first-turn refusal, which the hook raises before the loop's first request, unless the backend says it
    started one (`requests_sent`, an optional attribute of a backend: a request an executor sent before the hook ran
    may have been billed). A backend that keeps no count is taken at the refusal's word. Any other failure may have
    sent requests, so its usage stays unknown.
    """
    return isinstance(error, FirstTurnRefusal) and getattr(backend, "requests_sent", None) in (None, 0)


def failure_result(
    error: BaseException,
    *,
    ended_utc: str,
    wall_s: float,
    treatment_kinds: Sequence[str] = (),
    treatment_targets: Mapping[str, str] | None = None,
    sent_nothing: bool = False,
) -> AttemptResult:
    """Return the result of an executor that raised instead of returning a record; no usage is known.

    An `InfraError` is infrastructure of its kind, naming its target. Anything else is a crash in our own code: a
    harness defect, named by its class and a line-free stack hash, never by the exception's text, which may carry a
    secret. With no record there is no completed turn, so neither is treatment. With `sent_nothing` the caller knows
    no model request was sent (`sent_no_request`), so the usage is known to be none: zero tokens and no turn, which
    costs 0 at any price (C24) and is never charged the bound of an attempt whose usage is unknown, as the attempt of
    a run that found an outage before it began is not.
    """
    usage: dict[str, Any] = (
        {"input_tokens": 0, "output_tokens": 0, "turn_usage": ()} if sent_nothing else {"usage_complete": False}
    )
    unknown = {
        **usage,
        "treatment_kinds": tuple(treatment_kinds),
        "treatment_targets": dict(treatment_targets or {}),
    }
    if isinstance(error, InfraError):
        outcome = Outcome("infrastructure", error.detail, infra_kind=error.kind, infra_target=error.target)
    else:
        outcome = Outcome("harness_defect", type(error).__name__, defect=harness_defect_of(error))
    return AttemptResult(outcome, ended_utc, wall_s, **unknown)
