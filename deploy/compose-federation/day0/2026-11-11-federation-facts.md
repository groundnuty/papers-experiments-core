# The Compose federation's facts, 11 Nov 2026

The live pass of the declared layouts (#29): the default layout and the five-provider example, each brought up from
clean twice, with `reset --replicas`, `reset --clean` and down, run at eb7a11b with the shape script's fix of b30bb8a.
Both ran in the default root, one after the other. "Unknown" means not measured; it is never a pass. Every name below
is a layout's own invented one. The day-0 checks and the live contract tests were not run in this pass; their last
results are in `2026-11-10-federation-facts.md`.

## The deployment

As `describe` printed them. The host had 55 GiB in total and 43 GiB available (`free -g`) before each first up and
after each down.

| Fact | Default layout | Five-provider example |
|---|---|---|
| Layout | `federation.toml`, SHA-256 `c8f8cadf5ff7c704` | `five-providers.toml`, SHA-256 `39232d62b255b464` |
| Compose project | `expcore-fed`, domain `expcore-fed.test` | `expcore-fed5`, domain `expcore-fed5.test` |
| Root parent | `/mnt/volume2/expcore-federation` (default) | `/mnt/volume2/expcore-federation` (default) |
| Root | `/mnt/volume2/expcore-federation` | `/mnt/volume2/expcore-federation` |
| Onezone | `zone` on 127.0.0.1:29443, memory limit 6g | `zone` on 127.0.0.1:29543, memory limit 6g |
| Oneproviders | `pine` 29444, `birch` 29445, `cedar` 29446, 6g each | `aspen` 29544, `larch` 29545, `rowan` 29546, `alder` 29547, `hazel` 29548, 6g each |
| Spaces (owner; mirrors) | `alpha-space` (study; `pine`; `birch`, `cedar`), `beta-space` (`birch`; `cedar`), 10 GiB support | `alpha-space` (study; `aspen`; `rowan`), `beta-space` (`larch`; none), `gamma-space` (`alder`; none), `delta-space` (`hazel`; `alder`, 5 GiB), 10 GiB otherwise |
| Principals | `alpha-reader`, `alpha-tools`, `alpha-health`: space_view, space_read_data in `alpha-space` | the same, and `delta-reader` in `delta-space` |
| Memory limits together | 24.00 GiB for 4 services | 36.00 GiB for 6 services, under the 43 GiB available, so up's check passed |
| Releases | the pinned images of `2026-11-10-federation-facts.md`; not read again | the same |

## Memory per service

The peak since the container started, from its own cgroup (`memory.peak`), as `memory` read it after up and after the
two resets, and as down read it just before it stopped anything. Each cell is run 1 / run 2, in GiB. The cgroup limit
read 6.00 GiB for every service, as declared.

| Layout | Service | Peak after up | Peak after the resets | Peak at down |
|---|---|---|---|---|
| default | `zone` | 1.05 / 1.01 | 1.05 / 1.01 | 1.05 / 1.01 |
| default | `pine` (data provider) | 1.20 / 1.17 | 1.20 / 1.20 | 1.20 / 1.20 |
| default | `birch` | 1.05 / 1.04 | 1.06 / 1.07 | 1.06 / 1.07 |
| default | `cedar` | 1.03 / 1.06 | 1.03 / 1.06 | 1.03 / 1.06 |
| example | `zone` | 1.07 / 0.94 | 1.07 / 0.94 | 1.07 / 0.94 |
| example | `aspen` (data provider) | 1.11 / 1.11 | 1.11 / 1.11 | 1.11 / 1.11 |
| example | `larch` | 0.78 / 0.80 | 0.78 / 0.80 | 0.78 / 0.80 |
| example | `rowan` | 0.99 / 1.00 | 1.02 / 1.00 | 1.02 / 1.00 |
| example | `alder` | 0.78 / 0.79 | 0.78 / 0.79 | 0.78 / 0.79 |
| example | `hazel` | 0.72 / 0.86 | 0.72 / 0.86 | 0.72 / 0.86 |

The largest peak of any service was 1.20 GiB, on the default layout's data provider, and no service of either layout
passed 1.2 GiB. The current use stayed between 0.64 and 1.16 GiB. The cgroup charges the page cache too, so these
peaks overstate what a service needs, not understate it. They are for the synthetic baseline alone, about 1.4 MiB in
some 1200 files and a few transfers: what a study's own data and load take is unknown here, and is what a study's
limit should be sized from.

## Up, the two resets and down

Every command exited 0. Seconds and phases are the commands' own summary lines.

| Step | Layout | Command | Seconds | Phases and result |
|---|---|---|---|---|
| 1a | default | up from clean | 201 | prepare 3, zone 61, providers 68, spaces 6, baseline 63 |
| 1b | default | `measure_transfer_shapes.py` | | see "Call shapes" below |
| 1c | default | reset --replicas | 51 | `alpha-space`: 1204 files; `birch` held 1048576 B, `cedar` 0 B; evicted from `birch`; verified empty on 2 sweeps, 36 s. `beta-space`: `cedar` held 0 B, nothing to evict, 2 sweeps, 2 s |
| 1d | default | reset --clean | 23 | baseline 22 (5 entries removed and written again) |
| 1e | default | reset (bare) | | exit 2, naming `reset --clean` and `reset --replicas`; nothing touched |
| 1f | default | down | 36 | containers 34, data 2 |
| 2a | default | up from clean | 196 | prepare 3, zone 56, providers 67, spaces 6, baseline 64 |
| 2b | default | reset --replicas | 48 | as 1c: evicted from `birch` alone, 2 clean sweeps, 32 s |
| 2c | default | reset --clean | 30 | baseline 29 (5 entries) |
| 2d | default | down | 35 | containers 33, data 2 |
| 3a | example | up from clean | 201 | prepare 3, zone 56, providers 79, spaces 8, baseline 55 |
| 3b | example | reset --replicas | 45 | `alpha-space`: 1204 files; `rowan` held 1048576 B; evicted from `rowan`; 2 clean sweeps, 27 s. `beta-space` and `gamma-space`: no mirror; `delta-space`: `alder` held 0 B; nothing to evict in either; each 2 sweeps, 2 s |
| 3c | example | reset --clean | 33 | baseline 31 (7 entries) |
| 3d | example | down | 38 | containers 36, data 2 |
| 4a | example | up from clean | 210 | prepare 3, zone 56, providers 84, spaces 8, baseline 58 |
| 4b | example | reset --replicas | 51 | as 3b: evicted from `rowan` alone, 2 clean sweeps, 31 s |
| 4c | example | reset --clean | 33 | baseline 32 (7 entries) |
| 4d | example | down | 39 | containers 37, data 2 |

**`reset --replicas` evicted only the mirror's replica.** In every run the one replica a mirror held was the 1 MiB
`replicated.bin` that the baseline replicates to the study space's first mirror (`birch`, then `rowan`). Only that
mirror received an eviction, and every owner still held each of its files whole, on two sweeps in a row of every
file's block map. No other provider held anything to evict, and no eviction was ever sent to an owner.

After each down, `docker ps -a` and `docker network ls`, filtered to `expcore-fed` and `expcore-fed5`, listed nothing,
the root was gone, and nothing listened on 29443-29446 or 29543-29548.

## Call shapes the core's reset depends on

From `measure_transfer_shapes.py` on the default layout (run 1), with `pine` the data provider and `birch` the mirror,
in `alpha-space`, on a 256 KiB scratch file in a scratch directory. Each request and answer is shown by field names and
JSON types; no value was recorded.

| Call | Request (query, body) | Status | Answer |
|---|---|---|---|
| `POST /transfers`, a replication, on the data provider | body `type`: str, `replicatingProviderId`: str, `dataSourceType`: str, `fileId`: str | 201 | `transferId`: str |
| `POST /transfers`, an eviction of a file, on the mirror | body `type`: str, `evictingProviderId`: str, `dataSourceType`: str, `fileId`: str | 201 | `transferId`: str |
| `POST /transfers`, an eviction of a directory, on the mirror | the same, `fileId` the directory's | 201 | `transferId`: str |
| `GET /transfers/{transfer}`, an eviction's record once ended | none | 200 | see below |
| `GET /spaces/{space}/transfers?state=waiting` | query `state`: str, `limit`: int | 200 | `transfers`: list of str (0 on both), `nextPageToken`: null |
| `GET /spaces/{space}/transfers?state=ongoing` | the same | 200 | `transfers`: list of str (1 on the data provider, 0 on the mirror), `nextPageToken`: null |
| `GET /spaces/{space}/transfers?state=ended` | the same | 200 | `transfers`: list of str (4 on the data provider, 5 on the mirror), `nextPageToken`: null |
| the same with `limit=1` | query `state`: str, `limit`: int | 200 | `transfers`: list of 1 str, `nextPageToken`: str |
| its next page | query `state`: str, `limit`: int, `page_token`: str | 200 | `transfers`: list of 1 str, `nextPageToken`: str |

An ended transfer's record, for an eviction and for a replication alike, holds `type`, `transferStatus`,
`replicationStatus`, `evictionStatus`, `effectiveJobStatus`, `userId`, `spaceId`, `fileId`, `filePath`,
`dataSourceType` and `effectiveJobTransferId` (str), `scheduleTime`, `startTime`, `finishTime` and `lastUpdate` (int),
`filesToProcess`, `filesProcessed`, `filesReplicated`, `filesEvicted`, `filesFailed` and `bytesReplicated` (int), and
`rerunId` and `callback` (null). An eviction has `evictingProviderId` a str and `replicatingProviderId` null, and a
replication the reverse. `minHist`, `hrHist`, `dyHist` and `mthHist` are empty objects for an eviction; for a
replication they are keyed by a provider id, each a list of int (19, 62, 26 and 32 long). Every transfer ended
`completed`: the replications in 17 s and 12 s, the file's eviction in 8 s, the directory's in 7 s.

Every answer, on both providers, carried a `Date` header. At the same moment the two providers listed the space's jobs
differently, one job `ongoing` on the data provider and `ended` on the mirror, which is the lag the core's transfer log
settles before it believes a listing.

**Evicting a directory empties what the mirror holds of the files in it.** The mirror held 262144 B of the scratch file
after each replication. Its own block map showed 0 B at the first read after the directory's eviction ended, as it did
after the file's own eviction. Until the mirror had seen the new file (6.1 s), its `distribution` answered 404
`notFound`, which the per-run reset reads as unknown, never as empty.

## What listens on the host

Only the running layout's loopback ports: 127.0.0.1:29443-29446 for the default layout, 127.0.0.1:29543-29548 for the
example. `docker ps` showed each container publishing its 443 on 127.0.0.1 alone, and every container's root label
named the root.

## What could not be measured

- The releases were not read again; the images are the pinned ones of 10 Nov.
- The day-0 checks and the live contract tests: not run in this pass.
- Memory under a study's data and load: only the synthetic baseline ran.
- A mirror's replica in a space other than the study space: the baselines put none there (the example's `alder` held
  nothing of `delta-space`), so the live pass evicted none; the unit tests cover that case.
- QoS requirements and running transfers: `reset --replicas` does not reset them, which is left to the platform layer's
  per-run reset (#118).
