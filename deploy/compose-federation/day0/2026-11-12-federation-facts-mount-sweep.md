# The Compose federation: oneclient's read settings through the sandbox's mount, live, 12 Nov 2026

A sweep of oneclient's read and prefetch settings through the sandbox's mount, on a fresh `up` of the default layout,
from PR #165's branch (the live mount harness). Two recorded read sequences (`read-sequences/1`, offsets and lengths
only) were replayed in the agent's container, file A (8285081 B, 90 reads) and file B (323260 B, 31 reads), each in
two ways: **buffered** (`open(path, 'rb')` at default buffering, then seek and read) and **pread** (`os.pread` on a
bare descriptor). Every name below is the federation's own invented one: `pine` is the local provider, the only one
the oneclient sidecar dials (the receiver), and `birch` alone held each file read (the holder). "Unknown" means not
measured; it is never a pass. No token, address, address range or host path is recorded here.

`pine` is both the layout's data provider and the provider the sidecar dials, so a file held on `pine` would place
nothing there; as in the live mount test, every file was written through `birch` and held there only, and what `pine`
gained is the measure. The files were written as the admin through `birch`'s REST API (the harness's `SpaceFiles`),
since the platform layer's provisioner cannot write on 25.0.

## The settings, declared before measuring

oneclient's defaults, and four variants from its read and prefetch options (defaults in
`2026-11-11-federation-facts-mount.md`), from "fetch less" to "fetch more". Each is passed as oneclient arguments
through `[sandbox.mount] extra_args`, after the sandbox's own `-o ro,allow_other --force-proxy-io`:

| Setting | oneclient arguments | Meant to show |
|---|---|---|
| `defaults` | none | the baseline |
| `least` | `--min-block-prefetch-size=4096 --read-buffer-min-size=4096 --read-buffer-max-size=4096 --read-buffer-prefetch-duration=0` | the least oneclient can be asked to fetch: smallest prefetch block, smallest read buffer, no buffer read-ahead period |
| `min-block-4k` | `--min-block-prefetch-size=4096` | oneclient's minimum prefetch block alone: whether the 1 MiB placed per read is oneclient's or the provider's |
| `more` | `--min-block-prefetch-size=4194304 --read-buffer-min-size=4194304 --read-buffer-prefetch-duration=4` | larger blocks and a larger, longer read buffer |
| `most` | `--seqrd-prefetch-threshold=0 --rndrd-prefetch-threshold=0 --rndrd-prefetch-cluster-window=-1` | replication prefetch at once, over the whole file |

Each setting got a fresh directory, four fresh files (A and B for buffered, A and B for pread, random bytes, new names),
and a sandbox of its own. After each read the replayer waited while `pine`'s block map was read until two reads 2 s
apart agreed (as the live mount test settles), so each read's gain is its own; after a replay's last read the map was
read once more 5 s later. The pause between reads is the harness's, not the recorded reader's.

## Out of scope

- `--force-direct-io`: direct access to the providers' storage needs that storage mounted into the sidecar, a host
  mount; the sandbox runs oneclient with `--force-proxy-io`.
- Changing the mount's kernel read-ahead (`/sys/class/bdi/<bdi>/read_ahead_kb`): a host setting, never changed here.

Not declared, so not measured, though within scope: `--no-fullblock-read`, `--prefetch-mode=sync`, oneclient's other
random-read cluster options, and FUSE's `-o max_readahead=`.

## The commands

Outside any sandbox, from branch `day0/mount-sweep` (`<pool>` is a private range the host routes nowhere):

```sh
uv run python deploy/compose-federation/federation.py up
uv run python deploy/compose-federation/measure_mount_sweep.py --sequences <read-sequences file> --pool <pool> \
  --out <directory> --oneclient-image onedata/oneclient@sha256:aff60c5ff78c99f4cb0c5d0acb5392b37a1fdd3cdd73b73bca9b640d067a51d8
uv run python deploy/compose-federation/federation.py down
```

The input's SHA-256 was `7271303c37cd9156cc019a96f3408b5ed3feae2b37f3e6cb67602ce6fff685f9`. `up` took 219 s. The
oneclient image is on the host only by digest now, untagged; its id, `sha256:aff60c5f…`, is the one the 11 Nov run
used as `onedata/oneclient:25.0`. The agent's image is `python:3.12-slim`, `sha256:423ed6ab…`, as on 11 Nov. Three
smoke runs of up to six reads per file came first, to check the script; their numbers are not used below.

## The launch, per setting

Every sidecar started and mounted with its setting's arguments (the sandbox's start took 2.9 to 3.4 s), and each
declared option appeared in the running sidecar's `oneclient --help`. `docker inspect` showed the same for all five:

