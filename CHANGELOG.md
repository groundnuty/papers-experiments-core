# Changelog

Every release of `papers-experiments-core`. Versions follow semantic versioning with tags `vMAJOR.MINOR.PATCH`; while the version is 0.x, a minor release may break interfaces, and its entry says which.

## 0.1.0

The first release: what a study needs to run end to end on the core (agent loop, model backends, tools, prompts, records and replay, the campaign kernel, the statistics kernel, provenance and replication packages), with the Onedata platform layer's configuration, token planes, tool-server launch and health. A study depends on it by tag and copies nothing; [`docs/study-guide.md`](docs/study-guide.md) shows how. What the release guarantees is the list of regressions at the end of this entry: each has a test on this tree, named beside it, whose docstring starts with the ID. An ID of [`docs/regressions.md`](docs/regressions.md) that is not in the list is not guaranteed yet.

**Nothing in this release has run against a live Onedata deployment.** The platform layer is tested against fakes of the Onedata API and of the MCP server. The contract tests that would talk to a real deployment (`tests/live/`) skip unless `EXPCORE_LIVE_PLATFORM` names one, and they have not been run.

### Added

- The generic core, `expcore`: the agent loop with output capping and the closed outcome vocabulary; OpenAI-compatible, Anthropic and stub backends with retries and the served-model check; the MCP stdio client with frozen manifests and allow-lists; prompt templates with named slots; records, offline replay and the replay check; the campaign kernel (factor schema, manifest, store and resume, scheduling on one host, leases, budgets, export); the statistics kernel (named contrasts, the Wilcoxon cluster-bootstrap estimator, Holm, power simulation, preregistration) and kappa for cause coding; code stamps, provenance headers and the study-token check. The interfaces a study implements are in [`docs/interfaces.md`](docs/interfaces.md).
- The platform layer, `expcore_onedata` (the `[onedata]` extra): federation configuration, both token planes with fail-closed examination, the tool-server launch with its allow-list, and health checks against the declared support set.
- Replication packages: `python -m expcore package STUDY --out DIR` builds what the study's `package.toml` lists and nothing else, with a SHA-256 manifest, a study-token check over every path and content, whole-word anonymisation lists for double-blind submissions, and a deterministic archive. The package's own `make check` (`python -m expcore package-check`) verifies every file and derives the tables and the report anew from the records, byte for byte; the build runs it before it writes anything.
- The example study ships in the wheel: `python -m expcore example DEST` writes it into a new directory. `scripts/fresh_repo.py`, run by the `fresh-repo` workflow after each push to `main`, makes a new repository that depends on the core by commit and runs the example end to end from the installed package, its replication package included.
- [`docs/study-guide.md`](docs/study-guide.md): starting a study, configuration, factors and conditions, recording and replay, scoring and analysis, freezing a protocol, replication packages and which conclusions may say "no difference". Its code runs as part of the tests.

### Changed

For readers of the commits before this release:
- `export.json` and the campaign snapshot name each attempt's directory as `<digest>/try<N>`, relative to the batch, instead of the absolute path the store keeps; the snapshot is also rebuilt so its free space holds no older copy. Neither carries a path of the machine the campaign ran on any more.
- `analyse_study` writes nothing. `write_analysis` writes `export.json`, `scores.json` and `analysis.json`, `write_report` writes `report.md`, and `regenerate` compares all four with what the records give.
- The report and the terminal summary take a contrast's exploratory label from the statistics kernel's own flag, and refuse a result whose flag disagrees with its Holm decisions. A contrast outside the Holm family is shown as exploratory in every table, never as confirmatory.

### Known limits

