# Interfaces

What a study (or a platform) implements to plug into the core, with the signature as the code has it, what the core guarantees, what the implementer must guarantee, and the closed vocabularies. [`architecture.md`](architecture.md) says why the interfaces are shaped this way; this file says what they are in v0.1. The runnable example is [`examples/tiny_study/`](../examples/tiny_study/).

All of them are `typing.Protocol`s, so a test double needs no inheritance. Signatures below are copied from the code; where a docstring in the code says more, the code wins.

## What v0.1 has

| Architecture name | In v0.1 |
|---|---|
| `Study` | the study directory (`study.toml` and its siblings, which may declare the [side files](#side-files-of-a-record) of its records) plus one plugin module whose `build(context)` returns a `StudyPlugin` ([below](#the-study-plugin)) |
| `Platform`, `Sandbox` | not in the core. The platform layer (`expcore_onedata`, steps 4a and 4b) supplies a `HealthCheck`, a `ResourceProbe` (leased platform users), its own `ToolProvider`s and three `Instrument`s (the platform probes, the bytes the agent read through its data path, and a progressive ingest that makes part of a dataset arrive during a run) through the same `StudyPlugin`; its sandbox is [described below](#the-platform-layers-sandbox), and its fakes for a study's own tests are [its testing module](#the-platform-layers-testing-module). A platform of another kind is a distribution of its own that plugs in through the same `StudyPlugin` and the same protocols: [the study guide's section 10](study-guide.md#10-a-platform-of-your-own) lists what it needs, and `examples/sparql_platform/` is one |
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
    admission: RunAdmission | None = None  # (treatment_kinds, treatment_targets, cancel, exclusive) -> context manager
    platform: Mapping[str, Any] = field(default_factory=dict)  # the platform's settings that make one experiment
    backend_factory: BackendFactory | None = None  # None: the core's default_backend_factory
    environment_identity: Callable[[], Mapping[str, str]] | None = None  # a shard's identity, below
```

`admission` is the platform's hold on a resource every run shares (`expcore.campaign.attempt.RunAdmission`). The core enters it after the lease and the second look at the gates, before the attempt's backend, timer and executor exist, so its wait is outside the agent's clock, and leaves it after the post-run check and the store's record; a refusal (`InfraError`) leaves the run unstarted, with no attempt used. Every health check runs in its caller's context, so the post-run check sees what the run's admission holds. `expcore_onedata.admission.run_admission(config, schema)` is the one for a platform with `[faults]`; given the study's factor schema, it refuses at once a condition that declares `exclusive = true` on a platform without `[faults]`. `exclusive` is whether the run's condition declares `exclusive = true` in study.toml (`FactorSchema.exclusive`): a condition with no fault whose runs the platform holds exclusively, as it does an outage run's, so a control can run under the outage's isolation; a campaign with such a condition and no run admission is refused at its start, naming the file and the key. The context manager yields what it holds, `shared` or `exclusive`; the core stores it in the attempt's row as it is admitted (`none` without an admission) and the export carries it as `admission` in every analysed and excluded entry and as `admissions`, one per charged attempt, in every exhausted run.

The core guarantees: the code identity is computed once, from the study directory and the `[study] code` roots, and refused if it is unknown, stale or (without `--allow-dirty`) dirty; the plugin is rejected at load if a condition has no prompt or tool hash, two scorers share a name, or `platform` is not a mapping; `prompts`, `tools`, the scorers' names and versions, the hash of each configuration file and, when not empty, `platform` go into the campaign fingerprint, so a resume under any change is refused (C06). A study on the Onedata layer passes `platform=config.fingerprint()`: the platform file's TLS mode with the CA bundle's SHA-256; with a `[sandbox]`, each level's data path and listing mode and, when `[sandbox.served_bytes]` is declared, the caps of the tables of the bytes served; and, with `[faults]`, the fault injector's sanction and each target's label with the SHA-256 of its Compose project and service names. So a resume under another mode, bundle, data path or caps, or with `[faults]` added, dropped, unsanctioned or taking another container down, is refused. `[faults]` names its Compose `project` explicitly; Compose would otherwise take one from the environment, the Compose file or its directory.

The implementer must guarantee: `build` has no side effects beyond constructing objects; the hashes describe what the model sees under each condition; nothing is named by the core, so every name lives in the study.

**A plugin over several files.** `import_plugin_module` imports the plugin as a package whose path is the plugin's own directory, named `expcore_study_plugin_` plus a hash of the plugin's path. A module beside it is imported relatively (`from .scoring import Scorer`, or `from . import scoring`) and is named below that package, so two studies' `scoring.py` never meet in one process. Every module of the package is compiled from its file's bytes on each load, never from cached bytecode, so what runs is what the code identity hashed, and no bytecode is written into the study. `import scoring` finds nothing, since the study directory is not on the module search path; the refusal names the fix.

The core holds the code that runs to the code identity by where it comes from, not by its module name (`expcore.study.plugin_import`). While the plugin is imported and while `build` runs, it records every file whose code is executed, and it then finds every module in `sys.modules` whose file lies in the study directory; code the recorded environment covers does not count: the core's own packages, the running environment (a virtual environment inside the study directory, for one) and any installed distribution's code, editable or not, found as the environment record finds it (its `RECORD`, or for an editable install where it imports from), as does code below a directory the identity skips. It refuses the load, naming `study.toml` and the file relative to the study, when such a file is one `[study] code` does not cover, and when it is not a module of the plugin's package: a file imported through the module search path, or loaded by path (`spec_from_file_location`), would be cached under a name another study could be served, or run from stale bytecode. Before every import, and after a refused one, it drops every module of the study's own code, whatever its name, and every module of the package, so no study is served another's; it never drops a module of the core or of an installed distribution. The replay check's import is held to the same rules.

The implementer must guarantee that every file the plugin imports is in `[study] code` (and so in `package.toml`, whose build requires every file the identity covers), and that it imports each at the plugin's top level or in `build`: a module first imported while a run is under way escapes the check. The check sees code only: a data file the plugin reads by path (a prompt, a table) is not checked, and the identity covers it only if `[study] code` lists it, as the example lists `prompt.txt`. A backend factory in a module beside the plugin enters the fingerprint as `scoring.Factory`, without the hashed name.

The command line is `python -m expcore run|merge|check|analyze|report|package|package-check|example` ([`cli.py`](../src/expcore/cli.py)); `run` also takes `--shard i/N [--shard-label L]` ([below](#shards-of-a-campaign)). Output goes under `<study>/out/`; `merge` writes the batch of a campaign's shards where its first argument says (normally `<study>/out/`), `package` writes a replication package where `--out` says, and `example` writes the example study into a new directory. How to use them is in [`study-guide.md`](study-guide.md).

What the analysis may use. `check` replays every record and also copies the campaign store into the batch (`campaign-snapshot.sqlite`, made with SQLite's online backup over a read-only connection, which copies pages and so needs no particular SQLite version; then each attempt's directory is written relative to the batch and the copy is rebuilt with a plain `VACUUM`, so it holds no path of the machine the campaign ran on; and integrity-checked) and hashes the copy with the records. It lists the records it replayed (`replayed`) and refuses a run record named by a hashed-only pattern, so a check cannot replay nothing and still vouch for the records beside it. It refuses a store with attempts still running unless `--partial`, which the check records. `analyze` and `report` then use only those bytes: the snapshot, never the live store, and the replayed records, each read once and compared with the check's hash. They cross-validate the two: every record maps to an attempt of the snapshot, and every attempt scored (state `done`, a scored treatment failure included) has a replayed record that agrees with it on run key, attempt, outcome and treatment flag. Any mismatch is refused, naming it. With `--partial`, attempts still running at the check are tolerated, a final attempt no replayed record covers is left out. Every saved output (`export.json`, `analysis.json`, `report.md`) says whether the analysis is partial and lists the attempts left out (run key, attempt, reason); `analysed_attempts` holds only what was scored, and every count in the report is of that population, with the left-out runs' attempts in a column of their own.

A merged batch (`python -m expcore merge OUT SHARD_OUT...`, #154) is the batch of a campaign run as shards, each with its own store, `out/` and platform. The merge takes each shard's `out/` and writes `OUT/runs/` (the union of the shards' runs), `OUT/shards/<index>/campaign-snapshot.sqlite` (each shard's snapshot of its store, taken with the same online backup) and `OUT/merge.json`: `{"schema": "expcore-merge/1", "fingerprint", "rule", "count", "environment_identity", "shards": [{"index", "label", "started_utc", "shares", "runs", "snapshot_sha256"}]}`, where `fingerprint` is the SHA-256 of the campaign fingerprint every shard shares, `runs` is how many runs the shard planned, and `shares` is what the shard recorded. `OUT` must not exist or must be empty; a refused merge leaves nothing behind. It refuses, naming the shard (`shard <index> (<label>)`) and the reason, shards that are not one campaign run against one environment (C29): another rule or count, an index missing or given twice, a label used twice, another campaign fingerprint, a run in two shards, a run that the shards' rule over the whole plan (`assign()`, or `assign_by_model()` with the layout of their fingerprint) gives to another shard, a run of the plan no shard holds, a shard with attempts still running, a record that fails the replay check `check` makes, or another environment identity. `check` on `OUT/runs` makes no snapshot: it compares each snapshot with the SHA-256 merge.json names, refuses anything under `OUT/shards/` that merge.json does not list, rebuilds merge.json from the snapshots and the study's plan through the merge's own checks and refuses a file that is not that document (a merged batch has at least 2 shards, each snapshot under its own index), replays the records as one batch, and records merge.json's SHA-256 under `merge` (a check of an unmerged batch has `snapshot`, never `merge`). Rebuilding needs the plan, so the check of a merged batch loads the study's configuration, which imports the platforms `[study]` names. `analyze` and `report` read merge.json and each snapshot once, compared with those hashes, rebuild merge.json from those snapshots in the same way, and cross-validate the records against all the snapshots. The export they build joins the shards' exports: each run list in the order of the whole plan, every entry (those of `excluded_attempts` too) with `"shard": <index>`, the figures that are sums summed (`planned_runs`, `running_attempts`, and the rows of `discarded_usage`, `retries_by_kind`, `infrastructure_by_condition` and `budget_holds` that share a key), `deployment_shares` summed per model and fingerprint with every share computed again from the sums (a fingerprint is confirmed when any shard confirmed it, by the witness of the first shard that did; a shard whose plan has no model with a set exports none, which counts as no entries, and the joined export has the key exactly when any shard's does), and a top-level `"shards"` that repeats merge.json's list. The joined run lists must hold every run of the plan exactly once. An unmerged batch's export has neither key. `dispatch_seq`, `started_at` and `lease_wait_s` stay each shard's own, so the report's median dispatch position pools the positions of every shard's own sequence; every other figure, and the scores, equal what one driver of the whole campaign gives, except the witness of a deployment fingerprint two shards both confirmed, which is the first shard's where one driver names the earliest. A package of a merged batch lists `out/merge.json` and `out/shards` beside `out/runs`. Every build, plain or anonymised, first compares merge.json with the SHA-256 the check names and each snapshot with the SHA-256 merge.json names, and refuses any difference before it reads or rewrites either; an anonymised build rewrites each snapshot, then merge.json as the merge writes it from the rewritten snapshots (their new hashes, and the hash of the campaign fingerprint without the environment's sources).

Side files are verified with the records. A file a run writes beside its record, which the record names by its SHA-256, is declared in `study.toml` ([below](#side-files-of-a-record)). `check` hashes every file of a declared name in the batch it parses into its inputs, never replays one, and pairs each with the one record in its directory; the check records the declaration (`side_files`) and each side file's hash in `inputs`. `analyze`, `report`, `regenerate` and a package's `make check` refuse a check made under another declaration, refuse a side file changed, removed or added since the check, and read each side file once to pair it with its record again, whatever the check file claims; the bytes so read are what a [scorer](#scorer) that takes `side_files` is handed. A study that declares none is checked exactly as before, and its check file holds no `side_files`.

### Shards of a campaign

`run STUDY --shard i/N [--shard-label L [--shard-label-wide]]` runs shard i of N of one campaign on one host, with its own store, `out/` and platform ([`campaign/shard.py`](../src/expcore/campaign/shard.py), [`campaign/shard_run.py`](../src/expcore/campaign/shard_run.py)). It is given on the command line only, never in `campaign.toml` (which may declare only how many shards serve each model, below), so every shard has the campaign fingerprint of the whole plan. N is at least 2. The label is a plain lower-case name of 1 to 64 characters (`[a-z0-9-]`, starting with a letter or digit), `shard-i` by default, and never a host name or an address: it has no dot, so a fully qualified name or an IPv4 address is refused. `--shard-label-wide` (with `--shard-label`) allows `.` and `_` as well, for `ShardSpec.parse(..., wide_label=True)`; `ShardSpec.from_json` always reads the wide form, so a store or an export that pinned such a label reads back, and a resume of it gives `--shard-label-wide` again. Without `--shard` and without a `[shards]` table nothing here is used: the store, the records, the export and the report are byte for byte what they are without this feature. With a `[shards]` table, loading the study still runs the budget split (`shard_budgets_of`), whether or not the run is sharded, and so can refuse an unsharded run, for the budget reasons listed under sharding by model below.

- **Its plan** is the whole plan's runs of the pairs (task, model, seed) assigned to the shard by `shard.assign`, balanced per model, each run keeping its seq in the whole plan and the order of the order seed (C28). `manifest.shard_manifest` filters the manifest; `plan_problem` and the check of the run set compare a start against that part of the plan its fingerprint declares. `shard.partition(pairs, spec, layout)` picks the assignment by the spec's rule, and the merge checks each run with it.
- **Sharding by model** (C32): `[shards] per_model = { model = count, ... }` in `campaign.toml` (`CampaignConfig.shards`, a `shard.ModelShards` that keeps the declared order) puts `{"per_model": [[model, count], ...]}` under `caps.shards` of the campaign fingerprint, only when declared, and makes the spec's rule `shard.RULE_BY_MODEL` (`expcore-shard/3`; `shard.by_model` converts the command line's spec). N must be the sum (`--shard i/N` with another N is refused at start); a count of the models of one unit above its `[scheduler.unit_caps]` cap, a model of the plan without a count, and a sum below 2 are refused at load. `ModelShards.at(i)` gives shard i's model and its place k among that model's shards, the models in declared order; `shard.assign_by_model` deals each model's pairs among its own shards as `assign` deals them. Such a shard's shares are `{"unit_caps": {its model's unit: 1}, "budget": ...}` (`shard_run.share_settings_by_model`), its budget part from `campaign.shard_budget.shard_budgets`: its model's `per_model_usd` split over the model's shards, and `max_usd` over the priced models' shards in proportion to planned runs times `shard_budget.reservation_usd` (the larger of `bound_usd` with and without the reply maximum), in micro-dollars rounded down. A shard of a model that costs nothing records `max_usd` 0 and runs without a `Budget`. `load_study` refuses, as a `ConfigError` naming `campaign.toml`, a budget too small for one run's reservation in every priced shard, a priced shard whose part of `max_usd` or of a ceiling above 0 is below one reservation, and a model with no price that is not free. Stores pinned under `expcore-shard/2` resume and merge as before.
- **A shard that could never run its pairs** is refused at start, before the store is opened (`shard_run.refuse_unrunnable`), naming the model: one holding pairs of a model whose concurrency share is 0, which under `expcore-shard/2` is a unit cap below N for the shards past it, and under `expcore-shard/3` any model but the shard's own.
- **Its store** pins four facts in the meta table at the first start, inside the transaction that pins the order seed, as canonical JSON (sorted keys, no spaces): `shard_spec` (`ShardSpec.to_json()`: rule, index, count, label), `shard_started_utc`, `environment_identity` (the checked identity) and `shard_shares` (`{"unit_caps": {unit: n}, "budget": {"max_usd": x, "per_model_usd": {model: x}} or null}`).
- **A resume refuses**, as a `ShardMismatch`, naming the difference and leaving the store as it was: another spec, label or rule; a sharded store resumed without a shard, and an unsharded store resumed with one; another environment identity; other shares. The plan of a sharded store never grows on resume, whatever `allow_growth` says (the assignment depends on the whole plan): the `FingerprintMismatch` for the grown plan says so.
- **Shares** (C30) are applied after the fingerprint is computed from the totals: each unit's cap becomes `caps_shares(...)` and `max_usd` and `per_model_usd` become `budget_shares(...)`, by `dataclasses.replace` on the scheduler settings and the budget; the release step and file and `workers` stay the host's. A total below N is refused at start, naming its key, before the store is opened; so is a shard that has no run. A `per_model_usd` ceiling of 0 holds its model entirely and shares as 0 for every shard (`share_usd(..., allow_zero=True)`); a total of 0 anywhere else is still refused.
- **The environment identity** is `StudyPlugin.environment_identity`, a function with no arguments that returns names mapped to SHA-256 hex digests (`shard.check_identity`). The core calls it once, when a shard starts, never in an unsharded campaign, and never at plugin load; the plugin is refused at load only if the field is set and not callable. Absent, the identity is an empty mapping. A function that raises, or one that returns names that are not text, or anything else that is not names mapped to digests, is a refusal at start that names the plugin's file, before the store is opened. Names and digests only: no host name, path or label that identifies a machine (C29).
- **Its start says its environment** (C29): as a shard starts, after the refusals above and before the store is opened, `run` prints one block on stderr (`shard_run.environment_block`): the SHA-256 of the fingerprint's environment section (which every shard host of the campaign must print alike), the Python, and the distributions, each with its version up to `LIST_LIMIT` (60) of them and each with 12 characters of the SHA-256 of its version and source beyond that. No source URL, commit or path is printed. A host that would not merge shows before it runs, and a resume the store refuses for another environment has already said which one this host has. An unsharded start prints nothing.
- **The export** of a shard has a top-level `shard` object, last: `{spec, started_utc, environment_identity, shares}`, read from the store's meta. Per-run entries are the ones every export has. An unsharded export has no such key.

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
- A backend may have a `requests_sent` count, the requests it has started, a resend after a failure included; the core's backends have it. The scheduler reads it only to tell an attempt that sent no request after an instrument's first-turn refusal ([below](#refusing-an-attempt-the-first-turn-hook-and-the-post-run-check)): a backend of a study's own without it is taken at the refusal's word.

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
- **Record version.** This is record schema version 3 (`expcore-record/3`), which adds `extra` and `extra_cut` to the usage of each turn, both optional ([the schema](#the-run-records-schema)). A record of version 1 or 2 lacks both and stays valid: it loads, writes back to its own bytes and replays, and a replay of it never writes them. A record of version 3 written before this change lacks them too, and is valid. Every new record is version 3 and keeps version 2's first-turn rule. Version 3 is also the first under the reply-count rule (C26, `expcore.records.schema.reply_count_rule`): when the backend counted a billed reply that became no turn, the record's `totals.usage_incomplete` is true, while a record of version 1 or 2 keeps the totals it was written with. The merge-point fixtures pin both: `tests/data/record-v3/01-usage-extra.json` the usage fields, and `02-billed-unread-reply.json` a billed reply the adapter could not read. The third, `03-fault-target.json`, pins the target of a fault ([the schema](#the-run-records-schema)), and a fourth, `04-instrument-refusal.json`, pins the optional `instrument_refusal` ([below](#refusing-an-attempt-the-first-turn-hook-and-the-post-run-check)).

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
- An `InfraError` raised by the provider ends the attempt as infrastructure of that kind (C02), naming the target it carries (`InfraError(kind, detail, target="providers[1]")`, a position label, never a configured name); any other exception is a harness defect: the attempt is set aside (L08).

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
- An attempt that is set aside (infrastructure not declared as treatment, or a harness defect) is never scored. An attempt that ends in a declared treatment kind, after the model completed at least one turn, is, so the scorer must handle a partial record; the same kind before the first turn is set aside. Where the condition declares the kind with a target (record version 3), only a failure of that target while the fault injector held it down is scored; any other is set aside.
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

The study's executor calls them; nothing in the loop does. Two more methods are optional, `at_first_turn` and `check`, and let an instrument refuse the attempt ([below](#refusing-an-attempt-the-first-turn-hook-and-the-post-run-check)); an instrument without them is called exactly as before. `expcore.records.instruments` has the two helpers an executor uses:

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

The platform has three instruments. The first is `expcore_onedata.probes.PlatformProbes` (namespace `platform_probes`): the block map of each tracked file on each tracked provider before and after the run, the records of the transfers the provisioner scheduled, the record of every transfer of the space that started in the run's window whoever started it (`space_transfers`), the bytes moved between provider pairs, attributed to a source only where exactly one other provider held those bytes at the start, and per receiving provider the transfer bytes into it and a lower bound on the bytes fetched on demand (`on_demand`). Every provider in the snapshot (version 3) is named by its position, `providers[i]`, never by its configured name, since records are published. A provider's `gained_bytes` is placement gained: the bytes of the blocks a provider's block map shows at run end that it did not show at run start; what it showed at the start and no longer does is `lost_bytes`, kept apart, so it is not a net change, and it is not bytes on the wire. See its module docstring and `expcore_onedata.transferlog` for the snapshot's fields.

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
- the run's start (O24): an executor that checks the sandbox before the agent's loop, as the agent's user (`exec(code, timeout_s, record=False)`, a harness step), calls `sandbox.mark_run_start()` once its last such step has returned and before the loop starts. A REST data path's gate, signalled with SIGUSR1, writes a `run_start` line in its log that names each request it had begun and not yet logged, and the sandbox reads it back before it returns; an agent's cell entered meanwhile waits until it has, so no request of the agent's can precede it. The counts, the tables and so the lookups then hold only the answers after the mark; the answers before it, and those it names, are counted apart in `before_run_start` (`requests`, `served_bytes`, `content_bytes`, `listing_bytes`, `other_bytes`, `gate_answer_bytes`), never in a table, so nothing is hidden. `run_start_marked` says whether a mark was set: without one every answer is the run's, as before, and `before_run_start` is `None`; with no data path it is all zeros. A snapshot recorded before these two keys existed lacks them, which reads as not recorded, never as no mark. `mark_run_start` returns what it found, which `metadata()["run_start"]` keeps: `{"gate": false}` without a gate, otherwise `gate`, `signalled` and `confirmed`. It is refused a second time, once one of the agent's cells has begun (its `exec` entered: a mark during the agent's first cell would set that cell's reads apart), while a harness step runs, and after the collection; a mark that was not confirmed leaves the count unknown, and the executor decides whether the run goes on. The gate's own summary (`metadata()["gate"]`) and its refusals still cover its whole log;
- unknown, never zero: every count and every table is `None`, with the reasons in `unknown`, before the collection (`gate_not_collected`) and whenever a line may be missing or unreadable (`gate_not_stopped`, `gate_teardown_unconfirmed`, `gate_log_unreadable`, `gate_log_malformed`, `gate_log_write_failed`, `gate_output_unreadable`, `gate_teardown_incomplete`, `request_in_flight_at_stop`), so a table is never a part of the run's reads taken for the whole. So is a mark of the run's start the sandbox set and the log does not confirm (`run_start_unconfirmed`): one not read back before the loop started, or not in the log exactly once and before its `teardown_complete` line, is never taken as "everything after"; a `run_start` line in the log of a run that set none, or one that names no requests in the gate's form, is a malformed line. An answer of a file's content or a listing whose path names no file or directory is a malformed line. A log counts as whole only with a positive mark, the `teardown_complete` line the gate writes last, once every connection's close line is written: an exit code proves nothing, since a gate killed by its memory limit exits too. The gate's own output (`docker logs`) must also have been read, since it is where a failed write of the log shows; a read that fails leaves the count unknown. The sandbox's `metadata()["served_bytes"]` holds the same count for operators.

The count is taken once, when the collection stops the gate, and the record keeps it: replay and re-scoring read the recorded snapshot, tables included, and never measure them again.

The third is `expcore_onedata.ingest.ProgressiveIngest(provisioner, plan, *, journal, max_wait_s=1.0, join_timeout_s=None)` (namespace `progressive_ingest`, `progressive_ingest_space<i>` with several spaces, O27): part of a dataset arrives during the run, as if its owner were still ingesting it, so a study can test whether an agent notices that its data are incomplete. The plan is a TOML file of the study's, one per condition, loaded with `expcore_onedata.ingestplan.load_ingest_plan(path, config)`:

```toml
[ingest]
space = "<label>"                # the [[spaces]] entry; optional with one space
staging = "staged"               # a local directory, relative to the plan, holding every source
visible_on = ["<provider>"]      # optional: who must see each arrival; by default every provider of the space

[[ingest.files]]
label = "day-13"                 # how records and messages name the file, never by its path
path = "records/day-13.csv"      # where the agent finds it, relative to the space root
source = "day-13.csv"            # its content, relative to staging; hashed at load and never changed
at_s = 30                        # seconds after the instrument's start
baseline = "complete"            # required: "complete" (part of the dataset between runs) or "absent" (new data)
parts = 4                        # optional: written in parts, so it is partial for a while (1, whole, by default)
over_s = 60                      # with parts > 1: the last part this many seconds after the first
```

Each refusal is a `ConfigError` naming the plan's file and the key: an unknown key, a label or path given twice, a path under another, a source outside the staging directory, `parts` above the source's size, `over_s` without parts or parts without it, a `visible_on` provider that does not support the space. `IngestPlan.steps()` lists every part in the order it falls due, part k at `at_s + over_s * (k - 1) / (parts - 1)` and ending at byte `ceil(size * k / parts)`, with the SHA-256 of that prefix. The plan is part of the condition and of the campaign fingerprint: `StudyPlugin(platform={**config.fingerprint(), **ingestplan.fingerprint({condition: plan})})` holds one hash per plan, covering its space, its providers and each file's path, staged content, schedule and baseline, so a resume after any of them changed is refused (C06). An empty plan loads and records the same steps, for a sham control.

The executor gives the instrument a provisioner of its own, bound to the plan's space, whose change log is the ingest's and is never given to a teardown, and a journal file kept across runs. The instrument claims the log when it is built (`Provisioner.claim_log`, a line of the log): a log that already holds another user's creation or another role's claim is refused with `ProvisionError`, and `teardown` over a claimed log is refused with nothing sent; the ingests of one study's runs, one after another, share the claim. The executor starts the instrument with the others and finishes it with `finish_instruments`, which runs its post-run check:
- `start` restores the baseline first, which also recovers a crashed run (a part half written is finished from its staged source into the file the ingest made; a file removed and never restored is written back once every provider answers its old id absent), checks that every "absent" file's staged source still has the plan's hash (a "complete" one's restore checked its own) and every planned file's directory exists, and then removes each "complete" file through the gate (its staged copy checked again just before the delete, so a copy damaged between the restore and the removal leaves the file in place and refuses the start), confirmed absent on every provider of the space before its name can be written again (25.0 deletes a directory asynchronously and can lose files under a name deleted and recreated at once, #29; the wait covers every removed file as a precaution). It then starts the schedule's thread. A start that cannot do this restores what it can and raises `InfraError("ingest_failure")`, so the attempt is set aside before the agent runs; any other exception from it is a defect.
- each part is written through the provisioner's `upload(path, source, stop=<its end>)`, so through the mutation gate bound to the plan's space and never in another; it counts only when the hash read back is the plan's hash of that prefix, the data provider's block map covers it, and every provider of `visible_on` shows the file at that size (`consistency.wait_visible`, bounded). A part that cannot be written or confirmed is an ingest failure: the schedule stops and every later part is recorded as `abandoned`, never dropped.
- `finish` stops the schedule (a part not yet due is `not_due`: the run ended first, which is the manipulation, never a failure), restores the baseline and returns the snapshot; `restore()` is the same for an executor's `finally` when `finish` was not reached, and is idempotent; `stop()` only stops the schedule, returning False when a part was still being written at `join_timeout_s` (`[placement] transfer_timeout_s` by default), which is an ingest failure, and the restore then refuses to race it. The restore puts each "complete" file back from its staged source, checked against the plan's hash first, verified by size, SHA-256, the data provider's block map and every `visible_on` provider, and removes each "absent" file. It restores each file on its own: a file that fails stops none of the others, the failures are raised together after the loop (`restore.files` holds each file's result, a failed one with `ok` false), so one bad staged copy never leaves another file missing. `finish` and `restore()` block, so an executor on an event loop calls `restore()` in its `finally` through `asyncio.to_thread`, as it calls `finish_instruments`. It removes a file at a planned path only when the ingest's change log says it made it or its bytes read back are its staged source's, and every removal checks that file's staged source against the plan's hash first, a file the ingest made included, since the copy it came from may be the last good one of its bytes: a damaged copy leaves the file in place and fails that file's restore (or the start, which then restores what it can), an infrastructure failure the post-run check refuses, while the other files are still restored; anything else there is refused with nothing sent, and no directory is created or removed, so no data the study keeps is lost.
- `check()`, the post-run check (C31), raises `InfraError("ingest_failure")` naming the step (the start, an arrival with its label and part, the stop, or the restore) when it failed or never ran, and never a private name; `finish_instruments` puts the refusal in the record as `instrument_refusal` and the scheduler sets the attempt aside as charged infrastructure and runs it again, so a failed ingest is never scored as the agent's outcome, whatever the executor does after. It raises `RuntimeError` for a defect of the schedule, so the attempt is a harness defect. `raise_if_failed()` is the same check, a second guard an executor may call once the record is written. `ingest_failure` is registered with `treatable=False` in `expcore_onedata.sandbox.kinds`: a plan the harness could not carry out says nothing about the agent, while a read of a file that has not arrived is the agent's outcome.

The snapshot (version 1) is `{"version", "ok", "anchor", "anchor_clock", "providers", "plan", "start", "steps", "ids", "files", "failure", "defect", "restore", "reads_retried"}`, with `space` too when several spaces are declared. `reads_retried` is how many of the ingest's reads timed out and were tried again (O29, "A read that timed out is tried again" under the provisioner's files in and out below), which says how many of an ingest's reads needed it. Every reading is clock-labelled: `clock` is the provisioner's monotonic clock, the one the loop's clock readings use, `t_rel_s` the seconds from `anchor_clock` (the end of `start`, `anchor` `instrument_start`), and `utc`. `steps` holds each part as `planned` (its `due_s`, `bytes` and `sha256`), `written` (`late_s`, `action`, `file_id`), `confirmed` (`holder_covered`, `visible_s` per provider position), `failed`, `abandoned` or `not_due`. `ids` holds every file id the ingest removed, created or restored, recorded as soon as the file exists (a restore's before its hash is checked and its arrival confirmed; a file an upload made before it failed, found in the change log, as `created`), each with the file's `label`, `event` (`removed`, `created`, `restored`), `phase` (`start`, `run`, `restore`), `old_id`, `new_id`, the restore's `action`, and the `mtime` and `ctime` the data provider gives (None when it gives none, `attributes_ok` false when they could not be read), so a study can map gate errors to files and spaces after the run; the journal holds the same lines, a crashed run's included. `files` holds per label the `baseline`, the `state` (`arrived`, `partial`, `not_arrived`, `failed`), `parts_confirmed`, and the `baseline_id`, `arrived_id` and `restored_id`. `plan` is `IngestPlan.summary()`: its hash and each file's label, bytes, SHA-256, schedule and baseline. Providers are named by their position and files by their label, never by a name or a path. `ok` is false whenever `failure`, `defect` or a failed `restore` is set.

Not yet in it: the preallocated mode (a file at its final size with only a declared prefix written), which waits for the live fact of what a read of an unwritten range returns; offsets anchored at the agent's first model turn (#135's first-turn hook is not used yet; the snapshot's `anchor` names the anchor in use); #27's sanction, durable recovery and exclusive admission with its matched control, so a study that runs ingest and other conditions on one owner's space must keep them apart itself; and optional metadata signals of an ingest in progress. It has not run on a live federation. A live check on the Compose federation, with two providers supporting the space, must show: a "complete" file removed at `start` answers `posix` `enoent` on both providers before the run begins; at its offset it appears on the data provider and, within the visibility bound, on the other provider at its size, and a file in parts is seen there at each part's size in turn; every recorded file id answers `GET /data/{id}` while it exists and resolves to the plan's space by its parent links, and the removed ones answer absent; the clock readings fall in the plan's order with their lateness; after `finish` the file has its staged source's SHA-256, the data provider's block map covers it, and the staged copy and every other file of the space are unchanged; a name removed and written again after the absence check never loses the new file; and a refused write (a read-only token, say) is recorded as `ingest_failure`.

### Refusing an attempt: the first-turn hook and the post-run check

A manipulation can be defined on the agent's clock: at the agent's first turn, the arriving files are in their declared start state. An instrument that measures it needs a call at that moment to read the state, and a way to say, after the run, that the manipulation was not realised (the start state was wrong, or the plan failed mid-run), so that the attempt is not scored. Two optional methods give it both (C31). An instrument without them is called as it always was.

```python
class StartState:  # an instrument with both; neither is a member of the `Instrument` protocol
    namespace = "start_state"
    def start(self, identity: Mapping[str, Any]) -> None
    def at_first_turn(self, moment: Mapping[str, Any]) -> None  # moment == {"clock_s": <reading of the run's clock>}
    def finish(self) -> Mapping[str, Any]
    def check(self) -> None  # returns when the manipulation held; raises InfraError(<untreatable kind>, detail) to refuse
```

```python
def first_turn_instruments(instruments: Sequence[Instrument], timer: AgentTimer) -> None
# finish_instruments(record, instruments) -> RunRecord  runs the checks, after the snapshots
```

An executor with such instruments calls the first just before `record_run` and the second where it called `finish_instruments`:

```python
await asyncio.to_thread(start_instruments, instruments, identity)
# ... the executor's own steps before the loop, such as sandbox.mark_run_start() ...
await asyncio.to_thread(first_turn_instruments, instruments, request.timer)
record = await record_run(request, providers, system, user, config, code=code)
# ... sandbox.collect(...), when the platform needs it ...
record = await asyncio.to_thread(finish_instruments, record, instruments)  # snapshots, then the checks
record.write(request.run_dir)  # the record that holds the snapshots and, if a check refused, the refusal
```

- **When the hook fires.** `first_turn_instruments` runs after the executor's last step of its own and just before `record_run`, so before the loop starts the agent's clock and before the first request is sent. The agent's time is not charged for it, and it is no wait kind. The loop is not involved, so a little work of the core's own (building the tool box, starting the clock) still lies between the hook and the request. It takes one reading of the run's clock and logs it in the record's clock under `instrument_first_turn`; each hook gets it as `moment["clock_s"]`, the same number, so what the instrument reads is labelled with the clock that the loop's own readings (`start`, `before_model`, ...) are on. With no instrument that has `at_first_turn` it takes no reading and returns. It refuses to run once the timer has started.
- **What the hook may do.** Read the state, keep the reading for `finish` to record (the hook returns nothing; the snapshot is the record of it), and refuse. It runs blocking I/O on the executor's thread, so the executor calls it through `asyncio.to_thread`, and it bounds itself: the loop's watch does not cover it. It must not change the manipulation it measures.
- **How the hook's failure is classified.** An `InfraError` of a kind no condition can declare as its treatment (a core kind such as `tool_plane_integrity`, a platform's registered with `treatable=False`, or a study's own declared with `treatable = false` in `[study.kinds]`) is the instrument's refusal. It leaves `first_turn_instruments` as an `InfraError` of that kind whose detail names the instrument, the executor lets it reach its caller, and the attempt is recorded as infrastructure of that kind: set aside, charged, run again like any infrastructure failure. No request was sent, so it completed no turn and is never treatment, whatever a condition declares. It has no record, as an executor that raised has none, but its usage is known: it is a `FirstTurnRefusal`, raised before the loop's first request, so the attempt holds zero tokens and no turn and costs nothing at any price, as the attempt of a run that found an outage before it began does. It is still the run's charged attempt. If the backend's `requests_sent` says a request was started before the hook ran, the usage is unknown as for any executor that raised, and the attempt is charged its bound. A refusal the executor wraps in another error loses that, and is charged the bound too. An `InfraError` of a kind a condition may declare, and any other exception, is a defect of the instrument: `InstrumentRefusalError` for the first, the exception itself for the second, and the attempt is a harness defect (C10).
- **When the post-run check runs.** `finish_instruments` takes every snapshot first and then asks each instrument that has `check`, in order. The executor calls it after the platform's collection (`sandbox.collect(...)`) and before the teardown, so a check can still read what the run left; a check that needs a reading `finish` failed to take is still called, and refuses, since unknown is not a pass. A check that passes changes nothing. A check that raises a kind a condition may declare, or anything but a refusal, raises out of `finish_instruments`: a defect, as for the hook, and with no record.
- **What the check may refuse, and how that is recorded.** It may refuse any attempt, one the loop scored (a cap, an answer) and one that ended in the fault its condition declares as its treatment alike. The first refusal stops the rest and goes into the record as `instrument_refusal`, `{"namespace", "infra_kind", "detail"}` (the detail cut at 500 characters), beside every snapshot, so the attempt keeps its record and its usage and is not charged as unknown. The record's outcome stays what the loop saw; `RunRecord.set_aside` is true, so re-scoring skips it.
- **Where the scheduler sets it aside.** In the one place a finished attempt can still be set aside (`AttemptRunner._judge`): the post-run health check first (C11), then the refusal, then the endpoint's identity (C16). An attempt the store would score, a cap or an answer or a fault its condition declares as its treatment, becomes infrastructure of the refusal's kind with the instrument and the detail, and an event `instrument_refused` says so; the run is tried again. That holds when an outage flag is on the attempt too: the treatment rule is checked before the outage flag (C01), so such an attempt is scored until the refusal rewrites it, and once rewritten to a kind that is never treatment it is not. A result that still carries the outage's kind is then classified `outage_discarded` and not charged, as any attempt an outage overlapped is; any other is `infra_fail` and charged. The result no longer claims the treatment the record flagged, so the store and the record do not disagree about it. An attempt the store would not score, one set aside by the health check, an outage, or its own infrastructure failure or defect, keeps its outcome, and the event says the outcome stands.
- **Replay and fixtures.** Replay runs no instrument. It serves the recorded `instrument_first_turn` reading and `instrument_refusal` back, so the replayed record has the stored bytes; the reading is not the loop's, so the replay clock does not serve it to the loop. `tests/data/record-v3/04-instrument-refusal.json` pins both.
- **Without hooks.** A study whose instruments have neither method takes no reading, writes no `instrument_refusal` (the key is absent, never null) and calls `finish_instruments` as before: the example study's records are what they were.


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

One namespace is read by the core: a fault injector writes under `fault_injector` (`expcore.records.injections.FAULT_NAMESPACE`) a snapshot holding the run key, the attempt and a list of `injections`, each with its `target` and `injected` (true only once the injector verified the target was down). Under a condition that declares a kind with a target, the treatment flag needs that target in this list for this run and attempt; a missing, failed or stale snapshot gives none, so the attempt is set aside.

The platform's fault injector is `expcore_onedata.faults.FaultInjector`. It has two kinds, registered by `expcore_onedata.sandbox.kinds`, the module a study lists in `[study] platforms` ([below](#infrastructure-kinds-of-a-platform-or-a-study)): `provider_outage`, which a condition may declare with a provider's label as its target, and `injector_failure`, its own failure to inject, clear or verify, which no condition may declare. Built only with `sanctioned = true` in `[faults]` of platform.toml and `sanctioned=True` from its caller (O10), it takes its target down in `start` and brings it back in `finish` (or in the `finally` of `session`, or of `running`, the async form that an executor uses, which keeps the start's thread and still clears when the run is cancelled), both outside the agent's time, so no wait kind covers either. A restore counts only when the provider's identity changed and, after that, only when the provider serves: a restarted provider's REST endpoint refused connections right after the restart and its container's health check passed 48 to 52 s later (the live run of 12 Nov 2026), so the injection is journalled cleared and the federation's exclusive admission released only once the driver says the provider serves (its container healthy, when it has a health check, and the platform's authenticated read of a space through that provider answered; `compose_driver(settings, probe=provider_serves(config, settings, client))`), polled every `poll_s` for at most `ready_timeout_s` of `[faults]` (180 s by default). A provider that does not serve in time is an unverified restore (`not_serving`): the injection stays open, the admission held and the fault gate failing, as for a failed restore, and a later `clear`, `finish`, `start` or the gate's recovery waits again. The snapshot records the wait as `ready_after_s`, the seconds from the verified identity change to the first reading that showed the provider serving, in the `verified` step and in the injection entry, and the `recovered` reading of a crashed run's injection carries it too. The injection is journalled before it happens, and the next start settles one a crash left open (O11). Outage runs are exclusive with every other run: with `[faults]` configured, every run holds the federation's reader-writer lock (`expcore_onedata.admission.FederationLock`) from before its infrastructure starts until after its post-run check, taken by the campaign through the plugin's `admission` (`run_admission(config, schema)`), shared for a baseline run and exclusive for an outage run, whose injector refuses a shared admission; a post-run check runs under the run's own admission, so no outage run can take the federation between a run's teardown and the check that judges it; the sandbox (`SandboxPlatform.launch`) and the tool server refuse to start without one, before they mint or start anything, and a sandbox start thread that a cancelled launch stops waiting for keeps a share of the admission until it has ended, so the federation stays held while it creates or removes anything. An admission names its federation (the lock file's resolved path and the digest of platform.toml), and one held for another federation is refused as none. The wait is admission, before the agent's clock starts, and a turnstile (`federation.intent`) queues later runs behind a waiting outage run so it is not starved. The platform's standard health check (`PlatformHealth`) runs the fault gate around the whole check, built in and with no opt-out: it recovers what a crashed run left open whenever no run is on the federation, then takes the federation shared, waiting at most `health_wait_s`, and holds it until the check has answered, so no injection starts mid-check and the content type is never measured through a provider an outage holds down; a check whose wait runs out because an outage run holds the federation answers deferred, which charges no run (see HealthCheck); to tell that wait from a fault, whoever holds the federation exclusively keeps a holder record beside the lock (pid, boot id, purpose, target label, start time), so a wait that runs out is deferred for a live holder, fails naming the target for a restore that failed, and fails as stuck, after recovering what it left open, for a holder that is gone, keeping the record marked `stuck_since` until an acquisition of the lock clears it; and the check fails while an injection stays open (recovery failed or its lock is stuck), so the scheduler's gate holds back every run, baseline conditions included. `provider_outage(config, name, detail)` builds the failure the platform attributes to a provider, named by its label (O12).

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
- With instruments that can refuse an attempt, call `first_turn_instruments(instruments, request.timer)` just before `record_run`, and write the record `finish_instruments` returned, which holds a refusal. Nothing else calls the hook, so an instrument whose executor never does has no first-turn reading, and its `check` should refuse for want of one ([instruments](#refusing-an-attempt-the-first-turn-hook-and-the-post-run-check)).

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

`expcore.campaign.healthgate.HealthCheck`; `Health` is a named tuple `(ok: bool, detail: str, deferred: bool = False)`.

```python
class HealthCheck(Protocol):
    def __call__(self) -> Health
```

Core guarantees: it is asked before an attempt starts and after it ends, and again at an interval while the gate is closed. A check that raises, or answers anything other than a `Health` (or an `(ok, detail)` pair with a real boolean), counts as a failed check, never as a pass or a crash. A failed pre-run check closes the gate, holds dispatch, and the run that found it is recorded as the outage's attempt, an infrastructure failure of `SchedulerConfig.health_kind` (C11): the `health_kind` key under `[scheduler]` in `campaign.toml`, `sandbox` unless the study names another, as [the study guide](study-guide.md#the-schedulers-kinds) shows. A failed post-run check turns an attempt that would have been scored into an infrastructure failure of that kind, or discards it as the open outage's. A deferred check (`Health(False, detail, deferred=True)`: the platform is held on purpose, by a run, and could not be looked at now) is a wait, never a pass or an outage: a deferred pre-run check holds its run for `recheck_s` without closing the gate or charging an attempt (event `run_deferred`), a deferred post-run check verified nothing, so any result `classify` would score, a treatment failure included, is set aside as `post_check_unverified`, a core kind no condition may declare (`expcore.campaign.attempts.unverified`) (event `post_run_check_deferred`), and only a passing check opens a closed gate. A deferral is bounded: its start is kept in the store's meta `health_deferral` (a restart keeps its age; `healthgate.fact` and `alarms` report it), a pass before or after a run ends it, and a pre-run check deferred longer than `SchedulerConfig.max_defer_s` (campaign.toml `[scheduler] max_defer_s`, default 3600) fails with the deferred detail, which names the holder's purpose and the target, closing the gate as a `blocking` outage: the all-dispatch hold is then not temporary, so `exit_when_blocked` stops the campaign. A gate already closed for another failure, whose own re-checks stay deferred past the bound, becomes blocking the same way. On a resume, a restored blocking outage holds dispatch without blocking it until the restored gate's first check has answered (bounded by `health_timeout_s`), so a platform that is healthy again reopens the campaign.

Implementer must guarantee: it is quick (bounded by `health_timeout_s`), side-effect free, and `detail` holds no secret. `health_kind` must not be a kind any condition declares as its treatment, and, like `identity_kind` (the kind of a run whose endpoint changed its served identity, C16, `endpoint` by default), it must be a kind known when the study is loaded, or the load is refused. Each infrastructure kind is treatable or not: the core's `tool_plane_integrity`, `budget_hold` and `post_check_unverified`, any kind a platform registers with `treatable=False` (the Onedata layer's `data_gate_integrity`, `output_collection` and `injector_failure`) and any kind a study declares with `treatable = false` cannot be declared in `infrastructure_is_treatment`, and the factor schema refuses to load a condition that does, naming the file and the key; the scheduler refuses, at its start, a campaign whose health or identity kind a condition declares.

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
    def mark_run_start(self) -> dict      # where the agent's run begins, before its loop (O24)

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

**Data paths.** The study's `data_path` factor names its levels in its own words; `[sandbox.data_paths]` maps each to one of three kinds, `mount`, `rest` or `none`, written as the kind alone (`level = "rest"`) or as a table that also names a REST level's option (`level = { kind = "rest", listing = "complete" }`). A study selects a data path and its option per condition, through the level the condition gives its `data_path` factor. A `mount` run has a read-only FUSE mount made by a oneclient sidecar that holds the data-path token; the agent holds no token and no FUSE. The sidecar takes the token from a file bound into it alone, read-only, never from docker's configuration of the container, so `docker inspect` does not show it, and the host's copy is removed once the mount is up and again at teardown. A `rest` run reaches the zone and its pinned local provider only through the data gate, which holds the token and adds it to the requests it forwards (a token the agent sends is dropped); the agent holds no token in its environment, its files or its process arguments, only the placeholder described below. The gate, like the sidecar, takes the token from a file bound into it alone, read-only, never from docker's configuration of its container. A `none` run has neither.

**The REST agent's environment and trust (O23).** The agent's environment is what the published client, `onedatarestfsspec`, reads: `ONEDATA_ONEZONE_HOST` (the zone's host name, which resolves to the gate, on port 443), `ONEDATA_VERIFY_SSL=true`, the CA variables `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` (the run's CA, mounted read-only), and `ONEDATA_TOKEN`, set to `[sandbox] placeholder_token` (`placeholder` by default; 1 to 128 printable ASCII characters without white space). The placeholder is no credential: the client refuses to start without a token, and the gate drops it as it drops any token the agent sends, from every header in which the platform reads one (`X-Auth-Token`, `Authorization`, the deprecated `Macaroon`, `X-Onedata-Consumer-Token`, `X-Onedata-Service-Token`) and from a scope inference's body; the platform reads no token from a query string, and a request whose header block folds a line (one that starts with a space or a tab) or holds a CR or LF inside one is answered 400 and forwarded nowhere. A placeholder equal to the run's token refuses the start. The client verifies TLS against the certifi bundle of the agent's image and reads none of the CA variables, so the start asks the image's interpreter (`[sandbox] python`), in a one-shot container with no network and as the agent's user, where certifi keeps its bundle, and treats that path as one more `trust_paths` entry: the image's bundle with the run's CA appended is mounted over it read-only. `metadata()["certifi"]` is `run_ca_added`, `not_in_image` when the image has no certifi (the start goes on, and certifi-based clients cannot verify the gate), or None without a REST data path; an interpreter that fails, or an answer that is not one absolute path the sandbox can mount over, refuses the start. A client that verifies against another bundle needs that bundle's path in `trust_paths`. The data path is read-only in every condition at four points: the token (4a: read-only, scoped to the space, one interface), the gate (only the five read calls pass), the mount, and the agent's root filesystem. `start` proves the last two by trying to write from inside the agent and requiring the refusal.

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
- Every image is started by the id it is pinned to in `platform.toml`; a tag that now resolves elsewhere raises `StartRefused` before anything exists (O13). Each sandbox gets its own small network from the configured pool, whose ranges are strict networks checked at load (O14). The agent is on that network alone. The mount sidecar is on Docker's default bridge, dialling the host of the local provider's URL, or, when `[sandbox.federation]` names the federation's own network, on that network instead, dialling the local provider by its name there; a mount run whose federation network is not on the host raises `StartRefused` before anything exists, and the sandbox never changes or removes that network (O03, O15). [`study-guide.md`](study-guide.md#how-the-sandbox-reaches-the-federation) shows the whole path.
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
- `exec(code, timeout_s, record=False)` is a step of the harness's own, a check before the agent's loop, and it runs before the agent's run only: it raises `SandboxError` once `mark_run_start` was called or one of the agent's cells has begun (its `exec` entered, not ended), since after the mark its requests would count as the agent's, and between the agent's cells it would take a cell's refusals and kills. It runs as the agent's user in the agent's container, as a cell does, and is recorded under `metadata()["harness_steps"]` (`step`, `status`, `exit_code`, `duration_s`, `timed_out`, `gate_refusals`, and `earlyoom_kills`, the host killer's kills of the run read when the step ended, `None` when its journal could not be read), never as a cell of the agent's (`metadata()["cells"]` counts the agent's alone). The gate's refusals while it ran and the host killer's kills are its own and never put on the agent's first cell, and that cell reads its memory counters afresh, so a kill under the step is never the cell's. Its only time limit is the backstop `timeout` in the container, a second after `timeout_s`: the harness's timer, which would stop every process of the agent's, is not started, and no kill cause is read (O24).
- A killed cell's cause comes from the cgroup counters, the kernel log, the host killer's journal and the container's state, never from the exit code. The agent's own limit is its outcome; a failure of the host or a component raises `InfraError("sandbox")`; a kill nothing attributes raises `InfraError("unattributed_kill")`, which the core retries within the attempt limit (O17). Errors the agent causes through the mount (ENOENT, EROFS, EISDIR) are not counted against the platform (O18).
- A message of any exception names roles, keys and positions, never a configured value (image, pool, path, login, level) or the tail of a tool's stderr (which goes to the log).

Not carried over from the earlier study: the gate's whole-file mode, the tracer and its checks, the capacity probes tied to two data layouts, and the replication branch of its fourth condition. Its merged-listing mode is the complete listing above, written anew against this gate with bounds and checks the earlier one lacked.

## The platform layer's files in and out

`expcore_onedata.provision.Provisioner` puts a study's data into its space and reads it back; every write and every range read goes through `expcore_onedata.client.OnezoneClient`'s mutation gate, bound to the provisioner's space (`space`): the target file's space is found by its parent links and its live name read right before each request, and a target in any other space is refused unsent (O09). `Digest` and `Upload` are in `expcore_onedata.streamed`.

```python
class Provisioner:
    def put_file(self, path: str, data: bytes, *, content_type: str = "application/octet-stream") -> str
    def upload(self, path: str, source: Path | str, *, content_type: str = "application/octet-stream") -> Upload
    def read_file(self, file_id: str, *, provider: str | None = None, dest: Path | str | None = None,
                  size: int | None = None, deadline_s: float | None = None) -> Digest
    def children(self, directory_id: str, provider: str | None = None, *, listings: Listings | None = None,
                 retry: bool = True) -> dict[str, Entry]
    retry: ReadRetry            # property: the policy of this provisioner's plumbing reads
    reads_retried: int          # property: the retries made under it

class OnezoneClient:
    def get(self, path: str, *, provider: str | None = None, token: str | None = None,
            params: Mapping[str, Any] | None = None, timeout_s: float | None = None,
            retry: ReadRetry = ONE_ATTEMPT, deadline_s: float | None = None) -> Any
    def mutate(self, method: str, path: str, *, provider: str | None = None, json: Any = None,
               params: Mapping[str, Any] | None = None, content: bytes | None = None, timeout_s: float | None = None,
               token: str | None = None, space: Space | None = None, content_type: str | None = None) -> Any
    def read_content(self, path: str, *, provider: str, size: int, chunk_bytes: int, deadline_s: float,
                     sink: Callable[[bytes], None] | None = None, timeout_s: float | None = None,
                     clock: Callable[[], float] = time.monotonic, space: Space | None = None) -> Digest

@dataclass(frozen=True)
class Digest:      # size: int, sha256: str (hex)
@dataclass(frozen=True)
class Upload:      # file_id: str, digest: Digest, action: "created" | "skipped" | "resumed" | "restarted", sent_bytes: int
```

`path` is relative to the space root; the provisioner works on its space's data provider (`holder` of its `[[spaces]]` entry, or `[placement] holder`).

- **`put_file`** takes the content in memory, refuses a file already at the path, and verifies the size read back. It is for small files, a day-0 scratch file for one.
- **`upload`** never holds the file: it reads and sends it `[placement] chunk_bytes` at a time, each chunk a PUT at its offset. A target that already holds the same size and SHA-256 is skipped with nothing sent, whoever created it, and stays out of the change log. One this provisioner created (the change log says so) that holds a strict prefix is resumed from where it ends by offset writes; any other it created is restarted from a write without an offset, which truncates it. A differing file it did not create is refused before anything is written (O09). The upload succeeds only when the size and the SHA-256 read back equal those of the bytes it read and sent; otherwise `ProvisionError`. Skipping or resuming reads the target back first, and verifying reads it again, so expect the bytes to cross the network about twice (O25).
- **Every write of content carries `content_type`**, a plain `type/subtype`, `application/octet-stream` (`client.UPLOAD_CONTENT_TYPE`) unless named: Onedata 25.0 answers 415 to a write without one. Only `application/octet-stream` is confirmed on 25.0, and the fake federation refuses any other type.
- **`read_file`** reads a file through `provider` (the data provider unless named; another provider is first waited on until it shows the file, `await_visible`) by ranges of `chunk_bytes`, each gated as a range read bound to the space (a read through a provider that lacks the bytes fetches them), streamed and hashed as it comes, and writes it to `dest` when one is given: to `<dest>.part`, moved into place only when the read is whole and removed when it fails. It is bounded in bytes by `size`, or by the size the file's attributes give, which every answer must match: each must be a 206 for exactly the range asked for, of a file of that size, with exactly its bytes. It is bounded in time by `deadline_s` (`[placement] transfer_timeout_s` unless given), checked before every range and after every piece, with no wait for the platform longer than `request_timeout_s` or what remains of the deadline. `OnezoneClient.read_content` is the same read for a caller with its own `sink`.
- **A read that timed out is tried again, boundedly and in the log** (O29, `expcore_onedata.readretry`). On 25.0 a read on a provider has hung past its timeout and then answered in a hundredth of a second on a new connection. `OnezoneClient.get` takes `retry`, the call site's choice, which every call site in the layer names: a `ReadRetry` (three attempts, pauses of 2 s and 5 s, a clock, a sleep and a count of the retries made under it; `ReadRetry()` is the plumbing's policy) or `ONE_ATTEMPT`, the default. Only a GET is retried, and only when it timed out (an `httpx` read, connect or pool timeout); the pause and the retry are on the policy's clock and sleep, so a test's ticker moves with them. Never retried: a PUT, POST (the reading POSTs of `query` included), PATCH or DELETE, an error answer of any status, a connection error (a provider that is down must look down), a write timeout, and a streamed or range read; `get` is the only call of the client that takes a policy, so no other can be handed one. Each retry runs on a connection of its own, never one of the pool's, which may be as stale as the one that stalled: a client that built its own HTTP client sends the retry through a one-shot client of the same settings, which is closed when the answer is in, and the connection that stalled is closed by httpcore and dropped from the pool. `deadline_s` bounds the whole call: no retry starts unless its pause and a second more fit in what remains, and a retry's timeout is cut to what remains; `read_file`'s size read and the waits' reads are bounded by their own deadlines and bounds. Each timed-out attempt is logged (logger `expcore_onedata.readretry`) with the seconds it took, the attempt, and how its connection began, `new`, `reused` or `unknown`, and for a `reused` one how many requests it had served before (read from the pool when one connection is active, else "not seen"), and so is the outcome of the attempt after a retry; a line names the provider by its position and holds no path, id or token. The provisioner owns the policy of its plumbing reads (`Provisioner.retry`, `reads_retried`) and the reset and the ingest that work through it use it, so their records say how many reads were retried. **Retried:** the provisioner's listings (`children`, `walk`, `resolve`, `upload`, `make_dir`), its size and attribute reads, `read_file`'s size read and the visibility wait (`await_visible`), the reset's reads (summaries, transfer listings, records and waits, the occupancy, a space's root), the ingest's attribute reads and visibility waits, and the root a day-0 mutating check writes under. **One attempt:** the health gate's reads, the placement probes' reads (a probe's listing, which `resolve(wait=False)` makes one attempt, and the block map, `Provisioner.blocks`) with their own counted retry of a transport failure, the transfer log, day-0's reading checks, the transfer polls of `replicate`, `evict` and the eviction stage, the client's own gate reads (they fail closed), and whatever a poll that counts a failed read as a poll that did not match reads, since the poll is the retry. `tests/onedata/test_read_retry_sites.py` lists every call site with its choice, so none changes without that table changing.
- **A listing ends only on its last-page marker** (O26). A children page ends it only with a boolean `isLast` that is true, whatever its `nextPageToken`, and a page of the transfer listing only with `nextPageToken` null, with no `isLast` asked for; a page without the marker is refused (`PlatformError`, or `Unreadable` in the probes), never taken as the whole directory. These are the shapes measured on 25.0 (`deploy/compose-federation/day0/2026-11-12-federation-facts-listing-end.md`): a children page carries a token on the last page too, a full last page of either listing is followed by an empty one, and the page after a last children page answers 400 `posix` `eagain`. `provision.next_page_token(page)` reads a children page for any caller, and the testing module's fakes answer both listings so (`fake_data.LISTING_END_FACTS`).

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

## The platform layer's spaces and reset

`expcore_onedata.spaces`, `provision`, `eviction` and `reset`. A federation study has one space per data owner, each supported by its owner's provider, by the agent's local provider and sometimes by a mirror. `platform.toml` declares them as an array of tables, in place of `[space]` and `[support]` (one form or the other, never both):

```toml
[[spaces]]
label = "<label>"               # how code names the space; a message names it by its position, spaces[i]
name = "..."                    # name, id and prefixes as in [space]
id = "..."
prefixes = ["..."]
providers = ["<name>", ...]     # the providers declared to support this space (O04)
local = "<name>"                # the agent's own provider, one of these; the same in every entry (O03)
holder = "<name>"               # this space's data provider, one of its providers: required (O09)
tokens = ["data_path", "tool_server"]  # optional: the token planes that reach this space; both by default
```

A `[[placement.map]]` row (or `[[map]]` in a study's file) names its space with `space = "<label>"`, required with several spaces. `[placement] concurrency` (16 by default) bounds the requests a bulk operation has in flight.

```python
class PlatformConfig:               # config.py
    spaces: tuple[StudySpace, ...]  # the one-space form's single space (label "space"), or the [[spaces]] entries
    def space_named(self, label: str | None = None) -> StudySpace   # None: the only space
    space: Space; support: Support  # the only space's; refused when several are declared
    prefixes: tuple[str, ...]       # every space's, each once: what the mutation gate accepts
    def space_ids(self, plane: str) -> tuple[str, ...]              # the spaces a token plane is scoped to

class Provisioner:                  # provision.py: bound to one space
    def __init__(self, client, config, settings, *, provider_ids, log, space: str | None = None, ...)
    def resolve_many(self, paths) -> dict[str, Entry]               # one listing of each directory
    def walk(self, directory_id=None) -> Iterator[tuple[Entry, bool]]  # every entry under a directory, and its leafness

def evict_tree(provisioner, path: str | Entry, from_provider) -> dict   # eviction.py: one transfer of a directory
def evict_ids(provisioner, file_ids, from_provider) -> dict             # files in parallel under the bound

class SpaceReset:                   # reset.py
    def __init__(self, provisioner, *, providers=None)
    def run(self, evict: Mapping[str, Iterable[str]] | None = None,   # provider -> paths that must leave it
            allowed: Mapping[str, int] | None = None) -> dict          # provider -> physical bytes it may keep
```

The platform layer guarantees:
- The one-space form behaves as before, and `PlatformConfig.fingerprint()` is unchanged for it. Whenever `[[spaces]]` is used, with one entry or several, the fingerprint holds each entry's SHA-256 in order, never a label, name or id, so a resume under another set, order, support, data provider or token planes is refused (C06).
- A provisioner acts in its own space alone: its live name is checked against that space's name and prefixes before the first change, every mutation it sends is bound to that space (the gate refuses a target in any other space, even under a shared prefix), its data provider, which must support the space, is never evicted, by name or by id (O09), and with several spaces every message starts with the space's position. Placement, eviction and the probes take the space from the provisioner, and each space's probes have their own namespace, `platform_probes_space<i>`, and name the space in their snapshot.
- A read-only token's `data.path` caveat lists exactly the spaces its plane is scoped to, its principal's rights are read in each, and the examination refuses a token that misses one or adds another (O05). The data path's token is read-only, and so is the tool server's when its condition declares no write right. A writing condition's tool-server token carries no data-access caveat (`data.path`, `data.objectid`, `data.readonly`), only its expiry and the `rest` interface, because on Onedata 25.0 any of them limits a token to data access and its transfers and QoS requirements are refused (`tokenCaveatUnverified`); its examination refuses one that carries any. Its reach is its principal, held to the plane before the mint, at the tool server's launch and at its teardown: the user's rights are read in each space of the plane and must be the condition's, Onezone's scope of the token (`infer_access_token_scope`, every space the user is an effective member of) must be exactly the plane's spaces, the user must hold no Onezone admin privilege, and it must be an effective member of no cluster, since a member of a provider's cluster can mint that provider's tokens and change or delete the provider (`tokenscope`, O09). Onezone has no read of another user's clusters, so the clusters are read as the token itself (`GET /user/effective_clusters`, the current user's), and the admin privileges with the admin credential. A refusal names a space by its position, or counts the undeclared ones, and counts clusters, never naming one. The tool server's launch checks and the data gate's token follow the plane's set. The health check's token is the data path's, scoped to the spaces the data path reaches, and it compares each of them with its own support set (O04); its content-type check reads `[sandbox] content_check_file` under the root of the first space the data path reaches.
- `run` first refuses a plan that names the data provider (by name or id) or a provider that does not support the space, and verifies the space's live name; then it takes five stages in order, each read on every supporting provider and recorded with its seconds: `qos` (every requirement, found from the summaries of the root and the leaves, removed through the gate, each then answering that it is not there (`answered_absent`) on every provider and a second walk on each finding none), `transfers` (every waiting or ongoing job cancelled on every provider that lists it until `transfer_settle_polls` observations in a row, `transfer_settle_s` apart, show none), `eviction` (each provider's paths, a directory in one transfer and files together, verified on the block maps only once no transfer runs on any provider, and none running after, O01; what still holds bytes is scheduled again, as `Provisioner.evict` does, since an eviction on 25.0 can complete having evicted nothing, up to `EVICTION_ATTEMPTS`), `occupancy` (with no transfer running, the space root's per-provider physical size, never the lagging logical figures: a provider other than the data provider holding more than `allowed` fails the reset) and `history` (the ended listing's count and newest finish time per provider: ended transfers cannot be removed, so they are recorded, `removable` false). A stage that cannot verify what it did raises `ResetFailed` with the stages so far and runs no later stage; the caller must not start the next run. The record, the failed one's included, carries `reads_retried`: how many of the reset's reads timed out and were tried again during it (O29), 0 when none did.
- The mutation gate resolves a transfer's cancel by its record's `spaceId` and a QoS requirement's removal by its record's `fileId`, read right before the mutation and never kept. A directory's eviction and a transfer record's `spaceId` were read on the live federation (`deploy/compose-federation/day0/2026-11-11-federation-facts.md`), and so was the shape of a directory's distribution (`day0/2026-11-11-federation-facts-posix-and-fuse.md`); the cancel, the QoS summary, a requirement's record and its removal are named only by Oneprovider 25.0's REST description, so they are in `client.UNVERIFIED_ENDPOINTS`. Each has a live test (`EXPCORE_LIVE_MUTATE`; the whole reset also `EXPCORE_LIVE_RESET`).

## The platform layer's testing module

`expcore_onedata.testing`, installed with the platform layer (the `[onedata]` extra). It holds the fakes of the Onedata platform that the core's own tests use, so a study tests its instruments, probes and scorers against the same stand-ins without a live federation. [`study-guide.md`](study-guide.md#testing-a-studys-instruments-and-scorers) shows them in use.

```python
class FakeData(FakeFederation):     # fake_data.py: Onezone plus every provider's data API
    def __init__(self, *, error_shape: str = ERRORS_25_0, visibility_delay_s: float = 0.0, **kwargs)
    def add_file(self, path: str, content: bytes, *, holder: str | None = None) -> str
    def add_tree(self, root_id: str, path: str) -> str                     # a tree of a space no configuration knows
    def answer_missing_as_25(self, answer=(400, "posix", "enoent")) -> None # what is gone, as 25.0 answers it
    def add_transfer(self, kind, path, provider_id, *, scheduled, source_id=None, ended=True, waiting=False,
                     finished=None, bytes_replicated=None) -> str          # a job no request to /transfers made
    def end_transfer(self, tid: str, *, at: float | None = None) -> None    # places or removes the blocks, as of then
    def id_of(self, path: str) -> str
    def held(self, path: str, provider_id: str) -> list[list[int]]
    def client_http(self) -> httpx.Client                                   # the client the platform layer takes
    def respond(self, request: httpx.Request) -> httpx.Response

def make_federation(*, clock: Callable[[], float] | None = None, error_shape: str = ERRORS_25_0,
                    visibility_delay_s: float = 0.0) -> FakeData
def write_platform(directory: Path, edit: Callable[[dict], None] | None = None) -> Path
def write_with_placement(directory: Path, rows: str = "", **override) -> Path
def tool_plane_replicates(fake: FakeData, client: OnezoneClient, path: str, target_id: str) -> str
```

**What it may import, and who may import it.** It imports the core, the platform layer and the standard library, never pytest and nothing of a test tree, so a helper that needs pytest, a fixture, is the study's own thin wrapper around a name here. No runtime path imports it: `tests/test_import_boundary.py` fails if any module of `expcore`, any other module of `expcore_onedata`, the CLI or the shipped example imports it, by an import statement, a relative import or a literal dynamic import, and it imports every runtime module in a fresh interpreter and requires that the testing package is not among the loaded modules. The core's own tests take their fakes from here, so a fake cannot drift from the code it stands in for. Not in it: the sandbox's host fake (`FakeHost`), which only the sandbox's tests need, and every pytest fixture.

**The names are the interface.** A name below changes only with an entry in the changelog. Each fake's docstring says what it models and what it does not, in two paragraphs that begin `Models:` and `Does not model:`; a test fails if one lacks either. The table is the short form.

| Name | What it is | Models | Does not model |
|---|---|---|---|
| `MockOnedata`, `Request` | the provider REST surface the fsspec client uses, served over a real local HTTP socket from a fixed file set; `Request` is what it records of each call | lookup, attributes, paged children (a `limit` over the maximum is refused, not clamped), ranged content, the one token it accepts | writes, transfers, block maps, file times and permissions, more than one space |
| `FakeFederation` | an in-process Onezone: `client_http()` answers through an `httpx.MockTransport`, no socket | token examination and scope, temporary tokens with a time caveat, privileges (direct and effective), the study space and others added by `add_space`, with `grant(..., space=)` for their privileges | other Onezone calls, a token's expiry or revocation, group-derived privileges, files in an added space |
| `FakeData`, `Node`, `Transfer`, `PROVIDER`, `ROOT_ID`, `ERRORS_25_0`, `ERRORS_LEGACY` | Onezone plus the data API of every provider it reports, with files, block maps, range reads and transfer jobs; time is counted in calls, or in seconds of the fake's own `clock` | files, directories, per-provider block maps that a range read, a replication and an eviction change, per-reader views with a visibility delay, deletes that land late (`delete_delay_s`), replication, eviction and migration jobs, the transfer listing and a job's record, provider clock skew and listing lag, Onedata 25.0's error shapes (or the older one), injected faults | a transfer's duration, partial replication or eviction, QoS, storage backends beyond one, quotas, a token's space or interface caveats, a delete that other providers show late (unless `delete_delay_s` is set) |
| `Ticker` | a clock that moves only when something sleeps: `clock`, `sleep`, `now` | a bounded wait that runs instantly and exactly | real time, threads |
| `make_federation`, `add_users` | the federation the default `platform.toml` describes: the study space, `alpha` supporting it, `beta` known and not supporting it, the three users with the baseline privileges | what `write_platform` names | a federation of another shape: build `FakeData` and bend it |
| `write_platform`, `platform_document`, `write_with_placement`, `with_placement` | the default `platform.toml` as a dictionary and as a file, with the credential files and empty manifests it names, and a `[placement]` table with test settings | a document `load_platform` accepts | a real federation's names: every one is invented (`.test` hosts, `login-*` users) |
| `tool_plane_replicates` | the agent's replication through the tool plane: a job the harness did not schedule, so a probe finds it in the space's listing | the tool server posting the job with its own user's token | the tool server |

The names the default document and the federation share are `ADMIN_ENV` (the variable that holds the admin credential, which a test sets to `fake.admin_token`), `ALPHA_ID` and `BETA_ID` (the providers' ids), `SPACE_NAME` and `SPACE_PREFIX`, `USERS` (login to password and user id), `DATA_PATH_USER`, `TOOL_SERVER_USER` and `SERVICE_USER` (the three principals), `CREDENTIALS_DIR`, `BASELINE` and `MANAGE` (privilege sets), `NOW` and `CEILING_S` (the issuer's clock and a run's ceiling), `SETTINGS` (the `[placement]` table), `DATA` (4096 bytes whose every offset has its own value) and `TOOL_SERVER_ARGS` (the stand-in tool server's arguments after the interpreter).

Two stand-in MCP servers are run, not imported, so importing the package does not load the MCP SDK: `python -m expcore_onedata.testing.fake_mcp_server --tools echo,add` is a real MCP stdio server with a small catalogue and a tool allow-list, and `python -m expcore_onedata.testing.fake_onedata_mcp` is the Onedata tool server's stand-in, configured by environment as the real one is (`TOOL_SERVER_ARGS` is its command line). Neither models the real server's tools.

**Listings.** `FakeData` and `MockOnedata` end a children listing as Onedata 25.0 was measured to (`fake_data.LISTING_END_FACTS`, `deploy/compose-federation/day0/2026-11-12-federation-facts-listing-end.md`): every page has `nextPageToken`, `isLast` and `children`, in that order, with a token on the last page too; `isLast` is true only on a page shorter than its limit, so a page the last entries fill is followed by an empty last page; and the page after a last page answers 400 `posix` `eagain`. `FakeData`'s transfer listing gives a full page a token and the last page a null one, with no `isLast`, so a full page is followed by an empty one. A reader that stops on a missing or null token, or treats an empty last page as an error, fails against them as it would against 25.0.

**Per-reader views.** A file or directory is made on one provider (`add_file`'s holder, or the provider a `POST` of children was sent to), and every other provider shows it only after `visibility_delay_s` seconds of the fake's `clock` (`visibility_delay_by_provider` gives one reader its own delay; the default is none). Until then that reader's reads of it answer as a deleted entry does, and its parent's listing leaves it out. The delay is counted on the fake's clock, which a test binds to a `Ticker`, so a test of a six-second delay takes no time. About 6 s was seen live on Onedata 25.0.

**Pending deletes.** `FakeData.delete_delay_s` (also an argument of `make_federation`) keeps a delete under way for that many seconds of the fake's `clock` after it is answered, so a study can test that its code waits before it uses a name again. The default, 0, finishes every delete at once and everywhere, and nothing changes for a test that does not set it. With a delay, a DELETE of a file or a directory answers 204 and lands when the clock has moved by the delay (a test binds the clock to a `Ticker`, so none waits); `land_deletes()` lands every delete under way now. A delay needs a clock that moves: the fake's default never does, so the first DELETE that would pend with it raises `ValueError` naming `delete_delay_s` and how to bind a clock (`make_federation(clock=ticker.clock)` or `fake.clock = ticker.clock`), rather than let a delete never land and a test of a reused name pass for no reason. The fake answers one provider call at a time, so threads may read it at once, as the core's bulk reads do. `fake.nodes` still holds an entry whose delete is under way until it lands, though `id_of` and `exists` find it gone. `fake_data.DELETE_FACTS` lists where what is recorded was recorded.

| While a delete is under way | |
|---|---|
| Recorded on 25.0 | the provider the DELETE went to answers the entry as a deleted one at once, 400 `posix` `enoent`, with a deleted file's `parent_id` still reading; a provider finishes deleting a directory after the call returns, and a directory made again under the same name before then loses its files when it does (the reason a federation's reset never deletes a directory and makes it again), so the create is accepted and the files made under it go when the delete lands; other providers' directory sizes follow a delete about 5 s late |
| Assumed, for a live check to confirm | (a) the providers the DELETE did not go to keep showing the entry, in reads and listings, and refuse its name (409, as for any taken name), until it lands; a second DELETE through them changes nothing; (b) every provider lands it at once, after the one delay; (c) a file's delete is late in the same way, and a file's name made again loses nothing, where only a directory's was recorded; (d) the late removal takes the files of the directory made again at any depth, and no directory; (e) a listing through the DELETE's provider already leaves out a directory still going (a deleted file was seen gone from one); (f) a provider that still shows the old entry shows one made again beside it once the visibility delay lets it; (g) what is under a directory deleted through a provider answers as deleted there at once too (the facts cited have rows for the deleted file and the deleted directory themselves, none for what is under it); (h) a transfer scheduled through that provider for an entry it answers as deleted is refused as one of an entry that is not there, 400, as with no delay |

The provider the DELETE went to cannot tell a delete under way from one that landed, since it answers both as a deleted entry, so code that waits only until its own reads show the name absent can reuse it too soon. A test of a wait sets the delay, deletes, and reuses the name after that much time (files kept) and at once (files gone), through the provider that deleted.

**Error shapes, by version.** `FakeData.error_shape` selects what the data API answers with when an entry is gone, a write is refused or an upload is untyped:

| | `ERRORS_25_0` (the default) | `ERRORS_LEGACY` |
|---|---|---|
| Belongs to | Onedata 25.0, as measured live (`deploy/compose-federation/day0/`) | none known: the fake's first shape, kept for a study that targets an older platform |
| An entry that is deleted or not visible yet (`missing_answer`, `unseen_answer`) | 400, error `posix`, `errno` `enoent` | 404, error `notFound` |
| A deleted entry's `parent_id` (`keep_deleted_parents`) | still its old parent | gone with the entry |
| The space root before it is there (`root_enoent_answer`) | 400, error `posix`, `errno` `enoent` | 404, error `posix`, `errno` `enoent` |
| A write with a read-only token (`readonly_answer`) | 400, error `posix`, `errno` `eacces` | 403, error `forbidden` |
| An upload (`PUT` of content) not marked `application/octet-stream` (`require_octet_stream`) | 415, error `unsupportedMediaType` | accepted |

The statuses and the `posix` errors of the first column are what was seen. 25.0 was seen to answer 415 to an upload with no Content-Type at all; that the fake refuses every other type too, and the error id of its 415, are its own. Setting `error_shape` sets each attribute named in the table to that column's; a test sets one after it to bend one answer, as the core's tests do to see it fail closed on another 400. A job a provider does not show yet answers `transfer_unseen_answer`, 404 `notFound` in both shapes. The platform layer reads both columns: a 404 or exactly 400 `posix`/`enoent` is absence, 403 or 400 `posix`/`eacces` a refusal, and every upload is marked `application/octet-stream`.

**Transfers carried over.** A job carries the times the platform reports. The fake does not decide which jobs belong to a run's window: `PlatformProbes` does, from those times and the providers' `Date` headers. A job that started before the window and had not ended when it opened is carried over (`space_transfers.carried_over`, and `transfer_carried_over` among the reasons an `on_demand` bound is unknown): it is not one of the run's records and its bytes are not in `transfer_bytes_in`, even if it places them during the run. A job that ended before the window is neither. A stand-in that sums every job it holds counts the first kind and differs from the core there; the tests that pin this are in `tests/onedata/test_testing_module.py`.

## The run record's schema

`expcore.records.schema`. A record names its schema version (`expcore-record/N`), and every section of it is a table of fields in `SECTIONS`. `SCHEMAS` lists the versions; a version names the fields it adds (`added`), and may mark some of those optional (`optional`). A record is validated against its own version: a field of a later version is refused in it, a required field of its version may not be left out, and an optional one may be absent and is checked when it is present.

- **A rule attaches to the version that wrote the record.** What a version's records mean is read from the record's own version, never from the newest code: validation (which fields exist, which are required) and each rule the format does not show, as the first-turn rule of version 2 (`SchemaVersion.first_turn_rule`) is read by `first_turn_rule(record.schema_version)` and the target rule of version 3 (`SchemaVersion.target_rule`) by `target_rule(record.schema_version)`. A later rule never applies retroactively to an older record: a record of version 1 or 2 still validates, writes back to its own bytes and replays under the rule of its time, and a replay of it never writes a field its version lacks.
- **An absent field is absent.** Writing and replaying a record keep an absent optional field absent. It is never filled in as a null or a zero, so "not recorded" and "recorded as empty" stay different (R10).
- **Extending version 3 before v0.1.0.** Live runs write version-3 records while several changes still extend it, so until v0.1.0 is tagged every extension of `expcore-record/3` is strictly additive: a new field is optional and its absence means "not recorded"; no existing field changes its meaning or its validation; and a record written at any earlier merge point replays byte for byte under the newest code. To extend it, add the field to `SECTIONS`, list it in the `added` and the `optional` of the version-3 entry of `SCHEMAS`, write it into the record only when the run supplies it (the builder leaves a `None` out, and `RunRecord.to_json` leaves out a field of the record itself that holds its default), and serve it back from the record on replay, absent when it was absent. Then add the next fixture to `tests/data/record-v3/`, named `NN-topic.json` and written by your code, and leave every earlier fixture as it is: `tests/records/test_v3_fixtures.py` holds the rule in two ways. Structural tests check the schema itself. A `failed_request`, an outcome's `defect` and the malformed events of the totals are objects a fixture may hold none of, and each is a section of `SECTIONS` (`failed_request`, `defect`, `malformed_event`), so its keys are versioned like any other; every field version 3 adds, in every section, is listed in that version's `optional`; and the fields version 3 had at its first merge point, frozen in `tests/data/record-v3-fields.json`, keep their types, their order and whether they are required, while every field not in that file is optional. So a required key added to any section is caught, whether or not the version lists it in `added`, and that file is never edited. The objects outside `SECTIONS` are held otherwise: every record holds a `code` identity and an `agent_config` with its `limits`, so the fixtures catch a required key added to them, and every key of a retry entry may be left out (`RETRY_FIELDS`), so a new one is additive as it stands. And the test validates every fixture and replays it byte for byte under the current code, which catches a change to a field or a check that a fixture holds. A non-additive extension is caught by the two together; the fixtures alone would miss a required field in a section they leave empty. A change that cannot be additive waits for the next version, `expcore-record/4`, which is appended to the table as version 2 and 3 were.
- **The merge points of version 3 so far.** `01-usage-extra.json`: `usage.extra` and `usage.extra_cut` of each turn. `02-billed-unread-reply.json`: the reply-count rule, under which a billed reply that became no turn leaves the totals' usage incomplete. `03-fault-target.json`: the target of a fault, `outcome.infra_target` (the position label an infrastructure failure names, such as `providers[1]`) and `treatment_targets` (the target the condition declares per treatment kind, written sorted by kind), each absent when there is none and refused when present empty or as a null; with them the target rule (`target_rule`), which scores a failure of a declared kind as treatment only when it names the declared target and the fault injector's reading in `instruments.fault_injector` shows that target injected in this attempt (`expcore.records.injections`). A record that declares no target is decided by the kind and the first-turn rule alone, as before. `04-instrument-refusal.json`: `instrument_refusal`, at the top of the record after `treatment_targets`: the instrument whose post-run check refused the attempt, `{"namespace", "infra_kind", "detail"}`, where the kind is in the registry and no condition can declare it as its treatment. It is validated with its keys listed in `REFUSAL_FIELDS`, not as a section; a record of version 1 or 2 that holds it is refused, and one that does not is never given it.
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
| Infrastructure kinds | `endpoint`, `key_rejected`, `sandbox`, `knowledge`, `unattributed_kill`, `ceiling`, `tool_plane_integrity`, `budget_hold`, `post_check_unverified`, plus kinds a platform registers ([below](#infrastructure-kinds-of-a-platform-or-a-study)) and kinds a study declares in `[study.kinds]` | `expcore.errors.CORE_KINDS`, `infra_kinds()`, `untreatable_kinds()` |
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

A study's `study.toml` is validated when it is loaded, before its plugin is imported, so a kind registered by the plugin, or by a platform only the plugin imports, is unknown to the conditions. A study that declares a platform's kind as a treatment lists the module that registers it in `[study] platforms`; the loader imports each listed module before it validates anything, and refuses one that cannot be imported, naming the file, the key, the module and the type of the exception, never its text. For the Onedata layer, that module is `expcore_onedata.sandbox.kinds`, which registers every kind the layer raises: `data_gate_integrity` and the fault injector's `provider_outage` and `injector_failure`. A study without a platform declares its own kinds in `[study.kinds]`, as `name = { description = "...", treatable = true }`. They are held in the vocabulary only while the study is loaded and while the pipeline works on it (`expcore.errors.study_kinds`), so they do not outlast it, and a failed load leaves none behind; a platform's kinds stay for the whole process. The scopes are counted: two studies that overlap in one process each keep their kinds and can see the other's for the overlap, which can only widen validation, and declaring one name differently while they overlap is refused, naming the kind. A study's kind that is a core kind or a platform's is refused; so is a platform registering a kind of the study in scope. Neither key is part of the factor schema (study.toml's hash is in the campaign fingerprint, as before), so a study that uses neither keeps its fingerprint and schema digest. [`study-guide.md`](study-guide.md#treatment-kinds) shows both keys.

A condition that names a kind outside the vocabulary is refused as unknown, with the treatable kinds as the choices; a known kind that is not treatable is refused as one that can never be a treatment, with the reason its registration gave.
