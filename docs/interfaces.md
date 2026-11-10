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
    admission: RunAdmission | None = None  # (treatment_kinds, treatment_targets, cancel, exclusive) -> context manager
    platform: Mapping[str, Any] = field(default_factory=dict)  # the platform's settings that make one experiment
    backend_factory: BackendFactory | None = None  # None: the core's default_backend_factory
```

`admission` is the platform's hold on a resource every run shares (`expcore.campaign.attempt.RunAdmission`). The core enters it after the lease and the second look at the gates, before the attempt's backend, timer and executor exist, so its wait is outside the agent's clock, and leaves it after the post-run check and the store's record; a refusal (`InfraError`) leaves the run unstarted, with no attempt used. Every health check runs in its caller's context, so the post-run check sees what the run's admission holds. `expcore_onedata.admission.run_admission(config)` is the one for a platform with `[faults]`. `exclusive` is whether the run's condition declares `exclusive = true` in study.toml (`FactorSchema.exclusive`): a condition with no fault whose runs the platform holds exclusively, as it does an outage run's, so a control can run under the outage's isolation; a campaign with such a condition and no run admission is refused at its start, naming the file and the key. The context manager yields what it holds, `shared` or `exclusive`; the core stores it in the attempt's row as it is admitted (`none` without an admission) and the export carries it as `admission` in every analysed and excluded entry and as `admissions`, one per charged attempt, in every exhausted run.

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

### Retries of a reply

`ModelTurn.retries` holds the retries the reply took before it came, one object per retry, oldest first; `()` when it took none. The core's adapters fill it through `expcore.models.retry.Retrier`, and a backend of a study's own fills it the same way or leaves it empty. The keys of an entry are a closed set, `expcore.records.schema.RETRY_FIELDS`, and every one may be left out. Any other key makes the record invalid: writing it raises `RecordError`, and the attempt is recorded as a harness defect, which names the exception's class and not its message, so check the keys when you write the backend.

| Key | Holds |
|---|---|
| `retry` | the retry's number within this reply, from 1 |
| `kind` | the class of error that was retried (below) |
| `error` | the class name of the error |
| `status` | the HTTP status, or `null` for an error that has none (a timeout, a lost connection) |
| `retry_after_s` | the wait the server asked for (`Retry-After`, `Retry-After-Ms`), or `null` |
| `wait_s` | the wait before the retry, never less than `retry_after_s` |
| `message` | the provider's message, cut to 300 characters; empty when the error has none |

Only a transient error is retried, so `kind` is one of five: `rate_limit` (429), `timeout` (408, or a timeout while connecting or sending), `conflict` (409), `server` (5xx) and `connection` (the endpoint could not be reached). The retrier's other kinds never become an entry: `auth` (401 or 403) raises `InfraError("key_rejected", ...)`, `read_timeout` (the request went out and the reply is late) is never resent and abandons the request, `context_overflow` (a 400 or 413 whose code or message names the context) ends the run as the agent's outcome, and `not_found` and `bad_request` are raised unchanged. The record checks the keys and not the words, so a backend of its own uses these five for the same errors, and a reader can then count retries by kind across backends.

### BackendFactory

```python
class BackendFactory(Protocol):
    def __call__(self, spec: ModelSpec, *, seed: int, limits: Limits, base_url: str | None,
                 time_left: Callable[[], float | None] | None) -> ModelBackend
