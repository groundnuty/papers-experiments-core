# The Compose federation: the sandbox's mount path, live, 11 Nov 2026

The live test of the mount path (`tests/live/test_mount_path.py`, issue #116) ran against the federation that was up,
not a fresh `up`, from a local integration branch: #116's branch, rebased on `main` after #128, #148 and #151, with
#145 (the collection's freeze and copy by descriptor, #129 and #130) merged in. Three further runs were started with a
oneclient trace option, and the read-only input test (O20) ran once. Afterwards the federation was taken down (last section).
Every name below is the federation's own invented one: `pine` is the local provider, the only one the oneclient sidecar
dials, and `birch` is the owner, which alone held each file the agent read. "Unknown" means not measured; it is never
a pass. No token, address, address range or host path is recorded here.

The commands, outside any sandbox, from the integration branch (`<pool>` is a private range the host routes nowhere):

```sh
uv run python deploy/compose-federation/federation.py status
EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml \
EXPCORE_LIVE_SANDBOX_POOL=<pool> EXPCORE_LIVE_FACTS_OUT=<file> uv run pytest tests/live/test_mount_path.py -ra
# the same, with EXPCORE_LIVE_ONECLIENT_ARGS set to "--io-trace-log --log-read-write-perf",
# then to "--io-trace-log", then to "--log-read-write-perf"
EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_PLATFORM=<the first run's derived platform.toml> \
uv run pytest tests/live/test_sandbox.py -ra -k o20
```

## Results

| Run | Result |
|---|---|
| mount test, default oneclient arguments | 15 passed in 161 s |
| `tests/live/test_sandbox.py -k o20`, on the mount level of the derived `[sandbox]` table | 1 passed in 12 s |
| mount test, `--io-trace-log --log-read-write-perf` | 7 passed, 8 failed in 380 s: oneclient died at the first read |
| mount test, `--io-trace-log` | 7 passed, 8 failed in 373 s: the same |
| mount test, `--log-read-write-perf` | not run: 15 skipped, `onedata/oneclient:25.0` was no longer on the host |

The image went missing between the third and the fourth run. Nothing in this pass removes an image, and none is ever
pulled, so the fourth run measured nothing; `--log-read-write-perf` on its own is unknown.

The sections below are the first run's, unless they say otherwise.

## The images

| Image | Id |
|---|---|
| `onedata/oneclient:25.0` | `sha256:aff60c5ff78c99f4cb0c5d0acb5392b37a1fdd3cdd73b73bca9b640d067a51d8`, also its only repo digest |
| `python:3.12-slim` (the agent) | `sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf`, also its only repo digest |

oneclient's log names its version `25.0`, with an empty commit.

## The launch: FUSE for the sidecar alone

What `docker inspect` showed of the two containers of the sandbox, while they ran (the security options read once more
by hand during a later run, with the same answer):

| | Sidecar (oneclient) | Agent |
|---|---|---|
| Network | the federation's (`expcore-fed_fed`) only | the sandbox's own only |
| `/dev/fuse` | yes (one device) | no |
| Added capabilities | `CAP_SYS_ADMIN` | none; `--cap-drop ALL`, and `CapEff` 0 inside |
| AppArmor | `unconfined` (`--security-opt apparmor:unconfined`) | `docker-default`, with `no-new-privileges` |
| Privileged | no | no |
| Root filesystem | writable | read-only |
| A token in `docker inspect` | no | no |

The run's token file was gone from the host once oneclient had started. TLS to `pine` was verified against the
federation's CA (`ca_file`), and the FUSE mount appeared on the host 1.4 s after the sidecar's `docker run`, with
`--force-proxy-io`.

## Confinement of the agent

Inside the agent: one mount line, of type `fuse.oneclient`, read-only; its root lists one entry, the study space; a
write in the space fails with `EROFS`. `pine`'s name does not resolve; the four federation containers' addresses answer
`ENETUNREACH`; the four ports the federation publishes on the host's loopback answer `ECONNREFUSED` on the agent's own
loopback.

## On-demand reads, attributed by the probes

A file of 4 MiB + 123 B, written through `birch` only, read whole through the mount in 0.57 s; its SHA-256 matched what
was written. `pine`'s block map went from nothing to `[0, 4194427]` and settled there. The probes put all 4194427 bytes
gained on `pine` down to `birch` (lower bound of the on-demand fetch 4194427 B, no transfer bytes in, nothing
ambiguous or unattributed); `birch`'s map did not change.

## Unwritten ranges through the mount

Two files of 1 MiB with only the first 64 KiB written, one through `pine` and one through `birch`. Each read through
the mount returned the full length; the written bytes matched and every unwritten byte was zero:

| Read | Written through `pine` | Written through `birch` |
|---|---|---|
| 4096 B at 65536 (unwritten) | zeros, 0.090 s | zeros, 0.084 s |
| 32 B at 65520 (across the end of the prefix) | prefix, then zeros, 0.066 s | prefix, then zeros, 0.118 s |
| the whole file | prefix, then zeros, 0.097 s | prefix, then zeros, 0.125 s |

None blocked. A `stat` took 0.004 to 0.005 s and showed 1048576 bytes.

## The collection's freeze (#145) on real Docker

A reader in the agent read 1 MiB at random offsets of the 64 MiB file through the mount, without pause, counting its
reads in a file of the agent's working disk that the host read:

| | |
|---|---|
| Reader counting before the freeze | yes |
| The freeze (`docker pause`) reached `frozen 1` in the container's `cgroup.events` | yes, after 0.238 s |
| Reader counting during 3 s frozen | no |
| Container paused after the thaw | no |
| Reader counting after the thaw | yes |
| The run's collection, with the reader still reading | froze the agent, copied `reader-count` out, 1.19 s, nothing refused |

So a FUSE reader in the agent did not block the pause. The live read-only input test (O20) also passed on this
federation: the agent ran and read the input's script, writing, renaming, chmod-ing, deleting it and creating a file
beside it each failed with `EROFS`, and its working directory stayed writable.

## Accounting for the agent's reads, per file

The agent read 4096 B of a 1 MiB file twice at the same offset (the same bytes both times) and only `stat`ed a 64 KiB
file. What the sidecar could show of that, read before the stop:

- oneclient's log directory held one file, the `INFO` log, 1114 bytes, 17 lines: the start-up settings only (version,
  provider, forced proxy I/O 1, certificate verified 1, file read events disabled 0, I/O buffered 1, monitoring 0,
  read/write performance log 0, the mountpoint). No line named any of the seven fixture files, by REST id, by uuid or by
  name.
