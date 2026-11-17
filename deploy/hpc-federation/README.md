# Rootless Onedata on HPC batch nodes

`bundle/` runs one Onezone and Oneproviders on HPC batch nodes, rootless, each service in a setuid Apptainer
writable sandbox started with `--containall`, one service per node and per batch job. It is the platform layer of
issue #179's one-node proof. It is built for batch nodes where users get no network or user namespaces and no ports
below 1024, and where the node's `/tmp` may be shared with other users. The images are the pinned builds of `deploy/compose-federation`
(`docker.onedata.org/oneprovider:ID-765309c84f`, `docker.onedata.org/onezone:ID-ba7a778696`).

Status: what has run live, and what has not, is in the dated facts files under `facts/` (the live-run notes
themselves are kept privately, since they name the site). The agent's egress detection (below) has not run live.
`hpcfed.py` ("The driver", below), which brings a whole federation up and down from a layout file, has not run live.
The dated facts files so far: [`facts/2026-11-14-standing-1.md`](facts/2026-11-14-standing-1.md) (the first standing
allocation) and [`facts/2026-11-16-slot-7-proxy-equivalence.md`](facts/2026-11-16-slot-7-proxy-equivalence.md) (the host
mount against the Compose sidecar in forced proxy, with its record beside it).

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

## Two federations of one account

A federation's services run with no network namespace and the guard watches Couchbase's fixed port 8091, so two
federations of one account on one node would make the second one's Couchbase fail on its bind and its `up` fail
(issue 398). Slurm packs jobs onto a node by cores and memory and keeps nothing apart, so `up` keeps them apart itself,
with two read-only reads of the account's live jobs (`squeue --me`: ids, names, states and node lists, nothing else).
A job whose name starts with `hpcop-` is a federation's: a held job (`hpcop-<project>-<random>`), an agent's own job
(`...-sandbox`) and the bundle's own batch scripts (`hpcop-zone`, `hpcop-provider`). A job of any other name is none of
its business, whatever it runs. It never reads or prints a node's name into a message: only roles and counts.

- **Before it submits,** `up` collects the nodes of every such job (a pending job holds none yet, a completing one still
  does) and submits every component of its held jobs, and the agent's own job under `separate`, with
  `--exclude=<those nodes>`; the agent's sandbox never runs on a node that hosts an Onedata service, another
  federation's included. How many it excluded is in the state (`allocation.excluded_nodes`) and in what `up` says. **Fail
  closed:** with `squeue` unreadable, or a line of such a job that cannot be taken apart, `up` refuses before anything is
  made and submits nothing.
- **Once the job runs and before any step,** `up` reads them again. A node of this federation, a service's or the
  agent's, that holds another live federation's job (two ups read before either had submitted, and Slurm placed both on
  it) refuses `up`: its held jobs are cancelled by their ids, no step runs, and the message names the roles (`zone`,
  `providers[i]`, `agent`), never a host. A second read that fails refuses the same way. Two ups that raced may both
  refuse: `down`, then `up` again.
- **Too few nodes.** The exclusion can leave Slurm no node that fits the job: it stays pending, or sbatch refuses it. `up`
  waits no longer than its queue bound (`TIMEOUTS["queue"]`, 30 minutes), cancels the job by its id and says that it
  excluded N nodes of other live federations; a refusal by sbatch says the same, with Slurm's own words stripped of those
  nodes' names.

In a fleet each member's `up` is its own, so each reads the members already up: with `HPC_FLEET_PARALLEL` at 1 (the
default) every member takes nodes of its own. With more at once, two members that read before either has submitted can
land on one node, and the later one is refused (reported as a failed member, nothing left held): bring it down and add
another (`down --member fed-k`, `add 1`).

`tests/deploy/test_hpc_distinct_nodes.py` runs both reads against the fake Slurm (a live federation's nodes excluded
from each component and job, an unreadable `squeue` refusing, a race refused before any step with the roles named and
the jobs released, the queue bound's reason) and a fleet of three members on one shared pool. Not run live: what the
site's `sbatch` does when the exclusion leaves no node that fits (refuses at once, or keeps the job pending to the bound).

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
| `HPC_GATEPORT`, `HPC_GATEPORT_SHA256` | optional: the 443 mapper (`bundle/gateport.c`, built for the agent's SIF) and its SHA-256, read only for a template that names it (`layouts/pilot-shape-rest.toml`); both or neither |
| `HPC_STUDY_ARTIFACTS` | optional: the directory of the study's own artifacts (its tool server's interpreter and venv, the pinned clone, the frozen manifests and its data manifests), an absolute path with no `..`, on a filesystem the login node **and** the agent's node see (`<shared_root>/studies/<study>/artifacts`). A platform template, or a space's `data_manifest` in the layout, names paths under it as `@study_artifacts@`, so a public study carries no site path; `up` refuses a template that uses the placeholder without the key, naming it. `up` also digests its tree into the render identity (O74, below); the path is never in the record. The directory must be self-contained: a link whose target leaves it refuses `up` |

Node names, addresses and job ids are typed at the prompt or read from Slurm; they never go into a file of this
repository. The optional keys of the driver, the agent's sandbox and the study's artifacts are in `site.env.example`; the four of the next
section are the provider storage on scratch and its inode guard.

## Provider storage on scratch, and the inode guard

With `HPC_STORAGE_ROOT` set, each provider stores its data (its posix storage, `/volumes/posix` in its cell) in a
directory of its own on the shared scratch filesystem instead of its node's tmpfs, and the inode guard is mandatory. The
shared scratch filesystem has room for a federation's data and a limited inode quota (a pilot-shape federation's data is
about 17,500 files, and every federation of an account counts against one quota). `DESIGN.md`, section 11, is the full
contract; in short, with `HPC_STORAGE_ROOT` set in `site.env`:

| Key | What |
|---|---|
| `HPC_STORAGE_ROOT` | an existing directory on the scratch filesystem that only you can write (`<scratch_root>/hpcop-storage`, `mkdir -m 700`: owned by you, no group or other write bit, which `up` and `down` both check), neither inside the federation's root nor a shared tree, and named by its real path (no symbolic link on the way, which `up` refuses: the nodes bind through none). With none, nothing below runs |
| `HPC_QUOTA_CMD` | the site's command that prints the account's inode usage and quota (`<quota tool> --inodes <scratch_root>`), split as a shell would split it and **run without a shell** on the login node |
| `HPC_QUOTA_PATTERN` | a regular expression with the named groups `used` and `quota` that reads its output and matches **exactly once** (`inodes used (?P<used>[0-9,]+) of (?P<quota>[0-9,]+)` reads an invented line); you write it for the real output, which this repository never holds |
| `HPC_QUOTA_CHECK` | **refused with `HPC_STORAGE_ROOT`**: the check is mandatory for a federation bound onto scratch, so the site is not loaded with both. Without a scratch root there is no check, and `off` (its only value) changes nothing |

- **`up` checks before it submits anything:** it reads the usage and the quota through the command, estimates the
  federation's inodes from its data manifests (every space's manifest once, files and directories; a shared import's
  manifest is the one its layout names, every other space's is the baseline `up` writes), and **refuses when the usage
  plus the estimate passes 95% of the quota**, naming the used count, the estimate, the quota and the 95% line. A total
  exactly at the line passes. **It fails closed:** a command or pattern that is not set, a pattern that does not compile or
  lacks a group, a command that cannot run, fails or does not answer in 120 s, an output the pattern does not match exactly
  once, a figure that is no count (`unlimited` is none) or that the pattern cut out of a longer one (`1000.5`, `2G`: the
  pattern must capture the whole figure) and a quota of 0 all refuse `up`, naming why; nothing turns the check off.
  For the ingest layout the estimate is a handful until its ingested space declares the manifest of the study's data
  (`data_manifest`): that manifest is counted too (the pilot's largest space is 15,340 files), through the site's
  `HPC_STUDY_ARTIFACTS`, and a fleet counts it once for each of its federations; for `layouts/pilot-shape-import.toml`
  it is the four imported manifests.
- **`up` then makes the federation's storage:** below `HPC_STORAGE_ROOT`, one directory of its own named for its allocation
  (0700) and one per provider, each by an exclusive `mkdir` that never adopts what is already there and each holding an
  owner marker, recorded in the state file before the first is made, and in each provider's, beside its marker, the
  empty directory it stores in (`posix`, 0700).
- **Each provider stores there:** its job is given that directory (`--storage=PATH`, from the state's record checked
  whole), `configure.sh` has `bundle/posixstorage.py` check it (the form `up` makes for this provider, no link on the
  way, 0700 and yours (a setgid bit the scratch passes down is allowed), the marker naming the provider and the
  allocation, empty and not on the node's memory) and
  record it in the work directory, and the service binds it writable onto `/volumes/posix` at every start, `hpcfed.py
  start` included, which gives the start the same path again from the state: the bind happens only when the record
  names exactly it. A refusal stops the start: a provider never falls back to tmpfs or another directory. The provider runs as you in its
  cell, so 0700 is all it needs, and it never sees the marker, which stays beside the bound directory. Its storage is at
  the same path in the cell, so it is declared to Onedata as before (`finish.sh`), and the data check reads each space
  through its owner, wherever that stores it. Without `HPC_STORAGE_ROOT` the provider's step and the service's argv are
  word for word what they were.
- **The storage kind is part of the experiment.** Where a provider stores its data shapes a run's I/O timing, so the
  rendered `platform.toml` declares `provider_storage = "shared_filesystem"` (`"tmpfs"` without a scratch root) in
  `[sandbox.apptainer]`, never the root's path. The platform fingerprint and each run's facts carry it, so shards on
  tmpfs and on scratch never merge, and a campaign cannot resume from one onto the other. A platform template that
  declares the agent's sandbox must name it (`provider_storage = "@hpc_provider_storage@"`, as `layouts/pilot-shape`'s
  does): `up` refuses one that does not, before anything is submitted. The Compose federation has no such setting.
- **`down` removes exactly those directories,** after the job is gone and every service verified clean, and counts what
  remains: any entry fails it loudly, with the root and the state kept for a second `down`. It reads the paths from the
  state file alone, refuses a record that is not the exact form `up` writes (a state with the scratch root and no record
  is refused too), and removes nothing it cannot prove `up` made: a directory already there when `up` ran, one without
  this `up`'s marker (a provider's directory replaced by another of yours included), a link in a directory's place, an
  entry it did not make. Each directory is held open and proved before any is removed, and emptied through its
  descriptor, so a path swapped afterwards is never followed. A link inside a directory is removed, never followed.
  The bind lives only in the provider's container, so the teardown's removal of its work directory never reaches the
  storage on scratch: only `down` removes it, after the provider has stopped.

## The account's home, and its guard

The cluster account's home is small (a quota of files and of bytes) and shared by every agent of the account, so what a
checkout, a venv or a slot directory leaves in it counts against everyone's work: a full home breaks every agent of the
account at once. **A site should set the two keys below**, so that `up` reads the home before it submits anything.
`DESIGN.md`, section 11, is the full contract; the keys are in `site.env`, and they do not depend on a scratch root:

