# Rootless Onedata on HPC batch nodes

`bundle/` runs one Onezone and Oneproviders on HPC batch nodes, rootless, each service in a setuid Apptainer
writable sandbox started with `--containall`, one service per node and per batch job. It is the platform layer of
issue #179's one-node proof. It is built for batch nodes where users get no network or user namespaces and no ports
below 1024, and where the node's `/tmp` may be shared with other users. The images are the pinned builds of `deploy/compose-federation`
(`docker.onedata.org/oneprovider:ID-765309c84f`, `docker.onedata.org/onezone:ID-ba7a778696`).

Status: a zone and a provider came up on two shared batch nodes in the seventh live run (13 Nov 2026). Both passed
the Couchbase guard and the live port check, the provider registered and showed online, and a file written through
REST read back with the same SHA-256. A whole federation (a zone and five providers) and the agent sandbox on HPC
have not run yet. The live-run notes are kept privately, since they name the site.

**This file names no site.** Everything site-specific comes from `site.env`, a local file that is never committed
(see "Site configuration").

## Exposure model

There is no network namespace. The services share the node's interfaces with every other process on the node, so
the bundle works in layers:

1. **Loopback by default.** The port shim (`portshim.c`, preloaded through the rootfs's `/etc/ld.so.preload` in
   every dynamically linked process of the container) moves every `bind()` to 127.0.0.1 (or `::1`), except TCP 8443
   (from 443) and 6665. A `listen()` off loopback on any other port fails, a multicast bind fails, and a connect to
   this node's own address goes to loopback. The public ports are compiled in, and `ports.sh` names the same two.
2. **The owner check** (`portshim-owner.h`). Loopback is shared with the node's other users, so a loopback TCP
   connection between a socket of ours and one of another uid is refused: at `accept()`, at `connect()` (a listener of
   ours must hold the port) and once connected. The owner comes from `/proc/net/tcp` and `tcp6`.
3. **A per-job epmd port** and Erlang distribution on loopback, with a 256-bit cookie generated on the node.
4. **Couchbase's administrator, confirmed by a guard** (next section).
5. **A short life.** Every job has `--no-requeue` and a `--time`: submit each service just before the run and tear it
   down right after.

**The uid is the boundary.** Nothing separates two processes of the same uid. Any process of the job's account on a
service node reads the work directory, the cookies and the bucket. **The study's agent sandboxes must never run on a
node that hosts an Onedata service.**

What stays exposed on a shared node, for the study to accept: the password-less `onedata` bucket and Couchbase's
Erlang distribution behind loopback and the owner check for the whole job. Couchbase has no administrator for the
one or two seconds between its port opening and onepanel's `cluster-init`. goxdcr (Go, outside the shim) listens on
loopback port 9998 with no owner check. Paths the shim does not see (a static binary, a raw syscall, io_uring,
`sendto` with `MSG_FASTOPEN`) skip the shim. And 8443 and 6665 are open to the cluster network under Onedata's own
authentication, with the Onedata test CA, whose key is public.

## The Couchbase administrator and its guard

Couchbase 4.5 lets any REST caller act as its administrator until one exists. onepanel creates it, as the image
intends: its deployment starts Couchbase, waits for 8091, then runs `couchbase-cli cluster-init` with this launch's
random administrator, then `bucket-create`.

- **The password is 24 random alphanumerics** (`gen_alnum`, unbiased; about 142.9 bits). That is the most the image's
  `couchbase-cli` 4.5.1 accepts (`MAX_LEN_PASSWORD = 24`, `node.py:26`). Over it, `cluster-init` prints an error and
  exits 0 without creating the administrator (`node.py:434-437`), and onepanel, which reads only the exit status, goes
  on: the third live run's 48-character password left Couchbase with no administrator for its whole life.
  `cb_cred_ok` (`lib.sh`) holds every generated, rotated and overlaid pair to these limits.
- **The guard** (`hpcop-cb-init.py --watch`, started by the container's start script with that script's PID) fails
  closed right after onepanel's Couchbase setup. `GET /settings/web` must answer 200 to this launch's administrator
  and name it, **and** 401 without credentials. Anything else (a bucket while there is no administrator, an
  administrator that is not ours, an anonymous request that is answered, Couchbase gone or silent before it has one,
  an error of the guard itself) prints `hpcop: Couchbase guard FAILED: <cause>` into `run.log` and sends SIGTERM to
  the start script, whose trap stops every component. `finish*.sh` fails the start on that line, and on a deployment
  that ends without `hpcop: Couchbase guard passed`. From the host, `cb_guard` repeats both requests before and after
  the password is rotated.

`tests/deploy/test_hpc_couchbase_guard.py` and `test_hpc_credentials.py` run the guard and the password rules
against `tests/fake_couchbase.py`, a loopback stand-in with Couchbase 4.5's access rules (O48).

## Site configuration

Copy `site.env.example` to `site.env` beside it, fill it in on the login node, and source it before the steps.
`site.env` is ignored by git: never commit it, and never paste its values into an issue, a PR or a session log.

| Key | What |
|---|---|
| `HPC_ACCOUNT` | the grant the jobs are charged to (`<account>`) |
| `HPC_PARTITION` | the partition (`<partition>`) |
| `B` | the bundle on a filesystem the nodes see (`<shared_root>/hpc-federation/bundle`), `chmod -R go-w` |
| `SH` | a 0700 directory for the two rootfs tarballs, each with its `.provenance` (SHA-256): single files only (`<scratch_root>/hpcop-images`) |
| `L` | a 0700 directory for the batch logs (`<scratch_root>/hpcop-logs`) |

Node names, addresses and job ids are typed at the prompt or read from Slurm; they never go into a file of this
repository.

