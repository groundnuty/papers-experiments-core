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

- **The allocation bills its full shape, idle or busy, from its start to its end.** Billing is per node, the larger of
  its cores and a weight times its memory. `--time` is a layout setting, and `down` is expected well before it.
- **Every service node has the same shape** (`--cpus-per-task`, `--mem` per node), set by the larger service plus the
  control reserve (section 2). A smaller zone pays for a provider-sized node. The agent's node is not one of them: it
  has its own shape, as its own component of the job (below).
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

### Two federations of one account

**One service per node holds across federations too, and `up` enforces it** (issue 398). The services have no network
namespace and Couchbase's guard watches the fixed port 8091 (`bundle/lib.sh`), matching listeners by uid only, so a
second federation's Couchbase on a node of the first fails on its bind and its `up` fails closed. The held job asks for
cores and memory, never a whole node, and Slurm packs jobs onto a node that has room: nothing placed two federations
apart. **Decision: node exclusion, not per-federation ports.** The Couchbase port set is large and fixed in the image;
a port per federation would patch the shim, the guard and the images, for a placement question that the scheduler can
answer. Asking the site for whole nodes (`--exclusive`) was the other option: it would change the held job's shape and
what it bills (above), which this problem does not need.

- **Which jobs.** Every live job of the account (`squeue --me`, read-only, ids, names, states and node lists) whose
  name starts with `hpcop-` (`hpcexclude.JOB_PREFIX`, the one prefix `up` names its jobs by, and the bundle's batch
  scripts carry): the held job, the agent's own job (`-sandbox`), the older scripts' `hpcop-zone` and `hpcop-provider`. A
  job is read whole, a heterogeneous job's components together, so another federation's agent's node is one of its nodes:
  the agent's sandbox never runs on a node that hosts an Onedata service. A pending job holds none, a completing one still
  does.
- **Before the submit** (section 5, step 2): the nodes of those jobs go to every component of every held job `up`
  submits as `--exclude`, named on every component so that nothing depends on Slurm passing it on, and only when there
  are some, so
  the argv is what it was without them. The count, never a name, is in the state (`allocation.excluded_nodes`) and in what
  `up` says. A read that fails, or a line of such a job that cannot be taken apart, refuses `up` before the state file
  exists: the job is never submitted without the exclusion.
- **After the allocation, before any step** (step 3): a second read; any of this federation's nodes (a service's or the
  agent's) in another live federation's jobs (this up's own jobs left out by their ids) refuses `up`. Its held jobs are
  cancelled by their ids, none of the placement is recorded, and the refusal names the roles (`zone`, `providers[i]`,
  `agent`), never a host. A failed second read refuses and releases the same way. Two ups that read before either
  submitted are the case: the one whose job runs later sees the other's nodes and is refused, and both are when both
  ran before either read again.
- **Too few nodes.** Slurm keeps a job pending, or sbatch refuses it, when the exclusion leaves none that fits. `up`
  waits no longer than the queue bound it always had (30 min, step 3) and its message says that it excluded N nodes of
  other live federations; an sbatch refusal says the same, the nodes' names Slurm repeats removed. What the site's
  sbatch does is not known (a live slot sees it).
- **A fleet** (below) is N such federations: each member's `up` reads the others' jobs, so members brought up one after
  another take distinct nodes by this rule alone; members brought up at once may race and the later one is refused.

### The agent's node: a component of its own

The agent's node S (M5, section 8) needs about the agent's memory, work and tmp bytes plus 4 GB for the gate, the
dispatcher, the collector, the store and the staged image: 18 GB and 11 cores for an 8 GB, 8-core agent, where a service
node needs 11 GB and 5 cores. In one job of uniform nodes, every service node would pay for S's shape. **Decision (the
coordinator's, 16 Nov 2026): S is its own component of a Slurm heterogeneous job**, `[hpc] sandbox_placement = "het"`,
the default:

```
sbatch --parsable --job-name=<name> --account --partition --no-requeue --time --nodes=<1 + N> --ntasks-per-node=1 \
       --cpus-per-task=<service node> --mem=<service node> --chdir=/ --output=/dev/null --export=NONE \
       --wrap='exec sleep <seconds>' \
     : --account --partition --time --nodes=1 --ntasks-per-node=1 --cpus-per-task=<S> --mem=<S>
```

- **Component 0 holds the services' nodes, component 1 holds S.** Its leader's id (component 0's) is the job's id.
  **Every step names its component:** `--het-group=0` on a service node, `--het-group=1` on S. The site's Slurm
  refuses a step of a heterogeneous job that names none ("No directives to start application on any available hetjob
  components", the first live slot), so a step with no `--het-group` does not run in component 0 there. A held job of
  one component (no S, or `separate`) names none. `--wrap` stands once, in component 0 (sbatch builds the script once, for component 0's
  first node). Component 1 repeats the account, partition and time: Slurm propagates no partition, nodes or cores to a
  later component, and the read-back checks what it recorded rather than trusting propagation.
- **The read-back checks both components** (section 5): `scontrol show job -o <leader>` answers one record per
  component (`HetJobId`, `HetJobOffset`); each must hold what was asked, `Requeue=0` and the time included, or the job
  is cancelled by its leader's id, which ends every component. Component 1's own job id (its record's `JobId`) is
  recorded, since squeue -s and sacct list its steps under it.
- **The fallback, should a site refuse heterogeneous jobs: `sandbox_placement = "separate"`**, a second held job for S
  alone, named `<name>-sandbox`, submitted, read back and cancelled exactly as the first, and never stopped or started:
  its gate reaches the providers over the network, as it does in a component. Its steps take its own job id. The two
  jobs are scheduled apart, so the first can run, and bill, while the second waits (bounded as the first), and a
  partition that allows one submitted job per user (reason 5 above) refuses the second.
- **On a site whose switch plugin gives each job a virtual network identifier, `separate` is the placement to start
  from** (the second live slot, 16 Nov 2026). There a heterogeneous job's steps on component 0 failed from a point on
  (`srun: error: task 0 launch failed: Error configuring interconnect`: after the zone and five providers had started,
  every further step on a service node, so `up` failed at the first provider's registration); a step on component 1
  ran, and so did the same steps in a job of one component in two earlier runs. With `separate` the services' job has
  one component. The cause is not established (a count of steps per component, or exhausted identifiers), and the
  placement was not run live on such a site. The other way round it is a site setting, section 3: `HPC_STEP_NETWORK`.
  Neither changes a default.
- **The last resort, a uniform shape sized for S, is not built:** it bills every node at S's shape (below).
- **S never shares a node with a service:** a component, or a job, placed on a node another holds fails `up`, that
  job cancelled before any step.

What it bills, with the pilot shape (six service nodes of 5 cores and 11 GB) and a memory weight of 0.5:

| Placement | Service nodes | S | Per hour |
|---|---|---|---|
| `het` or `separate`, S = 9 cores, 18 GB | 6 × max(5, 5.5) = 33 | max(9, 9) = 9 | 42 |
| `het` or `separate`, S = 11 cores, 18 GB (an 8-core agent) | 33 | 11 | 44 |
| uniform at 9 cores, 18 GB | 7 × 9 = 63 | (one of the 7) | 63 |
| uniform at 11 cores, 18 GB | 7 × 11 = 77 | (one of the 7) | 77 |

### A fleet: N federations, one shard on each

A campaign's #154 shards run in parallel, **one shard per federation and one federation per held job**, as many at
once as the operator allows (`fleet.py`, `fleetrecord.py`, `fleetplan.py`; README, "A fleet of federations"). A
federation stays what it is above: the fleet is `hpcfed.py up` and `down` run N times, plus `shards.py`'s lines,
under one record. One federation's job failing ends one shard's step, never another's.

- **How many.** `HPC_FLEET_MAX` in `site.env` is the operator's maximum of federations at once in one fleet, set by
  the grant and the scratch inode quota; unset refuses. `up N` past it is refused before anything is made; `add K`
  counts every federation that may still hold a job (a failed `up` may, until its `down`). The inode guard
  (`hpcquota.guard`, section 11, mandatory with a scratch root) is asked once for N: N concurrent ups would each read the same usage and pass
  alone, so the fleet reads it against N times one federation's estimate. The home guard (`hpcquota.home_guard`,
  section 11, whenever the site sets its keys) is asked once too, ahead of it, at `up` and at `add`: it has no estimate to
  multiply, so one read answers for all N, and a full home refuses the call before the fleet's directory, a slot or a job
  is made. `HPC_FLEET_PARALLEL` (1 by default) is how
  many ups or downs run at once.
- **One process per federation.** A federation's driver changes process-wide state (the umask its secrets are written
  under), so each `up` and `down` is `hpcfed.py` in a child of its own, with `-I`, from the fleet's 0700 directory,
  its output in `<fleet>/logs/<label>.<command>.log`. One that fails is reported; the others go on.
- **The agent's node is a job of its own** (`sandbox_placement = "separate"`). On the site's Slurm, new steps on a
  heterogeneous job's component 0 failed with an interconnect error once the services had started (a live slot, 16
  Nov 2026), so the fleet refuses a layout that holds the agent's node as a component. A shard's step then lands in a
  job of one component and names no `--het-group`.
- **Names and roots, derived.** Federations are `fed-1` ... `fed-N` and shards `shard-1` ... `shard-N`, from their
  places, never a site value; each federation's job name is its own `up`'s (`hpcop-<project>-<random>`). Its root is
  `<fleet>/fed-k/root`, made by an exclusive `mkdir` inside a slot made the same way, so the fleet never adopts a
  directory; the fleet's own directory is made the same way.
- **The record (`fleet.json`) is the only list.** Each change is written whole, under its lock, before the action it
  enables: a federation is recorded before its slot is made, and its root's device and inode once made, so `down`
  finds whatever `up` reached. The record accepts no root but `<fleet>/fed-k/root`. Nothing is ever found by a search,
  so a federation another `up` made beside the fleet, in its directory or not, is never looked at.
- **`down` takes down only what the fleet's `up` made.** Before each federation's `hpcfed.py down`, its root must be
  the directory the fleet's `mkdir` made (device and inode, no link) and its state file the one that federation's `up`
  wrote (its `command` names this root, its job name is the one recorded, its agent's job record is named as that
  `up` names it, and every job id is the one the fleet recorded at its `up`); otherwise it is refused, untouched, and
  named. The driver's own `down` then cancels its jobs by id and never one whose id now names another job
  (section 5). A root an `up` never marked (refused in its preflight) is the fleet's empty `mkdir`, which hpcfed's
  `down` refuses as unmarked: the fleet removes it with `rmdir`, which removes nothing else. Each is verified: the
  child's exit status and the root gone.
- **One campaign.** `plan` gives a shard only to a federation whose `up` finished (`linked`: each space's data read
  back against its manifest, every link checked), whose identity is the fleet's, whose every space declares its data
  (`data_manifest`, C06) and whose `PlatformConfig.fingerprint()` is the fleet's. The first plan sets that reference,
  and refuses outright when the federations' fingerprints differ (naming each federation and the differing keys):
  their shards would never merge (C29). `step` checks all of it again, and that `shards.toml` is the one the record
  gives, before each launch: a line is never built from a manifest the fleet did not write. It then asks Slurm of the
  job the line lands in: still known, running, and under the name its federation's `up` gave it, since Slurm reuses
  ids and a job that ended may have handed its id to another. Each line runs with `--chdir=/`.
- **Moving a shard (the coordinator's rule).** The shard count is fixed at the fleet's `up`. A shard whose federation
  is taken down (`down --member`) is released, and given only to a federation brought up after (`add`), never one that
  held a shard before, with the same checks and an equal fingerprint, its step started with `--resume` from its
  snapshots or archive (O62). Its first reset there must be full. What the fleet can check is the platform file the
  step carries into the study: it moves a shard only to a federation whose `platform.toml` leaves `[placement]
  reset_shortcut` off (the default), under which every reset takes the full reads; one that turns it on is refused.
  It cannot see a study's own request for one full reset (O51), so a study that turns the shortcut on moves no shard.
- **The shards' results are copied back before `down`.** The shards' archives, archive manifests, journals, each root's evidence archive and (when newer than the archive) newest snapshot are on the shared filesystem the site's scratch policy cleans, and `down` ends the federation: the operator pulls them to their own host after `collect` and checks them with `shards.py verify-copy` before `down` (section 5, "Before `down`").
- **Not built:** a fleet's `down` of a failed federation on its own (the operator decides when a failed `up`'s job
  goes), a watch of each job's walltime, and a fleet across accounts (the maximum is one fleet's, not the account's:
  two fleets of one account must share it by hand).

## 2. Sizing: service steps, control steps, and the node

The sizes are layout settings, in an `[hpc]` table of the same layout file. `fedspec.py` accepts the table and keeps
it unread; the Compose path ignores it, and `hpcfed.py` checks it. The defaults are sized from the live peaks: the
zone about 4.0 GB of RSS plus its 1.9 GB rootfs on tmpfs, a provider about 4.8 GB plus its 2.5 GB rootfs, plus the
provider's posix storage on the same tmpfs (test data only, here).

```toml
[hpc]
time = "02:00:00"                        # the held job's --time
stop_seconds = 50                        # a service's graceful stop bound (teardown.sh --timeout); measured, 17 Nov
step_launch_wait_s = 240                 # what a control step may wait to start; added to every bound on its client
core = "login"                           # where the bootstrap and the link check run: "login" or "step" (section 9)
zone = { cpus = 4, memory = "8g" }       # the zone's service step
provider = { cpus = 4, memory = "10g" }  # each provider's service step (posix storage on tmpfs included)
control = { cpus = 1, memory = "1g" }    # the control reserve on every node (mint, deliver, teardown, status, exec)
sandbox = { cpus = 8, memory = "17g" }   # optional: the agent's node S and its step's size (M5; section 8)
sandbox_placement = "het"                # S as component 1 of a heterogeneous job, or "separate" (section 1)
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
- **A service node**: `--cpus-per-task = max(zone, provider).cpus + control.cpus`, `--mem = max(zone,
  provider).memory + control.memory`, `--ntasks-per-node=1`, `-N 1+N`. With the defaults: 5 cores and 11 GB per node,
  6 nodes for five providers.
- **The agent's node S**, when the layout has one: `sandbox.cpus + control.cpus` and `sandbox.memory +
  control.memory`, one node, its own component or job (section 1). `[hpc] sandbox` sizes its step; **when the layout's
  platform template declares the Apptainer sandbox** (`[sandbox] backend = "apptainer"` with `[sandbox.limits]`), the
  driver reads the agent's limits from it (each `@...@` read as 0): the step is then at least the agent's cores plus 2
  and its `memory_bytes + work_disk_bytes + tmp_bytes` plus 4 GiB, a smaller `[hpc] sandbox` is refused naming the
  least, and with no `[hpc] sandbox` that least is S's size.
- `hpcfed.py describe` prints each shape, where S is held, and the billing units per hour when
  `HPC_BILLING_MEM_WEIGHT` gives the site's memory weight: the service nodes × `max(cores, weight × GB)`, plus S at its
  own shape (the table in section 1).

## 3. How every step is launched

```
srun --jobid=<job> --exact -N1 -n1 -w <node> -c <cpus> --mem=<mem> -J <step name> --export=PATH --chdir=/ \
     [--network=<HPC_STEP_NETWORK>] [--output=<log>] /usr/bin/env -i <allowlist> <argv>  # stdin </dev/null unless the payload
```

- **Every step starts in `/`** (`--chdir=/`), never in the directory srun was called from: where that directory does not
  exist on the node, slurmstepd starts the step in the node's `/tmp` instead, which other users share (another study's
  audit, 16 Nov 2026). `/` exists on every node and nobody but root writes it; a service step's own work directory does
  not exist yet when it starts, and a control step names its work directory by `--workdir`, never by where it starts.
  Every script then works by absolute paths: the bundle's from `$(dirname "$0")`, its work directory from `--workdir`; a
  shard manifest's paths and the run harness's `B`, `SH`, `L` and `GATE_DIR` must be absolute. The shards' steps
  (`shards.py lines`), the test runner's (`deploy/hpc-tests`) and the run harness's (`run/common.sh`) are launched the
  same way, and the held job's `sbatch` takes `--chdir=/` too.

- **`--network=<value>` is on every step the driver launches when the site sets `HPC_STEP_NETWORK`** (empty by default:
  no option, and the line is what it was). A control step, a service step, a teardown and a step in either component
  of a heterogeneous job alike, since the site's switch plugin refused steps of component 0 while a step of component 1
  ran.
  The value is one option string of letters, digits and `_ . , = : + -` (at most 128, led by a letter or digit), checked
  when `site.env` is read, again when the state file's `site` record is read, and again when the argv is made, so it
  never carries a space or a shell character, and a record's value that is no string (a null, a false, a list) is refused
  too, never read as none; a refusal names `HPC_STEP_NETWORK` and not the value. It is recorded in
  the state file with the rest of the site, so `down` and the service verbs use it with no environment. The services
  talk over TCP and need no virtual network identifier, which `no_vni` withholds; it is the value that ran live on
  component 0 (section 9, item 6), and its effect on a whole `up` and on component 1 was not seen. The step line that
  `shards.py lines` prints for a shard is the manifest's own and does not carry the option.

- **The environment is an allowlist**, set on the node by `env -i`, so neither the login environment (srun passes it
  otherwise) nor a site task prolog decides it: `PATH` (`HPC_STEP_PATH` from `site.env`, by default
  `/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin`: the site's nodes keep `ss` in `/usr/sbin`, and the first
  live zone step, without it, failed in its first second), `LC_ALL=C`, `TMPDIR=<node tmpfs>` (`HPC_NODE_TMP`, default
  `/tmp`), and **no home** (`hpcslurm.home_env`): with `HOME` unset many tools take the passwd entry's home, which
  every job and agent of the account shares, so `HOME`, the XDG base directories, Apptainer's configuration and cache
  (`APPTAINER_CONFIGDIR`, `APPTAINER_CACHEDIR`) and uv's and pip's caches name a path below `/dev/null`, which nobody
  can make (a path on the shared tmpfs another user could make first, and fill), with `PYTHONNOUSERSITE=1`,
  `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`: a tool then fails rather than read what another user
  planted. Every bundle script that knows a service's work directory (the batch scripts, `teardown.sh`, `status.sh`,
  `exec.sh`, `restart.sh`, `zone-export.sh`, the token's mint and delivery, and `run_service`) points every one of
  them into it (`lib.sh`'s `home_in`, made 0700 there, gone with it) before any command that may read a home, Apptainer
  and curl among them (`status.sh`, which writes nothing, points them there without making it: `home_at`); a shard step's dispatcher gets them in the step's own directory (`shards.py step`). Nothing else, and never a secret. The bundle's helpers
  then run on the first `python3.11` on that `PATH` (`lib.sh`'s default `HPCOP_PY`), which must be the node's own:
  `supervise.py`, which holds every service, needs `os.pidfd_open`, which uv's standalone Python lacks. Only the core's
  step and `down`'s sweep of a node a shard step ran on (`HPC_CORE_PYTHON`), and the shard step itself (its
  manifest's `python`), run a venv's Python.
- **Every write on a node is under the node's tmpfs**, never Slurm's per-job `$TMPDIR`: on the site that is a link
  into a shared filesystem with a file quota. The allowlist sets `TMPDIR` itself, and the bundle refuses a `TMPDIR`
  that is a link (`lib.sh`). Work directories are the bundle's `mktemp -d <tmpfs>/hpcop-<oz|op>.XXXXXX` (0700); the
  driver passes `--workdir` explicitly and accepts no recorded work directory that does not match
  `^<tmpfs>/hpcop-o[pz]\.[A-Za-z0-9]{6}$`.
- **Every step has a name** (`-J`), `<prefix>-<service>-<verb>-<n>`, where `<prefix>` is the allocation's name (section
  5). The step id is read back by name from `squeue -s -j <job> -o '%i %j'`, so `down` finds a step whether or not its
  id reached the state file; under `het` from `squeue -s -j <component 1's own id>` too, since a step of component 1
  is listed there alone, as `<component id>.<n>`, never under the leader's id (seen live), and under `separate` from
  the agent's own job's.
- **A finished step's srun client can hang on the login node** with its work done (another study saw 25 minutes). A
  control step's client is ended only when its step has been gone from `squeue -s` for three polls in a row; a step
  still listed at the bound is cancelled by its own id (`scancel <job>.<step>`), never more. Both ends of a token's
  pipe are finished whatever the first does. Ending a client is verified (SIGTERM, then SIGKILL, each only while the
  pid still has its recorded start tick), and `down` also ends any `srun --jobid=<job>` of ours on the same login node (run `down` where `up` ran) that
  no record names (one spawned just before a crash).
- **Every Python the driver launches runs isolated and writes no bytecode** (`-I -B`, O58): on a node, on the login node
  (the fault relay's `serve`, `alive` and `stop`, and the verbs' argv prefix, section 7), in a container and in
  `shards.py` (its step, its dispatcher, the merge `collect --run` runs), and so does the test runner's pytest
  (`deploy/hpc-tests`). Without it `python -m` and `python -c` put the directory they were launched from first on
  `sys.path`, and every `PYTHON*` variable is read: on 16 Nov 2026 (F2) a `down` launched from the login node's `/tmp`,
  which held 49 `.py` files of other users, had the relay's `stop` import another user's `/tmp/inspect.py` in place of
  the standard library's. Launch `up`, `down` and the verbs as `python -I -B deploy/hpc-federation/hpcfed.py ...` from a
  directory of ours (the checkout) all the same, never a shared one: `uv run` takes the project of the working directory
  when it has a `pyproject.toml`, and builds it.
- **No bytecode is written** by the driver or a step: `hpcfed.py` sets `sys.dont_write_bytecode`, and every Python the
  driver and the shard driver launch, on a node or on the login node, takes `-I -B` (the bundle's `"$HPCOP_PY"`, the
  fault relay, the shard step, its dispatcher and its `expcore run`, the merge, and the sandbox's gate, collector,
  mount watcher and copy helper on the node). `-I` alone ignores `PYTHONDONTWRITEBYTECODE`, so only the flag stops
  it: live slot 5 found a shard step's first import of a stdlib module writing its `.pyc` into the core's interpreter,
  which uv had installed under the account's home, shared by every job and agent of the account. **Install the venv's
  interpreter under the site directory** (`UV_PYTHON_INSTALL_DIR=<site dir>/python` before `uv sync`), never in the
  shared home, so a node step that writes there by any other path still writes into the site's own tree. The fake Slurm
  of the tests fails any command that runs a Python without both flags.
- **The core the driver imports is recorded**: `up` writes the resolved paths of `expcore_onedata`, `fedlayout` and
  `hpcfed` into the state file, and refuses to run when `expcore_onedata` is not imported from this checkout's `src`.
  The core's step (`[hpc] core = "step"`) makes the same check on the node and exits 97 on a mismatch, which fails
  `up`. The bundle's scripts run from this checkout's `deploy/hpc-federation/bundle`, which must lie on a filesystem
  the nodes see; the driver never takes the bundle from anywhere else.

## 4. The federation's root and its state file

The root is the Compose federation's (`fedlayout.Layout`: `EXPCORE_FED_ROOT_PARENT`, `EXPCORE_FED_ROOT`, the same
checks, the same mark): a 0700 directory, here **on a filesystem the login node and the nodes see** (named in
`site.env`; never node tmpfs). It holds what `up` lays out for Compose (`platform.toml`, `fault-journal/`,
`link-check.json`, `facts.json`, `ca/`, `secrets/`), the federation's render identity, **`render-identity.json`**
(section 8, O74: hashes and a version, mode 0600), and the state file, **`hpc-federation.json`**:

