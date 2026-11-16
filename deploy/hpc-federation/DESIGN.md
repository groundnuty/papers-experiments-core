# The HPC federation driver: process model

`hpcfed.py` brings a federation up on HPC batch nodes, and takes it down, from the same layout file the Compose
federation reads (`deploy/compose-federation`: `fedspec.py`, `fedlayout.py`). Each service is the bundle's (`bundle/`,
README): a setuid Apptainer writable sandbox started with `--containall`, the port shim, the Couchbase guard, the hashed
default checks. This file is the contract the later milestones build on: where services run, how each is found, stopped
and started again, what the federation's state file holds, and where `platform.toml` comes from.

**This file names no site.** Accounts, partitions, node names, addresses and paths come from `site.env` (never
committed; `site.env.example`), or are read from Slurm at run time and written only into the federation's root.

Status: written, not run. What a live slot must verify first is listed under "Assumptions a live slot verifies first".

## 1. Placement: one held job, every service a step

**Decision: one held batch job for the whole federation, and every service, and every control action, a job step
launched from the login node with `srun --jobid=<job> --exact`.** The batch script only holds the allocation
(`exec sleep <seconds>`). This replaces the bundle README's manual model of one batch job per service.

Why, against one batch job per service (`zone.sbatch`, `provider.sbatch` submitted alone):

1. **A service must outlive its own stop.** When the service runs as the batch step of its own job, the job ends when the
   service ends, the node goes back to the scheduler, and the work directory on the node's tmpfs (rootfs, secrets,
   Couchbase bucket, registration, posix storage) goes with it. A provider can then be replaced, never stopped and
   started again. In a held job the node stays allocated across a stop, so a start lands on the same node, the same
   address and the same work directory (section 7).
