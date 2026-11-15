# The Compose federation's facts, 15 Nov 2026: the first live link heal

The first live run of the restore's link heal (`[faults] heal = "all_links"`, #267) and of the gate's recovery around it
(#289). It ran during another study's Day-0 fact 3, an outage of a space owner's Oneprovider and its restore, on that
study's Compose federation (one Onezone, five Oneproviders), at core 39bba713. Providers are named by their position in
the layout, as everywhere in this repository.

**Provenance:** every figure below was relayed by the coordinating session from that study's run record and journal.
None was read from the run's logs by this repository's session.

## The heal

| Step | Time (UTC) | What was recorded |
|---|---|---|
| The restore's link check | before 22:00:47 | one pair stuck: providers[0] → providers[2] (`ReadTimeout`), the same pair as in the 14 Nov outage's second run |
| The heal begins | 22:00:47 | link epoch 1 |
| `restart_link` sent to all five providers | 22:01:40 to 22:01:48 | no fetch between them |
| The no-fetch settle | to 22:04:48 | 180 s after the last `restart_link` (`heal_settle_s`) |
| The re-check of all ten pairs | about 22:05:09 | every pair passes, about 21 s after the settle ended |
| The heal journal | about 22:05:09 | `cleared`, how `restored` |

One round of `all_links` mended the stuck pair. **No container fallback ran, and no container was restarted.** The settle
held with no fetch inside it, and the re-check came after it, as the design requires: no fetch involving a just-restarted
reader until a settle has ended. From its start to the journal's `cleared`, the heal took about 4 min 22 s.

## The outage and the fact's end

| Fact | Value |
|---|---|
| The owner down | 141.5 s |
| `/distribution` per call, all providers up | 0.011 s |
| `/distribution` per call, during the outage | about 3.01 s (4 calls) |
| The fact's end | 22:06:10 |
| After the restore | the owner's restore verified, then the space it owns verified |
| Cleanup | verified |
| Post-checks | 6 containers healthy (the Onezone and five Oneproviders); 3 fault injections, all cleared; ten pairs pass; no transfer pending |

The study's own verdict for the fact failed, on a cause unrelated to the heal that it is diagnosing itself.

## What this does not show

- A heal that needs more than one round, or the container fallback: neither ran.
- A heal under load from agent runs: the federation ran no campaign during the fact.
- The heal at the pinned core (2bce7359): the fact ran at 39bba713. The heal's code is the same in both, since 2bce7359 changes only how transfer records are read.
