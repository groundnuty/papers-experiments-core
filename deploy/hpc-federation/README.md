# Rootless Onedata on HPC batch nodes

`bundle/` runs one Onezone and Oneproviders on HPC batch nodes, rootless, each service in a setuid Apptainer
writable sandbox started with `--containall`, one service per node and per batch job. It is the platform layer of
issue #179's one-node proof. It is built for batch nodes where users get no network or user namespaces and no ports
below 1024, and where the node's `/tmp` may be shared with other users. The images are the pinned builds of `deploy/compose-federation`
(`docker.onedata.org/oneprovider:ID-765309c84f`, `docker.onedata.org/onezone:ID-ba7a778696`).

Status: what has run live, and what has not, is in the dated facts files under `facts/` (the live-run notes
themselves are kept privately, since they name the site). The agent's egress detection (below) has not run live.
`hpcfed.py` ("The driver", below), which brings a whole federation up and down from a layout file, has not run live.

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

**What stays exposed on a shared node,** for the study to accept. Each of these was accepted before the live runs;
"co-located" means another user with a job on the same node, and `--exclusive` removes them all:

- **Behind loopback and the owner check for the whole job:** the password-less `onedata` bucket (onepanel 25.0
  cannot give it a password), Couchbase's Erlang distribution with its time-seeded cookies, and memcached's `_admin`
  user (128 random bits).
- **Short windows before a secret is in place:**
  - Couchbase has no administrator for the one or two seconds between its port opening and onepanel's
    `cluster-init`;
  - onepanel's 9443 waits about 50 ms for its passphrase.
- **Secrets on a command line for seconds, in the image's own code:** onepanel passes the deploy-time Couchbase
  password on `couchbase-cli`'s command line, and Couchbase's stop passes its cookie on `couchbase-server -k`'s. No
  setting of the image avoids either. `node-init.sh` refuses a node whose `/proc` has no `hidepid`, which hides both
  from other users, and the password is rotated right after the deployment. The bundle's own scripts put no secret
  on a command line, a submit line or in the environment.
- **Clients outside the shim:** goxdcr (Go) dials 8091 and 11210 with Couchbase's internal credentials and listens
  on loopback port 9998, all with no owner check. The host-side `curl` calls of `finish*.sh`, `cb_guard`, `cb_rotate`
  and `mint-registration-token.sh` carry the panel passphrase, the Couchbase administrator and the zone admin's
  password, and they run outside the container, with no owner check on the far end. They dial only once our
  listeners hold those ports, and the kernel keeps another uid off a port while our listener holds it. What is left
  is a listener of ours closing (a Couchbase or onepanel restart) and another user binding its port before the call.
- **Denial of service:** a co-located user can squat a port we are about to bind, flood a loopback listener (one
  `accept()` drops at most 8 foreign connections, then returns), or make the owner check's table reads slower by
  opening sockets.
- **Paths the shim does not see** (a static binary, a raw syscall, io_uring, `sendto` with `MSG_FASTOPEN`) skip the
  shim. The live port check fails on any listener of ours off loopback that one of them opens.
- **An earlier job's work directory** that a SIGKILL left in `/tmp` (0700, root can read it) stays until the next job
  of ours on that node removes it.
- **8443 and 6665** are open to the cluster network under Onedata's own authentication, with the Onedata test CA,
  whose key is public, so a client on the path cannot tell our zone from an impostor.

## The agent's egress: detected, not prevented

On these nodes the agent's network cannot be restricted: it reaches the internet (the standing run of 14 Nov reached a
public host from the agent's cell). The study's PI ruled that egress is acceptable here if it is detected, so every
run's outbound connections are logged and reported, and the analysis is run again without the flagged runs
(`expcore.egress`; `docs/interfaces.md`, "Egress where it cannot be blocked").

- **`egresslog.c`**, the agent's shim, loaded through the cell's `/etc/ld.so.preload` (a read-only bind), so every
  dynamically linked process loads it, a child started with a cleared environment included. The services' port shim
  is not loaded there: the agent's network behaves as without a shim. It sends one record per `connect()` (before the
  call), its result (after), each non-blocking connect's outcome (`getsockopt(SO_ERROR)`) and each `sendto`,
  `sendmsg` or `sendmmsg` to an address the process has not sent to: operation, pid, descriptor, time, socket type,
  family, address, port and result. **Never a payload.** Each record goes on a fresh Unix datagram socket to a path
  compiled in (`/hpcop-egress/sock`), so a child that closes every inherited descriptor still logs, and nothing the
  agent writes can be mistaken for the log. A connect, or a send to a new address, whose record cannot be delivered is
  refused with `EACCES`: through the shim, a destination is logged or not reached.
