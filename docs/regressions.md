# Regression list

Every failure mode an earlier study learned, with where it was learned and the test that keeps it learned. Issues #3 and #4 turn each entry into at least one test, and release notes name the entries a release guarantees. Part of #2; the inventory that cites these IDs is [`inventory.md`](inventory.md).

## How to read an entry

**ID. Title.** What the core must do. *Learned:* where each study met it, as `STUDY:path:line`, plus the commit or date that fixed it. *Test:* the check #3 or #4 writes.

Source keys (full refs in [`inventory.md`](inventory.md#sources)):

| Key | Repository and ref |
|---|---|
| PPAM | onedata-mcp, branch `ppam2026/14-tools` @ `08e201b`, directory `benchmark/` |
| ICSOC | icsoc-2026-code-agent @ `c1e76c2`; `ICSOC-pkg` is its replication package @ `06223f1` |
| WWW | www-2027-experiments, branch `feat/sib-u-compose` @ `46a5828d` |
| ICSA | icsa-2027-experiments, branch `feat/37-artifact-clear` @ `6fc798cf`; `ICSA-snap` is `feat/34-final-snapshot-code` @ `debba6d6` |
| OMCP | onedata-mcp @ `0cc17ce` |

Line numbers come from `git archive` snapshots of these refs. A few WWW scoring and statistics lines were located by a sweep and may be off by a few lines; every other location was read directly.

Groups: [B model backends](#b-model-backends) · [L loop and tools](#l-agent-loop-and-tools) · [R records and replay](#r-records-and-replay) · [C campaign and failure accounting](#c-campaign-and-failure-accounting) · [P provenance](#p-provenance) · [S statistics and cause coding](#s-statistics-and-cause-coding) · [O platform (Onedata)](#o-platform-onedata) · [K replication packages](#k-replication-packages).

## B. Model backends

**B01. Retry transient API errors with capped exponential backoff.** Retry 429, 408, 409, 5xx, timeouts before a reply, and connection errors. Back off exponentially up to a cap, with jitter, and honour `Retry-After`. Turn SDK-internal retries off so every retry is counted and recorded. *Learned:* PPAM `benchmark/llm_adapters/openai_compat.py:44-84` (dde4246, then 1d41f37 after a 14 s budget proved shorter than a 60 s rate-limit window; 5xx not retried); lost in ICSOC `harness/panel.py:337-348` (one try, then `break`); rebuilt in WWW `harness/agent/providers.py:760-823` (d1e22ac5, 18 Oct) and ICSA `icsa/harness/adapters.py:168-198, 334-358` (4f6d4580, 1 Nov; no jitter, no `Retry-After`). Neither source meets the rule as written: WWW caps `Retry-After` at its maximum delay and then applies jitter of ±25 % (`providers.py:819-823`), so a 60 s header can produce a 45 s sleep. The core applies jitter only above `Retry-After` and never shortens it. *Test:* a fake raising 429 three times then answering yields one turn with three recorded retries and sleeps of at least `Retry-After`; a 400 is not retried.

**B02. Exhausted retries are infrastructure.** A transient error that outlasts the retries ends the attempt as an infrastructure failure, unscored and re-run, never as an agent outcome. *Learned:* WWW `providers.py:896-901`; ICSA `adapters.py:186-195` (`InfraError("endpoint")`). *Test:* N+1 transient errors give `failed:infrastructure` with source `endpoint` and no score.

**B03. A request that was sent and is slow is never resent.** A read timeout after the request went out ends the run at its time cap with usage marked unknown; connect, pool and write timeouts are failures to reach the endpoint and stay infrastructure. A retry that would outlast the run's budget is infrastructure; the run's own deadline is checked before the retry limit. *Learned:* ICSA `adapters.py:83-99, 181-185`, `loop.py:195-197` (final-review fixes, 1 Nov, `docs/harness.md:141-156`); WWW `providers.py:864-907` (1810cfb5, 18 Oct). *Test:* a `ReadTimeout` ends as time cap after exactly one request; a connect timeout is retried and then infrastructure.

**B04. Echo the reasoning field when the model needs it.** Per model and off by default, send the previous turn's reasoning back under the field the deployment expects. Keep it in the transcript either way. *Learned:* PPAM `openai_compat.py:185-193` (HTTP 400 without it; 8f00137, 22bd69f); lost in ICSOC `panel.py:359-372`; rebuilt in WWW `providers.py:227-279` (a2ea65c6, 21 Oct) and ICSA `adapters.py:216-226, 264-265` (1 Nov). *Test:* with echo on, the next request carries the field; with echo off, the wire carries neither it nor the internal copy, and the transcript still holds it.

**B05. Anthropic thinking blocks go back verbatim.** *Learned:* ICSA `adapters.py:384-389`; WWW `providers.py:198-200`. *Test:* the next request's assistant content equals the previous response blocks, signatures included.

**B06. Served-model check.** Record the model id, system fingerprint and every echoed setting of each reply. Treat another id, a changed fingerprint, a setting that differs from the request, or a setting that was never requested (a silent server default) as a mismatch that stops the model's dispatch; treat a reply with no id as unreported. A reply carrying several fingerprints is a change by itself; a dated snapshot id (`<model>-YYYYMMDD`) is the same model. *Learned:* WWW `scripts/run_pilot.py:2175-2283`, `loop.py:488-500, 683-699` (id 57e148c3, 19 Oct; fingerprint c396cc38, 20 Oct; mixed fingerprints c2378e26, 21 Oct); ICSA `icsa/servedsettings.py:22-102`, `campaign/scheduler.py:948-965` (f1c242a8, 3 Nov; "the same silent-default bug", `servedsettings.py:1`). *Test:* `judge` returns mismatch for another id, a changed setting and an unrequested setting; unreported for no id; a missing setting is recorded as "not reported", never as the requested value.

**B07. A listed model is not a servable model.** Admit a model only after a one-token completion succeeds; a `/models` listing is not enough. *Learned:* ICSOC `panel.py:776-829` (inactive models and models outside the grant passed the listing). *Test:* a fake that lists the id but answers 400 on completion rejects the model before the campaign starts.

**B08. Unfrozen settings refuse a study run.** *Learned:* ICSA `icsa/harness/config.py:210-218` (`assert_frozen`; 9dd6dda5, 1 Nov). This is a configuration gate, separate from B06. *Test:* a model or harness config whose status is not `frozen` raises before any request.

**B09. A rejected key is not retried and not recorded.** 401 and 403 end as `key_rejected` after one request; the SDK's message, which may echo the key, is never recorded. *Learned:* ICSA `adapters.py:32-36, 179-180, 342-343` (f1c242a8, 3 Nov). *Test:* 401 and 403 each give one `key_rejected` after one request, with no SDK text in the record.

**B10. Usage is counted on arrival and never invented.** Count tokens before parsing can fail; a 200 without usage or without choices is infrastructure; unreported usage is `None`, never 0; cached prompt tokens count toward the token budget. *Learned:* ICSA `adapters.py:203-214, 360-371, 390-397` (8bc7ce09, 1 Nov; f1c242a8, 3 Nov); WWW `providers.py:17-28, 301-312`. *Test:* a 200 with usage and no choices raises infrastructure and the usage still counts; input tokens equal uncached plus cache-write plus cache-read.

**B11. Output-token limits fit the model and the endpoint.** `max_tokens` is the study value or the model's own lower maximum, checked against the endpoint's context at admission. *Learned:* ICSA `config.py:88-90` (amendment 6); PPAM `benchmark/panel.py:115-119, 169-173` (402 and context errors); ICSOC reset the limits to 4,096 tokens and 8 rounds (`panel.py:259-260`) after PPAM had raised them for empty answers (cb4a49f). *Test:* a model with a lower maximum sends its own value; a limit over the endpoint's context fails admission.

**B12. A reply cut at the output limit is not an answer.** Record `finish_reason`; a final reply stopped by `length` ends as an output cap, checked before the token budget. *Learned:* PPAM `openai_compat.py:158`; lost in ICSOC `panel.py:355-376`; ICSA `loop.py:151, 220-222`. *Test:* a tool-free reply with stop `length` ends as output cap even when the budget is also exceeded.

**B13. Context overflow is the agent's outcome.** A 400 or 413 that names the context is recorded as context overflow and not retried. *Learned:* WWW `providers.py:691-728` (agent-run/9). *Test:* each tested provider message classifies as overflow.

**B14. Vendor extras pass through.** `extra_body` and provider pinning without fallback reach the request. *Learned:* PPAM `benchmark/panel.py:125-150`, `openai_compat.py:125, 137` (a fallback provider returned gibberish); lost in ICSOC `panel.py:338-345`. *Test:* a condition's extras appear in the request body.

**B15. Record what was sent, not what was meant.** Sampling values in the record come from the request object; credentials are stripped from recorded URLs; keys are read at use time. *Learned:* ICSOC `harness/a2_judge.py:114-117` against `lenses/a2.py:34` (a68ff62); WWW `providers.py:915-923`; ICSA `runner.py:61-70`. *Test:* the recorded temperature equals the request's; a base URL with credentials is recorded without them.

**B16. Incompatible per-model rules are refused before any request.** *Learned:* WWW `loop.py:346-351, 971-998` (echo with reasoning-channel recovery; caps below the notice length). *Test:* each incompatible combination raises at configuration.

**B17. Streamed replies must be complete.** When streaming is used: a reply counts only with the end marker, a finish reason and the final usage chunk; an error event after HTTP 200 is transient; a call without id or name, or a token count that rises with no characters, is rejected and retried; a quiet stream is cut by a whole-request watchdog. *Learned:* WWW `providers.py:1308-1396, 1605-1805` (bec52d27, 1b7b7ec8, 79a3d49c; 19 Oct). Applies when a streaming transport is ported (see [`architecture.md`](architecture.md#open-questions-for-review), question 3). *Test:* WWW `tests/test_agent_streaming.py` cases.

## L. Agent loop and tools

**L01. Malformed tool-call arguments are never run as `{}`.** Do not execute the call; tell the model its arguments were not valid JSON; keep the raw text in history as `{"_raw": "..."}`, because servers reject invalid JSON in history with HTTP 400. Whether the attempt continues or ends is a declared policy. *Learned:* PPAM `openai_compat.py:224-229` and ICSOC `panel.py:380-385` silently ran the tool with `{}`; ICSA fixed it (`adapters.py:102-109, 227-237`, `loop.py:104-107`; `docs/harness.md:25` names the ICSOC base); WWW ends the attempt as `failed:malformed_tool_call` (`loop.py:782-788`; d2d0402b, 19 Oct). Issue #2 listed this item the other way round; the code shows ICSA as the fix. *Test:* arguments `'{"a":'` are not executed, the tool result says so, the resent history is valid JSON holding the raw text, and the round counts toward the turn cap.

**L02. Unknown tools and wrong argument types are tool errors, not crashes.** *Learned:* ICSA `loop.py:109-111, 148`; WWW `tools.py:693-694, 787`. *Test:* an unknown name returns an error listing the tools.

**L03. Turn cap counts rounds.** A turn with several calls counts once; a tool-free final reply after the last round is allowed. *Learned:* ICSA `loop.py:229-234`. *Test:* three calls in one turn count once; cap + 1 rounds ends as turn cap.

**L04. The agent is charged only for its own time.** Check the wall cap before every model and tool call; exclude waits on infrastructure (store, gate, slot, lock, output validation) from it; the agent's own cap decides before any whole-time ceiling. *Learned:* ICSA `loop.py:185-194`, `runner.py:442-452`; WWW `loop.py:636-651` (ac621236, f7d6a4d6; 20 Oct). *Test:* a slow fake infrastructure call leaves charged time under the cap; own time at the cap gives a wall cap even when the ceiling is also passed.

**L05. No hidden per-call limit.** A tool call is bounded by the run's remaining time, and none starts with less than a second left. *Learned:* ICSA `loop.py:98-101, 112-116` (a hidden 600 s cell cap would have looked like a measured outcome; be25edbc, 1 Nov, amendment 3). *Test:* the timeout passed to the sandbox equals the deadline minus now.

**L06. Tool output is capped by a declared rule, and the rule is pinned.** The cap counts characters, keeps its notice inside the cap, and is stated in the tool description so the tools hash pins it; the full raw result stays in the record. Errors are capped like any output by default (ICSA); WWW exempted errors. *Learned:* ICSA `icsa/harness/capping.py:11-33`, `loop.py:93-96`; WWW `harness/agent/tools.py:13-19, 121-141, 241-303` (9f28d89f, 61474bdc; 19 Oct); PPAM capped only the record and sent the model the full text (`openai_compat.py:34, 238-247`); ICSOC had no cap. *Test:* an output over the cap ends with the notice and stays within it; two caps give two tools hashes.

**L07. A truncation by the data source is flagged.** A result exactly at the source's own row or size limit is marked as possibly cut. *Learned:* WWW `tools.py:259-263` (2615bfd0, 20 Oct). *Test:* rows equal to the source limit set the flag.

**L08. A crash in harness code is not the agent's failure.** Record it as a harness defect with a line-free stack hash, set the attempt aside, and re-run it after the fix. *Learned:* WWW `loop.py:277-296, 805-822`; ICSA `runner.py:349-352` and the campaign's same-row re-queue (`store.py:234-249`). *Test:* a tool raising on valid input yields `harness_defect`, never a scored failure.

**L09. Calls after a terminal call are answered "skipped".** History stays byte-identical across a retry branch. *Learned:* WWW `loop.py:611-617, 905-919`. *Test:* calls following a submit are logged skipped.

**L10. Leaked tool calls are detected, not nudged.** A call written as text or in the reasoning channel is labelled. *Learned:* WWW `providers.py:377-511`; PPAM `rescore.py:200-243`. *Test:* each recorded leak shape is labelled; domain JSON naming no tool is not.

**L11. An empty final reply differs from no reply.** *Learned:* PPAM `oracles/placement.py:214-224` (324b72e); ICSOC `panel.py:408-418`. *Test:* the record distinguishes empty content from rounds exhausted.

**L12. MCP failures are split by side.** A closed transport or an internal server error is infrastructure; any other MCP error goes to the model as tool output. *Learned:* ICSA `icsa/harness/knowledge.py:84-95` (ruling of 1 Nov, b449aff1). *Test:* invalid params produce a tool error; a closed connection raises infrastructure.

**L13. The tool manifest is frozen and enforced by the server.** Names, descriptions and schemas must equal the frozen manifest; the allow-list is set server-side; no client-side filter, so a server exposing an extra tool fails to start. An allow-list naming a tool the server lacks also fails at start. *Learned:* ICSA `icsa/knowledge/launch.py:53-80`, `pin.py:82-94`, `harness/knowledge.py:58-60` (bc30a8e5, 1 Nov); PPAM `benchmark/_tool_export.py:22-24`, `test/unit/test_tool_allowlist.py`; ICSOC `panel.py:319-321`. *Test:* an extra tool and a missing tool each fail start.

**L14. A tool server that does not start in time is broken.** *Learned:* ICSA `runner.py:36, 326` (120 s). *Test:* a server that never answers fails start within the bound.

**L15. Blocking waits never run on the event loop.** *Learned:* PPAM `oracles/placement.py:209`, `fixture_runner.py:379` serialised trials (`run_panel.py:26-28`); ICSOC used `asyncio.to_thread` (`runner.py:578-595`). *Test:* two concurrent fake waits overlap in wall time.

## R. Records and replay

**R01. Records are written atomically and read back byte for byte.** Write to a temporary file, fsync, rename; a record written back equals its stored bytes; fields added by a later schema are never written into an older record. *Learned:* WWW `harness/agent/record.py:193-198, 341-351, 378-404` (a60bebc8, 18 Oct); ICSA `campaign/export.py:160-166`. *Test:* load and dump of every fixture record is byte-identical.

**R02. Unknown schema versions and fields are refused.** Validate before writing and at load. *Learned:* WWW `record.py:355-362`; ICSOC `runner.py:690-695` with `schema.py:315-812`. *Test:* an unknown top-level field fails load.

**R03. Replay re-derives every derived field without a model call.** Serve model turns, tool results and clock readings from the record; re-run the real loop; compare transcript, tool-call log, outcome, caps and verdicts; stop at the first divergence. *Learned:* WWW `harness/agent/replay.py` (13 divergence points, 39-454); ICSA had only re-execution of recorded code cells (`icsa/inspection/replayrun.py:402-499`). *Test:* WWW's divergence cases (`tests/test_agent_record_replay.py`) ported to the core format.

**R04. Analysis refuses records without a current replay check.** The check is bound to the code identity and a hash of every input; a check made under other code, with changed inputs, or with an older schema is refused. *Learned:* WWW `harness/agent/replay_check.py:838-918` (3d1a7965, 18 Oct; 2c1d4fc4, 20 Oct). *Test:* each refusal case of `require_current_check`.

**R05. Decisions served during replay are recomputed offline.** *Learned:* WWW `replay_check.py:325-351` (bee4404a, 20 Oct). *Test:* a recorded decision that the offline recompute contradicts holds the batch.

**R06. The echoed request is pinned by a digest.** *Learned:* WWW `replay.py:116-132` (c8b3f32d, 21 Oct). *Test:* tampering with recorded reasoning diverges on replay.

**R07. Records hold no interpreter-specific text.** *Learned:* WWW records hold Python 3.12 error strings and replay only under 3.12 (`tests/test_real_records_roundtrip.py:51-54`). *Test:* exception text is stored as a class name plus a stable message.

**R08. Every archived record set is covered by the roundtrip test, and no output is archived without its replay check.** *Learned:* WWW sharded archives escaped the test glob, and a runner killed by SIGTERM left no replay check (`results/dev-natural-20261020/README.md:34, 42`). *Test:* every `records*.tar.gz` is collected; archiving an output without a check fails.

**R09. An infrastructure failure keeps its partial transcript.** *Learned:* ICSA `runner.py:413-431` (`infra.json` with usage seen so far). *Test:* an endpoint failure at turn 3 leaves turns 1-2 and their usage.

**R10. Unknown is `None`, never 0.** Usage, byte counters, tokens into context and undefined metrics. *Learned:* ICSA `records.py:92`, `sandbox/sandbox.py:1294-1322`, `scoring/cost.py:23-42`; WWW `providers.py:17-28`. *Test:* a missing counter serialises as `null` and is excluded from means.

**R11. Run keys are unique and replicates are real.** *Learned:* PPAM appended duplicate rows when a run id was reused (`run_panel.py:83-84`, `_trial_artefact.py:76-89`); ICSOC's `run_sweep(trial_k=N)` ran one trial per cell and recorded `trial_k=1` (`harness/sweep.py:110-148`, `sweep_driver.py:116-147`, still in ICSOC-pkg). *Test:* expanding a factor schema with three seeds yields three distinct run keys; a duplicate key is refused.

**R12. Secrets never reach a record.** Redact environment-named secrets in strings and keys; never record token values (only id, subject and caveat types). *Learned:* WWW `record.py:189-304` (bca7e5e9, 18 Oct); ICSA `icsa/knowledge/tokencheck.py:42, 73`. *Test:* a planted key value is absent from the written record and a warning is raised.

**R13. Re-scoring is offline and versioned.** Parser and scorer changes re-score saved records without model calls; the parser version is recorded, with a content hash when git is absent. *Learned:* PPAM `benchmark/rescore.py:128-197`; ICSA `icsa/scoring/parser_version.py:13-26`. *Test:* re-scoring a fixture corpus needs no network and records the parser version.

## C. Campaign and failure accounting

**C01. An API failure is never scored as the agent's.** *Learned:* PPAM scored a returned adapter error as PASS or FAIL (`openai_compat.py:139-150` into `trial_runner.py:124-131`); ICSOC excluded leg errors as no-data (`schema.py:304-313`; 861596c). *Test:* an adapter error is infrastructure and absent from rates.

**C02. The infrastructure-kind vocabulary is closed and survives to the export.** *Learned:* ICSA folds `knowledge` and `unattributed_kill` into `sandbox` at the campaign boundary (`icsa/harness/run.py:113-116`, `campaign/errors.py:4`), so `retries_by_kind` cannot show them (`campaign/export.py:153-154`) although `loop.py:55` and `docs/harness.md:180` promise it. *Test:* every harness kind appears under its own name in the export.

**C03. Attempts are charged by cause.** Infrastructure attempts count against the attempt limit; key rejections, outage-discarded attempts and interrupted attempts do not. *Learned:* ICSA `campaign/store.py:7-9, 208-211`, `scheduler.py:2285-2298`. *Test:* a mixed sequence charges exactly the infrastructure attempts.

**C04. A kill at any point resumes cleanly.** Running attempts become interrupted at the next open and are never charged; an outcome marker written before the kill is committed, never re-run. *Learned:* ICSA `store.py:192-201`, `scheduler.py:2498-2542` (41fbfc8e, 1 Nov; `tests/campaign/test_killresume.py`). *Test:* crash injection before the marker, after the marker and after the metadata write.

**C05. An interrupted attempt costs its bound, never 0.** *Learned:* ICSA `campaign/attempt_bounds.py:1-14` (82359471, 4 Nov). *Test:* an interrupted attempt with no record is charged its bound and excluded from mean cost.

**C06. A campaign never resumes into a different experiment.** Refuse a resume whose manifest fingerprint (tasks, conditions, models, caps, prompt and tool hashes, scorer digest) or pinned sandbox image differs. *Learned:* WWW `run_pilot.py:1413-1645`; ICSA `store.py:136-153`, `scheduler.py:328-338` (7c70f8f0, 1 Nov). *Test:* changing any fingerprinted input refuses the resume.

**C07. One dispatcher per store.** Take a non-blocking exclusive lock; warn on network filesystems where the lock is unreliable. *Learned:* ICSA `scheduler.py:211-216`; WWW `run_pilot.py:2067-2096`. *Test:* a second dispatcher on the same store exits.

**C08. Run order is seeded and stable when the plan grows.** *Learned:* WWW `run_pilot.py:1782-1855`. *Test:* adding runs keeps the relative order of existing ones; resuming with another order seed is refused.

**C09. Exhausted runs reach the analysis.** Runs that used all attempts are exported with reasons, so no denominator loses them. *Learned:* ICSA `campaign/export.py:1-16` (165f0f1d, 1 Nov). *Test:* an exhausted run appears in the export, and the analysis refuses an export without the key.

**C10. Harness bugs are neither infrastructure nor outcomes.** Re-queue from the same row and seed; three of a kind stop a model; a wider spread stops all. *Learned:* ICSA `scheduler.py:2314-2318`, `campaign/harness_stop.py:1-15`. *Test:* three harness errors for one model stop its dispatch.

**C11. Health that cannot be read is unhealthy.** A failed pre-run check holds all dispatch for one attempt per outage; an unreadable gate state at restart opens closed. *Learned:* ICSA `campaign/healthgate.py:1-21`, `scheduler.py:2031-2067`. *Test:* unreadable state blocks dispatch until a check passes.

**C12. Unknown cost holds like over-budget.** Budgets are enforced, not only estimated. *Learned:* ICSA `campaign/gate.py:5-6`; WWW estimated cost per record but stopped on nothing (`run_pilot.py:1148`, `harness/agent/cost.py`). *Test:* a model without a price is held at the budget gate.

**C13. A release file counts once and only if newer than the stop it ends.** *Learned:* ICSA `campaign/release.py:1-6`. *Test:* a stale or reused release file does not resume dispatch.

**C14. Overrides reach execution.** The endpoint the campaign admitted is the one the run uses. *Learned:* ICSA `icsa/harness/run.py:63-72` ("admission checked server A while the harness file named B"). *Test:* a campaign override of the base URL is the URL in the run record.

**C15. Endpoint health is tracked per unit.** Pause a unit after a threshold of failures; clear the pause only on a success. *Learned:* ICSA `scheduler.py:2411-2490`. *Test:* N failures pause the unit; one success clears it.

**C16. An endpoint whose identity changed during a run makes the run infrastructure.** *Learned:* ICSA `scheduler.py:2254-2255`. *Test:* a slot fingerprint change between start and end retries the run.

**C17. An unexpected worker exception stops dispatch loudly.** *Learned:* ICSA `scheduler.py:2160-2169`. *Test:* a raising worker stops dispatch and leaves its run for recovery.

**C18. Two conditions never share attempt keys.** *Learned:* WWW A/B arms of one route model skipped each other's attempts (the 21 Oct tolerances result README, lines 152-154). *Test:* two conditions with the same model produce disjoint run keys.

**C19. Leases on shared mutable resources fail safe.** A dead holder's key is not re-granted until its sandbox is shown gone; an unproven teardown quarantines the resource; with every resource quarantined the run is unstarted, using no attempt; a file lock does not cross hosts, so a key leased on two hosts is refused; an unreadable reservation reads as held. *Learned:* ICSA `icsa/harness/slot_lease.py:55-58, 112-146, 208-215, 280-319`, `reservation.py:15-16, 105-132` (1c525365, 1 Nov; f1c242a8, 3 Nov). *Test:* each case with a fake resource and a fake reaper.

**C20. Store schema changes are crash-safe.** Add columns one at a time; read-modify-write shared metadata under `BEGIN IMMEDIATE`. *Learned:* ICSA `store.py:80-83, 111-122`. *Test:* a kill between two migrations reopens and finishes.

**C21. A snapshot of a live store is consistent.** Copy with `VACUUM INTO` over a read-only connection and check integrity; a pulled run directory counts only with a marker matching its attempt; the snapshot code must equal the campaign's. *Learned:* ICSA-snap `snapshot/fetch.py:5-16`, `snapshot/codecheck.py:1-12`. *Test:* a partial pull is refused; a code mismatch is refused.

**C22. Model-level infrastructure faults are confirmed before relabelling.** A trigger only holds a model; a frozen probe confirms the fault. *Learned:* WWW `harness/agent/infra_fault.py:1-80`, `fault_probe.py:1-35` (96 attempts of one model excluded; 5f817def, 21 Oct). Lower priority for v0.1. *Test:* WWW `tests/test_infra_fault.py`.

## P. Provenance

**P01. Unknown code is dirty code, and dirty code does not start a study run.** *Learned:* ICSOC recorded `unknown` silently (`runner.py:373-384`); ICSA read a failed `git status` as a clean tree (`analysis/run.py:30-39`) and fixed it (`icsa/codestamp.py:1-6, 84-111`; f1c242a8, 3 Nov). *Test:* a tree without git or stamp records `unknown`, dirty, and refuses start.

**P02. Code identity includes the data files the code reads, and is re-verified.** *Learned:* WWW `replay_check.py:52-71, 152-283`, `record.py:209-232` (330464f8, 27d28f58; 20 Oct). *Test:* editing a data file under a code path changes the identity.

**P03. Tree hashes ignore modification times and caches.** *Learned:* ICSA `codestamp.py:18, 27-42`. *Test:* touching a file or adding `__pycache__` leaves the hash unchanged.

**P04. Every output manifest carries the hash of its configuration and preregistration.** Test overrides mark the hash. *Learned:* ICSA `icsa/analysis/config.py:37-57`, `analysis/run.py:42-63`. *Test:* an override produces a hash ending in `+override`.

**P05. Scoring and inference code are frozen by digest and a known-answer test.** *Learned:* WWW `harness/score/digest.py:3-21`, `harness/analysis/kat.py` (scorer rule versions swap the digest, db8274c8, 21 Oct). *Test:* changing a frozen module fails the digest check.

**P06. One model table.** *Learned:* ICSA keeps it in three shapes with three naming schemes (`config/models.toml`, `icsa/campaign/config.py:16-39`, `config/harness.toml:55-76`, bridged by `config.py:131-141`); ICSOC kept three (`scored_sweep_driver.py:181-183`). *Test:* every model named anywhere in configuration resolves to exactly one table entry.

**P07. A replication package is not a code source.** *Learned:* this inventory: ICSA's anonymised build changed behaviour (see [`inventory.md`](inventory.md#icsa-replication-package-versus-the-code-that-ran)). *Test:* applying the private study-token map to the source blob named in a module's provenance header reproduces the module's import commit, minus the header; every pushed commit passes the study-token check ([`architecture.md`](architecture.md#provenance-of-extracted-code)).

## S. Statistics and cause coding

**S01. Float noise is snapped before rank tests.** *Learned:* ICSA `icsa/analysis/contrasts.py:49-54, 190-201` (a dry run moved p from 1.12e-5 to 6.09e-6; ruling #14). *Test:* differences equal in arithmetic tie regardless of summation order.

**S02. Holm keeps the declared family.** A missing or barred test counts as p = 1 and stays in the family. *Learned:* ICSA `contrasts.py:228-246`; WWW `harness/analysis/prereg.py:6-7`. *Test:* a family with a NaN keeps its size m.

**S03. Degenerate contrasts are defined.** All-zero differences give p = 1; dropped zero differences and dropped clusters are counted. *Learned:* ICSA `contrasts.py:204-220, 255-258`. *Test:* each degenerate input returns the documented values.

**S04. The cluster bootstrap resamples clusters once for all models.** *Learned:* ICSA `contrasts.py:168-187`. *Test:* the resampled cluster ids are identical across models within a replicate.

**S05. An undefined resample is NaN and never supports the hypothesis.** *Learned:* WWW `harness/analysis/stats.py:7-13, 43`. *Test:* a replicate with an empty arm yields NaN, not 0.

**S06. Multiplicity levels are exact at the boundary.** *Learned:* WWW `harness/analysis/multiplicity.py:86-88`. *Test:* a p-value equal to its level is rejected exactly as declared.

**S07. Kappa reports when it is undefined.** NaN when expected agreement is 1; a bootstrap interval is NaN when fewer than half the replicates are valid; blank, duplicate or missing ids raise. *Learned:* ICSA `icsa/causes/kappa.py:13-65`. *Test:* identical single-code ratings give NaN.

**S08. Blind coding sheets cannot leak the condition.** Shuffle rows under opaque ids, keep the key apart, and rename copied files. *Learned:* ICSA `icsa/causes/sampling.py:92-162`. *Test:* no condition or run key appears in the sheet or its file names.

**S09. The power simulator is independent of the pipeline it checks.** *Learned:* ICSA `icsa/analysis/sim.py:3-4, 72-81`. *Test:* an import-graph test, and refusal of parameters that would clip probabilities.

## O. Platform (Onedata)

**O01. Transfer status is not evidence of placement.** Only the block map (`GET /data/{id}/distribution`) is; Onedata 25.0 reports `completed` for a replication that placed zero bytes and an eviction that removed none. *Learned:* ICSOC onedata-issues 0003 and 0006; ICSA `icsa/scripted/placement.py:1-5`, `provision.py:4-5, 219-220`, `mplus.py:9, 180-184` (34b0ec84, 1 Nov). *Test:* `replicate()` returns success only when the block map covers the range, against a fake that reports `completed` with zero blocks.

**O02. Replication moves whole files.** Place byte ranges by reading them through the receiving provider. *Learned:* ICSA `scripted/s2ranges.py:3-4`, `mplus.py:12-14, 144-152`. *Test:* a range placement issues range reads, never a transfer job.

**O03. Provider discovery returns every provider of the space.** Pin the agent to its declared local provider, or refuse. *Learned:* ICSA `icsa/sandbox/sandbox.py:492-506` (unreachable in campaigns only because `route.py:77-79` refuses a configuration without a named provider). *Test:* a two-provider space yields only the pinned provider; no pin raises.

**O04. Health checks take the expected providers from configuration.** *Learned:* ICSA `icsa/harness/health.py:6-7, 64-65` requires exactly one supporting provider, pinned by `tests/harness/test_campaign_glue.py:169-171`; multi-provider federations need the opposite. *Test:* a declared multi-provider support set passes; an exclusive policy rejects a second provider.

**O05. The data path is read-only, enforced fail-closed.** Mint tokens with read-only and path caveats; examine every token before use and refuse on any failure; enforce again at the gate and with a read-only mount. Never rely on the MCP server's token policy, which keeps write tools when the examination fails. *Learned:* ICSA's six enforcement points ([`inventory.md`](inventory.md#read-only-enforcement-in-icsa)); OMCP `onedata_mcp/token_policy.py:65-91` fails open by design. *Test:* a token without the read-only caveat, or an examination that raises, refuses the run.

**O06. Token lifetimes follow the run.** TTL is derived from the run's time cap and checked before minting; tool-server tokens are distinct from run tokens. *Learned:* ICSA `config.py:221-234`, `tokencheck.py:20-24, 66-84`, `real_sandbox.py:73, 109` (0a2faf01, 1 Nov). *Test:* a token shorter than time cap plus margin is refused.

**O07. Eventual consistency is waited out, with bounds.** Transfers are visible only once ENDED; a new space root can return ENOENT; convergence needs two consecutive matching polls. *Learned:* PPAM `fixture_runner.py:85-96, 245-288` (ae18b6e); ICSOC `harness/fixtures.py:49-64` (e4cdde3), `provision.py:368-451`, `verifier.py:475-553`; ICSA `provision.py:311-320`. *Test:* a fake federation that flips after k polls returns; one that never flips raises at the bound.

**O08. A probe that fails is recorded as failed, never guessed.** Do not retry real HTTP answers; retry network resets a bounded number of times. *Learned:* ICSOC `harness/verifier.py:39-45, 132-171, 385-418` (7ec2b62). *Test:* a 404 is one request; two resets then 200 give the value; all failing gives `probe_ok=False`, value `None`.

**O09. Mutations are scoped.** Refuse to act on a space outside the configured prefixes; refuse to evict the data provider's own copy; tear down only logged ids; a teardown that finds files left revokes nothing. *Learned:* ICSA `provision.py:33-55, 206-210, 323-333`, `harness/slot_users.py:7-13`. *Test:* each refusal with a fake API.

**O10. Destructive operations need explicit sanction.** *Learned:* ICSOC `harness/sweep.py:235`, `provider_restart.py:279-286`. *Test:* a fault injector without the sanction flag raises.

**O11. Faults and fixtures are cleared in `finally`, and clearing is verified.** "Ready" is not "restarted": confirm by a changed identity. *Learned:* ICSOC `sweep_driver.py:130-131`, `netem.py:85, 362-391` (24e2f5e), `provider_restart.py:87-104` (f627555). *Test:* a raising trial still calls clear and teardown.

**O12. Federation names live in configuration only.** *Learned:* ICSA hard-codes them in at least six modules (`day0/scripts/od.py:26-34`, `harness/real_sandbox.py:27-34`, `slot_users.py:29-33`, `scripted/federation.py:21-41`, `knowledge/launch.py:21-22`, `knowledge/live_equality.py:26-29`), and `campaign/boverride.py` (845 lines) exists only to patch them in memory (d867010d, 4 Nov). *Test:* the hashed study-token test over the platform layer.

**O13. Sandbox images are pinned by resolved id.** *Learned:* ICSA `sandbox.py:283-288` (7c70f8f0, 1 Nov). *Test:* a moved tag refuses start.

**O14. Each sandbox gets its own small network from a dedicated pool.** *Learned:* ICSA `icsa/sandbox/netpool.py:3-39` (Docker's default pools ran out at concurrency 16). *Test:* 16 concurrent creates get disjoint networks.

**O15. FUSE teardown is ordered.** Readers, then unmount, then sidecar, then agent; the host froze otherwise. *Learned:* ICSA `icsa/sandbox/teardown.py:1-30` (f1c242a8, 3 Nov). *Test:* the teardown order.

**O16. The data gate makes no errors of its own and marks cut transfers.** *Learned:* ICSA `icsa/sandbox/gate.py:23-32` (1761261d, 1 Nov; e43b9f46, 3 Nov), `gatepolicy.py:15-18, 60-75`. *Test:* concurrent reads give no gate-origin 5xx; a client closing mid-body yields a close line with bytes unread.

**O17. Kill causes come from the kernel, never the exit code.** *Learned:* ICSA `icsa/sandbox/killcause.py:1-34`, `infradeath.py:1-42`, `earlyoom.py:1-14`. *Test:* each cause fixture maps to its category; unknown is retried within the attempt limit.

**O18. Errors the agent caused are not platform errors.** *Learned:* ICSA `icsa/sandbox/oneclient_log.py:30-45` (93fe68df, 1 Nov). *Test:* ENOENT, EROFS and EISDIR on a client log line are not sidecar errors.

## K. Replication packages

**K01. A check with no input fails.** *Learned:* ICSA `tools/artifact/build.py:411-412`, `check_secrets.py:32-34`. *Test:* the secret check on an empty input exits non-zero.

**K02. Canaries run before every build.** A planted case per rule must still be caught. *Learned:* ICSA `tools/artifact/canaries.py:1-7`, `build.py:413-417`. *Test:* disabling a rule fails the canary run.

**K03. The private name list never enters the repository or the output.** *Learned:* ICSA `build.py:304-313`. *Test:* a private list under the repository root refuses the build.

**K04. Anonymisation must not change numbers, model names or parseability.** *Learned:* ICSA `build.py:315-330, 439-465`, `tools/artifact/tablecheck.py:1-6` (1ac2f29b, 5 Nov; 349199da, 6 Nov). *Test:* a mapping that alters a number, a model name or a parse fails the build.

**K05. Findings never carry the matched value.** *Learned:* ICSA `tools/artifact/scan.py:3-4`, `build.py:510-519`. *Test:* a planted secret's finding shows only its masked shape.

**K06. Archives are deterministic.** Sorted names, uid and gid 0, gzip without timestamp or original name; publish by rename after the stream closes. *Learned:* WWW `results/dev-natural-20261020/README.md:39-43`; ICSA 77622e79 and aca37ffb (5 Nov). *Test:* two builds of one tree are byte-identical.

**K07. `make check` regenerates every table from records and compares byte for byte.** *Learned:* ICSOC-pkg `Makefile:7-28`, `regenerate.py:33-58`; ICSA's package (6 Nov). *Test:* the example study's `make check` passes from a clean checkout.

**K08. Shipped code parses on the oldest supported Python.** *Learned:* ICSA 8a578931 (6 Nov; 3.12 f-string syntax broke 3.11 nodes). *Test:* a compile pass under the minimum version.

**K09. Temporary credential copies are removed.** *Learned:* ICSOC `harness/panel.py:134-154, 553` never removed per-leg config directories holding a credentials copy. *Test:* teardown deletes every temporary directory it created.
