# Interfaces

What a study (or a platform) implements to plug into the core, with the signature as the code has it, what the core guarantees, what the implementer must guarantee, and the closed vocabularies. [`architecture.md`](architecture.md) says why the interfaces are shaped this way; this file says what they are in v0.1. The runnable example is [`examples/tiny_study/`](../examples/tiny_study/).

All of them are `typing.Protocol`s, so a test double needs no inheritance. Signatures below are copied from the code; where a docstring in the code says more, the code wins.

## What v0.1 has

| Architecture name | In v0.1 |
|---|---|
| `Study` | the study directory (`study.toml` and its siblings) plus one plugin module whose `build(context)` returns a `StudyPlugin` ([below](#the-study-plugin)) |
| `Platform`, `Sandbox` | not in the core. The platform layer (`expcore_onedata`, steps 4a and 4b) supplies a `HealthCheck`, a `ResourceProbe` and its own `ToolProvider`s through the same `StudyPlugin` |
| `ToolProvider`, `Scorer`, `Instrument` | [`ToolProvider`](#toolprovider-and-callcontext) and [`Scorer`](#scorer) are protocols. `Instrument` is **not yet a protocol**: see [Instrument](#instrument) |
| `ModelBackend`, `RunExecutor`, `HealthCheck`, `ResourceProbe` | protocols, below |

## The study plugin

A study directory holds `study.toml`, `models.toml`, `campaign.toml`, `preregistration.toml` and one Python file named by `[study] plugin`. `expcore.study.config.load_study(directory)` validates all of them against each other. `expcore.study.plugin.load_plugin` imports the file and calls

```python
def build(context: StudyContext) -> StudyPlugin: ...
```

```python
@dataclass(frozen=True)
class StudyContext:
    study: StudyConfig  # the loaded configuration
    code: Mapping[str, Any]  # the code identity every record carries


@dataclass(frozen=True)
class StudyPlugin:
    executor: RunExecutor
    scorers: Sequence[Scorer]
    prompts: Mapping[str, str]  # per condition: SHA-256 of its prompt template
    tools: Mapping[str, str]  # per condition: SHA-256 of its tool manifest (tools_sha256)
    health: HealthCheck | None = None
    probe: ResourceProbe | None = None
    lease_group: Callable[[Mapping[str, Any]], str | None] = lambda factors: None
    lease_keys: Callable[[str], Sequence[str]] = lambda group: ()
```

The core guarantees: the code identity is computed once, from the study directory and the `[study] code` roots, and refused if it is unknown, stale or (without `--allow-dirty`) dirty; the plugin is rejected at load if a condition has no prompt or tool hash, or two scorers share a name; `prompts`, `tools`, the scorers' names and versions and the hash of each configuration file go into the campaign fingerprint, so a resume under any change is refused (C06).

The implementer must guarantee: `build` has no side effects beyond constructing objects; the hashes describe what the model sees under each condition; nothing is named by the core, so every name lives in the study.

The command line is `python -m expcore run|check|analyze|report` ([`cli.py`](../src/expcore/cli.py)). Output goes under `<study>/out/`.

What the analysis may use. `check` replays every record and also copies the campaign store into the batch (`campaign-snapshot.sqlite`, made with SQLite's online backup over a read-only connection, which copies pages and so needs no particular SQLite version, and integrity-checked) and hashes the copy with the records. It lists the records it replayed (`replayed`) and refuses a run record named by a hashed-only pattern, so a check cannot replay nothing and still vouch for the records beside it. It refuses a store with attempts still running unless `--partial`, which the check records. `analyze` and `report` then use only those bytes: the snapshot, never the live store, and the replayed records, each read once and compared with the check's hash. They cross-validate the two: every record maps to an attempt of the snapshot, and every attempt scored (state `done`, a scored treatment failure included) has a replayed record that agrees with it on run key, attempt, outcome and treatment flag. Any mismatch is refused, naming it. With `--partial`, attempts still running at the check are tolerated, a final attempt no replayed record covers is left out. Every saved output (`export.json`, `analysis.json`, `report.md`) says whether the analysis is partial and lists the attempts left out (run key, attempt, reason); `analysed_attempts` holds only what was scored, and every count in the report is of that population, with the left-out runs' attempts in a column of their own.

## ModelBackend

`expcore.models.adapters.ModelBackend`; built by the core, never by the executor.

```python
class ModelBackend(Protocol):
    @property
    def output_limit(self) -> int | None
    @property
    def context_tokens(self) -> int | None
    def info(self) -> dict[str, Any]
    def begin(self, system: str, user: str, tools: list[dict]) -> None
    async def step(self) -> ModelTurn
    def add_tool_results(self, results: list[ToolResult]) -> None
    def transcript(self) -> list[dict]
    async def probe(self) -> None
    async def aclose(self) -> None
```

Core guarantees:
- The scheduler builds the backend of a run from the endpoint the campaign admitted (`probe` succeeded once, B07), through a `BackendFactory` (below); a run never resolves a URL of its own (C14). A model that is not frozen (B08), whose settings cannot be sent together (B16) or, with a budget, has no price (C12) is refused before a run starts.
- The backend is closed (`aclose`) after the executor returns, whatever happens.
- Retries of transient errors, the served-model check and usage totals are the adapters' (B01 to B16); the stub adapter takes a script (`StubAdapter.use(script)`, items: text, a list of `(tool, arguments)`, `{"text", "stop"}`, a ready-made `ModelTurn`, or an exception it raises).

Implementer must guarantee:
- `step` returns one reply per call. A token count the reply did not report is `None`, never 0 (R10).
- A failure of the endpoint that outlasts the retries is raised as `InfraError("endpoint", ...)`; a refused key as `InfraError("key_rejected", ...)`. A reply cut at the output limit sets `stop_reason` to a limit stop.
- `info()` and `transcript()` never contain a credential; the base URL has none (R12).

### BackendFactory

```python
class BackendFactory(Protocol):
    def __call__(self, spec: ModelSpec, *, seed: int, limits: Limits, base_url: str | None,
                 time_left: Callable[[], float | None] | None) -> ModelBackend
```

The scheduler calls it for admission and for every run with the same `base_url`. The default builds the adapter the model entry names.

## ToolProvider and CallContext

`expcore.plugin`. The core ships an MCP stdio client (`expcore.tools.mcp_client.McpToolProvider`) and a wrapper for plain functions (`expcore.tools.function_provider.FunctionProvider`); it has no tool of its own.

```python
class ToolProvider(Protocol):
    def tools(self) -> Sequence[ToolSpec]
    async def execute(self, name: str, arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext) -> ToolOutput

class CallContext(Protocol):
    def infrastructure_wait(self, kind: str) -> AbstractAsyncContextManager[None]
```

`ToolSpec(name, description, input_schema, terminal=False)` and `ToolOutput(text, is_error=False, error_class=None, source_truncated=None)` are the data types. A successful call to a `terminal` tool ends the attempt as `finished` (L09).

Core guarantees:
- `timeout_s` is the agent's time left; the loop cuts a call that outlasts it and ends the run as a time cap (L05). The tool box caps what the model sees (L06) and records the full result beside the capped text.
- `ctx.infrastructure_wait(kind)` pauses the agent's clock while it is open and measures the wait live, per kind. A kind outside `WAIT_KINDS` raises at once.
- A malformed call is not executed: the model gets the error and the raw arguments are recorded (L01).
- An `InfraError` raised by the provider ends the attempt as infrastructure of that kind (C02); any other exception is a harness defect: the attempt is set aside (L08).

Implementer must guarantee:
- `tools()` does not change during a run.
- Everything the model should see, errors included, is a `ToolOutput`. Raising is for infrastructure (`InfraError`) and for defects.
- Blocking work does not run on the event loop (`asyncio.to_thread`, or `FunctionProvider`), and bounds itself with `timeout_s`: a thread cannot be killed (L15).
- Every wait on infrastructure is wrapped in `ctx.infrastructure_wait`. What the agent's own call does (executing, reading data, a federation operation it triggers) is never a wait.

## Scorer

`expcore.records.rescore.Scorer`.

```python
class Scorer(Protocol):
    name: str
    version: str
    def score(self, record: RunRecord) -> Mapping[str, Any]
```

Core guarantees:
- A scorer receives a deep copy of the record. The result is kept in `record.scores[name] = {"version", "result"}`, apart from instrument output, and a scorer without `version` is versioned by the hash of its source file.
- An attempt that is set aside (infrastructure not declared as treatment, or a harness defect) is never scored. An attempt that ends in a declared treatment kind is, so the scorer must handle a partial record.
- The analysis reads `result["outcomes"]` and nothing else (`expcore.study.pipeline`). The pipeline scores in memory and never writes into a checked batch. `analyze` derives every score anew from the verified records and reads no stored score, so it is the re-scoring command (R13): after a scorer changes, `check` again (the scorer is code, so the code identity changed), then `analyze` and `report`. `scores.json` and the report name each scorer's version.

Implementer must guarantee:
- The score is a pure function of the record: no model, no platform, no clock (R13). The platform axis comes from run-end snapshots in the record.
- The result is JSON. By convention: `axes` (two named axes, what the agent did and what the platform's state shows), `verdict` (`fulfilled`, `violated` or `not_determined`; not enforced by the core) and `outcomes`, a mapping from a name to a finite number or `None`.
- An outcome that could not be computed is `None`, never 0, and a `not_determined` verdict leaves every outcome that depends on the unobserved state `None` (R10, S05).

## Instrument

Not yet a protocol. [`architecture.md`](architecture.md#plugin-interfaces) describes it: hooks at run start and end that write into the record under their own namespace. In v0.1 the record has the place for it (`RunRecord.instruments`, `RunRecord.with_instrument(namespace, data)`, a namespace of lower-case letters, digits and underscores, data a JSON object) and nothing calls an instrument for you. Platform probes (4b) will define the protocol; a study that needs one before then writes its snapshot with `with_instrument` in its executor.

## RunExecutor

`expcore.campaign.executor.RunExecutor`.

```python
class RunExecutor(Protocol):
    async def __call__(self, request: RunRequest) -> RunRecord

@dataclass(frozen=True)
class RunRequest:
    identity: RunIdentity
    run_dir: Path
    endpoint: Endpoint
    backend: ModelBackend
    timer: AgentTimer
    clock: Callable[[], float]
    treatment_kinds: tuple[str, ...]
    lease: Lease | None = None
```

`record_run(request, providers, system, user, config, *, code, **options)` runs the loop with the request's backend, timer and treatment kinds and returns the record.

Core guarantees:
- The gates (health, harness stop, budget, unit pause) were passed, the lease the platform declared for the run is held for the whole attempt, and the timer is not started: the loop starts it. The attempt has a row (`running`) before the executor is called, so a kill leaves a run that the next start settles (C04).
- `InfraError` raised by the executor is recorded as infrastructure of its kind; any other exception as a harness defect (C10). The result is written as a marker, then to the store in one transaction.
- The record's treatment kinds must equal the condition's, or the store refuses the result (C01).

Implementer must guarantee:
- Return the record `record_run` produced, and **write it to `request.run_dir`** (`record.write(request.run_dir)`) before returning: the core reads, checks and exports records from disk and does not write them for you.
- Script or configure the backend through the request; never build one.
- Pass `request.treatment_kinds` unchanged (`record_run` does) and the code identity from `StudyContext.code`.
- Close nothing the core owns (backend, timer, lease).

## HealthCheck

`expcore.campaign.healthgate.HealthCheck`; `Health` is a named tuple `(ok: bool, detail: str)`.

```python
class HealthCheck(Protocol):
    def __call__(self) -> Health
```

Core guarantees: it is asked before an attempt starts and after it ends, and again at an interval while the gate is closed. A check that raises, or answers anything other than a `Health` (or an `(ok, detail)` pair with a real boolean), counts as a failed check, never as a pass or a crash. A failed pre-run check closes the gate, holds dispatch, and the run that found it is recorded as the outage's attempt, an infrastructure failure of `SchedulerConfig.health_kind` (C11). A failed post-run check turns an attempt that would have been scored into an infrastructure failure of that kind, or discards it as the open outage's.

Implementer must guarantee: it is quick (bounded by `health_timeout_s`), side-effect free, and `detail` holds no secret. `health_kind` must not be a kind any condition declares as its treatment.

## ResourceProbe

`expcore.campaign.lease.ResourceProbe`. Both questions must fail safe.

```python
class ResourceProbe(Protocol):
    def holder_gone(self, owner: str) -> bool | None
    def resource_gone(self, key: str, note: Mapping[str, Any]) -> bool
```

Core guarantees: a lease is an exclusive file lock per key, dropped by the kernel when its holder dies; a key whose last holder did not release it goes to nobody until `resource_gone` answers `True` for it (C19). `holder_gone` returning `None` or raising is treated like gone; `resource_gone` frees a key only by returning exactly `True` (an identity test: `1`, `"unknown"` or any other truthy value does not count), on every path: at the end of a run, when a quarantine is settled and for a dead holder's key. Anything else, or raising, keeps the key quarantined and counted against its unit's cap.

Implementer must guarantee: `True` from `resource_gone` means everything the holder started with that key (its sandbox, processes, mounts) is shown gone, never "probably". `lease_group` and `lease_keys` in the plugin name the keys; a key is letters, digits, dot, dash and underscore.

## Other protocols the core defines

| Protocol | Where | Signature |
|---|---|---|
| `Estimator` | `expcore.stats.estimators` | `__call__(self, paired: PairedDifferences, settings: EstimatorSettings) -> EstimatorResult`; registered by name with `estimators.register`, named by a contrast in `preregistration.toml` |
| `Recompute` | `expcore.replay.check` | `name: str`, `version: str`, `__call__(self, record: RunRecord) -> int`; recomputes a recorded decision offline and raises on a contradiction (R05) |

## Closed vocabularies

An unknown value raises where it is built, so a typo never reaches a record.

| Vocabulary | Values | Defined in |
|---|---|---|
| Outcomes | `finished`, `turn_cap`, `time_cap`, `token_cap`, `output_cap`, `malformed_end`, `leaked_tool_call`, `harness_defect`, `error`, `infrastructure` | `expcore.loop.outcomes.OUTCOMES` |
| Infrastructure kinds | `endpoint`, `key_rejected`, `sandbox`, `knowledge`, `unattributed_kill`, `ceiling`, plus kinds a platform adds once with `expcore.errors.register_infra_kind` | `expcore.errors.CORE_KINDS`, `infra_kinds()` |
| Wait kinds | `model_retry`, `lease_queue`, `sandbox_lifecycle`, `toolserver_lifecycle` | `expcore.plugin.WAIT_KINDS` |
| Conclusion words | `difference`, `trivial_difference`, `equivalent`, `not_detected` | `expcore.stats.analysis.CONCLUSIONS` |
| Tool call statuses | `ok`, `error`, `malformed`, `skipped`, `cut`, `failed` | `expcore.loop.records.TOOL_STATUSES` |
| Attempt states | `running`, `done`, `infra_fail`, `outage_discarded`, `interrupted`, `harness_error` | `expcore.campaign.attempts.STATES` |
| Run statuses | `pending`, `done`, `infra_exhausted`, `harness_error` | `expcore.campaign.attempts.RUN_STATUSES` |
| Lease outcomes of an attempt | `granted`, `none` (a refused lease starts no attempt, so it is in the lease wait log and the event log) | `expcore.campaign.attempts.LEASE_OUTCOMES` |
| Malformed-call policies | `report`, `end` | `expcore.loop.config.MALFORMED_POLICIES` |

The conclusion words encode significance: `difference` and `trivial_difference` are significant, the other two are not; a contrast outside the Holm family is judged by its raw p-value and labelled exploratory wherever it is shown. Failing to detect a difference is not evidence of none.