```

The scheduler calls it for admission and for every run with the same `base_url`. The default, `expcore.campaign.executor.default_backend_factory`, builds the adapter the model entry names and refuses a backend the core does not build. A study replaces it with `StudyPlugin.backend_factory`, which `run_study` passes to the scheduler.

The core guarantees: a model entry whose `backend` the core does not build is accepted by `models.toml` only as a plain lower-case name, and only the core's rules that are not about its own adapters apply to it (its `base_url`, if given, its key reference, its limits, its sampling); the plugin is refused at load, naming `models.toml` and the key, when such a model is in the table and the plugin supplies no factory. A plugin's factory is in the campaign fingerprint by its qualified name and its `version`, so a resume under another factory or another version is refused (C06); a study on the core's factory has no such section and keeps its fingerprint. A factory in the plugin file goes by its name alone, since a plugin's module name holds the study's path. The code identity covers a factory whose module's file is among the files `[study] code` lists, and its `version` is taken as declared. Otherwise the `version` speaks for its code, so it is held to the installed distribution that owns the module's file (the one whose `RECORD` lists it, or, for an editable install, the one whose top-level package holds it where it imports from, not a loose file below its project; never a namespace sibling that shares its top-level name): the campaign refuses to start or resume unless the two are equal as versions (`1.0+Build_1` is `1.0+build.1`), naming both. A factory whose file nothing owns and the identity does not cover is refused, naming its module.

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
- An `InfraError` raised by the provider ends the attempt as infrastructure of that kind (C02), naming the target it carries (`InfraError(kind, detail, target="providers[1]")`, a position label, never a configured name); any other exception is a harness defect: the attempt is set aside (L08).

Implementer must guarantee:
- `tools()` does not change during a run.
- Everything the model should see, errors included, is a `ToolOutput`. Raising is for infrastructure (`InfraError`) and for defects.
- Blocking work does not run on the event loop (`asyncio.to_thread`, or `FunctionProvider`), and bounds itself with `timeout_s`: a thread cannot be killed (L15).
- Every wait on infrastructure is wrapped in `ctx.infrastructure_wait`. What the agent's own call does (executing, reading data, a federation operation it triggers) is never a wait.

### McpToolProvider

A study or platform whose tools come from an MCP server speaks to it through `McpToolProvider`, one server session per run over stdio. The Onedata layer builds it for its tool server (`SandboxPlatform.tool_server`); any other platform builds it the same way.

```python
class McpToolProvider:  # expcore.tools.mcp_client
    def __init__(self, command: str, args: Iterable[str] = (), *, frozen: FrozenManifest | Path,
                 env: dict[str, str] | None = None, cwd: str | Path | None = None,
                 allowed_tools: Iterable[str] = (), start_timeout_s: float = 120.0)
    async def __aenter__(self) -> McpToolProvider  # start the server and check its tools
    async def __aexit__(self, *exc) -> None        # end the session and stop the server
    manifest_sha256: str | None                    # the frozen manifest's sha256, once the start succeeded
```

- `command` and `args` start the server as a child process, in `cwd`. `env` holds the variables the server gets beyond the few the MCP SDK passes on by default (`HOME`, `PATH`, `USER` and the like on POSIX); nothing else of the caller's environment reaches it, so a token the server needs goes in `env`.
- `frozen` is the condition's frozen manifest, an object or the path of its file, read when the provider is made. The server's tools must equal it.
- `allowed_tools` is the allow-list the caller gave the server's launcher; the server enforces it, and the client only checks that the server shows exactly those tools. Empty, there is no such check.
- `start_timeout_s` bounds the whole start (spawn, initialize, list the tools); it must be positive (L14).

Use it as an async context manager around `record_run`, started before the loop, so its start is outside the agent's time, and pass it in the providers. `tools()` returns the server's tools, none of them terminal. What fails where:
- A server that cannot be spawned, does not start within the timeout or closes while starting raises `InfraError("knowledge", ...)` from `__aenter__`.
- A server whose tools differ from the frozen manifest in any way (an extra or a missing tool, a changed description or schema), that lacks a tool of the allow-list or shows one outside it, raises `ManifestError` from `__aenter__`, listing every difference in `problems`. That is a fault of the study's set-up, not of the infrastructure, so the attempt is a harness defect (L13, C10).
- During the run, a closed connection or an internal server error raises `InfraError("knowledge", ...)`; any other MCP error, and a result the server marks as an error, is a `ToolOutput` with `is_error` for the model to read (L12). The round trip to the server is the agent's time: the client marks no wait.

### The frozen manifest

`expcore.tools.manifest` reads a server's tools, freezes them into a file per condition and compares a server with that file.

```python
@dataclass(frozen=True)
class FrozenManifest:
    tools: list[dict]  # sorted by name: name, description, input_schema, output_schema, annotations
    sha256: str        # SHA-256 of the canonical JSON of tools
    server_name: str | None = None
    server_version: str | None = None
    protocol_version: str | None = None
    @classmethod
    def load(cls, path: Path) -> FrozenManifest  # ManifestError for a file that is not one, or was edited
    def dump(self) -> str                        # the file's text: sorted keys, one trailing newline
