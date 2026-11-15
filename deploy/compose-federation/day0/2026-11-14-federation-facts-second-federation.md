# What a second federation measured: a stuck rtransfer link, its heal, and directory statistics, 13 and 14 Nov 2026

Facts measured on another study's federation on the **25.0 development build (op-worker f118ed9)**, relayed by that
study's coordinator, not measured by this repository's own slots. "Unknown" means not measured. The federation has five
providers, labelled A to E here; no name, id, address or host is recorded.

This file adds to item 1 of `2026-11-14-federation-facts-slot.md`, where a stuck pair was cleared by stopping the
reader's container and running `up`. Here a lighter call cleared it.

## A stuck link after a provider's outage (14 Nov)

A is the owner of the files read in the outage; B is the provider that read A's owner-only files during it; C, D and E
are three other readers of B. A's container was stopped for 284.6 s and started again.

| Observed | Value |
|---|---|
| Every pair with B as the source, after A came back | failed for C, D and E: reads timed out at 30 s |
| The exceptions | the pair toward A, whose link started after B's crash, and every pair where B reads |
| Self-clear | none in 46 min; the stuck set grew (a reader not tested at the first check failed later) |
| `restart_link` on one stuck reader | rc 0, answer `ok`, container unchanged; that reader's pair passed in 0.875 s |
| The two other stuck readers, and the seven other pairs | the two still failed; the seven kept passing |

The stuck state is reader-side and per reader: each reader holds its own stale connection to the crashed source's
link, and only that reader's own call clears it. The call, which needs no Erlang shell:

```
docker exec <reader> bash -c '. /usr/lib/op_worker/lib/env.sh && $NODETOOL -rpctimeout 120000 rpcterms rtransfer_config restart_link ""'
```

## The settle after `restart_link` (14 Nov)

| Observed | Value |
|---|---|
| `restart_link` on a third reader, then a clean 3-minute settle with no fetch involving it | its pair healed, and every reverse pair (the other providers reading from it) stayed healthy, one of them on a connection only minutes old |
| The one reverse pair that was stale | a check run 23 s after a restart |
| The whole heal | four reader-side calls in all, the last on B, the provider whose link had crashed, each followed by a clean 3-minute settle with no fetch; then all ten pairs passed in 0.30 to 1.02 s |
| Container restarts | none |
| The settle | held all four times |

The relayed notes weighed two explanations of why a reverse pair went stale: a fetch too soon after a restart (the
settle), which held, and idleness, which they record as refuted.

B's log line "Dropped all connections from Acceptor" is the incoming side, the peers reading from B. B's own fetches from the
mirror kept working inside that window: the outage condition's replica read and a replication both succeeded. So the
crash affects the peers that read from B, not B's own reads.

## Directory statistics (13 and 14 Nov)

| Observed | Value |
|---|---|
| A space root's `/distribution`, after an eviction of a file whose only block on one provider was mid-file (offset 8 MiB) | credited that provider with 1,048,576 B for 23 min and more |
| The same moment | the file's own block map showed 0 B there, the disk held 0 B, and no transfer was active or waiting |
| A 1,572,864 B placement, 13 Nov | took 9.534 s (34 polls) to show in a directory's statistic: the counter read 0 while the bytes were held |

The first is a figure that stays wrong, not one that is late. The second is a lag of the kind the source gives for a
placement (up to 10 s plus 5 s per level); the depth was not relayed, so it is not set against that bound here.

## Not measured

- Whether B's own fetches outward keep working over a long outage: the relayed notes plan mirror reads every 30 s for
  the next slot.