| Key | What |
|---|---|
| `HPC_HOME_QUOTA_CMD` | the site's command that prints the home's usage and limits (`<quota tool> --home <home>`; a site's own script may print them, and the home need not be on the filesystem the scratch quota tool reads), split as a shell would split it and **run without a shell** on the login node |
| `HPC_HOME_QUOTA_PATTERN` | a regular expression with the named groups `files_used`, `files_limit`, `bytes_used` and `bytes_limit` that reads its output and matches **exactly once**, each figure captured whole. Capture the limit that **binds**: some tools print a soft column that reads 0 beside the hard limit, and a soft figure of 0 is refused as no limit. `files (?P<files_used>[0-9,]+) \(soft [0-9,]+; hard (?P<files_limit>[0-9,]+)\); bytes (?P<bytes_used>[0-9,]+) \(soft [0-9,]+; hard (?P<bytes_limit>[0-9,]+)\)` reads an invented line; you write it for the real output, which this repository never holds |
| `HPC_HOME_QUOTA_CHECK` | `off` (its only value) skips the check and `up` says so; unset is not off |

- **`up` checks before it submits anything:** it runs the command (bounded at 120 s), reads the four figures and
  **refuses when the used files or the used bytes pass 95% of their limit** (`used > limit * 95 // 100`, in integers: a
  figure exactly at the line passes), naming each figure that is past, its limit and the line. The bytes are compared with
  their own limit, so any unit works as long as the used figure and the limit are in the same one; a figure the pattern cut
  out of a longer one (`2G`, `1000.5`) is refused, like the inode guard's.
- **It fails closed.** With either key set, a command or pattern that is not set (a half-set check), a pattern that does
  not compile or lacks one of the four groups, a command that cannot run, exits non-zero or does not answer, an output
  the pattern does not match exactly once, a figure that is no whole count and a limit of 0 all refuse `up`, naming why.
  With **neither** key set there is no check, and `up` says nothing of the home: the keys are optional, which is why a
  site should set them.
- **It names figures only.** A home quota tool's stderr and command line name the user and the home's path, so a
  refusal repeats neither: a failed command is named by its exit status alone. The three keys are not recorded in the
  state file; `down` needs none of them.
- **The fleet reads the home once** for all its federations, at `up` and at `add`, before its directory, a slot or a job
  exists (each federation's own `up` then reads it again, as it reads its inode estimate again).

**Who owns what.** The federation operator owns the `site.env` every `up` reads and fills every pin in it. A study
supplies its agent image; the operator supplies what is built for that image or for the nodes. Each file is declared
with its SHA-256, which `up` checks (below, "The agent's sandbox").

| Keys | Supplied by | What |
|---|---|---|
| `HPC_AGENT_SIF`, `HPC_AGENT_SIF_SHA256` | the study | the agent's SIF and its SHA-256, in an owner-only (0700) directory under the images path `site.env.example` defines (`<shared_root>/images`), not in `SH`, which holds the rootfs tarballs alone |
| `HPC_EGRESS_SHIM`, `HPC_EGRESS_SHIM_SHA256` | the operator | the egress shim (`bundle/egresslog.c`) built for that image, and its SHA-256 |
| `HPC_ONECLIENT_BUNDLE`, `HPC_ONECLIENT_BUNDLE_SHA256` | the operator | the tar `oneclient_bundle.py` builds, and its SHA-256 |
| `HPC_AGENT_WORK_ROOT` | the operator | the agent's work root on its node's tmpfs |

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
  --mem=32G --chdir=/ --output="$L/hpcop-zone-%j.log" "$B/zone.sbatch" "$B" "$SH/onezone.tar" <zone-name> \
  <provider-address>)
# 2. A provider on node B, registered to the zone with a single-use token that travels only through the pipe.
PJ=$(sbatch --parsable --account="$HPC_ACCOUNT" --partition="$HPC_PARTITION" --nodelist=<node-B> --time=03:00:00 \
  --mem=32G --chdir=/ --output="$L/hpcop-provider-%j.log" "$B/provider.sbatch" "$B" "$SH/oneprovider.tar" \
  <provider-name> <zone-fqdn> <admin-email> <zone-address>)
srun --jobid="$ZJ" --overlap --export=PATH --chdir=/ -n1 -c1 --mem=200M bash "$B/mint-registration-token.sh" \
  <provider-name> </dev/null | srun --jobid="$PJ" --overlap --export=PATH --chdir=/ -n1 -c1 --mem=1G \
  bash "$B/deliver-token.sh"
# 3. Teardown: providers first, then the zone.
srun --jobid="$PJ" --overlap --export=PATH --chdir=/ -n1 -c1 --mem=100M bash "$B/teardown.sh" op </dev/null
srun --jobid="$ZJ" --overlap --export=PATH --chdir=/ -n1 -c1 --mem=100M bash "$B/teardown.sh" oz </dev/null
scancel "$PJ" "$ZJ"
```

The token delivery step needs about 1 GB: the seventh live run's step was killed for memory under `--mem=100M`.
Each batch job runs its service from the batch step itself (`node-init.sh`, `configure.sh`, `run-*.sh`,
`finish*.sh`), so polling costs no job steps; read a work directory's `run.log` with
`srun --jobid=<job> --overlap --export=PATH --chdir=/ -n1 cat <workdir>/run.log </dev/null`. Give every `srun` and
`sbatch` a `--chdir=/`: a step or job otherwise starts in the directory it was called from, or, where that does not
exist on the node, in the node's shared `/tmp`. On a failed start the batch
script stops the service, deletes and redacts every secret (`scrub.py`) and keeps the logs for
`HPCOP_INSPECT_SECONDS`. A job whose service stopped at `teardown.sh`'s request ends with status 0; a service that
ends any other way fails the job, with its own status. `--exclusive` keeps other users off a node, at the price of
the whole node for the job.

## Registered names (O73)

By default a service registers under its node's own FQDN, and its certificate is made for it. A federation reached by
the names its layout declares registers each service under its declared name instead, so that no node's name, address
or port 8443 is in anything the agent can read (the scope answer and the no-connection errors carry the registered
domain). `hpcfed.py up` does this for every layout; by hand, give each batch script the name and every service's
address:

```bash
"$B/zone.sbatch" "$B" "$SH/onezone.tar" <zone-name> <provider-address> \
  --declared=zone.<domain> --host=zone.<domain>=<zone-address> --host=<provider>.<domain>=<provider-address>
"$B/provider.sbatch" "$B" "$SH/oneprovider.tar" <provider-name> zone.<domain> <admin-email> <zone-address> \
  --declared=<provider>.<domain> --host=zone.<domain>=<zone-address> --host=<provider>.<domain>=<provider-address>
```

`configure.sh` then registers the service under `--declared` (`onezone.domainName`, `oneprovider.domain`, the
generated certificate's domain), writes the work directory's `hosts` (the loopback, the node's own names on its
address, every `--host`) and `declared-name`, and `run_service` binds the hosts file read-only over `/etc/hosts` with
`--no-mount bind-paths`: the site's own bind of the node's `/etc/hosts` is off, so `/etc/localtime` is the image's. A
provider's lookup of a peer crashes when the name does not resolve, so the script refuses, before it writes anything, a
peer address with no `--host`, a `--host` that is no peer, a name or an address listed twice, and a missing entry for
its own name or the zone's. The readiness calls, the token mint and the registration read-back dial the declared name
pinned to the loopback (`curl --resolve`), the one name the certificate holds. Onepanel's DNS check resolves through DNS
alone and reports the declared names `unresolvable`; it is advisory, and `facts.json` records that. From the login
node the names are reached through the address map `up` writes into the root (`secrets/address-map.json`, 0600),
which `platform.toml` names through its template's own line, `addresses = @hpc_addresses@` (a slot like `@hpc_root@`:
the render is the template with its slots filled, and adds no line; the slot is the whole inline table `{ file, resolver }`
and the line stands before the template's first table; `up` refuses a template without it before it submits anything,
and the render refuses the slot where the root holds no map); see `DESIGN.md`, "The registered names". Every login-side client of the services is built by `hpcboot.service_client`, which refuses a root with no usable map rather than build a client that could reach no name, and `tests/deploy/test_rest_constructors.py` fails on any other construction that is not listed with its reason. The first sandbox slot on a federation brought up
this way is owed the checks of `DESIGN.md` section 9, item 7.

## The cell's host name (O73)

An agent's cell on a batch node has no network namespace, and without `--uts` no UTS namespace either: `hostname`,
`uname -n`, `uname -a` and `/proc/sys/kernel/hostname` in it show the batch node's own name. That is a name the agent
must not read (the gate's count of node names, `rest_node_name_hits` in `docs/interfaces.md`, is for the same reason),
and a Compose cell, which reads its own container's name, does not show it. The core gives every cell on both backends
one fixed, neutral host name, `sandbox` (`expcore_onedata/sandbox/cellhost.py`, a constant of the core and no setting):
each `apptainer exec` carries `--uts --hostname sandbox`, and the Docker agent's container is created with
`--hostname sandbox`. A federation with a node of that name, or, for a run that scans for node names, an agent node whose own host name is that, is
refused at the start. The gate's scan for node names
(`rest_node_name_hits`) holds the agent node's own host name instead, in its fully qualified and one-label forms, read
from the node at the start (`NodeOps.own_names`), after the federation's nodes' names, so a leak of the name the cell
no longer shows is still counted; never `sandbox`, which is the allowed name and would count false hits. The fully
qualified names are a resolver lookup (`gethostbyaddr` of the host name: the primary name and every alias, each
scanned whole and by its first label), so it is bounded: 5 s in a thread of its own, after which the host name is
scanned alone, and a start waits no longer for a resolver that hangs. The run's facts (`own_node_scan`) say how many
forms were scanned and, by reason alone (`fqdn_timeout`, `fqdn_failed`, `host_name_failed`, and `fqdn_unqualified` when
no name the scan keeps has a domain, which a loopback alias or a declared name does not give, so a leak of the node's
name followed by its domain would not be counted), what was not read, never a name. A live slot should show `own_node_scan.unread` empty.

**`/etc/hostname`:** the sandbox binds none of its own. From Apptainer's source (read on its `main` branch, not on this
site's release), `--uts` with a `--hostname` makes Apptainer write an `/etc/hostname` of the name and bind it, and it
never binds the node's own: `/etc/hosts` and `/etc/localtime` are the only default bind paths, and `--containall` binds
no others of the site's. A bind of ours onto it would, by the same reading (not run), be a second mount on the same
path: Apptainer mounts its own first and a user bind after it, so ours would win, and for an image with no
`/etc/hostname` the source makes a user bind fatal where Apptainer's own is skipped (unless an overlay or underlay makes
the mount point, which was not read). So the sandbox adds none, and a hook (b) bind onto it is refused (it would replace
the name).

**Measured (17 Nov 2026, the pilot's site):** its Apptainer runs in setuid mode, and `apptainer exec --uts --hostname
sandbox <image> hostname` and the same with `uname -n` print `sandbox`, exit 0, with no warning; without `--uts` both
print the node's own name.

**Not shown offline, owed to the next live slot:**

1. `--uts --hostname sandbox` together with the sandbox's whole argv (`--containall`, `--no-mount
   hostfs,bind-paths,cwd`, the binds, the preloads and the egress shim). The measurement above was a bare exec of an
   image.
2. `cat /etc/hostname` in a cell of a live run, with no bind of ours, which is what ships: it must read `sandbox`
   (record `apptainer --version`, since the source above is `main`'s). If it reads anything else, a generated file bound
   at `/etc/hostname` is the fallback, and its start must then be shown not to fail on an image with no such file.
3. `hostname`, `uname -n`, `uname -a` and `cat /etc/hostname` in a cell of a live run, compared with a Compose cell's.

The tests (`tests/onedata/sandbox/apptainer/test_cell_hostname.py`) read the argv and binds a fake node was given; they
do not run Apptainer.

## Slots and the tool server (O83)

The tool-server arm needs a slot's users. `layouts/pilot-shape-slots.toml` is the pilot's shape with the four users of two
slots as ordinary principals, and its template, `pilot-shape-slots.platform.toml.template`, is the pilot's with the
slots and a `[toolserver]` after it (the Compose federation's own form, `deploy/compose-federation/README.md`, "Slots
and a tool server"). `up` provisions the users with the Compose bootstrap's own steps: the zone user, its password in a
0600 file of `secrets/users/` (in no cell, `gate.json`, fact, record, log or message), its membership of each declared
space with exactly the declared privileges, and the storage mapping to the job's uid that every principal gets. **A slot
user that already exists is verified and never changed**: exactly as declared, it is recorded `matched` in `facts.json`
with nothing sent; any difference is refused naming the user and the attribute. The template's `[slots]` takes each id as
`@user_id.<username>@` from the facts, and the written file is checked against the layout (a slot user that is no
principal, `rights` that are not exactly the privileges the layout gave the user, a data-path user with more than the
baseline are refused) and loaded as a campaign loads it, so `up` fails there, not at the first run.

The tool server runs from the study's artifacts directory, `HPC_STUDY_ARTIFACTS` in `site.env`, which the pilot fills
with its interpreter and venv, the pinned onedata-mcp clone and one frozen manifest per condition
(`python -m expcore.tools.manifest freeze`, run with the condition's allow-list). Before any job is held `up` refuses a
site without the variable when the template names it, a command that is no executable file, a clone that is not at the
pinned commit with no change to a tracked file, and a manifest that is not its condition's (edited, or showing other
tools than the allow-list); each message names the condition by its position and no path. The server is started through
the resolve launcher (`<interpreter> -I -B resolve_launch.py <map> -- -m <module>`, "Registered names"), so `command` is a
Python interpreter and `args` start with `-m <module>`: a console script behind `/usr/bin/env` is refused by `up`'s
preflight, before any job is held, and when the file is loaded, saying to use `<venv>/bin/python` with `args = ['-m',
'<module>', ...]`. Owed to the next HPC slot: a B2 run whose tool calls succeed, whose rights refusals match Compose's for the same
layout, and whose visible-names counts are zero.

## The driver: `hpcfed.py`

`hpcfed.py` brings a zone and the layout's providers up on batch nodes, and takes them down, from the Compose
federation's own layout file; `DESIGN.md` is its process model (one held job, every service and control action an
`srun --exact` step from the login node, the state file, the service verbs). **Written and tested offline against a
fake Slurm; not run live.**

```bash
set -a; . deploy/hpc-federation/site.env; set +a
uv run python -I -B deploy/hpc-federation/hpcfed.py --layout deploy/hpc-federation/layouts/pilot-shape.toml \
  --root <a directory on a filesystem the login node and the nodes see> describe   # the plan and what it bills
uv run python -I -B deploy/hpc-federation/hpcfed.py --layout <layout> --root <root> up
uv run python -I -B deploy/hpc-federation/hpcfed.py --layout <layout> --root <root> verify-data   # after the study's ingest
uv run python -I -B deploy/hpc-federation/hpcfed.py --layout <layout> --root <root> down
```

Run them as shown, `python -I -B`, from the checkout or another directory of ours, never a shared one such as the
login node's `/tmp`: `uv run` takes the project of the working directory when it has a `pyproject.toml`, and builds it.
Every Python the driver itself launches runs with `-I -B` (the fault relay among them, in the federation's root), so
the working directory never reaches its `sys.path` and no bytecode is written beside what it imports (`DESIGN.md`,
section 3).

- **The layout** is a `deploy/compose-federation` layout (its providers, spaces, principals, probe space and platform
  template) with an `[hpc]` table of sizes, which Compose ignores: the held job's `--time`, each service step's cores
  and memory, every node's control reserve, `stop_seconds`, `step_launch_wait_s` (what a control step may wait to
  start, 240 s by default), and where the core's phase runs (`core = "login"` or `"step"`). `layouts/pilot-shape.toml` has the pilot's shape with invented names: a zone, five providers, four data
  spaces and the probe space; its link check covers ten ordered pairs.
- **The agent's node** (M5), when `[hpc] sandbox` declares it or the layout's platform template declares the
  Apptainer sandbox's limits, is held at its own shape: by default (`sandbox_placement = "het"`) as component 1 of a
  heterogeneous job, every step there with `--het-group=1` and every step on a service node with `--het-group=0`
  (the site's Slurm refuses a heterogeneous job's step that names no component); or (`"separate"`) as a held job of
  its own, whose steps name none. Either is read
  back as the services' job is, and `down` releases both. The state file's `sandbox` record names its node, its job
  and its het group, which M5's shard step takes. With the pilot's shape and a memory weight of 0.5 this bills about
  42 units per hour, against 63 for one uniform 18 GB shape (`DESIGN.md`, section 1).
- **A site whose switch plugin gives each job a virtual network identifier (VNI)** (HPE Slingshot's does) refused a
  heterogeneous job's steps on component 0 from a point on, in the second live slot (16 Nov 2026): after the zone and
  five providers had started, every further step on a service node failed with `srun: error: task 0 launch failed:
  Error configuring interconnect`, so `up` failed at the first provider's registration. A step on component 1 ran,
  `--network=no_vni` ran on component 0 and `--network=single_node_vni` failed like no option, and the same steps ran
  in a held job of one component in two earlier live runs. Whether a count of steps per component or exhausted
  identifiers is the cause is not established. **Two ways round it, neither the default (another site is unchanged by
  either):** `HPC_STEP_NETWORK=no_vni` in `site.env`, which puts `--network=no_vni` on every step the driver launches
  (the services talk over TCP and need no VNI), and `sandbox_placement = "separate"` in the layout, which makes the
  services' job a job of one component. **On such a site, start from `separate`:** it changes nothing about the steps,
  where `no_vni` does, though it needs a second held job the partition may refuse. Not yet seen live on such a site: a
  whole `up` with either, and a step on component 1 with `no_vni`.
- **The agent's sandbox** (O63): the pilot's template declares the Apptainer sandbox, whose limits size the agent's
  node (an 8-core, 17 GiB step), and names the site's values as placeholders `up` fills from `site.env`:
  `HPC_AGENT_SIF`, `HPC_EGRESS_SHIM` and `HPC_ONECLIENT_BUNDLE` (the mount level's oneclient, O65) with their
  `..._SHA256`, and `HPC_AGENT_WORK_ROOT` (`site.env.example`). **A REST arm** (#384) is
  the layout `layouts/pilot-shape-rest.toml`: the pilot's, but for its own template, which adds the three things a REST
  level needs and nothing else (a test reads both and refuses any other difference): `[sandbox] content_check_file`
  (the study space's `hello.txt`, which the layout's own baseline writes at `up`), a REST level beside the mount level
  in `[sandbox.data_paths]`, and the cell's 443 mapper (`bundle/gateport.c`, built for the agent's SIF) as the one
  `[sandbox.apptainer] preload`, pinned like the other files: it is `@hpc_gateport@` with `@hpc_gateport_sha@`, filled
  from `HPC_GATEPORT` and `HPC_GATEPORT_SHA256`, two optional keys that only a template naming them reads. The pilot's
  template names neither, so a layout with no REST level needs no mapper, and a site that declares one has it read for
  the REST layout alone. `fleet.py` refuses the REST layout as it does the pilot's (its agent's node is a
  component of the services' job). A pin
  is a declaration: `up` reads each file and refuses, before the cluster is touched, a variable not set, a file
  missing or one whose SHA-256 is not the one declared, naming the variable and never the path. The egress collector
  is the bundle's own, hashed from this checkout. The rendered file's pins are in the campaign fingerprint
  (`sandbox_backend`), no path is. Every path `up` renders into the root is absolute (`@hpc_root@`), so a copy of the
  file (the one a shard step places beside a study) reads the root's secrets, CA and journal.
- **The slots only the driver fills** (the template's `@...@` names; `DESIGN.md` section 6 has each in full): the
  Compose renderer's own (`@zone_url@`, `@provider_url.<name>@`, `@provider_id.<name>@`, `@space_id.<space>@`,
  `@user_id.<user>@`), and the HPC driver's: `@hpc_root@` (the root, so every path into it is absolute),
  `@hpc_stop_bound_s@`, `@hpc_settle_timeout_s@`, `@hpc_command@`, `@hpc_direct_storages@` (an inline table, `{}` with
  no direct-I/O import), `@hpc_token_margin_s@` (the least `[tokens] margin_s` the loader accepts), and
  `@hpc_addresses@` (O73), the value of the template's top-level key `addresses = @hpc_addresses@`: the inline table
  `{ file = "<root>/secrets/address-map.json", resolver = "<root>/toolshim/resolve_launch.py" }` (no `resolver` until
  `up` copied the launcher into the root). Every HPC template carries that one line before its first table: a render is
  the template with its slots filled and has no line added, `up` refuses a template without the line before it submits
  anything, naming the slot, and the render refuses the slot on a root with no map, or a map on a template with no
  slot. A study's own HPC template needs the line.
- **The data each space holds** (C06, O63, O57): every template's `[[spaces]]` entry names its `data_manifest`. A shared import's is the manifest its layout names; a baseline space's is written by `up` into `<root>/data-manifests/` from its baseline, the same for every federation of a layout; an ingested space's is the one it declares (below). Before the link check `up` reads every baseline space back through its owner (directories, sizes, bytes; a shared import by `verify_imports`) and fails, naming the space, when it is not exactly its manifest. So shards on two federations of one layout record one campaign fingerprint (each space by its declared identity and its data, never its Onezone id), and `shards.py plan` refuses federations whose platform files leave a space's data undeclared.
- **An ingested space** is one whose data is the study's own, put there after `up`: `baseline = false`, no `import`, and its `@data_manifest.<space>@` named by the template (a probe space, which nothing names, is not one). Its baseline is empty, so a manifest made from that would fingerprint two federations that ingested different bytes alike; it **declares its manifest** instead, `data_manifest = "@study_artifacts@/manifests/<space>.json"` (or an absolute path) on the space in the layout, and `up` renders that path for the space. The file is in the core's import-manifest format (`expcore_onedata.importmanifest`: `{"version": 1, "files": [{"path", "size", "sha256"}, ...], "directories": [...]}`, files and directories sorted by path, every parent directory listed; `manifest_of_tree` makes one from a local copy of the data); `PlatformConfig.fingerprint()` takes `Manifest.digest()`, the SHA-256 of its canonical JSON, as for any `data_manifest`. A space that declares none is held to the empty manifest, so ingested bytes fail the check below; a template that writes a `data_manifest` path of its own is refused at `up`, since only the manifests `up` renders are checked. `up` checks it neither way (the space holds nothing yet) and prints which spaces await the check. **The order is `up`, the study's ingest, `verify-data`, `shards.py plan`:** `hpcfed.py verify-data` reads each ingested space back through its owner, compares every file's path, size and SHA-256 and every directory with the manifest, and exits 1, naming the space and how many files differ, on any difference; on a pass it records, in the state file (`data.ingested.<space>.verified`), a marker bound to the manifest's digest. `shards.py plan` and `lines` refuse a federation whose ingested spaces have no marker, whose marker is for another manifest than `up` recorded or the platform file names now, or whose state predates the record. A later ingest needs another `verify-data`: nothing watches the space after it. `verify-data` runs on the login node (it reaches the services' 8443 from there, as `core = "login"` does).
- **The render identity** (O74, `hpcidentity.py`): once `platform.toml` is rendered, `up` writes
  `<root>/render-identity.json` (mode 0600, canonical JSON, sorted keys), which holds hashes and a version and nothing
  else: `version` (`expcore-render-identity/1`), `template_sha256` (the template as the renderer read it, before it was
  rendered), `platform_fingerprint_sha256` (the canonical JSON of the rendered file's `PlatformConfig.fingerprint()`,
  hashed as the core hashes a fingerprint) and, for each pin the rendered platform holds, the SHA-256 `up` filled in and
  checked: `sandbox_sif_sha256`, `egress_shim_sha256`, `egress_collector_sha256`, `oneclient_bundle_sha256` and, for
  the REST layout, `gateport_sha256`; and, when `site.env` declares `HPC_STUDY_ARTIFACTS`, `study_artifacts_tree_sha256` (the next bullet). A pin the template
  does not name, and the digest of a site that declares no directory, are not in the record, and no entry is ever
  empty. It holds no address, id, job, node, path, user or secret. Two federations rendered from one template, with the same pins and the same data (every space
  declaring its `data_manifest`, as above), have byte-equal records; a changed pin changes its own entry and the
  fingerprint, which holds the Apptainer backend's pins, and an edited template changes the template's. A layout with no
  platform template writes none. `shards.py step` compares it with the record a plan expects (below).
- **The study's artifacts tree** (O74, `hpcidentity.tree_sha256`): with `HPC_STUDY_ARTIFACTS` set, `up` reads the
  directory's tree once, in its preflight on the login node before anything is made or submitted, and the record holds
  its SHA-256 as `study_artifacts_tree_sha256`. The digest is of one canonical listing: `expcore-artifacts-tree/1` and a
  NUL, then one record per entry, sorted by the entry's relative path as bytes (the whole path, so `a-b` comes before
  `a/b`) and none for the directory itself: `F NUL path NUL size NUL sha256 NUL` for a regular file, `D NUL path NUL`
  for a directory, `L NUL path NUL target NUL` for a symbolic link (paths are raw bytes, `/`-separated). The venv is in
  it. Mode, owner, time and the directory's own path are not, so a copy of the tree at another path has the same digest
  and no path is in the record. **A link is recorded by its target string and never followed, and the directory is
  self-contained:** what a link points at is not hashed, so it must be in the tree, where its own entry pins it. The
  study's interpreter is installed in the directory and its venv built on it (the venv's `bin/python3` links into the
  tree, `lib64` to `lib`), and `up` refuses a link whose resolved target leaves the directory, naming
  `HPC_STUDY_ARTIFACTS` and the link's path relative to the directory, never its target. A link is resolved as the
  kernel resolves it, against the tree's own entries alone (nothing outside is opened, so a target that does not exist,
  or a FIFO, is refused all the same): a relative target is read from the link's own directory; an absolute one must
  begin, component by component (`/p/artifacts2` is not inside `/p/artifacts`), with the directory's real path or the
  path `HPC_STUDY_ARTIFACTS` gives; `..` goes up one directory and one at the root is an escape; a link met on the way
  (a link to a parent, a chain) is followed, never collapsed as text; and a path may pass through at most 40 links (the
  link itself the first: Linux's own limit), so a loop is refused. A link that dangles inside the tree is allowed and
  recorded like any other (a file that appears where it points changes the tree, so it changes the digest). The
  canonical form is the one above, and a tree whose links stay inside has the digest it had before this check. A
  **special file** (a FIFO, a socket, a device) is refused, and so is a directory that does not exist,
  is a file or is itself a link. Every refusal fails `up` before the root is made, naming `HPC_STUDY_ARTIFACTS` and a
  path relative to the directory (escaped), never an absolute one. The tree is opened below one held descriptor with
  no link followed and is never written, so a read-only tree reads as any other, and a file that changes while it is
  read is refused. A file is hashed as it is: a venv whose scripts name the venv's own absolute path (a shebang, a
  `pyvenv.cfg`) digests differently at another path, so build the tree where it will stay. The digest goes through the
  state file (`study_artifacts_tree_sha256`, beside the site's `study_artifacts`) to the core's phase, so `[hpc] core =
  "step"` has it though a node's environment is an allowlist, and the core never reads the tree again. A layout with no
  platform template writes no record and reads no tree; each federation of a fleet reads the tree itself.
- **`[placement]` is always in the rendered file, complete** (O79): the platform layer requires it, and a reader that
  finds none patches in a default of its own. The core has no default for `holder`, so every template declares it
  (`placement = { holder = "..." }`, a provider the template declares) and `up` refuses one that does not, before
  anything is submitted, naming the key (`hpcplacement.py`). The render keeps what the template declares and writes
  every other setting at the core's default beside it, read from `PlacementSettings` (never a copy of the numbers),
  where the template declares the table: as one `placement = { ... }` line of the root or one `[placement]` table
  (any other spelling is refused). So every reader, a shard's step and an ingest tool alike, reads the same file, and
  none patches a default table in on its own.
- **`up`** submits one held job of `1 + providers` nodes and reads back what Slurm recorded (it cancels that one job if
  any field differs); places the zone on the first node and each provider on the next; reads each node's FQDN and
  address in a control step (`bundle/nodefacts.py`); writes the address map; runs `zone.sbatch` and `provider.sbatch`
  as service steps, each with every peer address and every service's declared name, and pipes a registration token from the zone into each provider; copies the zone's
  passphrase (0600) and the Onedata test CA's certificates, never its key, into the root (`bundle/zone-export.sh`,
  `hpcca.py`); then bootstraps the federation
  through the Compose bootstrap over `https://<declared name>:8443` (the layout's names, dialled through the address
  map `up` writes into the root), writes its baseline, `facts.json` and
  `platform.toml` and its render identity (`render-identity.json`), and runs the platform layer's link check, which
  writes `link-check.json` in the root; last, when
  the core has it, it starts the login node's fault relay (M4) and waits for its heartbeat. The providers are launched
  1 s apart, and a service whose step ended before its log said anything is launched **once more**, after a 60 s backoff counted from the
  step's own End (`launch_retry_backoff_s`: providers that failed together wait one backoff in all) and only when sacct shows
  that exact step as `CANCELLED` with exit `0:116`, srun's client log shows its task launch failing for want of an
  address, and its run log is empty (zero bytes, read again after the wait): the live failure of 17 Nov, where srun could not find the
  nodes' addresses. The retry and its proofs are recorded in the state file and printed; a second failure fails `up` as before
  (`DESIGN.md`, section 5, "A launch that failed at launch").