```

Two manifests are equal when their names, descriptions, schemas and annotations are equal, byte for byte. `load` refuses a file whose stored `sha256` is not the hash of its tools, so a hand-edited manifest is refused rather than trusted. Freeze each condition's manifest from the server it will run, and check it again whenever the server is upgraded:

```sh
python -m expcore.tools.manifest freeze [--pass-env NAME ...] [--timeout SECONDS] OUT -- COMMAND [ARG ...]
python -m expcore.tools.manifest check [--pass-env NAME ...] [--timeout SECONDS] FROZEN -- COMMAND [ARG ...]
```

Both start the server with `COMMAND [ARG ...]`, as the provider will, and read its tools within `--timeout` seconds (120 by default). `--pass-env NAME`, repeatable, copies that variable of your environment to the server by name, so no value is on the command line. `freeze` writes the frozen manifest to `OUT` and prints its tool count and hash; `check` compares the live server with `FROZEN`. Each exits 0 on success, 1 on a mismatch (listed on standard error) and 2 on an error. For example, with a server installed as `notes-server`:

```sh
python -m expcore.tools.manifest freeze --pass-env NOTES_TOKEN manifests/basic.json -- notes-server --read-only
python -m expcore.tools.manifest check --pass-env NOTES_TOKEN manifests/basic.json -- notes-server --read-only
```

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
- An attempt that is set aside (infrastructure not declared as treatment, or a harness defect) is never scored. An attempt that ends in a declared treatment kind, after the model completed at least one turn, is, so the scorer must handle a partial record; the same kind before the first turn is set aside. Where the condition declares the kind with a target (record version 3), only a failure of that target while the fault injector held it down is scored; any other is set aside.
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

An executor runs them around `record_run`, as the example's does (`FactsServed` in `examples/tiny_study/plugin.py`):

```python
instruments = [FactsServed(shelf)]  # new objects for every run
identity = {"run_key": request.run_key, "factors": dict(request.identity.factors), "attempt": request.identity.attempt}
await asyncio.to_thread(start_instruments, instruments, identity)
record = await record_run(request, providers, system, user, config, code=code)
record = await asyncio.to_thread(finish_instruments, record, instruments)
record.write(request.run_dir)  # the record finish_instruments returned, which holds the snapshots
```

- `identity` is the request's [`RunIdentity`](#runexecutor) as a plain mapping: `run_key`, `factors` and `attempt`. It is there for an instrument that labels its reading or looks something up by run; one that reads only the platform's state, such as the platform probes, ignores it.
- One fresh instrument per run: an instrument holds one run's reading between `start` and `finish`, and the scheduler can run attempts at the same time, so an instrument shared between runs would mix their readings.
- Which attempts get a snapshot: `record_run` returns a record however the loop ended, an infrastructure failure included (a tool's `InfraError`, an endpoint that outlasted its retries), so `finish` runs and the snapshot is kept: a treatment attempt's scorer reads it, and an attempt set aside keeps it for diagnosis. When the executor itself raises, before the loop or instead of returning, there is no record and so no snapshot. An exception from `start` is the executor's: an `InfraError` is recorded as infrastructure of its kind, anything else as a harness defect (C10).

Core guarantees:
- `check_instruments` runs first on both: a namespace that is not lower-case letters, digits and underscores, and two instruments with one namespace, raise before anything runs.
- `finish_instruments` returns a new record with each snapshot in `RunRecord.instruments[namespace]`, a JSON object (NaN and values JSON cannot hold are refused). An instrument whose `finish` raises, or returns anything but a JSON object, is recorded as `{"ok": false, "error": <class name>}`: the attempt keeps its record and a scorer that needs the snapshot sees that it failed (O08).
- The snapshot is the only platform state a scorer reads (R13); replay serves it and asks the platform nothing.

Implementer must guarantee:
- `start` and `finish` run outside the agent's time: before the loop starts and after it ends. They do blocking I/O, so an async executor calls them with `asyncio.to_thread`. The platform's waits for the federation to settle (`expcore_onedata.consistency`) are not a declared wait kind and refuse to run on the thread of a running event loop.
- `finish` records what it read and never guesses: a reading that failed is `None` (with the failure beside it), not zero, and anything derived from it is `None` too (R10).
- The snapshot holds no secret and no identifier of the deployment beyond the labels the study's configuration gives.

The platform's own instrument is `expcore_onedata.probes.PlatformProbes` (namespace `platform_probes`): the block map of each tracked file on each tracked provider before and after the run, the records of the transfers the provisioner scheduled, the record of every transfer of the space that started in the run's window whoever started it (`space_transfers`), the bytes moved between provider pairs, attributed to a source only where exactly one other provider held those bytes at the start, and per receiving provider the transfer bytes into it and a lower bound on the bytes fetched on demand (`on_demand`). A provider's `gained_bytes` is placement gained: the bytes of the blocks a provider's block map shows at run end that it did not show at run start; what it showed at the start and no longer does is `lost_bytes`, kept apart, so it is not a net change, and it is not bytes on the wire. See its module docstring and `expcore_onedata.transferlog` for the snapshot's fields.

One namespace is read by the core: a fault injector writes under `fault_injector` (`expcore.records.injections.FAULT_NAMESPACE`) a snapshot holding the run key, the attempt and a list of `injections`, each with its `target` and `injected` (true only once the injector verified the target was down). Under a condition that declares a kind with a target, the treatment flag needs that target in this list for this run and attempt; a missing, failed or stale snapshot gives none, so the attempt is set aside.

The platform's fault injector is `expcore_onedata.faults.FaultInjector`. It has two kinds, registered by `expcore_onedata.sandbox.kinds`, the module a study lists in `[study] platforms` ([below](#infrastructure-kinds-of-a-platform-or-a-study)): `provider_outage`, which a condition may declare with a provider's label as its target, and `injector_failure`, its own failure to inject, clear or verify, which no condition may declare. Built only with `sanctioned = true` in `[faults]` of platform.toml and `sanctioned=True` from its caller (O10), it takes its target down in `start` and brings it back in `finish` (or in the `finally` of `session`, or of `running`, the async form that an executor uses, which keeps the start's thread and still clears when the run is cancelled), both outside the agent's time, so no wait kind covers either. A restore counts only when the provider's identity changed; the injection is journalled before it happens, and the next start settles one a crash left open (O11). Outage runs are exclusive with every other run: with `[faults]` configured, every run holds the federation's reader-writer lock (`expcore_onedata.admission.FederationLock`) from before its infrastructure starts until after its post-run check, taken by the campaign through the plugin's `admission` (`run_admission(config)`), shared for a baseline run and exclusive for an outage run, whose injector refuses a shared admission; a post-run check runs under the run's own admission, so no outage run can take the federation between a run's teardown and the check that judges it; the sandbox (`SandboxPlatform.launch`) and the tool server refuse to start without one, before they mint or start anything, and a sandbox start thread that a cancelled launch stops waiting for keeps a share of the admission until it has ended, so the federation stays held while it creates or removes anything. An admission names its federation (the lock file's resolved path and the digest of platform.toml), and one held for another federation is refused as none. The wait is admission, before the agent's clock starts, and a turnstile (`federation.intent`) queues later runs behind a waiting outage run so it is not starved. The platform's standard health check (`PlatformHealth`) runs the fault gate around the whole check, built in and with no opt-out: it recovers what a crashed run left open whenever no run is on the federation, then takes the federation shared, waiting at most `health_wait_s`, and holds it until the check has answered, so no injection starts mid-check and the content type is never measured through a provider an outage holds down; a check whose wait runs out because an outage run holds the federation answers deferred, which charges no run (see HealthCheck); to tell that wait from a fault, whoever holds the federation exclusively keeps a holder record beside the lock (pid, boot id, purpose, target label, start time), so a wait that runs out is deferred for a live holder, fails naming the target for a restore that failed, and fails as stuck, after recovering what it left open, for a holder that is gone, keeping the record marked `stuck_since` until an acquisition of the lock clears it; and the check fails while an injection stays open (recovery failed or its lock is stuck), so the scheduler's gate holds back every run, baseline conditions included. `provider_outage(config, name, detail)` builds the failure the platform attributes to a provider, named by its label (O12).

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
    treatment_targets: Mapping[str, str] = {}  # kind -> the target the condition declares for it
    models: ModelTable | None = None
    defect_log: DefectSink | None = None

    @property
    def run_key(self) -> str  # identity.run_key

@dataclass(frozen=True)
class RunIdentity:  # expcore.records.build
    run_key: str
    factors: Mapping[str, Any]
    attempt: int
```

