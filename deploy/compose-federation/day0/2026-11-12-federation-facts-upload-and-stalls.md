# The Compose federation's facts, 2026-11-12: chunked uploads that stall on a reused connection, and the hedged read test

Step 1 of the bundled live slot, on the 25.0 development build (op-worker f118ed9), for #170 and #186.

**Three of 289 ranged PUTs on reused keep-alive connections stalled; none of 336 on new connections did** (330
without keep-alive, 6 with it, where the pool opened a new connection). In each,
the body started and never finished sending, and the core's `Provisioner.upload` raised `PUT failed (ReadError)`
342.6 to 346.3 s into the PUT. No byte of the stalled chunk reached the file. In the two stalls whose server side was
sampled, the provider left about 390 KiB of the body unread in its socket and closed the connection about 300 s after
the request began; the client's send failed about 45 s after that.

All three stalls were PUTs of 32 or 64 MiB (3 of 31 such PUTs on reused connections); no PUT of 16 MiB or less stalled
(0 of 258 reused). 64 MiB passed its first three keep-alive runs, then stalled in its fifth; 32 MiB stalled in two of
three. **16 MiB is the largest chunk size with no stall in any run.** Every other PUT, at every size, was answered 204
within 0.23 s of its body's last byte. Each failed upload resumed from its landed size at 4 MiB and read back the
right SHA-256.

**The hedged read test did not reproduce a stall in 960 files**: three bursts of 320 through the provider that owns
one space and mirrors two, two of them right after a create pass in a mirrored space. No size read took over 0.78 s,
so the hedge never fired.

No line of op_worker's logs or of the container's log matched `tls`, `cowboy`, `ranch`, `alert`, `timeout`,
`closed`, `inactiv` or `error` (case-insensitive) in any run's window. Every name below is the layout's own invented
one; no id, token, address or host is recorded.

## The slot

| Fact | Value |
|---|---|
| Build | 25.0 development build (op-worker f118ed9), the pinned images |
| Core | `main` at `36e2351`, with #188's read retry |
| Federation up | begun 15:40:11 UTC, done 15:43:44 (212 s: prepare 5, zone 61, providers 74, spaces 9, baseline 63) |
| Measurements | 15:44 to 16:15 UTC |
| Layout (derived, not committed) | a zone and four providers. `pine` owns `alpha-space`, the study space, and mirrors `beta-space` and `gamma-space`: the only provider supporting three spaces. `larch` owns `beta-space` and `gamma-space`. `birch` and `cedar` mirror `alpha-space` only, and `cedar`, owning no space, is the agent's local provider. Two lease slots (a writing condition's and a read-only condition's users with the rights of the 12 Nov tool-server run) and a sanctioned `[faults]`. Memory limit 6g for `pine`, 4g for the rest; support 50 GiB per provider per space |
| Host | 16 CPUs, 55 GiB, shared with another, idle federation. Load average 3.5 before up, 30 to 51 during and after the 15,360-file pass of (b), 2.8 at 16:10 |
| Memory at 16:16 (`federation.py memory`, cgroup `memory.events`) | `pine` peaked at its 6.00 GiB limit and its cgroup counted 2,203 `max` events, with no `oom` or `oom_kill`; at that moment 1.5 GiB anon and 0.22 GiB file. The cgroup charges page cache, and the step wrote about 9 GB through `pine`. The other services peaked at 1.11 (zone), 1.28 (`birch`), 1.23 (`cedar`) and 1.65 GiB (`larch`), with 0 `max` events. When `pine` first reached its limit, relative to the stalls, was not recorded |

## Images

Read on 12 Nov with `docker image inspect`; they match the pilot's. The oneclient value is the one
`2026-11-11-federation-facts-mount.md` records for the same image.

| Image | Digest |
|---|---|
| oneprovider (`docker.onedata.org/oneprovider:ID-765309c84f`) | `sha256:b032a5f8a23831f9e8253b529f910209a6dbc0a6630e2c618dd8869f36315482` |
| onezone (`docker.onedata.org/onezone:ID-ba7a778696`) | `sha256:a48dbf5bfb90df65fc471db32c5e2abb374f4723aa24fa84d333d4c1f3484d25` |
| oneclient | `sha256:aff60c5ff78c99f4cb0c5d0acb5392b37a1fdd3cdd73b73bca9b640d067a51d8` |

## (a) Uploads by chunk size

**Method.**
- **The upload:** `Provisioner.upload` of one file of 209,715,200 random bytes (200 MiB), to a fresh name per run in
  `alpha-space` through `pine`, its owner. `chunk_bytes` 64, 32, 16, 8 and 4 MiB, and `request_timeout_s` 400. That is
  above the server's 300 s, so a server-side close shows as one, and below the default 900, to bound a client-side
  hang.
