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
time = "02:00:00"                        # the held job's --time
stop_seconds = 600                       # a service's graceful stop bound (teardown.sh --timeout)
core = "login"                           # where the bootstrap and the link check run: "login" or "step" (section 9)
zone = { cpus = 4, memory = "8g" }       # the zone's service step
provider = { cpus = 4, memory = "10g" }  # each provider's service step (posix storage on tmpfs included)
control = { cpus = 1, memory = "1g" }    # the control reserve on every node (mint, deliver, teardown, status, exec)
sandbox = { cpus = 8, memory = "17g" }   # optional: the agent's node S and its step's size (M5; section 8)
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
- **The node**: `--cpus-per-task = max(zone, provider, sandbox).cpus + control.cpus`, `--mem = max(zone, provider,
  sandbox).memory + control.memory`, `--ntasks-per-node=1`, `-N 1+N` (one more with `sandbox`). With the defaults: 5
  cores and 11 GB per node, 6 nodes for five providers. `hpcfed.py describe` prints the cores and memory per node and in
  all, and the billing units per hour when `HPC_BILLING_MEM_WEIGHT` gives the site's memory weight: `nodes × max(cores,
  weight × GB)`. Raising a node's memory costs nothing while its cores are at least the weight times its GB, so a
  uniform shape sized for the agent's node may be free (with a weight of 0.5, 9 cores and 18 GB); where it is not, a
  heterogeneous job for that node is the later option.

## 3. How every step is launched

```
srun --jobid=<job> --exact -N1 -n1 -w <node> -c <cpus> --mem=<mem> -J <step name> --export=PATH \
     [--output=<log>] /usr/bin/env -i <allowlist> <argv>        # stdin </dev/null unless it is the payload