2. **Every address is known before any service starts.** A held job's nodes are allocated together, so the zone's
   peer list (the shim's `/etc/hpcop-peers`, read once per process before the service starts) holds every provider's
   real address. With one job per service, the zone starts before the providers' nodes exist: the live runs worked
   around it by pinning candidates and listing up to 30 candidate addresses as the zone's peers.
3. **No gap between dependent services.** Separate jobs are scheduled separately (a live run waited over a minute for a
   provider's job while the zone's ran, and another lost a node to a reservation between submits). A held job starts
   all its nodes at once or not at all.
4. **Steps can only be launched from the login node.** `srun` inside a batch script fails on the site's compute nodes
   (its interconnect plugin cannot load there), so every token pipe, every control step and every service already
   needs a login-node `srun`; the held job makes the services those steps too, with nothing else to schedule.
5. **One job to release, by its id.** `down` cancels exactly the job its own `up` submitted (section 5). Some fast
   partitions allow one submitted job per user, which one held job fits and one job per service does not.

The costs, named:

- **The allocation bills its full shape, idle or busy, from its start to its end.** Billing is per job, the larger of
  its cores and a weight times its memory. `--time` is a layout setting, and `down` is expected well before it.
- **Every node of the job has the same shape** (`--cpus-per-task`, `--mem` per node), set by the largest service plus
  the control reserve (section 2). A smaller zone pays for a provider-sized node.
- **The job fails as a whole**: a node failure ends every service. A federation here is throwaway, as in Compose.
- **Each step's `srun` client lives on the login node** for the step's life: detached (`setsid nohup`) for a service,
  bounded and supervised for a control step (section 3).

### Roles and nodes

The job has `1 + N` nodes for a layout of `N` providers. Once the job runs, its node list
(`scontrol show hostnames <NodeList>`, in Slurm's order) is assigned in the layout's order: the first node to the
zone (it also runs the holding `sleep`, which costs nothing), then one node per provider, in `[providers.*]` order.
**One service per node**: the public ports are fixed (8443, and 6665 on a provider), and the uid is the security
boundary on a node (bundle README). Later milestones add nodes with roles of their own (the agent's cell, which must
never share a node with a service) by appending them; the order of the existing roles does not change.

## 2. Sizing: service steps, control steps, and the node

The sizes are layout settings, in an `[hpc]` table of the same layout file. `fedspec.py` accepts the table and keeps
it unread; the Compose path ignores it, and `hpcfed.py` checks it. The defaults are sized from the live peaks: the
zone about 4.0 GB of RSS plus its 1.9 GB rootfs on tmpfs, a provider about 4.8 GB plus its 2.5 GB rootfs, plus the
provider's posix storage on the same tmpfs (test data only, here).

```toml
[hpc]
time = "02:00:00"                        # the held job's --time; up refuses a federation that would outlive it
stop_seconds = 600                       # a service's graceful stop bound (teardown.sh --timeout)
zone = { cpus = 4, memory = "8g" }       # the zone's service step
provider = { cpus = 4, memory = "10g" }  # each provider's service step (posix storage on tmpfs included)
control = { cpus = 1, memory = "1g" }    # the control reserve on every node (mint, deliver, teardown, status, exec)
```

- **A service step** runs `srun --exact -c <cpus> --mem=<memory>`. tmpfs counts against the memory of the cgroup that
  writes it, so the rootfs and the storage are inside the service step's memory.
- **A control step** (a token's mint or delivery, a teardown, M4's status and exec) runs `srun --exact -c 1
  --mem=<control memory>` on the node's one spare core. **No step ever uses `--overlap`**: another study saw every
  overlapping step land on the same CPUs. With `--exact` a control step never takes the service's CPUs and the service
  never takes the control core; two control steps on one node queue on that core (srun waits for it), bounded by the
  driver's timeout. Control memory is reserved for the same reason: a step's `--mem` cannot oversubscribe the node
  without `--overlap`. The largest control step seen live is the registration token's delivery (it peaked at about
  180 MB and was killed under 100 MB), so 1 GB.
- **The node**: `--cpus-per-task = max(zone, provider).cpus + control.cpus`, `--mem = max(zone, provider).memory +
  control.memory`, `--ntasks-per-node=1`, `-N 1+N`. With the defaults: 5 cores and 11 GB per node, 6 nodes for five
  providers. `hpcfed.py describe` prints the cores and memory per node and in all, and the billing units per hour when
  `site.env` gives the site's memory weight (`HPC_BILLING_MEM_WEIGHT`): `nodes × max(cores, weight × GB)`.

## 3. How every step is launched

```
srun --jobid=<job> --exact -N1 -n1 -w <node> -c <cpus> --mem=<mem> -J <step name> --export=PATH \
     [--output=<log>] /usr/bin/env -i <allowlist> <argv>        # stdin </dev/null unless it is the payload
```

- **The environment is an allowlist**, set on the node by `env -i`, so neither the login environment (srun passes it
  otherwise) nor a site task prolog decides it: `PATH` (`HPC_STEP_PATH` from `site.env`), `LC_ALL=C`, and
  `TMPDIR=<node tmpfs>` (`HPC_NODE_TMP`, default `/tmp`). Nothing else, and never a secret.
- **Every write on a node is under the node's tmpfs**, never Slurm's per-job `$TMPDIR`: on the site that is a link
  into a shared filesystem with a file quota. The allowlist sets `TMPDIR` itself, and the bundle refuses a `TMPDIR`
  that is a link (`lib.sh`). Work directories are the bundle's `mktemp -d <tmpfs>/hpcop-<oz|op>.XXXXXX` (0700); the
  driver passes `--workdir` explicitly and accepts no recorded work directory that does not match
  `^<tmpfs>/hpcop-o[pz]\.[A-Za-z0-9]{6}$`.
- **Every step has a name** (`-J`), `<prefix>-<service>-<verb>-<n>`, where `<prefix>` is the allocation's name (section
  5). The step id is read back by name from `squeue -s -j <job> -o '%i %j'`, so `down` finds a step whether or not its
  id reached the state file.