- **`down`** stops the fault relay first, then tears every service down (the providers, then the zone), waits for every step to leave, removes the
  recorded work directory of a service whose step it had to cancel (a cancel was an outright kill on the site, which
  left 2 to 2.6 GB per service), checks each node
  for the recorded work directory, sweeps the agent's node and every node a shard was resumed on (`shards.py sweep`, a
  control step there by `HPC_CORE_PYTHON`: every shard step's
  directory of this federation on its tmpfs whose step is gone is removed, and one holding an `out/` that could not be
  archived is kept and named in what `down` prints; the sandbox's killed runs are reaped, a dead FUSE mount unmounted
  first, and the work root removed once empty), cancels the held job (and the agent's own job, under `"separate"`) by its id (never by user or name), ends the srun clients (run it on the login node that ran
  `up`: it finds them there), then removes the provider storage on scratch that `up` recorded, when there is one, and
  shows by a count that none of it remains (the previous section), and removes the root. After a partial `up` it finds the
  job by its recorded id, or by its allocation's exact name.
- **Every step** is `srun --jobid --exact` with a name, its size, `--chdir=/` and `/usr/bin/env -i PATH=... LC_ALL=C
  TMPDIR=<node tmpfs>` (and `--network=<value>` when `site.env` sets `HPC_STEP_NETWORK`, on every step `up`, `down` and
  the service verbs launch; the line `shards.py lines` prints for a shard step is the manifest's own and does not carry
  it), and every Python in it runs with `-I -B`; no step uses `--overlap`; a control step's srun client is ended only
  once its step is gone from `squeue -s`, and one still listed at its bound is cancelled by its own id.