`RunIdentity` says which attempt of which run the request is, and goes into the record as it is:
- `run_key` is the run's key text, the factor values joined in the order of `[identity] order` (`task=lake|access=direct|tools=basic|model=alpha-stub|seed=1`). It is the same for every attempt of the run, and `RunRequest.run_key` returns it.
- `factors` maps every factor of the run key to the run's level: the study's declared factors and the built-in `task`, `model` and `seed` (an integer). The condition is not among them; `study.schema.condition_named(factors, run_key)` names it.
- `attempt` is 1 for a run's first attempt and one more for each attempt of the run that was charged before it, an infrastructure failure set aside for instance, so a stub that should fail on the first try and succeed on the next tests `attempt`. An attempt that was interrupted (a killed process) is not charged, and its run is tried again under the same number. The record's file is named by `run_key` and `attempt` (`record_filename`), in `request.run_dir`, the directory of this try (`try<N>`, where N counts every try of the run, interrupted ones included, so it can be larger than `attempt`).

`record_run(request, providers, system, user, config, *, code, **options)` runs the loop with the request's backend, timer and treatment kinds and returns the record. It records with the request's model table, which the scheduler fills in for every run, so the key of each model in `models.toml` is redacted whatever the backend (R12). It also hands on the request's `defect_log`, so the traceback of a crash the loop catches goes to the local harness-error log, redacted with every secret the executor gave `record_run` (L08). A sink should not raise; if one does (the log cannot be written, or an executor passed a `defect_log` of its own), the record is still built and written, and the attempt stops dispatch once its result is stored, naming the failure (C17).

