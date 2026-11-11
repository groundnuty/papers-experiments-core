# The Compose federation: eviction timing, placement granularity, and the first passing round trip, 11 Nov 2026

A sixth fresh `up`. The probe `../measure_granularity_and_eviction.py` measured what a range read places on a provider
that lacks the bytes and which eviction empties it again; a second, shorter run evicted right after a placement. Then
the day-0 checks and the live contract tests ran on the eviction retry and the per-file placement record. The earlier
facts files stay as they were. The day-0 checks' own report is beside this file (`day0-20261111T144521Z.md` and `.json`,
mutation allowed). "Unknown" means not measured; it is never a pass. Every name below is the federation's own invented
one: files were written through `pine`, the data provider, and read through `birch`.

## What one range read places

A fresh file per row, written through `pine`; `birch` was waited for until it saw it (2.5 to 8.1 s), then one range was
read through `birch` at an offset aligned to nothing, and `birch`'s block map was read until two reads agreed (1.0 to
1.2 s):

| File | Read | At offset | Answer | `birch` then holds | Bytes held |
|---|---|---|---|---|---|
| 64 KiB | 1 B | 36865 | 206, 1 B | `[36865, 28671]`, to the end of the file | 28671 |
| 64 KiB | 4 KiB | 36865 | 206, 4096 B | `[36865, 28671]`, to the end of the file | 28671 |
| 64 KiB | 1 MiB | 1 | 206, 65535 B (clipped) | `[1, 65535]`, to the end of the file | 65535 |
| 8 MiB | 1 B | 4198401 | 206, 1 B | `[4198401, 1048576]` | 1048576 |
| 8 MiB | 4 KiB | 4198401 | 206, 4096 B | `[4198401, 1048576]` | 1048576 |
| 8 MiB | 1 MiB | 4198401 | 206, 1048576 B | `[4198401, 1048576]` | 1048576 |
| 64 MiB | 1 B | 33558529 | 206, 1 B | `[33558529, 1048576]` | 1048576 |
| 64 MiB | 4 KiB | 33558529 | 206, 4096 B | `[33558529, 1048576]` | 1048576 |
| 64 MiB | 1 MiB | 33558529 | 206, 1048576 B | `[33558529, 1048576]` | 1048576 |

So the smallest placed unit is 1 MiB from the read's own offset, clipped to the end of the file, whatever the read asked
for up to 1 MiB. It is not aligned: every placed range starts where the read started, and no start or end fell on a
multiple of 4 KiB, 64 KiB or 1 MiB. A read of the first half of the round trip's 8 KiB scratch file therefore places all
of it.

## Which eviction empties the receiver

`POST /transfers` with `type` `eviction`, `dataSourceType` `file`, the file's id and `evictingProviderId` naming
`birch`, scheduled through `birch` itself or through `pine`, then `birch`'s block map read for 30 s after the transfer
ended:

| When | Scheduled through | Transfer | `filesEvicted` | `birch` afterwards |
|---|---|---|---|---|
| Minutes after the placement (the nine files above) | `birch`, 5 times | completed in 9.1 to 12.7 s | 1 | empty at the first read, each time |
| Minutes after the placement | `pine`, 4 times | completed in 18.2 to 18.8 s | 1 | empty at the first read, each time |
| 1 s after the placement's block map settled | `birch`, 2 times | completed in 0.5 s | 0 | still 1048576 bytes, 30 s later |
| The same eviction again, 30 s later | `birch` | completed in 12.2 s | 1 | empty |
| 1 s after the placement's block map settled | `pine`, 2 times | completed in 18.3 s | 0 | still 1048576 bytes, 30 s later |
| The same eviction again, 30 s later | `pine` | completed in 18.8 s | 1 | empty |

So the provider that schedules the eviction does not matter; when it is scheduled does. An eviction scheduled right
after a provider fetched the bytes on the fly is reported `completed` with nothing evicted, through either provider,
and the same eviction a little later works. The earlier facts file's reading (through `birch` fails, through `pine`
works) compared an eviction at once with one about 30 s later. The core now schedules through the evicting provider,
as the federation's own reset does, and judges by the block map alone: while the receiver still holds bytes after an
eviction ended, it schedules it again after 10 s, up to 5 times (`provision.EVICTION_ATTEMPTS`).

## Day-0 checks (`day0-20261111T144521Z.md`)

6 pass, 0 fail, 1 unknown, exit status 3, in 50 s.

- **reachability, health, tokens, listing, read_only: pass.**
- **round_trip: pass, for the first time.** The scratch file was created, seen by `birch`, half of it read through
  `birch` (`range_bytes` 4096), which placed all of it (`held_bytes` 8192, `held_ranges` `[[0, 8192]]`, covering the
  range read), read back with matching bytes, evicted on the second attempt (`eviction_attempts` 2: the first, right
  after the placement, evicted nothing), and removed, with the removal verified.
- **content_type: unknown**: no `[sandbox]` is configured, so nothing was measured.

## Live contract tests

`EXPCORE_LIVE_PLATFORM=<root>/platform.toml EXPCORE_LIVE_CONTENT_PATH=hello.txt EXPCORE_LIVE_MUTATE=1
EXPCORE_LIVE_SLOW=1 uv run pytest tests/live/test_contract.py`: 14 passed, 1 skipped, in 92 s. Both state-changing day-0
checks, run as tests, pass; the skip is the tool-server test, with no tool server installed. Afterwards the federation
was taken down: no container or network of the project, no data directory and no listener on its four ports.
