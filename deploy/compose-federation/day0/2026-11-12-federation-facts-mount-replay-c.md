# The Compose federation: a held-out read sequence and reads past the held end, through the sandbox's mount, live, 12 Nov 2026

This file extends `2026-11-12-federation-facts-mount-sweep.md`, which is sealed. That sweep left two cases unmeasured: a
held-out sequence from another file, and a read that reaches past the held end by much less than the window. Both are
measured here, at oneclient's defaults only, on the 25.0 development build (op-worker f118ed9). Every name below is
the federation's own invented one. "Unknown" means not measured, never a pass. No token, id, address, address range
or host path is recorded.

## What was found

- **File C** (8296722 B, 90 reads, 1020449 B requested) placed **5096862 B** on the local provider, about 5.0 times
  what was requested. Buffered and pread placed the same bytes, read for read. Seven reads gained bytes; the other
  **83 lay wholly inside held bytes and gained 0**. This is the same shape as file A under `defaults` in the sweep
  (5097334 B for 1011995 B; 7 gaining reads; 83 reads wholly inside, each gaining 0).
- **Reads past the held end:** after a first read placed `[0, 1048576)`, reads that started inside those bytes and
  reached **4 KiB, 64 KiB or 1 MiB** past their end each gained **exactly 1048576 B, from 1048576, the first byte not
  held**, buffered and pread alike. The **control**, a read wholly inside the held bytes that ended at the held end,
  gained **0**.
- So the 11 Nov sentence "a read starting inside held bytes gained 1 MiB from the first byte not held" holds for small
  overshoots too: 4096 B and 65536 B past the held end placed the whole window, 1044480 B and 983040 B past the read's
  own end. For the 1 MiB overshoot, the window and the read's unheld bytes are the same range, so that probe alone does
  not tell them apart.

## The setup

The federation used the bundled live slot's layout. `cedar` is the agent's local provider and the only one the
oneclient sidecar dials (the **receiver**). `pine` owns the study space; every file was written through it and held
there only (the **holder**). `birch`, the space's third supporting provider, held none of any file. This swaps the
sweep's names: there `pine` was the receiver and `birch` the holder. The federation had been up since 15:40 UTC.
Earlier the same day it had served the measurements in `2026-11-12-federation-facts-upload-and-stalls.md`,
`-progressive-ingest.md` and `-writing-token-views.md`, each in its own directories.

The method is the sweep's, through its script, unchanged and at `defaults` only. For each run, a fresh directory got
fresh files of random bytes with new names, one per mode, written as the admin through `pine`'s REST API. A sandbox in
mount mode was started with no oneclient arguments beyond the sandbox's own (`-o ro,allow_other --force-proxy-io`).
One resident process in the agent's container replayed each file, holding one open file for the whole sequence:
**buffered** (`open(path, 'rb')`, default buffering, then seek and read) or **pread** (`os.pread` on a bare
descriptor). After each read, `cedar`'s block map was read until two reads 2 s apart agreed. After the last read it was
read once more 5 s later.

Before every replay, the file was visible on `cedar` at its size, `cedar` and `birch` held none of it, and `pine` held
all of it. `docker inspect` showed the sweep's launch table for both sandboxes. The sidecar had `/dev/fuse`,
`CAP_SYS_ADMIN` added, `apparmor:unconfined` and was not privileged. The agent had no device, all capabilities dropped
and `no-new-privileges`. The sandboxes took 4.5 s and 3.1 s to start. The mount read as in the sweep: type
`fuse.oneclient`, `read_ahead_kb` 128 and not writable, `CapEff` 0, `st_blksize` 4096, and a shared read-only mapping
refused with `ENODEV` for every file.

## The commands

Outside any sandbox, from `main` at 36e2351, on the running federation (`<pool>` is a private range the host routes
nowhere):

```sh
uv run python deploy/compose-federation/measure_mount_sweep.py --sequences <read-sequences file> --pool <pool> \
  --out <new directory> --settings defaults \
  --oneclient-image onedata/oneclient@sha256:aff60c5ff78c99f4cb0c5d0acb5392b37a1fdd3cdd73b73bca9b640d067a51d8
```

It ran twice: the probes from 17:17 UTC, then file C from 17:24 to 17:32 UTC.

- **File C's input** was a `read-sequences/1` file with SHA-256
  `d6966950f616b22d46fa37d48ffbf3b90064ccd1e33d67bdbfcd731803cc5d48`. It holds one file, `C`: 8296722 B and 90 reads,
  1020449 B requested and 1019881 B distinct. Its last byte read is at 7396162.
- **The probes' input** was a `read-sequences/1` file written for this run, SHA-256
  `9fcad7ba09f0322894612a9c267e6a38a8eaae2d1b7e0d2c3759372c75f448cb`. It holds four files of 8388608 B, one per
  probe. Each file's first read is 8 B at 0, and its second read is the probe, so every probe starts from a fresh,
  known state. Every probe length is a multiple of 8192 B, Python's buffer size. The lengths were chosen so that
  Python's buffered reader would pass each probe through at its own offset and length, and would turn the 8 B first
  read into an 8192 B read at 0, inside the first window. What reached oneclient was not observed.