Core guarantees:
- The gates (health, harness stop, budget, unit pause) were passed, the lease the platform declared for the run is held for the whole attempt, and the timer is not started: the loop starts it. The attempt has a row (`running`) before the executor is called, so a kill leaves a run that the next start settles (C04).
- `InfraError` raised by the executor is recorded as infrastructure of its kind; any other exception as a harness defect (C10). The result is written as a marker, then to the store in one transaction.
- The record's treatment kinds and targets must equal the condition's, and its treatment flag the store's own decision from the outcome, the kinds and targets, the completed turns and the targets the fault injector recorded as injected (`AttemptResult.model_turns`, `recorded_treatment`, `first_turn_rule`, `target_rule` and `injected_targets`, all set by `result_from_record` from the record and its schema version), or the store refuses the result (C01). Record schema 2 added the first-turn rule and no field; schema 3 added the target of a fault and the target rule. Older records and outcome markers stay readable and are decided under their own rules.

Implementer must guarantee:
- Return the record `record_run` produced, and **write it to `request.run_dir`** (`record.write(request.run_dir)`) before returning: the core reads, checks and exports records from disk and does not write them for you.
- Script or configure the backend through the request; never build one.
- Pass `request.treatment_kinds` and `request.treatment_targets` unchanged (`record_run` does) and the code identity from `StudyContext.code`.
- Add the instruments' snapshots with `finish_instruments` before writing the record: it derives the treatment flag again, because under a declared target the fault injector's reading decides it.
- Close nothing the core owns (backend, timer, lease).