```

- **The environment is an allowlist**, set on the node by `env -i`, so neither the login environment (srun passes it
  otherwise) nor a site task prolog decides it: `PATH` (`HPC_STEP_PATH` from `site.env`), `LC_ALL=C`, and
  `TMPDIR=<node tmpfs>` (`HPC_NODE_TMP`, default `/tmp`). Nothing else, and never a secret. The bundle's helpers
  then run on the first `python3.11` on that `PATH` (`lib.sh`'s default `HPCOP_PY`), which must be the node's own:
  `supervise.py`, which holds every service, needs `os.pidfd_open`, which uv's standalone Python lacks. Only the core's
  step (`HPC_CORE_PYTHON`) runs a venv's Python.
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
  still listed at the bound is cancelled by its own id (`scancel <job>.<step>`), never more. Both ends of a token's
  pipe are finished whatever the first does. Ending a client is verified (SIGTERM, then SIGKILL, each only while the
  pid still has its recorded start tick), and `down` also ends any `srun --jobid=<job>` of ours on the same login node (run `down` where `up` ran) that
  no record names (one spawned just before a crash).
- **No bytecode is written** by the driver or a step: `hpcfed.py` sets `sys.dont_write_bytecode`, and every Python a
  step runs takes `-I -B`.
- **The core the driver imports is recorded**: `up` writes the resolved paths of `expcore_onedata`, `fedlayout` and
  `hpcfed` into the state file, and refuses to run when `expcore_onedata` is not imported from this checkout's `src`.
  The core's step (`[hpc] core = "step"`) makes the same check on the node and exits 97 on a mismatch, which fails
  `up`. The bundle's scripts run from this checkout's `deploy/hpc-federation/bundle`, which must lie on a filesystem
  the nodes see; the driver never takes the bundle from anywhere else.

## 4. The federation's root and its state file

The root is the Compose federation's (`fedlayout.Layout`: `EXPCORE_FED_ROOT_PARENT`, `EXPCORE_FED_ROOT`, the same
checks, the same mark): a 0700 directory, here **on a filesystem the login node and the nodes see** (named in
`site.env`; never node tmpfs). It holds what `up` lays out for Compose (`platform.toml`, `fault-journal/`,
`link-check.json`, `facts.json`, `ca/`, `secrets/`) and the state file, **`hpc-federation.json`**:

```json
{
  "format": "expcore-hpc-federation/1",
  "project": "<the layout's project>",
  "layout": {"source": "<file name>", "digest": "<sha256>", "path": "<absolute path>"},
  "core": {"expcore_onedata": "<path>", "fedlayout": "<path>", "hpcfed": "<path>"},
  "command": ["<python>", "<.../hpcfed.py>", "--layout", "<path>", "--root", "<root>"],
  "site": {"account": "...", "partition": "...", "images": "...", "logs": "...", "step_path": "...",
           "node_tmp": "/tmp", "node_python": "python3.11", "core_python": "...", "admin_mail": "...",
           "billing_weight": null},
  "allocation": {
    "name": "hpcop-<project>-<8 hex>",
    "job": "<id>",
    "nodes": ["<node>", "..."],
    "cpus_per_node": 5, "memory_mib_per_node": 11264, "time": "02:00:00",
    "readback": {"JobName": "...", "TimeLimit": "...", "Requeue": "0", "Account": "...", "Partition": "...",
                 "NumNodes": "...", "NumCPUs": "...", "MinMemoryNode": "..."},
    "started": "<Slurm's StartTime>", "ends": "<Slurm's EndTime>"
  },
  "roles": ["zone", "<provider name>", "..."],
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
  "phase": "preflight | submitted | placed | zone | providers | bootstrapped | linked | stopping | down"
}
```

- **`site`** is what `up` read from `site.env` (no secret): every later command reads it here, so `down` and the
  service verbs need no environment. **`command`** is this driver's argv prefix, which `platform.toml`'s `[faults]
  command` holds. **`clients`** are the detached srun clients of the service steps, by step name, which `down` ends
  once their steps are gone. **`owner`** is the job's uid and gid on the nodes (the posix storage's owner).
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
   imported tree's variables and manifest (section 8). A root that holds a state file is refused: `down` first.
2. **Submit** the held job: `sbatch --parsable --job-name=<allocation name> --account --partition --no-requeue
   --time --nodes --ntasks-per-node=1 --cpus-per-task --mem --chdir=/ --output=/dev/null --export=NONE --wrap 'exec sleep
   <seconds of --time>'`. **The read-back:** `scontrol show job -o <id>` must report what was asked (TimeLimit,
   Requeue=0, Account, Partition, NumNodes, NumCPUs, MinMemoryNode, JobName); otherwise `up` cancels that one job id
   and fails, naming the field. sbatch's stderr is kept apart from the id it prints.
3. **Wait for it to run**, bounded (30 min); at the bound, or if it ends first, it is cancelled by its id. **The read-back
   runs again on the running job**: a scheduler that allocated more than the pending request showed is caught there,
   and the job cancelled before any step.
4. **Place** (section 1), then one control step per node prints the node's FQDN and its IPv4 address on the
   cluster's network (`nodefacts.py`, Python 3.9-compatible, stdlib only).