| | Sidecar (oneclient) | Agent |
|---|---|---|
| Devices | `/dev/fuse` | none |
| Capabilities | `CAP_SYS_ADMIN` added | all dropped |
| Security options | `apparmor:unconfined` | `no-new-privileges` |
| Privileged | no | no |

Before each replay its file was visible on `pine` at its size, `pine` and `cedar` held none of it, and `birch` held
all of it.

## What was placed on `pine`, per replay

"Requested" is the sum of the reads' lengths; "distinct" the bytes they cover; "placed" what `pine`'s block map held
after the replay's last read and its late read; "beyond" the placed bytes no read covered. Every read returned all the
bytes it asked for; no read's map failed to settle, and no replay's late read found anything more.

| Setting | File | Mode | Requested | Distinct | Placed on `pine` | Beyond the reads | Reads that gained | Read seconds: sum / median / max |
|---|---|---|---|---|---|---|---|---|
| `defaults` | A | buffered | 1011995 | 1011427 | 5097334 | 4085907 | 7 | 0.381 / 0.00002 / 0.049 |
| `defaults` | A | pread | 1011995 | 1011427 | 5097334 | 4085907 | 7 | 0.815 / 0.00288 / 0.206 |
| `defaults` | B | buffered | 315652 | 315300 | 323260 | 7960 | 1 | 0.199 / 0.00254 / 0.124 |
| `defaults` | B | pread | 315652 | 315300 | 323260 | 7960 | 1 | 0.127 / 0.00295 / 0.029 |
| `least` | A | buffered | 1011995 | 1011427 | 8285081 | 7273654 | 14 | 0.396 / 0.00002 / 0.031 |
| `least` | A | pread | 1011995 | 1011427 | 8285081 | 7273654 | 13 | 0.636 / 0.00276 / 0.186 |
| `least` | B | buffered | 315652 | 315300 | 323260 | 7960 | 9 | 0.360 / 0.00263 / 0.153 |
| `least` | B | pread | 315652 | 315300 | 322748 | 7448 | 12 | 0.282 / 0.00316 / 0.029 |
| `min-block-4k` | A | buffered | 1011995 | 1011427 | 8285081 | 7273654 | 14 | 0.445 / 0.00002 / 0.093 |
| `min-block-4k` | A | pread | 1011995 | 1011427 | 8285081 | 7273654 | 13 | 0.495 / 0.00280 / 0.030 |
| `min-block-4k` | B | buffered | 315652 | 315300 | 323260 | 7960 | 10 | 0.546 / 0.00260 / 0.194 |
| `min-block-4k` | B | pread | 315652 | 315300 | 322748 | 7448 | 12 | 0.285 / 0.00337 / 0.036 |
| `more` | A | buffered | 1011995 | 1011427 | 8285081 | 7273654 | 2 | 0.258 / 0.00002 / 0.109 |
| `more` | A | pread | 1011995 | 1011427 | 8285081 | 7273654 | 2 | 0.381 / 0.00298 / 0.095 |
| `more` | B | buffered | 315652 | 315300 | 323260 | 7960 | 1 | 0.292 / 0.00275 / 0.217 |
| `more` | B | pread | 315652 | 315300 | 323260 | 7960 | 1 | 0.119 / 0.00255 / 0.032 |
| `most` | A | buffered | 1011995 | 1011427 | 5097334 | 4085907 | 7 | 0.512 / 0.00002 / 0.207 |
| `most` | A | pread | 1011995 | 1011427 | 5097334 | 4085907 | 7 | 0.546 / 0.00276 / 0.092 |
| `most` | B | buffered | 315652 | 315300 | 323260 | 7960 | 1 | 0.103 / 0.00256 / 0.028 |
| `most` | B | pread | 315652 | 315300 | 323260 | 7960 | 1 | 0.139 / 0.00278 / 0.041 |

Every read's offset, length, bytes, seconds and gained ranges are in
`2026-11-12-federation-facts-mount-sweep.json` beside this file (offsets and lengths only, files named A and B).

- **`defaults`:** A placed 5097334 B for 1011995 B requested, the same bytes buffered and by pread. Each of the five
  reads that started on bytes `pine` lacked placed 1 MiB from its own offset, aligned to nothing (the last one clipped
  to the end of the file, 902616 B); the two reads whose ends ran into held bytes gained only their own unheld bytes
  (146 B and 268 B). B's first read, 8 B at 0, placed the whole file.
- **`most`** placed exactly what `defaults` placed, read for read: the thresholds of 0 and the whole-file cluster
  window changed nothing measured here.