## HealthCheck

`expcore.campaign.healthgate.HealthCheck`; `Health` is a named tuple `(ok: bool, detail: str, deferred: bool = False)`.

```python
class HealthCheck(Protocol):
    def __call__(self) -> Health
```

Core guarantees: it is asked before an attempt starts and after it ends, and again at an interval while the gate is closed. A check that raises, or answers anything other than a `Health` (or an `(ok, detail)` pair with a real boolean), counts as a failed check, never as a pass or a crash. A failed pre-run check closes the gate, holds dispatch, and the run that found it is recorded as the outage's attempt, an infrastructure failure of `SchedulerConfig.health_kind` (C11). A failed post-run check turns an attempt that would have been scored into an infrastructure failure of that kind, or discards it as the open outage's. A deferred check (`Health(False, detail, deferred=True)`: the platform is held on purpose, by a run, and could not be looked at now) is a wait, never a pass or an outage: a deferred pre-run check holds its run for `recheck_s` without closing the gate or charging an attempt (event `run_deferred`), a deferred post-run check verified nothing, so any result `classify` would score, a treatment failure included, is set aside as `post_check_unverified`, a core kind no condition may declare (`expcore.campaign.attempts.unverified`) (event `post_run_check_deferred`), and only a passing check opens a closed gate. A deferral is bounded: its start is kept in the store's meta `health_deferral` (a restart keeps its age; `healthgate.fact` and `alarms` report it), a pass before or after a run ends it, and a pre-run check deferred longer than `SchedulerConfig.max_defer_s` (campaign.toml `[scheduler] max_defer_s`, default 3600) fails with the deferred detail, which names the holder's purpose and the target, closing the gate as a `blocking` outage: the all-dispatch hold is then not temporary, so `exit_when_blocked` stops the campaign. A gate already closed for another failure, whose own re-checks stay deferred past the bound, becomes blocking the same way. On a resume, a restored blocking outage holds dispatch without blocking it until the restored gate's first check has answered (bounded by `health_timeout_s`), so a platform that is healthy again reopens the campaign.

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
| Infrastructure kinds | `endpoint`, `key_rejected`, `sandbox`, `knowledge`, `unattributed_kill`, `ceiling`, `tool_plane_integrity`, `post_check_unverified`, plus kinds a platform registers ([below](#infrastructure-kinds-of-a-platform-or-a-study)) and kinds a study declares in `[study.kinds]` | `expcore.errors.CORE_KINDS`, `infra_kinds()`, `untreatable_kinds()` |
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

A study's `study.toml` is validated when it is loaded, before its plugin is imported, so a kind registered by the plugin, or by a platform only the plugin imports, is unknown to the conditions. A study that declares a platform's kind as a treatment lists the module that registers it in `[study] platforms`; the loader imports each listed module before it validates anything, and refuses one that cannot be imported, naming the file, the key, the module and the type of the exception, never its text. For the Onedata layer, that module is `expcore_onedata.sandbox.kinds`, which registers every kind the layer raises: `data_gate_integrity` and the fault injector's `provider_outage` and `injector_failure`. A study without a platform declares its own kinds in `[study.kinds]`, as `name = { description = "...", treatable = true }`. They are held in the vocabulary only while the study is loaded and while the pipeline works on it (`expcore.errors.study_kinds`), so they do not outlast it, and a failed load leaves none behind; a platform's kinds stay for the whole process. The scopes are counted: two studies that overlap in one process each keep their kinds and can see the other's for the overlap, which can only widen validation, and declaring one name differently while they overlap is refused, naming the kind. A study's kind that is a core kind or a platform's is refused; so is a platform registering a kind of the study in scope. Neither key is part of the factor schema (study.toml's hash is in the campaign fingerprint, as before), so a study that uses neither keeps its fingerprint and schema digest. [`study-guide.md`](study-guide.md#treatment-kinds) shows both keys.

A condition that names a kind outside the vocabulary is refused as unknown, with the treatable kinds as the choices; a known kind that is not treatable is refused as one that can never be a treatment, with the reason its registration gave.
