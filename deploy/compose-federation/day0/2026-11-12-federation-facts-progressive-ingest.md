# The Compose federation's facts, 2026-11-12: the progressive ingest, live

Step 2 of the bundled live slot, on the 25.0 development build (op-worker f118ed9), for #175 (O27). This is the first
run of `expcore_onedata.ingest.ProgressiveIngest` on a live federation.

**Every item the Instrument section of `docs/interfaces.md` asks a live check to show held**, in nine runs of the
instrument (16:33 to 16:51 UTC). A removed file's id answered `400 posix enoent` on every provider before the run. Each
of 47 parts appeared at its size on the owner, the local provider and the third provider, and read back with the
plan's hash of that prefix. Every one of the 78 recorded ids either resolved to the study space by its parent links
or was answered absent. After `finish`, each "complete" file had its staged copy's SHA-256 and the owner's block map
covered it, and nothing else in the space changed. **None of the 34 names written again after the absence check lost
its file.**

**444 timed reads, none over 5 s.** Every read was correct, the slowest took 0.794 s, and reads after a re-creation
were no slower than the control's beyond the local provider's first fetch of the new file.

**Fail closed:** with one staged copy damaged before `finish`, the instrument kept that file, failed its restore alone,
restored the other files, and `check()` raised `InfraError("ingest_failure")`, which `finish_instruments` recorded
as the attempt's refusal. **A refused write** (a user without write rights, `400 posix eperm`) was recorded the same
way.

Every name below is a role, never a configured name; no id, token, address or host is recorded. Ids appear only as
counts.

## The slot

| Fact | Value |
|---|---|
| Build | 25.0 development build (op-worker f118ed9), the pinned images |
| Layout | the study space owned by one provider (its data provider) and mirrored by two; the agent's local provider is one of the mirrors and owns no space; the owner also mirrors two other spaces |
| Driven as a study drives it | `start_instruments([ingest], identity)`, then `finish_instruments(record, [ingest])` over a stored record fixture; a provisioner of the instrument's own (its own change log, claimed by the instrument, never given to a teardown) and one journal across runs |
| Staged copies | a temporary directory on the federation's volume, removed at the end |
| Placement settings | the platform's: block map polled every 2 s, two consecutive polls to agree |
| Plans | a fresh directory of the study space. Main plan: three "complete" files (128 KiB, 512 KiB, and 3 MiB in three parts over 30 s) and two "absent" files (64 KiB, and 1 MiB in two parts over 10 s), due 10 to 55 s. Re-creation plan: six "complete" files of 64 KiB due 2 to 7 s. Control plan: six "absent" files of 64 KiB under fresh names. Fail-closed plan: one "complete" and two "absent" files |
| Timed reads | REST through the core's client (`OnezoneClient.get` of `/data/{id}`, `read_range` of its content), through the owner and through the local provider, on a keep-alive pool: 779 requests on 78 connections, counted by local port |
| Not covered | the mount. A sandbox in mount mode needs a derived `[sandbox]` platform, an address pool and two containers per launch, so it was not started; step 4 of the slot replays reads through the mount |

## The live claims

| Claim | What the run showed | Result |
|---|---|---|
| A file removed at `start` is absent on the providers before the run | After every start, every planned file was missing from its directory's listing on all three providers. Each removed file's old id answered `400 posix enoent` on all three: 102 of 102 readings (34 ids). An "absent" file has no id before it arrives, so its absence was read from the listings | pass |
| Each arrival appears at its size on the data provider and on the others, part by part | All 47 parts, read right after the instrument confirmed each one: the owner, the local provider and the third provider each showed the part's size (the three-part file at 1, 2 and 3 MiB, the two-part file at 0.5 and 1 MiB), and the content read through the owner and the local provider had the plan's SHA-256 of that prefix. The instrument's own `visible_s` reached at most 4.57 s | pass |
| Every recorded id answers while it exists and resolves to the plan's space; the removed ones answer absent | 155 id events, 78 distinct ids. After each run, 34 resolved to the study space by their parent links on both the owner and the local provider; 44 (the removed baselines and the "absent" arrivals) were answered absent on both; none other. Every id answered `GET /data/{id}` while it existed: all 162 timed attribute reads after an arrival or a restore, through the owner and the local provider, and the 47 reads of each part through the third provider | pass |
| The clock readings fall in the plan's order, with their lateness | In every run the `written` readings came in the plan's order with increasing `t_rel_s`. Lateness was 0.13 to 2.02 s in the main run (parts 5 to 15 s apart) and up to 21.9 s when six arrivals were due 1 s apart: each part is confirmed (two block-map polls, then each provider) before the next is written | pass; the lateness is the confirmation's |
| After `finish`: the staged SHA-256, the owner's block map, the staged copies and every other file unchanged | Each "complete" file read back its staged copy's SHA-256 through the owner and the local provider, and the owner's block map covered it, 34 of 34 times. Every "absent" file was gone from all three listings. Every staged copy kept its hash. The two bystander files in the directory kept their ids, sizes and hashes, and the 1,206 entries of the space outside the step's directory had the same ids, sizes and kinds before the main run and at the end of the step | pass |
| A name removed and written again after the absence check never loses its file | 34 re-creations: 3 in the main run, 30 in five re-creation runs, 1 in the fail-closed run. During the five runs a second writer added 201 entries to the same directory and removed 201, so it held 11 to 16 entries at the arrivals. Each re-created file read whole at its arrival and at its restore, which found it whole and sent nothing (`skipped`, 34 of 34). At the end, 2 to 16 minutes after each name's last re-creation, all ten names held their files whole under the id the instrument last recorded, through both providers | pass |
| A refused write is recorded as `ingest_failure` | An ingest whose provisioner held a token of the read-only data-path user: the arrival's create was answered `400 posix eperm`. The snapshot recorded the failure (phase `arrival`), `check()` raised `ingest_failure`, `finish_instruments` wrote `instrument_refusal` with that kind, no id was recorded, the restore passed and the directory's listing was unchanged | pass |
| Every removal checks its staged copy first | See the fail-closed path below: the damaged copy's file got no removal intent in the journal and stayed. Each of the 44 removals that went ahead was journalled as an intent before it was sent | pass |