- **`least`** and **`min-block-4k`** placed the same bytes as each other, read for read, so the read-buffer options
  changed nothing measured here. The first read (8 B at 0) placed 4096 B. Later gains were 4096 B blocks from a
  read's own offset, the unheld tail of a read, short fills between held ranges, and fetches past a read's end: by
  pread, seven per setting ended exactly 8192 B past the read's end; buffered, they ended up to 8071 B past it. Then,
  after 13 (buffered) or 12 (pread) reads had gained bytes, the next read, 512 B, placed every byte of A still
  missing, 8247458 B and 8251185 B, in 5 ranges at once. B by pread ended without the 512 B at 311944.
- **`more`:** A's first read placed 4194304 B from 0, and a later read inside those bytes (the buffered replay's 28th
  read, the pread replay's 14th) placed the rest of the file.

So `defaults` and `most` fetched least (A 5097334 B, about 5 times what was requested), and `least`, `min-block-4k`
and `more` fetched most (A whole, 8285081 B). B was placed whole, or within 512 B of whole, under every setting.

No placement came through a transfer. In every replay the probes put every byte `pine` gained down to `birch`
(transfer bytes in 0, so the on-demand lower bound is everything gained), and the space's transfer listing over each
replay held only the baseline's earlier, ended record. oneclient's log directory held one `INFO` file of 17 lines (the
start-up settings) under every setting, and no line named a fixture file by id or uuid. So what oneclient pulled per
read is known only as `pine`'s block-map gain, a lower bound, in every setting.

## Read-ahead

As the agent's container saw it, in every setting:

| | |
|---|---|
| The mount's `st_dev`, as in `/proc/self/mountinfo` | `0:63` (`0:61` for `more`), type `fuse.oneclient` |
| `/sys/class/bdi/<that>/read_ahead_kb` | `128`, readable in the agent |
| `blockdev --getra` on a file and on the mountpoint | present in the image; exit 1, `ioctl error on BLKRAGET: Inappropriate ioctl for device`: no block device behind FUSE, so not applicable |
| Could the agent set it | no: `/sys` is mounted read-only, `access(W_OK)` is false, and `CapEff` is 0. It would take a writable sysfs and `CAP_SYS_ADMIN` in the agent's container, beyond the approved scope; it was not tried |
| A file's `st_blksize` (the agent's `io.DEFAULT_BUFFER_SIZE`) | 4096 (8192) |
| A shared read-only mapping of a file in the mount | refused, `ENODEV`, for every file |

oneclient's own fetching accounts for the bytes placed beyond the reads, and the kernel's 128 KiB read-ahead does not:

- Under the 4 KiB settings, the first read (8 B at 0) placed exactly 4096 B, buffered and by pread alike. A kernel
  read-ahead of 128 KiB would have asked oneclient for 128 KiB. No gain in any setting was near 128 KiB long. The
  first read's window follows `--min-block-prefetch-size` instead: 1 MiB under `defaults` and `most` (B whole),
  4096 B under the 4 KiB settings, 4 MiB under `more`.
- A pread of bytes `pine` held took a median 2.7 ms (2.3 ms to 4.3 ms, tenth to ninetieth percentile, over 520
  reads of up to 4 KiB that gained nothing), a 48 B pread at 0 straight after an 8 B pread at 0 included: each went
  to oneclient, and the kernel's page cache did not answer it. Buffered reads that Python's buffer could answer took
  a median 20 µs.
- The refused shared mapping and the page cache that never answered are what FUSE's direct-I/O mode would show, a
  mode that bypasses the page cache and its read-ahead. The open flags themselves were not read.
- Buffered and pread placed the same bytes under `defaults`, `more` and `most`. They differ only under the 4 KiB
  settings, where the block is small enough for Python's buffering to change which reads reach oneclient (B: 323260
  against 322748; A: 14 gaining reads against 13).

The pause between reads is the harness's: the recorded reader does not wait 2 s after each read, and a setting that
depends on time (`--read-buffer-prefetch-duration`) may behave otherwise without the pause. That is not measured.

## Teardown

Every sandbox stopped cleanly, and after each setting no `fedsbx-` container or network and no FUSE mount under its
work root was left; every fixture file and directory was deleted with no failure. `federation.py down` took 39 s
(containers 37 s, data 2 s); peak memory over the federation's life was 0.92 GiB for `zone`, 1.32 GiB for `pine`,
1.13 GiB for `birch` and 1.05 GiB for `cedar`. Afterwards `docker ps -a`, `docker network ls` and `docker volume ls`,
filtered to `expcore` and `fedsbx` and by the Compose project's label, listed nothing; the federation's root directory
was gone; there was no `fuse.oneclient` mount and no `federation.py` or sweep process.
