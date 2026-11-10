# Architecture

Decision record for the reusable experiment core (#1), proposed in #2. Each decision gives its reason and points at the inventory row or regression entry it rests on: [`inventory.md`](inventory.md) chooses the source of each component, [`regressions.md`](regressions.md) lists the failure modes the core must keep handled.

Status: **accepted** in the review of #6 (7 Nov), with the review's requests incorporated ([decisions on review](#decisions-on-review)).

## Layers and package layout

Three layers. Dependencies point one way only: a study imports the platform and the core, the platform imports the core, and the core imports neither.

| Layer | Lives in | Contains | Never contains |
|---|---|---|---|
| Generic core | this repository, package `expcore` | agent loop, model backends, tool client, prompts, records and replay, campaign runner, statistics, cause coding, provenance, replication-package tooling, plugin interfaces | a platform's API, any study's names or design |
| Platform layer | this repository, package `expcore_onedata` | Onedata federation configuration, sandbox and data paths, tool-server launch and tokens, provisioning, probes, health, day-0 checks | a study's names or design |
| Study plugin | the study's repository | tasks and instances, factors and conditions, output validation, scorers, instruments, analysis registration, all names | copies of core or platform code |

One distribution, `papers-experiments-core`, ships both packages; the platform's third-party dependencies are an extra (`papers-experiments-core[onedata]`), so a study on another platform installs none of them. Two tests hold the boundary: an import-graph test fails if `expcore` imports `expcore_onedata` or any study, and the study-token test ([below](#the-study-token-list)) fails if a study's name appears in either package. ICSA's core imported study code in 29 places in `icsa/harness/` alone ([inventory](inventory.md#corrections-to-the-premises-in-1-and-2)); the import-graph test makes that impossible.

```
src/expcore/
  plugin.py      Study, Platform, Sandbox, ToolProvider, Scorer, Instrument protocols
  config/        TOML loading and validation; the model table
  models/        backend protocol, OpenAI-compatible, Anthropic, stub; retry; served-model check
  loop/          agent loop, tool box, output capping, outcome vocabulary
  tools/         MCP stdio client, frozen manifest, allow-list
  prompt/        templates with named slots
  records/       record schema, atomic write, read-back, secret redaction
  replay/        offline replay and the replay check
  campaign/      factor schema, manifest, store, scheduler kernel, leases, budgets, export, snapshot
  stats/         paired cluster contrasts, Wilcoxon, Holm, cluster bootstrap, power simulation
  coding/        sampling for cause coding, kappa
  provenance/    code stamp, extraction headers, study-token check
  package/       replication-package build, scan, canaries, checksums, check
src/expcore_onedata/
  federation.py  zones, providers, spaces and the agent's local provider, from configuration
  tokens.py      minting, examination, rights per condition
  sandbox/       containers, data gate, mount sidecar, networks, teardown, kill causes
  toolserver.py  onedata-mcp launch with a frozen allow-list per condition
  provision.py   scoped create and teardown of study data; replicate and evict, each confirmed by the block map
  placement.py   block-map arithmetic: what a provider holds, what is missing
  rangemap.py    the declared per-file, per-provider byte-range map and the [placement] settings
  ranges.py      range placement by reads through the receiving provider
  consistency.py bounded waits: transfers ended, a new space root, two consecutive matching polls
  probes.py      the platform instrument: file distribution, transfer records, bytes per provider pair
  users.py       leased platform users (a slot gives a run one user per token plane) and the token journal
  health.py      pre- and post-run checks against the declared support set
  day0/          registry of checks (reachability, health, tokens, listing, read-only, round trip) and a dated report
examples/        a toy study using only the public interfaces (#5)
```

## Plugin interfaces

A study implements these and registers them in `study.toml`; the platform implements `Platform`. All are `typing.Protocol`s with docstrings, so a test double needs no inheritance.

| Interface | Implemented by | Purpose | Replaces |
|---|---|---|---|
| `Study` | study | `instances()`; `prompt_slots(instance, condition)` returning named slot values; `validate_output(run_dir)` returning an output check; the list of scorers and instruments | ICSA's imports of `icsa.records`, `icsa.instances` and the heatwave parser from the harness (`run.py:82`, `report.py:11-15`) |
| `Platform` | platform | `sandbox(condition)`, `tool_servers(condition)`, `health()`, infrastructure kinds it may raise, probes | ICSA's `real_sandbox.py`, `knowledge/launch.py`, `health.py` called directly by the harness |
| `Sandbox` | platform (local stand-in in core) | `start`, `exec`, `collect`, `stop`, `metadata`; one `ExecResult` type | `sandbox_api.SandboxLike` with its two diverging `ExecResult`s |
| `ToolProvider` | core (MCP), platform or study | tools the agent sees for a condition, with schemas and an executor | ICSA's built-in `python` tool and WWW's in-process SPARQL tools |
| `Scorer` | study | a pure function of a record returning two axes (what the agent did; what the platform's state shows), a verdict of fulfilled, violated or not determined, and named numeric outcomes (for example share of the required extent consumed, F1, bytes moved, tokens); it reads only the record | PPAM's `OracleResult`, ICSOC's `lenses/a3b.py`, ICSA's `ScoredRow` ([inventory row 10](inventory.md#10-scoring-helpers)) |
| `Instrument` | study or platform | hooks at run start and end that write into the record under their own namespace; platform probes (file distribution, the record of every transfer in the run's window, bytes per provider pair) are instruments and write their snapshot at run end. A provider's gained bytes in that snapshot are placement gained, the bytes of the blocks a provider's block map shows at run end that it did not show at run start; what it showed at the start and no longer does is `lost_bytes`, kept apart, so it is not a net change, and it is not bytes on the wire | ICSA's study fields in `RunRecord.extras` and the sandbox metadata the runner parses (`runner.py:163-243`) |

The exact signatures as the code has them, what the core and the implementer each guarantee, and the closed vocabularies are in [`interfaces.md`](interfaces.md).

Scorers read the record and nothing else. In particular, the platform axis of a score comes from the run-end snapshots that platform instruments wrote into the record; no scorer queries the live platform. Re-scoring therefore never needs a model or the platform (R13), and a score computed an hour later equals one computed at run end. An instrument that needs live state records what it read, so replay can serve it.

Outcomes are what the analysis consumes; the verdict alone cannot carry a contrast. An outcome a scorer could not compute is missing (`None`), never zero, and a "not determined" verdict leaves every outcome that depends on the unobserved state missing (R10, S05).

## Run identity: the factor schema

A study declares its factors; the core derives run identity, conditions, pairing and the store schema from them. ICSA's fixed `RunId` (instance × option × layout × model × seed, `records.py:72-81`) reached 47 code and 87 test files ([inventory row 9](inventory.md#9-run-records-and-identity)); studies vary different things.

```toml
# study.toml (example; names are the study's, never the core's)
[factors.data_path]
levels = ["mount", "api", "download"]

[factors.tools]
levels = ["none", "read", "write"]

[factors.data_state]
levels = ["fresh", "arriving"]

[identity]
order = ["task", "data_path", "tools", "data_state", "model", "seed"]

[conditions.mount-read]
data_path = "mount"
tools = "read"
```

- `task`, `model` and `seed` are built in; every other factor is declared.
- A **run key** is the factor values in the declared order, joined canonically; its SHA-256 names files and store rows. Keys are unique (R11) and two conditions never share one (C18).
- A **condition** assigns every declared factor except `task`, `model` and `seed`, and maps to platform settings ([tokens](#tokens-and-permissions)). Conditions that differ in one factor only can run side by side in one campaign.
- **Contrasts** come in two forms, both clustered by a task attribute (for example a region):
  - *factor contrasts* name the factor that varies; pairing is on every other factor;
  - *named-condition contrasts* compare condition A with condition B, paired on task, model and seed. They serve designs that are chains rather than full crosses, such as ICSA's option chain, where adjacent conditions differ in one thing but the set is not a factorial.

  The Holm family is declared over named contrasts in `preregistration.toml`. ICSA's contrasts hard-coded option within layout (`analysis/contrasts.py:70`).
- **The estimator is a preregistration parameter.** Each contrast names its outcome and its estimator (for example a Wilcoxon signed-rank test with a cluster-bootstrap interval). The contrast interface and the records carry no assumption about the estimator, so another one (a permutation test, a mixed model for binary outcomes) can be added later without changing either. ICSA's kernel provides the first estimator.
- The store keeps the run key, the factor values as JSON, and indexed columns generated from the declared factors.

## Records and replay

**Format.** One JSON file per attempt, named by run-key digest and attempt id, written atomically and read back byte for byte (R01). It carries a schema version; unknown versions and fields are refused (R02). It holds:
- identity: run key, factor values, attempt number;
- route: backend, model, base URL without credentials, SDK versions, settings actually sent (B15);
- prompt: template and rendered hashes, slot values, tool manifest hash (L06);
- agent configuration as resolved for this run, not as a hash: turn, wall-time, token and output caps, the whole-time ceiling and the infrastructure kind it ends as (L04), the minimum time to start a tool call (L05), the terminal tools (L09), the malformed-call policy (L01), reasoning echo and other per-model rules, retry policy; replay reads these values from the record, and a table maps each older schema version to the defaults then in force (WWW `replay.py:352-371`);
- code stamp and core version (P01, P02);
- transcript: per turn the neutral message, the native provider message, stop reason, usage (`None` when unreported, R10), served model and fingerprint (B06), retries with their errors, the echoed-request digest (R06);
- tool calls: arguments, raw arguments when malformed (L01), the exact text the model saw, the raw result, status, timings, and two truncation flags kept apart: the harness cap's (L06) and the data source's own row or size limit, as the provider reports it (L07);
- clock readings at every point the loop reads the clock, the boundaries of waits on infrastructure (measured live through the call context, L04) and the loop's looks at a running call included; the agent's time, the waits and `cleanup_s` (harness time spent stopping a cut call, never charged to the agent) add up to the elapsed time;
- outcome from a closed vocabulary, and infrastructure kind when it applies (C02); an infrastructure outcome is flagged as treatment only if its condition declares the kind, the kind is treatable, and the model completed at least one turn, because a failure before the agent's first turn says nothing about the manipulation (C01). That first-turn rule is record schema version 2 (`expcore-record/2`, no field added); a version-1 record is read and replayed under the version-1 rule, the declared kind alone, and an outcome marker of schema 1 is committed under it too, with a `legacy_marker` event;
- instrument outputs, each under its own namespace;
- scores, kept apart and recomputable.

Secrets are redacted before writing (R12). An infrastructure failure writes the partial record (R09).

**What "replayable" guarantees.** Given a record and the code identity it names, replay re-runs the real loop with every model turn, tool result and clock reading served from the record, and re-derives the transcript, the tool-call log, the outcome with its caps, and every verdict the loop computes, byte for byte, with no model call and no platform access. It stops at the first divergence. Scores are re-derived by running the scorers on the record. This is WWW's design (`harness/agent/replay.py`, [inventory row 7](inventory.md#7-recording-transcripts-and-replay)) on the core's format.

Replay does **not** re-execute tool calls: a code cell's output is served, not recomputed. Re-execution of recorded code against a sandbox (ICSA's `inspection/replayrun.py`) is a study instrument.

**The replay check.** Before analysis, every record in a batch is loaded, written back, and replayed; the check is stored with the code identity and a hash of every input. Analysis refuses a batch without a current passing check (R04, R05). Archiving an output without its check fails (R08).

## Configuration

TOML files in the study's repository, validated at load with messages that name the file and key; nothing is read lazily at first use.

| File | Holds | Notes |
|---|---|---|
| `study.toml` | factors, conditions, task source, scorers, instruments | |
| `models.toml` | **the single model table**: key, backend, API id, reported-name aliases, sampling, output limit, reasoning echo field, status | Each model is named once; caps and concurrency refer to these keys (P06). Status `frozen` gates study runs (B08). |
| `campaign.toml` | caps per concurrency unit, attempt limits, budgets and prices, order seed | The campaign's endpoint and URL are the ones execution uses (C14). |
| `platform.toml` | zone, providers, spaces, each agent site's local provider, the expected support set, tool-server settings, the one TLS setting all four clients of the federation take (`[tls]`) | No platform name appears in code (O12); the agent is pinned to its local provider (O03); health checks the declared support set (O04). The TLS mode, with a CA bundle's SHA-256, is in each run's record and the campaign fingerprint (C06). |
| `preregistration.toml` | named contrasts with their outcome, estimator and cluster attribute; the Holm family; alpha, bootstrap replicates, power settings | Its hash goes into every analysis manifest (P04). |

Secrets are referenced by environment variable or file path, never written in configuration, and checked at startup. Every configuration file's hash is recorded in the campaign manifest; resuming with a different hash is refused (C06).

## Tokens and permissions

Two planes per run, each with its own principal and its own token, never shared.

| | Data path | Tool server (knowledge plane) |
|---|---|---|
| Principal | the run's leased platform user (C19), who mints its own token with its own credential | a separate leased platform user per run, distinct from the data-path user, who likewise mints its own token |
| Platform privileges | exactly view and read on the study's space, read back direct and effective, refused otherwise | exactly the privileges the condition declares: view and read, plus named write rights such as QoS management or replication scheduling |
| Token caveats | exactly four: an expiry, read-only, path-scoped to the space, and one interface | the same, but read-only only when the condition declares no write right, so three without it |
| Examination before use | fail-closed: refused unless the caveats are exactly the data-path set (ICSA `tokencheck.py:40-60`) | fail-closed: refused unless the caveats and the principal's effective privileges equal the condition's declared set, no more and no less |
| Further enforcement | data gate allows only read calls; read-only mount and root filesystem | server-side tool allow-list per condition; frozen manifest |

ICSA's six enforcement points ([inventory](inventory.md#read-only-enforcement-in-icsa)) all apply to the data path and stay fail-closed (O05); they are parameterised by plane, so the tool-server plane is checked against its own declared set rather than against "read-only". The MCP server's own token policy is not an enforcement point, because it keeps write tools when examination fails. The interface caveat limits a token to one way of reaching the data, the REST interface or the mounting client, so a token minted for the REST gate is refused when used to mount the space and the other way round; a token leaked from one path is of no use on the other. The agent's code holds no token at all: on the REST path the data gate holds it and adds it to the requests it forwards (dropping any credential the agent sends), and on the mounting path the sidecar owns it, so it is in neither the agent's environment, its files nor its process arguments (O05). The data path takes the interface of the access path the condition uses, and the tool server always takes REST. Both tokens get lifetimes derived from the run's whole-time ceiling, not from the agent's time cap, because the agent's clock pauses during declared waits on infrastructure and the run can last longer than the cap by the length of those waits (O06): the least a token may live is the ceiling plus a margin, the most adds a bounded excess, and a platform that cannot grant that lifetime refuses the run before anything is minted. A token outside that window fails examination. No token is ever revoked, because no earlier code revoked one: each ends with its lifetime. Both principals of a run are the users of one leased slot (`[slots.<name>]`), so one lease key per run serves both planes, and a run that needs neither a data-path token nor a tool server takes no slot. A leased slot is free again only once its sandbox is proven gone and after its tokens' journalled expiry plus a margin; that expiry is fixed and journalled before minting, on the assumption, checked at every mint, that the platform honours the requested time caveat, and a token that breaks it, or whose expiry cannot be read, holds its slot until an operator clears it. Neither value is ever recorded (R12). A refused token whose readable expiry is later than the one it was minted with is the residual risk of that assumption seen happening: it records a `token_expiry_beyond_request` event, through the same event sink as `tool_plane_rights_changed`, with the slot named by its position (`slots[i]`) and both expiries, never the slot's name or the token. The sink is a required argument of the slots' minting and of the sandbox platform. A minted token sits in no environment variable, so redaction cannot find it unless told: the platform layer registers every token it mints with `expcore.records.redact.register_secret` before the token is used. The agent's own data path stays read-only in every condition, including those that grant the tool server write rights. A client cannot hold a principal's privileges fixed between the launch-time check and the end of the run, so the design detects a change instead: the tool-server principal is a dedicated leased user (C19) that only this harness changes, the launch-time check refuses a start on any difference, and at teardown, after the server has stopped, the privileges are read again and the token examined again. A difference from the condition's declared rights records a `tool_plane_rights_changed` event and ends the run as infrastructure of kind `tool_plane_integrity`, so the run is set aside; a plane that cannot be re-verified at teardown, a token that fails its examination at launch or teardown and a privilege read that fails are the same kind, the last three without the event. That kind is not treatable: a study may declare `knowledge` (a server that goes down) as a condition's treatment, but the schema refuses `tool_plane_integrity`, so a tampered run is never scored as the outage.

Tool exposure is declared per condition as an allow-list; the server enforces it, the client checks the server's manifest against a frozen copy per condition, and an extra or missing tool fails the start (L13). Two conditions that differ only in tool exposure, or only in tool-server rights, run side by side in one campaign: each run leases its own principals, so no privilege set is shared. An equality check (ICSA `knowledge/equality.py`) confirms the agent sees exactly the declared difference.

Resources a run mutates or must not share, such as a run's platform user, a writable tool-server token, or a provider whose state the run changes, are held under exclusive leases (C19). Concurrency caps count leases as well as running attempts.

## Versioning and release

- Semantic versioning with tags `vMAJOR.MINOR.PATCH`. While the version is 0.x, a minor release may break interfaces and says so in the changelog.
- Studies depend on a tag: `uv add git+https://github.com/groundnuty/papers-experiments-core@v0.1.0`. Nothing is copied into a study.
- Every record carries the core version and commit, and a study's frozen protocol names the tag it ran on.
- The changelog of each release lists the regression IDs it guarantees, each backed by a passing test.
- A change to the record format bumps the schema version and adds the new fields to the excusal table, so older records still replay (R01).
- No release without the full test suite, the example study end to end from a clean checkout, and the study-token test.

## Provenance of extracted code

Every module that starts from earlier study code begins with a header naming where it came from:

```python
# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef1, icsa/harness/capping.py, blob <source blob id>
#   import: sanitised with study-token map <sha256 of the private map>
#   merged: www-2027-experiments @ 46a5828d, harness/agent/tools.py:121-141
#   changes: see git history of this file from its import commit
```

This repository is public and nothing is ever force-pushed, so whatever a commit contains stays visible. Source files hold private names (for example ICSA's `icsa/harness/slot_users.py:29-34`), so no commit may contain a source file verbatim. Extraction is reproducible through git and a private map instead:

1. **Sanitise privately.** Apply the study-token map, kept with the plain token list outside this repository, to the source file. The map replaces whole identifiers and literal components (the same candidate extractor as the [token check](#the-study-token-list)), never substrings inside other words, which is how ICSA's package broke code ([inventory](inventory.md#icsa-replication-package-versus-the-code-that-ran)).
2. **Import commit.** Commit the sanitised file with the provenance header, naming the source blob (the git blob id of the file at the source commit) and the hash of the map. Constants that sanitising turns into placeholders become configuration lookups in later commits.
3. **Later commits.** Every further change, including merged material, is its own commit, so `git log -p` of the module is the exact public transformation from the import.
4. **Gate every commit, not only the tip.** A pre-push hook and CI run the study-token check and the leak scan over each new commit, because a leak in any commit of a public history is permanent.

A module written fresh says `# Provenance: new`. A test fails if a module under `src/` has neither header. Sources are named by repository and commit, never by URL, so no token-bearing remote is copied. A maintainer-only test, run where the source repositories and the private map are present, checks P07: applying the map to the named source blob reproduces the import commit's file, minus the header. ICSA's anonymised replication package is never a source.

The papers' AI-use disclosure needs the same trail: the earlier harnesses were written by coding agents, and the source commits carry that attribution, so the header links each module to it.

### The study-token list

The plain list of study names (conditions, datasets, models, hosts, spaces, providers, users; categories in the [inventory appendix](inventory.md#appendix-study-tokens)) is kept **outside this repository**, because the repository is public. It lives in the CCGrid study repository, which is private (the team's project, not this repository), as `core-tokens/study_tokens.txt`; the study-token map that sanitises imports ([provenance](#provenance-of-extracted-code)) lives beside it as `core-tokens/sanitise_map.toml`. Both move to the private CCGrid experiments repository once it exists. The study-token test reads the list from the path in `EXPCORE_STUDY_TOKENS` when present, so the location can change without a code change.

The repository commits only `tests/data/study_tokens.sha256`, one hash per line. A study regenerates it with:

```
uv run python -m expcore.provenance.study_tokens hash "$EXPCORE_STUDY_TOKENS" > tests/data/study_tokens.sha256
```

Each token is normalised (Unicode NFKC, case-folded, surrounding whitespace removed) and hashed as SHA-256 of `expcore-study-token-v1:` followed by the token. Generation and checking share one candidate extractor, so a listed token is found wherever it is embedded:
1. Take every identifier, string literal and comment in `src/`.
2. Split each into maximal runs of letters, digits and `_ . -`; this separates URL schemes, paths, ports, queries and assignments.
3. From each run, emit every contiguous sequence of its components (split at `_`, `.` and `-`), re-joined with the separators it had. A hostname inside a URL, a space name inside a path and a name inside a composed identifier all become candidates.

The check hashes every candidate in every tracked file, and in each file's path, and fails on a match; findings print only a hash prefix, and a matching path is redacted. Tokens shorter than four characters match only as a whole string literal or identifier, to avoid false positives. A longer token that is an ordinary word, which would otherwise be found inside unrelated names (in a vendored dependency, say), is marked the same way in the plain list with a `whole: <token>` line; it is normalised and hashed like any other and rendered as `whole <scope> <sha256>`, and where a token is listed both ways the mark wins. Each hash carries a scope taken from the token's category:
- **code** (conditions, data states, tasks, datasets, models, repositories): names a study chose, which code must not hard-code but prose may mention;
- **all** (hosts, addresses, spaces, providers, users, environment variables): private everywhere.

Prose (Markdown anywhere, and everything under `docs/`) is checked against `all` hashes only. `make check` runs the check over the working tree, and CI runs it over every commit of a push or pull request. A missing or empty hash file fails both, so deleting it cannot switch the check off. The hashes keep the names out of casual reading; a determined reader can still confirm a guessed name, so the list must not hold anything secret.

## Out of scope

Any study's own code, study-specific instruments (for example ICSA's analysed-extent tracer), and analyses for a paper ([#1](https://github.com/groundnuty/papers-experiments-core/issues/1)). The inventory lists what is deliberately not extracted.

## Release boundary

v0.1 holds what a study needs to run end to end; the rest follows in v0.2. Each row of the inventory's [decision summary](inventory.md#decision-summary) carries its release.

| v0.1 | v0.2 |
|---|---|
| agent loop, model backends (non-streaming), model table and served-model check, output capping, MCP client and allow-lists, prompts | streaming transport (B17) |
| records, replay and the replay check; secrets hygiene | snapshot cut and fetch (C21) |
| campaign kernel: factor schema, manifest, store and resume, scheduling on one host, leases, budgets, export | two-host execution; operator tooling; confirmed model-level faults (C22) |
| infrastructure versus agent failures | cause coding (sampling, kappa) |
| statistics kernel: named contrasts, the first estimator, Holm, cluster bootstrap, power simulation | further estimators |
| provenance headers, code stamp, study-token test | |
| replication packages: inclusion list, checksums, study-token check, anonymisation lists (whole words, prose and metadata only), deterministic archive, `make check` (needed by #5's release) | replication packages: scanner, canaries |
| platform: federation configuration, tokens on both planes, sandbox and data paths, tool-server launch, provisioning, probes, health, day-0 checks (#4) | fault injection |

## Decisions on review

Settled in the review of #6 (7 Nov):

1. **Malformed tool-call arguments (L01):** report-and-continue is the default: the call is not executed, the model gets the error, and the raw arguments are recorded. Ending the attempt is the declared alternative. Executing with `{}` is not offered at all. Malformed calls are counted per run as a recorded metric.
2. **Error outputs and the cap (L06):** capped like any output, with the error class first; the full raw error stays in the record, with a truncation flag on what the model saw. The cap value is part of the configuration hash, so two conditions in one contrast cannot differ in it unnoticed.
3. **Streaming:** not in v0.1; B17 keeps what a port must preserve.
4. **Plain token list and sanitising map:** in the private CCGrid study repository's `core-tokens/`, moving to the private CCGrid experiments repository once it exists; the test finds them through `EXPCORE_STUDY_TOKENS` ([above](#the-study-token-list)).
5. **Package names:** `expcore` and `expcore_onedata`, one distribution with an `[onedata]` extra.
6. **Requests incorporated:** scorers read the platform axis only from run-end instrument snapshots ([plugin interfaces](#plugin-interfaces)); contrasts between named conditions and the estimator as a preregistration parameter ([factor schema](#run-identity-the-factor-schema)); scorers return named numeric outcomes, missing when not computed; the release boundary above.