- **A finished step's srun client can hang on the login node** with its work done (another study saw 25 minutes). A
  control step's client is ended only when its step has been gone from `squeue -s` for three polls in a row; a step
  still listed at the bound is cancelled by its own id (`scancel <job>.<step>`), never more.
- **The core the driver imports is recorded**: `up` writes the resolved paths of `expcore_onedata`, `fedlayout` and
  `hpcfed` into the state file, and refuses to run when `expcore_onedata` is not imported from this checkout's `src`.
  The bundle's scripts run from this checkout's `deploy/hpc-federation/bundle`, which must lie on a filesystem the nodes
  see; the driver never takes the bundle from anywhere else.

## 4. The federation's root and its state file

The root is the Compose federation's (`fedlayout.Layout`: `EXPCORE_FED_ROOT_PARENT`, `EXPCORE_FED_ROOT`, the same
checks, the same mark): a 0700 directory, here **on a filesystem the login node and the nodes see** (named in
`site.env`; never node tmpfs). It holds what `up` lays out for Compose (`platform.toml`, `fault-journal/`,
`link-check.json`, `facts.json`, `ca/`, `secrets/`) and the state file, **`hpc-federation.json`**:

```json
{
  "format": "expcore-hpc-federation/1",
  "project": "<the layout's project>",
  "layout": {"source": "<file name>", "digest": "<sha256>"},
  "core": {"expcore_onedata": "<path>", "fedlayout": "<path>", "hpcfed": "<path>"},
  "allocation": {
    "name": "hpcop-<project>-<8 hex>",
    "job": "<id>",
    "submitted": "<UTC>", "started": "<UTC>", "ends": "<UTC>",
    "nodes": ["<node>", "..."],
    "cpus_per_node": 5, "memory_per_node": "11g", "time": "02:00:00",
    "readback": {"TimeLimit": "...", "Requeue": "0", "NumNodes": "...", "NumCPUs": "...", "MinMemoryNode": "..."}
  },
  "services": {
    "zone": {
      "kind": "oz", "node": "<node>", "fqdn": "<node FQDN>", "address": "<IPv4>",
      "ports": {"https": 8443, "epmd": null},
      "workdir": "<tmpfs>/hpcop-oz.XXXXXX", "rootfs": "<workdir>/rootfs",
      "launch": 1,
      "step": {"name": "<prefix>-zone-run-1", "id": "<job>.<step>"},
      "client": {"pid": 0, "start": 0},
      "log": "<log dir>/<prefix>-zone-run-1.log",
      "state": "starting | running | stopping | stopped | failed | removed"
    },
    "<provider name>": {"kind": "op", "ports": {"https": 8443, "rtransfer": 6665, "epmd": null}, "...": "..."}
  },
  "phase": "submitted | placed | zone | providers | bootstrapped | linked | stopping | down"
}
```

- **Service names are the layout's**: `zone` and each `[providers.<name>]`. They are the names `platform.toml`'s
  `[faults.services]` lists and every verb of section 7 takes.
- **No secret**, ever: names, ids, paths, ports, states. Node names and addresses are site facts and stay in the root,
  which is never committed.
- **Written atomically** (a draft in the root, fsynced, renamed), and **before the action it enables**: the
  allocation's name before `sbatch`, the job id as soon as `sbatch` prints it (before the read-back), a step's name
  before its `srun`, its id once `squeue -s` lists it, its work directory once its log names it. So a crash at any
  point leaves enough for `down`.
- **The epmd port** is the job's random one (`configure.sh`), read from the rootfs's `etc/hpcop-erlang-env` by a control
  step once the service is up; `null` until then.

## 5. `up` and `down`

**`up`** (login node), each phase recorded in `phase`:

