# Inventory of the four harnesses

Which implementation of each component becomes the core, decided from the code of the four earlier studies. Part of #2 (the review gate of #1). Failure modes are cited by their IDs in [`regressions.md`](regressions.md); the design that uses these choices is [`architecture.md`](architecture.md).

## Decision summary

One chosen source per component, or **new** or **drop**. "Merge from" lists what a non-chosen study contributes, each item tied to a regression entry or a named feature. Layer: **core** is the generic package, **platform** the Onedata layer, **study** stays in the study's repository.

| # | Component | Layer | Chosen source | Merge from | Release |
|---|---|---|---|---|---|
| 1 | [Agent loop](#1-agent-loop) | core | ICSA `icsa/harness/loop.py` | WWW `harness/agent/loop.py`: closed outcome vocabulary, injected clock and sleep, harness-defect and skipped-call handling (L08, L09) | v0.1 |
| 2 | [Model backends](#2-model-backends) | core | ICSA `icsa/harness/adapters.py` | WWW `harness/agent/providers.py`: jitter and `Retry-After`, context-overflow table, fingerprint capture, credential stripping (B01, B13, B06, B15); PPAM: vendor extras (B14) | v0.1 (streaming v0.2) |
| 2b | [Model table and served-model check](#2b-model-table-and-served-model-check) | core | ICSA `icsa/servedsettings.py` and `icsa/harness/config.py` (`ModelSpec`) | WWW served-model and fingerprint check (B06); ICSOC admission probe (B07) | v0.1 |
| 3 | [Output capping](#3-output-capping) | core | ICSA `icsa/harness/capping.py` | WWW `harness/agent/tools.py`: cap pinned by the tools hash, raw result kept (L06, L07) | v0.1 |
| 4 | [MCP client and server launch](#4-mcp-client-and-server-launch-with-allow-lists) | core (client, manifest, allow-list); platform (Onedata launch, tokens) | ICSA `icsa/harness/knowledge.py`, `icsa/knowledge/{launch,pin,manifest,tokencheck}.py` | PPAM allow-list test (L13) | v0.1 |
| 5 | [Prompt templating](#5-prompt-templating) | core | ICSA `icsa/harness/prompt.py`, widened to any number of named slots | WWW `harness/agent/prompts.py`: content-addressed prompt blocks with recorded hashes | v0.1 |
| 6 | [Sandbox and data paths](#6-sandbox-and-data-paths) | core (interface); platform (implementation) | ICSA `icsa/sandbox/`, `icsa/harness/sandbox_api.py` | none | v0.1 |
| 7 | [Recording, transcripts, replay](#7-recording-transcripts-and-replay) | core | WWW `harness/agent/{record,replay,replay_check}.py` | ICSA partial transcript on infrastructure failure (R09) and `StubAdapter`; ICSOC validation before write (R02) | v0.1 |
| 8a | [Scheduling and concurrency](#8a-scheduling-and-concurrency) | core | ICSA `icsa/campaign/scheduler.py` (generic kernel only) | WWW seeded order stable under plan growth (C08) | v0.1 (one host; two-host v0.2) |
| 8b | [Store and resume](#8b-store-and-resume) | core | ICSA `icsa/campaign/store.py` | WWW manifest fingerprint refusing a resume into another experiment (C06) | v0.1 |
| 8c | [Budgets and cost gates](#8c-budgets-and-cost-gates) | core | ICSA `icsa/campaign/{attempt_bounds,gate}.py` | WWW per-turn cost estimate (`harness/agent/cost.py`) | v0.1 |
| 8d | [Leases on shared mutable resources](#8d-leases-on-shared-mutable-resources) | core (lease); platform (Onedata slot users) | ICSA `icsa/harness/{slot_lease,reservation}.py` | none | v0.1 |
| 8e | [Manifests and export](#8e-manifests-and-export) | core | ICSA `icsa/campaign/{manifest,export}.py` | WWW input hash over every analysed record (R04) | v0.1 |
| 9 | [Run records and identity](#9-run-records-and-identity) | core | **new**: factor schema | ICSA `RunRecord` generic fields; WWW schema-version discipline (R01, R02) | v0.1 |
| 10 | [Scoring helpers](#10-scoring-helpers) | core (interface, helpers); study (scorers) | **new**: record-only scorer interface: two axes, three-valued verdict, named numeric outcomes | PPAM `OracleResult` axes; ICSOC `lenses/a3b.py` NotDetermined verdict; ICSA `scoring/{parser_version,cost}.py`; WWW scorer digest (P05) | v0.1 |
| 11 | [Cause coding](#11-cause-coding) | core | ICSA `icsa/causes/{kappa,sampling,batch}.py` | none | v0.2 |
| 12 | [Statistics](#12-statistics) | core | ICSA `icsa/analysis/{contrasts,config,sim}.py`, `secondary.py` power simulation | WWW NaN rules, exact multiplicity levels, known-answer test (S05, S06, P05) | v0.1 (first estimator) |
| 13 | [Provenance and code stamping](#13-provenance-and-code-stamping) | core | ICSA `icsa/codestamp.py` | WWW code identity over code and data paths (P02) | v0.1 |
| 14 | [Replication-package tooling](#14-replication-package-tooling) | core | ICSA `tools/artifact/` | ICSOC-pkg `make check` shape (K07); WWW deterministic archives (K06) | v0.1: inclusion list, checksums, `make check`; v0.2: scanner, canaries, anonymisation |
| 15 | [Infrastructure versus agent failures](#15-infrastructure-versus-agent-failures) | core | ICSA `icsa/harness/errors.py`, `icsa/campaign/errors.py` | WWW retry-exhaustion and deadline rules (B02, B03), confirmed model-level faults (C22) | v0.1 (C22 in v0.2) |
| 16 | [Onedata platform layer](#16-onedata-platform-layer) | platform | ICSA `icsa/scripted/{provision,placement,mplus,s2ranges,metrics}.py`, `icsa/harness/{health,real_sandbox,slot_users}.py` | ICSOC verifier and waits (O07, O08, O10, O11); PPAM transfer-visibility wait (O07) | v0.1 |
| 17 | [Day-0 checks](#17-day-0-checks) | platform | ICSA `day0/` and `tools/newhost/check.sh` | none | v0.1 |
| 18 | [Snapshot cut and fetch](#18-snapshot-cut-and-fetch) | core | ICSA-snap `snapshot/{fetch,cut,codecheck,guard,stamp}.py` | none | v0.2 |
| 19 | [Operator tooling](#19-operator-tooling) | core | ICSA `icsa/campaign/{operator_queue,release}.py` | none | v0.2 |
| 20 | [Secrets hygiene in records](#20-secrets-hygiene-in-records) | core | WWW `harness/agent/record.py` redaction | ICSA token values never recorded (R12) | v0.1 |
| — | Claude Agent SDK backends (PPAM, ICSOC) | — | **drop** | The SDK loses the raw trace (ICSA `docs/harness.md:23-24`); ICSA replaced it with the Messages API. | — |
| — | ICSA executor glue `icsa/harness/run.py` | — | **drop** | Replaced by the plugin interfaces; it holds 7 of the core-to-study imports. | — |
| — | ICSA `campaign/boverride.py` (845 lines) | — | **drop** | Exists only to patch hard-coded federation constants in memory (O12). | — |
| — | ICSA `campaign/budget.py`, `progress.py` | — | **drop** | One-campaign plans and alarms; the reusable kernels are in rows 8c and 19. | — |

Not extracted, by design (study instruments or study analyses): ICSA `instruments/`, `inspection/` (blind code inspection; its code-cell re-execution is a study instrument), `faults/`, `dossier/`, `pipeline/`, `oracle_analysis.py`; ICSOC `a4_gate.py`, `a2_judge.py`, `lenses/`; WWW `harness/checks/`, `checkloop/`, `faults/`, `qgen*/`, `baselines/`, `sib.py`; PPAM `scenarios.py` and the scenario oracles. Fault injection (ICSOC `netem.py`, `provider_restart.py`) is a candidate platform instrument after v0.1.

## Sources

Evidence was read from `git archive` snapshots of these commits; the source repositories were not modified, and no remote URL, token or local setting was read.

| Key | Study | Repository | Ref | Dates |
|---|---|---|---|---|
| PPAM | PPAM 2026 | onedata-mcp, `benchmark/` | `ppam2026/14-tools` @ `08e201b` | 1–3 May |
| ICSOC | ICSOC 2026 | icsoc-2026-code-agent | `main` @ `c1e76c2` | 29 Jun–27 Sep |
| ICSOC-pkg | ICSOC 2026 | its replication package | `main` @ `06223f1` | 27 Sep |
| WWW | WWW 2027 | www-2027-experiments | `feat/sib-u-compose` @ `46a5828d` | 18–26 Oct |
| ICSA | ICSA 2027 | icsa-2027-experiments | `feat/37-artifact-clear` @ `6fc798cf` | 30 Oct–6 Nov |
| ICSA-snap | ICSA 2027 | icsa-2027-experiments | `feat/34-final-snapshot-code` @ `debba6d6` | 6 Nov |
| OMCP | — | onedata-mcp | `0cc17ce` | 6 Jul |

Why these refs:
- **WWW:** `main` holds 13 files; the harness is on unmerged branches. `feat/sib-u-compose` holds the newest `replay_check.py` (three review fixes on 24 Oct: 48b81fc5, 9beb5831, 71f8f2e2). `feat/sib-u-union-push6`, dated later, is not a descendant and has the 21 Oct version, 95 lines shorter.
- **ICSA:** the checkout's HEAD (`docs/34-alarm-runbook`, 2 Nov) is stale. `6fc798cf` is the newest ref and the source commit named in the replication package's `BUILD-REPORT.md`. For every generic component it matches the two other final refs, `debba6d6` (the code that cut the final snapshot) and `main` @ `f1c9dd94`; they differ in `inspection/` and `faults/`, and `debba6d6` lacks the 30-line `boverride` wiring in `campaign/cli.py`. `snapshot/` is cited from `debba6d6`.
- **OMCP:** `0cc17ce` contains the newest `onedata_mcp/token_policy.py` (4e815e2) and is the commit ICSA pinned (`protocol/knowledge/pin.json`).

### ICSA replication package versus the code that ran

Do not extract code from a replication package. ICSA's 6 Nov package (`icsa-2027-replication-package`, branch `draft` @ `e0e32728f`) is an anonymised build of `6fc798cf`; its build scanner ran at `349199d`, whose `icsa/` tree is identical. A token-level diff of the two `icsa/` trees found:

- 98 files differ; 9 files exist only in the source (`icsa/audit/`); none exist only in the package.
- No line was added or removed. Every edit replaces a substring with a neutral label (`site_*`, `handle_*`, `author_*`, `PRIOR_STUDY`, `host-N.invalid`, documentation-range addresses); 155 distinct substitutions.
- The renamer matched substrings, not identifiers, so some edits changed behaviour or broke text:
  - `icsa/sandbox/netpool.py`: the default sandbox network pool became an invalid CIDR (host bits set), which `ipaddress.ip_network` rejects in strict mode.
  - `icsa/harness/adapters.py`: the recorded Anthropic endpoint became `https://host-10.invalid`.
  - `icsa/harness/slots.py`: the class `NoSlotAvailable` became `Noauthor_6vailable`.
  - `campaign/credlife.py`, `campaign/config.py`, `inspection/krun.py`, `sandbox/sandbox.py`: dotted names became numeric labels.
  - IP literals in `inspection/canary.py` and `sandbox/netpool.py` changed.
  - Prose: "de-duplication" became "site_f-duplication" (`campaign/scheduler.py`).
- The package has no `tests/` and no `tools/artifact/`.

The package is evidence of what shipped, not of what ran. Provenance headers cite `6fc798cf` (regression P07; build-side guards K04).

## Corrections to the premises in #1 and #2

| Premise | What the code shows |
|---|---|
| Malformed tool-call arguments: ICSOC reports them to the model, ICSA replaces them with `{}` | The reverse. PPAM (`openai_compat.py:224-229`) and ICSOC (`panel.py:380-385`) ran the tool with `{}`; ICSA does not execute the call and tells the model (`adapters.py:102-109`, `loop.py:104-107`; `docs/harness.md:25`). `adapters.py:95` is a timeout helper. See L01. |
| `assert_frozen` is the served-model check | It checks that configuration status labels read `frozen` (`config.py:210-218`, B08). The served-model check is `servedsettings.py` acted on by `campaign/scheduler.py:948-965` (B06). |
| WWW rebuilt retry with backoff on 21 Oct | Retry is in WWW's first agent commit (d1e22ac5, 18 Oct); 21 Oct added the reasoning echo. The served-model check is 19 Oct (model id) and 20 Oct (fingerprint). |
| ICSA's core imports study code in 12 places | 29 in `icsa/harness/` alone (9 of `icsa.records`, 20 of other study modules), plus imports in `knowledge/`, `scoring/`, `causes/` and `analysis/`. `Option`/`Layout` are imported by 47 code and 87 test files. |
| The model table exists twice | Three shapes with three naming schemes: `config/models.toml`, `icsa/campaign/config.py:16-39`, `config/harness.toml:55-76` (P06). |
| The read-only rule is enforced in four places | Six; see [below](#read-only-enforcement-in-icsa). The MCP server's own token policy fails open (OMCP `token_policy.py:65-91`). |
| ICSA had a scripted replay stub | ICSA re-executes recorded code cells with a stub model (`inspection/replayrun.py:402-499`); nothing in ICSA replays model responses. WWW is the only offline replay. |
| `_discover_providers` returns every provider | True (`sandbox.py:492-506`), but campaign configurations never reach it: `route.py:77-79` refuses a configuration without a named provider. Still a hazard for a new study (O03). |
| ICSA's package regenerates tables with `make check` | True of the package repository; `6fc798cf` has no Makefile. Its build-side checks are `tools/artifact/build.py --scan-only`, canaries and `tablecheck.py`. |

Also found: PPAM did not retry 5xx (B01); ICSOC's `run_sweep(trial_k=N)` ran one trial per cell (R11); ICSA's harness and campaign disagree on infrastructure kinds (C02); WWW imports `httpx2` (`providers.py:1630, 1679`) while its `pyproject.toml` declares `httpx`.

## Components

Each table has one row per study. "Handles" lists the regression entries the code already satisfies; "lacks" those it fails.

### 1. Agent loop

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `benchmark/llm_adapters/openai_compat.py:96-261`; `claude_agent_sdk.py:140-271` | 297 + 315 lines; 11 adapter tests | none in the loop; tools called in-process through FastMCP (`openai_compat.py:269`), never over the wire | B04, L11; B12 partly (records `finish_reason` but takes a `length`-cut reply as final, `openai_compat.py:218-220`); lacks L01, B02 | drop |
| ICSOC | `harness/panel.py:217-714` (one loop per leg) | 863 lines; no unit tests (live smoke only) | MCP server name and binary path (`panel.py:69-74, 108`) | L11; lacks B01, B04, B12, L01 | drop |
| WWW | `harness/agent/loop.py`, `looping.py`, `engine_flags.py` | 1,179 + 68 + 59 lines; about 150 tests | SPARQL tool names, frozen scorer import (`loop.py:306-334`), checker phase (`1089-1091`), refusal word list (`194-197`) | L02, L04, L07–L10, B13, B16; ends the attempt on L01 | merge from |
| ICSA | `icsa/harness/loop.py`; `runner.py` (`execute_run` 284-517) | 265 + 517 lines; 75 + 20 tests | `icsa.records` types (`loop.py:17`); a free `python` tool built in (`26-32`); kill categories of ICSA's sandbox (`128-144`); 7 study imports in `runner.py` | L01, L03–L05, L12, B12, R09 | **source** |

ICSA's loop is small, tested and provider-neutral; its study ties are the record types, the built-in `python` tool and the sandbox's kill vocabulary, which become a tool provider and a platform callback. WWW contributes what makes replay possible: an injected clock and sleep (`loop.py:933-936`) and a closed, validated outcome vocabulary (`137-148, 430-451`).

### 2. Model backends

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `openai_compat.py`; `claude_agent_sdk.py` | 297 + 315 lines; 11 tests | upstream provider pin (`benchmark/panel.py:133-150`) | B01 (no 5xx), B04, B14; B12 partly (see row 1) | merge from (B14) |
| ICSOC | `panel.py:249-426` (OpenAI-compatible), `507-714` (Agent SDK) | within 863 lines; no unit tests | model ids (`panel.py:91-106`) | none of B01, B04, B12, B14 | drop |
| WWW | `harness/agent/providers.py` | 1,862 lines; about 150 tests | default model ids, gateway URL and key variables (`1401, 1439-1463`), per-model leak and reasoning rules (`384-390, 521-545`) | B01 (but jitter can undercut `Retry-After`, `providers.py:819-823`), B02, B03, B06 (fingerprint), B10, B13, B15, B17 | merge from |
| ICSA | `icsa/harness/adapters.py` (`ModelAdapter` 74-80, OpenAI-compatible 112-271, Anthropic 274-410, `StubAdapter` 413-461); `slots.py` | 461 + 83 lines; 24 + 10 tests | backend names for the HPC endpoints and the gateway (`adapters.py:116`, `slots.py:21`) | B01 (no jitter), B02–B05, B09–B12 | **source** |

ICSA's pair of adapters behind one protocol, plus a stub, is the base. WWW's transport is more thorough (streaming guards, watchdog), and its retry policy adds jitter and `Retry-After`. Streaming is not ported in v0.1; B17 records what a port must keep.

### 2b. Model table and served-model check

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `benchmark/panel.py` `build_panel` | 240 lines; no tests | every model id hard-coded (`89-92, 134, 229-234`) | none; inactive models found after the fact (`report.py:220-224`) | drop |
| ICSOC | `panel.py` `build_panel`, `verify_leg` (`719-829`); ICSOC-pkg `config/arms.json` | 5 tests (pkg) | model ids in three tables (`scored_sweep_driver.py:181-195`) | B07 | merge from (B07) |
| WWW | `scripts/run_pilot.py` `MODELS` (`692-745`), `ServedModels` (`2175-2283`) | about 110 tests | 12 model entries, per-graph caps | B06 (id, fingerprint, admitted set, mixed fingerprints) | merge from (B06) |
| ICSA | `config/models.toml`; `icsa/harness/config.py` `ModelSpec` (69-103); `icsa/servedsettings.py` | 130 + 234 + 102 lines; 25 tests | seven models across three tables (P06) | B06 (settings and silent defaults), B08 | **source** |

`servedsettings.py` has no imports and judges settings as well as ids. The core keeps one model table in study configuration (P06) and adds WWW's fingerprint rules.

### 3. Output capping

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `openai_compat.py:34, 238` | 8 KiB on the record only; model sees the full text | none | none | drop |
| ICSOC | absent | — | — | — | — |
| WWW | `harness/agent/tools.py:45-303` | 52 tests | row-aware cutting of SPARQL JSON; per-model, per-graph caps | L06, L07 | merge from |
| ICSA | `icsa/harness/capping.py` | 33 lines; 2 tests + loop tests | none (cap value in config) | L06 | **source** |

Row-aware cutting is task-shaped; the core offers it as a hook a study's tool can implement.

### 4. MCP client and server launch with allow-lists

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | in-process FastMCP; `benchmark/tool_allowlist.py`; `_tool_export.py` | 125 + 40 lines; 5 tests | tool names | L13 (allow-list names existing tools) | merge from (test) |
| ICSOC | `panel.py:301-333` (stdio client), `runner.py:43-49, 87-93` (allow-lists) | no tests | tool names | partly L13 | drop |
| WWW | absent (three in-process tools; `tools.py:163-215`) | — | — | — | — |
| ICSA | `icsa/harness/knowledge.py`; `icsa/knowledge/{launch,pin,manifest,tokencheck,equality}.py` | 95 + about 650 lines; about 46 tests | server repository and environment names (`pin.py:4-35`), Onedata token caveats (`tokencheck.py:52-73`), hard-coded hosts (`launch.py:20-22`) | L12–L14, O05, O06 | **source** |

`harness/knowledge.py` is a generic stdio client and goes to the core with the frozen-manifest check. Launching onedata-mcp, its allow-list environment and the token checks go to the platform layer. The equality check (`equality.py`), which sends probes through the real tool box, is a reusable pattern for "conditions differ only in X".

### 5. Prompt templating

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `trial_runner.py:51-57`; `_scenario_specialise.py:40-71` (global `str.replace`) | 9 tests | federation and space names in prompts | none | drop |
| ICSOC | `runner.py:232-328` (`str.format`) | 3 tests | scenario prose | prompt recorded verbatim (`panel.py:197`) | drop |
| WWW | `harness/agent/prompts.py` | 182 lines; about 100 tests | condition names (`108-113`) | content-addressed blocks refused on hash mismatch (`152-173`) | merge from |
| ICSA | `icsa/harness/prompt.py` | 78 lines; 9 tests | `icsa.records` import (`17`), option-to-file map (`22`), instance slots (`52-55`) | missing template and unfilled slot raise (`36-64`); placeholders refused (`run.py:35-40`) | **source** |

ICSA allows exactly one `{ACCESS}` slot (`prompt.py:43-44`); the core generalises it to named slots filled by the study plugin, and records the template and rendered hashes as WWW does.

### 6. Sandbox and data paths

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | absent (artefact directory only) | — | — | — | — |
| ICSOC | isolated config directory and temporary working directory for SDK legs (`panel.py:134-154, 540-553`) | no tests | — | K09 is its open lesson | drop |
| WWW | absent (no shell or file tool; read-only SPARQL) | — | — | — | — |
| ICSA | `icsa/sandbox/` (`sandbox.py` 1,567, `gate.py` 1,050, `gatepolicy.py` 78, `netpool.py`, `teardown.py`, `killcause.py`, …); `icsa/harness/sandbox_api.py` | 6,049 + 96 lines; 639 tests | option table (`gatepolicy.py:26-28`) and option branches (`sandbox.py:344, 417-420, 479-485`); dataset layouts (`config.py:197-198`) | O05, O13–O18 | **source** |

The core keeps only the interface (`SandboxLike`: start, exec, collect, stop, metadata) and a local stand-in for tests. The implementation goes to the platform layer, with the option branches replaced by a per-condition table of data paths (`gatepolicy.decide` already is one). `ExecResult` is defined twice and the copies differ (`sandbox_api.py:25-31` lacks `timed_out`); the core defines it once.

The only basis for the content type of a file's content, which the data gate streams on (`expcore_onedata/sandbox/gateboundary.py`, listed in `UNVERIFIED_ENDPOINTS`), is ICSA's mock of a provider: its content handler answers 200, or 206 for a range, with `application/octet-stream` (`icsa/sandbox/mock_onedata.py:170-195` at 6fc798cf). No source shows a real provider answering so; the live contract test does.

### 7. Recording, transcripts and replay

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `_trial_artefact.py` (JSONL), `rescore.py` | 89 + 447 lines; 19 tests | oracle module names patched in rescoring | R13 | drop (R13 kept) |
| ICSOC | `schema.py` (validate before write), one JSON per trial | 822 lines; 88 tests | arm and scenario enums | R02 | merge from (R02) |
| WWW | `harness/agent/record.py`, `replay.py`, `replay_check.py`; `scripts/verify_batch.py` | 408 + 484 + 918 + 133 lines; about 130 tests | SPARQL result rebuild (`replay.py:192-201`), checker replay (`424-427`) | R01–R08, R12 | **source** |
| ICSA | `runner.py:413-439` (`infra.json`, `transcript.json`, `run_record.json`); `inspection/replayrun.py` | 692 lines (replay run); 34 tests | heatwave-mask comparison | R09, R10 | merge from (R09, `StubAdapter`) |

WWW's replay re-runs the real loop and serves model turns, tool results and clock readings from the record. Porting it needs, per turn, the native provider message, stop reason and usage; per tool call, the exact text the model saw and a raw result rich enough to re-render it; the clock value at every read; retry metadata; prompt and tool hashes; and a table of fields each schema version excuses. The SPARQL-specific parts to replace are `RecordedSparql._rebuild` and `render_query_result`.

### 8a. Scheduling and concurrency

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `benchmark/run_panel.py` | 267 lines; 8 tests | scenario ids; parallelism tied to two providers (`240-246`) | none; L15 open | drop |
| ICSOC | `harness/sweep.py`, `sweep_run.py`, `sweep_driver.py` | 651 lines; 46 tests | fault conditions and provider labels | O10, O11; R11 open | drop |
| WWW | `scripts/run_pilot.py` (`_run_locked` about 4366-5275) | 5,280 lines; about 300 tests | phases, grid spec, model table | C06–C08 | merge from (C08) |
| ICSA | `icsa/campaign/scheduler.py`; `hosts.py`, `remote.py` | 3,130 + 697 + 550 lines; about 540 tests | about 25 in-function study imports; group names; about 15 study holds in `_next_dispatch_inner` (`1304-1522`) | C07, C10, C11, C15–C17 | **source** (kernel) |

The generic kernel is manifest order, caps per key over everything in flight (`1140-1146`), a thread per run, endpoint-unit health and the dispatch lock. The dispatch function is rewritten around a hold registry rather than copied: its 220 lines interleave study holds. Two-host execution (`hosts.py`, `remote.py`) is generic but deferred past v0.1.

### 8b. Store and resume

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | append-only JSONL | — | — | R11 open | drop |
| ICSOC | absent | — | — | — | — |
| WWW | output directory, `runs.jsonl`, exclusive claims (`run_pilot.py:2528-2594`), manifest fingerprint (`1413-1645`) | in 8a | experiment fields | C06, C07 | merge from (C06) |
| ICSA | `icsa/campaign/store.py` (SQLite, WAL, `synchronous=FULL`) | 355 lines; about 80 tests | `runs` columns option, layout, model, seed, instance (`24-28`) | C03, C04, C06 (image pin), C20 | **source** |

The marker-first write and reconcile-on-open protocol is generic; the `runs` table gets the factor schema's key and a JSON column instead of fixed study columns.

### 8c. Budgets and cost gates

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | absent (per-leg `max_tokens`) | — | — | — | — |
| ICSOC | SDK `max_budget_usd` passthrough (`panel.py:608-609`) | — | — | — | drop |
| WWW | `harness/agent/cost.py` (estimate only) | 186 lines; 8 tests | list prices | none; C12 open | merge from (estimate) |
| ICSA | `icsa/campaign/attempt_bounds.py`, `gate.py`; `budget.py`, `progress.py` spend alarm | 104 + 263 lines (kernel); about 178 tests | a gate for one model by design (`gate.py:1-6`); prices | C05, C12 | **source** (kernel) |

### 8d. Leases on shared mutable resources

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | absent; per-model spaces avoid sharing (`run_panel.py:15-24`) | — | — | — | — |
| ICSOC | absent; same-provider fault cells serialised (`sweep.py:150`) | — | — | — | — |
| WWW | heartbeat leases on attempt claims, not on resources (`run_pilot.py:2528-2594`) | — | — | — | — |
| ICSA | `icsa/harness/slot_lease.py`, `reservation.py`; `slot_users.py` (Onedata users) | 561 + 245 + 422 lines; about 115 tests | slot map path, user-name pattern per option, admin secret path (`slot_users.py:29-37`) | C19 | **source** |

The flock lease with quarantine and dead-holder reaping is generic: "one credential or resource per concurrent run, attributable afterwards". Creating Onezone users for it is platform code.

### 8e. Manifests and export

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `benchmark/report.py` | 451 lines; 8 tests | report names | — | drop |
| ICSOC | `harness/aggregate.py` | 322 lines | arm tallies | — | drop |
| WWW | `replay-check.json` with `records_sha256` (`replay_check.py:117-136, 838-849`) | in 7 | — | R04 | merge from |
| ICSA | `icsa/campaign/manifest.py`, `export.py` | 157 + 166 lines; 22 tests | block names and sizes; run-id fields; `region_id` (`export.py:106-116`) | C09, R01 | **source** |

### 9. Run records and identity

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `(run_id, llm, model, scenario, trial_ix)`, `_trial_artefact.py:32-36` | 6 tests | scenario ids | — | drop |
| ICSOC | `trial_id` plus arm and leg fields, `runner.py:528, 627-688` | 88 tests | arm and scenario enums | — | drop |
| WWW | `(question_id, model, repetition)` plus `run_id`; schema versions 1–22 (`record.py:181-185, 307-339`) | 35 + tests | question fields | R01, R02 | merge from |
| ICSA | `RunId` = instance × option × layout × model × seed (`icsa/records.py:72-81`); `RunRecord` (`105-147`) | 265 lines; no dedicated tests | `Option`, `Layout`, `Instance`, `ScoredRow` are study types | R10 | merge from |

**New:** run identity comes from a factor schema the study declares ([`architecture.md`](architecture.md#run-identity-the-factor-schema)). The generic half of ICSA's `RunRecord` (timing, termination, model and endpoint, usage, attempt and infrastructure fields, code stamp) is the base of the core record.

### 10. Scoring helpers

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `benchmark/oracles/`; `OracleResult(mcp_pass, federation_pass, diagnosis)` (`_runtime_types.py:94-116`) | about 1,400 lines; 58 tests | every oracle | oracle bugs crash, agent errors do not (`oracles/__init__.py:16-17`) | merge from (axes) |
| ICSOC | `harness/verifier.py`; `lenses/a3b.py` (Fulfilled, Violated, NotDetermined; agent competence × service evidence) | 553 + 409 lines; about 150 tests | required action name, provider ids | O08; unobserved target is NotDetermined (`a3b.py:89-140`) | merge from (verdict) |
| WWW | `harness/score/` | 1,381 lines; about 290 tests | RDF-term scoring | P05 | merge from (P05) |
| ICSA | `icsa/scoring/` (`parser_version.py`, `cost.py`, `import_errors.py`, `events.py` generic; rest study) | 938 lines; 144 tests | heatwave mask parser and extents | R10, R13 | merge from (helpers) |

**New:** a scorer interface that reads only the record and returns two axes (what the agent did; what the platform's state shows, from run-end instrument snapshots), a three-valued verdict, so an unobservable outcome is never scored as a failure, and named numeric outcomes for the contrasts, missing when not computed.

### 11. Cause coding

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | substring classifiers (`report.py:209-234`, `rescore.py:200-243`) | no tests | model names in labels | — | drop |
| ICSOC | automated attribution (`rq2.py`), LLM judge (`a2_judge.py`) | 27 tests | attribution vocabulary | — | drop |
| WWW | `harness/analysis/audit_sample.py` (deterministic sampling); no agreement statistic | 489 lines; 36 tests | graph names, seed | — | drop |
| ICSA | `icsa/causes/kappa.py`, `sampling.py`, `batch.py` | 70 + 169 + 95 lines; 66 tests | strata option × layout (`sampling.py:41`); codebook and rules | S07, S08 | **source** |

`kappa.py` is generic as is; `sampling.py` and `batch.py` are generic in algorithm and get the factor schema in place of option and layout. The codebook and rules stay with the study.

### 12. Statistics

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | per-cell pass counts (`report.py:86-208`) | 8 tests | — | — | drop |
| ICSOC | rates (`aggregate.py:157-213`); Wilson intervals reported in docs without code in the snapshot | — | — | — | drop |
| WWW | `harness/analysis/{stats,multiplicity,prereg,kat,calibration,simulate}.py` | about 5,600 lines; about 345 tests | six registered claims (`prereg.py:81-116`) | S02, S05, S06, P05; no Wilcoxon | merge from |
| ICSA | `icsa/analysis/contrasts.py`, `config.py` (reads `preregistered.toml`, records its hash), `sim.py`, `secondary.py` (`power_sim` 593-648) | 325 + 57 + 150 lines (kernel); about 230 tests | outcome names and grouping keys (`contrasts.py:30-40, 70`) | S01–S04, S09, P04 | **source** |

ICSA's kernel is exactly what #3 asks for (paired cluster contrasts, Wilcoxon, Holm over a declared family, cluster bootstrap, power simulation from a preregistration file) and becomes generic once outcomes and grouping keys are parameters.

### 13. Provenance and code stamping

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | model id per artefact; `rescore_version` | — | — | — | drop |
| ICSOC | `git rev-parse --short HEAD`, silently `unknown` (`runner.py:373-384`) | — | — | P01 open | drop |
| WWW | `record.py:209-232`; `replay_check.py:52-283` (commit plus tree digest over code and data paths) | 16 + tests | data paths in `CODE_PATHS` | P02 | merge from |
| ICSA | `icsa/codestamp.py` | 111 lines; 14 tests | `ROOTS` and exclusions (`17-19`) | P01, P03 | **source** |

### 14. Replication-package tooling

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | absent | — | — | — | — |
| ICSOC | ICSOC-pkg `Makefile` (check, verify, tables), `regenerate.py`, `checksums.py`, `audit.sh` | 454 + 62 + 55 lines; 7 tests | host allow-list (`audit.sh:39`) | K07 | merge from (K07) |
| WWW | `results/*/*/records.tar.gz` with deterministic layout; `tests/test_real_records_roundtrip.py` | 9 tests | archive names | K06, R08 | merge from (K06) |
| ICSA | `tools/artifact/` (build, inclusion, scan, neutralise, canaries, check_secrets, secret_hashes, tablecheck, verify_tree, …) | 7,090 lines, standard library only; 438 tests | inclusion list, private-list paths (`build.py:48-60`) | K01–K05, K08 | **source** |

ICSA's tooling caught real problems, but its own output (the package above) shows the limit of substring relabelling. The core keeps the scanner, canaries and checks, and runs neutralisation only on non-code files unless the mapping is identifier-aware.

### 15. Infrastructure versus agent failures

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `ADAPTER_ERROR` only when the adapter raises (`trial_runner.py:103-131`) | 2 tests | — | C01 open | drop |
| ICSOC | `schema.is_leg_error` (`schema.py:304-313`) | — | — | C01 | drop (lesson kept) |
| WWW | `harness/agent/errors.py`, `infra.py`, `infra_fault.py`, `fault_probe.py`; `providers.py:778-912` | 18 + 291 + 600 + 324 lines; about 185 tests | store-engine messages (`infra.py:75-82`) | B02, B03, C22 | merge from |
| ICSA | `icsa/harness/errors.py`, `icsa/campaign/errors.py`; classification in `loop.py`, `runner.py`, `scheduler.py:2171-2346` | 23 + 24 lines plus call sites; about 200 tests | `onedata_health`, `op_worker_restart` kinds | B02, B03, B09, C03, C10, C11; C02 open | **source** |

The registered rule is ICSA's (`protocol/parameters.md:40`): never infrastructure for slowness, caps, exceptions in agent code, or tool errors from the agent's own arguments. The core closes the kind vocabulary (C02) and lets the platform register its kinds.

### 16. Onedata platform layer

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | `benchmark/fixture_runner.py`, per-model space scripts | 516 + 680 lines; 14 tests | space and provider names and ids | O07 | merge from (O07) |
| ICSOC | `harness/fixtures.py`, `provision.py`, `verifier.py`, `netem.py`, `provider_restart.py` | 412 + 757 + 553 + 416 + 381 lines; about 150 tests | REST paths, cluster names, provider ids, login user | O07, O08, O10, O11 | merge from |
| WWW | not applicable (SPARQL stores) | — | — | — | — |
| ICSA | `icsa/scripted/{provision,placement,mplus,s2ranges,metrics}.py`; `icsa/harness/{health,real_sandbox,slot_users}.py` | 333 + 64 + 510 + 61 + 186 + 79 + 358 + 422 lines; about 215 tests | federation names in at least six modules (O12); one supporting provider assumed (O04) | O01–O06, O09 | **source** |

Provider-side probes: ICSA measures per-container interface counters (`metrics.py:20-50`) and reads transfer-record byte fields (`provision.py:236-240`). Bytes moved per provider pair are not measured anywhere: **new** in #4.

### 17. Day-0 checks

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | absent | — | — | — | — |
| ICSOC | transfer-health canary (`provision.py:681-750`) | in 16 | — | O07 | — |
| WWW | result folders with a README per measured fact (for example `results/qlever-timeouts-20261019/`) | — | — | — | — |
| ICSA | `day0/` (about 45 scripts, `REPORT.md`, `evidence/`); `tools/newhost/check.sh` | 59 tests (`check.sh`) | hosts, provider labels, space names throughout (`day0/scripts/od.py:26-34`) | unknown is not a pass (`check.sh:13-14`) | **source** |

`day0/REPORT.md` is the model for #4's checks: dated facts with evidence files, and "unknown" where something could not be measured.

### 18. Snapshot cut and fetch

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM, ICSOC, WWW | absent | — | — | — | — |
| ICSA | ICSA-snap `snapshot/` (`fetch.py`, `cut.py`, `codecheck.py`, `guard.py`, `stamp.py`; `bmerge.py`, `recohort.py` exist only for per-provider dispatchers) | 5,332 lines; about 339 tests | the paper's cut instant | C21 | **source** (generic files) |

### 19. Operator tooling

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM, ICSOC, WWW | absent (WWW has a fault monitor script) | — | — | — | — |
| ICSA | `icsa/campaign/operator_queue.py`, `release.py`; the `lowered_cap` ladder in `livecheck.py:60-83` | 411 + 89 lines; 34 tests | `progress.py` reads every subsystem | C13 | **source** (these pieces) |

### 20. Secrets hygiene in records

| Study | Where | Size, tests | Study-specific | Handles | Verdict |
|---|---|---|---|---|---|
| PPAM | absent | — | — | — | — |
| ICSOC | absent; stray-key abort (`scored_sweep_driver.py:258-264`) | — | — | — | — |
| WWW | `harness/agent/record.py:189-304`; `metadata.py` | 18 tests | — | R12 | **source** |
| ICSA | token values never recorded (`tokencheck.py:42, 73`); key read at use time (`runner.py:61-70`) | — | — | R12 | merge from |

## Read-only enforcement in ICSA

The agent's data path is read-only at six points; the platform layer keeps all six, fail-closed (O05).

1. **Token caveats at mint:** `data.readonly` plus `data.path` scoped to the study space, on every run, health and tool-server token (`icsa/harness/slot_users.py:59-67`, `real_sandbox.py:77-84, 102-123`, `scripted/federation.py:56-61`, `knowledge/launch.py:33-41`).
2. **Space privileges of each run's user:** exactly view and read, read back and refused otherwise (`slot_users.py:35, 336-351`).
3. **Gate policy:** only five read calls pass; writes, metadata, shares and other hosts are refused (`icsa/sandbox/gatepolicy.py:34-42, 55-78`, `gate.py:665-667`).
4. **Read-only mount and root filesystem** (`icsa/sandbox/sandbox.py:429, 456`).
5. **Token examination before use, fail-closed** (`icsa/knowledge/tokencheck.py:40-60`, called at `launch.py:98`).
6. **MCP server pruning of write tools** under a read-only token (OMCP `token_policy.py:25-42, 95-100`). It fails open (`65-91`), so ICSA also fixed the server's allow-list (`knowledge/pin.py:82-94`).

The knowledge-plane conditions of the first consumer need a writable tool-server token beside this read-only data path; [`architecture.md`](architecture.md#tokens-and-permissions) separates the two.

## Appendix: study tokens

Names that must never appear in the core or the platform layer. Values are withheld from this public repository; #3 commits them as hashes ([`architecture.md`](architecture.md#the-study-token-list)).

| Category | Where they occur (counts) |
|---|---|
| Conditions and options | PPAM tool tiers (`tool_allowlist.py:47-125`); ICSOC arms A0–A4, fault conditions (`schema.py:50-57`, `sweep.py:39-42`); WWW prompt conditions and phases (`prompts.py:108-113`, `run_pilot.py:495`); ICSA options T, T-unpaginated, D, M, M+ (`records.py:17-22`, `gatepolicy.py:26-28`; 47 code files) |
| Layouts and data states | ICSA S1, S2 (`records.py:25-27`, `sandbox/config.py:197-198`) |
| Tasks, scenarios, instances | PPAM D1–D6, A1–A6, P1–P6; ICSOC E1, E3; WWW question sets; ICSA instance boxes and groups (`records.py:51-69`, `campaign/config.py:42-120`) |
| Datasets and files | PPAM and ICSOC fixture paths; WWW graph and store names (5 graphs); ICSA dataset id, daily file names, mask variable (`config/harness.toml:87`, `scoring/report.py:46-50`) |
| Model names | PPAM, ICSOC and WWW model tables; ICSA 7 subject models plus a coder model in three naming schemes |
| Hosts and addresses | PPAM 6 lines; ICSOC 14 lines; WWW gateway and cluster hosts (about 15 lines); ICSA 10 lines in core files, a zone host in 9 files, a provider host in 19 files, an address range in `netpool.py` |
| Spaces | PPAM test spaces and per-model spaces; ICSOC sweep prefix; ICSA space name in 10 files and id in 4 |
| Providers | PPAM and ICSOC provider labels and ids; ICSA three provider labels (175, 52 and 11 lines) and backend names |
| Users | upstream handles, login users, local account names in paths (ICSOC 6 lines); ICSA slot-user prefix and admin principal |
