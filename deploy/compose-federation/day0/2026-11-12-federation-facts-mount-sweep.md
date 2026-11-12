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
