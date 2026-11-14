# The combined live slot on the Compose federation, 14 Nov 2026

One slot ran items 0 to 16 of the slot plan on the **25.0 development build (op-worker f118ed9)**, from 01:09Z to
02:21Z. Items 0 to 15 used one federation, taken down and verified at 02:05Z. Item 16 (#246, merged at 01:56Z) used a
second, fresh federation, taken down and verified at 02:21Z. "Unknown" means not measured. No token, id, address,
address range or host path is recorded here; every provider, space and user name is the slot layout's own invented one.

**What changes code:**

1. **A provider outage left one rtransfer link stuck, and up's link check cannot see it.** While `birch` was stopped
   through the fault injector (#212's test), `pine` logged "Failed to renew connections to provider 'birch'" and its
   `rtransfer_link` server crashed ("Generic server rtransfer_link terminating"); its link process came back about
   three minutes later. From then on, every fetch by `cedar` of a file only `pine` held timed out at 60 s, while the
   five other ordered pairs fetched in under 0.6 s. `cedar`'s link log showed "Fetch info promise failed due to
   std::runtime_error: canceled", then "Fetch for request ... failed", for each such probe, and `cedar`'s link process
   never restarted. Up's link check (#221) passed throughout, since it checks only the local provider's pairs, and
   `pine` was the local provider. Stopping `cedar` and running `up` again, which started it, fixed the pair: all six
   pairs passed.
2. **Re-ups do not break links.** Three re-ups, each adding one user, and one re-up adding a space's support on two
   running providers recreated no container. Each was followed by a check of all six pairs. None broke a pair that
   worked, and none repaired the stuck one.
3. **#246's mirror restore failed live.** After a preallocated start, the reset replicated the completed file to the
   mirror. The transfer reported `completed`, but the mirror's block map held the prefix alone: 1,577,178 B short,
   which is FINAL less the prefix. It did not converge within `settle_timeout_s` (300 s; the restore took 306.1 s).
   The extend-first order and the in-place completion on the data provider passed.
4. **Five live-test defects; in each case the behaviour under test held.** #209(b) acted through a mirror before
   dbsync had reached it. #236's refusal check rejects `eperm`, the error the platform returns, and its argument filler
   cannot fill `qos_id`. #227(b) compares a quoted mountpoint with an unquoted one. #212's O34 expects its kill step at
   exactly 0.0 s.

## The run

| Fact | Value |
|---|---|
| Build | 25.0 development build (op-worker f118ed9), the pinned images |
| Core | `main` at `8a3ea7e` for items 0 to 15, except #236's test, run from `feat/181-readonly-views` at `84b2a71`; `main` at `1655851` (#246 merged) for item 16 |
| Layout | a slot layout, never committed: three providers, `pine` the agent's local provider and the study space's owner; four agent spaces supported by `pine` (`alpha-space` mirrored by `birch` and `cedar`, `beta-space` owned by `birch`, `gamma-space` owned by `cedar`, `delta-space` on `pine` alone until step D); the probe space `link-probes`; the data-path user a member of all four agent spaces; the tool-server user holding exactly the read-only condition's rights (`space_view`, `space_read_data`, `space_view_transfers`, `space_view_qos`) |
| Memory limits | 4g per service, 16.00 GiB for 4 services |
| Derived platform files | the rendered one; the same with a sanctioned `[faults]`; with a `[toolserver]` of the read-only condition; and a several-spaces form (`[[spaces]]`, four entries, local `pine`). The `[sandbox]` tables were derived as the live tests derive theirs |
| Tool server | onedata-mcp at the OMCP pin of `docs/inventory.md` (`8c9b10c`), from an existing local checkout; the read-only condition's 7-tool manifest frozen from it, SHA-256 `ec5565bc…` (the same bytes as the 12 Nov writing manifest) |
| Images in the sandboxes | the REST items (1, 14): a study's agent image (CPython 3.12.13) as agent and gate; the mount items (2, 5, 10, 15): `python:3.12-slim` and oneclient 25.0 by id; nothing pulled |
| Sandbox networks | a private IPv4 range the host routes nowhere |
| Host load | every pytest through the shared limiter; available memory 28 to 32 GiB throughout |

## Results

| Item | Result | Key facts |
|---|---|---|
| 0 `up` | pass on the second try | First try failed after 2.5 min: "GET pine panel storages: answered 500 (errorOnNodes)", all four containers healthy right after. The second `up` picked up where the first stopped and passed: 112 s (spaces 12 s, baseline 84 s, links 13 s). Its link check passed: `birch`→`pine` 0.44 s, `cedar`→`pine` 0.42 s, 65,536 B each |
| 1 #213 published client through the gate | pass | `tests/live/test_rest_path.py` 5 passed in 10.4 s (o08 now passes). In an A+ sandbox, `onedatarestfsspec` 25.0.0 used the placeholder token and opened `onedata:///alpha-space/hello.txt` with no provider given. The listing held the file, and the client read 64 of 64 B, SHA-256 equal to the host's read. Gate log below |
| 2 #212 sidecar abort | 4 of 5 pass; the behaviour held | O34 failed only on `kill t_s == 0.0` (it was 0.127 s). Below |
| 3 #209 (a) `include_directories` | pass | the recursive listing with `include_directories` came back in 2 pages of 1,000 (1,210 entries) |
| 3 #209 (b) requirement through another provider | test failed; the fact holds | the test's POST went through the mirror before the mirror knew the file: `OutOfScope` ("unknown to its provider"). By hand, with the mirror first shown to know the file (5.1 s after its creation), the requirement added through the mirror was found by the data provider's listing after 3.89 s (2 polls) and applies to the file |
| 4 #209 occupancy lag | pass | depth 2 (the test): 10.76 s against 10 + 5·1 = 15 s; depth 3 (by hand): 15.91 s against 20 s; an eviction's: 20.03 s from its scheduling to the root's size dropping, the eviction job's own run included (not near 0) |
| 5 FUSE split (oneclient `-d`) | measured | INIT and READ sizes below; reads of at most 1 MiB, six for one 5 MiB `pread` |
| 6 #207 global clock | pass | anonymous `get_current_time` answered `{"timeMillis": N}`, offset from the host −0.007 s, half trip 0.026 s, the answer's Date header −1.23 s; the transfer listing by state and an ended record read. The space held only 6 ended transfers: "many earlier ended transfers" not reached |
| 7 #217 eviction counters | pass | one eviction of a directory of 3 files from the mirror: `filesProcessed` 3, `filesEvicted` 3, `filesFailed` 0, completed, 1 attempt, 16.2 s |
| 8 #196 default chunk | pass | 50 MiB: PUTs 1 to 3 of 16,777,216 B each on a new connection, PUT 4 of 2,097,152 B on a reused one; read back by hash |
| 9 does `up` break links | no; an outage does | below |
| 10 #227 several spaces, one mount | (a) pass, (b) fail, (c) pass | (a) one sandbox, one oneclient, a 65,543 B file from each of the four spaces read whole and matching, 0.083 to 0.134 s each. (b) only "Mountpoint: differs": oneclient logs `Mountpoint: "/mnt/oneclient"` (quoted), the argv gives it unquoted; 14 options logged, 11 expected; no variable but the token's reads an option. (c) the `--help` parse found the image's config path; the file recorded `unread` |
| 11 #221 link check live | pass | `tests/live/test_link_check.py` 1 passed in 75.5 s: two checks, each pair evicted cold first, fetched 65,536 B in 0.06 to 0.09 s, evicted after; the second check wrote no probe |
| 12 #224 revocation | pass | the health user's token not revoked before, its generation rose, then 400 `tokenRevoked`; after the 330 s settle a new token of that user minted and verified as not revoked. A slot freed through a lease pool: not exercised (this layout declares no `[slots]`) |
| 13 #236 read-only tool server | 2 pass, 3 fail, 1 skip; the design's refusal holds | below |
| 14 #234 bound that fired | pass | `test_sandbox.py -k "o23 or o39"` 2 passed in 20.1 s: `exec(record=False)` of a cell sleeping 3,600 s under a 2 s bound reads `timed_out`, bound `container_timeout`; a self-kill exits 137 with no bound |
| 15 #107 tail hole | pass | below |
| 16 #246 preallocated mode | 2 pass, 1 fail | on a fresh federation: `up` 276 s; `tests/live/test_ingest_preallocated.py` took 397.3 s. The extend-first order and the start, parts and in-place reset passed; the mirror's restore failed. Below |

## 1. The gate log of the published client's read

Every call went through the gate; each decision was `allow`, and the gate refused nothing.

| Call | Status | Bytes |
|---|---|---|
| zone token-scope inference (POST) | **200** (401 on 13 Nov) | 1,133 |
| provider children (GET) | 200 | 565 |
| `lookup-file-id` (POST), four times | 200 | 193 each |
| file attributes (GET), three times | 200 | 106, 11, 106 |
| content (GET) | 206 | 64 |

The run-start mark came first and `teardown_complete` last. The collection counted 2,757 B served: 64 content, 565
listing and 2,128 other.

## 2. #212: the read held by a stopped provider

`birch` was stopped through the injector, with `pine` local. The file was 16 KiB, written through `birch` alone and
seen at its size through the mount first. The cell's cap was 20 s and its grace 5 s; the margin was 30 s.

| Step (seconds after the cap) | Fact |
|---|---|
| kill | 0.127 s, 2 processes signalled |
| survived | 5.127 s, 1 left, state D |
| abort from the sidecar | 5.319 s, ok, 1 FUSE connection |
| ended | 5.615 s |

The outcome was `mount_aborted`, with error kind `cell_unkillable`, kill reason `kill_survived` (infrastructure) and
exit 137. The whole exec took 25.73 s against the bound of 50 s (cap plus margin). A later cell was refused as
`cell_unkillable`. The injector restored `birch` (the same container, started again). After the test the sandbox was
stopped, with 0 FUSE mounts and 0 files left.

The first attempt ran without `EXPCORE_FED_ROOT` and failed before the injection: the injector could not list the
service's containers, because the generated Compose file interpolates that variable. Nothing had been stopped.

## 5. The FUSE split

The oneclient was started with `-d` through `[sandbox.mount] extra_args`, and only the INIT and READ lines were taken
from its output.

- **The kernel's INIT:** protocol 7.39, flags `0x73fffffb`, `max_readahead` `0x20000`.
- **oneclient's INIT reply:** protocol 7.31, flags `0x00409029`, `max_readahead` `0x00020000`, `max_write`
  `0x00100000` (1 MiB), `max_background` 0, `congestion_threshold` 0, `time_gran` 1. The flags include
  `FUSE_MAX_PAGES`, but this debug output does not print `max_pages` itself: its value is unknown.
- **One 5 MiB `os.pread` at offset 0** took 0.156 s and its hash matched. It produced **six FUSE READs**, whose replies
  were 1,048,528 B, then four of 1,048,576 B, then 48 B, so no read exceeded 1 MiB. Splitting at 256 pages of a user
  buffer that begins 48 B into its first page would give these sizes (inference).

## 9. Links and `up`

Each check covered all six ordered (local, source) pairs. Each pair got a fresh 16 KiB file in the space, written
through the source alone and read once through the local provider with a REST range read and a 60 s bound. No read
could be served from a replica placed by another pair's read.

| When | Up | Containers | Pairs |
|---|---|---|---|
| after C (the outage), twice | — | `birch` started again by the injector | 5 ok (0.04–0.55 s); **`cedar` ← `pine` timed out at 60 s** |
| D1: up, one user added | 35 s, exit 0 | none recreated | the same |
| D2: up, one user added | 33 s, exit 0 | none recreated | the same |
| D3: up, one user added | 31 s, exit 0 | none recreated | the same |
| D4: up, `delta-space` supported by `birch` and `cedar` as well | 35 s, exit 0, support added | none recreated | the same in `alpha-space`, and in `delta-space` (the space just supported) |
| extra: `cedar` stopped, then up | 93 s (providers 47 s) | `cedar` started again, the same container | **all 6 ok** (0.04–0.26 s) |

Each up's own link check passed, `birch`→`pine` and `cedar`→`pine` taking 0.06 to 0.38 s.

The provider logs were read after each step, as counts only:

- "Resolved" never appeared on any provider.
- "Failed to renew connections" appeared only during the two stops. During `birch`'s stop, `pine` and `cedar` logged
  2 each. During `cedar`'s stop, `pine` and `birch` logged 2 each.
- `pine`'s error log held its `rtransfer_link` crash from `birch`'s stop. `cedar`'s stop caused no crash.

None of the re-ups added a line to any of these counts.

## 13. #236's read-only condition

The test ran on the branch with the tool server launched as a run's is. `EXPCORE_LIVE_OUTSIDE_SPACE` named
`beta-space`, which `pine` supports and the tool-server user does not belong to. The branch predates `[link_check]`,
so its platform file left that table out.

- **Fail:** the view tools stopped at `get_qos_requirement`: "its input schema requires qos_id, which the test cannot
  fill". `live_support.ARGUMENT_KINDS` has no word for `qos_id`.
- **Fail:** the direct transfer and QoS writes stopped at "a cancel: failed, but not as a refusal (404, notFound)". The
  running replication of a small file was not found when the token cancelled it.
- **Fail:** the direct data writes stopped at "a file's creation: failed, but not as a refusal (400, posix, eperm)".
  `live_support.refused` accepts 401, 403 or 400 `eacces` only.
- **Pass:** the write tools answer an error and change nothing; the outside space's root and transfer listing are not
  served. **Skip:** the outside space lists no ended transfer.
- **By hand, the same token straight at `pine`:** creating a file, PUT of content at offset 0, an empty PUT at offset
  4,096, creating a directory and a delete each answered **400 `posix` `eperm`**. The file's content (by digest) and
  the directory's entries were unchanged. So the token is refused data writes because its user lacks
  `space_write_data`, as the design requires. The refusal is `eperm`, not `eacces`.

## 15. The tail hole

The owner `pine` received a 1 MiB prefix with `offset=0`, then an empty-body
`PUT /data/{id}/content?offset=8388608` (`Content-Type: application/octet-stream`, `Content-Length: 0`).

| Check | Fact |
|---|---|
| The empty PUT | 204 |
| `GET /data/{id}` | size 8,388,608 |
| `/distribution` | `pine` blocks `[[0, 1048576]]`, `physicalSize` 1,048,576, `virtualSize` 8,388,608; `birch` and `cedar` blocks `[]`, `physicalSize` 0 |
| REST reads of the hole (4 KiB at 2 MiB and at FINAL−4 KiB) | 206, all zeros, through `pine` (0.017–0.019 s), `birch` (0.014–0.036 s) and `cedar` (0.022–0.036 s); `birch`'s distribution afterwards still shows no block of its own |
| Mount reads of the hole (4 KiB at 2, 5 and FINAL−4 KiB) | all zeros, 0.003–0.008 s each (0.006–0.016 s in a first run); **no** "Cannot synchronize block" line in oneclient's INFO log, where the source study expected one per read and about seven SynchronizeBlock round trips |

In the first run the mirrors were read before dbsync had brought them the file. They answered 400, and the mirror's
distribution answered 404.

## 16. #246's preallocated mode

The test used a fresh `up` of the same layout and the rendered platform file, from 02:13:23Z to 02:19:58Z. FINAL was
2,101,473 B and the prefix 524,295 B.

| Test | Result | Facts |
|---|---|---|
| Extend-first: an empty write at FINAL, then the prefix | pass | Right after the empty write the file showed size FINAL, with no block on any provider. After the prefix was written at 0, the data provider `pine` held `[[0, 524295]]` and `birch` and `cedar` held nothing. The gap (4 KiB at FINAL−8 KiB) read zeros through `pine` in 0.018 s, `birch` in 3.643 s and `cedar` in 0.078 s, and no block map changed |
| The instrument: start, parts from the first turn, reset in place | pass | `start_s` 23.652. At the start the file was at FINAL with the prefix only, seen on `birch` after 6.60 s and on `cedar` after 0.02 s; its block map read 2.07 s. The first turn came 0.039 s after the start. Both parts were written (part 1 0.061 s late) and confirmed. The reset took 2.176 s and completed the file in place, keeping the id the start made |
| The reset restores a mirror's replica | **fail** | `start_s` 22.912. The start found the baseline's replica on `birch` (`held`) and made the file again, at FINAL with the prefix only, seen on `birch` after 6.08 s. The one part was written 2.08 s after the start and confirmed on the data provider (`[[0, 2101473]]`). The restore then ran 306.1 s and failed (`RestoreFailed`, `NotPlaced`): "the transfer reported {'completed': 1} but the block map does not cover the file (1577178 B short)". The mirror's block map did not cover the file within `settle_timeout_s` (300 s). The shortfall is exactly FINAL less the prefix, so the mirror held only the prefix after a replication that reported completion |

The cause is inferred, not proven: the replication seems to have copied the data provider's blocks as the mirror last
saw them, the prefix alone, and not the completed file.

## Memory per service

| Service | Declared limit | Cgroup limit | Peak at the first down | Peak at the second down |
|---|---|---|---|---|
| `zone` | 4g | 4.00 GiB | 1.23 GiB | 1.00 GiB |
| `pine` | 4g | 4.00 GiB | 1.31 GiB | 1.18 GiB |
| `birch` | 4g | 4.00 GiB | 0.87 GiB | 1.01 GiB |
| `cedar` | 4g | 4.00 GiB | 0.76 GiB (since its restart; 1.05 GiB before) | 1.03 GiB |

## Teardown

Each `federation.py --layout … down` exited 0, in 37 s and 36 s, and removed the volumes. After each:

- no `expcore`, `fedsbx` or `fedrest` container, network or volume was left, and no volume labelled with the project;
- Docker held 0 volumes before the slot and 0 after;
- the data root was gone, and no FUSE mount was left;
- the host's networks and the other study's containers were as they were before the slot.

## Not measured

- A slot freed through a lease pool after revocation.
- The window of ended transfers on a space with many earlier ones.
- FUSE `max_pages` as a number.
- Whether `up` breaks a link on a federation that had no outage first. Every D step began with one pair already stuck.