```json
{
  "format": "expcore-hpc-federation/1",
  "project": "<the layout's project>",
  "layout": {"source": "<file name>", "digest": "<sha256>", "path": "<absolute path>"},
  "core": {"expcore_onedata": "<path>", "fedlayout": "<path>", "hpcfed": "<path>"},
  "command": ["<python>", "-I", "-B", "<.../hpcfed.py>", "--layout", "<path>", "--root", "<root>"],
  "site": {"account": "...", "partition": "...", "images": "...", "logs": "...", "step_path": "...",
           "node_tmp": "/tmp", "node_python": "python3.11", "core_python": "...", "admin_mail": "...",
           "billing_weight": null, "step_network": "", "study_artifacts": "<directory, or empty>",
           "storage_root": "<scratch root, or empty>"},
  "study_artifacts_tree_sha256": "<64 hex: the digest of that directory's tree, taken in the preflight (section 8); absent without one>",
  "storage": {"root": "<the site's scratch root>", "parent": "<root>/<allocation name>", "token": "<32 hex>",
              "providers": {"<provider name>": "<parent>/<provider name>"}},
  "allocation": {
    "name": "hpcop-<project>-<8 hex>",
    "job": "<id>",
    "nodes": ["<node>", "..."],
    "cpus_per_node": 5, "memory_mib_per_node": 11264, "time": "02:00:00",
    "readback": {"JobName": "...", "TimeLimit": "...", "Requeue": "0", "Account": "...", "Partition": "...",
                 "NumNodes": "...", "NumCPUs": "...", "MinMemoryNode": "..."},
    "started": "<Slurm's StartTime>", "ends": "<Slurm's EndTime>",
    "excluded_nodes": 0
  },
  "sandbox": {
    "placement": "het | separate", "name": "<allocation name> (het) | <allocation name>-sandbox (separate)",
    "job": "<the id a step on S takes: the leader's (het) or its own job's (separate)>",
    "het_group": 1, "component_job": "<het: component 1's own id>", "node": "<S>",
    "cpus_per_node": 9, "memory_mib_per_node": 18432, "time": "02:00:00",
    "readback": {"JobName": "...", "...": "..."}, "started": "...", "ends": "..."
  },
  "roles": ["zone", "<provider name>", "...", "sandbox"],
  "owner": [5001, 5001],
  "services": {
    "zone": {
      "kind": "oz", "node": "<node>", "fqdn": "<node FQDN>", "address": "<IPv4>",
      "ports": {"https": 8443, "epmd": null},
      "workdir": "<tmpfs>/hpcop-oz.XXXXXX", "rootfs": "<workdir>/rootfs",
      "launch": 1,
      "step": {"name": "<allocation name>-zone-run-1", "id": "<job>.<step>"},
      "client": null,
      "log": "<log dir>/<allocation name>-zone-run-1.log",
      "state": "planned | starting | running | stopping | stopped | failed | removed"
    },
    "<provider name>": {"kind": "op", "ports": {"https": 8443, "rtransfer": 6665, "epmd": null}, "...": "..."}
  },
  "clients": {"<step name>": {"pid": 0, "start": 0}},
  "relay": {"pid": 0, "start": 0, "log": "<log dir>/<allocation name>-fault-relay.log"},
  "data": {"ingested": {"<space>": {"manifest": "<sha256>", "declared": true,
                                    "verified": null}}},
  "shard_nodes": [{"job": "<the operator's job id>", "node": "<node>", "label": "<shard>", "name": "<its job name>"}],
  "phase": "preflight | submitted | placed | zone | providers | bootstrapped | linked | stopping | down"
}
```

- **`allocation`** is the services' job (under `het`, the heterogeneous job's leader and component 0's nodes and
  read-back). **`sandbox`** is the agent's node S, `null` without one: **M5's shard driver finds S here**, and launches
  its step with `--jobid=<sandbox.job>`, plus `--het-group=<sandbox.het_group>` when that is not `null`; S's facts
  (FQDN, address) are in `services.sandbox`, the role's record. `down` cancels `sandbox.job` itself only under
  `separate`; under `het` the leader's cancel ends S's component.
- **`shard_nodes`** (absent until one is recorded) are the nodes a shard of this federation was resumed on outside its
  own jobs, each in the operator's held job it was resumed in: `shards.py resume-line` records one, under the root's
  lifecycle lock, before it prints the line that starts the step there. `down` sweeps each such node in that job, only
  while the job runs and still carries the recorded name (Slurm reuses ids), and never cancels it; the launcher runs a
  step in such a job on its recorded node alone.
- **`site`** is what `up` read from `site.env` (no secret): every later command reads it here, so `down` and the
  service verbs need no environment. **`command`** is this driver's argv prefix, which `platform.toml`'s `[faults]
  command` holds. **`clients`** are the detached srun clients of the service steps, by step name, which `down` ends
  once their steps are gone. **`owner`** is the job's uid and gid on the nodes (the posix storage's owner).
- **`data.ingested`** (O57, C06) names each space whose data is the study's own, put there after `up` (section 6a),
  written by `up` from the layout alone: `manifest` is the SHA-256 of the manifest it is to be verified against
  (`Manifest.digest()`: the declared file's, or the empty manifest's for a space that declares none), `declared` whether
  the layout declares it, and `verified` is `null` until `hpcfed.py verify-data` has read the space back and found
  exactly that manifest, then `{"manifest": <the digest it compared with>, "files": n, "bytes": n}`. **A state without
  `data`** (an `up` of an earlier driver) cannot be shown verified, and `shards.py plan` refuses it. Every run of
  `verify-data` sets `verified` back to `null` first, and a pass sets it again.
- **`storage`** is `null` without a scratch root (`HPC_STORAGE_ROOT`, section 11), else the provider storage `up` makes
  on scratch, recorded before the first `mkdir`; `down` reads its paths here and nowhere else.
- **Read and changed through one store** (`hpclaunch.StateStore`): `read()` the whole file; `update(fn)` a
  read-modify-write under `hpc-federation.lock` in the root (`flock`), `fn` taking the state and returning the new one,
  written atomically. Two commands never interleave their writes.

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
   rules and mark (as Compose); the two rootfs tarballs and their `.provenance`; the core's self-check; a shared
   imported tree's variables and manifest (section 8); `HPC_STUDY_ARTIFACTS` where the template or a space's
   `data_manifest` names `@study_artifacts@`, and each declared manifest of an ingested space loaded (section 6a); with a
   scratch root (`HPC_STORAGE_ROOT`), that root and the inode guard (section 11). A root that holds a state file is
   refused: `down` first.