1. **Preflight**, touching nothing on the cluster: the layout and its `[hpc]` table; `site.env`'s keys; the root's
   rules and mark (as Compose); the two rootfs tarballs and their `.provenance`; the core's self-check. A root that
   holds a state file is refused: `down` first.
2. **Submit** the held job: `sbatch --parsable --job-name=<allocation name> --account --partition --no-requeue
   --time --nodes --ntasks-per-node=1 --cpus-per-task --mem --chdir=/ --output=/dev/null --export=NONE --wrap 'exec sleep
   <seconds of --time>'`. **The read-back:** `scontrol show job -o <id>` must report what was asked (TimeLimit,
   Requeue=0, Account, Partition, NumNodes, NumCPUs, MinMemoryNode, JobName); otherwise `up` cancels that one job id
   and fails, naming the field. sbatch's stderr is kept apart from the id it prints.
3. **Wait for it to run**, bounded (`--queue-timeout`, default 30 min); at the bound it is cancelled by its id.
4. **Place** (section 1), then one control step per node prints the node's FQDN and its IPv4 address on the
   cluster's network (`nodefacts.py`, Python 3.9-compatible, stdlib only).
5. **The zone**: a detached service step, `zone.sbatch <bundle> <zone tar> <zone name> <every provider's address>`,
   run as a step (it holds the service for the step's life, as the standing run did). `up` waits for its log's verdict
   (`hpcop: OK zone`, or `FINISH FAILED` or `hpcop: ERROR`, which fail `up`).
6. **The providers**: one detached service step each, `provider.sbatch <bundle> <provider tar> <name> <zone FQDN>
   <admin mail> <zone address> <every other provider's address>`; for each, a registration token minted in a control
   step on the zone's node piped into a delivery step on the provider's (`mint-registration-token.sh |
   deliver-token.sh`, the bundle's, with `--workdir`); then each log's verdict (`hpcop: OK provider`).
7. **The root's secrets and CA**: the zone's admin passphrase through a control step's stdout into
   `secrets/zone.passphrase` (0600), as the registration tokens travel; the Onedata test CA of the zone's rootfs (public)
   into `ca/federation-ca.pem`.
8. **Bootstrap, from the login node**, through the Compose federation's own `fedboot.Bootstrap` over a layout whose
   services' URLs are `https://<node FQDN>:8443`: the admin's token, the users, the spaces, the support (on each
   provider's one posix storage, owned by the job's uid, and that storage's LUMA local feed mapping the space and every
   member to that uid, as `onboard.py` does), the members, the baseline content (`fedcontent.Content`).
9. **`facts.json` and `platform.toml`** (section 6), then **the link check**: `fedlinks.check_after_up`, the platform
   layer's own check through the rendered `platform.toml`, which writes `link-check.json` in the root through
   `expcore_onedata.linkrecord`. `up` fails unless every pair passes.

**`down`** (login node), whatever `up` reached:

1. Read the state file. When it is missing or unreadable, find the allocation by its name among this user's jobs
   (`squeue -u <user> -h -n <allocation name> -o %i`, an exact name holding `up`'s random part); with no state file and
   no name, there is nothing of `up`'s to find, and `down` says so. **Never `scancel -u`.**
2. If the job runs: a `teardown.sh` control step per service with a recorded work directory (providers first, then
   the zone), each bounded by `stop_seconds`; then wait until no step but `batch` and `extern` is listed in `squeue -s`
   (ending each service step's srun client only then); a step still listed at the bound is cancelled by its own id.
3. One read-only control step per node checks that no recorded work directory is left on it.
4. `scancel <job>`, by the recorded id only; wait until `squeue -j` lists it no more.
5. Remove the root, as Compose's `down` does. The service logs stay in the log directory (no secret: the bundle
   redacts them), named in the run's output.

A job already ended (its `--time`, a failure) skips 2 and 3: Slurm stopped its steps, the bundle's cleanup removed
what its time allowed, and the next job of ours on a node sweeps what is left (`sweep_leftovers`). `down` says which
nodes it could not check.

## 6. `platform.toml` for an HPC federation

**Rendered by `up` into the root from the layout's own `platform_template`, by the Compose renderer
(`fedfacts.write_facts`), with the HPC facts:** `@zone_url@` and `@provider_url.<name>@` are `https://<node
FQDN>:8443`; the ids are this `up`'s. Every path in the template stays relative to the root, so the same template
serves both:

- `tls.ca_file = "ca/federation-ca.pem"` holds the **Onedata test CA** here (each service's web certificate is the
  image's generated test certificate for its node's FQDN; the bundle README lists what that CA's public key means);
- `credentials.admin` is `secrets/admin.token`, minted by the bootstrap; `credentials.users` the users' passwords;
- `[faults] journal = "fault-journal"`, when the template declares faults, so the link-check record lands in the root
  (`linkrecord.record_path`).

The core's platform layer (gate, health, link check, faults) points at `<root>/platform.toml` wherever it runs,
provided it reaches the services' 8443 and reads the root. A layout's link check covers the pairs its
`platform.toml` declares (`linksettings.link_pairs`): `layouts/pilot-shape.toml` beside this file has the pilot's shape
(a zone, five providers, four spaces and the probe space) with invented names, and its template declares the ten
ordered pairs.

## 7. Service verbs for faults and the heal (M4)

Per service, by its layout name. **The core gets one argv per verb**, run on the login node (only there can a step be
launched): `uv run python deploy/hpc-federation/hpcfed.py [--layout PATH] <verb> <service> [...]`, reading the root
from `EXPCORE_FED_ROOT_PARENT` and `EXPCORE_FED_ROOT` like every other command. A campaign that runs on a compute node
cannot launch a step, so it cannot run these; it must run them where the login node does.

**(a) STOP** `hpcfed.py stop <service>`: a control step on the service's node runs `teardown.sh <oz|op> --workdir W
--keep --timeout <stop_seconds>`: `stop-requested` is written with `keep` in it, SIGTERM goes to the recorded service
process (`service.pid`: PID and start tick, through a pidfd, as today), and the service step's script, seeing `keep`,
**removes nothing**: not the work directory, the secrets directory, the Couchbase data, the registration, the storage.
Its step then ends, which frees the service's CPUs and memory; the node stays in the job. `hpcfed.py` waits for the
step to leave `squeue -s`, records `stopped`, and returns. On a stopped service it returns at once (idempotent).

**(b) START** `hpcfed.py start <service>`: a new detached service step, the same size, on the same node, running
`restart.sh <oz|op> --workdir W`: no `node-init.sh`, no `configure.sh`, no token wait; `run_service` again on the same
rootfs, so the same config, cookie, epmd port, ports, address and registration. `launch` is incremented. A start of a
running service does nothing.

**(c) STATUS** `hpcfed.py status <service>`: one JSON line, no secret: `{"service", "state", "running", "identity",
"launch", "node", "step"}`. `running` is true only when the service step is listed in `squeue -s` **and** a control
step on the node finds the recorded service process alive with its recorded start tick. **`identity` is
`"<launch>:<pid>:<start tick>"`**, from `service.pid`, which `run_service` rewrites on every start: stable while the
service is untouched, different after every start.

**(d) EXEC** `hpcfed.py exec <service> -- <argv...>`: a control step on the service's node runs `exec.sh <oz|op>
--workdir W -- <argv>`, which runs `apptainer exec` with `run_service`'s flags (`--containall --no-home --pwd /root`,
a per-call `--workdir` under W) on the same rootfs. The new container has its own PID namespace but the node's network,
so `/etc/hpcop-erlang-env` gives it the job's epmd port and `/root/.erlang.cookie` the cookie, and Erlang's
distribution reaches the running node over loopback (same uid, so the shim's owner check passes). Output is captured;
the exit status is the command's.

**(e) The stop's bound:** at most `stop_seconds` (default 600, the bundle's) for the graceful stop, then supervise.py's
SIGKILL tail (30 s), then the step's end (polled, 60 s at most): **about `stop_seconds + 90` s** in all. The core's
`settle_timeout_s` must exceed it. A provider stopped in about 30 s live.

**(f) The state file**: section 4. `services.<name>` gives the job, the step, the node, the work directory and the
identity; it holds no secret.

**What the bundle must gain for (a), (b) and (d), and the traps a start meets** (M4 builds these; M1 runs the
service steps through `zone.sbatch` and `provider.sbatch` unchanged):

- `zone.sbatch`'s and `provider.sbatch`'s EXIT trap removes the secrets directory and the work directory; with `keep`
  in `stop-requested` it must remove neither, and end with status 0.
- `wait_ready` demands a fresh deployment (`Congratulations! New`) and the guard's first-start flow. A restart over an
  existing deployment prints neither: `restart.sh` needs its own readiness (`service-ready.lock`, `/configuration`
  answering, the guard's verdict for a restart) and the live default and port checks again. The guard's behaviour on a
  restart is untested.
- **`sweep_leftovers` removes a work directory whose `owner.pid` process has ended**, and a stop ends it. A stopped
  service's directory would then be removed by the next `node-init.sh` of ours on that node (another federation's,
  on a shared node). A kept directory needs a mark the sweep honours: `keep` with the allocation's end time (from the
  state file, passed by the stop), kept until that time has passed.
- Concurrent `exec` into a sandbox that a running service holds `--writable` is a live-slot item.
- After a stop, the service step's tmpfs pages (rootfs, bucket, storage) stay charged to the job's cgroup; the
  restarted step's memory must still fit the node with them. Measured live, not assumed.

## 8. A shared read-only imported storage (M3)

M3 replaces a provider's tmpfs posix storage for an imported space with a directory on the shared filesystem, bound
read-only into that provider's container. It changes the service step's binds and the support's storage, not the
process model: the step, the state file and the verbs stay as above.

## 9. Assumptions a live slot verifies first

1. **The login node reaches every service's 8443** on the nodes' FQDNs. The bootstrap, the link check and the core's
   platform layer depend on it. Proven live: node to node (the standing run's cell reached every public port). Not
   proven: login node to node. **Fallback**, if it does not: the bootstrap through the bundle's node-side helpers in
   control steps (as `run/fed_zone.py` and `run/fed_provider.py` do), and the link check in a step on a node, with the
   core's Python (3.12, from a venv on the shared filesystem) run as `python -I` with a self-check of what it imports.
2. **`zone.sbatch` and `provider.sbatch` as `--exact` steps** of a multi-node job with `-c` and `--mem` per step (the
   standing run used `--overlap`).
3. **`srun --export=PATH ... /usr/bin/env -i ...`**: no site prolog re-sets the step's `TMPDIR` after `env -i` (none
   can: `env -i` runs last).
4. **The panel through 8443 as the admin's token** (`fedboot.Bootstrap.panel`) on the bundle's providers, and the
   support with the LUMA local feed's mappings.
5. **The pair count**: the rendered `platform.toml` of the pilot's layout gives ten pairs (`link_pairs`), and each
   passes over the cluster's network.

## 10. Who builds what

| Milestone | Builds | Relies on |
|---|---|---|
| M1 (this) | `hpcfed.py up`, `down`, `describe`; the state file; placement; sizing; the submit read-back; the step launcher (allowlist, names, client handling); the bootstrap and `platform.toml`; the link check | the bundle as it is |
| M3 | the imported storage on the shared filesystem, read-only, in the provider's step | section 8 |
| M4 | `hpcfed.py stop`, `start`, `status`, `exec`; `teardown.sh --keep`, `restart.sh`, `exec.sh`; the sweep's kept mark | sections 4 and 7 |