- The package's study-token check uses the hashes whose scope is `all`, the names private everywhere (hosts, addresses, users, spaces, providers). A study's own condition, dataset and model names are expected in its own package, so code-scope hashes are not applied. `--hashes` takes another hash file, for example a study's own.
- Anonymisation relabels only whole words in the prose and metadata files its list names (JSON in its parsed keys and strings, other formats as text), and fails the build on a listed term anywhere else, escaped or encoded forms included; it never rewrites code. The scanner and canaries of earlier packages are not in this release.
- The package's checks open gzip, bzip2, xz, zip and tar content and refuse other compressed or archive formats they recognise; PDF and PNG files are read as raw bytes, without opening their own compressed streams.
- Not in this release: streaming transport (B17), snapshot cut and fetch (C21), confirmed model-level faults (C22), two-host execution, sampling for cause coding, further estimators and fault injection ([`docs/architecture.md`](docs/architecture.md#release-boundary)).

### Regressions guaranteed

Each row names the files whose tests back the ID; `tests/test_changelog.py` checks both directions against the tree.

| ID | Guarantee | Tests |
|---|---|---|
| B01 | Retry transient API errors with capped exponential backoff | `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_retry.py` |
| B02 | Exhausted retries are infrastructure | `tests/loop/test_loop_failures.py`, `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_retry.py` |
| B03 | A request that was sent and is slow is never resent | `tests/loop/test_agent_loop.py`, `tests/loop/test_loop_failures.py`, `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_retry.py` |
| B04 | Echo the reasoning field when the model needs it | `tests/models/test_adapters_openai.py` |
| B05 | Anthropic thinking blocks go back verbatim | `tests/models/test_adapters_anthropic.py` |
| B06 | Served-model check | `tests/models/test_adapters_openai.py`, `tests/models/test_served.py` |
| B07 | A listed model is not a servable model | `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_admission.py` |
| B08 | Unfrozen settings refuse a study run | `tests/config/test_models.py`, `tests/models/test_factory.py` |
| B09 | A rejected key is not retried and not recorded | `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_retry.py` |
| B10 | Usage is counted on arrival and never invented | `tests/loop/test_agent_loop.py`, `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_usage.py` |
| B11 | Output-token limits fit the model and the endpoint | `tests/config/test_models.py`, `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_admission.py` |
| B12 | A reply cut at the output limit is not an answer | `tests/loop/test_agent_loop.py`, `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_adapters_stub.py` |
| B13 | Context overflow is the agent's outcome | `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_retry.py` |
| B14 | Vendor extras pass through | `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py` |
| B15 | Record what was sent, not what was meant | `tests/models/test_adapters_anthropic.py`, `tests/models/test_adapters_openai.py`, `tests/models/test_route.py` |
| B16 | Incompatible per-model rules are refused before any request | `tests/config/test_model_checks.py`, `tests/config/test_models.py`, `tests/models/test_factory.py` |
| L01 | Malformed tool-call arguments are never run as `{}` | `tests/loop/test_agent_loop.py` |
| L02 | Unknown tools and wrong argument types are tool errors, not crashes | `tests/loop/test_agent_loop.py` |
| L03 | Turn cap counts rounds | `tests/loop/test_agent_loop.py` |
| L04 | The agent is charged only for its own time | `tests/loop/test_loop_time.py`, `tests/loop/test_loop_waits.py`, `tests/loop/test_outcomes_and_config.py`, `tests/loop/test_timer_toolbox_arguments.py`, `tests/records/test_schema.py`, `tests/replay/test_waits.py`, `tests/test_plugin.py`, `tests/tools/test_function_provider.py` |
| L05 | No hidden per-call limit | `tests/loop/test_loop_time.py`, `tests/loop/test_outcomes_and_config.py`, `tests/tools/test_function_provider.py` |
| L06 | Tool output is capped by a declared rule, and the rule is pinned | `tests/loop/test_agent_loop.py`, `tests/loop/test_capping.py` |
| L07 | A truncation by the data source is flagged | `tests/loop/test_agent_loop.py` |
| L08 | A crash in harness code is not the agent's failure | `tests/loop/test_loop_failures.py` |
| L09 | Calls after a terminal call are answered "skipped" | `tests/loop/test_agent_loop.py` |
| L10 | Leaked tool calls are detected, not nudged | `tests/loop/test_leaks.py` |
| L11 | An empty final reply differs from no reply | `tests/loop/test_agent_loop.py` |
| L12 | MCP failures are split by side | `tests/tools/test_mcp_client.py` |
| L13 | The tool manifest is frozen and enforced by the server | `tests/onedata/test_toolserver.py`, `tests/tools/test_manifest.py`, `tests/tools/test_mcp_client.py` |
| L14 | A tool server that does not start in time is broken | `tests/tools/test_mcp_client.py` |
| L15 | Blocking waits never run on the event loop | `tests/loop/test_loop_cancel.py`, `tests/tools/test_function_provider.py` |
| R01 | Records are written atomically and read back byte for byte | `tests/records/test_golden.py`, `tests/records/test_record.py`, `tests/replay/test_divergence.py` |
| R02 | Unknown schema versions and fields are refused | `tests/records/test_build.py`, `tests/records/test_golden.py`, `tests/records/test_record.py`, `tests/records/test_schema.py` |
| R03 | Replay re-derives every derived field without a model call | `tests/records/test_golden.py`, `tests/replay/test_divergence.py`, `tests/replay/test_native.py`, `tests/replay/test_raw_chars.py`, `tests/replay/test_redaction.py`, `tests/replay/test_replay.py`, `tests/replay/test_waits.py` |
| R04 | Analysis refuses records without a current replay check | `tests/e2e/test_verified_analysis.py`, `tests/replay/test_check.py` |
| R05 | Decisions served during replay are recomputed offline | `tests/replay/test_check.py` |
| R06 | The echoed request is pinned by a digest | `tests/records/test_build.py`, `tests/replay/test_divergence.py` |
| R07 | Records hold no interpreter-specific text | `tests/records/test_build.py` |
| R08 | Every archived record set is covered by the roundtrip test, and no output is archived without its replay check | `tests/e2e/test_verified_analysis.py`, `tests/replay/test_check.py` |
| R09 | An infrastructure failure keeps its partial transcript | `tests/records/test_build.py` |
| R10 | Unknown is `None`, never 0 | `tests/records/test_record.py`, `tests/records/test_schema.py`, `tests/stats/test_pairing.py` |
| R11 | Run keys are unique and replicates are real | `tests/campaign/test_factors.py`, `tests/campaign/test_manifest.py`, `tests/campaign/test_resume.py`, `tests/campaign/test_store.py` |
| R12 | Secrets never reach a record | `tests/onedata/test_credentials.py`, `tests/onedata/test_tokens.py`, `tests/records/test_golden.py`, `tests/records/test_jsontext.py`, `tests/records/test_redact.py`, `tests/replay/test_builder_inputs.py`, `tests/replay/test_redaction.py` |
| R13 | Re-scoring is offline and versioned | `tests/e2e/test_rescoring.py`, `tests/records/test_rescore.py`, `tests/test_report.py` |
| C01 | An API failure is never scored as the agent's | `tests/campaign/test_export.py`, `tests/campaign/test_holds.py`, `tests/campaign/test_record_agreement.py`, `tests/campaign/test_scheduler.py`, `tests/campaign/test_store.py`, `tests/campaign/test_treatment_turns.py`, `tests/e2e/test_tiny_study.py`, `tests/onedata/test_toolserver.py`, `tests/records/test_build.py`, `tests/records/test_golden.py`, `tests/records/test_schema.py`, `tests/records/test_treatment_versions.py` |
| C02 | The infrastructure-kind vocabulary is closed and survives to the export | `tests/campaign/test_export.py`, `tests/campaign/test_factors.py`, `tests/loop/test_loop_failures.py`, `tests/tools/test_mcp_client.py` |
| C03 | Attempts are charged by cause | `tests/campaign/test_scheduler.py`, `tests/campaign/test_store.py` |
| C04 | A kill at any point resumes cleanly | `tests/campaign/test_dispatch_position.py`, `tests/campaign/test_legacy_markers.py`, `tests/campaign/test_recovery.py`, `tests/campaign/test_resume.py`, `tests/campaign/test_scheduler.py`, `tests/campaign/test_scheduler_accounting.py`, `tests/campaign/test_store.py`, `tests/e2e/test_tiny_study.py` |
| C05 | An interrupted attempt costs its bound, never 0 | `tests/campaign/test_attempt_bounds.py`, `tests/campaign/test_executor.py` |
| C06 | A campaign never resumes into a different experiment | `tests/campaign/test_factors.py`, `tests/campaign/test_manifest.py`, `tests/campaign/test_resume.py` |
| C07 | One dispatcher per store | `tests/campaign/test_lock.py`, `tests/campaign/test_scheduler.py`, `tests/campaign/test_scheduler_close.py` |
| C08 | Run order is seeded and stable when the plan grows | `tests/campaign/test_manifest.py`, `tests/campaign/test_resume.py` |
| C09 | Exhausted runs reach the analysis | `tests/campaign/test_export.py`, `tests/test_report.py` |
| C10 | Harness bugs are neither infrastructure nor outcomes | `tests/campaign/test_executor.py`, `tests/campaign/test_harness_stop.py`, `tests/campaign/test_scheduler.py`, `tests/campaign/test_scheduler_accounting.py`, `tests/campaign/test_scheduler_gates.py` |
| C11 | Health that cannot be read is unhealthy | `tests/campaign/test_healthgate.py`, `tests/campaign/test_scheduler_close.py`, `tests/campaign/test_scheduler_gates.py`, `tests/campaign/test_scheduler_recheck.py` |
| C12 | Unknown cost holds like over-budget | `tests/campaign/test_gate.py`, `tests/campaign/test_scheduler_gates.py`, `tests/config/test_prices.py` |
| C13 | A release file counts once and only if newer than the stop it ends | `tests/campaign/test_gate.py`, `tests/campaign/test_harness_stop.py`, `tests/campaign/test_release.py`, `tests/campaign/test_scheduler_accounting.py`, `tests/campaign/test_scheduler_gates.py` |
| C14 | Overrides reach execution | `tests/campaign/test_scheduler.py`, `tests/models/test_factory.py` |
| C15 | Endpoint health is tracked per unit | `tests/campaign/test_scheduler_accounting.py`, `tests/campaign/test_scheduler_gates.py`, `tests/campaign/test_units.py` |
| C16 | An endpoint whose identity changed during a run makes the run infrastructure | `tests/campaign/test_identity.py`, `tests/campaign/test_scheduler_gates.py` |
| C17 | An unexpected worker exception stops dispatch loudly | `tests/campaign/test_scheduler.py` |
| C18 | Two conditions never share attempt keys | `tests/campaign/test_factors.py` |
| C19 | Leases on shared mutable resources fail safe | `tests/campaign/test_lease.py`, `tests/campaign/test_reservation.py`, `tests/campaign/test_scheduler_gates.py` |
| C20 | Store schema changes are crash-safe | `tests/campaign/test_dispatch_position.py`, `tests/campaign/test_store.py`, `tests/campaign/test_store_concurrent_open.py` |
| P01 | Unknown code is dirty code, and dirty code does not start a study run | `tests/provenance/test_codestamp.py` |
| P02 | Code identity includes the data files the code reads, and is re-verified | `tests/provenance/test_codestamp.py` |
| P03 | Tree hashes ignore modification times and caches | `tests/provenance/test_codestamp.py` |
| P04 | Every output manifest carries the hash of its configuration and preregistration | `tests/stats/test_analysis.py`, `tests/stats/test_prereg.py` |
| P05 | Scoring and inference code are frozen by digest and a known-answer test | `tests/stats/test_kat.py` |
| P06 | One model table | `tests/config/test_models.py` |
| P07 | A replication package is not a code source | `tests/test_provenance_history.py` |
| S01 | Float noise is snapped before rank tests | `tests/stats/test_contrasts.py` |
| S02 | Holm keeps the declared family | `tests/stats/test_analysis.py`, `tests/stats/test_contrasts.py` |
| S03 | Degenerate contrasts are defined | `tests/stats/test_analysis.py`, `tests/stats/test_contrasts.py`, `tests/stats/test_estimators.py`, `tests/stats/test_pairing.py` |
| S04 | The cluster bootstrap resamples clusters once for all models | `tests/stats/test_contrasts.py`, `tests/stats/test_estimators.py` |
| S05 | An undefined resample is NaN and never supports the hypothesis | `tests/stats/test_contrasts.py`, `tests/stats/test_estimators.py`, `tests/stats/test_pairing.py`, `tests/stats/test_rows.py` |
| S06 | Multiplicity levels are exact at the boundary | `tests/stats/test_analysis.py`, `tests/stats/test_contrasts.py`, `tests/stats/test_estimators.py`, `tests/stats/test_independence.py`, `tests/stats/test_power.py`, `tests/stats/test_rejection.py` |
| S07 | Kappa reports when it is undefined | `tests/coding/test_kappa.py` |
| S09 | The power simulator is independent of the pipeline it checks | `tests/stats/test_analysis.py`, `tests/stats/test_independence.py`, `tests/stats/test_sim.py` |
| O03 | Provider discovery returns every provider of the space | `tests/onedata/test_federation.py` |
| O04 | Health checks take the expected providers from configuration | `tests/onedata/test_health.py` |
| O05 | The data path is read-only, enforced fail-closed | `tests/campaign/test_factors.py`, `tests/live/test_contract.py`, `tests/onedata/test_live_gate.py`, `tests/onedata/test_live_support.py`, `tests/onedata/test_tokencheck.py`, `tests/onedata/test_tokens.py`, `tests/onedata/test_toolserver.py`, `tests/records/test_schema.py`, `tests/test_errors.py` |
| O06 | Token lifetimes follow the run | `tests/onedata/test_tokens.py`, `tests/onedata/test_toolserver.py` |
| O09 | Mutations are scoped | `tests/onedata/test_client.py` |
| O12 | Federation names live in configuration only | `tests/onedata/test_config.py`, `tests/onedata/test_messages.py`, `tests/test_no_placeholders.py` |
| K01 | A check with no input fails | `tests/package/test_archive.py`, `tests/package/test_build.py`, `tests/package/test_spec.py` |
| K03 | The private name list never enters the repository or the output | `tests/package/test_anonymise.py`, `tests/package/test_build.py`, `tests/package/test_scan.py` |
| K04 | Anonymisation must not change numbers, model names or parseability | `tests/package/test_anonymise.py` |
| K05 | Findings never carry the matched value | `tests/package/test_anonymise.py`, `tests/package/test_build.py` |
| K06 | Archives are deterministic | `tests/package/test_archive.py`, `tests/package/test_build.py` |
| K07 | `make check` regenerates every table from records and compares byte for byte | `tests/package/test_build.py`, `tests/package/test_spec.py`, `tests/test_fresh_repo.py` |
| K08 | Shipped code parses on the oldest supported Python | `tests/test_oldest_python.py` |