- **The site** comes from `site.env` (`HPC_ACCOUNT`, `HPC_PARTITION`, `SH`, `L`, and optionally `HPC_STEP_PATH`,
  `HPC_NODE_TMP`, `HPCOP_PY`, `HPC_CORE_PYTHON`, `HPC_ADMIN_MAIL`, `HPC_BILLING_MEM_WEIGHT`, `HPC_STEP_NETWORK`, `HPC_STUDY_ARTIFACTS`, and the four keys of the
  provider storage on scratch), recorded in the state file at `up` (of the storage keys, only `HPC_STORAGE_ROOT`), so `down` and the service verbs need no environment. `HPC_CORE_PYTHON` (a venv of this checkout on the
  shared filesystem) is required with an agent's node: `down`'s sweep reaps the sandbox's runs with the core's reaper.
  **Install that venv's interpreter under the site directory** (`UV_PYTHON_INSTALL_DIR=<site dir>/python uv sync`),
  never in the account's home: every Python the driver and `shards.py` launch takes `-I -B` and writes no bytecode,
  but the home is every job's and agent's of the account (live slot 5 found a node step's `.pyc` there). The bundle is this checkout's own, which must lie on a
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
(`expcore_onedata.fault_relay`) and `down` stops it. **The relay ends a verb that outlives its bound with its whole
process group, and watches for its late steps (O58):** each verb runs in a group of its own, so no `srun` client of a
timed-out verb, nor of one that exited early with its `srun` still waiting, survives to make its step after the relay has
answered (the controller can stall for about 200 s first);
the relay names the verb's control steps with a tag of its own (`hpcfed.py --step-tag`), and `hpcfed.py late-steps`, a
detached watch of 400 s (also opened for a verb that ran 25 s or more, since hpcfed ends its own `srun` only after its
own bound), cancels a step of those exact names that appears in the federation's job, by its exact
`<job>.<step>` id and no other, and writes what it saw to `<root>/fault-relay/late-steps.jsonl`. A `watch_opened` there
with no `watch_closed` is a window nobody watched to its end, and `none_seen` is written only when the looks and the
accounting were read (DESIGN.md section 7). The answer is still `timed_out`. **The answer also says where the verb's time went:** from one accounting call per verb,
the time from its issue to each step's accounted Start and from its answer to each End, so a controller stall is the
site's in the record (`timing`, journalled with the stop, the start's restore and the heal's link restart; DESIGN.md
section 7). Tested offline, none of it run live: a kept stop and a start again
over the same work directory, `exec.sh` beside a running service, and every stop's real duration (from which `[hpc]
stop_seconds` is to be set) are the live slot's. **Live notes (three provider outages on one federation, 17 Nov):**
a service ended 26.1 s, 27.1 s and 33.1 s after its SIGTERM, so every committed layout takes `stop_seconds` 50 (the
largest plus 50 %); relay verb steps started 47 to 103 s after they were issued during a heal and one round trip took
104.6 s against the relay's 120 s command bound. A step's creation can wait on the scheduler's controller up to its
message timeout (47 to 103 s measured, up to 200 s possible on the site), so every layout also declares
`step_launch_wait_s` 240 (the 200 s plus 20 %), what a control step may wait to start. It is added to every bound
that waits on a step's client from its start (the stop, status, exec and mount-check steps, `down`'s teardown step, a
start's wait to see its service step listed, the stop verb's `settle_timeout_s`, a link restart's bound), and the
relay's floor for every verb (`[faults] command_timeout_s` on this backend, declared in the platform file) is the launch
wait and 60 s, 300 s, not 120 s, and each verb's own bound is its longest run and 30 s (a status 600 s, a start 630 s, a
stop 450 s, a ping and a link restart 570 s): one bound of 300 s was below a start after an unrecorded stop (600 s) and
a restarted shared-import owner's status (570 s). **The launch wait widens timeouts only**, how long the harness waits before it fails
closed: it never lengthens an outage or a hold. The agent's node's step (M5) is not built here. A layout with a shared
import is wired into `up` (`DESIGN.md` section 8) but has not run live; it needs `[hpc] core = "login"`.

## An observed read of the services (`hpcobserve.py`)

`python -I -B hpcobserve.py --root <root>` prints one JSON line (`expcore-hpc-observe/1`) for the zone and each provider the state file lists: whether the service's step runs, its start, its end and its exit, as Slurm shows them now, by the step's EXACT id (`hpcslurm.Slurm.step_observed`: `squeue -s` and `sacct`; an id neither listed nor recorded, or the two disagreeing, is unknown with the reason, never running). The state file names the step to start from (`services.<n>.step`, `launch`, `stops`); a service whose step name is listed under another id than the state's is read as the listed one (`state_differs`). **Slurm's accounting trails a stop.** The driver confirms a stop once `squeue -s` no longer lists the step; `sacct` follows seconds later, and a read in between finds the step neither listed nor recorded as ended. When the state says the step was stopped (`state` `stopped`, the last recorded stop confirmed, the step the one of its `launch`) and the two commands do not yet agree that it ended (`hpcslurm.accounting_behind`), the read waits, polling every 2 s, for every such service at once, up to `--accounting-wait-s` (default 60 s, at most 300, 0 for none), and each service records the seconds it waited (`accounting_wait_s_used`). At the bound the service reads `unknown` with the reason `accounting_lag`: never running, never stopped by assumption. It never waits for a service the state does not say was stopped, for a step `squeue` lists under another id than the state's, or for a command that failed. `expcore_onedata.fedwatch.watch_for(..., accounting_wait_s=)` passes the bound as this option, so the copy run on the login node must be of this version: an older one exits 2 on the option. It runs where Slurm's client commands answer, the login node, and does nothing but `squeue` and `sacct`: it changes no job, no step and no file, takes no lock, and prints no node, address or path. `expcore_onedata.fedwatch` runs it for a study's post-check and adds each service's own `/configuration` answer and the cross-check against the fault journal and the state's recorded stops (O86; `docs/interfaces.md`, "The platform layer's read of the federation's own services"). The `status` verb is not used: it launches a control step and may record state.

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
| `configure.sh`, `run-node.sh`, `run-zone.sh` | batch step | configuration, peers, the name the service registers under and its hosts file (`--declared`, `--host`, below), the service under `supervise.py` |
| `importstorage.py`, `posixstorage.py` | batch step | a shared imported tree's read-only bind (O59); a provider's own storage directory on scratch, checked and bound onto `/volumes/posix` (O57) |
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
| `nodefacts.py` | control step | a node's FQDN, its IPv4 address, the job's uid and gid, and its OS release, glibc and kernel as `readings` (O87; recorded, never refused), as JSON (`hpcfed.py up`) |
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
shards.toml --into DIR` verifies every archive, unpacks each `out/` read-only into DIR (a new or empty 0700 directory of
ours) and prints the `<python> -I -B -m expcore merge` line (`--run` runs it in DIR). Run `shards.py` itself as `<python> -I -B`, from a private directory. The manifest names the site's
paths and stays out of the repository (the script's docstring has its form). Outage runs (`[faults]`) go through the
login node's fault relay (M4), which `up`
starts.

**Copy the shards' results back and verify them: required, after `collect` and before `down`.** Nothing durable may live only
on the cluster's scratch: a site's scratch policy can remove files 30 days after creation, a job directory 7 days after its job and all of scratch after 20 days with no job submitted (take your site's own limits; these are the ones this
step was written against). The cluster cannot push to your host, so you pull. Run `collect` once every step has ended
(a journal still being written is listed as `collect` read it) **and while the federation is still up**: `down` removes
the federation's root, and `collect` archives from it. It writes `archives.sha256.json` in `<results>` last, and prints
that file's SHA-256 on stderr, after the merge line; keep that line. `collect` is atomic: it unpacks and checks every shard
in a private directory beside `--into`, and only then moves the trees in, so a refused collect (an archive that holds
something besides `out/`, a root that is not whole) leaves `--into` exactly as it found it and writes no manifest. From your
own host, pull the whole results directory and check it:

```bash
rsync -a <login node>:<results>/ <local dir>/    # or scp -r: the whole directory, with its subdirectories
python3 -I deploy/hpc-federation/shards.py verify-copy <local dir>/archives.sha256.json <local dir> \
    --manifest-sha256 <the hash collect printed>
```

**What the required pull covers.** For each shard, exactly what the manifest lists, which `verify-copy` checks:

| in `<results>` | what it is | why it must be in the pull |
|---|---|---|
| `<label>.out.tar` | the shard's `out/`, which the merge reads | the results |
| `<label>.out.json` | the archive's manifest (its SHA-256, the exit status, the post-run record) | what the archive is |
| `<label>/journals/*` (every file) | a spend journal per step (`expcore run --spend-journal`) | what a step killed outright spent, which `--resume` counts as `lost_in_kill`: nowhere else |
| `<label>.root.tar` | the federation's **root evidence**: the state (`hpc-federation.json`), `platform.toml`, `facts.json`, `link-check.json`, `render-identity.json`, `day0-wiring.toml`, `shard-claim.json` and `shard-claim.lock` (the study's pre-step guard: who held the root), `data-manifests/*.json` (what each space's data identity was made from), `shard-steps/*.json` (which steps ran), the **whole `fault-journal/` directory** (its journals, the holder and drain records, the lock files, read as they are and never locked) and the fault relay's `late-steps.jsonl` and request, taken and answer files (those present). **An allow-list: never `secrets/`, a file holding a token, key or password, the CA, or the relay's heartbeat and lock** | what `down` removes with the root, and the study's post-check and the facts read: nowhere else once the root is gone |
| `<label>.root.json` | the root archive's manifest (its SHA-256 and size, and each file's name, size and SHA-256) | what the root archive is |
| `<label>/pass-env.json`, **when there is one** | the shard's first-step `pass_env` names and values (a site path of the study's artifacts) | what the first step was given, which the archive's `out/` does not hold |
| `<label>/snapshots/snap-N/*`, **only when newer than the archive** | the newest snapshot that verifies, when a step resumed from the archive was killed before it archived | the runs that step reached, which the archive lacks and the merge does not see; `collect` warns when it lists one, and `collect --run` refuses |