## The fail-closed path

One "absent" file's staged copy was changed by one byte after all three files had arrived and before `finish`.

- `finish` restored the "complete" file (`skipped`, whole) and removed the other "absent" file. It left the damaged
  copy's file in place under the id it arrived with, and the journal holds no removal intent for it.
- The snapshot's `restore.ok` is false, with that file alone failed ("its staged source no longer has the plan's
  hash") and the others ok. `ok` is false.
- `check()` raised `InfraError` of kind `ingest_failure`, and `finish_instruments` recorded
  `instrument_refusal` with `infra_kind` `ingest_failure` naming the restore.
- With the staged copy put back, `restore()` on the same instrument passed and removed the file, and `check()` then
  passed.

## The timed reads

After every arrival, re-creation and restore, the file's attributes and then its content were read, each timed, through
the owner and through the local provider. No read took over 5 s, so no read needed its connection and the directory's
entry count logged; the counts were recorded at each arrival anyway (11 to 16 entries in the re-creation runs, 17 to 22
in the control).

| Set | Reads | Correct | Median | p95 | Max |
|---|---|---|---|---|---|
| After a re-creation | 136 | 136 | 0.024 s | 0.071 s | 0.794 s |
| After a restore | 136 | 136 | 0.016 s | 0.030 s | 0.105 s |
| After a first arrival of an "absent" file or a later part | 28 | 28 | 0.024 s | 0.102 s | 0.125 s |
| Control run: arrivals under fresh names, no re-creation, the same second writer | 24 | 24 | 0.031 s | 0.054 s | 0.058 s |
| Control: the two bystander files, no ingest, no second writer | 120 | 120 | 0.015 s | 0.032 s | 0.066 s |
| All | 444 | 444 | 0.018 s | 0.059 s | 0.794 s |

By provider, after a re-creation: the owner's attribute reads took at most 0.012 s and its content reads at most
0.085 s. The local provider's attribute reads took at most 0.026 s, and its content reads had a median of 0.053 s
(0.020 s after a restore, when it already held the bytes) and a maximum of 0.794 s. That first read through the local
provider fetches the new file from the owner. No plumbing read timed out and was tried again: `reads_retried` was 0 in
all nine snapshots.

## What a run costs

| Run | Complete files | `start` | Whole run |
|---|---|---|---|
| Main | 3 | 35.9 s | 120.8 s |
| Re-creation, five runs | 6 | 62.3 to 72.6 s | 107.5 to 118.6 s |
| Control | 0 | 0.4 s | 82.4 s |
| Fail-closed | 1 | 8.5 s | 38.1 s |

`start` costs 8.5 to 12 s per "complete" file at the platform's placement settings. Each file's restore is verified
(its hash read back, the block map polled twice, each provider's view of it), then its removal is confirmed absent on
every provider on two consecutive polls.

The owner's memory cgroup counted 2,203 `max` events at the end of this step, the number step 1 left, with no OOM.

## What this does not show

- The mount: not timed (above).
- Whether a removed file needs the wait at all. The ingest always waits for absence on every provider before a name is
  written again, as O27's precaution, so this run shows only that the files survive with the wait. A re-creation
  without that wait was not run here.
- One plan per kind of run, on one layout, at one placement setting.
