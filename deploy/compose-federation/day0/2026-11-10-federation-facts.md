# The Compose federation's baseline facts, 10 Nov 2026

The day-0 checks' own report is beside this file (`day0-20261110T150215Z.md` and `.json`, mutation allowed). This file
holds what that report does not: the releases, the live contract tests, the fault test, a partially written file read
back, what listens on the host, the timings of up, reset and down, and what could not be measured. "Unknown" means not
measured; it is never a pass. Every name below is the federation's own invented one.

## The deployment

| Fact | Value |
|---|---|
| Onezone | 25.0, build 46-g14b5bda7 (`docker.onedata.org/onezone:ID-ba7a778696`) |
| Oneproviders `pine`, `birch`, `cedar` | 25.0, build 81-gf118ed9007 (`docker.onedata.org/oneprovider:ID-765309c84f`) |
| Study space | `alpha-space`, supported by all three; `pine` is the data provider and the agent's local provider |
| Other space | `beta-space`, supported by `birch` and `cedar`, outside the prefix `alpha-` |
| TLS | the federation's own CA; the core trusts it through `[tls] ca_file`; verification on everywhere |
| `platform.toml` | written by a fresh tester from the guides (`../platform.toml.template`) |

## U5: where the guides fell short

A fresh tester, given `docs/study-guide.md`, `docs/interfaces.md`, the example study and `facts.json` only, wrote a
`platform.toml` that loads and runs day-0 unchanged. On the way:

- Neither guide gives the schema of `platform.toml`: only `[tls]`, and in passing `[sandbox]`, `[slots]`,
  `[principals.data_path]` and `[toolserver]`, are named; `[zone]`, `[providers]`, `[space]` (with `prefixes`),
  `[support]`, `[credentials]`, `[principals]` and `[placement]` are not.
- Neither names the day-0 command (`python -m expcore_onedata.day0 PLATFORM --out DIR`), its checks, its exit codes,
  or `EXPCORE_LIVE_MUTATE=1` for the two that change state; `python -m expcore --help` does not mention it.
- Neither says which table each check needs (`health` needs `[principals.service]`, `tokens` and `read_only`
  `[principals.data_path]`, `round_trip` `[placement] holder`), nor that credential files must be 0600 and the caller's.
- Nothing warns that `content_type` passes without `[sandbox]`, measuring nothing, or says what `read_only` and
  `round_trip` do to a federation.
- **The first point it had to open `src/`:** how to run day-0, answered by `src/expcore_onedata/day0/__main__.py`;
  then the schema, answered only by the docstring of `src/expcore_onedata/config.py`.

## Day-0 checks (`day0-20261110T150215Z.md`)

5 pass, 1 fail, 1 unknown, exit status 1.

- **reachability, health, tokens, listing: pass.** Every provider answered its configuration and is online; the space is
  supported by exactly the declared three; the data path's token was minted and examined against its exact caveats;
  the root lists on all three, and a paged walk agrees.
- **content_type: pass, but nothing was measured.** No `[sandbox]` is configured, so no data path is REST and the
  check reads nothing. Treat it as unknown here.
- **read_only: unknown.** No write was sent: the mutation gate first reads the root's `path` attribute, which this
  release does not have (400 `badValueNotAllowed`), and the check reports that 400 as a refusal it could not read.
- **round_trip: fail**, for the same reason, before any step; its cleanup was verified.
- The tool-server plane was not checked: no tool server is installed.

## Live contract tests

`EXPCORE_LIVE_PLATFORM=<root>/platform.toml EXPCORE_LIVE_CONTENT_PATH=hello.txt EXPCORE_LIVE_MUTATE=1
EXPCORE_LIVE_SLOW=1 uv run pytest tests/live`: 10 passed, 3 failed, 2 skipped.