Every read's offset, length, bytes, seconds, gained ranges and whether it lay wholly inside held bytes are in
`2026-11-12-federation-facts-mount-replay-c.json` beside this file. It holds offsets and lengths only and names the
files C, PC, P4K, P64K and P1M.

## File C: what was placed on `cedar`

"Requested" is the sum of the reads' lengths and "distinct" the bytes they cover. "Placed" is what `cedar`'s block map
held after the last read and its late read, and "beyond" the placed bytes no read covered. "Wholly inside" counts the
reads whose whole range lay inside bytes `cedar` held before the read. Every read returned all the bytes it asked
for, every read's map settled, and neither late read found anything more.

| Mode | Requested | Distinct | Placed on `cedar` | Beyond the reads | Reads that gained | Wholly inside (their gain) | Read seconds: sum / median / max |
|---|---|---|---|---|---|---|---|
| buffered | 1020449 | 1019881 | 5096862 | 4076981 | 7 | 83 (0 B) | 0.391 / 0.00002 / 0.044 |
| pread | 1020449 | 1019881 | 5096862 | 4076981 | 7 | 83 (0 B) | 0.723 / 0.00282 / 0.251 |

The seven reads that gained bytes were the same in both modes, with the same gains:

| Read | Offset | Length | Gained from | Gained bytes | What it was |
|---|---|---|---|---|---|
| 0 | 0 | 8 | 0 | 1048576 | 1 MiB from its own offset |
| 8 | 3753533 | 38 | 3753533 | 1048576 | 1 MiB from its own offset |
| 9 | 3753387 | 146 | 3753387 | 146 | its own bytes; its end met the bytes read 8 placed |
| 43 | 1926999 | 512 | 1926999 | 1048576 | 1 MiB from its own offset |
| 53 | 3753119 | 512 | 3753119 | 268 | its own unheld bytes; its end ran into bytes held from 3753387 |
| 62 | 5576369 | 512 | 5576369 | 1048576 | 1 MiB from its own offset |
| 72 | 7394578 | 512 | 7394578 | 902144 | from its own offset to the file's end, 8296722 |

All seven started on a byte `cedar` did not hold, and each gain started at the read's own offset, aligned to
nothing. None of C's reads started inside
held bytes and reached past their end. The placed bytes formed 5 ranges: 1 MiB from 0, from 1926999 and from 5576369,
1048990 B from 3753119, and 902144 B from 7394578. In total that is 4 × 1048576 + 902144 + 146 + 268 B.

Reads 9 and 53 gained only their own unheld bytes, as A's two such reads did in the sweep. In both, the bytes held
just after the read reached past where a 1 MiB window from the read's offset would end. So these two reads do not
tell "only the read's own bytes" apart from "a 1 MiB window, minus what is held".

## The past-held-end probes

In all eight probe files, four per mode, the first read (8 B at 0) placed exactly `[0, 1048576)`. The held end
before each probe was therefore 1048576.

| Probe | Second read: offset, length | Inside held / past held end | Gain, buffered | Gain, pread | Window from the first byte not held |
|---|---|---|---|---|---|
| `PC` (control) | 1040384, 8192 | 8192 / 0 | 0 | 0 | none |
| `P4K` | 1044480, 8192 | 4096 / 4096 | 1048576 from 1048576 | 1048576 from 1048576 | yes, 1048576 B; ends 1044480 B past the read's end |
| `P64K` | 1040384, 73728 | 8192 / 65536 | 1048576 from 1048576 | 1048576 from 1048576 | yes, 1048576 B; ends 983040 B past the read's end |
| `P1M` | 1040384, 1056768 | 8192 / 1048576 | 1048576 from 1048576 | 1048576 from 1048576 | 1048576 B, ending at the read's own end |

`P4K` starts 4096 B inside the held bytes, the others 8192 B inside. Every gain was a single range,
`[1048576, 2097152)`. Each file ended with 2097152 B placed, except the controls with 1048576 B. Every probe returned
all its bytes, and its map settled at the first check. The probes took 0.040 s to 0.097 s and the controls 0.003 s.

## How the bytes arrived

In every replay, the run's probes attributed every byte `cedar` gained to `pine` and none to `birch`. `cedar`'s
transfer bytes in were 0, so the on-demand lower bound is everything gained. No transfer record named any of the files.
The space's transfer listing held two records over each replay, both carried over from before the replay.
oneclient's log directory held one file of 17 lines (1115 B) in both runs, and no line named a fixture file by id or
uuid. So what
oneclient pulled per read is known only as `cedar`'s block-map gain, a lower bound.

## Not measured

- Whether a window from an unheld read's offset stops at the next held byte, or covers only the read's own bytes.
  Reads 9 and 53 do not separate the two (see above).
- A read that starts inside held bytes and reaches past their end near the file's end, where the window would be
  clipped.
- Any setting other than oneclient's defaults, and a reader without the harness's pause between reads.

## Teardown

Both sandboxes stopped cleanly. After each run, the script found no `fedsbx-` container or network and no FUSE mount
under its work root, and every fixture file and directory was deleted with no failure. Afterwards, outside the
script, Docker listed no container or network named `fedsbx`, the host's mount table held no `fuse.oneclient` mount,
and the space's root on `pine`, `birch` and `cedar` held no directory of either run. The runs' output directories were
removed. The federation was left up, with all five services healthy, for the slot's next step.
