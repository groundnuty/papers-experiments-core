# Interfaces

What a study (or a platform) implements to plug into the core, with the signature as the code has it, what the core guarantees, what the implementer must guarantee, and the closed vocabularies. [`architecture.md`](architecture.md) says why the interfaces are shaped this way; this file says what they are in v0.1. The runnable example is [`examples/tiny_study/`](../examples/tiny_study/).

All of them are `typing.Protocol`s, so a test double needs no inheritance. Signatures below are copied from the code; where a docstring in the code says more, the code wins.

## What v0.1 has

| Architecture name | In v0.1 |
|---|---|
| `Study` | the study directory (`study.toml` and its siblings, which may declare the [side files](#side-files-of-a-record) of its records) plus one plugin module whose `build(context)` returns a `StudyPlugin` ([below](#the-study-plugin)) |
| `Platform`, `Sandbox` | not in the core. The platform layer (`expcore_onedata`, steps 4a and 4b) supplies a `HealthCheck`, a `ResourceProbe` (leased platform users), its own `ToolProvider`s and an `Instrument` (the platform probes) through the same `StudyPlugin`; its sandbox is [described below](#the-platform-layers-sandbox). A platform of another kind is a distribution of its own that plugs in through the same `StudyPlugin` and the same protocols: [the study guide's section 10](study-guide.md#10-a-platform-of-your-own) lists what it needs, and `examples/sparql_platform/` is one |
| `ToolProvider`, `Scorer`, `Instrument` | protocols: [`ToolProvider`](#toolprovider-and-callcontext), [`Scorer`](#scorer) and [`Instrument`](#instrument) (a protocol since step 4b2) |
| `ModelBackend`, `RunExecutor`, `HealthCheck`, `ResourceProbe` | protocols, below |

## The study plugin

A study directory holds `study.toml`, `models.toml`, `campaign.toml`, `preregistration.toml` and the plugin, the Python file `[study] plugin` names, with any modules it imports from beside it; `[study] requires` names the distributions beyond the core that the study's code imports. `expcore.study.config.load_study(directory)` validates all of them against each other. `expcore.study.plugin.load_plugin` imports the file and calls

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
    tools: Mapping[str, str]  # per condition: SHA-256 of its tool set (tools_sha256, below)
    health: HealthCheck | None = None
    probe: ResourceProbe | None = None
    lease_group: Callable[[Mapping[str, Any]], str | None] = lambda factors: None
    lease_keys: Callable[[str], Sequence[str]] = lambda group: ()
    platform: Mapping[str, Any] = field(default_factory=dict)  # the platform's settings that make one experiment
    backend_factory: BackendFactory | None = None  # None: the core's default_backend_factory
```

The core guarantees: the code identity is computed once, from the study directory and the `[study] code` roots, and refused if it is unknown, stale or (without `--allow-dirty`) dirty; the plugin is rejected at load if a condition has no prompt or tool hash, two scorers share a name, or `platform` is not a mapping; `prompts`, `tools`, the scorers' names and versions, the hash of each configuration file and, when not empty, `platform` go into the campaign fingerprint, so a resume under any change is refused (C06). A study on the Onedata layer passes `platform=config.fingerprint()`, the platform file's TLS mode with the CA bundle's SHA-256, so a resume under another mode or bundle is refused.

The implementer must guarantee: `build` has no side effects beyond constructing objects; the hashes describe what the model sees under each condition; nothing is named by the core, so every name lives in the study.

**A plugin over several files.** `import_plugin_module` imports the plugin as a package whose path is the plugin's own directory, named `expcore_study_plugin_` plus a hash of the plugin's path. A module beside it is imported relatively (`from .scoring import Scorer`, or `from . import scoring`) and is named below that package, so two studies' `scoring.py` never meet in one process. Every module of the package is compiled from its file's bytes on each load, never from cached bytecode, so what runs is what the code identity hashed, and no bytecode is written into the study. `import scoring` finds nothing, since the study directory is not on the module search path; the refusal names the fix.

The core holds the code that runs to the code identity by where it comes from, not by its module name (`expcore.study.plugin_import`). While the plugin is imported and while `build` runs, it records every file whose code is executed, and it then finds every module in `sys.modules` whose file lies in the study directory; code the recorded environment covers does not count: the core's own packages, the running environment (a virtual environment inside the study directory, for one) and any installed distribution's code, editable or not, found as the environment record finds it (its `RECORD`, or for an editable install where it imports from), as does code below a directory the identity skips. It refuses the load, naming `study.toml` and the file relative to the study, when such a file is one `[study] code` does not cover, and when it is not a module of the plugin's package: a file imported through the module search path, or loaded by path (`spec_from_file_location`), would be cached under a name another study could be served, or run from stale bytecode. Before every import, and after a refused one, it drops every module of the study's own code, whatever its name, and every module of the package, so no study is served another's; it never drops a module of the core or of an installed distribution. The replay check's import is held to the same rules.

The implementer must guarantee that every file the plugin imports is in `[study] code` (and so in `package.toml`, whose build requires every file the identity covers), and that it imports each at the plugin's top level or in `build`: a module first imported while a run is under way escapes the check. The check sees code only: a data file the plugin reads by path (a prompt, a table) is not checked, and the identity covers it only if `[study] code` lists it, as the example lists `prompt.txt`. A backend factory in a module beside the plugin enters the fingerprint as `scoring.Factory`, without the hashed name.

The command line is `python -m expcore run|check|analyze|report|package|package-check|example` ([`cli.py`](../src/expcore/cli.py)). Output goes under `<study>/out/`; `package` writes a replication package where `--out` says, and `example` writes the example study into a new directory. How to use them is in [`study-guide.md`](study-guide.md).

What the analysis may use. `check` replays every record and also copies the campaign store into the batch (`campaign-snapshot.sqlite`, made with SQLite's online backup over a read-only connection, which copies pages and so needs no particular SQLite version; then each attempt's directory is written relative to the batch and the copy is rebuilt with a plain `VACUUM`, so it holds no path of the machine the campaign ran on; and integrity-checked) and hashes the copy with the records. It lists the records it replayed (`replayed`) and refuses a run record named by a hashed-only pattern, so a check cannot replay nothing and still vouch for the records beside it. It refuses a store with attempts still running unless `--partial`, which the check records. `analyze` and `report` then use only those bytes: the snapshot, never the live store, and the replayed records, each read once and compared with the check's hash. They cross-validate the two: every record maps to an attempt of the snapshot, and every attempt scored (state `done`, a scored treatment failure included) has a replayed record that agrees with it on run key, attempt, outcome and treatment flag. Any mismatch is refused, naming it. With `--partial`, attempts still running at the check are tolerated, a final attempt no replayed record covers is left out. Every saved output (`export.json`, `analysis.json`, `report.md`) says whether the analysis is partial and lists the attempts left out (run key, attempt, reason); `analysed_attempts` holds only what was scored, and every count in the report is of that population, with the left-out runs' attempts in a column of their own.

Side files are verified with the records. A file a run writes beside its record, which the record names by its SHA-256, is declared in `study.toml` ([below](#side-files-of-a-record)). `check` hashes every file of a declared name in the batch it parses into its inputs, never replays one, and pairs each with the one record in its directory; the check records the declaration (`side_files`) and each side file's hash in `inputs`. `analyze`, `report`, `regenerate` and a package's `make check` refuse a check made under another declaration, refuse a side file changed, removed or added since the check, and read each side file once to pair it with its record again, whatever the check file claims. A study that declares none is checked exactly as before, and its check file holds no `side_files`.

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

`ToolSpec(name, description, input_schema, terminal=False)` is one tool the agent sees: the name is 1 to 64 letters, digits, underscores or dashes and the schema is a JSON schema of type object, both checked when the spec is built. A successful call to a `terminal` tool ends the attempt as `finished` (L09). What a call returns is `ToolOutput(text, is_error=False, error_class=None, source_truncated=None)`:

- `text` is the result, or for an error the message. `is_error` makes the call an error: the model reads its text as a failed call and the record gives the call the status `error`.
- `error_class` names the kind of failure and is allowed only with `is_error`; an error without one is `ToolError`. The model and the record see an error as `<error_class>: <text>` (the class alone when `text` is empty), and the record keeps the class in the call's `error_class` as well. So `text` should not repeat the class: a message that starts `QuerySyntax: ...` under the class `QuerySyntax` reaches the model as `QuerySyntax: QuerySyntax: ...`.
- `source_truncated` is the platform's word on a cut it made itself (L07). `True`: the source hit its own row or size limit, so the result may be cut. `False`: the provider checked, and the source did not cut. `None`, the default: the provider says nothing, and the record keeps `null`. A value that is not one of the three is refused. The core never sets or reads it: it copies it into the call's record, an error's included.

A platform's own cut is told apart from the core's cap by these two flags. The core caps what the model sees at `output_cap_chars` (`[agent]` in `campaign.toml`) after the provider returns: a longer text is cut, a notice that counts inside the cap ends it, the full text stays in the call's `raw_text`, and `truncated` in the record is `True` (L06). A platform that cuts at its own limit returns text that is already short, so the core cuts nothing: `truncated` stays `False`, the platform's text is the call's `raw_text`, and only `source_truncated=True` tells the platform's cut from a whole result. The core adds nothing to the text of the platform's cut, so the platform says in the text that it cut, as `examples/sparql_platform` does with a last line. Both flags are `True` on one call when the platform's result is still longer than the cap. A platform that wants the model to meet its own cut and never the core's sets `output_cap_chars` above what its capped result can hold, as the example's `campaign.toml` does.

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

### FunctionProvider

`expcore.tools.function_provider.FunctionProvider` makes a provider of plain functions, the usual way for a study or a platform to give the agent a tool that is Python code.

```python
class FunctionProvider:  # expcore.tools.function_provider
    def __init__(self, tools: Sequence[tuple[ToolSpec, ToolFunction]])  # ValueError: a tool name twice, a function that cannot take timeout_s
    def tools(self) -> Sequence[ToolSpec]  # the specs, in the order given
    def abandoned_threads(self) -> int     # cancelled calls whose thread may still be running
```

The calling convention:
- It takes `(ToolSpec, function)` pairs. A function serves the tool of its spec, and a tool name appears once in a provider.
- A plain function is `f(arguments, *, timeout_s)`. `arguments` is the call's arguments as a dict, checked against `input_schema` before the call (L02); the loop hands the provider a copy, so a function may change it. `timeout_s` is the time the run has left (L05). The function must accept the keyword (a `timeout_s` parameter, or `**kwargs`), or the provider refuses it when it is built, naming the tool: a thread cannot be killed, so a function bounds its own work with it (L15).
- A plain function runs in a worker thread (`asyncio.to_thread`), so it may block. A coroutine function, `async def f(arguments)`, runs on the event loop instead, takes no `timeout_s` and must not block.
- `ctx` is passed only to a function that declares a `ctx` parameter. A coroutine function gets the call context and uses `async with ctx.infrastructure_wait(kind)`. A plain function, in its thread, gets a stand-in that it uses with a plain `with ctx.infrastructure_wait(kind)`.
- It returns a `str` (the result text) or a `ToolOutput`. Anything else is turned into text with `str()`, so return one of the two. An error the model should read is a `ToolOutput` with `is_error`; a function that raises is a defect, or infrastructure if it raises `InfraError` (see above).
- A call the loop cancels while its thread runs (the run's time ran out) returns at once, and the thread goes on until the function returns. Until then `abandoned_threads()` counts it, the loop records the count, and every new call to the provider is refused with the tool error `AbandonedThread`.

`SparqlTools.functions()` in `examples/sparql_platform` builds the pairs of a platform's tools, and the tiny example's plugin has two plain functions.

### Several providers, and the hash of a tool set

`record_run(request, providers, ...)` takes a sequence of providers of any kind (`FunctionProvider`, `McpToolProvider`, a platform's own) and builds one tool box from them:
- The box offers the tools of every provider, and a call goes to the provider that offered its name.
- The model is told the tools sorted by name. The record's `tools` lists the specs in the order of the sequence, each provider's in the order of its `tools()`. No order changes a hash.
- A name offered by two providers is refused: `record_run` raises `ValueError("two providers offer a tool named ...")` before the loop starts, and nothing picks a winner. It surfaces from the executor, so it is a harness defect (C10): the attempt is set aside, keeping the exception's class and a stack hash, and its run is pending again. It shows at the first run and is never a scored result. One `FunctionProvider` refuses a name twice when it is built.

```python
def tools_sha256(specs: Sequence[ToolSpec]) -> str  # expcore.study.plugin
```

`tools_sha256` is the SHA-256 of a tool set: each tool's name, description, `input_schema` and `terminal` flag, in name order. A study gives one per condition as `StudyPlugin.tools`, over the specs its executor passes to `record_run` under that condition, and it goes into the campaign fingerprint: a changed description or schema, an added tool or a changed `terminal` flag refuses a resume (C06). It does not see a name clash, which shows at the first run as above. It is not the hash a record carries. `RunRecord.prompt["tools_sha256"]` is the tool box's, taken over the descriptions as the model saw them, each with the output-cap sentence appended, so it also changes with `output_cap_chars` (L06). The core compares neither with the other, nor the declared hash with the tools the executor then passes: the plugin guarantees that the hash describes what the model sees under the condition.

### Two tool hashes

Two hashes describe a run's tools, over different text, so they differ even for the same tools, by design:

| Hash | What it covers | Where it goes |
|---|---|---|
| `expcore.study.plugin.tools_sha256(specs)` | the tools as the study declares them: each `ToolSpec`'s name, description, input schema and terminal flag, in name order | the plugin's `StudyPlugin.tools`, one per condition, which the campaign fingerprint holds (C06) |
| `ToolBox.manifest_sha256` (`expcore.loop.toolbox`) | the definitions the model is sent: each tool's name, input schema and description with the output-cap sentence the tool box appends (`describe_cap`, which states `output_cap_chars`), plus each tool's terminal flag, which the model is not told | the record, as `prompt.tools_sha256` despite its name; the replay check rebuilds the tool box from the record's `tools` and the cap and refuses a record whose hash differs (R03) |

The record also keeps `tools`, each tool as its provider gave it, before the cap sentence: exactly what `tools_sha256` hashes. The cap itself is in the fingerprint through the loop's caps, so neither hash leaves it unguarded.

To check that conditions differ only in which tools they expose, compare `tools_sha256` values: the plugin's per condition before the runs (conditions meant to differ only in tools have equal `prompts` hashes and different `tools` hashes), and, after them, the hash recomputed from each record's `tools` (`tools_sha256([ToolSpec(**t) for t in record.tools])`) against the plugin's hash for the run's condition, which shows that the executor exposed what the plugin declared. That value does not depend on the output cap, so it compares across campaigns too. The records' `prompt.tools_sha256` values may be compared with each other within one campaign, where every run has the same cap, but never with a `tools_sha256`. An MCP server's frozen manifest has a third hash, `FrozenManifest.sha256` ([below](#the-frozen-manifest)), over the server's own tool listing.

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

### Side files of a record

An instrument whose reading is too large for the record, a compressed log of the byte ranges a run read for instance, can write it to a file beside the record and keep its SHA-256 in the snapshot. The study declares each such file in `study.toml` (`expcore.replay.side_files`, validated by `expcore.study.config` when the study loads):

```toml
[[study.side_files]]
pattern = "extent_ranges.json.gz"               # one file name, looked for beside each record
hash_field = "instruments.extent.side_sha256"   # the record field, a dotted path, that holds the file's SHA-256
```

- `pattern` is one literal file name, never a glob or a path, because each side file is paired with the one record in its own directory: the attempt directory `request.run_dir`, which holds one record. A name that starts with a dot is refused (the check never reads such a file), and so is a name the campaign or the check writes: a record's, `attempt-marker.json`, `campaign-snapshot.sqlite`, `replay-check.json`. A name ending in `.json` is a side file, never taken for a record.
- `hash_field` is record keys joined by dots, looked up in the record's stored JSON. Each pattern and each field is declared once.
- The declaration is part of `study.toml`, whose hash is in the campaign fingerprint, so a campaign resumed under another declaration is refused (C06).

Core guarantees, in `check` and again wherever the analysis reads the batch (R14), each refusal naming the file:
- a record whose field holds a hash with no such file beside it is refused;
- a side file whose bytes do not have the hash its own record names is refused, two records' side files swapped included;
- a file of a declared name in a directory that holds no record (left from an earlier campaign or attempt) is refused against its own path, wherever it lies in the batch, and never hashed into a passing check;
- a side file beside a record whose field is absent or `null` is refused; a record with neither the field nor the file is fine, since that run made no side file;
- a field that holds anything but 64 lower-case hexadecimal digits is refused;
- a hash or a side file in a directory that holds more than one record is refused, since it cannot be paired;
- the side files are found in exactly the batch directory the check parses, however the command names the study and the batch.

Implementer must guarantee: the executor writes the side file into `request.run_dir` with the record, and the record names it. An attempt killed after writing its side file and before writing its record leaves an orphan, which `check` refuses, naming it, until it is removed. A scorer still reads only the record (R13): a measure computed from a side file's content is the study's own code reading the file, outside the bytes the analysis reads once and verifies.

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
- The record's treatment kinds must equal the condition's, and its treatment flag the store's own decision from the outcome, the kinds and the completed turns (`AttemptResult.model_turns`, `recorded_treatment` and `first_turn_rule`, all set by `result_from_record` from the record and its schema version), or the store refuses the result (C01). Record schema 2 added the first-turn rule and no field; schema-1 records and schema-1 outcome markers stay readable and are decided without it.

Implementer must guarantee:
- Return the record `record_run` produced, and **write it to `request.run_dir`** (`record.write(request.run_dir)`) before returning: the core reads, checks and exports records from disk and does not write them for you.
- Script or configure the backend through the request; never build one.
- Pass `request.treatment_kinds` unchanged (`record_run` does) and the code identity from `StudyContext.code`.
- Close nothing the core owns (backend, timer, lease).

### What an executor reads

An executor keeps the `StudyContext` that `build` received (the example's keeps it as `self.context`) and reads the loaded configuration from `context.study`, an `expcore.study.config.StudyConfig`, beside what the request carries. All of it is read-only, and every field comes from the study's configuration files, whose hashes are in the campaign fingerprint, so nothing an executor reads there can change under a resumed campaign (C06).

| To get | Read | What it is |
|---|---|---|
| the run's levels | `request.identity.factors` | every factor of the run key: the declared ones and the built-in `task`, `model` and `seed` |
| the run's condition | `study.schema.condition_named(factors, request.run_key)` | the condition's name |
| a task's attributes | `study.tasks[task_id].attributes` | the text keys of `[tasks.<id>]` in `study.toml`; `study.tasks[task_id].id` is the id |
| the loop's caps | `study.campaign.agent.limits` | `max_tool_rounds`, `time_cap_s`, `token_budget`, `output_cap_chars` and `max_output_tokens` (`expcore.config.models.Limits`); `study.campaign.agent`, the whole loop configuration, goes to `record_run` |
| the plan | `study.campaign.models`, `.seeds`, `.order_seed`; `study.manifest` | the models and seeds of the plan, its order seed, and every run in the seeded order |
| a model's entry | `study.models.resolve(key)` with the run's `factors["model"]`; `request.models` is the same table (`ModelTable \| None`) | a `ModelSpec`: backend, API id, limits, price, status; the run's backend itself is `request.backend` |
| a condition's settings | `study.schema.conditions[name]`, `.condition_tasks[name]`, `.treatment_kinds[name]` | its levels, its task list (`None`: every task) and its treatment kinds; the run's own kinds are `request.treatment_kinds`, passed on unchanged |
| the study's name, its own kinds, its distributions | `study.name`, `study.kinds`, `study.requires` | |
| the code identity | `context.code` | passed to `record_run` unchanged |

The rest has no place in a run: `study.prereg` is the analysis plan, and `study.root`, `study.plugin_file` and `study.scope` hold paths of the machine, which must reach neither a prompt nor a record (the plugin finds its own files from `__file__`, as the example's `HERE` does). A prompt filled per task takes its values from these fields, as the example's does with the task's `region` (a value that is not text is rendered with `str`):

```python
task, limits = study.tasks[factors["task"]], study.campaign.agent.limits
slots = {"TASK": task.id, "REGION": task.attributes["region"], "ROUNDS": limits.max_tool_rounds}
user = template.render(slots, study_run=True)  # for a template with a {ROUNDS} slot, which the example's has not
model = study.models.resolve(factors["model"])  # the run's ModelSpec, looked up by the model key of its run key
```

## PromptTemplate

`expcore.prompt.templates`. A template is text with named slots. The executor renders it for each run and passes the result to `record_run`, which records the text with the template's hash and the values of its slots.

```python
@dataclass(frozen=True)
class PromptTemplate:
    text: str
    slot_names: tuple[str, ...]  # a property: the template's slots, sorted, each once
    sha256: str                  # a property: the SHA-256 of the template's text
    @classmethod
    def from_file(cls, path: Path, *, expected_sha256: str | None = None) -> PromptTemplate
    def render(self, values: Mapping[str, Any], *, study_run: bool = False) -> RenderedPrompt

@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    template_sha256: str
    rendered_sha256: str
    slots: dict[str, str]  # the values inserted, as text
```

- **Slots.** A slot is `{NAME}`: an opening brace, a capital letter, any capital letters, digits and underscores, and a closing brace. Every other brace is text and is sent as it is, so a JSON example in a prompt (`{"answer": "...", "sources": [{"id": 1}]}`), `{}` or `{lower}` needs no escape. The slot form itself has no escape: to show `{NAME}` to the model, give it as the value of a slot.
- **Values.** `render` needs a value for every slot, and refuses a value for a name the template does not have, or for a name that cannot be a slot. A value is made text with `str`, and one that is `None` or blank (empty, or only whitespace) is refused, so a condition with nothing to add in a slot still says something short (the example's `HINTS`). A value is inserted as it is and never searched for slots again.
- **`study_run=True`** refuses the draft marker `PLACEHOLDER`, in any case, in the template or in any value, so a draft prompt never reaches a model of a study run; it changes nothing else. The example's executor passes it. A pilot or a test that renders a draft leaves it off.
- **`from_file(path, expected_sha256=...)`** reads a template as UTF-8 and, given `expected_sha256`, refuses a file whose text has another hash, to pin the template to the hash a protocol froze.
- **Refusals** raise `PromptError`, a `ValueError` that names the slot. Raised in an executor, it makes the attempt a harness defect (C10).
- **The record.** Pass the `RenderedPrompt` itself to `record_run` as `system` or `user`: the record keeps its `text`, the text's `sha256`, `template_sha256` and `slots`. A plain string is recorded with neither a template hash nor slots. The hash a plugin gives for a condition (`StudyPlugin.prompts`) is the template rendered with what the condition decides and stand-ins for what each task fills in, as the example's `build` does; the tasks' values come from `study.toml`, whose hash the fingerprint holds.
- **Nesting.** To compose templates, render the inner one first and pass its `text` as the value of a slot of the outer one, as in `outer.render({"RULES": inner.render(rules).text, ...})`. A value is never searched again, so the inner text's braces stay as they are. **Known limit:** the record then holds the outer template's hash and the inner text as that slot's value, and neither the inner template's hash nor its slot values. A study that needs them pins the inner template with `expected_sha256`, or records its hash and slots itself, in an instrument's snapshot for instance.

## HealthCheck

`expcore.campaign.healthgate.HealthCheck`; `Health` is a named tuple `(ok: bool, detail: str)`.

```python
class HealthCheck(Protocol):
    def __call__(self) -> Health
```

Core guarantees: it is asked before an attempt starts and after it ends, and again at an interval while the gate is closed. A check that raises, or answers anything other than a `Health` (or an `(ok, detail)` pair with a real boolean), counts as a failed check, never as a pass or a crash. A failed pre-run check closes the gate, holds dispatch, and the run that found it is recorded as the outage's attempt, an infrastructure failure of `SchedulerConfig.health_kind` (C11): the `health_kind` key under `[scheduler]` in `campaign.toml`, `sandbox` unless the study names another, as [the study guide](study-guide.md#the-schedulers-kinds) shows. A failed post-run check turns an attempt that would have been scored into an infrastructure failure of that kind, or discards it as the open outage's.

Implementer must guarantee: it is quick (bounded by `health_timeout_s`), side-effect free, and `detail` holds no secret. `health_kind` must not be a kind any condition declares as its treatment, and, like `identity_kind` (the kind of a run whose endpoint changed its served identity, C16, `endpoint` by default), it must be a kind known when the study is loaded, or the load is refused. Each infrastructure kind is treatable or not: `tool_plane_integrity` and any kind a platform registers with `treatable=False` cannot be declared in `infrastructure_is_treatment`, and the factor schema refuses to load a condition that does, naming the file and the key; the scheduler refuses, at its start, a campaign whose health or identity kind a condition declares.

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
- Before a campaign admits its first run, the platform's standard health check (`health.PlatformHealth`, which composes `precampaign.PreCampaignHealth` itself, with no opt-out) measures the content type the gate streams on when a data path is REST: it reads `[sandbox] content_check_file` (required with a REST level) through the local provider, whole and by its first byte, and a type other than `application/octet-stream` with 200 and 206, or one it cannot measure, refuses the campaign: the verdict is kept for the campaign, so the health gate stays closed with the refusal as its detail and no run is admitted; with no REST data path the file is never read and the campaign is admitted. The day-0 check `content_type` is the same check, for the operator (`precampaign.measure_content_type`, which also says whether a type was measured): it passes only on a measured right type, fails on a measured wrong one, and is unknown whenever it measured nothing, with no REST data path included (O05, O08, O11).
- The reads of the content check, like every mutation and range read by file id, pass the client's mutation gate (`client.OnezoneClient`, O09): a request under `/spaces/{id}` goes only to a space whose live name starts with one of `[space] prefixes`, and a file addressed by id must lie in the configured space, whose live name must start with one too. A file's space is found by walking its `parent_id` links through the provider up to the configured space's root (the `fileId` of the provider's `GET /spaces/{id}`), at most `client.MAX_SPACE_DEPTH` (64) links; a walk that ends above every known root or past the bound resolves to no space and nothing is sent (`OutOfScope`). Roots and parent links are kept as the provider's that gave them, since a file's space does not change: at most `client.MAX_KEPT_LINKS` links (least recently used dropped first, all dropped by `close()`), and a walk that passes the bound over kept links walks once more, every link read anew, before it refuses. The space's name is read from Onezone again before every request. Every read of the gate is the client's own, with the admin credential and the platform's `[tls]`, never through the data gate, so none counts as the agent's. A gate read that fails refuses the request unsent (`ScopeUnread`, keeping the read's status, error id and transport flag); `OutOfScope`, `ScopeUnread` and `MintNotSent` are `NotSent`, which a caller tells from an answer of the platform.
- The day-0 checks report unknown, never passed, for what they did not measure: `read_only` passes only when every supporting provider refused a write it was sent (401, 403, or exactly 400 `posix` `eacces`, as Onedata 25.0 refuses it), and a write that was never sent (the gate refused it or could not check it, or the space root could not be read) makes it unknown with the reason; a write answered otherwise is unknown with its status, error id and errno.
- An entry is absent only when a provider answers it with a 404 or exactly 400 `posix` `enoent` (`client.answered_absent`), as 25.0 answers a deleted entry; teardown and the day-0 clean-up stop as soon as their consecutive reads show it, and any other answer, another 400 or an unreadable body included, is never absence: they read on to their bound and report the entry left. The absence is the provider's that was read; a directory's delete may still be under way on another provider or on storage (#114). Every raw body a mutation sends is marked `application/octet-stream`, which 25.0 requires of a content upload.
- Every answer that is no file's content is buffered whole up to 4 MiB, so each is bounded by the request: a listing whose explicit `limit` (in the query or a JSON body) is above `gatepolicy.MAX_LIST_LIMIT`, 1000, the server's own default page, is refused before anything is forwarded, as a 400 the agent reads; a listing without a limit passes unchanged. The worst case of a page of 1000, and the bound on the other non-content answers, are worked out in `gatepolicy.py` (O05, O16).
- A killed cell's cause comes from the cgroup counters, the kernel log, the host killer's journal and the container's state, never from the exit code. The agent's own limit is its outcome; a failure of the host or a component raises `InfraError("sandbox")`; a kill nothing attributes raises `InfraError("unattributed_kill")`, which the core retries within the attempt limit (O17). Errors the agent causes through the mount (ENOENT, EROFS, EISDIR) are not counted against the platform (O18).
- A message of any exception names roles, keys and positions, never a configured value (image, pool, path, login, level) or the tail of a tool's stderr (which goes to the log).

Not carried over from the earlier study: the gate's whole-file and merged-listing modes (that study's manipulated variables), the tracer and its checks, the capacity probes tied to two data layouts, and the replication branch of its fourth condition.

## Estimator

`expcore.stats.estimators`. A contrast names its estimator in `preregistration.toml` (`estimator = "..."`), with the estimator's parameters in `[estimators.<name>]`. The core registers one, `wilcoxon_cluster_bootstrap`.

```python
class Estimator(Protocol):
    def __call__(self, paired: PairedDifferences, settings: EstimatorSettings) -> EstimatorResult

@dataclass(frozen=True)
class PairedDifferences:
    clusters: tuple[str, ...]  # the rows: the clusters with at least one paired cell, sorted
    models: tuple[str, ...]    # the columns: the models with at least one paired cell, sorted
    matrix: np.ndarray         # clusters x models, the mean difference A - B of each cell; NaN where it has no pair
    cluster_differences: np.ndarray  # a property: per cluster, the equal-weight mean over its models with a cell

@dataclass(frozen=True)
class EstimatorSettings:
    replicates: int            # [bootstrap] replicates
    level: float               # the interval's level: [bootstrap] ci_level, or 1 - 2 alpha for the equivalence test
    seed: tuple[int, ...]      # [bootstrap] seed and a digest of the contrast's name: the only entropy
    params: Mapping[str, Any]  # [estimators.<name>] as preregistered

@dataclass(frozen=True)
class EstimatorResult:
    estimate: float            # the effect A - B; NaN where it cannot be computed, as every float here
    p_value: float             # two-sided
    ci_low: float
    ci_high: float
    n_clusters: int
    per_model: tuple[ModelEstimate, ...] = ()  # ModelEstimate(model, estimate, ci_low, ci_high, n_undefined_replicates)
    counts: Mapping[str, int] = field(default_factory=dict)  # the estimator's own counts, such as zero_dropped

def register(name: str, *, check_params=None, p_values=None)  # a decorator: @register("name") above the function
```

How pairs become the matrix's cells is in [the study guide's section 5](study-guide.md#how-pairs-become-a-contrasts-input).

Core guarantees:
- The matrix is read-only, and no row or column of it is all NaN; a contrast with no pair at all gets a `PairedDifferences` with no cluster.
- `check_params(params)`, given at registration, is called when the preregistration is loaded, so a bad parameter is refused naming `[estimators.<name>]`; without one, any parameter is refused.
- For a contrast with an `equivalence_margin` the analysis calls the estimator a second time with `level` at 1 - 2 alpha and the same seed. A NaN p-value counts as p = 1 in the Holm family.
- `p_values`, given at registration, is used to simulate the minimum detectable effect with the contrast's own parameters (S06, S09). Without it the contrast's minimum detectable effect is NaN (`null` in `analysis.json`).

Implementer must guarantee:
- The estimator is a pure function of its arguments: called twice with equal settings, it returns equal results, whatever ran in between. NaN, never a guess, where it cannot compute a value.
- `p_values(studies, params)` returns, for each row of a studies x clusters array of cluster differences, exactly the p-value the estimator reports for that row.
- The name is lower-case letters, digits and underscores, registered once: a second registration of a name is refused.

**Where a study's estimator is registered.** The preregistration is validated when the study is loaded, before its plugin is imported, so a name only the plugin registers is unknown at that point and the contrast is refused (`unknown estimator 'name'; registered: wilcoxon_cluster_bootstrap`). A study cannot register its estimator through its plugin. It registers it at the top level of a module that `[study] platforms` names, which the loader imports before it validates anything, as it does for a platform's infrastructure kinds. That module must be importable, so it is installed code, not a file of the study directory, which is not on the module search path: a distribution of the study's own, named in `[study] requires`, so it is part of the campaign's environment and the replication package vendors its wheel. Its version speaks for its code, so change the version when the estimator changes. Once registered, the estimator stays registered for the rest of the process.

## Other protocols the core defines

| Protocol | Where | Signature |
|---|---|---|
| `Estimator` | `expcore.stats.estimators` | `__call__(self, paired: PairedDifferences, settings: EstimatorSettings) -> EstimatorResult`, [above](#estimator) |
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