| Test | Result |
|---|---|
| a user mints its own token, examined cleanly (`subject.id`) | pass |
| the study space resolves to its declared name (`GET /spaces/{space}`) | pass |
| discovery finds the space and the local provider (providers' `name`, `online`) | pass |
| the health check reads the declared support set | pass |
| a file's content, whole and by range, is `application/octet-stream` (200, 206) | pass |
| a token stops working when its lifetime ends | pass: refused with 400 `tokenCaveatUnverified` |
| the read-only day-0 checks do not fail | pass |
| a block map has the documented shape | pass |
| the transfer listing answers every state, with a `Date` header | pass |
| a transfer record has the fields the probes read | pass |
| a file resolves to its space through its `path` attribute | **fail**: 400 `badValueNotAllowed` |
| the read-only day-0 check, run as a test | **fail**: as above |
| the round-trip day-0 check, run as a test | **fail**: as above |
| one test per tool-server condition | skipped: no tool server, so no condition |
| the sandbox tests (`EXPCORE_LIVE_DOCKER`) | not run: a sandbox's containers and networks are outside this project |

A transfer record read directly, for a replication and for an eviction, has `type`, `replicatingProviderId`,
`evictingProviderId` (the other one `null`), `scheduleTime`, `startTime` and `finishTime` in epoch seconds, the state in
`transferStatus` and the eviction counter in `filesEvicted`.

## Fault injection

The fault test of the fault-injector branch (#27, run from its tree, against this project's Compose driver, with a
copy of `platform.toml` without `[tls]`, which that branch predates, and with `[faults]` naming the three services):
pass. It took `birch` down, saw it down, brought it back with a new container identity, and left no injection open.
Reset afterwards waited 47 s for `birch` to come back online in the zone.

## A file at its final size with only a prefix written

Measured for the progressive-ingest instrument (#107) with `uv run python deploy/compose-federation/measure_partial_file.py`,
as the admin, in `alpha-space`. `{pine}` and `{birch}` are the providers' REST bases (`https://<loopback>:<port>/api/v3/oneprovider`),
every call carries the admin's `X-Auth-Token`, and the prefix holds no zero byte.

```text
POST {pine}/data/{root}/children?name=ingest-partial.bin&type=REG                                     201
PUT  {pine}/data/{file}/content?offset=0        body: 65536 bytes, application/octet-stream            204
PUT  {pine}/data/{file}/content?offset=1048576  body: empty                                            204
GET  {pine}/data/{file}?attribute=size                                                                 1048576
GET  {pine}/data/{file}/distribution                                       pine: blocks [[0, 65536]], physicalSize 65536
GET  {pine}/data/{file}/content   Range: bytes=65536-69631
GET  {pine}/data/{file}/content   Range: bytes=65520-65551
GET  {pine}/data/{file}/content   Range: bytes=0-1048575
GET  {pine}/data/{file}/content
GET  {birch}/data/{file}/content  Range: bytes=0-4095           (before and after birch sees the file)
GET  {birch}/data/{file}/content  Range: bytes=65536-69631
GET  {birch}/data/{file}/content
DELETE {pine}/data/{file}                                                                              204
```

**Size and block map while the range is unwritten.** The empty write at offset 1 MiB sets the size to 1048576 and writes
nothing: the size reads 1048576 and `pine`'s `virtualSize` is 1048576, while its block map holds only `[[0, 65536]]`
and its `physicalSize` is 65536. The other providers show `virtualSize` 0 and no blocks until they see the file, then
`virtualSize` 1048576. After `birch` read the file, its block map held `[[0, 65536]]`, the prefix it fetched on demand;
the unwritten range stays without blocks everywhere.

**Through the API data path**, the provider's content endpoint that the data gate forwards to:

| Read | Via | Status | Body | Unwritten bytes | Seconds |
|---|---|---|---|---|---|
| bytes 65536-69631 (unwritten) | `pine` | 206, `bytes 65536-69631/1048576`, octet-stream | 4096 bytes | all zero | 0.02 |
| bytes 65520-65551 (across the prefix's end) | `pine` | 206, octet-stream | 32 bytes: 16 of the prefix, 16 zero | all zero | 0.03 |
| bytes 0-1048575 | `pine` | 206, octet-stream | 1048576 bytes, the size | all zero | 0.05 |
| whole, no range | `pine` | 200, octet-stream | 1048576 bytes, the size | all zero | 0.05 |
| bytes 0-4095, before `birch` sees the file | `birch` | 400 `posix` (`enoent`), JSON | 387 bytes | none | 0.01 |
| bytes 0-4095, once it does (after 6.1 s) | `birch` | 206, octet-stream | the prefix's bytes | none | 0.10 |
| bytes 65536-69631 | `birch` | 206, octet-stream | 4096 bytes | all zero | 0.02 |
| whole, no range | `birch` | 200, octet-stream | 1048576 bytes, the size | all zero | 0.05 |

So an unwritten range reads as zeros, at once, at its full length, with no error, no short read and no wait: only the
block map tells it from written zeros. Every 200 and 206 above is one the gate streams (`gateboundary.streams`); the
400 a provider gives a file it has not seen yet is one the gate buffers and passes to the agent.

- **The data gate itself: unknown.** The gate runs only inside a sandbox, whose containers are outside this project;
  the answers above are what it would forward.
- **Through the mount (oneclient in the sandbox): unknown.** Neither pinned image holds oneclient, a FUSE mount needs a
  privileged container, and the sandbox starts containers outside this project; a oneclient image is on the host but
  is not one of the two images this federation may use.
- **Writing through the provider's own write path: unknown.** The only other way to write into a provider is that
  same mount; writing into its storage directly changes no metadata, since no storage import is configured.

## What listens on the host

Only the four loopback ports, each held by one `docker-proxy` of this project (the only four on the host). `ss -ltnp`
shows no process for them, because the proxies run as root; the proxies' own arguments tie each port to the project's
network (its address withheld):

```text
$ ss -ltnp | grep -E ':2944[3-6]\b'
LISTEN 0      4096      127.0.0.1:29446      0.0.0.0:*
LISTEN 0      4096      127.0.0.1:29444      0.0.0.0:*
LISTEN 0      4096      127.0.0.1:29445      0.0.0.0:*
LISTEN 0      4096      127.0.0.1:29443      0.0.0.0:*
$ ps -C docker-proxy -o args=
/usr/bin/docker-proxy -proto tcp -host-ip 127.0.0.1 -host-port 29443 -container-ip <project network> -container-port 443 -use-listen-fd
/usr/bin/docker-proxy -proto tcp -host-ip 127.0.0.1 -host-port 29444 -container-ip <project network> -container-port 443 -use-listen-fd
/usr/bin/docker-proxy -proto tcp -host-ip 127.0.0.1 -host-port 29445 -container-ip <project network> -container-port 443 -use-listen-fd
/usr/bin/docker-proxy -proto tcp -host-ip 127.0.0.1 -host-port 29446 -container-ip <project network> -container-port 443 -use-listen-fd
$ docker ps --filter label=com.docker.compose.project=expcore-fed --format '{{.Names}} {{.Ports}}'
expcore-fed-pine-1 80/tcp, 6665/tcp, 9443/tcp, 127.0.0.1:29444->443/tcp
expcore-fed-cedar-1 80/tcp, 6665/tcp, 9443/tcp, 127.0.0.1:29446->443/tcp
expcore-fed-birch-1 80/tcp, 6665/tcp, 9443/tcp, 127.0.0.1:29445->443/tcp
expcore-fed-zone-1 53/tcp, 80/tcp, 9443/tcp, 53/udp, 127.0.0.1:29443->443/tcp
```

The other ports (the transfer port 6665, the panels' 9443, the zone's 53 and 80) are exposed by the images and reach
only the project's network.

## Up, reset and down

Run in this order, each from the state the one before it left; every command exited 0, and the last down left no
container, network or file of the project and no listener.

| Step | Command | Seconds | Phases |
|---|---|---|---|
| 0a | down (the instance the facts above were measured on) | 37 | containers 35, data 2 |
| 0b | down again, already down | 0 | nothing to do |
| 1a | up from clean | 228 | prepare 2, zone 71, providers 78, spaces 6, baseline 70 |
| 1b | up again, already up | 2 | every step checked, nothing changed |
| 1c | reset | 36 | baseline 35 (5 entries removed and written again) |
| 1d | reset again | 34 | baseline 32 |
| 1e | down | 35 | containers 33, data 2 |
| 2a | up from clean | 221 | prepare 2, zone 66, providers 78, spaces 6, baseline 69 |
| 2b | reset | 34 | baseline 32 |
| 2c | down | 36 | containers 34, data 2 |

A reset after the fault test, which first waits for the restarted provider, took 85 s (providers 47).
