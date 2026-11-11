# Study guide

How to build a study on the core: start it, configure it, declare its design, run and record it, check, score and analyse it, freeze its protocol and ship its replication package, bring in code from an earlier study, and write a platform of your own. The exact interfaces are in [`interfaces.md`](interfaces.md), and the reasons behind them in [`architecture.md`](architecture.md); this guide is the order to use them in. It follows the example study (`examples/tiny_study/`), which the core carries, so every step below runs as written; the last section points at a second example, a platform in a distribution of its own (`examples/sparql_platform/`). `tests/test_study_guide.py` runs every `sh` and `python` block of this file in order, in a new directory, and checks every `toml` block against the example's files. The two lines that make the project and add the core are the exception: the test runs with the core already installed, and `scripts/fresh_repo.py`, which the fresh-repo workflow runs after every push to `main`, runs those two from a pushed commit.

## 1. Start a study

A study is its own git repository that depends on the core by tag and copies nothing from it. Start from the example: make a new repository, add the core, write the example out of the installed core into a directory of its own (`my-study` below; every later command runs from the repository's root), commit it, and change the names and the behaviour while keeping the shape.

```sh
git init --quiet my-project
cd my-project
uv init --bare
uv add git+https://github.com/groundnuty/papers-experiments-core@v0.1.0
uv run python -m expcore example my-study
git add .
git commit --quiet -m "the study, from the example"
```

The commit matters. A run, a replay check and a replication package are stamped with the study's code identity: the commit plus a hash of every file `[study] code` names. Uncommitted changes make the identity dirty, and a dirty identity refuses to start a study run unless you pass `--allow-dirty`, which a pilot may do and a published result should not. A copy without git needs `.code-stamp.json` instead (`expcore.provenance.codestamp.write_stamp`).

The identity covers the study's directory and nothing else. Platform and backend code outside it is part of the campaign's *environment*: the core and every distribution named in `[study] requires` (for example `requires = ["toy-platform"]`, names only, no versions), with everything they require, each with its version and, for an install from a URL, where it came from. A package the plugin imports and `requires` does not name is not recorded, and a reader's check fails on it. The campaign fingerprint holds the environment, so a resume under another one is refused; the replay check refuses to run under anything but the campaign's environment and records the one it ran under, and the analysis refuses a check made under another. A version says nothing about an editable checkout, whose code can change under the same version: bump the version when the code changes, and have a platform report its version in its instrument snapshot or `info()`, as a backend can in its route.

### What you implement

The study directory holds four configuration files and the plugin, the Python file `[study] plugin` names. The core imports the plugin and calls `build(context)`, which returns a `StudyPlugin`:

| Part | What it is | In the example |
|---|---|---|
| `executor` | a `RunExecutor`: given a `RunRequest`, script or configure the backend the request carries, run the loop with `record_run`, write the record to `request.run_dir`, return it | `TinyExecutor` |
| `scorers` | `Scorer`s: a pure function of a record, returning named numeric outcomes | `TinyScorer` |
| `prompts`, `tools` | per condition, the SHA-256 of its prompt template and of its tool set; both go into the campaign fingerprint | from `PromptTemplate` and `tools_sha256` ([what it hashes](interfaces.md#several-providers-and-the-hash-of-a-tool-set)) |
| tools | `ToolProvider`s the executor passes to `record_run`: plain functions through `FunctionProvider`, or an MCP server through `McpToolProvider` | `lookup` and `submit` |
| instruments | `Instrument`s the executor runs around the loop, fresh for every run, since each holds one run's reading and runs can overlap: `start_instruments(instruments, identity)` before `record_run`, `record = finish_instruments(record, instruments)` after, both through `asyncio.to_thread`, then write the record `finish_instruments` returned. `identity` is the request's `RunIdentity` as a plain mapping: `{"run_key": ..., "factors": {...}, "attempt": ...}`. The loop returns a record however the run ended, an infrastructure failure included, so every attempt with a record gets its snapshots. A scorer reads a snapshot at `record.instruments[namespace]`, the only platform state it may read | `FactsServed`, the stub platform's own count of the facts it served, which `TinyScorer` reads as its `served` outcome |
| `health`, `probe`, `lease_group`, `lease_keys` | the platform's health check, resource probe and leases; the Onedata layer supplies real ones | stubs that always succeed |
| `backend_factory` | optional: a `BackendFactory` that builds the backend of every model, for a study with a model backend of its own. Its model entries name that backend in `models.toml` with a plain lower-case `backend` value; the factory serves those and hands every other model to the core's `default_backend_factory`. It carries a `version` attribute that goes into the campaign fingerprint with its name; a factory imported from an installed package must declare the installed version of the distribution that owns its module's file, one defined in a file `[study] code` lists, which the code identity covers, may declare any, and one from anywhere else is refused. Without a factory, such a model is refused when the plugin is loaded | none: the core's factory builds the stub |

The plugin names everything; the core names nothing. What each interface guarantees, and what you must guarantee in return (an executor never builds a backend, a scorer never reads the platform), is in [`interfaces.md`](interfaces.md). So is what an executor may read of the study's configuration, such as a task's attributes (`study.tasks[task_id].attributes`) and the loop's caps (`study.campaign.agent.limits`): see [what an executor reads](interfaces.md#what-an-executor-reads). The example's prompt names each task's `region`, which its executor reads that way. How a template fills its slots is in [PromptTemplate](interfaces.md#prompttemplate): a blank value is refused, braces that enclose no slot name (a JSON example) are sent as they are, `study_run=True` refuses a draft marker, and an inner template is nested by passing its rendered text as a slot's value, which records only the outer template's hash.

A plugin that outgrows one file imports the modules beside it relatively: `from .scoring import Scorer` in `plugin.py` finds `scoring.py` in the same directory. The core imports the plugin as a package of its own directory, under a name made from the plugin's path, so two studies' modules of one name never meet, and `import scoring` finds nothing (the error says to import it relatively). List every such file in `[study] code` beside `plugin.py`, and in `package.toml`. The core goes by where code comes from: it refuses to load a plugin that runs, at its top level or in `build`, a file of the study directory the list leaves out, because the code identity would not cover code that runs, or that runs one other than as a module of its package (through the module search path, or loaded by path), because that module could be served to another study. It compiles the plugin and its modules from their source on every load. It sees code only: a data file the plugin reads by path, like the example's `prompt.txt`, is covered only if you list it.

## 2. Where configuration lives

All configuration is TOML in the study directory, validated when the study is loaded, with every error naming the file and the key; nothing is read lazily.

| File | Holds |
|---|---|
| `study.toml` | the study's name, its plugin, the code identity's scope, the distributions it requires, the platforms it imports and its own infrastructure kinds ([section 3](#treatment-kinds)), the side files of its records ([section 4](#side-files)), the factors, the conditions and the tasks |
| `models.toml` | the single model table: each model once, with backend, API id, sampling, limits and context window, price (flat or in tiers, [below](#prices-and-the-budget)) and status |
| `campaign.toml` | the plan (models, seeds, order seed), the loop's caps, the scheduler's caps and the kinds it records its failures as ([below](#the-schedulers-kinds)), and the budget |
| `preregistration.toml` | the named contrasts, the Holm family (or `exploratory_only = true`, [section 6](#6-what-a-conclusion-may-say)), alpha, the estimators, the bootstrap and the power settings |
| `package.toml` | the inclusion list of the replication package ([section 8](#8-ship-a-replication-package)) |
| `platform.toml` | a platform's own settings, read by the platform layer (`expcore_onedata`), not by the core |

Secrets are never written in configuration: a model names the environment variable or file that holds its key. The hash of every configuration file goes into the campaign's fingerprint, so resuming a campaign after any of them changed is refused.

```python
from expcore.study.config import load_study

study = load_study("my-study")
assert study.name == "tiny-study"
assert sorted(study.config_sha256) == ["campaign.toml", "models.toml", "preregistration.toml", "study.toml"]
statuses = {key: study.models.resolve(key).status for key in study.models}
assert statuses == {"alpha-stub": "frozen", "beta-stub": "frozen"}  # only frozen settings may start a study run
```

### The scheduler's kinds

Two keys of `[scheduler]` in `campaign.toml` name the infrastructure kinds the scheduler records its own failures as:

| Key | Default | The kind of |
|---|---|---|
| `health_kind` | `sandbox` | a failed health check: one before a run closes the gate and is recorded as the outage's attempt, and one after a run turns an attempt that would have been scored into an infrastructure failure of this kind |
| `identity_kind` | `endpoint` | a run whose model endpoint changed its served identity (the model it names or its fingerprint) while the run was going |

Both defaults are kinds of the core, and `sandbox` is the one the Onedata layer's sandbox fails with. A platform of another kind that leaves `health_kind` alone has its outages recorded as failures of a sandbox it does not have: it registers a kind of its own ([section 10](#10-a-platform-of-your-own)) and names it here. Each key must name a kind known when the study is loaded: a core kind, one the modules of `[study] platforms` register, or one of `[study.kinds]` ([section 3](#treatment-kinds)). Any other is refused at load, naming `campaign.toml` and the key and listing the kinds known then. Neither may be a kind a condition declares as its treatment, since a run that the health check stopped, or that the wrong endpoint served, would then be scored as that condition's outcome; the scheduler refuses such a campaign when it starts. The example writes both out with their defaults:

```toml
# my-study/campaign.toml
[scheduler]
health_kind = "sandbox"
identity_kind = "endpoint"
```

```python
import shutil
from pathlib import Path

from expcore.campaign.settings import SchedulerConfig
from expcore.config.loading import ConfigError

assert (SchedulerConfig().health_kind, SchedulerConfig().identity_kind) == ("sandbox", "endpoint")
assert study.campaign.scheduler.health_kind == "sandbox" and study.campaign.scheduler.identity_kind == "endpoint"

misspelt = Path("misspelt-study")
shutil.copytree("my-study", misspelt)
toml = misspelt / "campaign.toml"
toml.write_text(toml.read_text().replace('health_kind = "sandbox"', 'health_kind = "sandbxo"'))
try:
    load_study(misspelt)
    raise AssertionError("a health_kind that is no known kind was accepted")
except ConfigError as error:
    message = str(error)
    assert "campaign.toml" in message and "key 'health_kind'" in message
    assert "did you mean 'sandbox'" in message
shutil.rmtree(misspelt)
```

### A model served by several deployments

The scheduler checks that every reply of a run was served by the identity the model was first served with: its model id and its serving fingerprint (`system_fingerprint`). A run whose replies change either is set aside as `identity_kind` and run again. A load-balanced endpoint can serve one model id from several deployments, each with its own fingerprint, within one run, and then most long runs would be set aside. If the deployments are the same model (the same weights, the same precision), say so in the model's entry of `models.toml` with `accepted_fingerprints`, the fingerprints of those deployments. That decision is the study's; the core only records it and holds the runs to it:

- A reply served under a fingerprint of the set is the model. One under any other fingerprint sets its run aside, as before, and so does a reply served by another model id.
- The admission probe must be served by the model under a fingerprint of the set, or the model is not admitted.
- A declared fingerprint is confirmed by the admission probe, or by the first run whose reply shows it. The probe reaches one deployment, so another is often confirmed only by a run. A fingerprint no reply has shown stays unconfirmed, and the report says so.
- Each attempt records how many turns each fingerprint served. The export's `deployment_shares` and the report's "Deployments" section give, per model, each fingerprint's count and share of the analysed turns and runs, and how many runs more than one deployment served.
- The set is in the campaign's fingerprint, so a resume under another set is refused. A model without the key is checked, recorded and fingerprinted as before.

The key takes a list of fingerprints as your endpoint reports them; the `anthropic` backend records none, so it cannot take the key. The example's stubs report no fingerprint and declare none; a real model's entry would read:

```python
import tempfile

from expcore.config.models import load_models

with tempfile.TemporaryDirectory() as scratch:
    table = Path(scratch) / "models.toml"
    table.write_text(
        """
[models.relay-large]
backend = "openai_compat"
api_id = "vendor/relay-large"
base_url = "https://llm.example.invalid/v1"
key_env = "RELAY_KEY"
status = "frozen"
accepted_fingerprints = ["fp-southern-deploy", "fp-northern-deploy"]
""",
        encoding="utf-8",
    )
    relay = load_models(table).resolve("relay-large")
assert relay.accepted_fingerprints == ("fp-northern-deploy", "fp-southern-deploy")  # a set: kept sorted
assert study.models.resolve("alpha-stub").accepted_fingerprints == ()  # checked against a baseline, as before
```

### Prices and the budget

A model's price is `[models.<key>.price]` in `models.toml`, in US dollars per million tokens: `input_per_mtok` and `output_per_mtok`, and optionally `cache_write_per_mtok` and `cache_read_per_mtok`, which are the input price when left out. A model the study hosts itself says `free = true` instead. With a budget, every planned model that is not free also declares its endpoint's context window, `context_tokens`, for the reason given below. `run` refuses to start or resume the campaign without it, naming `models.toml` and the model's key; checking, analysing, reporting and a package's check of a finished study do not need it.

```toml
# my-study/models.toml
[models.alpha-stub]
context_tokens = 8192

[models.alpha-stub.price]
input_per_mtok = 1.0
output_per_mtok = 2.0
```

A price that depends on the prompt's length is two or more `[[models.<key>.price.tiers]]` tables in place of the flat keys, each with those four prices. Every tier but the last has `up_to_prompt_tokens`, the longest prompt it takes, increasing from tier to tier; the last takes every longer prompt. A request, its reply included, takes the prices of the tier its whole prompt, cached part included, fell in. A run's cost is the sum over its requests, from the usage each turn of its record reports, so the cost the campaign charged is the cost its records give again. A model without tiers is priced from a run's totals.

```python
import tomllib

from expcore.config.models import parse_models

LONG_CONTEXT = """
[models.long-context]
backend = "openai_compat"
api_id = "vendor/long-context-1"
base_url = "https://llm.example.invalid/v1"
key_env = "LONG_CONTEXT_KEY"
status = "frozen"
context_tokens = 200000

[[models.long-context.price.tiers]]
up_to_prompt_tokens = 128000
input_per_mtok = 0.25
output_per_mtok = 1.25
cache_write_per_mtok = 0.3125

[[models.long-context.price.tiers]]
input_per_mtok = 0.5
output_per_mtok = 2.5
cache_write_per_mtok = 0.625
"""
model = parse_models(tomllib.loads(LONG_CONTEXT), "models.toml").resolve("long-context")
assert model.price.for_prompt(128_000).input_per_mtok == 0.25  # at the boundary: the lower tier
assert model.price.request_usd(150_000, 2_000) == (150_000 * 0.5 + 2_000 * 2.5) / 1e6  # past it, reply included
```

`[budget]` of `campaign.toml` sets the ceiling, `max_usd`. Before a run starts, the budget gate reserves the most the run can cost, and the run starts only when what was spent, the reservations of the runs in flight and its own fit under the ceiling. What a run can cost follows from its caps in `[agent]`:

- The token cap, `token_budget` (C below), counts each reply's input and output tokens, the whole prompt with its cached part, summed over the run's requests. The budget is checked after each response and before each request, so a run can exceed it by at most one request: the last request goes out while the run is under its cap, and its prompt and reply then count. That request's prompt is within the context window (W, `context_tokens`), its reply within the reply maximum (R, `max_output_tokens`, or the model's own when lower) where the endpoint holds replies to it, and the run then ends as `token_cap`.
- A run makes at most one request more than its tool rounds, so the requests before the last carry at most O = `max_tool_rounds` x R output tokens.
- Let p_out be the dearest output price of any tier and p_in the dearest input price of any tier, the input or a cache price, whichever is higher.

The reservation of one run is then the most its requests within the cap can cost, plus the most the request past it can. For a model **held to its reply maximum**, the requests within the cap cost at most O x p_out + (C - O) x p_in when p_out is at least p_in and O is at most C, and C x max(p_in, p_out) otherwise, which is never more than the whole cap at the dearest rate; the request past the cap adds W x p_in + R x p_out. For every other model the requests within the cap are reserved the whole cap at the dearest rate, C x max(p_in, p_out), and the request past it W x max(p_in, p_out), since only the window holds its prompt and reply together.

A model is held to its reply maximum only when the campaign has shown it, never from the backend's name. Two things must hold: the core's own factory builds its backend, an OpenAI-compatible or Anthropic adapter that sends R as `max_tokens` in every request (a plugin's `backend_factory` may wrap that adapter or replace it, so a model it builds is never held to R); and with a budget, at each start of the campaign, admission sends the model a request with a `max_tokens` of 16 that asks for a long reply, and the reply comes back cut at that limit, its output count within it. A model the probe does not show is still admitted, with the larger reservation and a `reply_cap_not_shown` event; so is a model whose one-token completion at admission comes back with more than one output token, which is marked at once and not sent the probe. A reply that later reports more output than R takes the model off the smaller bound for the rest of the campaign, a resume included: the reply is a `reply_over_maximum` event, every run of the model is reserved the larger bound from then on, those in flight too, and each later request of a run that was reserved the smaller one goes out only while the campaign is within its ceilings. A resend is cleared again right before it goes out, after the retrier's wait, because the model may have been taken off its maximum during the wait: a top-up reserved by the smaller bound then reserves the rest of the larger one, or the resend is not sent and the attempt is a budget hold.

Two kinds of send are beyond what the token cap counts, and each is **topped up**: a send that failed after it may have been billed (a connection error or a 5xx), which the retrier sends again, and every request after a reply that did not report its input or its output tokens, since the loop counts a missing count as 0 and the cap then no longer bounds the run. A top-up is one request's bound, the same as the request past the cap above. It is reserved against the ceilings before the send goes out, charged at once and never refunded, because what that send cost is unknown; the last failed send of a request, after which nothing is sent again, is charged too. A reply without usage is a `usage_unreported` event. If a top-up does not fit, the send is not made and the attempt ends as a **budget hold**: infrastructure of kind `budget_hold`, set aside and run again when the budget allows, never a treatment, not charged against the run's attempts, and counted per condition and model in the export and the report. The loop's token counting and its `token_cap` ending are not changed. Without a budget nothing is topped up, and no probe of the reply maximum is sent.

Every model's one-token completion at admission must report its input and output tokens, budget or none: the token cap is held from the counts each reply reports, so an endpoint that omits them would be the one model the cap does not bind, and the model is not admitted.

With a budget, admission's probes are paid from it too, at every start of the campaign: each send of the one-token completion and of the probe of the reply maximum, a resend after a failure that may have been billed included, is reserved one request's bound by the window against the ceilings before it goes out and charged in the store at once, and a reply that reports both counts settles it to what they cost at the dearest rates. A send that failed keeps its bound. A probe the budget cannot cover is not sent, and its model is not admitted, its hold saying the budget is spent; a release written for a stopped campaign is taken at the start, before the probes ask for room. The stub sends nothing an endpoint could bill, so its probes are free, and a model without a price is not probed under a budget, which holds it for its unknown cost.

So a run never costs more than its reservation and its top-ups together, as long as the endpoint refuses a request longer than its window and, for a model held to R, applies the `max_tokens` it is sent; a reply found past R is itself outside that guarantee, and every request after it is covered. What was priced by R before that reply was seen falls inside the same limit: a top-up charged by R for a send already made is not raised when the model is taken off R, and an attempt reserved by R whose usage is unknown, an interrupted one among them, is charged that bound, at a restart too, until a reply past R takes its model off it. A stub's scripted usage beyond the model's context window, or a request of a study's own backend that outgrows it, is outside it too. An attempt killed before its usage was recorded, or whose usage is unknown, is charged its reservation, and its top-ups on top. Which bound that is, by the reply maximum or by the window, is written on the attempt's row when it starts, so a later start charges it the same before admission has shown any model held to R, and a start that shows its model held does not charge an attempt reserved by the window less; a model taken off R charges its attempts by the window, whatever their rows say.

For the tiered model above, with a 10M-token cap, 30 rounds and replies of at most 8,000 tokens, the upper tier's prices apply. Held to its reply maximum, the replies before the last carry at most 240,000 output tokens at $2.50 per million, $0.60, and the rest of the cap is input at its dearest rate, the cache write's $0.625, $6.10. The request past the cap adds a 200,000-token prompt at $0.625 and an 8,000-token reply at $2.50, $0.145, which is also what each of its top-ups is charged. The reservation, $6.845, is far below the $25.50 the model is reserved when it is not held: the whole cap and the whole window at $2.50. The example's stubs are never held, and reserve their whole cap and their whole window at the dearest rate:

```python
from expcore.campaign.attempt_bounds import bound_usd, cap_bound_usd, overshoot_usd, request_bound_usd
from expcore.config.models import Limits

limits = Limits(
    max_tool_rounds=30, time_cap_s=600.0, token_budget=10_000_000, output_cap_chars=8000, max_output_tokens=8000
)
assert round(cap_bound_usd(model, limits, reply_capped=True), 6) == 6.70  # 240,000 x 2.50 + 9,760,000 x 0.625
assert round(overshoot_usd(model, limits, reply_capped=True), 6) == 0.145  # 200,000 x 0.625 + 8,000 x 2.50
assert round(bound_usd(model, limits, reply_capped=True), 6) == 6.845
assert round(request_bound_usd(model, limits, reply_capped=True), 6) == 0.145  # one top-up
assert round(bound_usd(model, limits), 6) == 25.5  # not held: (10,000,000 + 200,000) x 2.50, per million

alpha = study.models.resolve("alpha-stub")
assert round(bound_usd(alpha, study.campaign.agent.limits), 6) == 0.056384  # (20,000 + 8,192) x 2, per million
```

### The federation's certificate

On the Onedata platform four clients talk to the federation: the platform layer's REST client, the data gate that carries the agent's REST data path, oneclient in the mount sidecar, and the MCP tool server. All four take their certificate check from one `[tls]` table of `platform.toml`, so no two can check differently. Without the table, every client verifies the federation's certificate against its own default CAs, which is right for a federation with a publicly trusted certificate. A federation whose certificate no default trusts, such as a self-signed one, takes one of two modes:

- **`ca_file`**, preferred: `ca_file = "federation-ca.pem"` names a CA bundle the federation's certificate chains to, relative to `platform.toml`. Verification stays on, against that bundle. The bundle must hold certificates and no private key. No client reads the file you named: its bytes are hashed when `platform.toml` is loaded, read once more for each run and refused unless they have that hash, and every client gets those bytes, the REST client in memory and the gate, the sidecar and the tool server as the run's own read-only copy. A bundle replaced after the load is never trusted; a run started after it is refused until `platform.toml` is loaded again.
- **`insecure_tls`**: `insecure_tls = true` turns verification off for all four clients. It is for disposable test federations only: anyone on the path to the federation can then read and alter the data and the tokens. While it is on, every health reading ends with a warning.

The two keys never go together, and the per-client keys of earlier versions (`insecure_tls` under `[toolserver]`, `tls` under `[sandbox.mount]`) are refused at load. Each run's sandbox metadata records the mode, `verify`, `ca_file` with the bundle's SHA-256, or `insecure`, and never the bundle's path or contents. To make the mode part of the campaign's fingerprint, the plugin passes the platform's half of it, `StudyPlugin(..., platform=config.fingerprint())` with `config` the loaded `platform.toml`; a resume under another mode or another bundle is then refused.

## 3. Declare the design

### Factors and conditions

A study declares its factors; `task`, `model` and `seed` are built in. The identity order says how a run key is composed, and a condition assigns every declared factor.

```toml
# my-study/study.toml
[factors.access]
levels = ["direct", "relay"]

[factors.tools]
levels = ["basic", "extended"]

[identity]
order = ["task", "access", "tools", "model", "seed"]

[conditions.baseline]
access = "direct"
tools = "basic"
```

Conditions need not form a full cross. Contrasts are declared in `preregistration.toml`, either between two named conditions, paired on task, model and seed, or as a factor contrast that varies one factor and pairs on every other. Both are clustered by a task attribute.

```toml
# my-study/preregistration.toml
[contrasts.extended-vs-baseline]
conditions = ["extended", "baseline"]
outcome = "accuracy"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "region"
equivalence_margin = 0.2

[contrasts.tools-effect-on-rounds]
factor = "tools"
levels = ["extended", "basic"]
outcome = "rounds"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "region"
```

### Chain designs

In a chain design an arm runs on a subset of the tasks: the condition lists them with `tasks = [...]`. The plan expands each condition over its own tasks, and a contrast with that arm drops, for that contrast only, the tasks the arm did not run, and names them in the report.

```toml
# my-study/study.toml
[conditions.extended]
access = "direct"
tools = "extended"
tasks = ["lake", "river", "hill"]
```

### Treatment kinds

An infrastructure failure is normally set aside and the run is tried again. A condition whose manipulation is the failure itself, a tool server that goes down for example, declares that kind as its treatment, and a run that ends in it is then scored as that condition's outcome and never run again.

```toml
# my-study/study.toml
[conditions.relay-down]
access = "relay"
tools = "basic"
infrastructure_is_treatment = ["knowledge"]
```

Two rules bound this. A failure before the model's first turn is never treatment, whatever the condition declares: nothing the agent did can have met the manipulation, so the attempt is set aside and run again. And a kind may be non-treatable: `tool_plane_integrity` (the tool server's rights or token could not be verified) and `budget_hold` (the budget stopped the run, [section 2](#prices-and-the-budget)) can never be a treatment, nor can a kind a platform registers with `treatable=False`, and loading a study whose condition declares one is refused, naming the file and the key. The scheduler also refuses a campaign in which a condition declares the kind its health check or its endpoint-identity check reports.

```python
import shutil
from pathlib import Path

from expcore.config.loading import ConfigError
from expcore.errors import untreatable_kinds

assert study.schema.treatment_kinds["relay-down"] == ("knowledge",)
assert study.schema.condition_tasks["extended"] == ("lake", "river", "hill")
assert "tool_plane_integrity" in untreatable_kinds()

tampered = Path("tampered-study")
shutil.copytree("my-study", tampered)
toml = tampered / "study.toml"
toml.write_text(toml.read_text().replace('["knowledge"]', '["tool_plane_integrity"]'))
try:
    load_study(tampered)
    raise AssertionError("a non-treatable kind was accepted")
except ConfigError as error:
    assert "infrastructure_is_treatment" in str(error) and "can never be a treatment" in str(error)
shutil.rmtree(tampered)
```

A condition may name only the kinds known when `study.toml` is loaded, and it is loaded before the plugin is imported. Two keys of `[study]` add kinds first. `platforms = ["module", ...]` names modules imported before anything is validated, so the kinds a platform registers in them with `register_infra_kind` are known; for the Onedata layer the module is `expcore_onedata.sandbox.kinds`. A module that cannot be imported is refused, naming the file and the key. `[study.kinds]` declares the study's own kinds, one line each: `name = { description = "...", treatable = true }`, where `treatable` is true unless it says false. A study's kind that is a core kind or a platform's is refused, and it lasts only while the study is loaded or run. A kind named nowhere is refused as unknown, and the error lists the treatable kinds. The `health_kind` and `identity_kind` of `[scheduler]` are checked against the same vocabulary when the study is loaded ([section 2](#the-schedulers-kinds)).

```python
from expcore.errors import infra_kinds

own = Path("own-kind-study")
shutil.copytree("my-study", own)
toml = own / "study.toml"
declared = '"prompt.txt"]\n\n[study.kinds]\nrelay_restart = { description = "the relay restarted", treatable = true }\n'
text = toml.read_text().replace('"prompt.txt"]\n', declared, 1)
toml.write_text(text.replace('["knowledge"]', '["relay_restart"]'))
assert load_study(own).schema.treatment_kinds["relay-down"] == ("relay_restart",)
assert "relay_restart" not in infra_kinds()  # the kind lasts only while its study is loaded or run
shutil.rmtree(own)
```

## 4. Run, record and check

```sh
uv run python -m expcore run my-study
uv run python -m expcore check my-study/out/runs
```

`run` opens the campaign or resumes it: a killed run is settled and the plan finishes, and it exits 3 if runs are left pending, held by the budget or a stop. While it runs, it says each hold on stderr once as it begins and once as it ends (a unit paused after repeated failures, a closed health gate, a budget), with when dispatch resumes or that it will not by itself. Every attempt writes one record under `my-study/out/runs/<digest>/try<N>/`: the route and the settings actually sent, the prompt and the tool manifest, the agent configuration as resolved, every model turn and tool call with what the model saw and what was raw, every clock reading, the outcome from a closed vocabulary, and the code identity. Secrets are redacted before writing.

An exception from the study's executor that is not an `InfraError` is a harness defect, and so is a crash in our own code that the loop catches (a backend or a tool provider that raises): the attempt is set aside, its run is pending again at the campaign's next start, and its marker, its record and the store keep only the exception's class and a stack hash, since its text can carry a secret. The traceback, redacted as the record is (with the secrets the executor gave `record_run` too), goes to `my-study/out/logs/harness-errors.log`, one entry per defect headed by the run key, the attempt and that stack hash. The directory ignores itself for git and no replication package takes it, and `run` prints the log's path when harness errors stop dispatch.

`check` is the replay check. It loads every record, writes it back, and replays it: the real loop runs again with every model turn, tool result and clock reading served from the record, and must re-derive the transcript, the tool-call log and the outcome byte for byte, with no model and no platform. A record keeps each model reply twice, as the model's raw reply (`native`) and as the loop read it (`neutral`: text, reasoning and tool calls), and replay checks that the backend still reads the first as the second, so an answer edited in either copy fails, naming the turn and the field. It copies the campaign store beside the records and hashes the copy with them. Analysis refuses a batch without a current passing check, so a record edited, added or removed after the check, or code changed since, makes `analyze` and `report` refuse until the check is run again. A campaign with attempts still running is refused unless `--partial`, and every output then says that it is partial and lists what was left out.

`check` reads only the `[study]` table of `study.toml`. A batch recorded entirely with the core's backends, whose infrastructure kinds are the core's or declared in `[study.kinds]`, imports nothing from the study, so checking it runs none of the study's code. Checking a batch can import the platforms it names: a record that names a kind still unknown makes `check` print one line and import the modules in `[study] platforms`, which are installed code the study declares, and not the plugin; a kind still unknown after that fails its record. A record of a backend the core has no parser for needs the parser the study registers, so `check` prints one line saying so and also imports the plugin module: checking such a batch runs the plugin's code, so check it only if you trust the study. It does not call `build`, so it needs no model and no key. A module that cannot be imported is refused, naming the module and the exception's type.

The check is stored beside the records, with the code identity it was made under and the hash of every input:

```python
import json

check = json.loads(Path("my-study/out/runs/replay-check.json").read_text())
assert check["n_ok"] == check["n_records"] == len(check["replayed"]) and not check["failures"]
assert check["code"]["dirty"] is False and "campaign-snapshot.sqlite" in check["inputs"]
```

### Files an agent writes

A file the agent writes during a run (a report, a mask, a table) has no place in the batch unless the study declares it as a side file ([below](#side-files)). Undeclared, the check takes every `.json` file under `out/runs` as a record, so a `.json` file beside a record fails the check at `load` (`unsupported record schema None`); a file of any other kind is not among the check's inputs, so nothing vouches for it; and `analyze` reads the records alone. The scorer is a pure function of the record and of its declared side files, so what it needs of an undeclared file must be in the record.

The supported way is an instrument, as the example's `FactsServed` is. The executor makes it with access to where the agent's files land; its `finish` reads them after the loop and returns what the scorer needs as its snapshot, a JSON object: the fields or counts the scorer reads, `None` with the failure beside it for a file that is missing or cannot be parsed, and the file's SHA-256 if a reader should be able to match the snapshot to the original bytes. The snapshot is in `record.instruments[namespace]`, so it is replayed and hashed with the record and travels into the package with it. The original bytes do not: keep the files outside `out/runs`, and out of the package unless `package.toml` attests each one (the package's checks refuse a file they cannot read, binary for one).

### Side files

A run can write a file beside its record that the analysis reads, a compressed log of the byte ranges a run read for instance, with the file's SHA-256 kept in the record, typically in an instrument's snapshot. Declare each such file in `study.toml`, one `[[study.side_files]]` table per file, with two keys: `pattern`, the file's name as the executor writes it into `request.run_dir` (one literal name, no glob and no directory), and `hash_field`, the record field that holds its SHA-256, as record keys joined by dots (`instruments.extent.side_sha256`). The exact form is in [`interfaces.md`](interfaces.md#side-files-of-a-record). The example declares none, and a study that declares none is checked exactly as before.

`check` then hashes every file of a declared name in the batch into its inputs, never replays one, and pairs each with the one record in its directory: the attempt directory, which holds one record. It refuses, naming the file, a record that names a side file by its hash with no such file beside it, a side file whose bytes do not have the hash its own record names (two side files swapped included), a side file in a directory with no record, left from an earlier campaign or attempt, a side file beside a record that names no hash for it, and a hash that is not 64 lower-case hexadecimal digits. A record with neither the hash nor the file is fine: that run made no side file. The side files are looked for in the batch the check parses, wherever `--study` puts the study. The check records the declaration under `side_files`, and `analyze` and `report` refuse a check made under another, refuse a side file changed, removed or added since the check, and pair each side file with its record again. The declaration is in `study.toml`, whose hash is in the campaign fingerprint, so a campaign resumed under another declaration is refused.

An attempt killed between writing its side file and writing its record leaves a side file with no record, which `check` refuses until you remove it.

A side file may hold at most 64 MiB (`expcore.replay.side_files.MAX_SIDE_FILE_BYTES`), since the analysis reads each one whole into memory to hand it to the scorers ([section 5](#5-score-and-analyse)). `check` fails a larger one at stage `side_file`, and `analyze` refuses one that grew past the cap after the check, having read no more than one byte beyond it. Keep a larger log compressed, or split it into side files of their own names.

### Running one campaign on several hosts

A main study can be too long for one host. Run it as N shards, one driver per host, each with its own copy of the study at the same commit, its own `out/`, store and platform: `python -m expcore run my-study --shard 2/4 --shard-label rack-b` is shard 2 of 4. N is at least 2. The shard is given on the command line only, never in `campaign.toml`, so every shard has the fingerprint of the whole plan, and a campaign run without `--shard` is exactly the campaign it was before shards existed: its store, records, export and report are byte for byte the same.

**The label** names the shard in its export and in a merge. It is 1 to 64 lower-case letters, digits, `.`, `_` or `-`, chosen by you, and defaults to `shard-i`. Never use a host name, an address or anything else that identifies a machine: a replication package is public and may be double-blind, and the label travels with the export. A name for the part of the work (`north`, `rack-b`, `shard-2`) is enough.

**The partition is by pair.** A pair is one task, one model and one seed. Every condition of a pair runs in the same shard, so no contrast's pair is split across hosts, and no host holds half of a comparison. Within a model, the pairs are ranked by a SHA-256 of their text and dealt round the shards from a starting shard the model's own hash picks. Each shard so gets the floor or the ceiling of n/N of every model's pairs, and the shards that get the ceiling differ from model to model, so no shard collects every remainder. Every host computes the same assignment from the same plan, and a run keeps its place in the whole plan and the run order of the seed:

```python
from expcore.campaign.shard import assign

pairs = [(f"task-{n}", model, seed) for n in range(10) for model in ("model-a", "model-b") for seed in (1, 2)]
assignment = assign(pairs, 3)
for model in ("model-a", "model-b"):
    sizes = [sum(1 for pair, shard in assignment.items() if pair[1] == model and shard == i) for i in (1, 2, 3)]
    assert max(sizes) - min(sizes) <= 1  # the floor or the ceiling of 20 / 3, for every model
```

The assignment depends on the whole plan, so the plan of a sharded campaign cannot grow on resume: a resume that adds tasks, models, seeds or conditions is refused, whatever `allow_growth` says. A shard that would get no run (more shards than pairs) is refused at start.

**The shares.** `[scheduler.unit_caps]`, `max_usd` and `per_model_usd` in `campaign.toml` are totals of the whole campaign: a model's concurrent sessions and the money are limits of the study, not of one host. Shard i of N runs on an equal part of each: the remainder of a cap goes to the first shards, so the shares of a cap sum to the total, and dollars are shared in whole micro-dollars, rounded down, so they sum to at most the total. The fingerprint holds the totals; the shares are applied after it is computed, and each is recorded in the store and the export. A total below N is refused at start, naming its key (`[scheduler.unit_caps] alpha-stub is 1, less than the 2 shards`). `workers`, the release step and the release file are the host's own and are not shared.

The shares are fixed, never pooled. A shard whose pairs run longer, or cost more, than the others can hit its own ceiling while another has capacity or budget left over, and nothing moves between them. Uneven shards therefore may leave part of a cap or a budget unused. To lose less, choose N so that each shard gets many pairs per model, and keep a margin in the budget. A resume with other totals is refused: the shares are pinned at the first start, so change the totals only by starting every shard on a new store.

**Declaring an environment identity.** A shard that ran against other data than its siblings must not join their batch unseen. Declare what a shard runs against with `StudyPlugin.environment_identity`, a function that returns names mapped to SHA-256 hex digests: the platform's configuration, the layout of its federation, the verified ingest manifest of each space. The core calls it once when the shard starts, refuses anything but such names and digests, and keeps the result in the store and the export. A resume of the shard against another identity is refused, naming what changed, and a merge refuses shards whose identities differ. Names and digests only, for the same reason as the label. A study without a platform, or one that declares nothing, has an empty identity.

A shard's own `check`, `analyze` and `report` work as for any campaign, over its own runs. Its store pins four facts at the first start, with the order seed: the spec, when the shard first started, the identity and the shares. A resume is refused when it gives another spec, label or rule, when a sharded store is resumed without `--shard` or an unsharded one with it, or when the identity or the shares differ. The export of a shard has one more top-level key, `shard`, holding those facts; its run entries are the ones any export has.

## 5. Score and analyse

```sh
uv run python -m expcore analyze my-study
uv run python -m expcore report my-study
```

`analyze` scores every record anew, from the record and its side files alone, with the scorers the plugin returns, and writes `export.json`, `scores.json` and `analysis.json` under `my-study/out/`. Nothing reads an earlier score back, so `analyze` is also how a study is re-scored after a scorer changes: the scorer is code, so run `check` again first. `report` writes `report.md`: every contrast with its estimate, interval, equivalence margin and interval at 1 - 2 alpha (see section 6), raw and Holm-adjusted p-value and conclusion, the pairs it used and the pairs it is missing by cause, each condition's planned runs and how they ended, the infrastructure failures by condition (every condition, zeros included), the preregistration hash, the code identity, the core version and each scorer's version. `analysis.json` carries the same counts.

The infrastructure failure the example declares as treatment comes after the model's first turn, so it is an analysed attempt, marked as treatment with its kind, and it is scored as its condition's outcome. A scorer is a pure function of the record and of its side files: no model, no platform, no clock, no path. It returns named numeric outcomes, and an outcome it could not compute is `None`, never 0; a `not_determined` verdict leaves every outcome that depends on the unobserved state `None`.

A treatment outcome is what the agent achieved under the manipulation, so a scorer returns values for a treatment attempt: the failure itself (the example's agent gave no answer, so its accuracy is 0) and what the agent did before it (its rounds and lookups). `None` stays reserved for what could not be measured, and it drops the run's pair from every contrast on that outcome: the attempt is scored as treatment and then leaves the comparison it was kept for. The analysis counts these drops per contrast. A confirmatory contrast (one in the Holm family) with any drop is refused, naming the contrast, the count and the fix, unless its preregistration says how treatment attempts enter it:

- `treatment_outcomes = "scored"`: the scorer returns values for them. This is a claim the analysis checks: a confirmatory contrast that still loses a pair to a treatment attempt's `None` is refused. For a rare attempt that genuinely could not be measured, the contrast may preregister `max_unscored_treatment = N` (0 unless set; only on a contrast in the Holm family that declares `"scored"`; it cannot excuse every treatment attempt of the contrast), and the report shows how many pairs that cost;
- `treatment_outcomes = "excluded"`: every pair with a treatment attempt is left out of that contrast, whatever the scorer returned, and the report names the contrast and how many pairs it left out beside its result. A confirmatory contrast this leaves with no pair is refused, since its conclusion would read as no effect detected.

An exploratory contrast reports its drops and is not refused. The example's confirmatory contrast on the treatment condition declares its choice:

```toml
# my-study/preregistration.toml
[contrasts.relay-vs-baseline]
treatment_outcomes = "scored"
```

Here is a scorer applied to the treatment attempt. Its record also holds the snapshot of the example's instrument, taken when the loop returned after the outage, as it is after every run that returns a record:

```python
from expcore.records.record import load_record, record_filename
from expcore.records.rescore import rescore

export = json.loads(Path("my-study/out/export.json").read_text())
treated = [e for e in export["analysed_attempts"] if e["treatment"]]
assert {(e["condition"], e["infra_kind"]) for e in treated} == {("relay-down", "knowledge")}
entry = treated[0]
record = load_record(Path("my-study/out/runs") / entry["run_dir"] / record_filename(entry["run_key"], entry["attempt"]))
assert record.instruments["facts"] == {"ok": True, "served": 1}  # the platform served one fact before the outage
assert record.prompt["user"]["slots"]["REGION"] == study.tasks["lake"].attributes["region"] == "north"  # per task


class LookupCount:
    """How many lookups the agent made, under the declared treatment as well: the lookups before the failure count."""

    name = "lookups"
    version = "1"

    def score(self, record):
        if record.outcome["name"] == "infrastructure" and not record.outcome["treatment"]:
            # set aside and run again: the core never scores it, and nothing the agent did can be judged
            return {"verdict": "not_determined", "outcomes": {"lookups": None}}
        looked = [call for call in record.tool_calls if call["name"] == "lookup"]
        return {"verdict": "fulfilled", "outcomes": {"lookups": float(len(looked))}}


scored = rescore(record, [LookupCount()])
assert scored.scores["lookups"] == {
    "version": "1",
    "result": {"verdict": "fulfilled", "outcomes": {"lookups": 1.0}},  # one lookup, then the tool server went down
}

analysis = json.loads(Path("my-study/out/analysis.json").read_text())
relay = next(c for c in analysis["contrasts"] if c["name"] == "relay-vs-baseline")
assert relay["treatment_outcomes"] == "scored" and relay["pairs_missing"]["treatment_outcome_none"] == 0
assert relay["n_pairs"] == 8  # both treatment attempts stay in their own contrast
```

A measure computed from a side file's content ([section 4](#side-files)) comes from a scorer that declares a `side_files` parameter. It is handed the record's side files by declared pattern, as the bytes the loader read once, hashed and paired with the hash the record names: `analyze`, `report`, `regenerate` and a package's `make check` take them from the bytes they verified against the check, and `rescore_files(directory, scorers, side_files=study.side_files)` reads them beside the record of an attempt directory. The scorer never sees a path, so what it reads is what was verified, and the same batch gives the same scores through each of them. A record with no side file, or a study that declares none, gives an empty mapping. A side file that no longer matches is refused before any scorer runs. A scorer that takes only the record is called as before:

```python
import gzip


class LoggedRanges:
    """A measure from a side file's content: the bytes the analysis verified, never a path."""

    name = "ranges"
    version = "1"

    def score(self, record, side_files):
        data = side_files.get("extent_ranges.json.gz")  # by the pattern study.toml declares
        if data is None:  # this run made no side file
            return {"verdict": "not_determined", "outcomes": {"ranges": None}}
        return {"verdict": "fulfilled", "outcomes": {"ranges": float(len(json.loads(gzip.decompress(data))))}}


scored = rescore(record, [LookupCount(), LoggedRanges()], side_files={})  # the example declares no side file
assert scored.scores["ranges"]["result"]["outcomes"] == {"ranges": None}
assert scored.scores["lookups"]["result"]["outcomes"] == {"lookups": 1.0}
```

### Which tool hash to compare

Two hashes describe a run's tools. `tools_sha256`, which the plugin computes for each condition from the tools it declares (their names, descriptions, schemas and terminal flags), is what the campaign fingerprint holds. The record's `prompt.tools_sha256` holds the other, `ToolBox.manifest_sha256`: the definitions the model was sent, whose descriptions end with the sentence that states the output cap, and the terminal flags. They differ for the same tools, by design. To check that conditions differ only in which tools they expose, compare `tools_sha256` values: the plugin's for each condition, and the one recomputed from a record's `tools`, which the record keeps as declared, before the cap sentence ([two tool hashes](interfaces.md#two-tool-hashes)).

```python
from expcore.plugin import ToolSpec
from expcore.study.plugin import StudyContext, load_plugin, tools_sha256

plugin = load_plugin(StudyContext(study, {}))
exposed = tools_sha256([ToolSpec(**tool) for tool in record.tools])  # the treated run's tools, as declared
assert exposed == plugin.tools["relay-down"] == plugin.tools["baseline"] != plugin.tools["extended"]
assert record.prompt["tools_sha256"] != exposed  # what the model was sent: the cap sentence in each description
```

### How pairs become a contrast's input

What a contrast estimates is decided by how its pairs are combined, in stages of plain means:

1. **Pairs.** A pair is one run of each arm with the same pairing key: task, model and seed for a contrast between two named conditions, and every other factor of the run key as well for a factor contrast. Its difference is A - B on the contrast's outcome. A pair whose outcome is missing on either side is left out and counted by its cause.
2. **Per task and model.** The pairs of one task and one model (its seeds, and a factor contrast's levels of the other factors) are averaged into one value.
3. **Per cluster and model.** The task values of one cluster (the tasks with one value of the contrast's `cluster` attribute) and one model are averaged, each task with equal weight whatever its number of pairs. These are the cells of the estimator's input, `PairedDifferences.matrix`: a row per cluster, a column per model, NaN where a model has no pair in a cluster.
4. **Per cluster.** A cluster's difference is the mean over the models that have a cell in it, each model with equal weight (`cluster_differences`).

`wilcoxon_cluster_bootstrap` tests those cluster differences with a signed-rank test (after snapping float noise and dropping zero differences, which it counts), its estimate is their mean, and its interval resamples whole clusters, one draw for every model. The cluster is the unit of inference: a task with more seeds, or a cluster with more tasks, weighs no more than another, and each model counts the same however many runs it has. The example has two regions, so each of its contrasts has two clusters.

```python
import numpy as np

from expcore.stats.estimators import PairedDifferences

cells = PairedDifferences(("north", "south"), ("alpha-stub", "beta-stub"), np.array([[1.0, 0.0], [0.5, np.nan]]))
assert list(cells.cluster_differences) == [0.5, 0.5]  # south: beta-stub has no pair there, so alpha-stub's alone
assert relay["n_clusters"] == len({task.attributes["region"] for task in study.tasks.values()}) == 2
```

### Unknown platform estimates are data

A study on the Onedata platform reads the run-end platform probes from each record's instruments (`platform_probes`). Per tracked provider they give `gained_bytes` (placement gained: the bytes of the blocks the provider's block map shows at run end that it did not show at run start; what it showed at the start and no longer does is `lost_bytes`, kept apart, so it is not a net change, and it is not bytes on the wire), `transfer_bytes_in` (what the transfers in the run window moved into it), and `on_demand_lower_bound`, which is `None` whenever one of its inputs is not established, with each reason in `unknown` (`transfer_not_ended`, `transfer_carried_over`, `listing_not_settled`, ...).

That missingness is informative. An estimate is unknown exactly when, for example, the agent starts a transfer and its run ends first, which is likelier under conditions that encourage placement, so dropping the unknowns would bias a comparison between conditions. Count them per condition and reason over every attempt, set-aside and excluded ones as well as those analysed, and report the counts beside any analysis of the bound; never drop them silently. `gained_bytes` and `transfer_bytes_in` are missing far less often, when a block map or the transfer listing could not be read or a tracked path named another file at the run's end than at its start, but they too can be missing (the split is then `None`), so count those as well, under `no_split`, whichever measure you analyse; or analyse the bound and state its missingness as a limitation. A study without the platform has no probes, and the count is empty:

```python
from collections import Counter


def unknown_on_demand(export, runs):
    """Count the on-demand estimates that are unknown, by condition, provider and reason, over every attempt's record
    under `runs`, analysed, set aside or excluded. An attempt whose probes give no split at all counts once under the
    reason "no_split"."""
    condition_of = {
        entry["run_key"]: entry["condition"]
        for part in ("analysed_attempts", "exhausted_runs", "unfinished_runs", "excluded_attempts")
        for entry in export.get(part, [])
    }
    counts = Counter()
    for path in sorted(runs.glob("*/try*/*.json")):
        if not path.name.split(".")[0].isalnum() or path.name.count(".") != 2:
            continue  # only record files, named <sha256 of the run key>.<attempt>.json
        record = load_record(path)
        entry = {"condition": condition_of.get(record.run_key, "unknown condition")}
        probes = record.instruments.get("platform_probes")
        if probes is None:  # no platform: nothing to count
            continue
        if probes.get("on_demand") is None:
            counts[entry["condition"], None, "no_split"] += 1
            continue
        for provider, split in probes["on_demand"].items():
            for reason in split["unknown"] or ():
                counts[entry["condition"], provider, reason] += 1
    return counts


counts = unknown_on_demand(export, Path("my-study/out/runs"))
for (condition, provider, reason), n in sorted(counts.items(), key=str):
    print(condition, provider, reason, n)
records = [p for p in Path("my-study/out/runs").glob("*/try*/*.json") if p.name.count(".") == 2]
assert records and all(load_record(p).run_key for p in records)  # every attempt's record is read
assert counts == Counter()  # the example study runs without the platform
```

## 6. What a conclusion may say

Every contrast ends in one of five words, `difference`, `trivial_difference`, `equivalent`, `not_detected` or `untestable`, and never in "no difference". Only `equivalent` may be read as no difference, and only a contrast with an `equivalence_margin` in the preregistration can reach it: the interval at 1 - 2 alpha lies inside plus or minus the margin and the test is not significant. `not_detected` is the absence of evidence, never evidence of absence; without a margin, it is the most a non-significant result can say. Both contrast tables of the report show the margin and the interval at 1 - 2 alpha beside the conclusion, so a reader can check `equivalent` and `trivial_difference` without `analysis.json`.

`untestable` means the contrast used no pair, whatever the cause: every partner run exhausted or unfinished, or scored `None` on an ordinary run. Nothing was tested, so it is not evidence of anything, least of all of no effect, and it has no estimate, p-value or minimum detectable effect. Its row in the report gives the causes of its missing pairs with their counts. In the Holm family it stays a member as p = 1, so the family keeps its size and the other members are adjusted as if it were there; it never rejects. An exploratory contrast can be untestable too.

A contrast outside the preregistered Holm family is exploratory. It is judged by its raw p-value, nothing controls its multiplicity, and every table and report shows it apart, labelled exploratory, never as confirmatory. Only the Holm family carries the study's confirmatory claims.

A preregistration that makes no confirmatory claim, such as a pilot's, says so with `exploratory_only = true` in place of `holm_family`. Every contrast is then exploratory: `analysis.json` has `"exploratory_only": true` and no Holm decision, the report has one table, of exploratory contrasts, and its summary and `analyze`'s terminal output say that no confirmatory claim is made. An empty `holm_family = []` is refused, because it is more likely a family never filled in than a choice; the flag is the way to make that choice. The flag is in the file, so in the preregistration's hash, and a pilot's runs analysed under it never enter a confirmatory test.

A contrast that declares `treatment_outcomes = "excluded"` compares its arms without the runs the outage ended, so its conclusion says nothing about the outage itself, and `not_detected` there is not the absence of an outage effect. The report puts `excluded` and the pairs it left out in the contrast's own row, beside its conclusion.

```python
from expcore.stats.analysis import CONCLUSIONS, conclude

assert CONCLUSIONS == ("difference", "trivial_difference", "equivalent", "not_detected", "untestable")
assert conclude(significant=False, equivalent=True) == "equivalent"  # needs a margin: without one, equivalent is False
assert conclude(significant=False, equivalent=False) == "not_detected"
assert conclude(significant=True, equivalent=True) == "trivial_difference"
assert conclude(significant=False, equivalent=False, untestable=True) == "untestable"  # no pair: nothing was tested

analysis = json.loads(Path("my-study/out/analysis.json").read_text())
labels = {c["name"]: (c["exploratory"], c["equivalence_margin"]) for c in analysis["contrasts"]}
assert labels == {
    "extended-vs-baseline": (False, 0.2),  # in the family, and the only one that could ever read "equivalent"
    "relay-vs-baseline": (False, None),
    "tools-effect-on-rounds": (True, None),  # outside the family: exploratory wherever it is shown
}
report = Path("my-study/out/report.md").read_text()
assert "tools-effect-on-rounds" in report.split("## Exploratory contrasts")[1]
```

## 7. Freeze the protocol

A protocol is frozen when nothing that decides a result can change unnoticed. Before the study's runs:

- Freeze every model: `status = "frozen"` in `models.toml`, with its settings collected. A study run refuses a model that is not frozen.
- Write `preregistration.toml`: the contrasts, the Holm family, the margins, alpha, the estimators and the bootstrap and power settings. Its hash goes into every analysis and the report.
- Commit the study, and pin the core by tag in its dependencies (`uv add ...@v0.1.0`); every record carries the core version.
- Then run. The campaign fingerprint holds the hash of every configuration file, the prompts, the tool manifests and the scorers' versions, and a resume under any change is refused.

```python
from expcore.provenance.codestamp import version

assert version("my-study", study.scope)["dirty"] is False  # the committed study: a clean identity
assert analysis["preregistration"]["preregistration_sha256"] == study.prereg.sha256
assert analysis["code"]["dirty"] is False
```

### A rehearsal on stubs

A dress rehearsal runs the whole plan on stub models before the real campaign: the executor, the tools, the scorers, the check, the analysis and the package, at no cost. The core's stub takes no backend's settings: `base_url`, `key_env`, `key_file`, `sampling`, `extra_body`, `prompt_cache_ttl` and `echo_reasoning_field` are refused on a `stub` entry when `models.toml` is loaded. So a rehearsal cannot run on the frozen model table with only the backend swapped; it runs on a `models.toml` of its own, whose hash is in the campaign fingerprint, and its fingerprint differs from the real campaign's. That is by design: a rehearsal's records can never be resumed into the real campaign or taken for its records.

Rehearse in a copy of the study directory (a branch, or a directory of its own), so the rehearsal's `out/` stays apart: in the same `out/`, the real campaign would be refused as a resume under a changed configuration. In the copy, change `models.toml` alone. Keep every model key, so `campaign.toml`'s plan is unchanged, and `status = "frozen"`; set `backend = "stub"` and drop the settings a stub refuses; keep each model's price, `max_output_tokens` and `context_tokens`, which a stub takes, so the budget gate reserves the bound it will reserve for the real model and holds where it will hold. Keep identical what decides a result: the plan, the prompts, the tools, the scorers and the preregistration. The plugin's prompt and tool hashes and the scorers' versions are then the same in both fingerprints, and what differs comes from the model table alone: its hash and each model's entry.

```python
rehearsal = Path("rehearsal-study")
shutil.copytree("my-study", rehearsal, ignore=shutil.ignore_patterns("out"))
models = rehearsal / "models.toml"
entry = '[models.alpha-stub]\nbackend = "stub"\nstatus = "frozen"\n'
# the example already declares a context window, so only the reply cap is added
models.write_text(models.read_text().replace(entry, entry + "max_output_tokens = 256\n"))
assert load_study(rehearsal).config_sha256["models.toml"] != study.config_sha256["models.toml"]  # another campaign
models.write_text(models.read_text().replace(entry, entry + 'prompt_cache_ttl = "5m"\n'))
try:
    load_study(rehearsal)
    raise AssertionError("a stub took a setting of another backend")
except ConfigError as error:
    assert "prompt_cache_ttl applies to anthropic models only" in str(error)
shutil.rmtree(rehearsal)
```

The example's executor runs on stubs only: it requires its backend to have `use(script)` and raises without it. A plugin meant for both the rehearsal and the real campaign scripts its backend only when the backend has `use`, and otherwise leaves the model to answer; the plugin is then the same file in both, so its code identity is the same. A stub given no script replies `done` to every turn.

```python
from expcore.models.adapters import StubAdapter


def script_if_stub(backend, script):
    """Script a backend that takes a script, the rehearsal's stub; a real model's backend has no `use`."""
    use = getattr(backend, "use", None)
    if callable(use):
        use(script)


script_if_stub(StubAdapter([]), ["deep"])  # the rehearsal: the stub plays the script
script_if_stub(object(), ["deep"])  # the real campaign: a backend without `use` is left to answer
```

## 8. Ship a replication package

`package.toml` lists what the package holds; anything it does not list is left out. The example lists its configuration and code, the records with the replay check (`out/runs`), and the four outputs.

```toml
# my-study/package.toml
[package]
name = "tiny-study-package"
```

```sh
uv run python -m expcore package my-study --out dist
tar -xzf dist/tiny-study-package.tar.gz -C dist
make -C dist/tiny-study-package -f Makefile check PYTHON="uv run python"
```

The build refuses a check made under code that is not a clean commit, outputs made by another core version, a check made under another environment than the build's, a file the package's check needs and the list leaves out (every side file the replay check hashed is one), and any path or content that matches a name private everywhere in the core's study-token hashes (`--hashes` takes your own list in place of the core's: see [the study-token hashes](#the-study-token-hashes)). Before it writes the archive it runs the package's own check on the staged files: every file against `SHA256SUMS`, the environment against the check's, then `export.json`, `scores.json`, `analysis.json` and `report.md` derived anew from the records and compared byte for byte. Deriving them goes through the same refusals as `analyze`, so a study's [side files](#side-files) are verified too: one changed, removed or added in an unpacked package fails `make check` even when `SHA256SUMS` was made to agree, and so does one that no longer pairs with its record. That check is what `make -f Makefile check` runs for a reader; naming the Makefile matters, because the package may hold no other file make would read first, and the build refuses one, but a reader's copy could. The archive is deterministic: two builds of one tree are the same bytes.

### What a reviewer runs

A reviewer needs only the package, `make` and Python. `wheels/` holds one wheel per distribution of the environment the records were checked under: the core, what `[study] requires` names, and everything they require. `make -f Makefile check` makes a fresh environment beside the package (`<package>.venv`, or `VENV=...`), installs those wheels from `wheels/` alone, with no index and no network, each pinned by its SHA-256 in `wheels/requirements.txt`, then verifies every file, the environment and the outputs. pip runs isolated from the reader's environment variables and configuration files, so neither can add a requirement or a URL, and without a cache, so it neither reads nor writes the reader's own. The wheels are the ones of the build's platform and Python, so the reviewer needs the same; `make check` says so if the Python differs. `make -f Makefile check-installed PYTHON=...` runs the same check with an interpreter that already holds the environment. The example's package grows by about 65 MB (52 MB of it NumPy and SciPy), and a study on a heavier stack by what that stack weighs.

`PACKAGE.json` records what the package was built from: the study's commit and tree hash, the core version, `environment` (Python, every distribution's version and, for an install from a URL, its source, which is how a reader learns where the core came from), `wheels` (each with its SHA-256, how deep its text was read, the members that are not text with their hashes, and, for a wheel you supplied, the members there was nothing installed to compare with), `study_tokens_exempt` (the wheels the study-token list was not applied to, each with the hash your `uv.lock` records for it), `uncommitted` and, for a study that declares side files, `side_files`, the declaration, which `make check` compares with the package's `study.toml`.

The wheels are made at package time. A distribution is packed again from its installed files, each checked against its `RECORD`. An editable install cannot be, since its installed files only point at a checkout: build its wheel (`uv build --wheel --out-dir DIR`) and pass `--wheels DIR`. A wheel given there goes in byte for byte once its code is shown to be what is installed. For an installed distribution, each member must equal the installed file its `RECORD` names, byte for byte (the hash `RECORD` records is not trusted, since an installed file can be edited after the install), and every installed file must be in the wheel. For an editable install, each member must equal the checkout's file it was built from, looked for first where the install imports from: below a directory its `.pth` puts on the path, then where its top-level package imports from (which finds a src layout behind an import hook). Only a member found in neither is looked for elsewhere, so a stale copy in the checkout never stands in for what is imported: at the project root when the `.pth` names no directory, then through the wheel mapping your `pyproject.toml` declares (hatch's `sources` and `force-include`; with none declared, the member's path below the project with its top-level package stripped). Every Python file the install imports, directory links followed, must be in the wheel, a Python member found nowhere is refused, and a data member found nowhere goes in, listed in `PACKAGE.json`. A wheel of older code under the same version is refused, naming the member. An editable checkout is also part of the environment by a digest of its Python source, so editing it changes the environment like a new version would.

Every wheel is opened, whatever it was installed from. Each text member (Python source, `METADATA`, `RECORD`, entry points, licences) is read with the study-token list, and in an anonymised build with the terms of the anonymisation list and the identity it must not hold; a member that is not text (a compiled extension, a scientific library's test data) is attested by its hash and named in `PACKAGE.json`. Your own code (the core, a wheel from a URL or a directory, a wheel you supplied) is read with every view the checks use for any file; a release from an index is read as it is, line by line, since its text runs to tens of megabytes.

A public library's code can hold a word that the study-token list also holds, so that list applies by provenance. A wheel is a public release, read without the study-token list, only when its SHA-256 equals a hash your `uv.lock` records for a wheel of that name and version from the pypi.org registry; a matching name and version are not enough. The lock is the nearest `uv.lock` from the study directory up to the top of its repository (only the study's own directory for a study outside a repository); with none, one that cannot be read (the build warns, naming it, and refuses nothing for it), or a package it does not hold, every wheel gets the list. A wheel the build packs again from installed files never has the published artifact's bytes, so it is never exempt: to exempt a release, put its published wheel (the file whose URL and hash the lock records) in the `--wheels` directory, where it goes in byte for byte once its code is shown to be what is installed. The core's own wheel, and the wheel of anything installed from a URL, a repository or a directory, editable or not, always get the list. The anonymisation list and the identity apply to every wheel, always. Any other member that holds a listed word makes the build name it and print the `[[opaque]]` entry (`path = "wheels/<wheel file>/<member>"` and its SHA-256) that attests it once you have read it.

### A double-blind submission

`--anonymise LIST` takes an anonymisation list, kept outside the study's repository and the output. It names the prose and metadata files to rewrite (`files = [...]`) and each term with its label (`[terms]`). A term is replaced only as a whole word, only in those files: JSON lexically, one string at a time, so every other byte, numbers included, stays as it was; TOML and text as text. YAML cannot be named, because without a YAML parser its rewrite cannot be checked. Nor can `include` put a file at another path in the package: each file goes in at its own path. So leave a YAML file out, or write an anonymised copy of it yourself under another name in the study (`CITATION.anonymous.cff`, say), commit it, and include the copy instead of the original; the term check reads it like any other file. A term found anywhere else, code and records included, fails the build naming the file and the line, never the term, and so does one that an HTML entity, a percent-escape, a backslash escape or a line continuation hid from the rewrite. Code is never rewritten.

The same option gives the package a code identity by tree hash alone, because a commit id against a public repository is a lookup key to its authors, and so is a source URL. Every commit of the study (in the records, the replay check, the code stamp, `analysis.json`, the report and `PACKAGE.json`) becomes the first 40 hexadecimal digits of the tree hash of the same code, which a reader can recompute; `code_identity` in `PACKAGE.json` says so. The environment keeps names and versions and loses every source, so the core's commit and URL go too, and so does an editable checkout's digest; a distribution with a local version label (`1.2+g1a2b3c`) is refused, since a label can spell a commit, and so is a wheel that holds a `direct_url.json`. The provenance header at the top of each vendored module, which names the earlier repository the code was extracted from with its commit, is replaced by one neutral comment line, and the build checks that each rewritten module has the same syntax tree as before. No seal covers a record's code block, so the seals stay valid; the build makes the replay check again over the rewritten records, and the tables must be exactly yours with the commit replaced, so `make check` regenerates them byte for byte. The build holds every record to the bytes the original check hashed before it rewrites anything. The real commits stay in your local campaign. Then every file of the assembled package, the wheels included, is searched for each commit of the study's repository, the records, the environment and the withheld headers, for each source URL and repository remote, and for each repository name the headers held, and a hit fails the build naming the file and the line, never the value.

An anonymised package still shows the distribution name `papers-experiments-core` and the module name `expcore`, in its wheel, its environment and the command its Makefile runs, and the core's code, which the reviewer has to run. If the core is public under your name, those are lookup keys no build removes: check what your venue allows.

### Which build goes where

A paper needs both builds, from one batch:

- **The submission** gets the anonymised package (`--anonymise LIST`), behind an anonymous link that reviewers can open without learning who you are: an anonymous file share, or the venue's own upload of supplementary material.
- **The camera-ready and the archived copy** get the plain build, without `--anonymise`. It keeps the study's commits and the provenance headers of the vendored code, which name the repository and the commit each module was extracted from. The paper's AI-use disclosure and its attribution of reused code rest on those, so the copy that stays with the paper must carry them.

Build both from the same batch and the same replay check: run `check` once, then build the two packages from its result, without running the campaign or the check again in between. The anonymised build holds every record to the bytes that check hashed, so the two packages carry the same results, and the anonymised one differs only where anonymising changes it: tree hashes in place of the commits, an environment without sources, the files its list rewrites and the neutral headers. What the reviewers checked is then what you archive.

### The study-token hashes

The build checks with SHA-256 hashes of names, never the names, so the list can be public and a finding shows only the start of a hash. By default it checks with the core's public list, the names its maintainers' studies keep private. `--hashes FILE` checks with your list instead: it replaces the core's list, it does not add to it, and the build prints how many hashes it checks with and whether they are the core's or yours, never your file's path. To check with both, give one file holding the lines of both; the format is one hash per line, so `cat` joins them.

Make the file from a plain list kept outside the repository, one name per line under `# category: <name>` lines: `uv run python -m expcore.provenance.study_tokens hash PLAIN > FILE`. After its comment lines, which start with `#`, each line of the result reads `<any|whole> <code|all> <sha256>`. The hash is the SHA-256 of `expcore-study-token-v1:` followed by the name normalised: Unicode NFKC, case-folded, surrounding white space removed. `any` finds the name wherever it is embedded, inside a hostname, a path or a longer identifier; `whole` only as an entire identifier, literal or line, which is how a name shorter than four characters is always matched, and a longer one whose plain-list line reads `whole: <name>`. The scope comes from the category: `all` for hosts, addresses, spaces, providers, users and environment variables, and `code` for the names a study chose (conditions, data states, tasks, datasets, models, repositories). A package is checked against the `all` lines alone, since its own condition, dataset and model names belong in it, and a file with no `all` line is refused, since it would check nothing. So is a file with a line in any other format, and a bare digest, as `sha256sum` prints one, is named as such. A line in the right format whose digest was made some other way cannot be told from a correct one, because a digest shows nothing of its input: make the file with the command, never by hand. To see that a list finds what it should, write a listed name into a scratch file and run `uv run python -m expcore.provenance.study_tokens check --hashes FILE SCRATCH`, which prints the count and then the finding.

### What the checks read

What these checks are for: they catch accidental leaks of private names and listed terms, in text and in its common encodings (UTF-16, escapes, entities, base64 and hex runs, compressed and archived content, SQLite databases). They do not defend against someone who sets out to hide a name from them, and they never read text inside images, so a figure is your responsibility. The study-token exemption trusts your own `uv.lock`, so a hash forged there to carry a private name past the check is deliberate concealment, which is outside this threat model. A wheel exempt as a published release is read against the anonymisation list as literal text, without decoding escapes or encodings, so a dependency you published yourselves should not rely on that: supply a wheel you build from its checkout, which the lock does not record and the checks therefore read with every view as your own code, or check its text yourself.

The redaction covers the core's own messages, and not every form a name can take in them. A listed name of more than twelve words, or one that ends in punctuation, is not looked for as a phrase among a message's words, and a name a message splits across a line break is not joined. What a study's own plugin prints while the build or `make check` runs it to regenerate the outputs is the study's code, not a message of the core, and is not redacted: keep a plugin from printing private names. A listed name that is an ordinary word or phrase is hidden wherever a message uses it, and can withhold a whole line, which errs on the safe side.

The token and term checks fail closed: a file goes in only if they can read all of it. That is text (UTF-8 without NUL bytes, or UTF-16 with a byte-order mark), an SQLite database, or a gzip, bzip2, xz, zip or tar archive of such files, opened down to its last member. In text, every base64 or hex run of 16 characters or more, a `data:` URI's included, is decoded and read the same way (a run that only looks encoded, a digest for instance, refuses nothing), and in a database every value is read, each BLOB as a file of its own. Anything else is refused at the first such stream, naming the file and what was found in it: a PDF or PostScript file (even one in plain ASCII), a PNG, a raw zlib stream, unknown binary, an archive member that fails to decompress. No message of the build, a finding, a warning, a hint or a refusal, ever shows a path, a name or a value that holds a private name or a term of the anonymisation list: such a path is named by the start of its SHA-256, `<hidden 1a2b3c4d5e6f>`, the same way by every check, and a line that would still show one is withheld. That holds for anything the build meets, an unreadable file or a wheel member it cannot open included, which refuses the build through the same redaction; `package-check` prints its problems and errors through the core's public list, the only one a reader has, and neither command repeats an argument it does not take. A path is reported once, on its own: the files the build generates, `SHA256SUMS` and `PACKAGE.json`, list the package's paths too, and are read with each exact mention of a reported path masked (a whole manifest field or JSON string, never part of a longer one), so a finding in one of them has a cause of its own. `--show-private-paths` makes the command line print, after the build, the path each hidden name stands for, relative to the study. Pass it only on your own terminal, never where the output is logged or recorded, since that is exactly what the redaction keeps out of a log.

A finding says where and never what: `README.md:12: a private name (study token 3fa9c1d2e8b7)` for a name of the study-token list, `README.md:12: a term of the anonymisation list` for a term, and `<where>: the path holds ...` for a name in a path, reported once for the file. Each occurrence is reported once, whichever of the views below found it, at its line in the file as it is, so a name written plainly on one line and encoded on another is reported at both. `, once decoded` marks a name that only the decoded text shows at that line (escapes, entities and percent-escapes undone, continuations joined), and the line is still the file's, the one a split name starts on. `, in its parsed strings` marks one that only the strings of a JSON or TOML file show, and `, in its raw bytes` one in the bytes of a database or an archive, or in decoded bytes that are not text; neither has a line. Inside a file, `A!B` names B inside A: an archive member (`bundle.zip!notes/a.txt`), a decompressed stream (`!(gzip)`, `!(zlib)`), a database's schema (`!(schema)`) or the text of a table's column (`store.sqlite!runs.host`, with `#N` for the BLOB of row N), or an encoded run. `notes.txt!(base64@N):L` is the base64 run that starts N characters into the text, counted from 0, decoded, with the name on line L of what it decodes to; N counts in the text as it is when the run shows there, and otherwise in the first form that shows it, the decoded text or the parsed strings; the same payload encoded at two places is reported for each run. `!(hex@N)` is the same for a hex run. A file you have inspected by hand, a figure for instance, can go in unscanned if `package.toml` attests that exact file by its SHA-256 (`sha256sum figures/flow.pdf`):

```toml
# an [[opaque]] entry of package.toml
[[opaque]]
path = "figures/flow.pdf"
sha256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
reason = "a figure drawn by hand; read page by page, no name in it"
```

The build checks the hash, refuses the file if its bytes changed, and lists every attestation in `PACKAGE.json`, so a reader sees which files rest on the author's word rather than on the checks.

### Warnings

The code identity vouches for the code, and the replay check and the regeneration for what is under `out/`. Any other file the package includes (a README, `package.toml`, a figure) has only the commit it came from behind it, so the build warns about each included file no commit holds and lists it in `PACKAGE.json` under `uncommitted`: `untracked` if git does not track it, ignored or not, and `modified` if it differs from HEAD. An anonymised package names no commit, so the build refuses such a file unless you pass `--allow-uncommitted`, and then it is listed like any other. A study outside any repository (a stamped snapshot) has no commit to compare with: the build says so, `vcs` in `PACKAGE.json` is `none`, and nothing is refused.

## 9. Importing code from an earlier study

Code an earlier study wrote can start a module of yours, but its files hold that study's private names, and whatever a commit of a public repository holds stays visible. So code from an earlier study enters only through the sanitising import, which replaces every private name with a neutral stand-in before anything is committed and records where the code came from. Never copy its text by hand.

The replacements are a map, kept with the plain token list outside the repository. It is TOML: `version = 1`, then one `[[entry]]` table per name, with its `token`, its `replacement` and a `category`, one of the token list's: `conditions`, `data-states`, `tasks`, `datasets`, `models`, `repositories`, `hosts`, `addresses`, `spaces`, `providers`, `users`, or `environment-variables` or `env-vars` for environment names (a singular, capitals, `_` or a space are read as the same). Any other category refuses the map when it loads, since it could be a token typed in the wrong field. A token is replaced wherever it is a whole identifier or string literal, and a token of four characters or more also as a component of a longer name, between `_`, `.` and `-`, never inside another word. Each spelling is its own entry: `pebble-ridge` does not cover `pebble_ridge`. A replacement holds no quotes, backslashes, braces, line breaks or edge whitespace and no token of the map, and a replacement whose token appears inside an identifier lands there, so it must itself be an identifier: letters, digits and underscores. No error shows a value of the map, whatever the error: an entry is named by its number and a field by its name, never a token, a replacement or a category, since a replacement can hold its own token in ways no check of the map catches; an unknown key is counted, never shown, and a TOML error is named by its line and column alone. The import and the check below name neither a source repository nor a source path in an error either, and every line they print, an unexpected exception's included, goes through the same redaction as the package build's, with the map's tokens; an argument they do not take is never repeated. With invented names:

```python
from expcore.provenance.sanitise import sanitise_checked
from expcore.provenance.sanitise_map import parse_map

token_map = parse_map(b"""
version = 1

[[entry]]
token = "pebble-ridge"
replacement = "cluster-a"
category = "hosts"

[[entry]]
token = "pebble_ridge"
replacement = "cluster_a"
category = "hosts"
""")
source = 'HOST = "pebble-ridge.example.org"\n\n\ndef pebble_ridge_slots():\n    return 4\n'
assert sanitise_checked(source, token_map.entries) == (
    'HOST = "cluster-a.example.org"\n\n\ndef cluster_a_slots():\n    return 4\n'
)
```

The import reads the file as the earlier study's repository holds it at a commit, never from its working tree, which may be on another branch: `git -C OLD show COMMIT:PATH > BLOB`, then check that `git hash-object BLOB` prints what `git -C OLD rev-parse COMMIT:PATH` does. Then `uv run python -m expcore.provenance.sanitise import --map MAP --source-repo NAME --commit COMMIT --path PATH BLOB DEST` writes the sanitised module to `DEST` with a provenance header and prints the map's SHA-256. It never overwrites a file, and it refuses the import if the token check still finds a mapped name in the result (behind an escape, in a full-width form, glued to other letters): fix the map then, never the module. Commit `DEST` on its own: that is the import commit. Every adaptation, a stand-in turned into a configuration lookup for instance, is a later commit, so the module's history from its import is the whole public transformation. The header names the source repository by name, never by URL, the commit, the path and the git blob id of the file there, and the map by its SHA-256:

```python
# Provenance:
#   source: old-study @ 4b1e2f3a9c0d, harness/slots.py, blob 8e0c2d41b7a95f63e18d0b2c4a6f7e9d1c3b5a70
#   import: sanitised with study-token map 2f6a9c1e8b3d5f70a4c2e9b1d6f8a3c5e7b9d1f2a4c6e8b0d2f4a6c8e0b2d4f6
```

`uv run python -m expcore.provenance.sanitise verify --map MAP --source BLOB --module DEST` sanitises the blob again and says whether `DEST` is still exactly the import; it is, until the first adaptation. After that, anyone who holds the map and the source repositories checks the whole history (P07 in the regression list) with `uv run python -m expcore.provenance.history --map MAP --source-repos "NAME=PATH;NAME=PATH"`, which reads `EXPCORE_SOURCE_REPOS` when `--source-repos` is not given. For each module under `src/` (`--root` names another directory) that carries a source header, it checks that the header's blob is the one the source repository holds at the commit and path the header names, and that the module as its import commit holds it, minus the header, is exactly the sanitisation of that blob under the map. The import commit is the commit that started the module's history: the latest commit that added its path, followed back through a rename only when that commit deleted the earlier path. A copy of an imported module, edited or not, is therefore checked at its own adding commit, never at the import it resembles. A merge that holds the module as none of its parents does, and whose first parent lacks it, is where its history starts, so a module a merge adds, or adds back edited, is checked at the merge; a merge that only brings a branch up to date with main is not, since its file is main's. A merge is compared with each of its parents for a rename, not only the first: when a side branch imported a module and the merge renames it, the history goes on along that side branch, to its import. The command names a module by its path and never its source repository; it prints each module that fails and exits 1, says every module holds and exits 0, and exits 2 when there is no module to check.

## 10. A platform of your own

A platform is what a study runs its agent against: a store, a service, a federation. The core names none. The Onedata layer (`expcore_onedata`) is one, and [`examples/sparql_platform/`](../examples/sparql_platform/) is another, an in-process RDF store built from these documents alone, and a distribution of its own. A platform shipped that way needs five things, each described where it applies:

1. **The distribution, named by the study.** The study lists it in `[study] requires`, by name and without a version ([section 1](#1-start-a-study)). The campaign's environment then records its installed version, and a resume or a replay check under another is refused. An editable checkout can change its code under one version, so bump the version when the code changes, and have the platform report its version in its instrument snapshot or `info()`, as the example's snapshot does for the platform and for its store.
2. **Its infrastructure kinds.** A module of the platform calls `register_infra_kind(kind, description, treatable=...)` at its top level and does nothing else, so that importing it is cheap. The study lists that module in `[study] platforms` ([section 3](#treatment-kinds)), and the kind is then known before the conditions and `campaign.toml` are validated, and when `check` reads a record that names it. The platform raises `InfraError(kind, ...)` for its own failures, from its tools and its instrument, and returns every failure the model should read as a `ToolOutput` with `is_error`.
3. **Its health check, and `health_kind`.** The plugin passes the platform's `HealthCheck` as `StudyPlugin.health`. A failed check is recorded as the `health_kind` of `[scheduler]`, which is `sandbox` unless the study names another, so the study sets `health_kind` to the platform's kind in `campaign.toml` ([section 2](#the-schedulers-kinds)). Left alone, the outage is recorded as the failure of a sandbox the platform does not have.
4. **Its settings, as `StudyPlugin.platform`.** The platform's own settings that make two starts one experiment (the example's row cap and the digest of the graph it loaded), as plain data without a secret. They go into the campaign fingerprint, so a resume under other settings is refused. The Onedata layer's are `config.fingerprint()` ([section 2](#the-federations-certificate)). A platform with none leaves the mapping empty.
5. **Its wheel in the package.** The replication package vendors one wheel per distribution of the environment, the platform's included ([section 8](#8-ship-a-replication-package)). A platform installed in the ordinary way is packed again from its installed files. One installed editable cannot be: build its wheel in its checkout (`uv build --wheel --out-dir DIR`) and pass `--wheels DIR` to `package`. A platform that is not a public release from an index is read for private names like the study's own code, so keep its code and its metadata free of them. A file its code loads when the package's check scores the records again, the example's graph for one, is a file of the study: `[study] code` and `package.toml` list it.

What the platform supplies beyond these is in [`interfaces.md`](interfaces.md): its tools (`FunctionProvider`, or `McpToolProvider` for an MCP server), an `Instrument` whose snapshot a scorer reads, and, for a resource that runs share, a `ResourceProbe` with `lease_group` and `lease_keys`. A platform that shares nothing leaves the last three out. The example's [README](../examples/sparql_platform/README.md) lists its parts, and its `study/` is a small study on it that runs end to end, with its replication package.