- **The instrument, from outside the core:** the client module's `_build_http` was swapped for a builder adding httpx
  event hooks and an httpcore trace, chained after the core's own trace on retried reads. The client still owns its
  pool, so a retried read still gets a one-shot connection. Per request it recorded:
  - the method, path shape, offset and bytes;
  - when the body started and finished sending, and when the response headers came;
  - the local port, whether the connection was new, the request's index on that connection, and its age;
  - the status or the exception class.
- **Keep-alive off:** `httpx.Limits(max_keepalive_connections=0)`. The port column shows a new connection for every
  PUT, 330 of 330.
- **The runs:** at each size, three runs with keep-alive and three without, from 64 down to 4 MiB (15:44 to 15:55). A
  size's series stopped at its first failure. A second batch repeated 32 and 64 MiB, three runs each way: with
  keep-alive from 16:00:49, as soon as the burst after the 15,360-file pass of (b) ended, and without it from 16:13.
- **After a failure:** the landed size and block map, read through `pine`. Then `upload` again through a 4 MiB
  provisioner sharing the change log, so it resumes, and the SHA-256 read back.
- **Sampled while each ran:**
  - `docker stats` of `pine` every 2 s (CPU, memory, network);
  - from the second batch on, `pine`'s established `:443` sockets with a non-empty queue (`ss` inside the container);
  - after each run, the growth of `/var/log/op_worker/*.log` and `erlang.log.*`, and `docker logs`, grepped.

| Chunk | Keep-alive | Runs: pass, fail (in order, batch 1; batch 2) | PUTs: on a reused connection, on a new one | Stalled | Body's last byte to 204, s (median, max) |
|---|---|---|---|---|---|
| 64 MiB | on | 4, 1 (P P P; P F) | 18, 0 | 1 | 0.079, 0.116 |
| 64 MiB | off | 6, 0 (P P P; P P P) | 0, 24 | 0 | 0.088, 0.148 |
| 32 MiB | on | 1, 2 (F; P F) | 13, 0 | 2 | 0.147, 0.221 |
| 32 MiB | off | 6, 0 (P P P; P P P) | 0, 42 | 0 | 0.082, 0.144 |
| 16 MiB | on | 3, 0 | 39, 0 | 0 | 0.063, 0.096 |
| 16 MiB | off | 3, 0 | 0, 39 | 0 | 0.051, 0.106 |
| 8 MiB | on | 3, 0 | 73, 2 | 0 | 0.041, 0.074 |
| 8 MiB | off | 3, 0 | 0, 75 | 0 | 0.041, 0.071 |
| 4 MiB | on | 3, 0 | 146, 4 | 0 | 0.026, 0.044 |
| 4 MiB | off | 3, 0 | 0, 150 | 0 | 0.026, 0.056 |

**Notes on the table.**
- The PUT counts leave out the resumes' 4 MiB PUTs.
- A passing run took 6.0 to 6.7 s at 64 MiB and 9.1 to 11.6 s at 4 MiB, read-back included. Without keep-alive it
  took up to 2.5 s longer at 4 MiB.
- Before the table's runs, one more 64 MiB keep-alive upload completed and verified: 4 PUTs on one reused connection,
  no stall. The measurement script failed after it returned, so the table leaves it out.
- Before every PUT the core's mutation gate read the space from Onezone (`GET /spaces/{id}`), on its own connection
  to the zone.
- A keep-alive connection to `pine` never served more than 30 requests; the next request went on a new connection.

**How likely is this by chance?** Suppose each PUT stalled independently with the same chance.
- All three stalls falling among the 31 reused PUTs of 32 MiB or more, out of 289 reused: probability about 0.001.
- Among the 97 PUTs of 32 MiB or more, all three falling on the 31 reused ones rather than the 66 new ones: about
  0.03.
- Over all sizes, all three falling on reused PUTs (289 of 625): about 0.1.

### The three stalled PUTs

| | Stall 1 | Stall 2 | Stall 3 |
|---|---|---|---|
| Chunk | 32 MiB at offset 32 MiB (2nd of 7) | 32 MiB at offset 96 MiB (4th of 7) | 64 MiB at offset 64 MiB (2nd of 4) |
| PUT began (UTC) | 15:46:03.691 | 16:01:01.097 | 16:07:11.150 |
| Connection | reused: its 9th request, 0.86 s old | reused: its 30th request, 11.8 s old | reused: its 22nd request, 9.1 s old |
| The PUT before it, same connection | 204, 0.08 s after its body | 204, 0.12 s after its body | 204, 0.10 s after its body |
| Body | started at +0.002 s, never finished | started at +0.0006 s, never finished | started at +0.0006 s, never finished |
| `pine`'s socket | not sampled | 397,460 bytes unread in every sample to +296.6 s; gone at +298.6 s | 401,696 bytes unread from +0.7 s to +297.3 s; gone at +299.3 s |
| Client | send failed (`WriteError`) at +342.6 s, then the response read (`ReadError`) | the same at +346.3 s | the same at +344.9 s |
| Landed (size; block map through `pine`) | 33,554,432 B; one block from 0 | 100,663,296 B; one block from 0 | 67,108,864 B; one block from 0 |
| Resume at 4 MiB | `resumed`: 176,160,768 B in 9.2 s, SHA-256 equal | `resumed`: 109,051,904 B in 10.1 s, SHA-256 equal | `resumed`: 142,606,336 B in 9.5 s, SHA-256 equal |
| `pine`'s CPU during the stall (2 s samples) | median 22 %; max 172 % in its first 15 s, 97 % after | median 23 %; max 218 % in its first 15 s, 122 % after | median 23 %, max 122 % |
| `pine`'s Couchbase during the stall (4 s samples) | median 3 ops/s, write queue 0 | median 3 ops/s, write queue at most 8 | median 3 ops/s, write queue at most 1 |

