# Interfaces

What a study (or a platform) implements to plug into the core, with the signature as the code has it, what the core guarantees, what the implementer must guarantee, and the closed vocabularies. [`architecture.md`](architecture.md) says why the interfaces are shaped this way; this file says what they are in v0.1. The runnable example is [`examples/tiny_study/`](../examples/tiny_study/).

All of them are `typing.Protocol`s, so a test double needs no inheritance. Signatures below are copied from the code; where a docstring in the code says more, the code wins.

## What v0.1 has

| Architecture name | In v0.1 |
|---|---|
| `Study` | the study directory (`study.toml` and its siblings) plus one plugin module whose `build(context)` returns a `StudyPlugin` ([below](#the-study-plugin)) |
| `Platform`, `Sandbox` | not in the core. The platform layer (`expcore_onedata`, steps 4a and 4b) supplies a `HealthCheck`, a `ResourceProbe` (leased platform users), its own `ToolProvider`s and an `Instrument` (the platform probes) through the same `StudyPlugin`; its sandbox is [described below](#the-platform-layers-sandbox) |
| `ToolProvider`, `Scorer`, `Instrument` | protocols: [`ToolProvider`](#toolprovider-and-callcontext), [`Scorer`](#scorer) and [`Instrument`](#instrument) (a protocol since step 4b2) |
| `ModelBackend`, `RunExecutor`, `HealthCheck`, `ResourceProbe` | protocols, below |

## The study plugin

A study directory holds `study.toml`, `models.toml`, `campaign.toml`, `preregistration.toml` and one Python file named by `[study] plugin`; `[study] requires` names the distributions beyond the core that the study's code imports. `expcore.study.config.load_study(directory)` validates all of them against each other. `expcore.study.plugin.load_plugin` imports the file and calls

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
    platform: Mapping[str, Any] = field(default_factory=dict)  # the platform's settings that make one experiment
    backend_factory: BackendFactory | None = None  # None: the core's default_backend_factory
```

The core guarantees: the code identity is computed once, from the study directory and the `[study] code` roots, and refused if it is unknown, stale or (without `--allow-dirty`) dirty; the plugin is rejected at load if a condition has no prompt or tool hash, two scorers share a name, or `platform` is not a mapping; `prompts`, `tools`, the scorers' names and versions, the hash of each configuration file and, when not empty, `platform` go into the campaign fingerprint, so a resume under any change is refused (C06). A study on the Onedata layer passes `platform=config.fingerprint()`, the platform file's TLS mode with the CA bundle's SHA-256, so a resume under another mode or bundle is refused.

The implementer must guarantee: `build` has no side effects beyond constructing objects; the hashes describe what the model sees under each condition; nothing is named by the core, so every name lives in the study.

The command line is `python -m expcore run|check|analyze|report|package|package-check|example` ([`cli.py`](../src/expcore/cli.py)). Output goes under `<study>/out/`; `package` writes a replication package where `--out` says, and `example` writes the example study into a new directory. How to use them is in [`study-guide.md`](study-guide.md).

What the analysis may use. `check` replays every record and also copies the campaign store into the batch (`campaign-snapshot.sqlite`, made with SQLite's online backup over a read-only connection, which copies pages and so needs no particular SQLite version; then each attempt's directory is written relative to the batch and the copy is rebuilt with a plain `VACUUM`, so it holds no path of the machine the campaign ran on; and integrity-checked) and hashes the copy with the records. It lists the records it replayed (`replayed`) and refuses a run record named by a hashed-only pattern, so a check cannot replay nothing and still vouch for the records beside it. It refuses a store with attempts still running unless `--partial`, which the check records. `analyze` and `report` then use only those bytes: the snapshot, never the live store, and the replayed records, each read once and compared with the check's hash. They cross-validate the two: every record maps to an attempt of the snapshot, and every attempt scored (state `done`, a scored treatment failure included) has a replayed record that agrees with it on run key, attempt, outcome and treatment flag. Any mismatch is refused, naming it. With `--partial`, attempts still running at the check are tolerated, a final attempt no replayed record covers is left out. Every saved output (`export.json`, `analysis.json`, `report.md`) says whether the analysis is partial and lists the attempts left out (run key, attempt, reason); `analysed_attempts` holds only what was scored, and every count in the report is of that population, with the left-out runs' attempts in a column of their own.

## ModelBackend

`expcore.models.adapters.ModelBackend`; built by the core, never by the executor. The core builds three backends, named by `backend` in `models.toml`: `openai_compat`, `anthropic` and `stub` (`expcore.config.models.BACKENDS`). A study plugs in a backend of its own without editing the core: its model entries name it with a plain lower-case `backend` value, and the plugin supplies the `backend_factory` that builds it ([BackendFactory](#backendfactory)).

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
- An executor that needs more than the protocol (the example's executor scripts the stub with `use`) checks for that method, never for a class, so a backend that wraps or replaces the stub serves it too.

### BackendFactory

```python
class BackendFactory(Protocol):
    def __call__(self, spec: ModelSpec, *, seed: int, limits: Limits, base_url: str | None,
                 time_left: Callable[[], float | None] | None) -> ModelBackend
```

The scheduler calls it for admission and for every run with the same `base_url`. The default, `expcore.campaign.executor.default_backend_factory`, builds the adapter the model entry names and refuses a backend the core does not build. A study replaces it with `StudyPlugin.backend_factory`, which `run_study` passes to the scheduler.

The core guarantees: a model entry whose `backend` the core does not build is accepted by `models.toml` only as a plain lower-case name, and only the core's rules that are not about its own adapters apply to it (its `base_url`, if given, its key reference, its limits, its sampling); the plugin is refused at load, naming `models.toml` and the key, when such a model is in the table and the plugin supplies no factory. A plugin's factory is in the campaign fingerprint by its qualified name and its `version`, so a resume under another factory or another version is refused (C06); a study on the core's factory has no such section and keeps its fingerprint. A factory in the plugin file goes by its name alone, since a plugin's module name holds the study's path. The code identity covers a factory that lives in the study directory, whose `version` is taken as declared; the `version` speaks for one in an installed package, so when the factory's module belongs to an installed distribution (by its top-level package), the campaign refuses to start or resume unless the declared version equals that distribution's, naming both.

The implementer must guarantee: a factory a plugin supplies has a `version` attribute, a non-empty string changed whenever its behaviour changes (the plugin is refused at load without one); the factory decides which models it serves and passes every other model to `default_backend_factory`, so the core's backends keep their own rules (B08, B16); the backend it returns meets the `ModelBackend` contract above; a backend whose transcript is not in a core format registers its parser with `expcore.replay.native.register_native_parser`, under the name its `info()` gives, at the top level of the plugin module or of a module in `[study] platforms`, or the replay check cannot read its records. `check` imports both only when a record names a backend no parser is registered for, says so in one line, and does not call `build`, so it needs no model or key and a parser registered only inside `build` is not there. A record that names an infrastructure kind neither the core nor `[study.kinds]` holds makes it import the `[study] platforms` modules alone, and a batch that needs neither imports nothing from the study.

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
- An attempt that is set aside (infrastructure not declared as treatment, or a harness defect) is never scored. An attempt that ends in a declared treatment kind, after the model completed at least one turn, is, so the scorer must handle a partial record; the same kind before the first turn is set aside.
- The analysis reads `result["outcomes"]` and nothing else (`expcore.study.pipeline`). The pipeline scores in memory and never writes into a checked batch. `analyze` derives every score anew from the verified records and reads no stored score, so it is the re-scoring command (R13): after a scorer changes, `check` again (the scorer is code, so the code identity changed), then `analyze` and `report`. `scores.json` and the report name each scorer's version.

Implementer must guarantee:
- The score is a pure function of the record: no model, no platform, no clock (R13). The platform axis comes from run-end snapshots in the record.
- The result is JSON. By convention: `axes` (two named axes, what the agent did and what the platform's state shows), `verdict` (`fulfilled`, `violated` or `not_determined`; not enforced by the core) and `outcomes`, a mapping from a name to a finite number or `None`.
- An outcome that could not be computed is `None`, never 0, and a `not_determined` verdict leaves every outcome that depends on the unobserved state `None` (R10, S05).
- A treatment attempt (`record.outcome["treatment"]` is true) gets values: what the agent achieved under the manipulation (the failure itself, for example no answer and zero coverage) and what it did before it. `None` drops the run's pair from every contrast on that outcome; the analysis counts those drops per contrast, and refuses a contrast in the Holm family that has any, unless its preregistration declares `treatment_outcomes = "excluded"`; `"scored"` is a claim the analysis checks, so such a contrast is refused too, past its preregistered `max_unscored_treatment` (0 unless set) (S10).

## Instrument

`expcore.plugin.Instrument`, defined in step 4b2. Hooks at the start and the end of a run that write into the record under their own namespace.

```python
class Instrument(Protocol):
    namespace: str
    def start(self, identity: Mapping[str, Any]) -> None
    def finish(self) -> Mapping[str, Any]
```

The study's executor calls them; nothing in the loop does. `expcore.records.instruments` has the two helpers an executor uses:

```python
def start_instruments(instruments: Sequence[Instrument], identity: Mapping[str, Any]) -> None
def finish_instruments(record: RunRecord, instruments: Sequence[Instrument]) -> RunRecord
```

Core guarantees:
- `check_instruments` runs first on both: a namespace that is not lower-case letters, digits and underscores, and two instruments with one namespace, raise before anything runs.
- `finish_instruments` returns a new record with each snapshot in `RunRecord.instruments[namespace]`, a JSON object (NaN and values JSON cannot hold are refused). An instrument whose `finish` raises, or returns anything but a JSON object, is recorded as `{"ok": false, "error": <class name>}`: the attempt keeps its record and a scorer that needs the snapshot sees that it failed (O08).
- The snapshot is the only platform state a scorer reads (R13); replay serves it and asks the platform nothing.

Implementer must guarantee:
- `start` and `finish` run outside the agent's time: before the loop starts and after it ends. They do blocking I/O, so an async executor calls them with `asyncio.to_thread`. The platform's waits for the federation to settle (`expcore_onedata.consistency`) are not a declared wait kind and refuse to run on the thread of a running event loop.
- `finish` records what it read and never guesses: a reading that failed is `None` (with the failure beside it), not zero, and anything derived from it is `None` too (R10).
- The snapshot holds no secret and no identifier of the deployment beyond the labels the study's configuration gives.

The platform's own instrument is `expcore_onedata.probes.PlatformProbes` (namespace `platform_probes`): the block map of each tracked file on each tracked provider before and after the run, the records of the transfers the provisioner scheduled, the record of every transfer of the space that started in the run's window whoever started it (`space_transfers`), the bytes moved between provider pairs, attributed to a source only where exactly one other provider held those bytes at the start, and per receiving provider the transfer bytes into it and a lower bound on the bytes fetched on demand (`on_demand`). A provider's `gained_bytes` is placement gained: the bytes of the blocks a provider's block map shows at run end that it did not show at run start; what it showed at the start and no longer does is `lost_bytes`, kept apart, so it is not a net change, and it is not bytes on the wire. See its module docstring and `expcore_onedata.transferlog` for the snapshot's fields.

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
    models: ModelTable | None = None
```

`record_run(request, providers, system, user, config, *, code, **options)` runs the loop with the request's backend, timer and treatment kinds and returns the record. It records with the request's model table, which the scheduler fills in for every run, so the key of each model in `models.toml` is redacted whatever the backend (R12).

Core guarantees:
- The gates (health, harness stop, budget, unit pause) were passed, the lease the platform declared for the run is held for the whole attempt, and the timer is not started: the loop starts it. The attempt has a row (`running`) before the executor is called, so a kill leaves a run that the next start settles (C04).
- `InfraError` raised by the executor is recorded as infrastructure of its kind; any other exception as a harness defect (C10). The result is written as a marker, then to the store in one transaction.
- The record's treatment kinds must equal the condition's, and its treatment flag the store's own decision from the outcome, the kinds and the completed turns (`AttemptResult.model_turns`, `recorded_treatment` and `first_turn_rule`, all set by `result_from_record` from the record and its schema version), or the store refuses the result (C01). Record schema 2 added the first-turn rule and no field; schema-1 records and schema-1 outcome markers stay readable and are decided without it.

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

Implementer must guarantee: it is quick (bounded by `health_timeout_s`), side-effect free, and `detail` holds no secret. `health_kind` must not be a kind any condition declares as its treatment. Each infrastructure kind is treatable or not: `tool_plane_integrity` and any kind a platform registers with `treatable=False` cannot be declared in `infrastructure_is_treatment`, and the factor schema refuses to load a condition that does, naming the file and the key; the scheduler refuses, at its start, a campaign whose health or identity kind a condition declares.

## ResourceProbe

`expcore.campaign.lease.ResourceProbe`. Both questions must fail safe.

```python
class ResourceProbe(Protocol):
    def holder_gone(self, owner: str) -> bool | None
    def resource_gone(self, key: str, note: Mapping[str, Any]) -> bool
```

Core guarantees: a lease is an exclusive file lock per key, dropped by the kernel when its holder dies; a key whose last holder did not release it goes to nobody until `resource_gone` answers `True` for it (C19). `holder_gone` returning `None` or raising is treated like gone; `resource_gone` frees a key only by returning exactly `True` (an identity test: `1`, `"unknown"` or any other truthy value does not count), on every path: at the end of a run, when a quarantine is settled and for a dead holder's key. Anything else, or raising, keeps the key quarantined and counted against its unit's cap.

Implementer must guarantee: `True` from `resource_gone` means everything the holder started with that key (its sandbox, processes, mounts) is shown gone, never "probably". `lease_group` and `lease_keys` in the plugin name the keys; a key is letters, digits, dot, dash and underscore.

## The platform layer's sandbox

`expcore_onedata.sandbox`. Nothing in `expcore` knows it; a study reaches it through the platform layer's objects, which take their names from `platform.toml` (`[sandbox]`, loaded with the rest of the file by `load_platform`).

```python
class Sandbox(Protocol):            # api.py
    def exec(self, code: str, timeout_s: float) -> ExecResult
    def collect(self, dest_dir: str | Path) -> dict[str, Any]
    def stop(self) -> None
    def metadata(self) -> dict[str, Any]

class DockerSandbox:                # sandbox.py
    @classmethod
    def start(cls, host: HostOps, settings: SandboxSettings, spec: SandboxSpec, **options) -> DockerSandbox
    def collect(self, dest_dir, outputs: Sequence[str] = ()) -> dict      # outputs: files of /work to copy

class HostOps(Protocol):            # hostops.py: every effect on the host, so a test supplies a fake
    def docker(self, *args, check=True, timeout=None, env=None, input=None) -> CmdResult
    def run(self, argv, timeout_s, *, sudo=False, input=None) -> CmdResult
    def read_text(self, path) -> str      # also readlink, exists, process_alive, make_quota_dir, make_ca,
    ...                                   # unmount, image_id, host_facts

class SandboxPlatform:              # provider.py
    def __init__(self, config, issuer, host, *, slots: Slots, journal: TokenJournal,
                 tool_condition=lambda factors: None, tool_server: ToolServerLauncher | None = None, ...)
    def mode(self, factors) -> DataPathMode
    def lease_group(self, factors) -> str | None                     # users.GROUP, or None when a run needs no slot
    def lease_keys(self, group: str) -> Sequence[str]                # the names of [slots]
    def probe(self) -> SlotProbe                                     # the ResourceProbe (see below)
    def sandbox_probe(self) -> SandboxProbe                          # its sandbox half
    def launch(self, ctx, *, run_key, level, ceiling_s, lease=None, condition=None)  # yields a DockerSandbox
    def tool_server(self, ctx, sandbox, *, ceiling_s, extra_env=None)  # yields the run's McpToolProvider

class CodeExecutionProvider:        # codetool.py: a ToolProvider with one tool that runs Python
    def __init__(self, sandbox: Sandbox, *, name="python", description=...)
```

**Data paths.** The study's `data_path` factor names its levels in its own words; `[sandbox.data_paths]` maps each to one of three kinds, `mount`, `rest` or `none`. A `mount` run has a read-only FUSE mount made by a oneclient sidecar that holds the data-path token; the agent holds no token and no FUSE. A `rest` run reaches the zone and its pinned local provider only through the data gate, which holds the token and adds it to the requests it forwards (a token the agent sends is dropped); the agent holds no token in its environment, its files or its process arguments. A `none` run has neither. The data path is read-only in every condition at four points: the token (4a: read-only, scoped to the space, one interface), the gate (only the five read calls pass), the mount, and the agent's root filesystem. `start` proves the last two by trying to write from inside the agent and requiring the refusal.

**One pool of leased users (C19).** A run that needs a data-path token or a tool server leases one slot of `[slots.<name>]` (`users.py`), and one that needs neither takes none (`lease_group` is None). The slot gives the run both planes' users: `launch` mints the run's tokens through `users.SlotTokens` at once, with one journalled expiry, the data path's on the slot's data-path user and, when `condition` names the run's tool-server condition, the tool server's on its tool-server user; `tool_server` then starts the server with that token, and the launcher refuses a token whose subject is not that slot user. A run with a tool server and no lease is refused; a run without a lease and with a data path (a live check) mints as the fixed `[principals.data_path]` user. The platform's `ResourceProbe` is `SlotProbe(journal, sandbox_gone=SandboxProbe(...).resource_gone)`: a key is freed only when the sandbox is proven gone and the slot's tokens have ended, by their journalled expiry plus a margin. A `[sandbox.users]` table, the second pool of earlier versions, is refused at load with a message that points to `[slots]`.

Core guarantees: `ctx.infrastructure_wait` is given a kind of `WAIT_KINDS`; the lease the platform declared for the run is held for the whole attempt and freed through the platform's probe, `SlotProbe.resource_gone`, whose sandbox half is `SandboxProbe.resource_gone`.

Implementer (the platform layer) guarantees:
- `launch` opens `ctx.infrastructure_wait("sandbox_lifecycle")` around the start (the token mint, the image checks, the containers) and around the stop, and nowhere else. Executing the agent's code and its reads of data are never waits.
- Every image is started by the id it is pinned to in `platform.toml`; a tag that now resolves elsewhere raises `StartRefused` before anything exists (O13). Each sandbox gets its own small network from the configured pool, whose ranges are strict networks checked at load (O14).
- The data gate's upstream connection and oneclient check the federation's certificate as the platform's one `[tls]` says, as the REST client and the tool server do (`expcore_onedata.tls`): against their default CAs, against the run's own read-only copy of the `ca_file` bundle's verified bytes (written once, after their hash is checked against the one loaded, and mounted read-only; a bundle that changed since the load refuses the start), or not at all with `insecure_tls`. The agent trusts the run's own CA alone. `metadata()["tls"]` records the mode, with the bundle's SHA-256 under `ca_file`, never its path or contents (O05, O12).
- Teardown runs on every path that ends a sandbox, in the order readers, unmount, sidecar, agent, and verifies that each step took effect; a sandbox that is not verifiably gone makes `stop` raise `SandboxError` and `launch` raise `InfraError("sandbox")`, and the probe keeps the lease key quarantined (O15, C19).
- `SandboxProbe.resource_gone(key, note)` answers exactly `True` only when docker lists no container and no network labelled with the key and the mount table shows no mount in a directory of the key; any failed read is `False`.
- The gate passes only reads, answers a refusal as 4xx and never as an error, and marks a transfer it cut (`cut`, `unread_bytes`) so it is told from an error of the gate (`error_origin: gate`) or of the platform (`upstream`) (O16).
- Before a campaign admits its first run, the platform's standard health check (`health.PlatformHealth`, which composes `precampaign.PreCampaignHealth` itself, with no opt-out) measures the content type the gate streams on when a data path is REST: it reads `[sandbox] content_check_file` (required with a REST level) through the local provider, whole and by its first byte, and a type other than `application/octet-stream` with 200 and 206, or one it cannot measure, refuses the campaign: the verdict is kept for the campaign, so the health gate stays closed with the refusal as its detail and no run is admitted; with no REST data path the file is never read. The day-0 check `content_type` is the same check, for the operator (O05, O11).
- Every answer that is no file's content is buffered whole up to 4 MiB, so each is bounded by the request: a listing whose explicit `limit` (in the query or a JSON body) is above `gatepolicy.MAX_LIST_LIMIT`, 1000, the server's own default page, is refused before anything is forwarded, as a 400 the agent reads; a listing without a limit passes unchanged. The worst case of a page of 1000, and the bound on the other non-content answers, are worked out in `gatepolicy.py` (O05, O16).
- A killed cell's cause comes from the cgroup counters, the kernel log, the host killer's journal and the container's state, never from the exit code. The agent's own limit is its outcome; a failure of the host or a component raises `InfraError("sandbox")`; a kill nothing attributes raises `InfraError("unattributed_kill")`, which the core retries within the attempt limit (O17). Errors the agent causes through the mount (ENOENT, EROFS, EISDIR) are not counted against the platform (O18).
- A message of any exception names roles, keys and positions, never a configured value (image, pool, path, login, level) or the tail of a tool's stderr (which goes to the log).

Not carried over from the earlier study: the gate's whole-file and merged-listing modes (that study's manipulated variables), the tracer and its checks, the capacity probes tied to two data layouts, and the replication branch of its fourth condition.

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
| Infrastructure kinds | `endpoint`, `key_rejected`, `sandbox`, `knowledge`, `unattributed_kill`, `ceiling`, `tool_plane_integrity`, plus kinds a platform registers ([below](#infrastructure-kinds-of-a-platform-or-a-study)) and kinds a study declares in `[study.kinds]` | `expcore.errors.CORE_KINDS`, `infra_kinds()`, `untreatable_kinds()` |
| Wait kinds | `model_retry`, `lease_queue`, `sandbox_lifecycle`, `toolserver_lifecycle` | `expcore.plugin.WAIT_KINDS` |
| Conclusion words | `difference`, `trivial_difference`, `equivalent`, `not_detected`, `untestable` | `expcore.stats.analysis.CONCLUSIONS` |
| Tool call statuses | `ok`, `error`, `malformed`, `skipped`, `cut`, `failed` | `expcore.loop.records.TOOL_STATUSES` |
| Attempt states | `running`, `done`, `infra_fail`, `outage_discarded`, `interrupted`, `harness_error` | `expcore.campaign.attempts.STATES` |
| Run statuses | `pending`, `done`, `infra_exhausted`, `harness_error` | `expcore.campaign.attempts.RUN_STATUSES` |
| Lease outcomes of an attempt | `granted`, `none` (a refused lease starts no attempt, so it is in the lease wait log and the event log) | `expcore.campaign.attempts.LEASE_OUTCOMES` |
| Malformed-call policies | `report`, `end` | `expcore.loop.config.MALFORMED_POLICIES` |

The conclusion words encode significance: `difference` and `trivial_difference` are significant, the other three are not; a contrast outside the Holm family is judged by its raw p-value and labelled exploratory wherever it is shown. Failing to detect a difference is not evidence of none. `untestable` is the word of a contrast that used no pair, and of no other: nothing was tested, and in the Holm family it counts as p = 1.

### Infrastructure kinds of a platform or a study

```python
def register_infra_kind(kind: str, description: str, *, treatable: bool = True) -> None
```

A platform adds a kind with `expcore.errors.register_infra_kind`, once, at the top level of a module of its own, so the kind exists as soon as that module is imported. `kind` is lower-case letters, digits and underscores and not a core kind; `description` is not blank and is the reason given when the kind is refused as a treatment; `treatable=False` marks a kind that can never be a condition's treatment, such as a failed integrity check of the harness. Registering a kind again with the same answer is harmless, and with the other answer is refused.

A study's `study.toml` is validated when it is loaded, before its plugin is imported, so a kind registered by the plugin, or by a platform only the plugin imports, is unknown to the conditions. A study that declares a platform's kind as a treatment lists the module that registers it in `[study] platforms`; the loader imports each listed module before it validates anything, and refuses one that cannot be imported, naming the file, the key, the module and the type of the exception, never its text. For the Onedata layer, that module is `expcore_onedata.sandbox.kinds`. A study without a platform declares its own kinds in `[study.kinds]`, as `name = { description = "...", treatable = true }`. They are held in the vocabulary only while the study is loaded and while the pipeline works on it (`expcore.errors.study_kinds`), so they do not outlast it, and a failed load leaves none behind; a platform's kinds stay for the whole process. The scopes are counted: two studies that overlap in one process each keep their kinds and can see the other's for the overlap, which can only widen validation, and declaring one name differently while they overlap is refused, naming the kind. A study's kind that is a core kind or a platform's is refused; so is a platform registering a kind of the study in scope. Neither key is part of the factor schema (study.toml's hash is in the campaign fingerprint, as before), so a study that uses neither keeps its fingerprint and schema digest. [`study-guide.md`](study-guide.md#treatment-kinds) shows both keys.

A condition that names a kind outside the vocabulary is refused as unknown, with the treatable kinds as the choices; a known kind that is not treatable is refused as one that can never be a treatment, with the reason its registration gave.