## Steps

Nothing secret is typed, set in the environment or passed as an argument: every secret is generated on the node,
inside the job, and moves only through 0600 files and stdin. Every `srun` uses `--export=PATH` and, except a step
whose stdin is the payload, ends in `</dev/null`.

```bash
source deploy/hpc-federation/site.env
# 0. The two rootfs tarballs, once per image.
sbatch --account="$HPC_ACCOUNT" --partition="$HPC_PARTITION" --no-requeue --time=01:00:00 --cpus-per-task=4 \
  --mem=24G --output="$L/build-op-%j.log" --wrap "bash $B/build-rootfs.sh op $SH/oneprovider.tar"
sbatch --account="$HPC_ACCOUNT" --partition="$HPC_PARTITION" --no-requeue --time=01:00:00 --cpus-per-task=4 \
  --mem=24G --output="$L/build-oz-%j.log" --wrap "bash $B/build-rootfs.sh oz $SH/onezone.tar"
# 1. The zone on node A, listing every provider's address as a peer.
ZJ=$(sbatch --parsable --account="$HPC_ACCOUNT" --partition="$HPC_PARTITION" --nodelist=<node-A> --time=03:30:00 \
  --mem=32G --output="$L/hpcop-zone-%j.log" "$B/zone.sbatch" "$B" "$SH/onezone.tar" <zone-name> <provider-address>)
# 2. A provider on node B, registered to the zone with a single-use token that travels only through the pipe.
PJ=$(sbatch --parsable --account="$HPC_ACCOUNT" --partition="$HPC_PARTITION" --nodelist=<node-B> --time=03:00:00 \
  --mem=32G --output="$L/hpcop-provider-%j.log" "$B/provider.sbatch" "$B" "$SH/oneprovider.tar" <provider-name> \
  <zone-fqdn> <admin-email> <zone-address>)
srun --jobid="$ZJ" --overlap --export=PATH -n1 -c1 --mem=200M bash "$B/mint-registration-token.sh" <provider-name> \
  </dev/null | srun --jobid="$PJ" --overlap --export=PATH -n1 -c1 --mem=1G bash "$B/deliver-token.sh"
# 3. Teardown: providers first, then the zone.
srun --jobid="$PJ" --overlap --export=PATH -n1 -c1 --mem=100M bash "$B/teardown.sh" op </dev/null
srun --jobid="$ZJ" --overlap --export=PATH -n1 -c1 --mem=100M bash "$B/teardown.sh" oz </dev/null
scancel "$PJ" "$ZJ"
```

The token delivery step needs about 1 GB: the seventh live run's step was killed for memory under `--mem=100M`.
Each batch job runs its service from the batch step itself (`node-init.sh`, `configure.sh`, `run-*.sh`,
`finish*.sh`), so polling costs no job steps; read a work directory's `run.log` with
`srun --jobid=<job> --overlap --export=PATH -n1 cat <workdir>/run.log </dev/null`. On a failed start the batch
script stops the service, deletes and redacts every secret (`scrub.py`) and keeps the logs for
`HPCOP_INSPECT_SECONDS`. `--exclusive` keeps other users off a node, at the price of the whole node for the job.

## The scripts

| Script | Where | Purpose |
|---|---|---|
| `lib.sh`, `patch.sh` | sourced | the job's `TMPDIR`, private 0700 work directories, secrets through builtins and 0600 files, the Couchbase limits (`gen_alnum`, `cb_cred_ok`), the host-side guard (`cb_guard`) and rotation, bounded stop, the failure-path scrub, the verified patches |
| `ports.sh` | sourced | the port policy, the pre-start and live port checks, the job's epmd port |
| `prep.sh`, `prep-zone.sh` | node | patch a provider or zone sandbox; every patch asserted |
| `portshim.c`, `portshim-owner.h` | in container | the default-deny shim and the owner check |
| `hpcop-cb-init.py` | in container | `--watch`: the Couchbase guard; `--wait-listener`: the launcher's wait for our 9443 |
| `build-rootfs.sh` | job | pull, build the shim with the image's gcc, prep, preload check, tar, SHA-256 |
| `node-init.sh` | batch step | node checks, leftovers of earlier jobs removed, tarball checks, work directory, per-launch secrets, file default check |
| `configure.sh`, `run-node.sh`, `run-zone.sh` | batch step | configuration, peers, the service under `supervise.py` |
| `finish.sh`, `finish-zone.sh` | batch step | readiness and the guard's verdict, the host-side guard, storage, REST checks, rotation, live default and port check |
| `check-defaults.sh` | node | fails on an image default, a leaked secret or a listener outside the model |
| `mint-registration-token.sh`, `deliver-token.sh` | steps | a 1 h single-use registration token, from the zone's job into a 0600 file of the provider's |
| `teardown.sh` | step | graceful stop of the recorded process only, then the work directory |
| `zone.sbatch`, `provider.sbatch` | batch | hold the service for the job's life; `--no-requeue` and a `--time` |
| `supervise.py`, `scrub.py`, `onboard.py`, `jsonget.py`, `tokenbody.py`, `probe.sh` | node | the service process and its signals, redaction, space support, JSON helpers, read-only node checks |

## What the repository's tests cover

`tests/deploy/test_hpc_*.py` run the Couchbase guard against the stand-in, the password rules, a 3.8 parse of every
Python file (the guard runs on the images' Python 3.8), the batch jobs' `--no-requeue` and `--time`, the shim's
public ports against `ports.sh`, and, where `gcc` exists, the shim's build with every warning an error and its unit
test (`tests/shim_unit.c`). The bundle's wider offline suite reads files copied out of the two pinned images
(patch drift, the images' binaries under the shim) and is not in the repository; neither are those files.