`collect --roots-only [--shard L]` archives and marks the root evidence alone, for a shard about to move while the other
shards are not final (see "Moving a shard" under the fleet); it writes no `archives.sha256.json`, so a full collect lists
the archives later (a root archive whose federation the shard has left is put aside under `<label>.<12 hex digits>.root.*`
and listed too). The archives are mode 0600. `collect` reads `<label>.out.tar` and `<label>.out.json` as regular files and
never through a link, as it reads every journal, `pass-env.json` and snapshot file: a link in their place refuses the
collect, naming the shard, since a pull that keeps links could not reproduce it. It makes the manifest durable (a sync of
the results directory) before it marks any root: an I/O error from that sync refuses the collect (exit 2, `--into` as it
was found, no marker, so `down` still refuses), while a filesystem that says it cannot sync a directory (`EINVAL`,
`ENOTSUP`) is left as it always was.

**The root archive and `pass-env.json` are private evidence.** The state file and the facts name nodes and site paths, the
state file also the account and the administrator's address (no secret, but the site's own), and `pass-env.json` a site
path. They are for the operator: keep the pulled copies like the journals, and never publish them. `expcore package` can
never take them: `package.spec.never_selected` leaves out any `*.root.tar`, `*.root.json` and `pass-env.json`, whatever
directory they were pulled into and whatever the patterns say (tests: `tests/package/test_spec.py`, `test_build.py`); the
alternative, an anonymisation that covers node names, addresses and site paths, was not chosen, since no list of terms
can name a site's details before they are known.