5. **The zone**: a detached service step, `zone.sbatch <bundle> <zone tar> <zone name> <every provider's address>`,
   run as a step (it holds the service for the step's life, as the standing run did). `up` waits for its log's verdict
   (`hpcop: OK zone`, or `FINISH FAILED` or `hpcop: ERROR`, which fail `up`).
6. **The providers**: one detached service step each, `provider.sbatch <bundle> <provider tar> <name> <zone FQDN>
   <admin mail> <zone address> <every other provider's address>`; for each, a registration token minted in a control
   step on the zone's node piped into a delivery step on the provider's (`mint-registration-token.sh |
   deliver-token.sh`, the bundle's, with `--workdir`); then each log's verdict (`hpcop: OK provider`). A provider that
   owns a shared imported tree is also given `--import=SPACE=PATH`, and once every provider is up a control step on
   its node proves the tree read-only (section 8).
7. **The root's secrets and CA**: the zone's admin passphrase through a control step's stdout into
   `secrets/zone.passphrase` (0600), as the registration tokens travel; the Onedata test CA of the zone's rootfs (public)
   into `ca/federation-ca.pem`.
8. **The core's phase** (`hpcboot.core_phase`), on the login node or in a control step on the zone's node, as `[hpc]
   core` says (section 9). **The bootstrap** is the Compose federation's own `fedboot.Bootstrap` over a layout whose
   services' URLs are `https://<node FQDN>:8443`: the admin's token, the users, the spaces, the support (on each
   provider's one posix storage, owned by the job's uid, and that storage's LUMA local feed mapping the space and every
   user to that uid, as `onboard.py` does), the members, the baseline content (`fedcontent.Content`). An owner of a
   shared imported space also has its `import-<space>` storage made before any support, and the scan awaited after
   the members (section 8).
9. **`facts.json` and `platform.toml`** (section 6), then each shared import verified against its manifest (section 8),
   then **the link check**: `fedlinks.check_after_up`, the platform
   layer's own check through the rendered `platform.toml`, which writes `link-check.json` in the root through
   `expcore_onedata.linkrecord`. `up` fails unless every pair passes.
10. **The fault relay** (M4's `expcore_onedata.fault_relay`), when the core has it: started detached on the login node
    (`env -i PATH HOME LC_ALL [SLURM_CONF] <python> -m expcore_onedata.fault_relay serve --root <root> -- <this
    driver's argv prefix>`, its output appended to `<L>/<allocation name>-fault-relay.log`), then `fault_relay alive`
    polled until it beats (30 s at most; a relay that never beats fails `up` and is ended). It is recorded in the
    state file as `relay`. One relay per root: it takes a lock.

A failed `up` leaves its state file: what it placed is recorded, and `down` releases it.

**`up` and `down` each hold the root's lifecycle lock** (`hpc-federation.lifecycle.lock`, `flock`, never waited
for) from start to end, so a `down` never runs between an `up`'s state and its `sbatch` (it would find no job and
remove the root); the service verbs do not take it.

**`down`** (login node), whatever `up` reached:

0. **Stop the fault relay first** (`fault_relay stop --root <root>`, when the core has it), so no service verb launches
   a step while the federation goes down; a relay that will not stop is reported, and `down` goes on. Its directory
   `<root>/fault-relay/` goes with the root.
1. Read the state file. A recorded job id whose job now carries another name is not ours (Slurm reuses ids): nothing
   is torn down or cancelled. When the id is missing, find the allocation by its name among this user's jobs
   (`squeue -u <user> -h -n <allocation name> -o %i`, an exact name holding `up`'s random part); with no state file and
   no name, there is nothing of `up`'s to find, and `down` says so. **Never `scancel -u`.**
2. If the job runs: a `teardown.sh` control step per service with a recorded work directory (providers first, then
   the zone), each bounded by `stop_seconds`; then wait until no step but `batch` and `extern` is listed in `squeue -s`
   (ending each service step's srun client only then); a step still listed at the bound is cancelled by its own id.
3. One read-only control step per node checks that no recorded work directory is left on it.
4. `scancel <job>`, by the recorded id only; wait until `squeue -j` lists it no more. **Whatever fails in 0 to 3 (a
   relay that will not stop, a failed `squeue`, a work directory the driver refuses), the job is still cancelled and
   every client ended; the failure is reported as unverified.**
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
- **storages are found by name**, never by place in a list: finish.sh's `posix-local` for a provider's support, and
  `import-<space>` for an owner's support of an imported space (M3: such an owner has two); the LUMA mappings are
  made on the storage the support names;
- `[faults] journal = "fault-journal"`, when the template declares faults, so the link-check record lands in the root
  (`linkrecord.record_path`);
- `[faults]` for the HPC backend (M4's loader): `backend = "hpc"`, `stop_bound_s`, `journal`, `[faults.services]`
  naming every provider, and none of `compose_file`, `project`, `command` or `docker`, which that backend refuses;
  its defaults are `settle_timeout_s = stop_bound_s + 60`, `poll_s` 10 and `relay_wait_s` 30;
- the values only the HPC driver fills, for a template that names them: **`@hpc_stop_bound_s@`**, `stop_seconds + 90`
  (690 by default, section 7e); **`@hpc_settle_timeout_s@`**, `stop_bound_s + 60`; **`@hpc_command@`**, this
  driver's argv prefix as a TOML array; and **`@hpc_token_margin_s@`**, the least `[tokens] margin_s` the platform
  layer's own loader accepts for the rest of the file: `up` writes the file with a provisional margin no rule refuses,
  reads it with `load_faults` as a campaign would, and writes it again with the least that loader's rule gives (5,340 s
  with M4's defaults), so a measured `stop_seconds`, or a term the loader adds, moves it. A core whose loader cannot
  read the HPC `[faults]` (one without M4's backend) gets the restore's rule over the keys it knows, with the HPC
  settle time and poll.

The core's platform layer (gate, health, link check, faults) points at `<root>/platform.toml` wherever it runs,
provided it reaches the services' 8443 and reads the root. A layout's link check covers the pairs its
`platform.toml` declares (`linksettings.link_pairs`): `layouts/pilot-shape.toml` beside this file has the pilot's shape
(a zone, five providers, four data spaces each owned by one provider and mirrored by another, and the probe space on
all five) with invented names, and its template declares the four spaces as `[[spaces]]`, which gives the ten ordered
pairs the pilot's check reports. Its `delta-space` stands for the pilot's largest space (15,340 files); `up` writes
none into it. `layouts/pilot-shape-import.toml` is the same shape with the four data spaces imported from trees on the
shared filesystem (section 8), whose template adds `import = { readonly = true, shared = true }` to each of them and
leaves the ten pairs as they were.

## 7. Service verbs for faults and the heal (M4)

Per service, by its layout name. **Each verb is one argv**, run on the login node (only there can a step be
launched): `<python> deploy/hpc-federation/hpcfed.py --layout PATH --root PATH <verb> <service> [-- argv...]`; the
site's values come from the state file, so the verb needs no environment. A campaign that runs on a compute node
cannot launch a step: its fault injector asks the login node's **fault relay** (M4's, which `up` starts with this
argv prefix and `down` stops first; section 5), which runs the verb and answers.

**The hook** (stable; `hpcfed_control.py` is M4's, `hpcfed.py` M1's): after parsing `--layout`, `--root` and the verb,
`hpcfed.py` calls `hpcfed_control.run(verb, service, argv, *, launcher, state, stop_seconds)` for a verb in
`hpcfed_control.VERBS = ("stop", "start", "status", "exec")` and exits with its status; `argv` is what follows `--`
(for `exec`), else `[]`; `stop_seconds` is the layout's. What it is given:

- `launcher` (`hpclaunch.Launcher`): `control(node, argv, *, name, timeout_s)` runs a bounded control step (`--exact
  -c <control cpus> --mem <control memory>`, the allowlist, stdin `/dev/null`) and returns `returncode`, `stdout` and
  `stderr`, or raises `TimeoutError` at its bound after cancelling that step by its id (`returncode` is None when
  its client was ended after its step vanished and the accounting holds no status for it, and a step a signal ended
  reads as `128 + signal`, never 0); `service(node, argv, *, kind,
  name, log)` launches a detached service step of the kind's size (`"oz"` or `"op"`) and records its srun client;
  `listed(name)` is the step id `squeue -s` lists under that name, or None; `cancel(step_id)` cancels that one step of
  the held job. Every step runs on one of the held job's own nodes, or is refused.
- `state` (`hpclaunch.StateStore`): `read()` and `update(fn)` (section 4).

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

**(c) STATUS** `hpcfed.py status <service>`: one JSON line, no secret: `{"service", "state", "running", "ready",
"identity", "launch", "node", "step"}`. `running` is true only when the service step is listed in `squeue -s` **and**
a control step on the node (`status.sh`) finds the recorded service process alive with its recorded start tick.
**`ready`** is true only once the service's own checks passed for this launch: a marker whose content is the current
`service.pid` line, written by `zone.sbatch` and `provider.sbatch` after `finish*.sh` and by `restart.sh` after its
checks; running but not ready is a Compose container not yet healthy, so a restore is never verified before a
restart's guard, rotation and live checks pass. **`identity` is `"<launch>:<pid>:<start tick>"`**, from
`service.pid`, which `run_service` rewrites on every start: stable while the service is untouched, different after
every start.

**(d) EXEC** `hpcfed.py exec <service> -- <argv...>`: a control step on the service's node runs `exec.sh <oz|op>
--workdir W -- <argv>`, which runs `apptainer exec` with `run_service`'s flags (`--containall --no-home --pwd /root`,
a per-call `--workdir` under W) on the same rootfs. The new container has its own PID namespace but the node's network,
so `/etc/hpcop-erlang-env` gives it the job's epmd port and `/root/.erlang.cookie` the cookie, and Erlang's
distribution reaches the running node over loopback (same uid, so the shim's owner check passes). Output is captured;
the exit status is the command's.

**(e) The stop's bound:** at most `stop_seconds` (default 600, the bundle's) for the graceful stop, then supervise.py's
SIGKILL tail (30 s), then the step's end (polled, 60 s at most): **`stop_seconds + 90` s** in all (690 by default). A
provider stopped in about 30 s live. For backend `"hpc"` (M4's loader): the core bounds `hpcfed.py stop` by
`settle_timeout_s`, not the 120 s command timeout; `up` renders `stop_bound_s` (section 6), from which
`settle_timeout_s` defaults to `stop_bound_s + 60` (750), and the least `[tokens] margin_s` that loader accepts, the
start's stop entering it as its own term (5,340 s with the defaults); `poll_s` defaults to 10 s (every status is a
step on the one control core);
`restart_link`'s bound gets 60 s more (the step's launch and the client's end polling), and the heal's reserves count
it.

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
| After every provider's verdict (step 6) | `Driver.check_mounts(provider)`, for each owner: a control step named `<allocation>-<provider>-mounts` on its node, `hpcimport.mount_check_argv(bundle, workdir, python=site.node_python)` | exits 0 only when every listed tree, and every mount below it, is held with `ro` in the mount's own options by the service's container processes (the children of the starter `service.pid`'s supervisor started; nothing else of the account's on the node counts). A failure fails `up` before the bootstrap starts any scan, naming the provider and the spaces and repeating only the check's own `FAIL: shared import <space>` lines. **The driver also reads the report** (`hpcimport.mount_check_problems`) against the spaces it asked the provider to mount, since the script exits 0 for a work directory with no list of imports: an empty or unparsable report, a space reported twice, not at all or not asked for, held by no process, or writable in any, fails `up` too. Each owner that passes is recorded in the state (`services.<name>.mounts_proven`), and the core's phase refuses to start, naming the provider, for an owner with no such record, so `hpcfed.py core` run by hand over a partial `up` never starts a scan. M4's `start` runs the same check after a start (section 7), and clears the record while it does |
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
`[hpc] sandbox = { cpus, memory }` makes S one more role of the held job, its last node, recorded in the state file
as the service `sandbox` (kind `sandbox`, no work directory); its size enters the uniform node shape (section 2). M5
sizes it (the agent's cores plus 2, plus the control core; its memory, work and tmp bytes, the gate, the dispatcher
and the staged image) and renders `[sandbox]` into `platform.toml` through the layout's template; M1 holds and places
the node and launches nothing on it.

## 9. Assumptions a live slot verifies first

1. **The login node reaches every service's 8443** on the nodes' FQDNs. The bootstrap, the link check and the core's
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

## 10. Who builds what

| Milestone | Builds | Relies on |
|---|---|---|
| M1 (this) | `hpcfed.py` (`up`, `down`, `describe`, `core`, the hook); the state file and its store; placement; sizing; the submit read-back; the step launcher (allowlist, names, client handling); the bootstrap and `platform.toml`; the link check; `bundle/nodefacts.py`, `bundle/zone-export.sh` | the bundle as it is |
| M3 | the imported storage on the shared filesystem, read-only, in the provider's step | section 8 |
| M4 | `hpcfed_control.py` (`stop`, `start`, `status`, `exec`, behind the hook); `teardown.sh --keep`, `restart.sh`, `exec.sh`, `status.sh`; the sweep's kept mark; the `"hpc"` backend of `[faults]` | sections 4 and 7 |
| M5 | the agent's node's step and `[sandbox]` in the template | section 8 |
