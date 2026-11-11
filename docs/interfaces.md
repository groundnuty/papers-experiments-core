# Interfaces

What a study (or a platform) implements to plug into the core, with the signature as the code has it, what the core guarantees, what the implementer must guarantee, and the closed vocabularies. [`architecture.md`](architecture.md) says why the interfaces are shaped this way; this file says what they are in v0.1. The runnable example is [`examples/tiny_study/`](../examples/tiny_study/).

All of them are `typing.Protocol`s, so a test double needs no inheritance. Signatures below are copied from the code; where a docstring in the code says more, the code wins.

## What v0.1 has

| Architecture name | In v0.1 |
|---|---|
| `Study` | the study directory (`study.toml` and its siblings, which may declare the [side files](#side-files-of-a-record) of its records) plus one plugin module whose `build(context)` returns a `StudyPlugin` ([below](#the-study-plugin)) |
| `Platform`, `Sandbox` | not in the core. The platform layer (`expcore_onedata`, steps 4a and 4b) supplies a `HealthCheck`, a `ResourceProbe` (leased platform users), its own `ToolProvider`s and two `Instrument`s (the platform probes, and the bytes the agent read through its data path) through the same `StudyPlugin`; its sandbox is [described below](#the-platform-layers-sandbox). A platform of another kind is a distribution of its own that plugs in through the same `StudyPlugin` and the same protocols: [the study guide's section 10](study-guide.md#10-a-platform-of-your-own) lists what it needs, and `examples/sparql_platform/` is one |
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

The core guarantees: the code identity is computed once, from the study directory and the `[study] code` roots, and refused if it is unknown, stale or (without `--allow-dirty`) dirty; the plugin is rejected at load if a condition has no prompt or tool hash, two scorers share a name, or `platform` is not a mapping; `prompts`, `tools`, the scorers' names and versions, the hash of each configuration file and, when not empty, `platform` go into the campaign fingerprint, so a resume under any change is refused (C06). A study on the Onedata layer passes `platform=config.fingerprint()`: the platform file's TLS mode with the CA bundle's SHA-256 and, with a `[sandbox]`, each level's data path and listing mode and, when `[sandbox.served_bytes]` is declared, the caps of the tables of the bytes served, so a resume under another mode, bundle, data path or caps is refused.

The implementer must guarantee: `build` has no side effects beyond constructing objects; the hashes describe what the model sees under each condition; nothing is named by the core, so every name lives in the study.

**A plugin over several files.** `import_plugin_module` imports the plugin as a package whose path is the plugin's own directory, named `expcore_study_plugin_` plus a hash of the plugin's path. A module beside it is imported relatively (`from .scoring import Scorer`, or `from . import scoring`) and is named below that package, so two studies' `scoring.py` never meet in one process. Every module of the package is compiled from its file's bytes on each load, never from cached bytecode, so what runs is what the code identity hashed, and no bytecode is written into the study. `import scoring` finds nothing, since the study directory is not on the module search path; the refusal names the fix.

The core holds the code that runs to the code identity by where it comes from, not by its module name (`expcore.study.plugin_import`). While the plugin is imported and while `build` runs, it records every file whose code is executed, and it then finds every module in `sys.modules` whose file lies in the study directory; code the recorded environment covers does not count: the core's own packages, the running environment (a virtual environment inside the study directory, for one) and any installed distribution's code, editable or not, found as the environment record finds it (its `RECORD`, or for an editable install where it imports from), as does code below a directory the identity skips. It refuses the load, naming `study.toml` and the file relative to the study, when such a file is one `[study] code` does not cover, and when it is not a module of the plugin's package: a file imported through the module search path, or loaded by path (`spec_from_file_location`), would be cached under a name another study could be served, or run from stale bytecode. Before every import, and after a refused one, it drops every module of the study's own code, whatever its name, and every module of the package, so no study is served another's; it never drops a module of the core or of an installed distribution. The replay check's import is held to the same rules.

The implementer must guarantee that every file the plugin imports is in `[study] code` (and so in `package.toml`, whose build requires every file the identity covers), and that it imports each at the plugin's top level or in `build`: a module first imported while a run is under way escapes the check. The check sees code only: a data file the plugin reads by path (a prompt, a table) is not checked, and the identity covers it only if `[study] code` lists it, as the example lists `prompt.txt`. A backend factory in a module beside the plugin enters the fingerprint as `scoring.Factory`, without the hashed name.

The command line is `python -m expcore run|check|analyze|report|package|package-check|example` ([`cli.py`](../src/expcore/cli.py)). Output goes under `<study>/out/`; `package` writes a replication package where `--out` says, and `example` writes the example study into a new directory. How to use them is in [`study-guide.md`](study-guide.md).

What the analysis may use. `check` replays every record and also copies the campaign store into the batch (`campaign-snapshot.sqlite`, made with SQLite's online backup over a read-only connection, which copies pages and so needs no particular SQLite version; then each attempt's directory is written relative to the batch and the copy is rebuilt with a plain `VACUUM`, so it holds no path of the machine the campaign ran on; and integrity-checked) and hashes the copy with the records. It lists the records it replayed (`replayed`) and refuses a run record named by a hashed-only pattern, so a check cannot replay nothing and still vouch for the records beside it. It refuses a store with attempts still running unless `--partial`, which the check records. `analyze` and `report` then use only those bytes: the snapshot, never the live store, and the replayed records, each read once and compared with the check's hash. They cross-validate the two: every record maps to an attempt of the snapshot, and every attempt scored (state `done`, a scored treatment failure included) has a replayed record that agrees with it on run key, attempt, outcome and treatment flag. Any mismatch is refused, naming it. With `--partial`, attempts still running at the check are tolerated, a final attempt no replayed record covers is left out. Every saved output (`export.json`, `analysis.json`, `report.md`) says whether the analysis is partial and lists the attempts left out (run key, attempt, reason); `analysed_attempts` holds only what was scored, and every count in the report is of that population, with the left-out runs' attempts in a column of their own.

Side files are verified with the records. A file a run writes beside its record, which the record names by its SHA-256, is declared in `study.toml` ([below](#side-files-of-a-record)). `check` hashes every file of a declared name in the batch it parses into its inputs, never replays one, and pairs each with the one record in its directory; the check records the declaration (`side_files`) and each side file's hash in `inputs`. `analyze`, `report`, `regenerate` and a package's `make check` refuse a check made under another declaration, refuse a side file changed, removed or added since the check, and read each side file once to pair it with its record again, whatever the check file claims; the bytes so read are what a [scorer](#scorer) that takes `side_files` is handed. A study that declares none is checked exactly as before, and its check file holds no `side_files`.

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
    async def probe(self) -> ProbeReply | None
    async def aclose(self) -> None
```

Core guarantees:
- The scheduler builds the backend of a run from the endpoint the campaign admitted (`probe` succeeded once, B07), through a `BackendFactory` (below); a run never resolves a URL of its own (C14). `probe` returns what its one-token reply reported (`ProbeReply`: the `max_tokens` it carried, the input and output counts, the stop reason); a reply without both counts, or a probe that returns `None`, is not admitted, because the token budget is held from the counts each reply reports (B07). A backend that wraps another passes the inner probe's reply on. With a budget, each send of a probe is paid from it, reserved before it goes out (C26); a model whose probe the budget cannot cover is not admitted. A model that is not frozen (B08), whose settings cannot be sent together (B16) or, with a budget, has no price (C12) or no `context_tokens` (C25) is refused before a run starts.
- With a budget, each run is reserved the most its caps let it cost (C25). For a model held to its reply maximum that counts at most the rounds times the reply maximum as output; for any other, the whole token cap and, for the request a run can make past it, the whole context window at the dearest rate. A model is held to it only when the core's own factory builds an `openai_compat` or `anthropic` adapter for it, which sends the maximum as `max_tokens`, and admission's probe showed the endpoint cuts a long reply at a small `max_tokens`; never a model a plugin's factory builds, whatever its `backend`, and no longer once one of its replies reports more than the maximum (C27). A backend of the study's own whose request outgrows the window is outside that bound.
- With a budget, a send the token cap cannot account for reserves its own bound before it goes out (C26): a resend after a connection error or a 5xx from the core's retrier, and every request after a reply that left a count out. The guard reaches the core's adapters through a context variable the attempt runner installs (`expcore.models.spend`), so a core adapter a plugin's backend wraps is covered; a backend of the study's own that resends on its own is not. A top-up that does not fit ends the attempt as infrastructure of kind `budget_hold`.
- The backend is closed (`aclose`) after the executor returns, whatever happens.
- Retries of transient errors, the served-model check and usage totals are the adapters' (B01 to B16); the stub adapter takes a script (`StubAdapter.use(script)`, items: text, a list of `(tool, arguments)`, `{"text", "stop"}`, a ready-made `ModelTurn`, or an exception it raises).
- A model whose entry declares `accepted_fingerprints` (B18) is admitted only when its probe was served by the model (its `api_id` or an alias) under a fingerprint of the set. Admission reads what the probe's reply said it ran from the admission backend's `info()`, as `served_model_ids` and `system_fingerprints`, which the core's OpenAI-compatible adapter fills from the probe; a backend whose `info()` has neither confirms nothing at admission and is not refused for it. Each run's replies are then checked against the set instead of a fingerprint baseline, the served model id against its baseline as before (C16). A declared fingerprint is confirmed by the probe or by the first run whose reply shows it under the model's id, which is a record and not a gate; each attempt records the turns each fingerprint served, and the export's `deployment_shares` and the report's "Deployments" section give the shares. A model that declares no set is admitted, checked and fingerprinted as before.

Implementer must guarantee:
- `step` returns one reply per call. A token count the reply did not report is `None`, never 0 (R10).
- A failure of the endpoint that outlasts the retries is raised as `InfraError("endpoint", ...)`; a refused key as `InfraError("key_rejected", ...)`. A reply cut at the output limit sets `stop_reason` to a limit stop.
- `info()` and `transcript()` never contain a credential; the base URL has none (R12).
- A backend of a study's own that serves a model declaring `accepted_fingerprints` sets `ModelTurn.system_fingerprint` on each reply that reports one, and after `probe` gives the probe's served model id and fingerprint in `info()` (`served_model_ids`, `system_fingerprints`, each a list); without them no declared fingerprint is confirmed at admission.
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

### Usage fields beyond the counts

A provider may report more in its usage object than input and output tokens: an endpoint's own credit count, cache reads and writes, reasoning tokens. `ModelTurn.usage_extra` keeps the numeric ones, per turn, under the provider's own names, and the record holds them in the turn's usage as `transcript[i].usage.extra` (a mapping of name to number) with `extra_cut` beside it (an integer). They are recorded only: the token budget, the totals and the prices read the counts above and nothing here, and no price is looked up for a field until a model's price table names one.

- **Taken as reported.** The reply's usage object is read as a mapping, an SDK model or a plain object. A field is kept when its value is an integer of 64 bits or a finite float and its name is a plain ASCII identifier (at most 80 characters). An object one level down is read the same way under `parent.child`: `completion_tokens_details.reasoning_tokens`, `prompt_tokens_details.cached_tokens` and `cache_creation.ephemeral_5m_input_tokens` are fields. Text, flags, null, lists, non-finite numbers and other names are left out. A zero the provider reported is kept as a zero.
- **Absent, never 0.** A field the reply leaves out is not in the mapping, so a sum of a named field over a run's turns is "not reported" for a turn that lacks it, never a short total. An adapter that read a usage object with no extra field gives `{}` and `extra_cut` 0. A backend that reads none (the stub, or one of a study's own) leaves both at `None` ("not recorded"), and the record then has no `extra` and no `extra_cut` for the turn.
- **Not repeated.** The counts a turn already holds are not among them: `prompt_tokens`, `completion_tokens` and `total_tokens` (their sum) of an OpenAI-compatible reply, and `input_tokens`, `output_tokens` and the two cache counts of an Anthropic reply, which stay in their own fields.
- **Bounded.** At most `expcore.models.usage.USAGE_EXTRA_CAP` (32) fields are kept per turn: the first by name, so the same reply always keeps the same ones, and `extra_cut` is the number of numeric fields the cap left out. A field left out for its type or name is not counted as cut.
- **Replay.** A replay serves the recorded mapping and count exactly, and the replayed record has the same bytes.
- **Record version.** This is record schema version 3 (`expcore-record/3`), which adds `extra` and `extra_cut` to the usage of each turn, both optional ([the schema](#the-run-records-schema)). A record of version 1 or 2 lacks both and stays valid: it loads, writes back to its own bytes and replays, and a replay of it never writes them. A record of version 3 written before this change lacks them too, and is valid. Every new record is version 3 and keeps version 2's first-turn rule. Version 3 is also the first under the reply-count rule (C26, `expcore.records.schema.reply_count_rule`): when the backend counted a billed reply that became no turn, the record's `totals.usage_incomplete` is true, while a record of version 1 or 2 keeps the totals it was written with. The merge-point fixtures pin both: `tests/data/record-v3/01-usage-extra.json` the usage fields, and `02-billed-unread-reply.json` a billed reply the adapter could not read.

A backend of a study's own fills `usage_extra` and `usage_extra_cut` with `expcore.models.usage.extra_usage(usage, skip=...)`, which applies these rules, or leaves them at `None`. A value outside the rules (a flag, text, more than the cap, a name that is not a plain identifier) makes the record invalid: writing it raises `RecordError`, as for a retry entry.

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

`expcore.records.rescore.Scorer`, and `SideFileScorer` for a scorer that reads the record's [side files](#side-files-of-a-record).

```python
class Scorer(Protocol):
    name: str
    version: str
    def score(self, record: RunRecord) -> Mapping[str, Any]

class SideFileScorer(Protocol):
    name: str
    version: str
    def score(self, record: RunRecord, side_files: Mapping[str, bytes]) -> Mapping[str, Any]

def rescore(record: RunRecord, scorers: Iterable[Any], *, side_files: Mapping[str, bytes] | None = None) -> RunRecord
def rescore_files(source, scorers, *, out_dir=None, side_files: Sequence[SideFile] | None = None) -> list[Path]
```

A scorer, or a plain function used as one, that declares a parameter named `side_files` (positional or keyword-only) is passed it by keyword; one that does not is called with the record alone, exactly as before. The parameter is chosen over a `StudyContext` accessor because a scorer is built once in `build`, before any record exists, while the parameter is scoped to the one call that scores one record.

Core guarantees:
- A scorer receives a deep copy of the record. The result is kept in `record.scores[name] = {"version", "result"}`, apart from instrument output, and a scorer without `version` is versioned by the hash of its source file.
- A scorer that takes `side_files` gets the record's side files as a read-only mapping of its own from declared pattern to bytes: the bytes the loader read once, within the cap, whose SHA-256 it compared with the hash the record names, never a path or a handle to reopen. They are bytes rather than a handle because the loader must hold them to hash them, and bytes cannot be read twice differently or moved between one scorer and the next. `analyze`, `report`, `regenerate` and a package's `make check` hand over the bytes `study.verified.load_verified` read against the replay check, all of them read and paired before the plugin is loaded, so a side file that no longer matches is refused before any scorer runs. `rescore_files` with the study's declaration reads them beside the record of an attempt directory and pairs them as the check does. The same batch therefore gives the same scores, byte for byte, through each. `check` scores nothing. A record with no side file, or a study that declares none, gives an empty mapping; a caller that hands over nothing (`rescore` without `side_files`, `rescore_files` without the declaration) refuses such a scorer, naming it, rather than give it an empty mapping for a record that has a side file.
- A side file holds at most `expcore.replay.side_files.MAX_SIDE_FILE_BYTES`, 64 MiB. Every side file of a batch is held in memory while it is analysed, as every record is, so a larger one is refused with an error naming the file and the cap: `check` fails it at stage `side_file`, and the loader stops reading one byte past the cap. Neither the check nor the comparison of the batch with it reads a side file whole: a side file is never looked into for a record, and it is hashed in chunks.
- An attempt that is set aside (infrastructure not declared as treatment, or a harness defect) is never scored. An attempt that ends in a declared treatment kind, after the model completed at least one turn, is, so the scorer must handle a partial record; the same kind before the first turn is set aside.
- The analysis reads `result["outcomes"]` and nothing else (`expcore.study.pipeline`). The pipeline scores in memory and never writes into a checked batch. `analyze` derives every score anew from the verified records and reads no stored score, so it is the re-scoring command (R13): after a scorer changes, `check` again (the scorer is code, so the code identity changed), then `analyze` and `report`. `scores.json` and the report name each scorer's version.

Implementer must guarantee:
- The score is a pure function of the record, and of `side_files` for a scorer that takes them: no model, no platform, no clock, and no file opened by path (R13). The platform axis comes from run-end snapshots in the record.
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

The platform has two instruments. The first is `expcore_onedata.probes.PlatformProbes` (namespace `platform_probes`): the block map of each tracked file on each tracked provider before and after the run, the records of the transfers the provisioner scheduled, the record of every transfer of the space that started in the run's window whoever started it (`space_transfers`), the bytes moved between provider pairs, attributed to a source only where exactly one other provider held those bytes at the start, and per receiving provider the transfer bytes into it and a lower bound on the bytes fetched on demand (`on_demand`). A provider's `gained_bytes` is placement gained: the bytes of the blocks a provider's block map shows at run end that it did not show at run start; what it showed at the start and no longer does is `lost_bytes`, kept apart, so it is not a net change, and it is not bytes on the wire. See its module docstring and `expcore_onedata.transferlog` for the snapshot's fields.

The second is `expcore_onedata.sandbox.served.ServedBytes(sandbox)` (namespace `served_bytes`): the bytes the run's agent read through its data path (O22), for a measure such as bytes moved but never consumed. Build it once `SandboxPlatform.launch` has yielded the run's sandbox, and call `sandbox.collect(...)` before `finish_instruments`: the collection stops the data gate and counts its log once, and `finish` records that count, so replay serves the recorded value and nothing measures it again. The snapshot is `{"version": 1, "data_path", "listing", "run_start_marked", "served_bytes", "content_bytes", "listing_bytes", "other_bytes", "gate_answer_bytes", "files", "files_total", "files_cut", "listings", "listings_total", "listings_cut", "caps", "before_run_start", "unknown"}`:
- what it counts: the body bytes of the platform's answers that the gate's socket accepted for the agent, after any cap or cut; `content_bytes` are the 200 and 206 answers to reads of a file's content (a cut transfer's part included), `listing_bytes` the 200 answers to listings as they were served (each page in the paged mode, the merged answer in the complete one), `other_bytes` every other answer of the platform's (the token's scope, a looked-up file id, a file's attributes, the platform's errors), and `served_bytes` the three together. `gate_answer_bytes`, the gate's own refusals and errors, is kept out of `served_bytes`;
- per file and per directory, so a study can check an agent's claimed reads file by file, which a run's total cannot (a read log that moves reads between files keeps the total):
  - `files` holds a row for every file whose content the platform served, keyed by the file id the request's path names (`/api/v3/oneprovider/data/<file id>/content`, as sent, never decoded): `path`, that path as the gate logged it, which names the file by its id and never by a name; `requests`, its content answers; `ranges`, the byte ranges they served, merged where they overlap or touch, lowest first, each `[start, stop]` with `stop` excluded; `range_bytes`, the bytes in those ranges, each counted once; `ranges_cut`; and `body_bytes`, every body byte of its answers, so a file read twice counts twice here and once in `range_bytes`. The rows' `body_bytes` add up to `content_bytes` when the table is not cut;
  - a range begins where the platform's answer says, never where the agent's `Range` header asked: at the file's start for a 200, at the first byte of the `Content-Range` the gate logs on a 206 (`content_range`), and it runs for the bytes the gate's socket accepted. A 206 whose `Content-Range` is not one range holding those bytes (a multipart answer, say), or a line without the platform's status, leaves that file's `ranges`, `range_bytes` and `ranges_cut` `None`, never a part of them; its `requests` and `body_bytes` stay known;
  - a file whose attributes alone were read, or whose content read was refused or answered with the platform's error, served no content and has no row;
  - `listings` holds a row for every directory whose listing the platform served, keyed by the directory's id as the request's path names it (`/api/v3/oneprovider/data/<directory id>/children`): `path`, `requests` and `body_bytes`, which add up to `listing_bytes` when the table is not cut. A listing answered with the platform's error counts as another answer, not under its directory;
  - bounded by caps that platform.toml declares, in an optional `[sandbox.served_bytes]` table: `max_files` files (1000 by default) and `max_directories` directories (1000), the first the log shows, and `max_ranges_per_file` ranges a file (64), the lowest. Each cap must be an integer of at least 1, and anything else, or an unknown key, is refused at load with an error naming the file and the key. The snapshot records the caps in effect as `caps` (`{"max_files", "max_directories", "max_ranges_per_file"}`), in every snapshot, a mount's and an unknown one's included. A declared table is in the campaign fingerprint (`PlatformConfig.fingerprint()`, under `served_bytes`), since the caps decide what a run's evidence can show: a resume under other caps is refused, naming them (`platform.served_bytes.max_files: recorded 2000, now 4000`). Without the table the fingerprint keeps the shape it had before the caps could be declared, so a campaign that ran under the defaults still resumes; declaring the table, even with the default values, is then a change the resume refuses. `files_total` and `listings_total` count every file and directory served, listed or not; `files_cut`, `listings_cut` and a file's `ranges_cut` say a cap cut the list. `range_bytes` counts every range whatever the cap, and the run's own counts are never cut;
  - a cut is never read as "not served". With `files_cut` false, a file without a row was not served; with it true, a file without a row is unknown, since a cap may have left it out, and never not served or served 0 bytes. In a row whose `ranges_cut` is true, every byte below the end of its last listed range is known (inside a range it was served, in a gap it was not), and a byte past that end is unknown. Directories follow the rule of files with `listings_cut`. `served.lookup_file(snapshot, file_id)`, `lookup_directory(snapshot, dir_id)` and `lookup_bytes(snapshot, file_id, start, stop)` (a claimed read of `[start, stop)`) apply exactly this rule to a snapshot (the instrument's, or the sandbox's `served_bytes()`): each returns a `Lookup` whose `state` is `served`, `not_served` or `unknown`, with `reasons` (`files_cut`, `listings_cut`, `ranges_cut`, `ranges_unknown`, or the run's own `unknown` reasons) and the file's or directory's `row` when it has one. A snapshot that is not one of this version raises `ValueError`;
- what it does not count: headers and TLS framing, what the gate read upstream (so the pages behind a merged listing are not counted, only the answer the agent got), the plain TCP forwards of the knowledge plane, and anything the agent read from its own disk. A count of what was served can exceed what the agent's code consumed by what sat unread in its socket when it stopped reading; the gate log's cut and close lines show how much;
- per data path: a REST run is counted from the gate's log; a mount's reads go through oneclient, which no gate sees, so every count is `None` with `unknown: ["mount_not_through_gate"]`; a run with no data path read nothing through one, so every count is 0 and every table empty;
- the run's start (O22): an executor that checks the sandbox before the agent's loop, as the agent's user (`exec(code, timeout_s, record=False)`, a harness step), calls `sandbox.mark_run_start()` once its last such step has returned and before the loop starts. A REST data path's gate, signalled with SIGUSR1, writes a `run_start` line in its log that names each request it had begun and not yet logged, and the sandbox reads it back before it returns; an agent's cell entered meanwhile waits until it has, so no request of the agent's can precede it. The counts, the tables and so the lookups then hold only the answers after the mark; the answers before it, and those it names, are counted apart in `before_run_start` (`requests`, `served_bytes`, `content_bytes`, `listing_bytes`, `other_bytes`, `gate_answer_bytes`), never in a table, so nothing is hidden. `run_start_marked` says whether a mark was set: without one every answer is the run's, as before, and `before_run_start` is `None`; with no data path it is all zeros. A snapshot recorded before these two keys existed lacks them, which reads as not recorded, never as no mark. `mark_run_start` returns what it found, which `metadata()["run_start"]` keeps: `{"gate": false}` without a gate, otherwise `gate`, `signalled` and `confirmed`. It is refused a second time, once one of the agent's cells has begun (its `exec` entered: a mark during the agent's first cell would set that cell's reads apart), while a harness step runs, and after the collection; a mark that was not confirmed leaves the count unknown, and the executor decides whether the run goes on. The gate's own summary (`metadata()["gate"]`) and its refusals still cover its whole log;
- unknown, never zero: every count and every table is `None`, with the reasons in `unknown`, before the collection (`gate_not_collected`) and whenever a line may be missing or unreadable (`gate_not_stopped`, `gate_teardown_unconfirmed`, `gate_log_unreadable`, `gate_log_malformed`, `gate_log_write_failed`, `gate_output_unreadable`, `gate_teardown_incomplete`, `request_in_flight_at_stop`), so a table is never a part of the run's reads taken for the whole. So is a mark of the run's start the sandbox set and the log does not confirm (`run_start_unconfirmed`): one not read back before the loop started, or not in the log exactly once and before its `teardown_complete` line, is never taken as "everything after"; a `run_start` line in the log of a run that set none, or one that names no requests in the gate's form, is a malformed line. An answer of a file's content or a listing whose path names no file or directory is a malformed line. A log counts as whole only with a positive mark, the `teardown_complete` line the gate writes last, once every connection's close line is written: an exit code proves nothing, since a gate killed by its memory limit exits too. The gate's own output (`docker logs`) must also have been read, since it is where a failed write of the log shows; a read that fails leaves the count unknown. The sandbox's `metadata()["served_bytes"]` holds the same count for operators.

The count is taken once, when the collection stops the gate, and the record keeps it: replay and re-scoring read the recorded snapshot, tables included, and never measure them again.

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

Implementer must guarantee: the executor writes the side file into `request.run_dir` with the record, and the record names it. An attempt killed after writing its side file and before writing its record leaves an orphan, which `check` refuses, naming it, until it is removed. A side file holds at most 64 MiB (`MAX_SIDE_FILE_BYTES`). A measure computed from a side file's content is a [scorer](#scorer) that takes `side_files`, which is handed the bytes the analysis read once and verified; the scorer never opens the file.

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
    def exec(self, code: str, timeout_s: float, *, record: bool = True) -> ExecResult  # record=False: a harness step
    def collect(self, dest_dir: str | Path) -> dict[str, Any]
    def stop(self) -> None
    def metadata(self) -> dict[str, Any]

class DockerSandbox:                # sandbox.py
    @classmethod
    def start(cls, host: HostOps, settings: SandboxSettings, spec: SandboxSpec, **options) -> DockerSandbox
    def collect(self, dest_dir, outputs: Sequence[str] = ()) -> dict      # outputs: files of /work to copy
    def served_bytes(self) -> dict                                        # what ServedBytes records (O22)
    def mark_run_start(self) -> dict      # where the agent's run begins, before its loop (O22)

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
    def launch(self, ctx, *, run_key, level, ceiling_s, lease=None, condition=None, inputs=(), inputs_record=None)
    def tool_server(self, ctx, sandbox, *, ceiling_s, extra_env=None)  # yields the run's McpToolProvider

class ReadOnlyInput:                # api.py: files a study supplies, read-only in the agent (O20)
    source: Path                    # a directory on the host: regular files and directories only
    target: str                     # the absolute path the agent sees it at
    sha256: str | None = None       # its content's hash when it was declared (inputs.declare)

class CodeExecutionProvider:        # codetool.py: a ToolProvider with one tool that runs Python
    def __init__(self, sandbox: Sandbox, *, name="python", description=...)
```

**Data paths.** The study's `data_path` factor names its levels in its own words; `[sandbox.data_paths]` maps each to one of three kinds, `mount`, `rest` or `none`, written as the kind alone (`level = "rest"`) or as a table that also names a REST level's option (`level = { kind = "rest", listing = "complete" }`). A study selects a data path and its option per condition, through the level the condition gives its `data_path` factor. A `mount` run has a read-only FUSE mount made by a oneclient sidecar that holds the data-path token; the agent holds no token and no FUSE. A `rest` run reaches the zone and its pinned local provider only through the data gate, which holds the token and adds it to the requests it forwards (a token the agent sends is dropped); the agent holds no token in its environment, its files or its process arguments. A `none` run has neither. The data path is read-only in every condition at four points: the token (4a: read-only, scoped to the space, one interface), the gate (only the five read calls pass), the mount, and the agent's root filesystem. `start` proves the last two by trying to write from inside the agent and requiring the refusal.

**A REST data path's listing (O21).** `listing = "paged"`, the default, passes the server's page of a listing as it comes, as the official client's `ls` reads it: a directory of more than 1000 entries shows its first 1000. `listing = "complete"` has the gate answer every listing with all of the directory's entries, in the server's order, as one page that is the last (`isLast` true, `nextPageToken` null). The gate follows the pages upstream itself, with its own page size, and the agent's other options go with every page. It goes on while a page says it is not the last, however short. For a directory that does not change while it is read (the known limit below), it never passes a part of a listing as if it were the whole:
- a page after the first that is not a 200, or any page that neither ends nor continues the listing (no boolean `isLast`, `isLast` false without a token, a token given before, not an object with a list of children, not JSON with each key once), or that gives an entry the listing gave before (by its file id, else its name), is the platform's 502 (`error_origin: upstream`); the first page's own error, a 404 for a directory that does not exist say, is passed on as the paged mode passes it;
- a listing whose merged answer, with every continuation token it gave, would pass 64 MiB, or that needs more than 10,000 pages (`gatepolicy.COMPLETE_LISTING_BYTES`, `COMPLETE_LISTING_PAGES`), is refused for the gate's integrity (`gate_refusal: listing_too_large`, with `listing_bound` on the line), so the run ends as `data_gate_integrity` and is set aside. Of each token and entry the gate keeps only a 16-byte digest, to find a repeat;
- a listing that names where it starts (`token`, `index` or `offset`, in the query or the body) is refused with a 400 the agent reads, a policy refusal; its `limit` and `tuneForLargeContinuousListing` are dropped, the gate's to set;
- every page passes the response boundary as any answer that is no file's content does, and the merged answer is checked once more before any byte of it goes.

A known limit: Onedata gives a listing no snapshot, so a complete listing is complete only for a directory that does not change while the gate reads it, as a study's data should not. An entry another client adds meanwhile shifts the ones after it, one of them comes twice, and the listing is refused; an entry removed meanwhile can shift another out of the pages unseen, and the answer then lacks it with nothing to show. Entries that carry neither a file id nor a name (a listing that asks for neither attribute) cannot be told apart, so a repeat among them is not found.

The option is refused on a mount or no data path, where no gate lists, and a mode the gate does not know refuses the load, the start and the gate itself. `metadata()["listing"]` records it (None without a gate), and `PlatformConfig.fingerprint()` holds every level's kind and listing mode (`data_paths`), so a campaign is refused a resume after a level changed either: two modes are never one experiment.

**One pool of leased users (C19).** A run that needs a data-path token or a tool server leases one slot of `[slots.<name>]` (`users.py`), and one that needs neither takes none (`lease_group` is None). The slot gives the run both planes' users: `launch` mints the run's tokens through `users.SlotTokens` at once, with one journalled expiry, the data path's on the slot's data-path user and, when `condition` names the run's tool-server condition, the tool server's on its tool-server user; `tool_server` then starts the server with that token, and the launcher refuses a token whose subject is not that slot user. A run with a tool server and no lease is refused; a run without a lease and with a data path (a live check) mints as the fixed `[principals.data_path]` user. The platform's `ResourceProbe` is `SlotProbe(journal, sandbox_gone=SandboxProbe(...).resource_gone)`: a key is freed only when the sandbox is proven gone and the slot's tokens have ended, by their journalled expiry plus a margin. A `[sandbox.users]` table, the second pool of earlier versions, is refused at load with a message that points to `[slots]`.

Core guarantees: `ctx.infrastructure_wait` is given a kind of `WAIT_KINDS`; the lease the platform declared for the run is held for the whole attempt and freed through the platform's probe, `SlotProbe.resource_gone`, whose sandbox half is `SandboxProbe.resource_gone`.

Implementer (the platform layer) guarantees:
- `launch` opens `ctx.infrastructure_wait("sandbox_lifecycle")` around the start (the token mint, the image checks, the containers) and around the stop, and nowhere else. Executing the agent's code and its reads of data are never waits.
- Every image is started by the id it is pinned to in `platform.toml`; a tag that now resolves elsewhere raises `StartRefused` before anything exists (O13). Each sandbox gets its own small network from the configured pool, whose ranges are strict networks checked at load (O14).
- The data gate's upstream connection and oneclient check the federation's certificate as the platform's one `[tls]` says, as the REST client and the tool server do (`expcore_onedata.tls`): against their default CAs, against the run's own read-only copy of the `ca_file` bundle's verified bytes (written once, after their hash is checked against the one loaded, and mounted read-only; a bundle that changed since the load refuses the start), or not at all with `insecure_tls`. The agent trusts the run's own CA alone. `metadata()["tls"]` records the mode, with the bundle's SHA-256 under `ca_file`, never its path or contents (O05, O12).
- A read-only input (`launch(..., inputs=[ReadOnlyInput(...)])`, which the executor passes from the run's condition or its own; `inputs.py`, O20) is copied into the run's directory, hashed while it is copied, and mounted read-only (`readonly`) in the agent's container from that copy, in every data path. `start` proves from inside the agent that writing, renaming, chmod-ing and deleting a file of it and creating a file or a directory beside it each fail with EROFS, which no permission the agent could change gives, that the agent can read it and run its executable files, and that `/work` stays writable; it then hashes the copy and the source again, and raises `SandboxError` for an input that changed between its hash and its mount. `metadata()["read_only_inputs"]` records each input, in order, by its path in the agent, the SHA-256 of its content (each entry's relative path and kind, and a file's executable bit, size and SHA-256, in path order), its files and its bytes, beside the other run inputs the metadata names by hash (`images`, `tls`, `platform_config_sha256`), and never by the host's path; `InputsRecord`, an instrument `launch` fills (`inputs_record`, required with `inputs`), puts the same in the run's record itself, `RunRecord.instruments["read_only_inputs"]`. The plugin declares its inputs once, `declared = inputs.declare(config.sandbox, {name: [ReadOnlyInput(...)]})` by name (a condition, or one name for every run), which checks each against every path the sandbox reserves (`inputs.taken_paths`) and gives it its content's hash; it puts them in the campaign fingerprint, `StudyPlugin(platform={**config.fingerprint(), **inputs.fingerprint(declared)})`, so a resume whose inputs changed is refused (C06), builds the platform with them, `SandboxPlatform(..., read_only_inputs=declared)`, and passes them to `launch` as declared. `launch` refuses an input it was not declared with, and every start hashes each input again and raises `StartRefused`, before anything exists, for one whose content is no longer the declared one. The run's copy is given to root and made read-only on the host too (0444 files, 0555 directories and executable files), and `collect` hashes it again: a copy that lost its hash raises `InfraError("output_collection")`. A path that is not plain and absolute, or that is, holds or lies under `/work`, `/tmp`, `/dev`, `/proc`, `/sys`, a file docker writes in `/etc`, the run's CA, a trust path, the data mount or another input, and a source that holds a link, a special file or no file, raise `StartRefused` before anything exists.
- `collect` copies the named outputs of `/work` and the gate log out as root without following or keeping a link (`copyout.py`, O19). The agent's container is frozen through its cgroup (`docker pause`, confirmed by `frozen 1` in the cgroup's `cgroup.events`, never by a list of processes) while its outputs are listed and copied, and thawed after; a freeze that cannot be confirmed raises `InfraError("output_collection")` before anything is listed. The listing and the copy are made by a root helper (`copyhelper.py`, run as `sudo PYTHON -I -c`, its request on stdin) that opens every name from its parent directory's descriptor with `O_NOFOLLOW` and checks every directory on the way and every object copied against the device, inode, kind and size the listing showed, so a name swapped for a link or another object after the listing stops the copy rather than being read through. A root listing that follows nothing must show each output, and every directory on the way to it, holding only regular files and directories: no symbolic link, no hard link to a file outside the output, no FIFO, socket or device, no other file system (on the way to an output too: only a regular file there makes the output absent), no path longer than 2048 bytes, and no more bytes in all than `work_disk_bytes`. Otherwise the agent made the outputs so, which is its own doing: nothing of the outputs is copied, and `collect` returns normally with `refused_output` in its result and in the metadata (and so in `metadata.json`), naming each object by its path under `/work` and what it is, never a link's target, with their count in all and by what they are. The study's scorers read it; the attempt is never set aside for it. The copy is the driver's (`fchown`, modes 0644 and 0755), and is then verified by lstat to hold only the driver's directories and files of one link, the files and bytes the listing showed; one that does not, or one the helper stopped, is removed. A collection that fails for the harness's own reasons (a container that cannot be frozen, the helper or the disk failing, a copy that is not what was listed, a gate log that is missing or not a plain file, a read-only input's copy that lost its hash) raises `InfraError("output_collection")`, a platform kind registered with `treatable=False` in `expcore_onedata.sandbox.kinds`, and keeps the sandbox's disks. The driver reads the gate log, for its refusals and its summary, without following a link: one that is a link is unreadable. The reaper (`reap.py`) copies a dead run's outputs under the same rule and keeps its disks when it refuses.
- Teardown runs on every path that ends a sandbox, in the order readers, unmount, sidecar, agent, and verifies that each step took effect; a sandbox that is not verifiably gone makes `stop` raise `SandboxError` and `launch` raise `InfraError("sandbox")`, and the probe keeps the lease key quarantined (O15, C19).
- `SandboxProbe.resource_gone(key, note)` answers exactly `True` only when docker lists no container and no network labelled with the key and the mount table shows no mount in a directory of the key; any failed read is `False`.
- The gate passes only reads, answers a refusal as 4xx and never as an error, and marks a transfer it cut (`cut`, `unread_bytes`) so it is told from an error of the gate (`error_origin: gate`) or of the platform (`upstream`) (O16).
- Before a campaign admits its first run, the platform's standard health check (`health.PlatformHealth`, which composes `precampaign.PreCampaignHealth` itself, with no opt-out) measures the content type the gate streams on when a data path is REST: it reads `[sandbox] content_check_file` (required with a REST level) through the local provider, whole and by its first byte, and a type other than `application/octet-stream` with 200 and 206, or one it cannot measure, refuses the campaign: the verdict is kept for the campaign, so the health gate stays closed with the refusal as its detail and no run is admitted; with no REST data path the file is never read and the campaign is admitted. The day-0 check `content_type` is the same check, for the operator (`precampaign.measure_content_type`, which also says whether a type was measured): it passes only on a measured right type, fails on a measured wrong one, and is unknown whenever it measured nothing, with no REST data path included (O05, O08, O11).
- The reads of the content check, like every mutation and range read by file id, pass the client's mutation gate (`client.OnezoneClient`, O09): a request under `/spaces/{id}` goes only to a space whose live name starts with one of `[space] prefixes`, and a file addressed by id must lie in the configured space, whose live name must start with one too. A file's space is found by walking its `parent_id` links through the provider up to the configured space's root (the `fileId` of the provider's `GET /spaces/{id}`), at most `client.MAX_SPACE_DEPTH` (64) links; a walk that ends above every known root or past the bound resolves to no space and nothing is sent (`OutOfScope`). Roots and parent links are kept as the provider's that gave them, since a file's space does not change: at most `client.MAX_KEPT_LINKS` links (least recently used dropped first, all dropped by `close()`), and a walk that passes the bound over kept links walks once more, every link read anew, before it refuses. The space's name is read from Onezone again before every request. Every read of the gate is the client's own, with the admin credential and the platform's `[tls]`, never through the data gate, so none counts as the agent's. A gate read that fails refuses the request unsent (`ScopeUnread`, keeping the read's status, error id and transport flag); `OutOfScope`, `ScopeUnread` and `MintNotSent` are `NotSent`, which a caller tells from an answer of the platform.
- The day-0 checks report unknown, never passed, for what they did not measure: `read_only` passes only when every supporting provider refused a write it was sent (401, 403, or exactly 400 `posix` `eacces`, as Onedata 25.0 refuses it), and a write that was never sent (the gate refused it or could not check it, or the space root could not be read) makes it unknown with the reason; a write answered otherwise is unknown with its status, error id and errno.
- An entry is absent only when a provider answers it with a 404 or exactly 400 `posix` `enoent` (`client.answered_absent`), as 25.0 answers a deleted entry; teardown and the day-0 clean-up stop as soon as their consecutive reads show it, and the waits for a fresh space root and for a transfer not shown yet wait on it. Any other answer, another 400 or an unreadable body included, is never absence: teardown and the clean-up read on to their bound and report the entry left, and the waits raise at once. The absence is the provider's that was read; a directory's delete may still be under way on another provider or on storage (#114). Every raw body a mutation sends is marked `application/octet-stream`, which 25.0 requires of a content upload. A provider sees a file written through another only some seconds later on 25.0, so provisioning waits, bounded by `consistency.VISIBLE_TIMEOUT_S`, until it reports the file at its size before reading or scheduling a transfer through it (`Provisioner.await_visible`); past the bound `NotVisible` names the provider by its position, and any answer other than absence fails closed. An eviction counts only when the block map shows the provider empty; one that ended while bytes remain is scheduled again, bounded (`provision.EVICTION_ATTEMPTS`). Placement records per file what the block map holds beside what was asked for, since a provider may place more than a range read asks.
- Every answer that is no file's content is buffered whole up to 4 MiB, so each is bounded by the request: in the paged mode, a listing whose explicit `limit` (in the query or a JSON body) is above `gatepolicy.MAX_LIST_LIMIT`, 1000, the server's own default page, is refused before anything is forwarded, as a 400 the agent reads; a listing without a limit passes unchanged. In the complete mode the gate reads pages of 1000 itself, each buffered so, and merges them into one answer of at most 64 MiB, one listing at a time. The worst case of a page of 1000, the bound on the other non-content answers and the gate's memory for both are worked out in `gatepolicy.py` and `gate.py` (O05, O16, O21).
- Every line of the gate's log for an answer says what its body was (`body`: `content`, `listing`, `other` or the gate's own, `gate`) and how many of its bytes the gate's socket accepted (`body_bytes`), and a 206 of a file's content carries the platform's `Content-Range` (`content_range`); the count of the bytes served, per run, per file and per directory, is taken from those (O22).
- `exec(code, timeout_s, record=False)` is a step of the harness's own, a check before the agent's loop, and it runs before the agent's run only: it raises `SandboxError` once `mark_run_start` was called or one of the agent's cells has begun (its `exec` entered, not ended), since after the mark its requests would count as the agent's, and between the agent's cells it would take a cell's refusals and kills. It runs as the agent's user in the agent's container, as a cell does, and is recorded under `metadata()["harness_steps"]` (`step`, `status`, `exit_code`, `duration_s`, `timed_out`, `gate_refusals`, and `earlyoom_kills`, the host killer's kills of the run read when the step ended, `None` when its journal could not be read), never as a cell of the agent's (`metadata()["cells"]` counts the agent's alone). The gate's refusals while it ran and the host killer's kills are its own and never put on the agent's first cell, and that cell reads its memory counters afresh, so a kill under the step is never the cell's. Its only time limit is the backstop `timeout` in the container, a second after `timeout_s`: the harness's timer, which would stop every process of the agent's, is not started, and no kill cause is read (O22).
- A killed cell's cause comes from the cgroup counters, the kernel log, the host killer's journal and the container's state, never from the exit code. The agent's own limit is its outcome; a failure of the host or a component raises `InfraError("sandbox")`; a kill nothing attributes raises `InfraError("unattributed_kill")`, which the core retries within the attempt limit (O17). Errors the agent causes through the mount (ENOENT, EROFS, EISDIR) are not counted against the platform (O18).
- A message of any exception names roles, keys and positions, never a configured value (image, pool, path, login, level) or the tail of a tool's stderr (which goes to the log).

Not carried over from the earlier study: the gate's whole-file mode, the tracer and its checks, the capacity probes tied to two data layouts, and the replication branch of its fourth condition. Its merged-listing mode is the complete listing above, written anew against this gate with bounds and checks the earlier one lacked.

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

## The run record's schema

`expcore.records.schema`. A record names its schema version (`expcore-record/N`), and every section of it is a table of fields in `SECTIONS`. `SCHEMAS` lists the versions; a version names the fields it adds (`added`), and may mark some of those optional (`optional`). A record is validated against its own version: a field of a later version is refused in it, a required field of its version may not be left out, and an optional one may be absent and is checked when it is present.

- **A rule attaches to the version that wrote the record.** What a version's records mean is read from the record's own version, never from the newest code: validation (which fields exist, which are required) and each rule the format does not show, as the first-turn rule of version 2 (`SchemaVersion.first_turn_rule`) is read by `first_turn_rule(record.schema_version)`. A later rule never applies retroactively to an older record: a record of version 1 or 2 still validates, writes back to its own bytes and replays under the rule of its time, and a replay of it never writes a field its version lacks.
- **An absent field is absent.** Writing and replaying a record keep an absent optional field absent. It is never filled in as a null or a zero, so "not recorded" and "recorded as empty" stay different (R10).
- **Extending version 3 before v0.1.0.** Live runs write version-3 records while several changes still extend it, so until v0.1.0 is tagged every extension of `expcore-record/3` is strictly additive: a new field is optional and its absence means "not recorded"; no existing field changes its meaning or its validation; and a record written at any earlier merge point replays byte for byte under the newest code. To extend it, add the field to `SECTIONS`, list it in the `added` and the `optional` of the version-3 entry of `SCHEMAS`, write it into the record only when the run supplies it (the builder leaves a `None` out), and serve it back from the record on replay, absent when it was absent. Then add the next fixture to `tests/data/record-v3/`, named `NN-topic.json` and written by your code, and leave every earlier fixture as it is: `tests/records/test_v3_fixtures.py` holds the rule in two ways. Structural tests check the schema itself. A `failed_request`, an outcome's `defect` and the malformed events of the totals are objects a fixture may hold none of, and each is a section of `SECTIONS` (`failed_request`, `defect`, `malformed_event`), so its keys are versioned like any other; every field version 3 adds, in every section, is listed in that version's `optional`; and the fields version 3 had at its first merge point, frozen in `tests/data/record-v3-fields.json`, keep their types, their order and whether they are required, while every field not in that file is optional. So a required key added to any section is caught, whether or not the version lists it in `added`, and that file is never edited. The objects outside `SECTIONS` are held otherwise: every record holds a `code` identity and an `agent_config` with its `limits`, so the fixtures catch a required key added to them, and every key of a retry entry may be left out (`RETRY_FIELDS`), so a new one is additive as it stands. And the test validates every fixture and replays it byte for byte under the current code, which catches a change to a field or a check that a fixture holds. A non-additive extension is caught by the two together; the fixtures alone would miss a required field in a section they leave empty. A change that cannot be additive waits for the next version, `expcore-record/4`, which is appended to the table as version 2 and 3 were.
- **After v0.1.0.** A change to the format is a new version in `SCHEMAS`, as before. The fixtures of version 3 stay, and records of every earlier version keep loading and replaying.

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
| Infrastructure kinds | `endpoint`, `key_rejected`, `sandbox`, `knowledge`, `unattributed_kill`, `ceiling`, `tool_plane_integrity`, `budget_hold`, plus kinds a platform registers ([below](#infrastructure-kinds-of-a-platform-or-a-study)) and kinds a study declares in `[study.kinds]` | `expcore.errors.CORE_KINDS`, `infra_kinds()`, `untreatable_kinds()` |
| Wait kinds | `model_retry`, `lease_queue`, `sandbox_lifecycle`, `toolserver_lifecycle` | `expcore.plugin.WAIT_KINDS` |
| Conclusion words | `difference`, `trivial_difference`, `equivalent`, `not_detected`, `untestable` | `expcore.stats.analysis.CONCLUSIONS` |
| Tool call statuses | `ok`, `error`, `malformed`, `skipped`, `cut`, `failed` | `expcore.loop.records.TOOL_STATUSES` |
| Attempt states | `running`, `done`, `infra_fail`, `outage_discarded`, `interrupted`, `harness_error` | `expcore.campaign.attempts.STATES` |
| Run statuses | `pending`, `done`, `infra_exhausted`, `harness_error` | `expcore.campaign.attempts.RUN_STATUSES` |
| Lease outcomes of an attempt | `granted`, `none` (a refused lease starts no attempt, so it is in the lease wait log and the event log) | `expcore.campaign.attempts.LEASE_OUTCOMES` |
| Malformed-call policies | `report`, `end` | `expcore.loop.config.MALFORMED_POLICIES` |

The conclusion words encode significance: `difference` and `trivial_difference` are significant, the other three are not; a contrast outside the Holm family is judged by its raw p-value and labelled exploratory wherever it is shown, and under a preregistration with `exploratory_only = true`, which has no family, every contrast is. Failing to detect a difference is not evidence of none. `untestable` is the word of a contrast that used no pair, and of no other: nothing was tested, and in the Holm family it counts as p = 1.

### Infrastructure kinds of a platform or a study

```python
def register_infra_kind(kind: str, description: str, *, treatable: bool = True) -> None
```

A platform adds a kind with `expcore.errors.register_infra_kind`, once, at the top level of a module of its own, so the kind exists as soon as that module is imported. `kind` is lower-case letters, digits and underscores and not a core kind; `description` is not blank and is the reason given when the kind is refused as a treatment; `treatable=False` marks a kind that can never be a condition's treatment, such as a failed integrity check of the harness. Registering a kind again with the same answer is harmless, and with the other answer is refused.

A study's `study.toml` is validated when it is loaded, before its plugin is imported, so a kind registered by the plugin, or by a platform only the plugin imports, is unknown to the conditions. A study that declares a platform's kind as a treatment lists the module that registers it in `[study] platforms`; the loader imports each listed module before it validates anything, and refuses one that cannot be imported, naming the file, the key, the module and the type of the exception, never its text. For the Onedata layer, that module is `expcore_onedata.sandbox.kinds`. A study without a platform declares its own kinds in `[study.kinds]`, as `name = { description = "...", treatable = true }`. They are held in the vocabulary only while the study is loaded and while the pipeline works on it (`expcore.errors.study_kinds`), so they do not outlast it, and a failed load leaves none behind; a platform's kinds stay for the whole process. The scopes are counted: two studies that overlap in one process each keep their kinds and can see the other's for the overlap, which can only widen validation, and declaring one name differently while they overlap is refused, naming the kind. A study's kind that is a core kind or a platform's is refused; so is a platform registering a kind of the study in scope. Neither key is part of the factor schema (study.toml's hash is in the campaign fingerprint, as before), so a study that uses neither keeps its fingerprint and schema digest. [`study-guide.md`](study-guide.md#treatment-kinds) shows both keys.

A condition that names a kind outside the vocabulary is refused as unknown, with the treatable kinds as the choices; a known kind that is not treatable is refused as one that can never be a treatment, with the reason its registration gave.