- the mount's FUSE connection on the host (`/sys/fs/fuse/connections/<n>`) has four entries, `abort`,
  `congestion_threshold`, `max_background` and `waiting`: nothing per file.
- oneclient's options that may record reads: `--io-trace-log`, `--log-read-write-perf`, `--monitoring-type`,
  `--monitoring-level-basic`, `--monitoring-level-full` and `--monitoring-period` (a Graphite reporter, which needs
  `--graphite-url`), besides `--log-dir`, `--verbose-log-level` (debug builds only) and `--disable-log-buffering`.

With `--io-trace-log`, alone or with `--log-read-write-perf`, the mount appeared (1.7 and 2.5 s) and then the first read
through the mount answered `ENOTCONN`. The sidecar's own output ended `Oneclient received (11) signal - releasing
mountpoint`, and the container had exited (code 0) before the accounting step, so no log was left to read; the
teardown found no mount to unmount, removed both containers and left nothing. So the default log shows nothing per
file, `--io-trace-log` crashes oneclient 25.0 on the first read, and `--log-read-write-perf` alone and the monitoring
reporter are unknown.

## oneclient's read, cache and prefetch options

From `oneclient --help` in the running sidecar (exit 0, 13610 bytes), the options on reading, caching, prefetching,
buffering and direct I/O, with the defaults it prints:

| Option | Default | Help text |
|---|---|---|
| `--force-proxy-io` | off (the sandbox passes it) | Force proxied access to storage via Oneprovider for all spaces |
| `--force-direct-io` | off | Force direct access to storage for all spaces |
| `--no-buffer` | off | Disable in-memory cache for input/output data blocks |
| `--disable-read-events` | off | Disable reporting of file read events |
| `--no-fullblock-read` | off | With it, a read can return less than requested when that much is available and further blocks must be prefetched |
| `--read-buffer-min-size` | 4096 | minimum in-memory cache for input data blocks, bytes |
| `--read-buffer-max-size` | 104857600 | maximum in-memory cache for input data blocks, bytes |
| `--read-buffer-prefetch-duration` | 1 | read-ahead period of that cache, seconds |
| `--read-buffers-total-size` | 2097152000 | total read cache of all open handles, bytes; 0 is unlimited |
| `--min-block-prefetch-size` | 1048576 | minimum prefetch block size, bytes |
| `--prefetch-mode` | async | block prefetch mode, async or sync (experimental) |
| `--seqrd-prefetch-threshold` | 1.000000 | fraction of the file already replicated that triggers replication prefetch (experimental) |
| `--rndrd-prefetch-threshold` | 1.000000 | the same, for random blocks across the file (experimental) |
| `--rndrd-prefetch-eval-frequency` | 50 | |
| `--rndrd-prefetch-block-threshold` | 0 | |
| `--rndrd-prefetch-cluster-window` | 20971520 | cluster window for prefetching, bytes; -1 is the whole file (experimental) |
| `--rndrd-prefetch-cluster-block-threshold` | 5 | |
| `--rndrd-prefetch-cluster-window-grow-factor` | 0.000000 | |
| `--cluster-prefetch-threshold-random` | off | random cluster prefetch threshold selection (experimental) |
| `--metadata-cache-size` | 5000000 | |
| `--readdir-prefetch-size` | 2500 | |
| `--dir-cache-drop-after` | none printed | how long a directory's cache is kept, seconds |
| `--write-buffer-min-size`, `--write-buffer-max-size`, `--write-buffers-total-size`, `--write-buffer-flush-delay` | 20971520, 52428800, 1048576000, 5 | |
| `--buffer-scheduler-thread-count`, `--scheduler-thread-count`, `--storage-helper-thread-count`, `--communicator-pool-size` | 1, 1, 10, 25 | |
| `--provider-timeout`, `--storage-timeout` | 120, 120 | seconds |
| `-o [ --opt ]` | | mount arguments passed to FUSE (the sandbox passes `ro,allow_other`) |

## What a read through the mount places on `pine`

Two files written through `birch` only, of 8 MiB and 64 MiB; `pine` first saw them after 2.1 and 6.1 s. The agent read
each step's ranges through the mount by one descriptor; then `pine`'s block map was read until two reads 2 s apart
agreed, which every step did at the first comparison (2.0 s). Ranges are `[start, length]`; "gained" is what `pine`'s
map covers after the step and did not before. Every read returned all the bytes it asked for:

| File | Read | Took | `pine` held before | Gained | Bytes gained |
|---|---|---|---|---|---|
| 8 MiB | 1 B at 0 | 0.043 s | 0 | `[0, 1048576]` | 1048576 |
| 8 MiB | 1 B at 3145733 | 0.050 s | 1048576 | `[3145733, 1048576]` | 1048576 |
| 8 MiB | 4 KiB at 5242980 | 0.042 s | 2097152 | `[5242980, 1048576]` | 1048576 |
| 8 MiB | 1 MiB at 6291463 (its first 93 B held) | 0.087 s | 3145728 | `[6291556, 1048576]` | 1048576 |
| 8 MiB | 1 MiB at 7336036 (its first 4096 B held) | 0.088 s | 4194304 | `[7340132, 1048476]`, to the end of the file | 1048476 |
| 64 MiB | 1 B at 10485763 | 0.059 s | 0 | `[10485763, 1048576]` | 1048576 |
| 64 MiB | 4 KiB at 20971520 | 0.057 s | 1048576 | `[20971520, 1048576]` | 1048576 |
| 64 MiB | 1 MiB at 31469625 | 0.074 s | 2097152 | `[31469625, 1048576]` | 1048576 |
| 64 MiB | 5 MiB at 41943040 | 0.372 s | 3145728 | `[41943040, 5242880]` | 5242880 |
| 64 MiB | three 1 MiB reads in order from 52428800 | 0.084, 0.061, 0.060 s | 8388608 | `[52428800, 3145728]` | 3145728 |

A read of up to 1 MiB on bytes `pine` lacks placed 1 MiB from the read's own offset, aligned to nothing, as a REST
range read does (`2026-11-11-federation-facts-eviction-and-granularity.md`). A read of more than 1 MiB placed exactly
its range, and three sequential 1 MiB reads placed exactly their 3 MiB; nothing was fetched past them within the 2 s
of the settling read. A read starting inside held bytes gained 1 MiB from the first byte not held, clipped to the end
of the file, so its gain ended 93 B past the read's own end in the first case and at the end of the file in the
second. Through REST, a 4 KiB read starting inside held bytes gained up to 3 MiB past its own offset; through the
mount, these two gained no more than 1 MiB.

The 8 MiB file ended with 5242780 bytes in 3 blocks, the 64 MiB file with 11534336 bytes in 5.

## Teardown of the sandbox

Every run's teardown, the two that failed included: unmount (none to do where oneclient had died), stop and remove
the sidecar, remove the agent; then no container, network, mount, FUSE mount, mount record or run directory of the
sandbox left on the host, the sandbox's own probe finding nothing left under its lease key, and the federation's network present with the same containers on
it as before. Every fixture file was deleted and shown gone (a 404, or 25.0's 400 `posix` `enoent`). Checked by hand
after the last run as well: no `fedsbx-` container or network and no `fuse.oneclient` mount.

## The federation taken down

`federation.py down` took 42 s (containers 38 s, data 4 s); peak memory over the federation's life was 1.01 GiB for
`zone`, 1.33 GiB for `pine`, 1.20 GiB for `birch` and 1.02 GiB for `cedar`. Afterwards `docker ps -a` and `docker
network ls`, filtered by the name `expcore` and by the project's label, listed nothing, nor any `fedsbx-` container or
network; the federation's root directory was gone, and nothing listened on its four ports.
