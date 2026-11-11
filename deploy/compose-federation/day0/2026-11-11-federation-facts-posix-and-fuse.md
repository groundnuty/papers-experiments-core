# The Compose federation after the posix answers, with a FUSE probe, 11 Nov 2026

A third run, on a fresh `up` of the same federation, after the core learned the three answers of Onedata 25.0 the
second run found (`2026-11-10-federation-facts-parent-walk.md`): a content upload is marked
`application/octet-stream`, a write refused as 400 `posix` `eacces` counts as refused, and an entry read as 400
`posix` `enoent` counts as gone, each matched exactly. The gate's walk also keeps each provider's links apart, bounded,
and walks stale ones again. The earlier facts files stay as they were. The day-0 checks' own report is beside this file
(`day0-20261111T003902Z.md` and `.json`, mutation allowed). "Unknown" means not measured; it is never a pass. Every name
below is the federation's own invented one.

## Day-0 checks (`day0-20261111T003902Z.md`)

5 pass, 1 fail, 1 unknown, exit status 1, in 8 s (the second run took about ten minutes).

- **reachability, health, tokens, listing: pass**, as before.
- **content_type: unknown**, as on the second run: no `[sandbox]` is configured, so nothing was measured.
- **read_only: pass.** Each provider was sent the write and answered 400 `posix` `eacces`, now a refusal; the rows hold
  the status, the error id and the errno.
- **round_trip: fail, at its second step, with the scratch removed and that verified.** The upload now succeeds (step
  `created`), and the teardown confirms the scratch gone on consecutive reads at once (`cleanup_verified: true`).
  What fails is new: right after the file is made through `pine`, the placement reads its block map through the
  receiver, `birch`, which answers `GET /data/{file}/distribution` with 404 `notFound`, since it does not see the file
  yet (the second run measured about 6 s before `birch` sees a new file). Placement does not wait for the receiver to
  see the file (O07). That is outside the three fixes and left for a decision.

## Live contract tests

`EXPCORE_LIVE_PLATFORM=<root>/platform.toml EXPCORE_LIVE_CONTENT_PATH=hello.txt EXPCORE_LIVE_MUTATE=1
EXPCORE_LIVE_SLOW=1 uv run pytest tests/live/test_contract.py`: 13 passed, 1 failed, 1 skipped, in 39 s (616 s on the
second run). Against the second run, the read-only day-0 check, run as a test, now passes and shows every write
refused; the round-trip day-0 check still fails, at the receiver's 404 above, with its scratch removed and verified.
The skip is the tool-server test, with no tool server installed; the sandbox tests (`EXPCORE_LIVE_DOCKER`) were not
run, as before.

## A file at its final size with only a prefix written, through the mount: not measured

The mount could not be made. The probe ran one short-lived container of the pinned `onedata/oneclient:25.0` (already
on the host, never pulled) on the project's network, labelled as the project's, with `--device /dev/fuse` and
`--cap-add SYS_ADMIN` and nothing more, and removed it when it ended. The commands, with the scratch directory as
`<scratch>`; the token is in no command, only in a file read inside the container:

```text
uv run python <scratch>/fuse_partial.py prepare <root>/platform.toml <scratch>/token
    made fuse-partial.bin in alpha-space's root through pine: 1048576 bytes long (size read back over REST),
    only the first 65536 written, none of them zero; wrote a read-only data-path token for the oneclient
    interface, scoped to alpha-space, to <scratch>/token, mode 0600

docker run --rm --name expcore-fed-fuse-probe --label com.docker.compose.project=expcore-fed \
  --network expcore-fed_fed --pull never --device /dev/fuse --cap-add SYS_ADMIN \
  --mount type=bind,src=<scratch>/token,dst=/run/probe/token,readonly \
  --mount type=bind,src=<root>/ca/federation-ca.pem,dst=/etc/expcore/federation-ca.pem,readonly \
  --mount type=bind,src=<scratch>/probe.sh,dst=/run/probe/probe.sh,readonly \
  --entrypoint /run/probe/probe.sh onedata/oneclient:25.0

    inside, for pine and then birch: unset ONECLIENT_INSECURE; SSL_CERT_FILE=/etc/expcore/federation-ca.pem;
    ONECLIENT_ACCESS_TOKEN="$(cat /run/probe/token)" oneclient -f -H <provider>.expcore-fed.test -o ro /mnt/oc-<provider>
    then, once mounted: stat; dd of bytes 0-4095, 65536-69631, 65520-65551, the whole file, and past its end,
    each timed, counting the bytes and the zero bytes returned

uv run python <scratch>/fuse_partial.py cleanup <root>/platform.toml <scratch>/token
    deleted the file (the root no longer lists it) and removed the token file
```

What it printed, for each provider:

```text
Connecting to provider '<provider>.expcore-fed.test:443' using session ID: '<number>'...
Getting configuration...
fuse: mount failed: Permission denied
mount /mnt/oc-<provider>: not up after 60 s
mounts left: 0
```

So oneclient connected to each provider, with verification on against the federation's CA, and asked for its
configuration; its next step, the FUSE mount, was refused by the kernel. Whether the token would have been accepted
for reading is not shown. The daemon runs AppArmor, and the container ran under
Docker's default profile, whose rule against mounting is an explicit deny, which the kernel does not log; the kernel log
held no denial, so the cause is consistent with that profile but not shown by a log line. The core's own mount sidecar
runs with `--security-opt apparmor:unconfined` beside the same two privileges (`sandbox/containers.py`), which is beyond
what this probe was allowed, so no further try was made. What a read of a not-yet-written range returns through the
mount (zeros, a block, a short read or an error), how long it takes and what stat reports stay **unknown**; through the
REST data path the second run measured zeros at once, at full length.