**`down` refuses a root that holds evidence a run left, whose evidence was not collected.** The gate keys on the
evidence **present** in the root, not on a shard step having run. `shards.py step` leaves `shard-steps/<label>.json` in
its federation's root when a step starts (the shard's spec and a count; a step that cannot write it does not start), and
`collect` writes `evidence-collected.json` there, naming the root archive's SHA-256, the digest of those records and the
SHA-256 of each archived file. `hpcfed.py down` reads the root first, before it stops or cancels anything. What counts as
evidence is what a run, a step or the operator leaves: **any step record, a file of `fault-journal/` with a line in it
(at any depth), a file of the relay's journals (`late-steps.jsonl`, a request, a taken request, an answer) that is not
empty, `shard-claim.json` (or its lock) that is not empty, and the pilot's `day0-wiring.toml`**; so a root that only ran
Day-0 measurements, with no shard step, is refused too. An empty file (the injector's lock files, say) is no evidence, and
a link or a special file where the journal is counts, since it proves no absence, and so does a file with a line in it at any depth (a tree nested past the archive's bound, or with an odd name, counts for what it holds). **What `up` alone
writes does not count:** the state, `platform.toml`, `facts.json`, `render-identity.json`, the data manifests and the link
check's record and lock (`up` takes no admission and writes no line of the fault journal; the relay it starts makes only
empty directories, a lock and a heartbeat). A root after `up` alone, or a failed `up`'s, holds none, and `down` removes it
as it always did, so an emergency teardown is never blocked. A root with evidence and **no current marker** is refused,
naming its shards (or, with no step record, the kinds of evidence it holds: the fault journal, the relay's journals, a
shard claim, the day-0 wiring; never a file), and stays whole so that a `collect` can still read it; **`shards.py collect
--roots-only --shard L` marks a root with no step record** (it works from the shard plan, which must name the root; a
plan has at least two shards, so a single federation's Day-0 root needs a manifest that names another shard too), and a
root no plan names, or one the collect refuses (a link, a special file, a file holding a secret), is removed with
`--discard-evidence`. A Day-0 run leaves evidence in the root only through the injector's journal and the relay's files,
which need a platform with `[faults]`: its own report, and the checks that only read, are written under its `--out`
directory, never in the root, so `down` does not see them.
The runbook order stays **collect, pull and verify, then `down`.** `down` reads the root's evidence again and compares it
with the marker file by file, **strictly only for the evidence a step or the study writes:** the fault journal
(`fault-journal/`), the relay's journals (`fault-relay/`, `late-steps.jsonl` among them), the shard claims
(`shard-claim.json` and its lock), the step records (`shard-steps/`), the data manifests, and the files `up` writes once
(`facts.json`, `platform.toml`, `render-identity.json`) with the pilot's `day0-wiring.toml`. A step that started after
the collect (a resume), a running step that goes on writing the journals (the collect ran while it ran), a line appended
to a journal, or any other change to one of them makes the marker stale, and `down` refuses, naming the files, until the
step has ended and `collect` is run again (a `down` that began and failed halfway is not gated again). **The
federation's own files are not refused:** the state (`hpc-federation.json`) and the link check (`link-check.json`) are
rewritten while the federation runs (by state updates, and by each link check), so they post-date every run once
`collect` has run. `down` goes on, says in its output which of them changed after the collect (names only), and records
it in the state (`evidence_rewritten_after_collect`: the time in UTC, the shards, the names); the copy `collect` took
stays what the post-check reads, so no second archive is made. A link check a step makes also appends to the fault
journal's holds, so a step still running after the collect is refused through the journal all the same.
The marker names each archived file by its path in the root, and takes every path the archive can hold: up to
`MAX_DEPTH` (4) directories below a tree and names of at most 200 plain characters, the longest being 1,018 characters
of the fault journal's. `pass-env.json` is in the results, not in the root: `verify-copy` checks it. A root that holds no evidence a run left
(an NFS silly-rename file, a removed file still held open, is none: a `down` run again after a removal cut short finds only
these) goes down as it always did. To remove a root whose evidence you do not want, give `hpcfed.py down --discard-evidence`:
down's output then says which shards' evidence (or which kinds, for a root with no step record) was discarded, and **every
use is recorded in the federation's state file** (`evidence_discarded`: the time in UTC, the shards, the kinds of
evidence, whether their evidence had been collected and changed since, and the reason given with `--discard-reason TEXT`,
at most 500 characters, if any). The state file goes with the root when
`down` ends, so that line in down's output is what outlives it: keep it. (`fleet.py down` takes no such flag: it reports the refusal,
and `hpcfed.py down --discard-evidence` is run for that root by hand.) A `collect` after the root is gone (a down that
followed an earlier collect) lists the root archive that collect wrote, if it still verifies, and refuses otherwise.

Every other snapshot is resume material and **not part of the required pull.** A snapshot names the archive its step
started from (`shards.py`, `_run_step`: `facts = {"shard": spec, "base": base}`), and `--resume` restores the newest one
only if it names the shard's current archive (`restore_newest`: `facts.get("base") != base` returns none). The snapshots
of the step that wrote an archive name an earlier archive or none, and the last of them was taken before the archive was
written, so the verified archive holds all they do. (Also not required: `<label>/running.json` and the manifest's `*.part`
drafts.) Pulling the whole directory, as above, brings them anyway; `verify-copy` does not check them.

**A snapshot newer than the archive stops `collect --run`.** A step resumed from the archive and killed before it
archived leaves runs only in its snapshot, and the merge would leave them out. Plain `collect` warns (naming the shard
and the snapshot), lists the snapshot for the pull and prints the merge line, so you can pull first. `collect --run`
refuses before any merge (exit 2, nothing written): resume the shard so that its runs reach a new archive, or run
`collect` without `--run` and run the merge line it prints yourself, knowing they are left out. `merge` has no flag to
leave a shard short, so there is none to pass.

`verify-copy` uses the standard library alone (Python 3.11 or later), so it runs on a host where the core is not
installed: take `shards.py` from a checkout. It exits 0 only when the manifest is the file whose hash `collect` printed
(a manifest pulled with the archives proves nothing about itself, so the hash is required) and every file it names is in
the directory, as a regular file under plain directories, with its size and SHA-256. It exits 1 naming each file that is
missing, not a regular file, cut short or changed (name and reason, never a path), and 2 for a manifest it cannot use,
among them a manifest with no list of the files beside the archives (an earlier tip's: it checked the archives alone).
A file in the directory that the manifest does not name (an `*.out.tar` or `*.out.json`, or a journal in the `journals/`
of any shard whose archive it names, whether or not the shard had a journal then: a step that ran after `collect`) is a
warning. **Do not run `down` until it exits 0**: `down` cannot see
your host and does not check that you did.

> **The core on the shared filesystem is a git checkout, or a wheel built with its build identity (C06).** The core
> names itself by its commit in every campaign's environment (`provenance.core_identity`), read from the git work tree
> its `src/` lies in, or from `expcore/_build_identity.json`, which the build (`hatch_build.py`, under `uv build` or
> `hatch build`) writes into a wheel from the checkout it builds from. A copy of the core's files with neither (a
> `git archive`, a tarball, an `rsync` that leaves `.git` out) has no commit to name: its identity is unknown, and
> `expcore run`, `check`, `merge` and `analyze` refuse to start on it, with or without the allow-dirty option. Set the
> core up one of two ways:
>
> - **A checkout:** `git clone` the core's repository to a directory the nodes see, check out the commit to pin, make
>   the interpreter's environment from it (`uv sync`, or `uv venv` and `uv pip install --editable .`), and give
>   `core_src` as the checkout's `src/`. Commit what you run: a checkout with uncommitted changes is refused too unless
>   the command allows dirt, and a campaign run so records the core dirty.
> - **A wheel:** build it from a checkout of that commit (`uv build --wheel`), install it into the interpreter's
>   environment (`uv pip install --python <venv>/bin/python <wheel>`), and give `core_src` as that environment's
>   `site-packages` directory.
>
> `shards.py plan` and `lines` ask the manifest's `python` which core it imports, and `step` reads its own, before
> anything is made: an unknown or stale core (an installed one edited since its build) is refused there, naming this
> remedy. Before, a shard on a `git archive` reached its node, started, and stopped there with "the core's commit is
> unknown".

**One campaign fingerprint before any line (O63).** A campaign's fingerprint holds the platform settings that shape a
run, which each federation's `platform.toml` renders: the faults sanction and targets, the placement bounds, the data
identity, the sandbox's pins. Shards on federations that render different ones pin different fingerprints, and the core's
`merge` refuses them only after the shards have run. `shards.py plan` and `lines` therefore take each shard's campaign
fingerprint first, as `resume-line` does (the manifest's Python, `-I -B`, on a private copy of the study with that
federation's `platform.toml` beside it, as the step places it, and the manifest's `pass_env` variables given it as the
step is: see `pass_env`, below; `shardresume.check_one_campaign`), and print nothing unless they are all one. A refusal names each shard that differs from the largest group of equal shards by its label and
the first differing top-level key of the fingerprint (`platform` for a setting of the platform file, the usual one),
never a host, a path or a value, so the fix is to compare those federations' files; a federation whose `platform.toml`
cannot be read, or whose fingerprint cannot be taken, is refused by its label too (so is a study that holds a
`platform.toml` of its own, a file or a link, as `step` refuses it: the copy is given the federation's by a write that
would follow a link to its target), with the program's status and none
of its output. It costs one start of the manifest's Python per shard.

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

**A killed run's sandbox, and the work root (W1, W2: live slot 5).** A step killed outright takes its sandbox's
processes with it, and leaves the run's directory and its `_runs` record under the work root, and a dead
`fuse.oneclient` mount in it ("Transport endpoint is not connected"). The next shard step on that node, before it uses
its work root, and `down`'s sweep run the Apptainer run reaper over the work root: a run whose driver is shown gone has
its token files removed, its processes ended, each mount in its directory unmounted (`fusermount3 -u`, then `-uz`)
and shown gone by the mount table, then its directory and record removed. A dead run whose leftovers cannot all go
refuses the step and fails the sweep (so `down` keeps the root for a second pass). The sweep then removes the work
root, by `rmdir` alone, once it is empty and no step directory is on the node.

**A resume on another node (`resume-line`).** When the agent's node is lost, or a shard is better continued elsewhere,
hold a job of your own (one node, the manifest's cores and memory, `--no-requeue`) and ask for its line:

    <python> -I -B shards.py resume-line --manifest shards.toml --label L --job J --node N

It prints the shard's step line with `--resume` in job J on node N (in `/`, named as every step of the shard) only
after these checks, each refusing with the shard's label and printing nothing: J is RUNNING, yours, of one component
and the one node N, not a federation's services' job, with the manifest's cores and memory free on it (its own less
its running steps', from `sacct`); no step of the shard is listed under any job of yours (`squeue -s -u`), and its
running mark names no step still listed; the newest snapshot that verifies (restored into a private copy on the login
node: every file against its hash, the store's integrity, every ended attempt's marker) names the shard and the archive
its results hold, and pins the campaign fingerprint the study pins with this federation's `platform.toml` (computed by
the manifest's Python), as every other shard's newest snapshot or archive does. It then records J and N in the
federation's state file (`shard_nodes`), so `down` sweeps N in J (it never cancels J; one that ended is said, and the
next shard step there reaps what it left). Hand-edited lines are not supported: nothing checks them.