2. **The provider storage** (only with a scratch root, section 11): the state file is created, then the federation's
   own directory on scratch and one per provider, each by an exclusive `mkdir`, before any job exists.
   **Read the account's other live federations' nodes** (section 1, "Two federations of one account"), before the state
   file is made: read-only, and a failed read refuses. **Submit** the held job: `sbatch --parsable --job-name=<allocation
   name> --account --partition --no-requeue --time --nodes --ntasks-per-node=1 --cpus-per-task --mem
   [--exclude=<those nodes>] --chdir=/ --output=/dev/null --export=NONE --wrap 'exec sleep <seconds of --time>'`. **The read-back:** `scontrol show job -o <id>` must report what was asked (TimeLimit,
   Requeue=0, Account, Partition, NumNodes, NumCPUs, MinMemoryNode, JobName); otherwise `up` cancels that one job id
   and fails, naming the field. sbatch's stderr is kept apart from the id it prints. With the agent's node under
   `het`, the same sbatch has a second component (section 1), and **each component's record** is read back (one per
   line, by `HetJobOffset`), a mismatch named with its component; under `separate`, a second held job for S is
   submitted, recorded and read back the same way, a mismatch cancelling that job alone (`down` releases the first).
   A job id that already carries another name (Slurm reuses ids) is not the job submitted: the read-back and the wait
   for it to run never cancel it, and `up` fails naming it. A provider named `sandbox` is refused with an agent's node,
   whose role has that name.
3. **Wait for it to run** (each job, under `separate`), bounded (30 min); at the bound, or if it ends first, it is
   cancelled by its id. **The read-back runs again on the running job**: a scheduler that allocated more than the
   pending request showed is caught there, and the job cancelled before any step; so is a component, or a job, on
   another count of nodes than asked, or on a node another holds, and so is a node another live federation of the
   account holds (the second read, section 1): the job is cancelled before any step and the roles named.
4. **Place** (section 1), then one control step per node prints the node's FQDN and its IPv4 address on the
   cluster's network (`nodefacts.py`, Python 3.9-compatible, stdlib only); on S, with `--het-group=1` under `het` and
   S's own job id under `separate`, and on a service node with `--het-group=0` under `het`. Nothing else of `up` runs
   on S. The state records each service's **declared name** beside its node's, and `up` writes **the address map**
   into the root (`secrets/address-map.json`, 0600), before any service is launched ("The registered names", below).