- **`egress-collector.py`**, outside the cell in the run's sandbox step: it binds the socket in an empty 0700
  directory that the cell sees read-only, and writes every record, with the sender's pid and uid as the kernel attests
  them, to a log it creates in a directory the cell never sees. Each line is numbered; the first holds the run's nonce,
  the last the counts, written when the collector is stopped. A datagram that is no record is kept as its size and
  SHA-256 only.
- **The canary.** Before the agent runs, the run driver connects once to the gate from inside the cell, under the same
  binds. The log's first record must be that connect, or the core reads the log as unknown: a shim that never loaded
  would otherwise leave an empty log that reads as clean.

**The limits.** Detection, not prevention, and the log is evidence, not a barrier:

- **What passes the shim:** a statically linked program or a Go binary, raw system calls (`syscall(2)`, inline
  assembly), io_uring, the C library's own DNS queries (glibc's resolver calls the kernel through internal aliases a
  preload cannot interpose; the connect that follows a lookup is logged), and a process that never loads the shim.
  The static scan of the agent's code cells is the second line of detection, and it reads text: a name or a call built
  at run time passes it. Neither line prevents egress.
- **Under one uid,** the agent cannot see or signal the collector (the cell has its own PID namespace,
  `--containall`) or reach its log, but it can send forged datagrams to the socket. They add lines and never remove
  one, and a malformed one makes the log unknown.
- **A collector that dies mid-run** makes every later connect of the agent fail with `EACCES`, its data path's too, and
  leaves a log without an end line, which reads as unknown.

`tests/deploy/test_hpc_egress_shim.py` builds the shim with the host's gcc and runs it against
`tests/egress_probe.c`, which connects to a loopback listener and, non-blocking, to 192.0.2.1 (TEST-NET-1, never
routed), so no test sends outside traffic. It needs Unix sockets, which some sandboxes forbid; it skips there.

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
against `tests/fake_couchbase.py`, a loopback stand-in with Couchbase 4.5's access rules (O53).

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

The steps below start one zone and one provider by hand. A whole run (a zone, three providers, the agent's cell with
its egress log, teardown and release, each step writing raw evidence that `run/evidence.py` turns into the run's
summary), and the steps planned for the next live run, are in [`run/README.md`](run/README.md).

Nothing secret is typed, set in the environment or passed as an argument by these steps or the bundle's scripts:
every secret is generated on the node, inside the job, and moves only through 0600 files and stdin (the image's
own exceptions are listed under "What stays exposed"). Every `srun` uses `--export=PATH` and, except a step
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
`HPCOP_INSPECT_SECONDS`. A job whose service stopped at `teardown.sh`'s request ends with status 0; a service that
ends any other way fails the job, with its own status. `--exclusive` keeps other users off a node, at the price of
the whole node for the job.

## The driver: `hpcfed.py`

`hpcfed.py` brings a zone and the layout's providers up on batch nodes, and takes them down, from the Compose
federation's own layout file; `DESIGN.md` is its process model (one held job, every service and control action an
`srun --exact` step from the login node, the state file, the service verbs). **Written and tested offline against a
fake Slurm; not run live.**

```bash
set -a; . deploy/hpc-federation/site.env; set +a
uv run python deploy/hpc-federation/hpcfed.py --layout deploy/hpc-federation/layouts/pilot-shape.toml \
  --root <a directory on a filesystem the login node and the nodes see> describe   # the plan and what it bills
uv run python deploy/hpc-federation/hpcfed.py --layout <layout> --root <root> up
uv run python deploy/hpc-federation/hpcfed.py --layout <layout> --root <root> down
```

- **The layout** is a `deploy/compose-federation` layout (its providers, spaces, principals, probe space and platform
  template) with an `[hpc]` table of sizes, which Compose ignores: the held job's `--time`, each service step's cores
  and memory, every node's control reserve, `stop_seconds`, and where the core's phase runs (`core = "login"` or
  `"step"`). `layouts/pilot-shape.toml` has the pilot's shape with invented names: a zone, five providers, four data
  spaces and the probe space; its link check covers ten ordered pairs.