**The running mark.** Every step keeps `<results>/<label>/running.json` (its node, process, job, step name and a
nonce) from its start to its own end, written exclusively; a step killed outright leaves it. A start is refused while
one is there; a `--resume` takes one over only on the node it names once its process is gone (a resume of `lines` on
the same node), or with the nonce `resume-line` showed gone (`--over`). The mark is never absent while a step holds
it: a takeover first makes that mark's one claim (`.running.json.<nonce>.taken`, exclusively, never removed), then
renames its own mark over it, so of any number of launches of one line one alone runs. `down` cancels a step of this
federation's shards still running in a resume's job, by its id, before it sweeps that node, and a step still there
after it fails that sweep (the root is kept).

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
`platform_fingerprint_sha256` for a changed SIF: the fingerprint holds the pins; a federation whose artifacts tree
differs, has one where the plan expects none or none where it expects one, is named by `study_artifacts_tree_sha256`)
and never a value, a hash or a path; so does a federation with no record in its root, an unreadable or malformed one, or no `EXPCORE_FED_ROOT`. Nothing is
made. A record is only the file `up` wrote: a `platform.toml` edited by hand afterwards is not seen, and a line edited
to drop the two words runs unchecked.

Whether or not one is expected, a step that has the federation's record writes it into
`out/study-state/render-identity.<step id>.json` (the record as it validated, in canonical form: the bytes `up` wrote,
never anything else the file held; a key given twice refuses the record) at its start: after a `--resume` has restored
its `out/` and before the dispatcher starts. The file is the step's own (its name holds the UTC time and four random
bytes of that step, and it is made new, mode 0600), so a resumed shard keeps the record of every step that ran, each
under the render it ran under, and none overwrites an earlier one. Being in `out/` from the start, it is in every
snapshot (a snapshot copies each file of `out/`), in the `out/` of a step killed mid-shard, in an `out/` kept for
`--archive-left`, in the archive, and where a post-run script can read it. A step that cannot write it does not start
(exit 2, nothing spent). A dispatcher that leaves nothing in `out/` but the record leaves no `out/` to archive, as one
that leaves none. A federation whose `up` wrote no record (one brought up before O74, or a layout with no platform
template) and a plan that expects none run as they did and write none; `collect` and the core's `merge` take an
`out/` with the directory as it is (the merge reads `out/runs` alone).

**The operator's own variables: `pass_env` (O63).** A step's environment is an allowlist, so no variable of the
operator's reaches the dispatcher. A study's fault sanction has two halves, the configuration's (`sanctioned = true` in
`platform.toml`) and the operator's, a variable in the dispatcher's environment; without it every fault-injecting run
launched through `shards.py` is refused. The manifest declares such variables by name, and only by name:

    pass_env = ["FAULT_ACK_MODE"]   # an invented name; each once; the value is the operator's, read when `lines` runs

A name matches `^[A-Z][A-Z0-9_]{0,63}$`, contains none of KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL or AUTH
(any case, anywhere in it) and is none the step sets itself: the allowlist's, `HOME`, `TMPDIR`, the XDG, Apptainer, uv,
pip and git variables of its own home, anything starting `EXPCORE_` or `PYTHON`; and none that changes how a program
is loaded or started: anything starting `LD_` or `DYLD_`, `BASH_ENV`, `ENV`, `IFS`, `GCONV_PATH`, `LOCPATH` and `NLSPATH`
(a library named by `LD_PRELOAD` runs before Python starts, so before the step's own checks, and the step's `env -i`
words would carry it there). `plan` and `lines` read each value
from the environment they run in before any line is printed (`resume-line` the same, before it records its node); a
variable that is unset, or whose value is neither a plain word (`^[A-Za-z0-9._:=,+-]{0,128}$`, but not `.` or `..`) nor
an absolute path (`^/[A-Za-z0-9._/-]{1,1024}$`, such as `/srv/example/artifacts/tree`, with no `..` or `.` part and no
`//`), so that no space, quote, `$`, `;` or other shell character rides on a line, refuses them naming the variable
and never the value. Each line then pins the value: an `env -i` word
`NAME=VALUE` after the step's own, and `--pass-env NAME` at the end of the step's arguments; `plan` prints
`pass_env: {NAME: VALUE}` beside each line. What the operator's environment holds when the step starts is not read, so
a step cannot gain or change one. With no `pass_env` the lines and the plan are what they were, word for word.

The declared variables reach the plan-time builds of the study's plugin too (O63). `plan` and `lines`, `resume-line`
and `fleet.py plan` build the plugin to take each shard's campaign fingerprint (above), by the manifest's Python in a
program of its own whose environment is the same allowlist. A plugin that needs a declared variable (the first
study's refuses without its artifacts directory) could never be fingerprinted there, so no line could be printed. That
program is now given each declared variable with the value the lines pin, read as above before it runs, so an unset
variable or a bad value refuses before any program is started and names only the variable; it is given nothing else of
the operator's environment, and `fleet.py plan` gives it the values of the environment `fleet.py` runs in, as its lines
pin them. A plan that declares variables and has not read their values is refused before the program runs. The probe
that asks which core the Python imports builds no plugin and is given none.

`step` checks again, as its first act (before the render identity and the guard), what `lines` checked, since a line
can be edited: each name declared with `--pass-env` must be one it may pass, be given by an `env -i` word with a plain
value and be declared once, or the step ends with status 73 (`PRE_STEP_REFUSED`), naming the variable, with nothing
made. The dispatcher's environment is then the allowlist, the step's home and `TMPDIR` and exactly those variables; a
variable the node holds that the line did not declare never reaches it. The post-run script runs in the dispatcher's
environment and sees them too; the pre-step guard, which runs before anything is made, does not. `step.json` and the
archive's manifest hold each passed name and value as `pass_env`. `step` never reads the manifest, so it cannot see a
line that dropped a declaration together with its word (the same as the render identity's two words, above): `collect`
refuses a shard whose manifest lacks a name the plan declares, or holds one it does not, naming the shard and the
variable, as it refuses one whose line dropped the post-run script. A dropped sanction variable also fails closed: the
study's own refusal stops the run.

**One value for a shard's whole life (O63).** The first step of a shard that passes variables records each name and
value once, in `<results>/<label>/pass-env.json` (whole, mode 0600, once it holds the shard's running mark; nothing is
written when it passes none, and never more than 65,536 bytes, which is what its reader takes: a step whose variables
would make a longer record is refused, naming the limit, before its dispatcher starts). Every later step is compared
with that record before anything is made or launched, so a
resume or a later fleet step cannot carry a value its shard's first step did not have:

- `step` does it as its first act, `--resume` or not (a first step killed before its first snapshot leaves this record
  and no archive, so a plain relaunch is compared too), and ends with status 73;
- `resume-line` does it before it asks Slurm anything or records its node, and prints no line;
- `fleet.py step` does it for each shard it would launch, and refuses the whole command, launching nothing. `plan` and
  `lines` print first steps' lines and are not compared.

A declared variable whose value differs, one the first step did not pass and one it passed that the line no longer
carries are each refused, naming the variable and never either value. A record that is empty, is not JSON, is another
shard's, holds a name or a value a variable may not carry, is too long or is a link is refused too, never taken for
none, so emptying it does not lift the rule. A shard with no record and no results (an archive or a snapshot) has had
no first step: nothing is compared, and a step that passes variables records them. A shard with results and no record
either had a first step that passed nothing or has lost the record (deleting `pass-env.json` is no way round the rule):
its latest archive's manifest, which holds each name and value its step passed, says which, and a later step is compared
with what it records (a step that now passes a declared variable the manifest does not hold adds one mid-shard, one that
drops a recorded variable or gives another value is refused likewise, each naming the variable and never a value). A
step that matches the manifest continues and writes the record again. A manifest that cannot be read as the shard's
(not JSON, another shard's, a link, a name or a value a variable may not carry) is refused too, never taken for "nothing
was passed". A shard with a snapshot alone and no archive has nothing to say what its first step passed. Nothing here removes the record: a shard that has
made nothing and must start over with other values has it removed by hand, which is the operator's call.

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

## A fleet of federations (`fleet.py`)

A campaign's shards run in parallel, one shard per federation and one federation per held job (O68; DESIGN.md,
section 1, "A fleet"). `fleet.py` brings N federations up from one layout, gives each a shard, launches each shard's
step and takes them all down. Run it on the login node, from a private 0700 directory of yours, with `-I`. Its
layout holds each agent's node as a job of its own, `[hpc] sandbox_placement = "separate"`: on the site's Slurm, steps
on a heterogeneous job's services failed with an interconnect error (a live slot), so `up` refuses a `het` layout.
`layouts/pilot-shape-separate.toml` is that layout: `layouts/pilot-shape.toml` with that one line changed (a test reads
both and refuses any other difference), beside the same platform template, so use it as it is:

```bash
set -a; . deploy/hpc-federation/site.env; set +a
F=<an absolute path, not there yet, on the filesystem the login node and the nodes see>
uv run python -I deploy/hpc-federation/fleet.py --fleet "$F" --layout deploy/hpc-federation/layouts/pilot-shape-separate.toml up 4
uv run python -I deploy/hpc-federation/fleet.py --fleet "$F" plan --manifest <shard settings>
uv run python -I deploy/hpc-federation/fleet.py --fleet "$F" step
uv run python -I deploy/hpc-federation/fleet.py --fleet "$F" status
uv run python -I deploy/hpc-federation/shards.py collect --manifest "$F/shards.toml" --into <a new directory> --run
# REQUIRED before down: pull the manifest's `results` directory (archives, journals and the rest, as listed in the
# section on a shard's archive) to your own host and run verify-copy with the hash collect printed
uv run python -I deploy/hpc-federation/fleet.py --fleet "$F" down
```

- **How many.** `HPC_FLEET_MAX` (in `site.env`) is your maximum of federations at once in one fleet, which the grant
  and the scratch inode quota set; with none set, `up` refuses. `up N` past it is refused before anything is made, and
  `add K` counts every federation that may still hold a job (a failed one may, until its `down`). The inode guard is the
  core's own (`hpcquota`, mandatory with `HPC_STORAGE_ROOT`: an override, an unset command or a quota that cannot be read
  refuses the fleet as it refuses one `up`), asked once for all N by the fleet: the account's usage against N times one
  federation's estimate (each federation's own `up` then checks its own estimate again, as it always does). Without a scratch root nothing is on scratch, and the fleet says it made no check.
  The account's home guard (`hpcquota.home_guard`, the section "The account's home, and its guard") is read once too, ahead of the inode guard, whenever the site sets its keys: a home past 95% of its files or bytes refuses the fleet's `up` and `add` before anything is made.
  `HPC_FLEET_PARALLEL` (1 by default) is how many ups or downs run at once; with more than one, two members' ups can
  race for a node ("Two federations of one account"). The maximum is one fleet's: two fleets of one account share the
  grant by hand.