After the probe, `docker ps -a` filtered to the project listed only its four services, and the root of `alpha-space`
no longer held the probe's file.

## Directory sizes per provider, and one file's distribution read

Measured on a fourth fresh `up` later the same day (the third had been taken down), as the admin, with
`../measure_dir_distribution.py` and direct reads; `<base>` is a provider's REST base, `<zone>` the zone's. These
measurements decide how long a per-run reset takes. The host was shared with other agents' test suites throughout.

### The calls and what 25.0 answers

| Call | Answer |
|---|---|
| `GET <base>/data/{dir}/distribution`, the space root or a subdirectory | 200, the same shape through each provider: `{"type": "DIR", "distributionPerProvider": {<provider id>: {"virtualSize": int, "logicalSize": int, "success": bool, "locationsPerStorageBackend": {<storage id>: {"success": bool, "location": str or null}}, "distributionPerStorageBackend": {<storage id>: {"success": bool, "physicalSize": int}}}}}`. No blocks, no timestamp, no flag that a figure is being computed; `location` is null for a provider that holds nothing |
| `GET <base>/data/{dir}?attribute=size` | 200 `{"size": int}`: the directory's logical size, as the provider asked sees it |
| `GET <base>/data/{dir}/dir_size_stats?mode=layout` | 200: the series `dir_count`, `dir_error_count`, `file_error_count`, `logical_size`, `physical_size_<storage id>`, `reg_file_and_link_count` and `virtual_size`, each by `minute`, `hour`, `day` and `month`. Another `mode` is 400 `badValueNotAllowed`, allowed `layout` and `slice` |
| `GET <base>/data/{dir}/dir_size_stats`, JSON body `{"mode": "slice", "layout": {"logical_size": ["minute"]}}` | 200 `{"slice": {"logical_size": {"minute": [{"value": int, "timestamp": int}, ...]}}}`, newest first, each window's start in epoch seconds. With `layout` in the query string, 400 `badValueJSON` |
| `GET <base>/data/{dir}/dir_stats` | 404 |
| `GET <zone>/spaces/{space}` | `supportParametersRegistry`, per provider `{"dirStatsServiceStatus": "enabled", "dirStatsServiceEnabled": true, "accountingEnabled": false}`, and `providers`, per provider its support size |

So 25.0 gives a directory's size per provider over REST: physical bytes per storage backend and logical and virtual
sizes, in the directory's distribution, and the same as time series in `dir_size_stats`. Nothing says whether a figure
is current, beyond the series' window timestamps and the service's state, `enabled` on all three providers here.

### How fast they follow a change

The space root against the sum of its files' physical sizes, per provider, over all 1204 regular files, each file's
own distribution read through `pine`:

| When | pine | birch | cedar | The root's distribution, through each provider |
|---|---|---|---|---|
| Baseline | 1395520 | 1048576 | 0 | equal at the first read |
| After replicating `samples/sample-1.bin` (65536 bytes) to `cedar` | 1395520 | 1048576 | 65536 | equal at the first read after the transfer's end, 0.1 s |
| After evicting it from `cedar` again | 1395520 | 1048576 | 0 | equal at the first read after the transfer's end, 0.1 s |

The transfer's end is seen by polling its record every 0.5 s, so up to that much may hide in the 0.1 s; a full recount
after each change equalled what was expected.

A write and a delete in `samples/` (300000 bytes, through `pine`; polls every 0.5 s, bound 180 s):

| Figure | Through pine | Through birch |
|---|---|---|
| `pine`'s logical and physical size in the distribution | 0.1 s | 0.1 s |
| `birch`'s and `cedar`'s logical size in the distribution | 5.6 s after the write, 4.5 s after the delete | the same |
| The `size` attribute | 0.1 s | 5.6 s, 4.5 s |
| `dir_size_stats`, `logical_size`, newest minute | 0.1 s | 5.6 s, 4.5 s |

The providers that hold none of the bytes kept a physical size of 0 throughout. An earlier, coarser run (1 s polls)
measured 6.5 s and 7.2 s through `birch`. So a provider's physical bytes in a directory's distribution follow a
placement or an eviction at once, while another provider's logical figures follow a write or a delete about 5 s later.

### One file's distribution read

`GET <base>/data/{file}/distribution` for `hello.txt` (64 bytes, held by `pine` alone), 200 reads after one warm-up:

| Through | Holds the file | Concurrency | Median ms | 95th percentile ms | Max ms | Reads per second |
|---|---|---|---|---|---|---|
| pine | yes | 1 | 8.88 | 17.10 | 48.26 | 97 |
| pine | yes | 4 | 14.84 | 31.85 | 74.32 | 239 |
| birch | no | 1 | 8.59 | 18.54 | 50.19 | 96 |
| birch | no | 4 | 14.24 | 30.90 | 50.52 | 238 |

The same probe's first run measured 7.29 and 12.77, 11.33 and 21.96, 9.33 and 21.84, and 14.29 and 27.76 (median and
95th percentile, in the order above). The owner and a non-owner answer alike; four at once makes each read about 1.7
times slower at the median and the throughput about 2.5 times higher.
