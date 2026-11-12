# The Compose federation after the gate's parent walk, 10 Nov 2026

A second run on a fresh `up` of the same federation, after the mutation gate stopped reading a file's `path` attribute
and walked its `parent_id` links instead (F1), the day-0 `content_type` check stopped passing when it measured nothing
(F3), and `read_only` stopped reporting a write it never sent as one not refused (F2). The releases, the images, the
names and the layout are those of `2026-11-10-federation-facts.md`, which stays as the record of the first run. The
day-0 checks' own report is beside this file (`day0-20261110T214021Z.md` and `.json`, mutation allowed). "Unknown" means not
measured; it is never a pass. Every name below is the federation's own invented one.

## The walk, read directly

As the admin, through each provider's REST base, on the baseline:

| Read | Answer |
|---|---|
| `GET /spaces/{alpha-space}` on `pine`, `birch`, `cedar` | the keys `archivesDirId`, `dirId`, `fileId`, `name`, `providers`, `spaceId`, `trashDirId`; the same `fileId` on all three |
| `GET /data/{samples/sample-1.bin}?attribute=parent_id` | `{"parent_id": <the id of samples>}`, on each of the three |
| `GET /data/{samples}?attribute=parent_id` | `{"parent_id": <the root's fileId>}` |
| `GET /data/{root}?attribute=parent_id` | `{"parent_id": null}` |
| `GET /data/{file}?attribute=parent_id&attribute=file_id` | both keys, and nothing else |
| `GET /data/{root}?attribute=path` | 400 `badValueNotAllowed`, as on the first run |
| `GET /data/{file}` with no attribute | every attribute, `parent_id` and `parentFileId` among them, no `path` |

So the gate reads one link per level and stops at the root of the configured space, whose own link is null. The live
contract test `test_a_nested_file_resolves_to_its_space_by_its_parent_links` checks the same shapes on every run.

## What the gate's fix let through, read directly

With the gate no longer refusing every write by file id, the two state-changing checks reach the providers for the first
time. What they meet, read directly in `alpha-space` through `pine` (a scratch directory and file, removed again; the
listing of the root afterwards held no entry of the checks or of these reads):

| Request | Answer |
|---|---|
| `POST /data/{root}/children?name=...&type=DIR` as the data-path token (read-only caveat) | 400 `posix`, errno `eacces` |
| `PUT /data/{file}/content?offset=0`, an octet body with no `Content-Type` | 415 |
| the same with `Content-Type: application/octet-stream` | 204 |
| `DELETE /data/{file}` as the admin | 204 |
| `GET /data/{deleted file}`, three times in a row | 400 `posix`, errno `enoent`, each time |
| `GET /data/{deleted file}?attribute=parent_id` | 200, the old parent |
| `GET /data/{deleted directory}` | 400 `posix`, errno `enoent` |

Three facts follow, each outside the three fixes and left for a decision:

- **A refused write is a 400 here, not a 401 or 403.** Onedata 25.0 refuses a write made with the read-only token as
  400 `posix` with errno `eacces`. `read_only` counts only a 401 or 403 as a refusal, so it now reports the write as
  sent and answered otherwise (`400 posix`), unknown. It never was accepted.
- **The provisioner's content upload is refused with 415.** `OnezoneClient.mutate` sends a raw body with no
  `Content-Type`, and 25.0 refuses that; with `application/octet-stream` the same write is accepted. The round trip
  fails at its first upload (`PlatformError after nothing`).
- **A deleted entry reads as 400 `enoent`, not 404.** Teardown and `read_only`'s clean-up take only a 404 as "gone",
  so they wait out their bound (300 s per entry for the provisioner's teardown) and then report the entry as not
  removed, though the listing shows it gone. The day-0 `round_trip` takes about ten minutes for this reason and says
  `scratch NOT removed`. The answer errs on the safe side: it never claims a removal it did not see.

A deleted file still answers its `parent_id`, so the gate resolves its space and lets a DELETE through, which the
provider then answers as it does for any missing file.

## Day-0 checks (`day0-20261110T214021Z.md`)

4 pass, 1 fail, 2 unknown, exit status 1. Against the first run (5 pass, 1 fail, 1 unknown), `content_type` moved from
a pass that measured nothing to unknown, and `read_only` now reports a write that was sent.

- **reachability, health, tokens, listing: pass**, as on the first run.
- **content_type: unknown.** No `[sandbox]` is configured, so no data path is REST and the check read nothing; it now
  says so instead of passing (F3). The same check, given a REST `[sandbox]` by the live test below, measured a file
  two levels down through the gate's walk and passed.
- **read_only: unknown.** The gate resolved the space root and let each write through; every provider answered it
  400 `posix` (`eacces` when read directly, above). No write was accepted. The check counts only 401 and 403 as a
  refusal, so it reports the answer as it came instead of a refusal it could not read (F2).
- **round_trip: fail.** The first upload of the scratch file is answered 415 (no `Content-Type`), before any step; the
  scratch was then deleted, and the teardown, reading a 400 `enoent` where it waits for a 404, reports it not removed.
  The listing of the root holds no `day0-` entry afterwards.
- The tool-server plane was not checked: no tool server is installed.

## Live contract tests

`EXPCORE_LIVE_PLATFORM=<root>/platform.toml EXPCORE_LIVE_CONTENT_PATH=hello.txt EXPCORE_LIVE_MUTATE=1
EXPCORE_LIVE_SLOW=1 uv run pytest tests/live/test_contract.py`: 12 passed, 2 failed, 1 skipped, in 616 s (the round
trip's teardown waits out its bound twice). Against the first run (10 passed, 3 failed, over `tests/live`, whose
sandbox module skips), the failing path test is replaced by the two tests of the walk, which pass. The two
state-changing checks still fail, now on what 25.0 answers past the gate rather than at the gate.

| Test | Result |
|---|---|
| a user mints its own token, examined cleanly (`subject.id`) | pass |
| the study space resolves to its declared name (`GET /spaces/{space}`) | pass |
| a nested file resolves to its space by its parent links, on each provider (`GET /data/{id}`) | **pass** (new) |
| the pre-campaign content check measures a nested file through the walk, and passes | **pass** (new) |
| discovery finds the space and the local provider (providers' `name`, `online`) | pass |
| the health check reads the declared support set | pass |
| a file's content, whole and by range, is `application/octet-stream` (200, 206) | pass |
| a token stops working when its lifetime ends | pass: refused with 400 `tokenCaveatUnverified` |
| the read-only day-0 checks do not fail | pass |
| a block map has the documented shape | pass |
| the transfer listing answers every state, with a `Date` header | pass |
| a transfer record has the fields the probes read | pass |
| the read-only day-0 check, run as a test | **fail**: unknown, every write answered 400 `posix` (a refusal 25.0 gives as `eacces`) |
| the round-trip day-0 check, run as a test | **fail**: the first upload answered 415, and the teardown cannot read 400 `enoent` as gone |
| one test per tool-server condition | skipped: no tool server, so no condition |
| the sandbox tests (`EXPCORE_LIVE_DOCKER`) | not run: a sandbox's containers and networks are outside this project |

After the run the root of `alpha-space` lists its four baseline entries on each provider and no entry of a check.
