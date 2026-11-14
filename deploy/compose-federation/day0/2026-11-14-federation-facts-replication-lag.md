# A replication can complete short of the file, 14 Nov 2026

On the **25.0 development build (op-worker f118ed9)**, a replication job can report `completed` and leave its target
without part of the file. The target runs the job from its own copy of the other providers' file locations, and dbsync
brings that copy late. A replication scheduled before the copy holds the source's last write fetches only what the copy
shows, fetches nothing for the rest, and ends `completed`. No REST call shows the lag:
`GET /data/{id}/distribution` read through any provider gathers each provider's block map from that provider itself, so
it shows the source's current blocks even while the target's copy is behind. The target's own block map is the only
sign: it stays short after the job ended. Nothing schedules the job again; it stays short until something does.

This is a property of the platform. It applies to any replication, so to a study's agent as much as to a harness: a
replication an agent schedules right after a file changed may complete short of the file, and only the target's own
block map read afterwards tells.

## The source

Read at op-worker `f118ed90074434045bad5d7bd2611bd206f1a405` (the commit `2026-11-12-federation-facts-build.md`
records); `file:line` in that repository's own tree.

| What | Where |
|---|---|
| The distribution of a regular file is gathered from every provider that supports the space, by provider RPC | `src/modules/fslogic/distribution/data_distribution.erl:183-199` (`provider_rpc:gather_from_cosupporting_providers`), `src/provider_rpc/provider_rpc.erl:47-56,88-104` |
| Each provider answers that RPC from its own local file location | `src/provider_rpc/worker/provider_rpc_handlers.erl:30-31`, `src/modules/fslogic/distribution/provider_reg_distribution.erl:31-38` (`file_ctx:get_local_file_location_doc_const`) |
| A replication's synchronizer finds what to fetch, and from whom, in the file location documents it holds, the other providers' among them | `src/modules/fslogic/replication/replica_synchronizer.erl:1368-1372` (`fslogic_cache:get_all_locations()`, then `replica_finder:get_blocks_for_sync`) |
| A block that no other provider's location shows is in no fetch request | `src/modules/fslogic/replication/replica_finder.erl:48-89` (the present blocks of each remote location; an empty set is filtered out) |
| A file location is a synced document: another provider's reaches the target through dbsync | `src/modules/datastore/models/file/location/file_location.erl:51` (`sync_enabled => true`) |
| dbsync's timers: changes are broadcast every 5 s, and a receiver that sees a gap in the sequence asks again after 15 s | `src/modules/dbsync/dbsync_worker.erl:237-239`, `src/modules/dbsync/dbsync_in_stream_worker.erl:445-452` |

**[inference]** On a quiet space the copy lags by up to about 8 s, and by 15 s more when a batch must be asked for
again; that sum was built from the timers above, not measured.

## Seen live

The 14 Nov live slot ran the preallocated progressive ingest's gated test of a mirror's replica on the Compose
federation (three providers; the owner is the data provider, the mirror another provider of the space). The code was
main at `1655851f`. The file was 2,101,473 B, with a prefix of 524,295 B.

| Observed | Value |
|---|---|
| The start: the file made again at its final size, the prefix alone written, visible on the mirror | after 6.08 s; the start took 22.9 s |
| The one part: written 2.08 s after the start, then confirmed | the owner holding `[0, 2101473)` |
| The part's confirmation | read the block map through every provider of the space and passed (the default `visible_on`) |
| The reset's replication to the mirror, scheduled right after the reset's own confirmation | reported `{completed: 1}` |
| The mirror's own block map after it | the prefix alone: 1,577,178 B short (the file less the prefix) |
| Within `settle_timeout_s`, 300 s | the mirror's map never covered the file; the restore failed after 306.1 s |

**[inference, from the code the test ran]** The part's confirmation passes only once the block map read through each
provider of the space, the mirror included, shows the owner holding exactly `[0, 2101473)` on consecutive polls
(`ingestsparse.SparseBaseline.confirm_state` at `1655851f`). So the block map through the mirror showed the owner
holding the whole file before the replication was scheduled, and waiting on it would not have helped. **[inference,
from the source above]** The mirror's copy of the owner's location still held the prefix's state, so the job fetched
the prefix alone.

## What the platform layer does about it

`Provisioner.replicate_file` (and `replicate`, through it) judges the replication by the target's own block map, as
before (O01). While that map is short after a job ended, it schedules the replication again after a pause of 5 s, then
10 s, then 15 s, five times at most, all within `settle_timeout_s` from the first job's end. Every job's id is in the
change log and in the record (`tries`, `transfer_ids`, `converged_s`). A map that never covers the file is refused as
`NotPlaced`, as before. Each try leaves an ended transfer record, which never expires.