- **What `up` makes.** The fleet's directory (new: never adopted), and federations `fed-1` ... `fed-N`, each its own
  root `<fleet>/fed-k/root`, state file and held job, each brought up by `hpcfed.py up` in a process of its own (its
  output in `<fleet>/logs/fed-k.up.log`). The fleet's record, `<fleet>/fleet.json`, holds each one's root, its job's
  name and its shard, and is the only list the other commands act on. **A failed `up` leaves the others up:** it is
  reported with any job it may still hold, and `down --member fed-k` releases it.
- **`plan`** takes the shard settings, `shards.py`'s manifest keys without `[[shards]]` (its docstring has them), gives
  `shard-k` to `fed-k`, writes `<fleet>/shards.toml` for `collect` and prints each shard's line as `shards.py plan`
  does. A federation that is not linked, is not the one the fleet made, or leaves a space's data undeclared
  (`data_manifest`) gets no shard; a shard whose federation failed stays unassigned. **The first plan refuses outright
  when the federations' platform fingerprints differ** (another zone image's CA, another SIF, another data manifest):
  their shards would never merge. It names each federation and the differing keys. **A federation whose layout has an
  ingested space** (the pilot's `delta-space`) is planned only once its data is verified: run `hpcfed.py --layout L
  --root <fleet>/fed-k/root verify-data` on each, after the study's ingest and before `plan` (the fleet has no verb
  for it); until then `plan` refuses it, naming the space. **The study's own fingerprint is checked too (O63):** the
  platform fingerprint above is the platform's half, and a study's plugin may fingerprint a setting that half leaves
  out, so `plan` also takes the campaign fingerprint beside each assigned shard's `platform.toml`, as `shards.py plan`
  does, and refuses before it assigns or writes anything unless they are all one.
  It also refuses a manifest whose `python` imports a core that names no code (unknown or stale: the note above), before anything is recorded.
- **The study's own refusals and the operator's variables (O63).** The shard settings take `shards.py`'s optional
  keys: `pre_step`, `post_run` (and their bounds), `expect_render_identity` and `pass_env`, each as `shards.py`'s
  manifest has it (above). `plan` and `step` pin them as `shards.py plan` and `lines` do before they print or spawn a
  line: the study's scripts hashed as the study holds them then, each declared variable read from the environment
  `fleet.py` runs in (`NAME=VALUE` as an `env -i` word, with `--pass-env NAME`; an unset variable or a value that may not
  ride on a line refuses the whole `plan` or `step`, naming the variable, before anything is assigned or launched). The
  value a line pins is the one the operator holds when that command runs, so `step` pins afresh (`plan` prints the
  values it pinned beside each line, as `"pass_env"`), but a shard whose first step recorded its values
  (`<results>/<label>/pass-env.json`, above) is launched only with those: another value, or a name added or dropped,
  refuses the whole `step` before anything is launched, naming the variable and never either value. `shards.toml` declares the names, so `collect` refuses a shard
  whose step was not given them. Before this the settings took `snapshot_every_s` alone, so a step launched by a fleet
  could carry none of the study's own refusals, and `shards.py resume-line` refused a manifest that declared one.
- **A campaign that injects faults is given only federations whose fault relay serves.** A federation without a
  serving relay can neither inject nor restore. `up` records the relay's state in the federation's state file
  (`fault_relay`): `serving` once the relay's heartbeat is seen, `none` when the core has no relay. An `up` that fails
  before the heartbeat, or one of an older core, leaves no record, and no record counts as not serving. The campaign
  injects when its study's `study.toml` has a condition whose treatment is a `provider_outage`, or a federation's
  `platform.toml` has `[faults] sanctioned = true` (`fleetplan.injection`; a `study.toml` that cannot be read counts as
  injecting, one that does not exist as none, and the campaign check refuses a study with no study file). Then `plan`
  gives such a federation no shard and says why (`not used: fed-k: its fault relay is not serving (none: its core has
  no relay), and this campaign injects faults ...`), and `step` refuses a planned shard whose federation's state file no
  longer says `serving`. The state file decides: the `fault relay: serving` or `none` line in a federation's up log is
  for the operator and is never read. A campaign without either marker is unaffected. The test is the campaign's, not
  the shard's: a shard's runs are a share of every condition (the partition is by pair), so one outage condition makes
  every shard inject, whatever tasks the condition names. The record is what `up` saw: a relay that stops afterwards is
  not noticed here, and a federation an older core brought up has no record, so bring one up again (`down --member`,
  `add 1`) before it takes such a campaign.
- **`step`** launches each assigned shard's line detached on the login node (`srun ... --chdir=/`, so the step never
  starts in the directory you ran it from), its client recorded and its output in `<fleet>/logs/`, after checking the
  federation again, that `shards.toml` is the one the record gives, that the manifest's `python` still imports a core
  that names its code, and that Slurm still holds the job the line lands in, running, under the name its federation's
  `up` gave it (Slurm reuses ids). A shard is not launched again while its
  client runs; one whose step ended is relaunched only with `--shard L --resume`. A shard refused exits 1. A site that
  needs `HPC_STEP_NETWORK` gives it to each federation's own steps (`hpcfed`); a shard's step, on the agent's own job of
  one component, carries none.
- **`status` and the first lines of `plan` say each federation's state in plain words**, by its label (`fed-k`),
  never a host: `up and ready` (linked, its relay serving), `up with no relay`, ``failed, still HOLDING 2 job(s): it
  bills until `fleet.py down --member fed-k` `` (the count is the distinct job ids its state file and the fleet's record
  name; a failed member whose up was refused before any job says `no job on record`; a member whose down failed after
  its jobs were cancelled, the state file's phase `down`, says its jobs are gone and its root is kept), or `down`. `plan` says them
  first, so a refused plan shows a failed federation that still bills. `status` runs nothing and then adds each
  federation's phase and jobs, and each shard's place.
- **Moving a shard.** End its step, then collect the federation's root evidence: `shards.py collect --manifest M --roots-only --shard L`
  (no archive, no `--into`: the other shards need not be final; a step still writing makes `down` refuse the collect as stale), since `down --member fed-k` refuses a root that holds
  evidence a run left (a step ran against it, say) which is not collected (or `hpcfed.py down --discard-evidence --discard-reason ...` by hand: recorded).
  The shard's archive under its label is named by the shard, so the root archive the move leaves (`<label>.root.*`, made of
  the first federation) is put aside by the next collect as `<label>.<12 hex digits>.root.*` and listed in
  `archives.sha256.json`, never overwritten. `down --member fed-k` takes one federation down and frees its shard (its step ends with the
  job); `add 1` brings a fresh federation up; `plan` gives it the shard only after its own `up` (data and links
  checked), with the fleet's fingerprint, and only when its `platform.toml` leaves `[placement] reset_shortcut` off, so
  every reset there, the shard's first included, takes the full reads; `step` then starts it with `--resume` from its
  snapshots or archive. A federation that ran a shard is never given another.
- **`down`** takes down every federation the fleet made (or `--member` one), each by `hpcfed.py down` (which cancels
  its jobs by id) and each verified: the exit status and its root gone. A root that is not the directory the fleet
  made, or whose state file is another federation's (another job name, an agent's job record not the one its `up`
  held, a job id other than the one the fleet recorded), is refused, left untouched and named, and `down` exits 1;
  nothing outside the record is ever looked at. The fleet's directory stays with its record, its logs and each root's
  lifecycle lock (hpcfed never removes one); remove it by hand once `status` shows every federation down.

`tests/deploy/test_hpc_fleet.py`, `test_hpc_fleet_plan.py` and `test_hpc_fleet_relay.py` run the fleet over a fake cluster per federation, the
core's phase the real one over a bootstrap stand-in: several federations, one failing `up`, the maximum and the inode
guard refusing before anything is made, a `het` layout refused, the bound on ups at once, `down` of all and of one, a
federation and a job the fleet did not make left untouched (an agent's job record replaced included), the fingerprint
check, each step on its own federation's agent's job and node and never into a job Slurm no longer holds for it, a
shard moved to a fresh federation, the relay's state read from the state file whichever way the log disagrees, a
federation with no relay refused for a campaign that injects faults and accepted for one that does not, and a failed
federation that still holds jobs said by count. Not run live.

## What the repository's tests cover

`tests/deploy/test_hpc_*.py` run the Couchbase guard against the stand-in, the password rules, a 3.8 parse of every
Python file (the guard runs on the images' Python 3.8), the batch jobs' `--no-requeue` and `--time`, the shim's
public ports against `ports.sh`, the hash check for the images' shipped Couchbase values (on a stand-in value), and, where `gcc` exists, the shim's build with every warning an error and its unit
test (`tests/shim_unit.c`), and the egress shim and collector against `tests/egress_probe.c` (where Unix sockets are
allowed). `tests/deploy/test_hpc_fed_plan.py`, `test_hpc_fed_updown.py` and `test_hpc_fed_bundle.py` run the driver
against a fake Slurm (O57): the layout's sizes and the held job's shape, the submit read-back, every step's form and
allowlist, a hung client and a step past its bound, the state file, `up`'s placement, peers and token pipes, `down`
after a whole or a partial `up`, the core's phase writing `platform.toml` and the ten-pair link-check record, and
`zone-export.sh` on a stand-in work directory. `tests/deploy/test_hpc_fed_slots.py` and `test_hpc_fed_slots_provision.py` (O83) provision a layout's slots with
the Compose bootstrap and the HPC one over a fake zone each (an existing slot user matched or refused, never changed),
render the pilot's slot layout through the core's phase and load the file with its map, slots and tool server, and run the
driver's preflight over the site's study directory. `tests/deploy/test_hpc_inode_guard.py` and `test_hpc_fed_storage.py` run
the provider storage on scratch (O57) against the same fake Slurm, with an invented quota command and a temporary
directory for scratch: usage under, at and one past the 95% line, every way a quota can fail to be read, the override
refused on scratch, the estimate against the manifests `up` writes, the directories `up` makes and records first, and
`down` removing exactly the recorded ones, shown gone by a count, never following a link and never touching what `up` did
not make. `test_hpc_storage_bind.py` and `test_hpc_posix_storage.py` run the bind: each provider's step given its own
directory and, without the key, word for word as before; the node's checks and refusals; the service's argv with the bind
and without; two platform fingerprints for a tmpfs and a scratch federation, differing in the storage kind alone; and the
data check reading the storage on scratch.
`tests/deploy/test_hpc_home_guard.py` and `test_hpc_fleet_home_guard.py` run the account's home guard (O57) over the
same fake Slurm and a real `Runner` against a small script, with an invented command and format: files and bytes under,
at and one past the 95% line, every way the home can fail to be read (a limit of 0 included), messages that hold
figures and never the tool's stderr or command line, the override, a site with no key, the keys not recorded in the
state file, and the fleet reading the home once at `up` and at `add`, ahead of the inode guard.
`tests/deploy/test_hpc_oneclient_bundle.py` builds the oneclient bundle
from invented images against a fake Docker (O65): the closure inside the image, the scratch container's discipline, a
reproducible tar and the self-check's refusals. The bundle's wider offline suite reads files copied out of the two pinned images
(patch drift, the images' binaries under the shim) and is not in the repository; neither are those files.
