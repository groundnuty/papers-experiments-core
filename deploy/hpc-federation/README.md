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

## The agent's gate on a shared loopback

The data gate has no network of its own on these nodes either: it listens on the loopback every user of the node
shares, on a port above 1023. Two pieces, neither run live yet:

- **The gate's owner check** (`peer_uid_check: true` in the gate's configuration, `src/expcore_onedata/sandbox/gatepeer.py`).
  The gate serves a connection only if the peer's socket belongs to its own uid, by the same `/proc/net/tcp` and
  `tcp6` rows `portshim-owner.h` reads, and closes any other, one it cannot attribute and every one when the tables
  cannot be read, before a byte is read or written, logging one `peer_refused` line with the reason. It is off by
  default (Docker's gate is on its own network); the gate directory then needs `gate.py`, `gatepolicy.py` and
  `gateboundary.py` as before, and `gatepeer.py` beside them when it is on. The gate does not start with it on if it
  runs as uid 0 or cannot read the table.
- **`gateport.c`**, the cell's connect-side mapper: an `LD_PRELOAD` shim that sends a TCP `connect()` to
  `127.0.0.1:443` to the gate's port, because the published REST client dials 443 only, and touches no
  other connection. The port is read once at load from the cell's `/etc/expcore/gate.json`
  (`{"address": "127.0.0.1", "port": P, ...}`), which decides whenever it exists, and otherwise from
  `EXPCORE_GATE_PORT`; a malformed file or variable makes the process redirect nothing and say so once on stderr. It
  calls the next `connect()` by `dlsym(RTLD_NEXT)` and goes **before `egresslog.so`** in the preload list, so the
  egress log records the gate's port; after it, it is never reached. Build: `gcc -shared -fPIC -O2
  -fno-stack-protector -Wall -Wextra -Werror -o gateport.so gateport.c -ldl`, in the cell's image. The bundle hashes
  tarballs (`.provenance`), not shims: whoever loads the built shim into a cell pins it by the hash of the file it
  built. No IPv6 destination is rewritten, `[::1]:443` included: the core's gate listens on 127.0.0.1 only, and a rewrite to `[::1]:<gate port>` could reach another user's listener (see the header of the file).
- **An image without gcc** (a slim Python image has none): build both shims, `egresslog.so` and `gateport.so`, with
  the node's gcc and the same flags. The built library loads in the cell only when the node's glibc is no newer than
  the image's (older, on the nodes the probes ran on), so every symbol version it needs is in the image's. A node whose glibc is newer than the image's makes
  a library the cell's loader refuses: build it in a container of the image's own release then. Either way, pin the
  file you built.

`tests/deploy/test_hpc_gate_port_shim.py` builds the shim with the host's gcc and drives it with a Python client
against listeners of its own, a stand-in for the next `connect()` (`tests/fake_next_connect.c`) and the egress shim;
`tests/onedata/sandbox/test_gatepeer.py` and `test_gate_peer_owner.py` read the kernel's own tables for a real
connection and write another uid's rows from its address pair.

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

## The host mount's oneclient: a relocatable bundle

A mount run on a batch node (O60, `expcore_onedata.sandbox.apptainer.hostmount`) runs oneclient on the node, outside
every cell, so that its FUSE mount is in the node's mount namespace, where the setuid starter binds it into each cell.
A batch node has no oneclient of its own, and one started from a SIF mounts inside the SIF's own mount namespace. So
the host mount runs a **relocatable bundle** of the very build the Compose federation's mount sidecar runs (O65):

```sh
python3 deploy/hpc-federation/oneclient_bundle.py --image onedata/oneclient@sha256:<id> --id sha256:<id> \
    --out <directory> --self-check
```

- **From the sidecar's pinned image only, through one scratch container.** The reference must name the id given
  (`[sandbox.images.oneclient]`'s); a container of it is created under a name of its own (`docker create --pull
  never`), never started, its root copied out (`docker cp <name>:/ -`, from that stopped container), and that
  container alone removed (`docker rm -v`), whatever happens. Nothing is pulled, started, tagged or removed besides.
- **The closure, inside the image.** The binary's every `DT_NEEDED` name, looked up as the image's loader would
  (RUNPATH or RPATH with `$ORIGIN`, the image's `ld.so.conf`, the loader's own directories), every link resolved in
  the image's tree; the NSS modules glibc opens by name for a host lookup (`libnss_files`, `libnss_dns`), which no
  `ldd` lists, with their own closure; and the binary's `PT_INTERP`, the image's loader. A name found nowhere fails
  the build. For `onedata/oneclient:25.0` that is 105 files, every one from the image's `/opt/oneclient`.
- **The tar:** `bin/oneclient`, one regular file `lib/<name>` per library, the loader as
  `lib/ld-linux-x86-64.so.2`, and `PROVENANCE.json` (the image's id, the binary's GNU build id, each file's place in
  the image, size and SHA-256); sorted, time 0, owner 0, fixed modes, so a rebuild of one image gives the same
  SHA-256. Beside it, `<tar>.sha256` and `<tar>.provenance.json` (with the reference and the self-check's result).
- **How it runs:** `lib/ld-linux-x86-64.so.2 --inhibit-cache --inhibit-rpath <every file of the bundle>
  --library-path lib bin/oneclient ...`. The image's libraries carry the DT_RPATH `/opt/oneclient/lib`, which glibc
  searches *before* `--library-path`, and a library opened at run time would fall back to the node's
  `/etc/ld.so.cache`: with both inhibited, a name is looked for in the bundle and then only in the loader's own
  Debian-layout directories (a node that keeps its libraries under `/usr/lib64` has none there). `--self-check` runs that launcher on the build host: `--list` must place every library
  in the bundle, and `--version` must run with no object initialised from outside it (`LD_DEBUG=files`).
- **Declared and pinned:** put the tar on the filesystem the nodes see and declare it in `[sandbox.apptainer]`,
  `oneclient_bundle` and `oneclient_bundle_sha256` (from `site.env`'s `HPC_ONECLIENT_BUNDLE` and
  `HPC_ONECLIENT_BUNDLE_SHA256` for the pilot's template; `up` checks the file against the pin before the cluster is
  touched). A mount level without it does not load. Each run unpacks its own copy into a private directory of the
  run from the bytes it hashes, records `oneclient_build` (oneclient's `--version`, the binary's build id, the pin)
  in the mount helper's metadata (the sandbox's `metadata()["mount"]`), and removes the copy at its stop. The pin is in the campaign fingerprint
  (`sandbox_backend`), so runs on two bundles are never one experiment.
- **What the node must give:** a setuid `fusermount3` (the bundle's libfuse 3 runs `/usr/bin/fusermount3`, then
  `fusermount3` on `PATH`), a `/dev/fuse` the job's uid may open, and a run directory on a filesystem that is not
  `noexec`. The bundle's TLS library looks for a trust store in the image's own paths: give the platform's `[tls]
  ca_file` (the host mount passes it as `SSL_CERT_FILE`), or the provider's certificate cannot be checked.
- **What is shown live (16 Nov 2026, one batch node):** the tar's SHA-256 and every file's against
  `PROVENANCE.json`; `oneclient --version` natively in the node's environment and in an empty one; no file outside
  the bundle tried or initialised by the launcher above (the launcher with `--library-path` alone tried the image's
  RPATH on the node first); and a `--help` dry run that accepts every option the host mount passes. **Not yet run
  live:** a real mount from the bundle, its bind into a cell, a known file's bytes through it and the effective
  `io_mode`; then, before HPC mount runs are scored, the same read script against the Compose sidecar's mount, per
  file served and placed bytes and `io_mode`, proxy first.

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

## A shared imported tree

A space can be held on a directory of the cluster's shared filesystem that other federations import too, so several
federations serve one dataset with no copy of it and each run's data is the same bytes. The tree is **read-only in every
federation**; this section is what the bundle does for that, and `../../docs/storage-import.md`, "A tree on the
cluster's shared filesystem", is why. `layouts/shared-import.toml` declares it (invented names),
`layouts/pilot-shape-import.toml` is the pilot's shape with its four data spaces imported this way (the ingest-based
`layouts/pilot-shape.toml` stays the primary layout until a live slot shows the two equivalent), `DESIGN.md` section 8
says where `hpcfed.py up` calls each step, and `site.env.example` names the two variables of each import.

- **Declared** in the layout: `import = { readonly = true, shared = true, source_env, manifest_env }` on the space, and
  `direct_io = true` if the agent's mount reads the storage directly (a mode of the mount, which reads the storage of the
  agent's own provider, so only on a space that provider owns; the sandbox node must see the same directory, and the
  run's record shows the mode in effect; HPC mounts are proxy-only until the direct-I/O comparison passes, since the
  Apptainer backend's mount helper refuses a direct mount). The path and the manifest's file are the variables'
  values in `site.env`, never the layout's. The owner of the space is the one provider that mounts the tree; its mirrors
  keep node-local storage.
- **Checked on the node** (`bundle/importstorage.py`, called by `configure.sh` and at every start): `--import=SPACE=PATH`
  must be a plain absolute path of `A-Z a-z 0-9 . _ + / -`, with no symbolic link on the way, an existing directory the
  job's user can read, on no tmpfs (a tree on a node's memory is one the other nodes do not see), outside the work
  directory. Nothing is created unless every tree passes. Then the service starts with `--bind PATH:/volumes/import/SPACE:ro`
  over an empty mountpoint of the rootfs, which is the one thing of the node's filesystems the container sees. A refusal
  names the space, never the path.
- **Proven read-only by the kernel**: `python3.11 -I -B importstorage.py mounts --workdir <W>`, which `hpcfed.py up` runs
  in a control step on each owner's node once every provider is up (the node's Python is `HPCOP_PY`; a failure fails
  `up` before the bootstrap starts any scan), reads the mount table of this service's container processes (the children of the starter
  its supervisor, named by `service.pid`, started, so another service of the account on the node is not counted),
  prints per space how many hold the mount, or one below it, read-only and how many writable, and exits 0 only when each
  space is held and none writable. It attempts no write: a write probe is the first thing that
  could change the tree if a declaration were wrong.
- **What the platform layer adds**: the Onedata storage is made `readonly` and imported by the bootstrap; the core
  refuses a shared import that is not read-only (at load, and on the owner's panel), the provisioner refuses every write
  and every eviction from the owner, and `fedimport.verify_imports` shows after up that the space holds exactly its
  manifest.
- **A reset** of the space evicts the mirrors and removes the run's own QoS requirements; it writes no file. To start
  again: down, then up. `teardown.sh` removes the work directory with `rm -rf --one-file-system`, which never enters the
  bind (it exists only in the container's own mount namespace).
- **Not for the first run's helpers**: `bundle/onboard.py` and `run/fed_provider.py` support a space on the provider's
  *first* storage, which is arbitrary once a provider has an imported one beside its own. The driver's bootstrap names
  the storage; do not use those two helpers for a provider that owns an import.

Written, not run. A live slot must show: that the mount check finds the service's container as the (visible) child of the
setuid starter its supervisor started, and that `/proc/<pid>/task/*/children` exists on the nodes' kernel (it fails closed,
proving nothing, when either is otherwise); Onepanel accepting the imported storage made with the job user's ids, the local
LUMA feed and the canonical path type; the bind read-only by the mount table on this site's filesystem; the files'
owner and mode as the scan gives them for another account's files (the tree must be readable by the job's group); the
scan's time and its load on the shared filesystem for a tree of 15,000 files, which the site's file quota (pending) may
bound; the sandbox node seeing the same directory for a direct mount; and two federations importing one tree at once.

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
- **The agent's node** (M5), when `[hpc] sandbox` declares it or the layout's platform template declares the
  Apptainer sandbox's limits, is held at its own shape: by default (`sandbox_placement = "het"`) as component 1 of a
  heterogeneous job, every step there with `--het-group=1` and every step on a service node with `--het-group=0`
  (the site's Slurm refuses a heterogeneous job's step that names no component); or (`"separate"`) as a held job of
  its own, whose steps name none. Either is read
  back as the services' job is, and `down` releases both. The state file's `sandbox` record names its node, its job
  and its het group, which M5's shard step takes. With the pilot's shape and a memory weight of 0.5 this bills about
  42 units per hour, against 63 for one uniform 18 GB shape (`DESIGN.md`, section 1).
- **The agent's sandbox** (O63): the pilot's template declares the Apptainer sandbox, whose limits size the agent's
  node (an 8-core, 17 GiB step), and names the site's values as placeholders `up` fills from `site.env`:
  `HPC_AGENT_SIF`, `HPC_EGRESS_SHIM` and `HPC_ONECLIENT_BUNDLE` (the mount level's oneclient, O65) with their
  `..._SHA256`, and `HPC_AGENT_WORK_ROOT` (`site.env.example`). A pin
  is a declaration: `up` reads each file and refuses, before the cluster is touched, a variable not set, a file
  missing or one whose SHA-256 is not the one declared, naming the variable and never the path. The egress collector
  is the bundle's own, hashed from this checkout. The rendered file's pins are in the campaign fingerprint
  (`sandbox_backend`), no path is. Every path `up` renders into the root is absolute (`@hpc_root@`), so a copy of the
  file (the one a shard step places beside a study) reads the root's secrets, CA and journal.
- **The data each space holds** (C06, O63): every template's `[[spaces]]` entry names its `data_manifest`. A shared import's is the manifest its layout names; every other space's is written by `up` into `<root>/data-manifests/` from its baseline, the same for every federation of a layout. Before the link check `up` reads every space back through its owner (directories, sizes, bytes; a shared import by `verify_imports`) and fails, naming the space, when it is not exactly its manifest. So shards on two federations of one layout record one campaign fingerprint (each space by its declared identity and its data, never its Onezone id), and `shards.py plan` refuses federations whose platform files leave a space's data undeclared.
- **The render identity** (O74, `hpcidentity.py`): once `platform.toml` is rendered, `up` writes
  `<root>/render-identity.json` (mode 0600, canonical JSON, sorted keys), which holds hashes and a version and nothing
  else: `version` (`expcore-render-identity/1`), `template_sha256` (the template as the renderer read it, before it was
  rendered), `platform_fingerprint_sha256` (the canonical JSON of the rendered file's `PlatformConfig.fingerprint()`,
  hashed as the core hashes a fingerprint) and, for each pin the rendered platform holds, the SHA-256 `up` filled in and
  checked: `sandbox_sif_sha256`, `egress_shim_sha256`, `egress_collector_sha256` and `oneclient_bundle_sha256`. A pin
  the template does not name is not in the record, and no entry is ever empty. It holds no address, id, job, node, path,
  user or secret. Two federations rendered from one template, with the same pins and the same data (every space
  declaring its `data_manifest`, as above), have byte-equal records; a changed pin changes its own entry and the
  fingerprint, which holds the Apptainer backend's pins, and an edited template changes the template's. A layout with no
  platform template writes none. `shards.py step` compares it with the record a plan expects (below).
- **`up`** submits one held job of `1 + providers` nodes and reads back what Slurm recorded (it cancels that one job if
  any field differs); places the zone on the first node and each provider on the next; reads each node's FQDN and
  address in a control step (`bundle/nodefacts.py`); runs `zone.sbatch` and `provider.sbatch` as service steps, each
  with every peer address, and pipes a registration token from the zone into each provider; copies the zone's
  passphrase (0600) and the Onedata test CA's certificates, never its key, into the root (`bundle/zone-export.sh`,
  `hpcca.py`); then bootstraps the federation
  through the Compose bootstrap over `https://<node FQDN>:8443`, writes its baseline, `facts.json` and
  `platform.toml` and its render identity (`render-identity.json`), and runs the platform layer's link check, which
  writes `link-check.json` in the root; last, when
  the core has it, it starts the login node's fault relay (M4) and waits for its heartbeat.
- **`down`** stops the fault relay first, then tears every service down (the providers, then the zone), waits for every step to leave, checks each node
  for the recorded work directory, sweeps the agent's node (`shards.py sweep`, a control step there: every shard step's
  directory of this federation on its tmpfs whose step is gone is removed, and one holding an `out/` that could not be
  archived is kept and named in what `down` prints), cancels the held job (and the agent's own job, under `"separate"`) by its id (never by user or name), ends the srun clients (run it on the login node that ran
  `up`: it finds them there), and
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
feed's mappings; the submit read-back's fields as the site's Slurm prints them, for each component of a heterogeneous
job too, and a step with `--het-group=1` from the login node (`DESIGN.md`, section 9); and the ten pairs passing over
the cluster's network. The service verbs (`stop`, `start`, `status`, `exec`) are M4's (`hpcfed_control.py`, over this
driver's launcher and state file; DESIGN.md section 7), which also repeat the mount check after a `start` (the first
`status` that finds a restarted owner otherwise ready runs it); `up` starts the core's fault relay
(`expcore_onedata.fault_relay`) and `down` stops it. Tested offline, none of it run live: a kept stop and a start again
over the same work directory, `exec.sh` beside a running service, and every stop's real duration (from which `[hpc]
stop_seconds` is to be set) are the live slot's. The agent's node's step (M5) is not built here. A layout with a shared
import is wired into `up` (`DESIGN.md` section 8) but has not run live; it needs `[hpc] core = "login"`.

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
| `shards.py` | login node, sandbox step | a campaign's shards, one per federation: the step lines, the step, its snapshots, the archives the merge takes |
| `nodefacts.py` | control step | a node's FQDN, its IPv4 address, and the job's uid and gid, as JSON (`hpcfed.py up`) |
| `leftovers.py` | control step | how many work directories of ours of one kind the node's tmpfs holds, as JSON (`hpcfed.py down`, for a service whose step ended before naming one) |
| `zone-export.sh` | control step on the zone's node | the zone's passphrase or the Onedata test CA on stdout, for the federation's root (`hpcfed.py up`) |
| `gateport.c` | agent's cell | the connect-side mapper: a TCP connect to 127.0.0.1:443 goes to the gate's port; nothing else is touched |
| `oneclient_bundle.py` | build host | the host mount's oneclient: a relocatable bundle of the sidecar's pinned image, its SHA-256 and provenance |

## A campaign's shards: one per federation (`shards.py`)

A campaign whose agent runs in the core's Apptainer sandbox (`[sandbox] backend = "apptainer"`, O56) runs as #154
shards, one per federation. The sandbox's data gate listens on its node's loopback and its cells are that node's
`apptainer exec`s, so each shard's dispatcher runs as one job step on its federation's sandbox node, launched from the
login node like every other step. `shards.py lines --manifest shards.toml` prints each shard's `srun --jobid --exact`
line, whose environment is an allowlist (`PATH`, `LC_ALL` and the federation's root, where the study's plugin finds
`platform.toml` and the platform layer finds the fault relay), to run detached. In the step, `shards.py step` copies
the stamped study to the node's tmpfs (the store's lock refuses a network filesystem), checks the interpreter imports
the core it was given, runs `python -m expcore run STUDY --shard i/N --shard-label L`, and once the dispatcher has
exited writes the copy's `out/` whole to `<results>/<label>.out.tar` with a manifest. `shards.py collect --manifest
shards.toml --into DIR` verifies every archive, unpacks each `out/` read-only and prints the `python -m expcore merge`
line (`--run` runs it). The manifest names the site's paths and stays out of the repository (the script's docstring
has its form). Outage runs (`[faults]`) go through the login node's fault relay (M4), which `up`
starts.

**What the step leaves on the node (O63).** When the federation's `platform.toml` declares the Apptainer sandbox, the
step makes its `work_root` (0700) before the dispatcher and refuses one that is a link, another user's, open to others
or not under `--tmpfs`. It copies the federation's `platform.toml` beside the study's `study.toml` in its copy, where
the study's plugin loads it, so no federation's path is ever in `study.toml`; a study that holds a `platform.toml` of
its own is refused. The node's tmpfs is the node's, not the job's: each step's directory there records its process,
its campaign and its federation, and whether its dispatcher has ended (`step.json`). Only a step killed while its
dispatcher ran is removed, once it and every process naming its directory are shown gone: by `--resume` (its own
shard's, of its own campaign; it refuses while one still runs) and by `down`'s sweep of the agent's node. One whose
dispatcher ended and whose `out/` was not archived (exit 75, or killed before the archive) is kept and named:
`--resume` refuses while it is there, and `shards.py step ... --archive-left` archives it with the same manifest and that
step's exit status, then removes it, unless an archive was published after that step started.

**The study's own refusals: a pre-step guard.** A study that refuses a campaign it must not run (one worker, each
planned model's cap exactly 1, a federation root of its own) can have every shard step assert that again, from its
stamped `campaign.toml`, before the step makes anything. The manifest declares one script of the study:

    pre_step = "guards/pre_step.py"   # a path relative to the study's root
    pre_step_timeout_s = 60           # optional, with pre_step: above 0, at most 600 (60 by default)

`shards.py plan` and `lines` read the file and record its SHA-256 (`plan` prints `{path, sha256, timeout_s}` as
`pre_step` beside each line), and each line then ends in `--pre-step REL --pre-step-sha256 HEX --pre-step-timeout S`.
They refuse a path that is absolute, has an empty, `.` or `..` part, or goes through a link, a file that is a link, is
not a regular file, is missing or is over 1 MiB, printing nothing; a plan that has not pinned the guard makes no line.
`collect` never reads it. With no `pre_step` the lines and the plan are what they were, word for word.

`step` runs the guard first, before the core check, before a `--resume` removes a killed step's directory, and before
it makes the sandbox's work root, its own directory on the tmpfs, the journals or an archive. It opens the study's root
(`--study`, the stamped copy: the step's tmpfs copy does not exist yet) once and holds it, and refuses a root that is
not a directory of this user's or that group or others can write to. It reads the guard part by part below that
descriptor with no link followed, refuses a file whose SHA-256 is not the one on the line, and runs those bytes, never
the file read a second time: `<python> -I -B - <label> <campaign.toml>`, the script on the interpreter's stdin. The
guard's `sys.argv[1]` is the shard's label and `sys.argv[2]` names the stamped `campaign.toml` through the held root
(a path under `/proc/self/fd`, good in the guard only), and its working directory is that same root, so a root renamed
after the check is never the one it runs in. It runs with the same interpreter as the step, in an environment of the
step's allowlist (`PATH`, `LC_ALL`, `EXPCORE_FED_ROOT_PARENT`, `EXPCORE_FED_ROOT`) and its no-home variables and nothing
else, and in a session of its own, killed with everything it started when it does not end within its bound or when the
step is told to stop. Its stdout and stderr are the step's. A non-zero exit, a signal, a timeout, a hash that is not the
plan's or a guard that cannot be read refuses the step with exit status 73 (`PRE_STEP_REFUSED`), naming the guard and
why, and leaves nothing made. A step that passes records `pre_step: {path, sha256, exit_status}` in its `step.json`
and in its archive's manifest (`<label>.out.json`).

The guard rides on the generated line, so running a line directly runs it; nothing in `lines` skips it. What it does
not cover: a line edited by hand to drop the three `--pre-step` words runs without it, and `step --archive-left`, which
starts no work and makes no directory, ignores them, so a refusing guard cannot strand the only whole `out/` of a step
(the archive carries the record of the guard that step ran). The guard is the study's own code, hashed as it was when
the plan was made (run `lines` again after editing it); it is not a sandbox, and it asserts only what its own file reads.

**The study's own check of its results: a post-run script.** The same shape for the check that needs the live
federation, such as the study's owner check of what the shard left in it:

    post_run = "guards/post_run.py"   # a path relative to the study's root, with the guard's path rules
    post_run_timeout_s = 300          # optional, with post_run: above 0, at most 1800 (300 by default)

`plan` and `lines` record its SHA-256 too (`plan` prints `post_run: {path, sha256, timeout_s}`), and each line ends
with `--post-run REL --post-run-sha256 HEX --post-run-timeout S` after the guard's words. `step` reads and hashes it
with the guard, before anything is made: a script that is a link, missing or not the plan's hash refuses the step with
73, so a campaign is never spent on a check that cannot run. It holds the bytes and runs them once the dispatcher has
exited 0, while the federation is up, and before `archive()`: `<python> -I -B - <label> <out/> <exit status>`, so
`sys.argv[1:]` is the shard's label, the path of the study copy's `out/` on the node's tmpfs and the dispatcher's exit
status (`0`). Its working directory is the study copy's root on the tmpfs and its environment is the dispatcher's own
(the allowlist, the step's home and TMPDIR), in a session of its own, killed at its bound or when the step is told to
stop. It may read `out/` and the federation, and it may write into `out/`, which is archived afterwards.

An exit 0 makes the shard's results **final**. A non-zero exit, a signal, a timeout, or a dispatcher that did not exit 0
(the script is then not run) leaves them **not final**. They are archived all the same, so nothing is lost: the
manifest says `final: false` and the script's record with its reason, the archived `out/` holds `NOT-FINAL.json` (written into the archive itself, so a full tmpfs cannot leave it out), and
the step ends with exit status 74 (`POST_RUN_FAILED`) when the dispatcher's own status was 0, the dispatcher's otherwise.
A step's record (`step.json`) and its manifest hold `post_run: {path, sha256, ran, exit_status, final}` (and a `reason`
when not final); with none declared the record, the manifest and the archive are what they were. `collect` refuses a
shard that is not final, naming it and the reason, before it unpacks anything, and, when the plan declares a post-run
script, a shard whose manifest does not say `final: true` with that script's hash (a line that dropped the words). The
core's `python -m expcore merge` refuses an `out/` holding `NOT-FINAL.json`, naming the shard. `--resume` starts from a
not-final archive as from any other, without the mark, and decides afresh; `step --archive-left` of an `out/` kept on the
node archives it final only when the step's record says its script passed, and not final (with the mark) otherwise, so a
step killed before its script passed is never archived as final.

**Which render a shard runs against: the render identity (O74).** A shard is one experimental condition across
federations only when every federation was rendered alike, and nothing but the merge, after the spend, said otherwise.
`up` records what it rendered (see `up` above), so a plan can say which render it expects. Copy a federation's record
into the study as the expected one, commit it with the study, and declare it in the manifest:

    cp <root>/render-identity.json <study>/render-identity.expected.json
    expect_render_identity = "render-identity.expected.json"   # a path relative to the study's root, the guard's rules

`plan` and `lines` read the file (the guard's path rules; a file that is not a well-formed record refuses them, printing
nothing), record its SHA-256 (`plan` prints `expect_render_identity: {path, sha256}` beside each line), and each line
then ends in `--expect-render-identity REL --expect-render-identity-sha256 HEX`, after the post-run words. With none
declared the lines and the plan are what they were, word for word. `collect` never reads it.

`step` compares first, before the pre-step guard (a script of the study) and before the core check, a resume's
removals, the work root, its own directory, the journals or an archive. It reads the expected file below the study's
root with no link followed, refuses one that is not the bytes the plan hashed, takes the federation's own record from
`$EXPCORE_FED_ROOT/render-identity.json` (read once below that root, no link followed, a root that is this user's and
that no group or other can write to) and requires both to be well formed and equal, key by key. Any difference refuses
the step with exit status 73 (`PRE_STEP_REFUSED`), naming the keys that differ (`sandbox_sif_sha256` and
`platform_fingerprint_sha256` for a changed SIF: the fingerprint holds the pins) and never a value, a hash or a path;
so does a federation with no record in its root, an unreadable or malformed one, or no `EXPCORE_FED_ROOT`. Nothing is
made. A record is only the file `up` wrote: a `platform.toml` edited by hand afterwards is not seen, and a line edited
to drop the two words runs unchecked.

Whether or not one is expected, a step that has the federation's record copies it into
`out/study-state/render-identity.json` (the record as it validated, in canonical form: the bytes `up` wrote, never
anything else the file held; a key given twice refuses the record) once the dispatcher has ended, before the post-run script and the archive, so the
archive, an `out/` kept for `--archive-left`, and a post-run script all see which render the shard ran against. A
failure to write it is said on stderr and never stops the archive. A federation whose `up` wrote no record (one brought
up before O74, or a layout with no platform template) and a plan that expects none run as they did and archive none;
`collect` and the core's `merge` take an `out/` with the directory as it is (the merge reads `out/runs` alone). A resume
writes the record of the federation it ran against, in place of an earlier step's.

**A step killed outright (O62).** Walltime, a node's end or an OOM in the step's cgroup ends the step with no chance to
archive, and the tmpfs copy goes with the node. So while the dispatcher runs, the step snapshots its `out/` into
`<results>/<label>/snapshots/` every `snapshot_every_s` seconds (a manifest key, 900 by default, at least 60) and after
each run's end (the thread looks for a new attempt marker every 30 s), one at a time, and once more after the
dispatcher exits, before the archive:

- the store first, by the core's online backup of the live store, then the rest of `out/` (`runs/`, the lease notes,
  which keep a key whose holder died from anyone until its resource is shown gone, the logs and the lock file), so the
  snapshot's store never names a record its `runs/` lacks. A run that ends between the two is `running` in the
  snapshot with its marker beside it, which the resume's recovery commits. A run directory no row of the store copy
  names (an attempt begun after the backup) is left out: after a restore that run's next attempt would get the same
  directory and the same attempt id;
- written into a fresh `.snap-<n>.part`, checked (the store's integrity, every ended attempt's marker present, every
  file read back against its hash), its manifest written last, then renamed to `snap-<n>`; the two newest are kept,
  so a kill at any point leaves the last good one and a damaged newest falls back to the one before;
- the dispatcher journals its spend to `<results>/<label>/journals/<stamp>-<pid>-<hex>.jsonl`, a new file per step
  (`expcore run --spend-journal`): one line per attempt begun, top-up, attempt ended, probe charge and model taken off
  the reply maximum, written after the store commits it and before the spend; a journal that cannot be written stops
  the dispatcher.

`shards.py step --resume` restores the newest snapshot that verifies, unless the archive is newer (each snapshot names
the archive its step started from), else the archive, as before; either way the store's attempt directories are
pointed at the new copy, and a restored lease directory is marked rolled back, so every key waits for its resource to
be shown gone before a new holder takes it (a key released in the snapshot may have been taken since). A shard with
snapshots none of which verifies, and no archive, is refused rather than started afresh. The dispatcher's start then
counts what the journals hold and the restored store lacks (attempts begun, top-ups and probe sends after the
snapshot, and a model taken off the reply maximum since) as `lost_in_kill`, once, against the shard's share of the
budget: an attempt whose end was journaled at its measured cost, one in flight at its reserved bound. The export and
the report list it. A start without `--resume` of a shard with a snapshot is refused; a snapshot or a journal of
another shard (by its whole spec, so the same label in another partition too) or campaign is refused, naming it. A
snapshot holds the whole of `out/` but the live store's own files, which its backup stands for, and copies it whole
each time: size `snapshot_every_s` to what the shared filesystem can take.

## What the repository's tests cover

`tests/deploy/test_hpc_*.py` run the Couchbase guard against the stand-in, the password rules, a 3.8 parse of every
Python file (the guard runs on the images' Python 3.8), the batch jobs' `--no-requeue` and `--time`, the shim's
public ports against `ports.sh`, the hash check for the images' shipped Couchbase values (on a stand-in value), and, where `gcc` exists, the shim's build with every warning an error and its unit
test (`tests/shim_unit.c`), and the egress shim and collector against `tests/egress_probe.c` (where Unix sockets are
allowed). `tests/deploy/test_hpc_fed_plan.py`, `test_hpc_fed_updown.py` and `test_hpc_fed_bundle.py` run the driver
against a fake Slurm (O57): the layout's sizes and the held job's shape, the submit read-back, every step's form and
allowlist, a hung client and a step past its bound, the state file, `up`'s placement, peers and token pipes, `down`
after a whole or a partial `up`, the core's phase writing `platform.toml` and the ten-pair link-check record, and
`zone-export.sh` on a stand-in work directory. `tests/deploy/test_hpc_oneclient_bundle.py` builds the oneclient bundle
from invented images against a fake Docker (O65): the closure inside the image, the scratch container's discipline, a
reproducible tar and the self-check's refusals. The bundle's wider offline suite reads files copied out of the two pinned images
(patch drift, the images' binaries under the shim) and is not in the repository; neither are those files.
