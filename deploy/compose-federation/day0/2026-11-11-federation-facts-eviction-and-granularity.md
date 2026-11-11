# The Compose federation: eviction timing, placement granularity, and the first passing round trip, 11 Nov 2026

A sixth fresh `up`. The probe `../measure_granularity_and_eviction.py` measured what a range read places on a provider
that lacks the bytes and which eviction empties it again; a second, shorter run evicted right after a placement. Then
the day-0 checks and the live contract tests ran on the eviction retry and the per-file placement record. The earlier
facts files stay as they were. The day-0 checks' own report is beside this file (`day0-20261111T144521Z.md` and
`.json`, mutation allowed). A later `up`, measurement only, added the three sections after the first one below, from
`../measure_read_windows.py`; afterwards the federation was taken down, and no container or network of the project, no
data directory and no listener on its four ports was left. "Unknown" means not measured; it is never a pass. Every name
below is the federation's own invented one: files were written through `pine`, the data provider, and read through
`birch`.

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

## What a read places when it starts inside bytes already held

`../measure_read_windows.py` ran twice on that federation; the two runs gave the same numbers in every row below (the
control row was added for the second run). It is the nine rows' counterpart for a second read on a file. Ranges are
`[start, length]` as above, and "gained" is what `birch`'s block map covers after the read and did not before. Calls,
all as the federation's admin:

- a file is created by `POST /data/<space root>/children` (`name`, `type` `REG`) and filled by
  `PUT /data/<file>/content?offset=<n>` in 4 MiB pieces, both through `pine`;
- `GET /data/<file>?attribute=size` through `birch` until it shows the size;
- one read is `GET /data/<file>/content` through `birch` with `Range: bytes=<first>-<last>`;
- the block map is `GET /data/<file>/distribution` through `pine`, `birch`'s entry, read until two reads 1 s apart
  agreed (1.0 to 2.1 s) and once more 5 s later, which never differed.

A fresh 8 MiB file per row; each first holds `[0, 1048576]` with a read of 1 MiB at offset 0 (206, 1048576 B, `birch`
then holding exactly `[0, 1048576]`):

| Second read, 4 KiB at | Answer | `birch` then holds | Gained | Fetch ends at |
|---|---|---|---|---|
| 524288 (inside the held bytes) | 206, 4096 B | `[0, 3670016]` | `[1048576, 2621440]` | 3670016 = offset + 3 MiB |
| 1048575 (last held byte, then unheld ones) | 206, 4096 B | `[0, 4194303]` | `[1048576, 3145727]` | 4194303 = offset + 3 MiB |
| 4198401 (control: no held byte) | 206, 4096 B | `[0, 1048576]`, `[4198401, 1048576]` | `[4198401, 1048576]` | offset + 1 MiB |

So a read that starts inside held bytes gains bytes beyond the read, and the gain begins at the first unheld byte
(1048576), not at the read's offset. It ends 3 MiB past the read's own offset, so how much is gained depends on where in
the held bytes the read began; a 4 KiB read wholly inside them still gained 2.5 MiB. The control shows what causes it:
a second read that starts in no held byte places the ordinary 1 MiB window from its own offset, so being a second read
is not enough. What the block map shows is what is held, so whether the provider asked for the bytes from the read's
offset or from the first unheld one is unknown.

## What a read longer than 1 MiB places

A fresh 8 MiB file per row, one read at offset 4198401 (aligned to nothing), the same calls and block map reads:

| Read | Answer | `birch` then holds | Gained |
|---|---|---|---|
| 1.5 MiB (1572864 B) | 206, 1572864 B | `[4198401, 1572864]` | exactly the read's length |
| 3 MiB (3145728 B) | 206, 3145728 B | `[4198401, 3145728]` | exactly the read's length |

So a read longer than 1 MiB places exactly its own length from its own offset: nothing is rounded up to a multiple of
1 MiB (the 1.5 MiB row would show it) or to a window. The 1 MiB is a minimum. The 3 MiB row is itself a multiple of
1 MiB, so it adds nothing about rounding.

## What sequential reads place

A fresh 64 MiB file per size, three reads back to back in offset order from 0, each settled and its block map read
before the next (so "back to back" here means no other read between them, and about 7 s apart); calls as above:

| Reads of | Read | At offset | `birch` then holds | Gained |
|---|---|---|---|---|
| 4 KiB | 1 | 0 | `[0, 1048576]` | `[0, 1048576]` |
| 4 KiB | 2 | 4096 | `[0, 3149824]` | `[1048576, 2101248]` |
| 4 KiB | 3 | 8192 | `[0, 3149824]` | nothing |
| 64 KiB | 1 | 0 | `[0, 1048576]` | `[0, 1048576]` |
| 64 KiB | 2 | 65536 | `[0, 3211264]` | `[1048576, 2162688]` |
| 64 KiB | 3 | 131072 | `[0, 3211264]` | nothing |

Every answer was 206 with the read's length. So the window does grow, once: the first read places 1 MiB, the second,
which starts inside it, brings the held range to its own offset + 3 MiB (3149824 = 4096 + 3 MiB, 3211264 = 65536 +
3 MiB, the same rule as the overlap table), and the third gained nothing, though its own offset + 3 MiB lies
4096 B (4 KiB reads) and 65536 B (64 KiB reads) past what was held. Whether the third read gained nothing because
the growth happens once per file or for another reason is unknown, as are reads that reach the end of the grown window,
and reads without a settling pause between them. Read-ahead therefore makes what a file holds larger than what was
asked for, by an amount that depends on the order of the reads; a record of what is held has to come from the block
map, not from the reads' own ranges.

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