5. **The zone**: a detached service step, `zone.sbatch <bundle> <zone tar> <zone name> <every provider's address>
   --declared=<its declared name> --host=<name>=<address> ...` (one `--host` per service of the federation, itself
   included), run as a step (it holds the service for the step's life, as the standing run did). `up` waits for its log's verdict
   (`hpcop: OK zone`, or `FINISH FAILED` or `hpcop: ERROR`, which fail `up`).
6. **The providers**: one detached service step each, launched `launch_stagger_s` (1 s) apart (below), `provider.sbatch <bundle> <provider tar> <name> <zone's declared
   name> <admin mail> <zone address> <every other provider's address> --declared=<its declared name> --host=...`; for each, a registration token minted in a control
   step on the zone's node piped into a delivery step on the provider's (`mint-registration-token.sh |
   deliver-token.sh`, the bundle's, with `--workdir`); then each log's verdict (`hpcop: OK provider`). A provider that
   owns a shared imported tree is also given `--import=SPACE=PATH`, and once every provider is up a control step on
   its node proves the tree read-only (section 8).
7. **The root's secrets and CA**: the zone's admin passphrase through a control step's stdout into
   `secrets/zone.passphrase` (0600), as the registration tokens travel; the Onedata test CA of the zone's rootfs (public)
   into `ca/federation-ca.pem`, **its `CERTIFICATE` blocks alone** (`hpcca.py`): the image's file holds the CA's
   private key before its certificate (seen live), and a key block of any kind is left out, so the root never holds a
   key; an export with no certificate, or a block that is not whole, fails `up`. The core's refusal of a key in
   `ca_file` stays as the backstop. **The CA stays the image's own**, the same for every federation of one image: its
   digest is in the campaign fingerprint (`PlatformConfig.fingerprint()`), so a CA made per federation would stop
   shards of different federations merging, as a Onezone id did. If one is ever wanted, its digest must leave the
   fingerprint first.
8. **The core's phase** (`hpcboot.core_phase`), on the login node or in a control step on the zone's node, as `[hpc]
   core` says (section 9). **The bootstrap** is the Compose federation's own `fedboot.Bootstrap` over a layout whose
   services' URLs are `https://<declared name>:8443`, its client given the address map: the admin's token, the users, the spaces, the support (on each
   provider's one posix storage, owned by the job's uid, and that storage's LUMA local feed mapping the space and every
   user to that uid, as `onboard.py` does), the members, the baseline content (`fedcontent.Content`). An owner of a
   shared imported space also has its `import-<space>` storage made before any support, and the scan awaited after
   the members (section 8).
9. **`facts.json` and `platform.toml`** (section 6) and the render identity of that platform (`render-identity.json`,
   section 8), then each shared import verified against its manifest (section 8)
   and each baseline space read back against its own (section 6a; an ingested space holds nothing yet, so it is not
   checked here), then **the link check**: `fedlinks.check_after_up`, the platform
   layer's own check through the rendered `platform.toml`, which writes `link-check.json` in the root through
   `expcore_onedata.linkrecord`. `up` fails unless every pair passes.
10. **The fault relay** (M4's `expcore_onedata.fault_relay`), when the core has it: started detached on the login node
    (`env -i PATH HOME LC_ALL [SLURM_CONF] <python> -I -B -m expcore_onedata.fault_relay serve --root <root> --
    <this driver's argv prefix>`, its output appended to `<L>/<allocation name>-fault-relay.log`), in the root as its
    working directory (a 0700 directory of ours that no symbolic link reaches, or the relay is not started: `up`'s
    preflight refuses such a root before anything is submitted), never the directory `up` was launched from, since
    every verb it runs inherits it; then `fault_relay alive` (`-I -B` too) polled until it beats (30 s at
    most; a relay that never beats fails `up` and is ended). It is recorded in the state file as `relay`. One relay
    per root: it takes a lock.

**A launch that failed at launch (O57; live 17 Nov 2026, twice).** Five provider steps launched in the same second
failed together: srun's fan-out on the login node could not find the nodes' addresses, each step was accounted
`CANCELLED` with exit `0:116` after about 12 s, and the services' run logs were empty (the bundle's first write never
happened). `up` could only fail and be torn down, although a minute later every node resolved. Two changes:

- **The providers are launched `launch_stagger_s` apart**: a declared bound of the driver (`hpcdriver.LAUNCH_STAGGER_S`,
  1 s), a pause between two launches, never before the first provider's or after the last. It is recorded in the state
  file as `launch.stagger_s`.
- **A service is launched once more, only when both proofs hold** (`hpclaunchproof.py`; the zone's launch and each
  provider's). The one failure considered is the one `up` always raised: the step ended before its log said what `up`
  waited for. `up` then reads, at that moment, three sources. **(a)** sacct's rows of the step's job (`sacct -j <job> -n
  -P -o JobID,JobName,State,ExitCode`) for the step id that srun's own client log names (`<run log>.client`: `srun:
  error: Task launch for StepId=<id> failed on node <node>: Can't find an address, check slurm.conf`): exactly one row
  of that id, under the step's name, State exactly `CANCELLED` and ExitCode `0:116`, both as the site printed them twice
  (a step a user cancels reads `CANCELLED by <uid>`, exit `0:9`; a job's batch step cancelled with the job reads plain
  `CANCELLED`, exit `0:15`: the state alone is not the proof). The client's log must name that one step and its failure
  must be the missing address, which srun's message reads as a request that was never sent (its message, not something
  seen on the node); a task launch that failed otherwise (a socket that timed out) may have reached the node, and is no
  proof. **(b)** The run log is a
  regular file of zero bytes, by a fresh `lstat` read last, and again immediately before the retry is written. A
  missing log proves nothing. The step's id comes from the client's log, not from the state: `up` waits for the
  providers one after the other, so the steps of the later ones were never listed while it waited; an id the state does
  hold must be the same. A step sacct cannot show (the command fails, times out, prints nothing, or no row is the
  step's) is no proof, and `up` fails with its own error, never sacct's.

  The retry is **one**, counted in the state file (`services.<name>.launch_retries`), never in the process. A second
  failure of the same service, a step still listed when its bound runs out (it may be running), a run log holding a
  single byte, and a log that says it failed all stop `up` exactly as before. A retry is written to the state **before**
  the launch, with its proofs: `launch` (the launch that failed), `step`, `step_id`, `sacct_state`, `sacct_exit`,
  `run_log_bytes`, and `failure`, a class (`task-launch-no-address`) and never the client's line, which names a node. It
  is said while `up` runs and printed in `up`'s output (`launch retried: ...`). The new launch has the next launch
  number, so its step, run log and client log are new files (`...-run-2`); the first launch's client stays recorded and
  `down` ends it. **The retry waits first**: `launch_retry_backoff_s`
  (`hpcdriver.LAUNCH_RETRY_BACKOFF_S`, 60 s; recorded as `launch.retry_backoff_s`), since live the nodes resolved
  again a minute after the failure, and a retry 15 to 30 s after it (three polls with the step gone) would have failed
  for the same reason. **Each service's backoff counts from its own step's End**, read from the same sacct row as the
  proofs (`End` is one of its fields; the row's `step_end` is recorded), and `up` sleeps only until End + the backoff,
  which is nothing when that is already past: the services are awaited one after another, and five providers that
  failed in the same second wait about one backoff in all (58 s of 60, the three polls having used some), not five. A
  service that failed later waits what is left of its own. sacct prints End in the local time of the node that prints
  it, so it is read in this node's local time and **believed only between this process's launch of the step (less
  10 s) and now (plus 10 s)**: an End that is "Unknown", empty, not a time, in another zone or on another clock takes
  the whole backoff from now, never zero. The seconds waited are recorded (`waited_s`). Proof (b) is read again after
  the wait, or when there is none, and immediately before the retry is written and the service launched: a run log
  that gained a byte meanwhile cancels the retry (said, nothing recorded, `up` fails as before).

A failed `up` leaves its state file: what it placed is recorded, and `down` releases it.

**`verify-data`** (login node, after the study's ingest; section 6a) is no part of `up` and touches no job: it reads
the state file, and reads each ingested space back through its owner. It is its own branch of the command line, never
the `else` of `down`.

### The registered names (O73)

A layout declares a name for the zone and for each provider, `<service>.<domain>`. On a cluster those names resolve
nowhere, and the nodes have names and addresses of their own that nothing the agent can read may carry (the zone's
scope answer, the no-connection errors and the registered domain a client dials all carry what a service registered).
So every service **registers under its declared name**, and the login node's clients reach it by that name through a
map. Four parts land together, in this order, since only then does every step have a working check:

1. **The address map.** Once the nodes' facts are read, `up` writes `secrets/address-map.json` (0600; the root's own
   file, never in a cell, the gate's file, `facts.json` or `gate.json`): each declared name with its node's address and
   the node's own names (its FQDN and its first label). A node's name or address belongs to one declared name; a
   service with no address stops `up` before it launches any (`hpcnames.address_map`). `platform.toml` names it
   (`addresses = { file = "<root>/secrets/address-map.json" }`, first, written once the root holds one). The platform
   layer's client, the bootstrap's, the gate and the host mount's oneclient dial the mapped address and keep the
   declared name in the URL, the handshake, the certificate check and `Host`.
2. **The URLs.** Every URL the driver builds or records for a service is `https://<declared name>:8443`
   (`placed_layout`): the layout's name, so `federation.is_declared` matches a URL's host to the domain a service
   reports, and `facts.json`, `platform.toml` and the link check hold the names.
3. **The registration.** `configure.sh` takes `--declared=<name>` and one `--host=<name>=<address>` per service, and
   sets `onezone.domainName`, `oneprovider.domain` and `ONEPANEL_GENERATED_CERT_DOMAIN` from the declared name, while
   the cluster section keeps the node's own names. It writes the work directory's `declared-name` and `hosts`: the
   loopback, the node's own names on its address, every service's declared name with its address. `run_service` binds
   `hosts` read-only over `/etc/hosts` with `--no-mount bind-paths` (the site's bind paths drop out, `/etc/localtime`
   with them, which becomes the image's), so every peer's declared name resolves in the cell; an unresolved peer
   crashes a provider's lookup of it (`inet:getaddrs`). A restart rebinds the file from the work directory.
   `configure.sh` refuses, before it writes anything, a peer address with no declared name, a declared name that is
   no peer, a name or address listed twice, an entry that is not `NAME=ADDRESS`, no entry for its own name and, for a
   provider, none for the zone's; without `--declared` it registers under the node's FQDN as before. The readiness
   calls (`finish.sh`, `finish-zone.sh`), the token mint and the line `zone.sbatch` prints name the registered name,
   pinned to the loopback by `curl --resolve` (the certificate holds it alone). Onepanel's DNS check resolves through
   DNS alone and will report a layout's name `unresolvable`: it is advisory (deployment acknowledges it), no step of
   `up` depends on it, and `facts.json` records `dns_check: {advisory, expected: unresolvable, measured: false}`.
4. **The check and the ordering.** After the bootstrap, `up` reads each service's `/configuration` `domain`
   (`hpcnames.registered_domains`) and fails, naming the role and its declared name, unless it is the declared name;
   `facts.json` records it per service (`registered_domain`). A `[sandbox.federation]` table whose names are not those,
   or declared with no such record, is refused as `platform.toml` is rendered (`hpcnames.check_federation_table`),
   naming the table's own names alone: the cell resolves only the declared names, and the published client dials the
   domain a provider registered, so the table is usable only once the providers register them.
5. **The tool server.** It is given **no `ONEDATA_MCP_NODE_NAMES`**: once the services register the declared names
   there is nothing to map, so onedata-mcp runs unset on both platforms and its text is a Compose federation's (the
   registration is the fix, since raw REST answers reach the agent without passing through it). It still has to reach
   the declared names, which resolve nowhere on a node. It runs as a process of the driver's, from a venv and a
   checkout the driver cannot name, so a cell with the agent's hosts file (a bind of those paths) is no option. A
   Python server is started through the **resolve launcher** (`toolshim/resolve_launch.py`): `up` copies it into the
   root, `platform.toml` names it (`[addresses] resolver`), and `toolserver.server_argv` starts the server as
   `<the declared interpreter> -I -B <launcher> <map> -- -m <module> ...` (every Python of the driver's runs
   isolated, so there is no `PYTHONPATH` and no `sitecustomize`, and nothing is shadowed). The launcher wraps
   `socket.getaddrinfo` so that exactly the names of the private map resolve, to their addresses, and every other host
   as before, never consulting the environment (the map is an argument), then runs the server with `runpy`; the URL,
   the handshake, the certificate check and `Host` stay the declared name. A map it may not use (readable by others, a
   link, garbled) stops the start with exit 78, naming nothing. A `[toolserver]` it cannot start (not a Python
   interpreter, no `-m <module>` or script) is refused when the platform is loaded. It covers Python servers only, and
   `-I` applies to the server too; a C preload would cover any process but is a built file pinned in `site.env`.
   **Fallback, not built:** the same `ONEDATA_MCP_NODE_NAMES` mapping set on both platforms, only if the registration
   cannot land before the pilot's freeze.

What stays as it was: the certificate is the image's generated test certificate for the declared name, signed by the
image's test CA (section 7 of `up`); the cluster's Erlang names are the node's and use no TLS; the port shim moves
443 to 8443 for the listed peers, whose declared names resolve to those addresses.

**`up` and `down` each hold the root's lifecycle lock** (`<root>.lifecycle.lock`, beside the root, `flock`, never
waited for) from start to end, so a `down` never runs between an `up`'s state and its `sbatch` (it would find no job
and remove the root); the service verbs do not take it. **It lies outside the root and is never removed.** On the
site's shared filesystem (NFS) a file held open while it is removed becomes a silly-rename file (`.nfsXXXX`) until its
last close: the first live `down` held the lock inside the root it removed, which was then not empty. A lock file
removed while held would let the next `up` or `down` lock another file of the same name, so it stays.

**`down`** (login node), whatever `up` reached:

0. **Stop the fault relay first** (`<python> -I -B -m expcore_onedata.fault_relay stop --root <root>`, when the core
   has it), so no service verb launches
   a step while the federation goes down; a relay that will not stop is reported, and `down` goes on. Its directory
   `<root>/fault-relay/` goes with the root. The stop signals the relay by the pid and start tick of its heartbeat,
   through a pidfd where the kernel has `pidfd_open` and, on a login node whose kernel has none (Linux before 5.3, which
   answers ENOSYS), by the same start tick read just before an `os.kill`, so a pid another process took is never
   signalled. On either path it signals only a process whose command line holds
   `-m expcore_onedata.fault_relay serve --root <root>` in order, since the heartbeat is a file. A relay whose stop failed is ended as a
   recorded client in step 4, and `down` checks it again only after that: it is reported unverified only if the second
   stop still shows a heartbeat.
1. Read the state file. A recorded job id whose job now carries another name is not ours (Slurm reuses ids): nothing
   is torn down or cancelled. When the id is missing, find the allocation by its name among this user's jobs
   (`squeue -u <user> -h -n <allocation name> -o %i`, an exact name holding `up`'s random part; a heterogeneous job,
   listed once per component as `<leader>+<k>`, counts once, by its leader's id); with no state file and no name,
   there is nothing of `up`'s to find, and `down` says so. **Never `scancel -u`.** Under `separate`, the agent's own job
   is found and checked the same way, on its own: by its recorded id or its own name (`<allocation name>-sandbox`), and
   never touched once its id names another job; a lookup that fails is reported, and the other job is still cancelled.
2. If the job runs: a `teardown.sh` control step per service with a recorded work directory (providers first, then
   the zone), each bounded by `stop_seconds`; then wait until no step but `batch` and `extern` is listed in `squeue -s`
   under the held job's id and, under `het`, its component 1's own id (S's steps, a shard step's among them, are listed
   there alone), one deadline for both (ending each service step's srun client only then); a step still listed at the
   bound is cancelled by its own id. **A cancel's own exit status is no verdict:** on a heterogeneous job `scancel
   <leader>.<n>` printed "Invalid job id specified" and exited 225 while it did end the step (live slot 5); whether the
   step left the list decides. **A cancel on that site was an outright kill** (the step's processes ended within a
   second, no trap ran), which left each such service's work directory (2 to 2.6 GB of the node's tmpfs) on its node:
   once a cancelled step has left the list, `down` removes the work directory the state recorded for that service, and
   only one the bundle makes (`hpcstate.checked_workdir`), by `teardown.sh <kind> --workdir W --timeout 0` in a
   control step on its node, and step 3 shows it gone. Nothing the state did not record is removed: a service whose
   log never named a work directory is left to `leftovers.py`, which only counts.
3. One read-only control step per node checks that no recorded work directory is left on it. A service whose step
   was launched but whose log never named a work directory (seen live: a zone that failed in node-init before making
   one) is clean only when both are shown: its step has ended (listed no more, once seen listed, or cancelled by its
   id in 2, or with its srun client ended), and a read-only step on its node (`bundle/leftovers.py`) counts no work
   directory of ours of its kind in the tmpfs. A step whose end cannot be shown, a log that names a work directory the
   driver refused, a tmpfs that is a link (the bundle then makes its work directories elsewhere, `lib.sh`), or a check
   that fails or prints no count, keeps it unverified.
4. `scancel <job>`, by the recorded id only; wait until `squeue -j` lists it no more (nor any `<job>+<k>`: a
   heterogeneous job's leader's cancel ends every component, S's included). Under `separate`, the agent's own job too,
   by its own id: both are cancelled before either's failure is raised, and the srun clients of both are ended. **Whatever
   fails in 0 to 3 (a relay that will not stop, a failed `squeue`, a work directory the driver refuses), the job is
   still cancelled and every client ended; the failure is reported as unverified.** A cancel that fails is raised once
   every job was tried, the clients left (their steps may still run) and the root kept for a second `down`.
5. **Remove the provider storage on scratch** (section 11), when the state records one: only here, once the job is
   cancelled and gone, every client ended and every service verified clean (a `down` that could not verify one keeps the
   storage, the root and the state), and while the state that names the storage still exists. A removal that fails keeps
   the root and the state, the job cancelled all the same, and a second `down` finishes it.
   Remove the root, as Compose's `down` does, **its marker last**. A file another process holds open becomes a
   silly-rename file, which only its holder's close removes: the removal never removes one, and waits up to 30 s in
   all for them to go. At the bound it fails, saying the job is gone, with the marker kept, so a second `down` finishes
   the removal. A root a removal left unmarked (empty, or of nothing but silly files, with its lifecycle lock beside
   it) is finished by a second `down` too, by `rmdir` alone; any other unmarked root is refused. The service logs stay
   in the log directory (no secret: the bundle redacts them), named in the run's output.

**Before `down`: the copy back, a required step of the operator's, not of `down`'s.** Nothing durable may live only on scratch: a site's scratch policy can remove files 30 days after creation, a job directory 7 days after its job and all of scratch after 20 days with no job submitted. The cluster cannot push to the operator's host, so after `shards.py collect` (run once every step has ended) the operator pulls the results directory (rsync or scp, from their host) and runs `shards.py verify-copy MANIFEST LOCAL_DIR --manifest-sha256 HASH` there. **What the required pull covers is what the manifest lists,** per shard: the archive `<label>.out.tar`; its manifest `<label>.out.json`; every file under `<label>/journals/` (a spend journal per step, which holds what a step killed outright spent and which `--resume` counts as `lost_in_kill`, so it is held nowhere else); and the newest snapshot that verifies, only when it is newer than the archive. **Why snapshots are not otherwise required:** a snapshot is a copy of `out/` taken while its step ran, and it names the archive its step started from (`_run_step`: `facts = {"shard": spec, "base": base}`). The snapshots of the step that wrote an archive name an earlier archive or none, and the last of them was taken after the dispatcher exited and before the archive, so the verified archive holds all they do; `--resume` agrees (`restore_newest` returns none for a snapshot whose `base` is not the archive's SHA-256, the archive being newer). **The exception:** a step resumed from the archive and killed before it archived leaves snapshots that name that archive, so the newest holds the runs that step reached, which the archive lacks and the merge will not have; `collect` picks it as `--resume` would (`restore_newest` with the verified archive as the base, restored into a private directory that is removed), warns, and lists every file of it. Plain `collect` does not refuse that state (the merge it prints is of the archive, and the operator may mean to run it): the warning and the manifest keep the snapshot from being lost with the scratch. `collect --run`, which runs the merge at once, refuses in that state (exit 2, naming the shard and the snapshot: resume the shard so its runs reach a new archive, or run `collect` without `--run` and merge the line it prints yourself), because the merge has no flag to leave a shard short and would analyse an incomplete campaign. `collect` writes `archives.sha256.json` beside the archives: the plan's id, then each archive's, and each other file's, relative name, size and SHA-256, read by `collect` (an archive from its private verified copy, never from a step's claim; a link anywhere among the journals or the snapshot refuses the collect), sorted, with no path or timestamp, and prints the file's own hash on stderr; the hash must come from that output, never from the pulled files, or a manifest damaged or swapped in the pull would vouch for itself. `verify-copy` is standard library only and is dispatched before the plan is loaded, so it runs on a host without the core (a test runs it under `python -I -S`); it checks each file as a regular file under plain directories (a link on the way is not followed) and refuses a manifest with no list of the files beside the archives, which an earlier tip wrote. `down` does not see the operator's host and does not check that this was done: the order is the operator's rule, and the README states it as a required step.


**`down` and the root's evidence (O63).** `down` removes the root, which holds the files the study's post-check and the facts read, so it does not remove one whose evidence was not collected. The evidence is an allow-list (`hpcevidence.py`): the state, `platform.toml`, `facts.json`, `link-check.json`, `render-identity.json`, `day0-wiring.toml` where present, `shard-claim.json` and `shard-claim.lock` where present (the study's pre-step guard), `data-manifests/*.json` (the manifests each space's data identity in `platform.toml` is the hash of), `shard-steps/*.json`, the whole of `fault-journal/` (its journals, `link-epoch.json`, the holder, drain and intent records and the lock files the pilot's heal flocks: read as they are, never locked) and the fault relay's `late-steps.jsonl` and its request, taken and answer files. Never in it: `secrets/` (the zone's passphrase, the CA key, the admin credential, the users' passwords), any file that holds a token, key or password whatever its name, the relay's heartbeat and lock, the CA, the shim, a provider's data. The state is the one file of the list that holds site values (account, partition, paths, the administrator's mail address) and no credential: the pulled archive is private like the journals. Each file is opened below a descriptor of the root with no link followed and read once, and the tar is built from those bytes; a file that is a link, or that holds the content of a file of `secrets/`, refuses the collect naming it. `collect` publishes `<label>.root.tar` and `<label>.root.json` beside the shard archives and lists both in `archives.sha256.json`, so `verify-copy` checks them. `down` cannot see the results directory: `shards.py step` leaves `shard-steps/<label>.json` in its root (a step ran), and `collect` writes `evidence-collected.json` there once its manifest is written (the root archive's hash and the digest of the step records it read), so `down`, which reads both before it stops anything (`Down.evidence_gate`), refuses a root with records and no current marker, and one whose evidence no longer archives to the hash the marker names (a later step, a step still writing the journals when `collect` ran, a changed file: it archives the root again to compare; a `down` that began is not gated again); `--discard-evidence` removes the root without it, the output says so, and the use is recorded in the state file (`evidence_discarded`: time, shards, reason from `--discard-reason`; it goes with the root, so down's output is what outlives it). The archive is named by the shard's label and its manifest records a hash of the root's path (`root_id`, no path): a shard a fleet moves to another federation gets a root archive made of the second, and the first's is put aside as `<label>.<12 hex digits>.root.*` (never overwritten) and listed with the rest; `collect --roots-only [--shard L]` archives and marks the roots alone, for the shard about to move while the others are not final. The root archive, like the shard's `pass-env.json` (listed in the evidence when present), is private: `package.spec.never_selected` keeps both out of any `expcore package`, wherever they lie. A root with no record goes down as before. `collect` itself is atomic: everything is staged in a private directory beside `--into` and the manifest is the last write to the results directory and `--into`.

A job already ended (its `--time`, a failure) skips 2 and 3: Slurm stopped its steps, the bundle's cleanup removed
what its time allowed, and the next job of ours on a node sweeps what is left (`sweep_leftovers`). `down` says which
nodes it could not check.

## 6. `platform.toml` for an HPC federation

**Rendered by `up` into the root from the layout's own `platform_template`, by the Compose renderer
(`fedfacts.write_facts`), with the HPC facts:** `@zone_url@` and `@provider_url.<name>@` are `https://<declared
name>:8443` (O73; the address map's file is named by an `addresses` line the driver puts first); the ids are this
`up`'s. Every path in the template stays relative to the root, so the same template
serves both:

- `tls.ca_file = "ca/federation-ca.pem"` holds the **Onedata test CA** here (each service's web certificate is the
  image's generated test certificate for its node's FQDN; the bundle README lists what that CA's public key means);
- `credentials.admin` is `secrets/admin.token`, minted by the bootstrap; `credentials.users` the users' passwords;
- **storages are found by name**, never by place in a list: finish.sh's `posix-local` for a provider's support, and
  `import-<space>` for an owner's support of an imported space (M3: such an owner has two); the LUMA mappings are
  made on the storage the support names;
- `[faults] journal = "<root>/fault-journal"` in full (`@hpc_root@/fault-journal` in the template), when the template
  declares faults, so the link-check record lands in the root (`linkrecord.record_path`) and a copy of the file beside a
  study names the root's journal (`load_faults` refuses a relative one in a study directory);
- `[faults]` for the HPC backend (M4's loader): `backend = "hpc"`, `stop_bound_s`, `journal`, `[faults.services]`
  naming every provider, and none of `compose_file`, `project`, `command` or `docker`, which that backend refuses;
  `step_launch_wait_s` (from the layout's `[hpc]`, 240) and `command_timeout_s` (`step_launch_wait_s + 60`, 300: the
  floor of every relay verb's own bound; a Compose table's is 120), both declared by `up`; its defaults are
  `settle_timeout_s = stop_bound_s + step_launch_wait_s + 70`, `poll_s` 10 and `relay_wait_s` 30;
- **`@study_artifacts@`**, from `site.env`'s `HPC_STUDY_ARTIFACTS` (section 6a): the study's own directory on the nodes,
  so a template names the paths of its tool server's venv, clone and manifests under it and a public replication package
  carries no site path. A template that names it without the key is refused at the preflight, naming the variable; an
  unset key is no value, so the renderer would refuse the placeholder too, never write an empty path;
- the values only the HPC driver fills, for a template that names them: **`@hpc_stop_bound_s@`**, `stop_seconds + 90`
  (140 by default, section 7e); **`@hpc_step_launch_wait_s@`**, `[hpc] step_launch_wait_s` (240 by
  default); **`@hpc_command_timeout_s@`**, that and 60 (300); **`@hpc_settle_timeout_s@`**, `stop_bound_s +
  step_launch_wait_s + 70` (450); **`@hpc_command@`**, this
  driver's argv prefix as a TOML array; and **`@hpc_token_margin_s@`**, the least `[tokens] margin_s` the platform
  layer's own loader accepts for the rest of the file: `up` writes the file with a provisional margin no rule refuses,
  reads it with `load_faults` as a campaign would, and writes it again with the least that loader's rule gives (5,370 s
  with M4's defaults), so a measured `stop_seconds`, or a term the loader adds, moves it. A core whose loader cannot
  read the HPC `[faults]` (one without M4's backend) gets the restore's rule over the keys it knows, with the HPC
  settle time and poll;
- **`[placement]`, always and complete** (O79; `hpcplacement.py`): the platform layer requires the table
  (`rangemap.load_placement`), and a reader that found the render without one patched in a default of its own;
  defaults supplied separately by each tool are how a setting drifts unseen. The core has no default for `holder`
  (the file-wide data provider; every `[[spaces]]` entry names its own, which the provisioner uses), so the template
  declares it, a provider the file declares, with a comment saying why, and `up` refuses a template that declares no
  holder (or a holder that is no provider of the file), or declares the table in a spelling the render cannot add to,
  naming the key and never a value, in its preflight (which runs the render's own completion over the template), before
  anything is submitted. The render, in both of its passes, keeps what the template declares and writes every other
  setting at the core's default, read from the fields of `PlacementSettings` (the one place the numbers are, so a field
  added there is in the next render with no change here), each with its own TOML type. The missing settings are added
  where the template declares the table (inside one `placement = { ... }` line of the root, or after one
  `[placement]` header: TOML cannot add to an inline table from elsewhere, nor reopen a table, so any other spelling is
  refused) and the result is read back as TOML, which must be the declared document plus exactly the added settings or
  the render fails and writes nothing. No tool adds a table to the render or reads it with a default of its own:
  `shards.py step` copies the file as it is, and `load_placement` still refuses a file without the table.

**Slots and a tool server (O83).** The core starts a tool server only for a slot's user, and no federation provisioned one,
so the tool-server arm could not run here. The users are ordinary principals of the layout and a platform template
declares the slots and the server (`layouts/pilot-shape-slots.platform.toml.template`), so the HPC driver adds nothing to
provisioning: section 5's bootstrap makes the zone user, its 0600 password file and its memberships, and
`HpcBootstrap.support` maps every principal to the job's uid. The shared bootstrap treats a slot user that exists as
read-only (`fedboot.audit_slot_users`: verified, recorded `matched`, or refused naming the user and the attribute), the
slot users being those the template declares, read as it stands (`fedtools.slot_users`). `@user_id.<name>@` is the facts' id
for every principal; `@study_artifacts@` is `HPC_STUDY_ARTIFACTS` (`Site.study_artifacts`, recorded in the state file for a
hand-run `core` step; an unset key is no value, so a placeholder is never written empty). `preflight` refuses a site
without it when the template names it, and artifacts the server cannot run from, before a job is held
(`fedtools.check_artifacts`); `write_facts` loads the written file as a campaign does and checks its slots against the
layout (`fedtools.check_rendered`), so a federation is not ready on a file the core refuses or one whose rights are not
what was provisioned. A template with no `[slots]` takes none of these paths.

The core's platform layer (gate, health, link check, faults) points at `<root>/platform.toml` wherever it runs,
provided it reaches the services' 8443 and reads the root. A layout's link check covers the pairs its
`platform.toml` declares (`linksettings.link_pairs`): `layouts/pilot-shape.toml` beside this file has the pilot's shape
(a zone, five providers, four data spaces each owned by one provider and mirrored by another, and the probe space on
all five) with invented names, and its template declares the four spaces as `[[spaces]]`, which gives the ten ordered
pairs the pilot's check reports. Its `delta-space` stands for the pilot's largest space (15,340 files); `up` writes
none into it. `layouts/pilot-shape-import.toml` is the same shape with the four data spaces imported from trees on the
shared filesystem (section 8), whose template adds `import = { readonly = true, shared = true }` to each of them and
leaves the ten pairs as they were.

## 6a. The data identity of each space (C06, O63, O57)

A space is the same experimental condition across federations when its declared identity and its data identity match,
and the campaign fingerprint holds a space's data identity only when its `platform.toml` entry declares it
(`data_manifest`, the manifest's content hash, never its path): otherwise it holds the space's Onezone id, which every
`up` makes anew. `up` renders `@data_manifest.<space>@` for each space, and shows the space holds exactly it:

| The space | Its manifest | When its data is shown to be it |
|---|---|---|
| a shared import (section 8) | the layout's own (`import.manifest_env`) | at `up`, `fedimport.verify_imports` |
| a baseline space | made by `up` from the baseline plan, into `<root>/data-manifests/` | at `up`, read back through its owner (every directory, every file's size and bytes) |
| an **ingested** space | the one it declares: `data_manifest` on the space, a path under `@study_artifacts@` or absolute (the empty manifest when it declares none) | **after the ingest**, `hpcfed.py verify-data` |

An **ingested space** is `baseline = false`, no `import`, with its `@data_manifest.<space>@` named by the template (the
link check's probe space is `baseline = false` too, and nothing names it). Its baseline is empty, so a manifest made from
that, as for every `baseline = false` space before, fingerprinted two federations that ingested different bytes alike.
The manifest is **in the core's import-manifest format** (`expcore_onedata.importmanifest`): `{"version": 1, "files":
[{"path", "size", "sha256"}, ...], "directories": [...]}`, both lists sorted by path, each once, every directory a file
lies in listed; `manifest_of_tree` makes one from a local copy of the data, `load_manifest` checks one.
`Manifest.digest()`, the SHA-256 of `json.dumps(manifest.to_json(), sort_keys=True)`, is the hash the fingerprint
holds, so the directories count: the ingest must create exactly the listed ones.

The layout (`deploy/compose-federation/fedspec.py`, read by this driver only) refuses `data_manifest` on a baseline space
or an import, and a path that is neither absolute nor under `@study_artifacts@`, of plain names with no `..` (it is
written into a quoted TOML string). `up` loads each declared manifest in the preflight, so a mistake fails before a job
is held, naming the space and never the path. **The directory must be on a filesystem both the login node and the
agent's node see**: `up`, `verify-data` and `plan` read the manifest on the login node, and the dispatcher's fingerprint
reads it on the agent's node. `up` also refuses a rendered `platform.toml` whose entry for a space names another
`data_manifest` than the one it rendered (a template that hard-codes a path of its own), or declares one for a space the
study fills without taking it from `@data_manifest.<space>@` (even the root's own file): only a manifest `up` renders
is read back or verified.

**`verify-data`** (`Driver.verify_data`, `hpcboot.verify_ingested`): refused unless the state's phase is `linked`, the
layout's ingested spaces are the ones the state recorded, and each manifest's digest is the one `up` recorded. Each space
is read back through its owner (`hpcdata.content_reader`: every directory walked, every file's bytes read and hashed; the
space ids come from `facts.json`, as the process is a fresh one) and compared with the manifest by `hpcdata.differences`,
which counts, in counts alone (never a path): files missing, files not in the manifest, files of another size or other
bytes, directories not as listed. On any difference it exits 1 naming the space and the counts, and, for a space that
declares none, says where to declare it. Every earlier marker is cleared before anything is read, so a failure, or a
read that dies half way, leaves none. On a pass it records the marker in
`data.ingested.<space>.verified` (section 4).

**`shards.py plan` and `lines`** (`federation()`, so a shard is never planned past it) refuse a federation whose
ingested spaces lack a marker bound to the manifest `up` recorded, and to the manifest the platform file names now (a
file edited after the verification changes the hash the fingerprint carries, which the pass was not for), naming the
shard and the spaces. A later ingest needs another `verify-data`: nothing watches the space after the pass. Reading
every file's bytes through the owner holds each whole in memory (16 workers): a very large ingested file is the limit. `verify-data` runs on the login node, as `[hpc] core = "login"`
runs the core's phase: it reaches the services' 8443 from there, and has no `core = "step"` form yet.

## 7. Service verbs for faults and the heal (M4)

Per service, by its layout name. **Each verb is one argv**, run on the login node (only there can a step be
launched): `<python> -I -B deploy/hpc-federation/hpcfed.py --layout PATH --root PATH <verb> <service> [-- argv...]`; the
site's values come from the state file, so the verb needs no environment. A campaign that runs on a compute node
cannot launch a step: its fault injector asks the login node's **fault relay** (M4's, which `up` starts with this
argv prefix and `down` stops first; section 5), which runs the verb and answers.

**How the answer is awaited.** The request and the answer are files under `<root>/fault-relay/` on the shared
file system, and a compute node's NFS client keeps a negative entry for a name it looked up while the file was
missing, failing every later lookup of it until the directory's attributes expire (30 s by default). The client
therefore never looks the answer's path up while waiting: on every poll (0.5 s) it lists `answers/` with
`os.scandir`, whose `opendir` revalidates the directory, and reads `<id>.json` only once the listing shows that
exact name. The file system then adds at most one poll to a relayed verb's time, which is the verb's own plus
the wait for any request issued before it (the relay runs one at a time, in order); polling the path had cost
about 30 s on every verb, measured live. The relay lists `requests/` the same way; a login-node wait (`up`'s for the
heartbeat, `stop`'s for it to go) runs on the host that writes the file and is no such poll.

**A verb that outlives the relay's bound (O58).** A verb is a process that launches an `srun` step and waits for it, and
the controller can stall silently for its message timeout (about 200 s on the site) before it makes the step. Ended
alone at the bound, the verb left its `srun` orphaned: when the controller caught up the step was made and launched,
so the verb ran after the relay had answered `timed_out`, and its own cancel path (`hpcslurm.Slurm._finish`) never
ran. A late stop could land inside a restore, a late `restart_link` inside a heal's settle, up to about twice the
message timeout after the request. The relay now does three things (`fault_relay_group`, `fault_relay_late`, `hpclate.py`):

- **Each verb runs in a session and process group of its own, and at the bound the whole group is ended:** SIGTERM, a
  grace of 5 s, then SIGKILL, with the leader reaped only afterwards so the group id is its own. No `srun` client of the
  verb survives it. The pipes are closed on our side, so a descendant that left the group cannot hold the relay. **A
  verb that exits before its bound with a child still in its group** (hpcfed failing while it ends its own step, say)
  has that child ended the same way (only while a live member holds the group), and the relay treats it as a timed-out
  verb for the watch below; its answer is the verb's own exit status.
- **The relay stamps the verb's steps.** It derives a tag of 12 hex digits from its own nonce and the request's id and
  runs `hpcfed.py ... --step-tag <tag> <verb> <service>`; every control step the verb launches is then named
  `<allocation>-<service>-<word>-<tag>` (`word` of `hpcfed_control.CONTROL_WORDS`: `status` and `mounts` for a status,
  `stop`, `status` for a start, `exec`), in place of the clock's millisecond. The name is the request's alone: no other
  request, and no verb run by hand, can share it.
- **After a timed-out verb, and after any verb that ran 25 s or more, the relay starts a watch.** (hpcfed ends one of
  its own `srun` clients only after its step's own bound, 30 s at the least, and the controller may still hold that
  step, which no group kill reaches: the verb has exited by itself. A verb quicker than that cannot have left one
  pending; a test keeps the threshold below every bound `hpcfed_control` ends a control step at. A verb that ended
  unknown, or left a child in its group, is watched too.) The watch (`hpcfed.py late-steps <service> --for-verb <verb> --request <id>
  --window-s 400 --poll-s 5`; a command of its own, outside `fault_hpc.VERBS`, which no request can name; it writes
  the relay's journal in the root it was given, and nowhere else). The watch is a detached process, so it outlives a relay
  that stops or dies. For 400 s (twice the controller's message timeout, from the bound) it lists the steps of the
  federation's own job (`hpclaunch.listings`) every 5 s and cancels any whose name is one of the verb's exact names,
  by its exact `<job>.<step>` id through `Launcher.cancel`, which refuses an id of another job of the federation's;
  never by user, name or pattern. At the window's end it reads the accounting (`sacct`) for the same names, to record a
  step that was made and ended between two looks (`accounted_late`) when its accounted Start is not before the moment
  the relay answered (`--since`; a step with no accounted Start is counted, since it may have run late), and a step that
  began before it as `accounted_in_time`, which is no late step: a verb watched for having run long (a stop of a minute)
  has its own steps in the accounting. Nothing is cancelled that is over.

The answer is still `timed_out` and the injector still reads the verb's outcome as unknown: a stop is waited out and a
start verified by a changed identity, as before. **The record** is `<root>/fault-relay/late-steps.jsonl` (0600, one JSON
line per event, fsynced): `watch_opened` (written before the watch starts, so a relay that dies in the window still
leaves the record that a watch was owed, with `why`: `timed_out`, `unknown`, `left_behind` or `own_bound`), `watch_failed`, `late_step` (the step's name, its id, whether the cancel
worked and, if not, the error's type; a failed cancel is tried again at the next look), `accounted_late`, `accounted_in_time`, `watch_ended`
(a watch that exited nonzero) and `watch_closed` with an outcome: `late_steps`, `none_seen` (only when at least one look
and the accounting were read), `unreadable` (a controller that never answered in the window says nothing about
whether a step came), `job_gone` or `state_gone`. **A `watch_opened` with no `watch_closed` is a window nobody
watched to its end.** The cancel is best-effort (a step may have run before it was found, and Slurm gives a cancelled
step its KillWait, 182 s here, to end); the record is the guarantee. **Not covered, on purpose:** a start's service step
(`<allocation>-<service>-run-<n>`) is launched detached, in a session of its own, and recorded in the state file by the
verb; it is the start arriving, which a restore wants, and is never cancelled by name (a later stop or `down` ends
it). A look that fails half way (the job check, or the agent's job's listing) keeps the step it found and cancels it, and
a cancel refused as another job's id is said once, never at every look. **A timed-out start's late service step is
the one thing the watch leaves running:** the federation window the campaign keeps (the study's `fedwatch`) reads a
provider that restarted during a later run's outage as an unexplained restart, a finding that stops the batch. That
fails closed, never unseen; the start's `watch_opened` line (its tag, its window, `why: timed_out`) is what attributes
such a restart to the request afterwards. **Not covered:** a relay that dies mid-verb: no `watch_opened` was written,
and the verb, in its own session, runs to hpcfed's own bounds.

**Where the time went (O58).** After each verb the relay reads the accounting once (`sacct -j <job>`, bounded to
10 s and to what is left of the client's wait, never polled) and puts in the answer's `timing`, for each of the verb's
steps found by its exact name (the control words stamped with the tag, and for a start that launched, the service step
the state file records for the new launch), two figures: `start_after_issue_s`, the step's accounted Start minus the
moment the relay issued the verb, and `end_after_answer_s`, its accounted End minus the moment the answer was ready
(negative when the step ended first, the usual case). A step the controller was slow to make has a large first figure,
one that ran past the answer a positive second: the stall is the site's, and the record says so. sacct prints whole
seconds in the login node's local time, so the figures are good to about a second (`resolution_s`). A figure that cannot
be given is null, with the reason (`not_recorded`, `no_such_step`, `unparsable`), and a reading that cannot be made is
`accounting`: `unreadable` (`sacct_failed`, `sacct_timeout`, `sacct_not_run`) or `skipped` (`no_job`,
`no_time_left`); it never fails a verb and never changes its answer. The client puts it on the `CommandResult`
(`timing`, never compared) and on the `timed_out` exception; `HpcDriver.last_timing` keeps the latest of each kind of
request, checked and with no names, and the injector journals it where the verb's own evidence goes: a stop's with the
stop's (`stop.timing`, in `injected` and in `stop_unknown`, where there is no other evidence of the stop), a start's in
the restore (`start_timing`, in the `verified` step and the injection, served or not), a link restart's in the heal's
action (`timing`). A status's and a ping's are kept by the driver and journalled by no line. **Reading a figure:** `start_after_issue_s`
counts from the verb's issue, so for a verb's later step (a status's read-only check, a start's service step after its
status read) it includes what the verb did before it launched that step; the journalled timing keeps `issued_wall` and
`answered_wall`, which let a reader take that out. The login node's clock and Slurm's are read as the same, and sacct's
local time is ambiguous in the hour a clock goes back.

**The hook** (stable; `hpcfed_control.py` is M4's, `hpcfed.py` M1's): after parsing `--layout`, `--root` and the verb,
`hpcfed.py` calls `hpcfed_control.run(verb, service, argv, *, launcher, state, stop_seconds, mounts)` (and
`step_tag=<tag>` when the relay gave one) for a verb in
`hpcfed_control.VERBS = ("stop", "start", "status", "exec")` and exits with its status; `argv` is what follows `--`
(for `exec`), else `[]`; `stop_seconds` is the layout's; `mounts` is the read-only proof of a shared import's owner
(`hpcfed.mount_check`: None in a core without `hpcimport`, section 8). What it is given:

- `launcher` (`hpclaunch.Launcher`): `control(node, argv, *, name, timeout_s)` runs a bounded control step (`--exact
  -c <control cpus> --mem <control memory>`, the allowlist, stdin `/dev/null`) and returns `returncode`, `stdout` and
  `stderr`, or raises `TimeoutError` at its bound after cancelling that step by its id (`returncode` is None when
  its client was ended after its step vanished and the accounting holds no status for it, and a step a signal ended
  reads as `128 + signal`, never 0); `service(node, argv, *, kind,
  name, log)` launches a detached service step of the kind's size (`"oz"` or `"op"`) and records its srun client;
  `listed(name)` is the step id `squeue -s` lists under that name, or None; `cancel(step_id)` cancels that one step of
  the held job. Every step runs on one of the held job's own nodes, or is refused.
- `state` (`hpclaunch.StateStore`): `read()` and `update(fn)` (section 4).

The verbs read a work directory only as `hpcstate.checked_workdir` accepts it, name every step as the launcher's
`STEP_NAME` allows, and read a `returncode` of None (an end not known) as a failure, never a success. A stop or a start
holds `hpc-federation.verbs.lock` in the root, never waiting for it, from the state read that decides it to its last
record, so two never interleave; status and exec take no lock. A verb acts on a service of kind `oz` or `op` alone:
the agent's node (`services.sandbox`, kind `sandbox`, and the state's `sandbox` record, section 4) is refused, exit 2,
with no step run, and the fault relay lists the providers alone.

**(a) STOP** `hpcfed.py stop <service>`: a control step on the service's node runs `teardown.sh <oz|op> --workdir W
--keep --timeout <stop_seconds>`: `stop-requested` is written with `keep` in it, SIGTERM goes to the recorded service
process (`service.pid`: PID and start tick, through a pidfd, as today), and the service step's script, seeing `keep`,
**removes nothing**: not the work directory, the secrets directory, the Couchbase data, the registration, the storage.
Its step then ends, which frees the service's CPUs and memory; the node stays in the job. `hpcfed.py` waits for the
step to leave `squeue -s`, records `stopped`, and returns. On a stopped service it returns at once (idempotent). The
stop is confirmed only once teardown exited 0 and the step is gone (for a step never seen listed, once its srun
client has ended too: a pending step is not in the list and could still be made), and its `stopped` is recorded only
for the launch it stopped. `--keep-until` is the allocation's end: Slurm's `EndTime` as `up` recorded it, in the login
node's local time, as `scontrol` printed it. **Every stop's evidence** is printed and appended to `stops` in the
state, confirmed or not: `stopped`, `ended` (`exit`, `kill`, `none`), `seconds` (from the request to the verb's
verdict) and `service_seconds` (the service's own end after its SIGTERM, as teardown measured it on the node); the
core journals it (section (e)).

**(b) START** `hpcfed.py start <service>`: a new detached service step, the same size, on the same node, running
`restart.sh <oz|op> --workdir W`: no `node-init.sh`, no `configure.sh`, no token wait; `run_service` again on the same
rootfs, so the same config, cookie, epmd port, ports, address and registration. `launch` is incremented. A start of a
running service does nothing. A start follows a recorded `stopped`, and also `stopping`, `starting` or `running` once
`status.sh` finds the process not alive and its step is not listed (a stop whose verdict never reached the state, its
verb killed at the relay's bound; a start whose step ended), so a restore is never refused for a provider that is
down; it never launches beside a live process, nor while an earlier start's step was never listed and its srun client
still runs. **A restarted owner of shared imported trees (O59)** has `mounts_proven` cleared in the state change that
claims its launch, and is `ready` only once a status has run `hpcimport.mount_check_argv` on its node and read the
report against the spaces it owns: the start returns once its step is listed, before the service holds its mounts, so
the proof is taken by the first status that finds it otherwise ready, and recorded for that launch alone.

**A restart's identity check waits for the domain (O58).** `finish.sh --restart` compares the provider id and the domain that op_worker's `/configuration` reports with `identity.json`, before anything is rotated. op_worker answers `domain` from the provider record it reads over its Onezone connection, so for a moment after its REST listener answers it says null (live: a restart's check ran 1.9 s before the connection came up, and compared the word null with the recorded domain of a service that had not changed). `finish.sh` therefore polls `/configuration` first (`wait_domain` in `lib.sh`, every `HPCOP_POLL_S`, 1 s) until `domain` is neither null nor empty, under **one deadline with `wait_ready`: 1800 s from `finish.sh`'s start**, `wait_ready`'s own bound (`HPCOP_READY_SECONDS`, validated, for a test to shorten), so the wait takes what `wait_ready` left; there is always one read, and the deadline is checked between requests, so the wait can end one request (`http`'s 120 s bound) past it. The first non-empty answer ends the wait whatever it holds and the check runs unchanged: another non-null domain fails as `the service's identity changed across the restart`; a domain still null at the deadline fails as `stayed null`, so a start that lacks Onezone is not read as a changed deployment; an answer with no `domain` field, or one that is not JSON, fails at once, and so does a service that exited meanwhile. A first start waits the same way and records the identity only from a real domain. How long a start waited is kept in `domain-wait` and reported as `domain_wait_s` (section (c)). `jsonget` never prints a JSON null as a word: with no `--default` it prints nothing and exits 3 (2 is a field that is not there), so no script compares or records "null" as an id, a domain, a user id or a token.

**(c) STATUS** `hpcfed.py status <service>`: one JSON line, no secret: `{"service", "state", "running", "ready",
"identity", "launch", "node", "step"[, "domain_wait_s"]}`. `running` is true only when the service step is listed in `squeue -s` **and**
a control step on the node (`status.sh`) finds the recorded service process alive with its recorded start tick.
**`ready`** is true only once the service's own checks passed for this launch: a marker whose content is the current
`service.pid` line, written by `zone.sbatch` and `provider.sbatch` after `finish*.sh` and by `restart.sh` after its
checks; running but not ready is a Compose container not yet healthy, so a restore is never verified before a
restart's guard, rotation and live checks pass. **A ready launch's status also carries `domain_wait_s` (O58)**: the seconds its start waited for op_worker's
domain (`finish.sh` writes `domain-wait` in the work directory, 0.000 when the first read held it, and `restart.sh`
removes an earlier launch's first; `status.sh` prints it only for a ready launch and only in the form `finish.sh` writes;
the status verb passes it on only for a ready launch and only as a finite number of at least 0). The injector records it
as `domain_wait_s` beside `ready_after_s` in the restore: the part of the provider's down time spent waiting for its
Onezone connection, which a live slot measures. Absent when nothing reported it, never 0. **`identity` is `"<launch>:<pid>:<start tick>"`**, from
`service.pid`, which `run_service` rewrites on every start: stable while the service is untouched, different after
every start.

**(d) EXEC** `hpcfed.py exec <service> -- <argv...>`: a control step on the service's node runs `exec.sh <oz|op>
--workdir W -- <argv>`, which runs `apptainer exec` with `run_service`'s flags (`--containall --no-home --pwd /root`,
a per-call `--workdir` under W) on the same rootfs. The new container has its own PID namespace but the node's network,
so `/etc/hpcop-erlang-env` gives it the job's epmd port and `/root/.erlang.cookie` the cookie, and Erlang's
distribution reaches the running node over loopback (same uid, so the shim's owner check passes). Output is captured;
the exit status is the command's.

**(e) The stop's bound:** at most `stop_seconds` (default 50) for the graceful stop, then supervise.py's SIGKILL tail
(30 s), then the step's end (polled, 60 s at most): **`stop_seconds + 90` s** in all (140 by default), the service's own
stop. **The 50 s is measured:** three provider outages on one HPC federation (17 Nov) ended a service 26.1 s, 27.1 s
and 33.1 s after its SIGTERM, and the coordinator's rule is the largest plus 50 % (33.1 x 1.5 = 49.7, so 50); the
bundle's own default is 600.

**A control step's bound is its launch wait, then its work (`[hpc] step_launch_wait_s`, 240 s).** A step's client is
waited for from its start, so a bound on it holds whatever the step waits to start as well as its work. A step's
creation can wait on the scheduler's controller up to its message timeout (measured 47 to 103 s live, up to 200 s
possible on the site, with no error), so 240 s is that 200 s plus 20 %. In the outages the relay's verb steps started
47 to 103 s after they were issued during a heal, and 103 s plus a 33 s stop (136 s) is past a stop step bounded at
`stop_seconds` + 60 s (110 s): a stop that times out so is unconfirmed, and fails an outage run. The two things are
therefore apart: `stop_seconds` bounds the service's stop, which was measured, and `step_launch_wait_s` the wait to
start, added to **every bound that waits on a step's client from its start**: the stop's control step
(`stop_seconds` + launch wait + 60 s, 350 s), `down`'s teardown step (launch wait + `stop_seconds` + the 30 s kill +
90 s, 410 s), the status step (launch wait + 60 s), the exec step (launch wait + 300 s), the mount check (launch wait
+ 30 s), a start's wait to see its new service step listed (launch wait + 60 s), and every relay verb's bound. The
setting is rendered into `[faults]` (`step_launch_wait_s`), where it is in the faults fingerprint with the other
bounds. **It widens timeouts only:** how long the harness waits before it fails closed. It never lengthens an outage
or a hold, and a step that starts at once costs nothing. A step's other bounds (`up`'s control steps at 600 s, `down`'s
node checks) already exceed it. The held job's `time` must exceed twice the stop verb's whole bound (launch wait + stop
bound, 380 s).

**Each relay verb has a bound of its own,** the longest the verb can run and 30 s, never below the declared floor
`command_timeout_s` (launch wait + 60 s, 300 s, declared in `platform.toml` beside the launch wait). The longest a verb
runs is its steps, each at its whole bound with the launch wait in each, and its waits at their whole wait
(`fault_hpc.verb_inner_worst_s`, which a test runs against this module's own verbs): a **status** is its step and, for
a restarted shared-import owner, the mount check after it (240 + 60 + 240 + 30 = 570 s, so 600 s); a **start** after an
unrecorded stop is a status step and then the wait to see its new service step listed (300 + 300 = 600 s, so 630 s); a
**stop** is its control step (350 s), the wait for the service step to leave the list (60 s) and after a cancel (10 s),
420 s, so 450 s (`settle_timeout_s`'s default is that, and it is the stop's floor); a **ping** and a **link restart**
are the exec step, bounded alike for every command (240 + 300 = 540 s, so 570 s). One bound of 300 s was below the
first, the second and the fourth, and a relay that cuts a verb short times out one about to complete (and, with the
orphan fix, kills it): a restore after a stop of unknown outcome would then fail when it matters. The bounds are
recorded in the faults fingerprint, and the heal's reserves, the restore's bound, the token margin (5,370 s with the
defaults) and the platform's deferral are counted from them (a `max_defer_s` check sees the true figure).

For backend `"hpc"` (M4's loader): the core bounds `hpcfed.py stop` by its verb's bound, not the command timeout; `up`
renders `stop_bound_s`, `step_launch_wait_s` and `command_timeout_s` (section 6), from which `settle_timeout_s` defaults
to the stop's own worst case and 30 s (450); `poll_s` defaults to 10 s (every status is a step on the one control core).
A verb's bound is the request's `bound_s`, and a verb past it is answered `timed_out` and read as a command with no
answer, never as done. **`stop_seconds` is to be set from live measurement**: the core journals every stop's evidence,
confirmed or not (the injection's `injected` line, or its `stop_unknown` line for a stop that answered unconfirmed, key
`stop`), so a live slot's journal gives each stop's `seconds` and the service's own `service_seconds`.

**(f) The state file**: section 4. `services.<name>` gives the job, the step, the node, the work directory and the
identity; it holds no secret.

**What the bundle must gain for (a), (b) and (d), and the traps a start meets** (M4 builds these; M1 runs the
service steps through `zone.sbatch` and `provider.sbatch` unchanged):

- `zone.sbatch`'s and `provider.sbatch`'s EXIT trap removes the secrets directory and the work directory; with `keep`
  in `stop-requested` it must remove neither, and end with status 0.
- `wait_ready` demands a fresh deployment (`Congratulations! New`) and the guard's first-start flow. A restart over an
  existing deployment prints neither: `restart.sh` needs its own readiness (`service-ready.lock`, `/configuration`
  answering with a domain, the guard's verdict for a restart) and the live default and port checks again. The guard's behaviour on a
  restart is untested.
- **`sweep_leftovers` removes a work directory whose `owner.pid` process has ended**, and a stop ends it. A stopped
  service's directory would then be removed by the next `node-init.sh` of ours on that node (another federation's,
  on a shared node). A kept directory needs a mark the sweep honours: `teardown.sh --keep --keep-until <epoch of the
  allocation's end>` (from the state file), kept until that time has passed.
- M4's bundle scripts: `teardown.sh --keep [--keep-until <epoch>]`; `restart.sh <oz|op> --workdir W` (a start's
  service step); `exec.sh <oz|op> --workdir W -- argv` (a control step: `apptainer exec`, not `--writable`,
  `HOME=/root`); `status.sh <oz|op> --workdir W` (a control step).
- Concurrent `exec` into a sandbox that a running service holds `--writable` is a live-slot item.
- After a stop, the service step's tmpfs pages (rootfs, bucket, storage) stay charged to the job's cgroup; the
  restarted step's memory must still fit the node with them. Measured live, not assumed.

## 8. A shared read-only imported storage (M3)

M3 replaces a provider's tmpfs posix storage for an imported space with a directory on the shared filesystem, bound
read-only into that provider's container. It changes the service step's binds and the support's storage, not the
process model: the step, the state file and the verbs stay as above, and `down` is unchanged (the bind exists only in
the container's own mount namespace, so a teardown never enters the tree). It is declared in the layout the driver
already reads (`layouts/shared-import.toml` is an example, and `layouts/pilot-shape-import.toml` is the pilot's shape
with its four data spaces imported; `layouts/pilot-shape.toml` stays ingest-based until a live slot shows the two
equivalent), as `import = { readonly = true, shared = true, source_env, manifest_env, direct_io }` on a space. The driver
makes one call at each of seven places, all in `hpcimport.py` or in the Compose federation's modules it imports
(`docs/storage-import.md`, "A tree on the cluster's shared filesystem", says why each is there). **Every variable is
read from the driver's own environment** (`Driver.environ`, which `hpcfed.py main` fills from the login node's, as
`set -a; . site.env; set +a` left it), never `os.environ` directly:

| Where in `up` | The call | What it does |
|---|---|---|
| Preflight (section 5, step 1) | `Driver.check_imports`: `hpcimport.shared_imports_of(layout, provider)` for every provider, then for each of `layout.shared_imports()` `Layout.import_source(space, environ)` and `Layout.import_manifest(space, environ)`, then `hpcimport.check_support(layout, environ)` and `hpcimport.check_separate(layout, environ, {root, L})` | refuses an import a provider owns that is not shared (a writable one copies its tree into the root, and one not marked shared would leave the sharing out of the platform file the core checks), a variable that is unset, relative, with a `..` step or a link on the way, a source that is not a directory, a manifest that is not a file or lies in a tree, and a manifest of more bytes than the space's `support_bytes`, two trees that are one directory or one inside the other, and a root or a log directory `L` that is, holds or lies inside a tree (the real paths are compared: the driver writes both on the host, which the container's read-only bind never reaches, and a root beneath a link into a tree would have `up` write its state there); **and `[hpc] core = "step"`**: the node's `env -i` allowlist carries none of the variables, so the core's phase there cannot read a manifest. Nothing is submitted: no sbatch, no state file, no root |
| A provider's service step (step 6) | `hpcimport.provider_import_args(layout, provider, environ)` appended to the `provider.sbatch` argv, after the peers | `--import=SPACE=PATH` per tree the provider owns; none for a mirror. The path travels as an argument and never in a step's environment (every step's `env -i` list is the same three names). The bundle checks each tree again on the node, makes the mountpoint in the rootfs and binds it `:ro` at every start of the service, a restart (section 7) included, since the list is in the work directory |
| After every provider's verdict (step 6) | `Driver.check_mounts(provider)`, for each owner: a control step named `<allocation>-<provider>-mounts` on its node, `hpcimport.mount_check_argv(bundle, workdir, python=site.node_python)` | exits 0 only when every listed tree, and every mount below it, is held with `ro` in the mount's own options by the service's container processes (the children of the starter `service.pid`'s supervisor started; nothing else of the account's on the node counts). A failure fails `up` before the bootstrap starts any scan, naming the provider and the spaces and repeating only the check's own `FAIL: shared import <space>` lines. **The driver also reads the report** (`hpcimport.mount_check_problems`) against the spaces it asked the provider to mount, since the script exits 0 for a work directory with no list of imports: an empty or unparsable report, a space reported twice, not at all or not asked for, held by no process, or writable in any, fails `up` too. Each owner that passes is recorded in the state (`services.<name>.mounts_proven`), and the core's phase refuses to start, naming the provider, for an owner with no such record, so `hpcfed.py core` run by hand over a partial `up` never starts a scan. M4's `start` clears the record in the state change that claims the new launch, and the first `status` that finds the restarted owner otherwise ready runs the same check and records it for that launch, `ready` false until then (section 7) |
| The bootstrap (step 8) | `HpcBootstrap.import_storage_extra(service)` returns `hpcimport.import_storage_extra(uid, gid)`; `hpcboot.bring_up` calls `boot.ensure_import_storages()` between `ensure_spaces` and `ensure_support`, and `boot.wait_import(1800)` after the members | the imported storage is made with the job user's ids, the local LUMA feed and the canonical path type, and `fedboot` names it `import-<space>` and supports the space on it with the one-scan import configuration, as the Compose federation does; the scan is awaited, and one that failed or did not complete fails `up`. Both calls are the bootstrap's own no-ops for a layout with no import |
| The bootstrap's LUMA step (step 8) | `HpcBootstrap.support` sends `hpcimport.luma_requests(storage_id, space_id, member_ids, uid, gid)` for the storage `storage_for(service, space)` returns | the space's default credentials on **that storage**, and each member's mapping on it once per (provider, storage): an owner of an imported space has two storages. A user mapping that is already there is read back and must name the job's uid |
| After `platform.toml` is rendered (step 9), before the link check | `fedimport.verify_imports(layout, environ, timeout_s=1800)`, inside `hpcboot.core_phase` | the core's `ImportedSpace.provision` over the rendered file: read-only imported storage on the owner's panel, scan completed, every provider's listing is the manifest, every file whole on the owner. A space the layout shares and the file does not is refused unsent, so a template that leaves `shared = true` out fails `up` before the link check runs |
| With the rendered file (step 9) | `hpcimport.direct_storages(layout, boot.ids.storages, environ)` fills `@hpc_direct_storages@` | an inline TOML table of each `direct_io` import's storage id to its tree's directory, `{}` when none declares one, for a template's `[sandbox.mount] storages` (M5 is its consumer; no template names it yet). The file that holds the host's paths is the root's own, never committed |

Two things the contract did not say, which the driver keeps and the tests pin: **the storage a support names is
found by name** (`import-<space>` for the owner of an imported space, the provider's regular one for every other
support; never the first the panel lists), and **`platform.toml`'s template repeats each `import` declaration** with
`shared = true`, so the core, which reads the file and not the layout, can refuse a storage that is not read-only.
`direct_io` is read by the sandbox milestone.

**Not built, and what would build it.** `[hpc] core = "step"` with a shared import: the variables would travel to the
core's step as arguments (`hpcfed.py core --import-env NAME=VALUE`, the way `--import=SPACE=PATH` reaches a provider)
rather than in the allowlist; a first live slot that finds the login node cannot reach the services' 8443 (section 9,
assumption 1) is what would need it. The scan is also awaited inside the core's phase, so on `core = "step"` the
core's bound (5,400 s) would have to cover it.

**The agent's node S (M5).** The campaign's dispatcher runs as one step on S, launched from the login node like any
step here; the gate is on S's loopback and every cell a local `apptainer exec` on S, so S hosts no Onedata service.
`[hpc] sandbox = { cpus, memory }`, or the agent's limits in the layout's template, makes S one more role, placed
last, recorded in the state file as the service `sandbox` (kind `sandbox`, no work directory) and as the top-level
`sandbox` record (section 4). S is held at its own shape (section 2), as component 1 of the held job
(`sandbox_placement = "het"`) or as a held job of its own (`"separate"`; section 1). `[sandbox]` is in the pilot's
template (O63), with placeholders `up` fills from `site.env` (`hpcpins`: the SIF and the egress shim, each checked
against its declared SHA-256 before the cluster is touched, the work root, and the bundle's collector), and every path
into the root is rendered absolute. The REST arm (#384) is a layout of its own, `pilot-shape-rest.toml`, whose template
adds a content check file, a REST level and the 443 mapper as the one preload, pinned by `@hpc_gateport@` and
`@hpc_gateport_sha@` (`HPC_GATEPORT`, `HPC_GATEPORT_SHA256`) and checked by the same preflight, only when its template
names them. M5 launches its shard's step on `sandbox.node` with `--jobid=<sandbox.job>`, plus
`--het-group=<sandbox.het_group>` when that is not `null`; the step makes the sandbox's work root on S's tmpfs and copies
the root's `platform.toml` beside the study's `study.toml`. The driver holds, reads back and places S, runs its node
facts step there, and in `down` the sweep of its tmpfs (`shards.py sweep`, by `HPC_CORE_PYTHON`, which `up` asks for
with an agent's node: each shard step's directory of this federation whose step is gone; then the Apptainer run reaper
over the sandbox's work root, each dead run's mounts unmounted, `fusermount3 -u` then `-uz`, shown gone by the mount
table, before its directory and record go, and the work root removed once empty and no step directory is on the node),
on S and on every node `shard_nodes` records, then releases it. A sweep that cannot reap (its leftovers, or a Python
without the core) fails, and `down` keeps the root. The next shard step on a node reaps the same way before it uses its
work root, and refuses when it cannot.

**The render identity (O74).** What `up` rendered is recorded as hashes alone in `<root>/render-identity.json` (mode 0600,
canonical JSON with sorted keys; `hpcidentity.py`, written by `hpcboot.write_facts` beside `platform.toml`, so for
`[hpc] core = "login"` and `"step"` alike): `version` (`expcore-render-identity/1`); `template_sha256`, the template as
the renderer read it, before it was rendered; `platform_fingerprint_sha256`, the canonical JSON of the rendered file's
`PlatformConfig.fingerprint()` hashed as the core hashes a fingerprint (`expcore.campaign.manifest.fingerprint_sha256`);
and the SHA-256 of each pin the platform holds (`sandbox_sif_sha256`, `egress_shim_sha256`, `egress_collector_sha256`,
`oneclient_bundle_sha256`, and `gateport_sha256` for the REST layout), the very values the preflight checked
(`hpcpins`), never read again; and, when `site.env`
declares `HPC_STUDY_ARTIFACTS`, `study_artifacts_tree_sha256`, the digest of that directory's tree. It holds no address,
id, job, node, path, user or secret, so it can travel with a shard's results and be committed beside a study. A pin the
template does not name is left out, and so is the artifacts digest of a site with no such directory; a layout with no
template writes no record. **The artifacts digest** is taken by `up` once, in its preflight on the login node, before
the root is made or a job submitted (a tree that cannot be digested fails `up` there), kept in the state file and
handed to the core's phase among its values, so a core step on a node, whose environment is an allowlist, has it and
never reads the tree. It is the SHA-256 of one canonical listing: `expcore-artifacts-tree/1` and a NUL, then one record
per entry sorted by its relative path as bytes, none for the directory itself: `F NUL path NUL size NUL sha256 NUL` (a
regular file), `D NUL path NUL` (a directory), `L NUL path NUL target NUL` (a symbolic link, by its target string, never
followed, so what it points at must be in the tree to be pinned: the directory is self-contained, its interpreter
installed in it and its venv built on it, and a link whose resolved target leaves it is refused. A link is resolved as
the kernel resolves it, against the tree's own entries alone: a component that is a link is replaced by its target, `..`
goes up one and one at the root is an escape, an absolute target must begin, by whole components, with the root's real
path or the path it was declared by, and a path may pass through 40 links at most, so a loop is refused; a link that
dangles inside is allowed and recorded, and a tree whose links stay inside has its earlier digest). A special file, or a
root that is not a directory of its own, is refused, naming `HPC_STUDY_ARTIFACTS` and a path relative to the directory, never an absolute one. No mode, owner, time
or path of the directory is in it, so a copy elsewhere has the same digest. The tree is read below one held descriptor
with no link followed and never written (the pilot makes it read-only first). A manifest's `expect_render_identity`
(a path of the study; the lines carry it with its hash as `--expect-render-identity*`) makes `step` compare the
federation's record with the expected one before it makes anything, beside the pre-step guard and before it, refusing
with 73 and naming the keys that differ; and every step writes the record it validated into
`out/study-state/render-identity.<step id>.json` at its start (after a resume has restored `out/`, before the
dispatcher), a file of its own per step, so every snapshot, a killed step's `out/` and the archive carry it and a
resumed shard keeps the identity each step ran under. `shards.py` keeps its own copy of the record's names, since it runs alone with `-I`; a test compares them.

**The study's own scripts: a pre-step guard and a post-run script.** A study's own refusals (one worker, every
planned model's cap 1, a federation root of its own) must hold at every shard's start, and `step` makes the work root and
its tmpfs directory itself, so they cannot be checked from the study's plugin, which runs later, nor from a wrapper, which
a line run directly skips. The manifest's `pre_step` (a path relative to the study's root, with its bound
`pre_step_timeout_s`) is hashed by `plan` and `lines` and rides on every line as `--pre-step`, `--pre-step-sha256` and
`--pre-step-timeout`. `step` runs it before anything else: it holds the stamped study's root open (a directory of this
user's, not writable by group or others; the tmpfs copy is not made yet), reads the guard below it, checks the hash,
and runs those bytes as `<python> -I -B - <label> <campaign.toml>` with the held root as its working directory, under the
step's allowlist and no-home variables, in a session of its own with a bound. A refusal ends the step with 73 and nothing
made; a pass is recorded in `step.json` and the archive's manifest. The symmetrical `post_run` (`--post-run*` on the
line, a bound of 300 s by default) is checked at the same point and run once the dispatcher has exited 0, before the
archive, while the federation is up: `<python> -I -B - <label> <out/> <status>` in the tmpfs copy of the study, in the
dispatcher's environment. A pass makes the shard's results final; anything else archives them anyway, with `final: false`
in the manifest and `NOT-FINAL.json` in `out/`, which `collect` and the core's merge refuse. `--archive-left` runs
neither script: it starts no work, and it archives from the step's record.

## 9. Assumptions a live slot verifies first

1. **The login node reaches every service's 8443** at the nodes' addresses, by the declared names (the address map). The bootstrap, the link check and the core's
   platform layer depend on it with `[hpc] core = "login"`. Proven live: node to node (the standing run's cell reached
   every public port). Not proven: login node to node. **The fallback is a setting, not a code change:** `[hpc] core =
   "step"` runs the same phase (`hpcfed.py core`) in a control step on the zone's node, with the core's Python
   (`HPC_CORE_PYTHON`: 3.12, a venv of this checkout on the shared filesystem) as `python -I`, its environment the
   allowlist, its self-check exiting 97 when it imports another core. **The first live slot tests this first:** one
   `up` with `core = "step"`, then one with `"login"`.
2. **`zone.sbatch` and `provider.sbatch` as `--exact` steps** of a multi-node job with `-c` and `--mem` per step (the
   standing run used `--overlap`).
3. **`srun --export=PATH ... /usr/bin/env -i ...`**: no site prolog re-sets the step's `TMPDIR` after `env -i` (none
   can: `env -i` runs last).
4. **The panel through 8443 as the admin's token** (`fedboot.Bootstrap.panel`) on the bundle's providers, and the
   support with the LUMA local feed's mappings.
5. **The pair count**: the rendered `platform.toml` of the pilot's layout gives ten pairs (`link_pairs`), and each
   passes over the cluster's network.
6. **The heterogeneous job** (`sandbox_placement = "het"`). Proven on the site: `sbatch --test-only` accepted two such
   jobs, with `--wrap` in component 0 and component 1 naming only `-N`, `-c` and `--mem`. **Proven by the first live
   slot (16 Nov 2026):** `scontrol show job -o <leader>` answers one line per component with `HetJobId` and
   `HetJobOffset` and each component's `Requeue` and `TimeLimit` as asked; `squeue -h -j <leader> -o %i` lists
   `<leader>+<k>` (the leader as `+0`); an `srun --jobid=<leader> --het-group=1 -w <S>` from the login node lands on S;
   a step of component 1 is listed by `squeue -s -j <component id>` alone, as `<component id>.<n>`; and a leader's
   `scancel` ends component 1. **Found by it:** a step that names no `--het-group` is refused, component 0 included
   (section 1). Not seen yet: how `squeue -s -j <leader>` lists a step of component 0 (read as `<leader>.<n>` or
   `<leader>+0.<n>`), `scancel <component id>.<n>`, and `sacct -j <component id>` for a step's status. **The fallback
   is a setting:** `sandbox_placement = "separate"`. **Found by the second live slot (16 Nov 2026), on a site whose switch
   plugin gives each job a virtual network identifier:** in a heterogeneous job, every new step on component 0 failed
   with `srun: error: task 0 launch failed: Error configuring interconnect` after the zone and five providers had
   started, the first provider's token mint and delivery were cancelled in 0 s, and `up` failed at that provider's
   registration; `--network=single_node_vni` failed the same way, `--network=no_vni` ran, and a step on component 1 ran.
   The same steps ran in a job of one component in two earlier runs. Whether a count of steps per component or exhausted
   identifiers is the cause is not established. Two settings go round it, neither the default: `HPC_STEP_NETWORK`
   (section 3) and `sandbox_placement = "separate"` (section 1). Not seen: a whole `up` with either.

7. **The registered names** (O73), not yet run live: the service cells' own hosts file in a `--writable` sandbox
   (`--no-mount bind-paths` and a file bind onto `/etc/hosts`, which the site's Apptainer accepted for the agent's SIF
   only); a registered domain unrelated to `cluster.domainName`; the zone's `/configuration` and each provider's
   answering their declared names; a provider's check of a peer and an rtransfer replication between two providers by
   their declared names; Onepanel's DNS check, measured; each service's leaf naming only its declared name (and
   `s3.<name>` on a provider), verified by `curl --resolve` with the exported CA; and **a sandbox slot that records the
   cell's environment, `/etc/hosts`, `gate.json` and the gate's certificates' names, the agent's typical REST answers,
   one forced no-connection error, the K1 tools' answers and the transcript, and asserts none holds a node's name or
   address or `8443`.** The tool server's connection to a declared name through the resolve launcher (section 5,
   "The registered names", item 5) is tested offline in a real child process only: on a node, with onedata-mcp's own
   venv and interpreter, it is owed too.

## 10. Who builds what

| Milestone | Builds | Relies on |
|---|---|---|
| M1 (this) | `hpcfed.py` (`up`, `down`, `describe`, `core`, the hook); the state file and its store; placement; sizing; the submit read-back; the step launcher (allowlist, names, client handling); the bootstrap and `platform.toml`; the link check; `bundle/nodefacts.py`, `bundle/zone-export.sh` | the bundle as it is |
| M3 | the imported storage on the shared filesystem, read-only, in the provider's step | section 8 |
| M4 | `hpcfed_control.py` (`stop`, `start`, `status`, `exec`, behind the hook); `teardown.sh --keep`, `restart.sh`, `exec.sh`, `status.sh`; the sweep's kept mark; the `"hpc"` backend of `[faults]` | sections 4 and 7 |
| M5 | the agent's node's step and `[sandbox]` in the template (built: `hpcpins`, `shards.py`, O63); its node held by M1 as a heterogeneous job's component or a job of its own | sections 1, 4 and 8 |

## 11. Provider storage on scratch, and the inode guard

The cluster's shared scratch filesystem has room for a federation's data and a limited **inode** quota: a pilot-shape
federation's data is about 17,500 files, and every federation of an account counts against one quota. With
`HPC_STORAGE_ROOT` set in `site.env` (an invented `<scratch_root>/hpcop-storage`; `site.env.example`), `up` makes the
federation's provider storage there, guards the quota before it submits anything, and binds each provider's own
directory onto its posix storage, and `down` removes it. With none set nothing of this runs: each provider stores on its
node's tmpfs, and `up` and `down` are what they were. The bind and the guard are built and tested offline; their live
run is open.

**The inode guard** (`hpcquota.py`; the preflight, before the root, the state or any job exists):

1. Read the account's inode usage and quota through the command `site.env` names, `HPC_QUOTA_CMD`, split as a shell
   would split it and **run without a shell** on the login node (bounded: 120 s), and read its output with
   `HPC_QUOTA_PATTERN`, a regular expression with the named groups `used` and `quota`, which must match exactly once and
   capture each figure whole: a figure the pattern cut out of a longer one (a `1000.5` read as `1000`, a `2G`, a `95%`, a
   digit run on) is refused, so the characters touching a captured figure may not be a letter, a digit, `_`, `.`, `,`,
   `+`, `-` or `%`.
   The repository holds neither the site's tool nor its output: the tests run an invented command and an invented format.
2. **Estimate** the federation's inodes from its data manifests: every space's manifest once, **files and directories**
   (each is an inode). A shared import's manifest is its layout's own file; every other space's is the baseline `up`
   writes (`hpcdata.declared` is the pure read of what `hpcdata.data_manifests` writes once the services are up, and one
   test pins the two together). A shared import's owner adds nothing on scratch (its storage is the tree, read-only), but
   the copy a mirror fetches does, so it counts once like every other space; a replica on every mirror is **not**
   multiplied in (no multiplier is invented), and the 5% below the line is the room for what the estimate leaves out. For
   the ingest layout (`pilot-shape.toml`) the estimate is a handful of files while its ingested space declares no
   manifest (`baseline = false`, nothing the study will put there is known to `up`); a study that declares it
   (`data_manifest = "@study_artifacts@/..."`, which `up` loads and records, section 6a) has that manifest counted too,
   from the site's `HPC_STUDY_ARTIFACTS`: the pilot's largest space is 15,340 files (`hpcdata.declared` takes it through
   `ingested_manifests`, so the estimate and the manifest `up` verifies the space against are one count); for the import layout
   (`pilot-shape-import.toml`) it is the four imported manifests, about the pilot's 17,500.
3. **Refuse** when `used + estimate > quota * 95 // 100`, in integers, so a total exactly at the line passes. The message
   names the used count, the estimate, the quota and the 95% line.

**It fails closed.** The guard refuses `up`, naming why, when: `HPC_QUOTA_CMD` or `HPC_QUOTA_PATTERN` is unset; the
pattern does not compile or lacks `used` or `quota`; the command cannot run, exits non-zero or does not answer in time;
the pattern matches the output never or more than once; a figure is no count (including `unlimited`) or is cut out of a
longer one; the quota is 0.
**The check is mandatory for a federation bound onto scratch.** `HPC_QUOTA_CHECK=off` with `HPC_STORAGE_ROOT` is
refused as the site is loaded (`hpcsettings.checked_site`), naming the combination, and again by the guard itself for a
site however it was built; any value other than `off` is refused at the start whatever else is set. Without a scratch
root nothing lands on scratch, there is no check, and `off` changes nothing. A site that sets `HPC_STORAGE_ROOT` and
names no command cannot `up`.

**The home guard** (`hpcquota.home_guard`; the preflight, ahead of the inode guard). The account's home is small and
shared by every agent of the account: a checkout and its venv left in it run to thousands of files, and a full home
breaks every agent at once. Nothing of the federation is stored there, so this guard has no estimate and no multiplier,
and it does not depend on `HPC_STORAGE_ROOT`.

1. Read the home's usage and limits through `HPC_HOME_QUOTA_CMD`, split as a shell would split it and **run without a
   shell** on the login node (bounded: 120 s), with `HPC_HOME_QUOTA_PATTERN`: a regular expression with the named
   groups `files_used`, `files_limit`, `bytes_used` and `bytes_limit`, matching exactly once and capturing each figure
   whole (the same rule as the inode guard's: no letter, digit, `_`, `.`, `,`, `+`, `-` or `%` may touch a captured
   figure). Files and bytes are separate figures with their own limit; the bytes are in the tool's unit, the same for
   the used figure and the limit, since only their ratio is compared (no unit is named in a message). The limit
   captured is the one that **binds**: a tool may print a soft column that reads 0 beside the hard limit, and a limit
   of 0 is refused, so a pattern on the soft column cannot pass a full home. The command is generic: the home need not
   be on the filesystem the scratch quota tool reads, and a site's own script may print the figures. The repository
   holds neither the site's tool nor its output.
2. **Refuse** when `files_used > files_limit * 95 // 100` or `bytes_used > bytes_limit * 95 // 100`, in integers, so a
   figure exactly at the line passes. The message names each figure that is past (its used figure, its limit and the
   95% line) and that nothing was submitted.

**It guards whenever a key is set**, and fails closed as the inode guard does: a command set without its pattern or a
pattern without its command, a pattern that does not compile or lacks one of the four groups, a command that cannot
run, exits non-zero or does not answer, an output the pattern matches never or more than once, a figure that is no
whole count (or is too large to be one: 10^15 files, 10^19 bytes) or is cut out of a longer one, and a limit of 0 all
refuse `up`, naming why. **With neither key set there is no check** and `up` says nothing of the home (the inode
guard's rule, which guards only when `HPC_STORAGE_ROOT` is set: a site is expected to set the keys, and the docs say
so). `HPC_HOME_QUOTA_CHECK=off` (the one value; anything else is refused when the site is loaded, and unset is not off)
skips the check, whatever else is set, and `up` says so (`home check: skipped: HPC_HOME_QUOTA_CHECK=off`).

**A message holds figures and variable names only.** A home quota tool's stderr and its command line name the user and
the home's path, which the inode guard's messages may repeat for the scratch tool (a bounded tail of stderr) and these do
not: a failed command is named by its exit status alone, a command that could not run by its exception's class, a
pattern that does not compile by the compiler's own reason. The three keys are not recorded in the state file
(`Site.to_json`): `down` needs none of them, and a command line that names the user belongs in no record.

**The directories** (`hpcstorage.py`): below `HPC_STORAGE_ROOT` (a directory only this user can write, owned by this user
with no group or other write bit, named by its real path since the nodes bind through no link, which is neither the
federation's root nor inside it, and is no shared tree and holds or lies inside none, since `down` removes below it and
nothing may be written to a shared tree) `up` makes `<root>/<allocation name>` (0700; the name's random part is the
allocation's) and in it one directory per provider (0700), each by an exclusive `mkdir`: anything already there refuses
`up`, and is never adopted. Each directory holds the owner marker `.hpcop-storage-owner` (0600): the allocation's name and
a random token the state file also holds, and a provider's directory's also names the provider. Each provider's
directory also holds, beside its marker, the empty directory the provider stores in (`posix`, 0700), made after the
marker so a failure there leaves a directory `down` can prove; the marker is outside what is bound, so the provider
never sees it.
A marker that cannot be written, for want of inodes, removes the empty directory just made. The state file records every path **before** the first `mkdir`, and the
directories are made after the state exists and before the job is submitted, so a failure here leaves no job billing.

**The bind** (`hpcstorage.provider_args`, `bundle/posixstorage.py`, `configure.sh`, `run_service`):

- `up` gives each provider's batch script one more argument, last, `--storage=<its directory>/posix`, read from the
  state's record checked whole (`hpcstorage.checked`): a step is never given another provider's directory or a path the
  record does not name, and a state that names a scratch root and no record starts no provider (it never falls back to
  tmpfs). The zone takes none. Without a scratch root the argument is absent and every step is what it was.
- `configure.sh` takes it out of the peers and has `posixstorage.py prepare` check it: a plain absolute path of
  characters that cannot end a bind option; of `up`'s form for this provider, `<…>/<allocation>/<provider>/posix`; no
  link at any step of the path (so `up` refuses a scratch root that is not its own real path, before anything is
  submitted); the bound directory, the provider's and the allocation's each a directory of this user, permission bits
  0700 (a setgid bit, which a project's scratch passes down, is allowed), that this user can write; the provider's directory holding `up`'s marker that names this provider and this
  allocation (a regular file of this user, never read through a link); neither inside nor holding the work directory;
  empty; and not on node-local memory (the mount table: tmpfs or ramfs is refused, since the bind is there to keep the
  data off the node). The rootfs's `/volumes/posix`, which the bind covers, must be a real directory holding nothing.
  Only then is the path recorded in the work directory (`posix-storage`, 0600, `<provider><TAB><path>`, by a draft made
  exclusively and replaced whole). A refusal fails the job, naming what was refused and never the path.
- **Every start names its storage, from the state.** The first start's `run-node.sh` is given the job's own argument
  (`provider.sbatch`), and every start again is given it by `hpcfed.py start`, which reads it from the state's record
  checked whole (`hpcstorage.provider_args`; a state with a scratch root and no record, or an edited one, is refused
  before anything is recorded or launched) and passes it to `restart.sh`. `run_service` binds the directory, writable,
  onto `/volumes/posix` (`--bind <path>:/volumes/posix:rw`, after any shared import's read-only binds and before the
  rootfs) only when the work directory's record names exactly that path: `posixstorage.py binds` checks it again (all
  but the emptiness and the mount table, since after a first start it holds the provider's data). A record edited to
  another directory (another federation's, whose marker would pass every other check), a record missing where a start
  names a storage, and a record present where a start names none each stop the start before the service (`restart.sh`
  refuses before it takes the work directory over): a provider is never started on another directory or on tmpfs
  instead. A start that names none over a work directory with no record binds nothing, and the argv is word for word
  what it was.
- **Ownership and mode.** Setuid Apptainer without `--fakeroot` runs the cell's processes as the job's user, and
  `finish.sh` declares the posix storage owned by that uid and gid (`rootUid`, `rootGid`), as on tmpfs, whose
  `/volumes/posix` is a 0700 directory of the same user: a 0700 directory of this user is what the provider needs, and the
  node's check refuses one that is not this user's (a uid that differs between the login node and the nodes included).
- **The same storage in the cell, a different kind in the fingerprint.** The provider sees its storage at
  `/volumes/posix` either way, so `finish.sh` adds the same `posix-local` storage and `platform.toml` names no storage
  path. But where a provider stores its data shapes a run's I/O timing, and so possibly its time-cap outcomes, so the
  rendered file declares the **kind**, `provider_storage = "tmpfs"` or `"shared_filesystem"` in `[sandbox.apptainer]`
  (`hpcpins.values` fills `@hpc_provider_storage@` from whether `HPC_STORAGE_ROOT` is set: the word, never the root's
  path): it is in `PlatformConfig.fingerprint` (`sandbox_backend`) and in every run's `backend` and
  `infrastructure_facts`, so shards on tmpfs and on scratch can never merge by accident, and a test shows the two files
  differ in that one line. A template that declares the sandbox without the line is refused by `up` before anything is
  submitted, and the renderer reads the file it wrote back for it. The Compose federation has no such setting, and its
  fingerprint is unchanged. The data check (`hpcdata.check`, O63) reads each space through its owner's REST, so it reads
  the space where that provider stores it, on scratch; nothing of it changes.
- **The work directory never reaches scratch.** The bind exists only in the container's mount namespace; on the node the
  rootfs's `/volumes/posix` stays the empty mountpoint, so the teardown's removal of the work directory, the next job's
  sweep of leftovers and the failure path's scrub never touch the storage. Only `down` removes it, from the login node,
  once the provider has stopped.

**`down` removes exactly what `up` made.** After the job is gone and every service verified clean (section 5, step 5):

- the recorded `storage` must be, character for character, the form `up` writes: the site's recorded scratch root (normal,
  absolute, not `/`), the allocation's own name below it, the owner token, and a directory of each provider of the layout
  below that, nothing else. A state edited to name any other path is refused, before anything is touched;
- the storage's own directory and each provider's are **opened without following a link, held open, and proved** (a real
  directory of this user that holds this `up`'s marker for it) **before any is removed**, so a bad one stops the whole
  removal. A directory already at the path when `up` ran (it refused it), one with no marker or another `up`'s, an
  unrelated directory of this user put in a provider's place (the parent's marker proves nothing of its children), and a
  link in a directory's place are not `up`'s: nothing is removed and `down` fails saying so, keeping the root and state;
- each provider's directory is **emptied through its descriptor** (`hpcremove.remove_tree` on `/proc/self/fd/<n>`: never
  through a link, a link inside is removed and its target never touched, a read-only directory made writable first), then
  retired (its marker, then the directory by `rmdir` through its parent's descriptor; a failed `rmdir` puts the marker
  back), then the storage's own directory the same way. A path swapped for a link or another directory after the proof is
  never followed: the proved directory is the one emptied, and before each `rmdir` (which takes a name) the name is checked to be
  the directory held (the same device and inode), else the directory keeps its marker, the replacement is left whole and `down` fails.
  The scratch root must be this user's alone to write (`up` refuses one that is not, and `down` checks it again before removing
  anything), since another user who could rename its entries could otherwise swap a directory in the instant before an `rmdir`. A concurrent change
  inside the 0700 tree by this same user is not defended against. An entry in the storage's directory that `up` did not
  make is kept with the marker, and fails `down` naming how many;
- **what remains of the recorded paths is then counted** and any entry fails `down` loudly, naming the count (a removal
  that raised nothing and left entries is caught there). `down` prints `provider storage: <n> directories removed, 0 entries
  remain`.

The removal is idempotent: a `down` cut short, or one that failed, is finished by the next (the marker goes only when
nothing else is left, and is put back if the last `rmdir` fails). A `down` over a root with no state file removes
nothing on scratch: there is no record of what `up` made. A state written before this key (no `storage`, no
`storage_root`) goes down as it did; a state with the site's `storage_root` and no `storage` (the record deleted, or
null) is **refused**, since `up` always records what it makes: nothing on scratch is removed and the root and state are
kept.
