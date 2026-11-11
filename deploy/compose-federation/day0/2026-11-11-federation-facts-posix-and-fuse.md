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