- **Socket sample times:** each marks the start of a sample that takes up to 2 s, so each close fell between about
  +297 and +301 s.
- **Hedged during stall 3:** at 16:12:11, while the client still waited (its error came at 16:12:56), three reads on a
  new connection to `pine` answered at once:
  - the stalled file's attributes, 200 in 0.019 s;
  - `/configuration`, 200 in 0.026 s;
  - the file's distribution, 200 in 0.008 s.
  That was 300 s into the stall; the provider-side socket was last seen at 16:12:08.4.

## (b) The hedged stall test

**Method.**
- **Per file:**
  - a create (`POST /data/{dir}/children`, type `REG`) in a new directory of `alpha-space` through `pine`;
  - one PUT of 1 MiB;
  - then `GET /data/{id}?attribute=size`, timed.
  All on one keep-alive httpx client (HTTP/1.1) with the admin's token, 40 files per directory, 8 directories per
  run, at a target of 10 files per second.
- **The hedge:** the first GET had a 120 s timeout. Had it waited 10 s, the same GET would have gone on a new
  connection while the first still waited, both logged. It never fired.
- **The passes:** empty files created in new directories of `beta-space` through `larch`, its owner (`pine` mirrors
  it), from 8 threads. The burst began as soon as the pass ended.
- **Sampled:** `pine`'s CPU every 2 s, and Couchbase's own bucket statistics in `pine` (its REST API, 4 s samples).

| Run | Before the burst | Burst (UTC), files per second | Size GET, s: median, p99, max | GETs over 1 s; hedged | `pine` CPU in the burst: median, max | Couchbase write queue in the burst: median, max |
|---|---|---|---|---|---|---|
| Control | nothing | 15:55:54 to 15:56:26, 10.0 | 0.007, 0.020, 0.032 | 0; 0 | 274 %, 310 % | 129, 470 |
| After a 4,000-file pass | 4,000 files in 18.6 s (15:56:37 to 15:56:55) | 15:56:55 to 15:57:39, 7.3 | 0.008, 0.037, 0.774 | 0; 0 | 524 %, 794 % | 6,750, 16,600 (pass and burst) |
| After a 15,360-file pass | 15,360 files in 103.8 s (15:58:03 to 15:59:46) | 15:59:47 to 16:00:44, 5.6 | 0.009, 0.049, 0.149 | 0; 0 | 563 %, 750 % | 15,100, 22,700 |

- **Result:** not reproduced in 960 files (2,907 requests to `pine`). Every create succeeded, every PUT answered 204,
  and every size read answered 200 with the size written.
- **The rate:** under the pass's load the per-file sequence slowed, so the two later bursts ran below the 10 per second
  target.
- **Connections:** each run's 969 requests to `pine` used 33 connections. Each served exactly 30 requests except the
  last, and the oldest was 1.0, 4.6 and 4.8 s old at its last request.
- **Couchbase in `pine`:**
  - no `ep_oom_errors` or `ep_tmp_oom_errors` at any point;
  - `mem_used` peaked at 155 MB, during the burst after the 15,360-file pass;
  - `curr_connections` 40 to 41;
  - the write queue peaked at 34,400 during the 15,360-file pass;
  - `vb_avg_total_queue_age` peaked at 22.7 in the burst after it.

## Not measured, or unknown

- **The handler:** what op_worker's request process was doing while a body went unread. No process state or stack was
  taken.
- **The gap of about 45 s:** between the provider's socket disappearing and the client's send failing. The client
  reaches `pine` through Docker's userland proxy on the loopback port, and that hop was not observed.
- **Stall 1's server socket:** socket sampling began with the second batch.
- **Load:** whether a stall depends on it. Stall 1 came with nothing but the slot's own uploads on the federation,
  2 minutes after up's baseline. Stall 2 came 75 s after the 15,360-file pass ended, stall 3 seven minutes after it.
- **HTTP/2:** not tried; every request was HTTP/1.1.

The step's directories in `alpha-space` were deleted at the end, and were gone from the root's listing at the first
read. The 19,360 empty files of the two passes stay in `beta-space`.