- **`up`** submits one held job of `1 + providers` nodes and reads back what Slurm recorded (it cancels that one job if
  any field differs); places the zone on the first node and each provider on the next; reads each node's FQDN and
  address in a control step (`bundle/nodefacts.py`); runs `zone.sbatch` and `provider.sbatch` as service steps, each
  with every peer address, and pipes a registration token from the zone into each provider; copies the zone's
  passphrase (0600) and the Onedata test CA into the root (`bundle/zone-export.sh`); then bootstraps the federation
  through the Compose bootstrap over `https://<node FQDN>:8443`, writes its baseline, `facts.json` and
  `platform.toml`, and runs the platform layer's link check, which writes `link-check.json` in the root; last, when
  the core has it, it starts the login node's fault relay (M4) and waits for its heartbeat.
- **`down`** stops the fault relay first, then tears every service down (the providers, then the zone), waits for every step to leave, checks each node
  for the recorded work directory, cancels the held job by its id (never by user or name), ends the srun clients, and
  removes the root. After a partial `up` it finds the job by its recorded id, or by its allocation's exact name.
- **Every step** is `srun --jobid --exact` with a name, its size and `/usr/bin/env -i PATH=... LC_ALL=C TMPDIR=<node
  tmpfs>`; no step uses `--overlap`; a control step's srun client is ended only once its step is gone from `squeue
  -s`, and one still listed at its bound is cancelled by its own id.
- **The site** comes from `site.env` (`HPC_ACCOUNT`, `HPC_PARTITION`, `SH`, `L`, and optionally `HPC_STEP_PATH`,
  `HPC_NODE_TMP`, `HPCOP_PY`, `HPC_CORE_PYTHON`, `HPC_ADMIN_MAIL`, `HPC_BILLING_MEM_WEIGHT`), recorded in the state file
  at `up`, so `down` and the service verbs need no environment. The bundle is this checkout's own, which must lie on a
  filesystem the nodes see. The first `python3.11` on `HPC_STEP_PATH` must be the node's own, not a uv-installed one:
  the bundle's `supervise.py` needs `os.pidfd_open`.

**What has not run, and what the first live slot checks first:** that the login node reaches the services' 8443 (or,
with `[hpc] core = "step"`, that the core's Python from a venv on the shared filesystem runs on a node); the two
batch scripts as `--exact` steps of a multi-node job; the bootstrap's panel calls through 8443 with the LUMA local
feed's mappings; the submit read-back's fields as the site's Slurm prints them; and the ten pairs passing over the
cluster's network. The service verbs (`stop`, `start`, `status`, `exec`) are M4's (`hpcfed_control.py`); an imported
space (M3) and the agent's node's step (M5) are not built here.

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
| `baked.py` | node | finds the Couchbase cookies and memcached password the images ship, by the SHA-256 that `lib.sh` lists (never the values) |
| `egresslog.c` | agent's cell | the agent's egress shim: logs each destination, never a payload |
| `egress-collector.py` | sandbox step | receives the shim's records outside the cell and writes the run's egress log |
| `nodefacts.py` | control step | a node's FQDN, its IPv4 address, and the job's uid and gid, as JSON (`hpcfed.py up`) |
| `zone-export.sh` | control step on the zone's node | the zone's passphrase or the Onedata test CA on stdout, for the federation's root (`hpcfed.py up`) |

## What the repository's tests cover

`tests/deploy/test_hpc_*.py` run the Couchbase guard against the stand-in, the password rules, a 3.8 parse of every
Python file (the guard runs on the images' Python 3.8), the batch jobs' `--no-requeue` and `--time`, the shim's
public ports against `ports.sh`, the hash check for the images' shipped Couchbase values (on a stand-in value), and, where `gcc` exists, the shim's build with every warning an error and its unit
test (`tests/shim_unit.c`), and the egress shim and collector against `tests/egress_probe.c` (where Unix sockets are
allowed). `tests/deploy/test_hpc_fed_plan.py`, `test_hpc_fed_updown.py` and `test_hpc_fed_bundle.py` run the driver
against a fake Slurm (O57): the layout's sizes and the held job's shape, the submit read-back, every step's form and
allowlist, a hung client and a step past its bound, the state file, `up`'s placement, peers and token pipes, `down`
after a whole or a partial `up`, the core's phase writing `platform.toml` and the ten-pair link-check record, and
`zone-export.sh` on a stand-in work directory. The bundle's wider offline suite reads files copied out of the two pinned images
(patch drift, the images' binaries under the shim) and is not in the repository; neither are those files.
