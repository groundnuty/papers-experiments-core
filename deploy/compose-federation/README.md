# A throwaway Onedata federation in Docker Compose

A Onezone and Oneproviders on this machine, for the live contract tests, the day-0 checks (#29) and federation
studies, as a layout file declares them. Without one, the layout is `federation.toml` here: one Onezone and three
Oneproviders. Every name in a layout is invented.

## Commands

```sh
uv run python deploy/compose-federation/federation.py up       # from nothing to a bootstrapped federation
uv run python deploy/compose-federation/federation.py reset --clean     # back to the baseline: ingested data gone
uv run python deploy/compose-federation/federation.py reset --replicas  # between runs: only mirrors' replicas go
uv run python deploy/compose-federation/federation.py down     # nothing of it left, on Docker or on disk
uv run python deploy/compose-federation/federation.py status   # the project's containers, the root's parent
uv run python deploy/compose-federation/federation.py memory   # each service's limit, current use and peak
uv run python deploy/compose-federation/federation.py describe # the deployment's table for the day-0 report
uv run python deploy/compose-federation/federation.py compose  # the Compose file the layout generates
```

Each takes `--layout PATH` for a layout other than the default; give every command of one federation the same layout.
Run them outside any sandbox: they drive Docker, and they reach the services on the host's loopback.

- **up** checks that the host's available memory holds the services' limits together (before a first start), makes
  the root, the CA, one web certificate per service and the secrets, starts the zone, signs in as its admin, makes the
  admin's access token and the declared users, writes one registration token per provider, starts the providers, waits
  until each is online, makes the spaces, has each supported by exactly its owner and its mirrors, makes the users
  members of their spaces with exactly their privileges, writes the baseline files into every space that keeps them (all
  but those declared `baseline = false`), and writes `facts.json` and, from the layout's platform template,
  `platform.toml`. Run on a federation that is up, it starts what is stopped and checks every step again, and writes
  no baseline file (see "The baseline" for changing a space's `baseline` key).
- **reset --clean** returns the deployment to clean: it starts what is stopped, checks every step again, removes
  everything in every space, ingested datasets included, and writes the baseline files again into the spaces that keep
  them; a space declared `baseline = false` is left empty. The providers, spaces and users keep their ids, so
  `platform.toml` stays valid. A fault-injection journal left behind is emptied.
- **reset --replicas** is the reset between runs: it starts what is stopped and checks every step again, then keeps
  every owner's data and evicts only what the mirrors hold (`fedreplicas.py`). A space is supported by exactly its
  owner and its mirrors, so every non-owner that can hold a replica, the agent's local provider included, is a
  declared mirror, and its replicas go. It refuses, evicting nothing in any
  space, unless every owner's block map holds each of its files whole, since a file only a mirror holds is an original.
  It schedules one eviction of the space's root on each mirror that holds anything, and accepts the result only from
  the block maps, read through the platform layer's own parser: every mirror holding none of each file and the owner
  still holding each whole, on two sweeps in a row. No file is removed. After it, the default layout's `birch` no
  longer holds its replica of `replicated.bin`; the ended transfer record stays. QoS requirements and running
  transfers are not touched: the platform layer's per-run reset (#118) is to take them, and to replace the directory
  eviction and the sweep behind the same function, `evict_replicas(boot, timeout_s)`.
- A bare **reset** is refused with a message naming both, so nobody wipes ingested datasets by accident.
- **down** reads each service's peak memory, runs `docker compose down --volumes` for the project (all the data is
  bind-mounted under the root, so no volume holds anything the federation needs), then removes the root with
  everything in it, through a container of the project with no network (the services write their data as root), and
  checks that no container, network, Docker volume labelled with the project (`com.docker.compose.project`) or file
  of the project is left; a volume left fails the command, naming it. It runs Compose only over a root that exists and
  carries the project's mark: with the root gone and nothing of the project left it does nothing, and with the root
  gone while containers or networks of the project remain it refuses. It refuses the same way, naming them, when the
  root is gone and a volume labelled with the project remains, as on the retry of a `down` that failed on one: it does
  not run Compose without the root, so the volume is removed with `docker volume rm`.

## Declaring a layout

A layout is a TOML file; `federation.toml` is the default and the reference, and `layouts/five-providers.toml` is an
example with five providers and four spaces, one provider mirroring a second owner's space. Its generated Compose file
is `layouts/five-providers.compose.yaml`.

| Key | Holds |
|---|---|
| `project` | the Compose project name, as Compose accepts one (`[a-z0-9][a-z0-9_-]*`); the root's mark holds it |
| `domain` | the services' domain on the project's network, under `.test` |
| `study_space` | the space the core studies; its owner is the data provider |
| `memory_limit` | each service's memory limit (default `6g`); `[zone]` and `[providers.<name>]` may set their own |
| `support_bytes` | each provider's support of each space (default 10 GiB); `[spaces.<name>]` may set its own |
| `platform_template` | optional: the template of `platform.toml`, a relative path to a file inside the layout file's directory or this deploy directory, with no symbolic link on the way, since up copies it into the root |
| `[zone] port`, `[providers.<name>] port` | each service's port on `127.0.0.1` |
| `[spaces.<name>] owner`, `mirrors` | the provider the space's data is written through and kept on, and the other providers that support it |
| `[spaces.<name>] baseline` | `false` for a space that up leaves empty and `reset --clean` empties, the study space included (default `true`: the baseline files, see "The baseline") |
| `[spaces.<name>] agent_visible` | `false` for a space the day-0 checks alone use: no user of the agent may be a member, and it is never the study space |
| `[principals.<user>] privileges`, `spaces` | a fixed user's exact privileges, in the study space unless `spaces` names others |
| `[principals.<user>] agent` | `false` for a user the agent's tokens are never minted for (the harness's or the day-0 checks'); only such a user may join a space with `agent_visible = false` |

Every key is checked when the file is read, and a refusal names the file and the key: names are unique and usable as
Compose services and domain labels, ports lie in 1024-65535 and never clash, every owner and mirror is a declared
provider, the study space and every member's space are declared, and nothing but `127.0.0.1` can be named as an
address. The images stay the two pinned ones whatever the layout says.

A space the agent must never reach, such as one only the day-0 checks write to, is declared `agent_visible = false`.
No user of the agent is a member of it, so no token the agent holds reaches it whatever its caveats, and the data-path
caveat, which names the study space, never names it. Its members are users declared `agent = false`; a layout that
makes an agent user a member, or hides the study space, is refused.

A study whose agents read the spaces can declare `baseline = false` on every space they read, so the core's baseline
files are not agent-visible material and need no deleting after `up`, which would leave an asynchronous delete racing
the ingest. The space is still made, supported by its owner and mirrors, and given its members and tokens; only the files
are left out. The study space may be one of them: a check that reads a baseline file then finds none, and says so
(see "The baseline"). A layout declares `agent_visible = false` and `baseline = false` independently, and the study
space is refused only for the first. The key sits in the space's table, beside its owner and mirrors; neither committed
layout sets it, so both keep every baseline file:

```toml
[spaces.alpha-space]             # the study space, left empty by up and by reset --clean
owner = "pine"
mirrors = ["birch", "cedar"]
baseline = false
```

The Compose file is generated from the layout at every command (`fedcompose.py`), never edited: what keeps the
federation to itself is part of the generator. `compose.yaml` here is the default layout's output, committed for
review, and a test keeps it, and the example's, equal to what the generator makes.

A layout with no `platform_template` gets `facts.json` alone. A layout's template names its providers, spaces and users
by placeholder (`@provider_id.<name>@`, `@space_id.<name>@`, `@user_id.<user>@`, `@zone_url@`, `@space_name@`).

## What it touches

- **Docker:** only the layout's Compose project (default `expcore-fed`): its services, its network `<project>_fed`,
  and down's short-lived `janitor`. Every container and the project's network carry a label naming the root they
  bind, and a command refuses, before it touches anything, a project whose containers or networks bind another root or
  name none: that is another federation of the same name. A federation brought up before both carried that label is
  refused, so take it down with the code that brought it up. The two images are exactly `docker.onedata.org/onezone:ID-ba7a778696` and
  `docker.onedata.org/oneprovider:ID-765309c84f`, never pulled (`pull_policy: never`) and never rebuilt. They are a
  25.0 development build, not the released 25.0 tag: `day0/2026-11-12-federation-facts-build.md` gives each component's
  commit and names the earlier dated files whose "25.0" means it. Nothing is pruned.
- **Ports:** each service publishes its 443 on `127.0.0.1` only, on the port the layout gives it (the default: the
  zone on 29443, `pine` on 29444, `birch` on 29445, `cedar` on 29446). Nothing else is published. On the project's
  network the services reach one another by their domains (`zone.<domain>`, `<provider>.<domain>`), which resolve
  nowhere else.
- **Memory:** each service is limited to the layout's `memory_limit`. A first up refuses when the limits together
  exceed the host's available memory, so the federation never starves the host. `memory` reads each service's current
  use and its peak since the container started from the container's own cgroup, and down prints the peaks before it
  stops anything; the day-0 report records them.
- **Disk:** the root, `EXPCORE_FED_ROOT` (mode 0700), and nothing outside it. The root must lie in the allowed parent,
  `EXPCORE_FED_ROOT_PARENT` (default `/mnt/volume2/expcore-federation`), which must be absolute and not `/`, with no
  symbolic link from it down to the root. Under the default parent the root defaults to the parent itself. A study
  confined to a directory of its own names that directory as the parent; it must then exist, and `EXPCORE_FED_ROOT`
  must be named and lie strictly below it, so a parent that holds other data is never itself the root. Every command
  checks this before it touches a file or a container, and `facts.json` and `status` record the parent and which
  setting gave it. Down deletes nothing in a root without the mark (`.expcore-fed-root`) naming the layout's project,
  so a root holds one federation at a time. Two federations at once need two roots beside each other below the
  parent (under the default parent, neither may be the parent itself): up refuses a root inside another federation's
  root or holding one, and down refuses to empty a root that holds one. Marks are looked for in every directory above
  the root up to `/`, and below it to `NESTED_DEPTH` (8) levels, never through a link. The root holds:

  | Path | Holds |
  |---|---|
  | `ca/federation-ca.pem` | the federation's CA, the one file the core trusts (`[tls] ca_file`) |
  | `ca/<service>.pem` | each service's web certificate, for its domain and `127.0.0.1` |
  | `secrets/` (0700, every file 0600) | the CA's key, the services' keys, the panels' passphrases and env files, the registration tokens, `admin.token`, and `users/<user>.pass` |
  | `<service>/persistence` | each service's state; up seeds the certificates and the CA into it before the first start |
  | `<provider>/storage` | each provider's POSIX storage |
  | `facts.json` | what an integrator needs to write `platform.toml`: addresses, ids, names, where the credentials are; no secret |
  | `platform.toml` (0600) | rendered from the layout's template, next to the CA and the secrets it names by relative path |
  | `fault-journal/` | the fault injector's journal, when the fault test runs |

  Nothing is kept between a down and the next up: up makes a new CA and new secrets every time.

## Certificates

The federation has its own CA (RSA, 30 days), made by `openssl` at up. Each service's certificate names its domain on
the project's network, its node (`node1.<domain>`) and `127.0.0.1`, so the core verifies it at the published port.
Up seeds the certificate, its key and the CA into each service's persistence directory, where the image's panel and
worker read them, and `start.sh` adds the CA to each container's system store, which the providers' transfer service
reads. Verification stays on everywhere; the images' test CA and their insecure-connection setting are not used.

The zone's GUI package check is off (`gui_package_verification`, as the images' own demo mode does), because the
images are development builds whose provider GUI packages the zone would refuse. Only the browser interface uses them.

## The baseline

Every space that keeps its baseline, which is every space unless the layout declares `baseline = false` for it, has its
files written through its owner. In the default layout:

- `alpha-space`, the study space, written through `pine` (the data provider): `hello.txt` (64 bytes), `replicated.bin`
  (1 MiB, also replicated to the first mirror, `birch`, so the space lists an ended transfer), `samples/sample-1.bin`
  and `samples/sample-2.bin`, and `listing-1200/` with 1200 entries of 16 bytes, for a complete-listing test (no test
  in this repository reads a variable naming it yet). A study space without a mirror gets no replica and no transfer record, and neither
  does one declared `baseline = false`.
- `beta-space`, written through `birch`: `beta.txt`. Every other space that keeps its baseline holds one such file,
  named after it. Its name is outside the study's prefix, so the core may never change it.

Every file's bytes are derived from its path, so each reset writes the same files.

A space declared `baseline = false` holds nothing after `up`. `reset --clean` removes what a study ingested into it and
writes nothing there, and `reset --replicas` treats it as any other space, walking what it holds and finding nothing to
evict when it is empty. `facts.json` records `spaces.<name>.baseline`, and its `files` entry (the content file and the
listing directory) is `null` when the study space is one of them, so nothing names a file the space does not hold.

Changing the key on a federation that is up takes `reset --clean`, since `up` there writes and removes no baseline file.
`up` refuses a space brought up with baseline files that the layout now declares `baseline = false`, naming the space
and saying `reset --clean`: the files would stay where an agent can read them. `reset --clean` empties the space and
writes nothing into it. A space declared `true` again gets its files at the next `reset --clean`.

The day-0 checks never assume a baseline file. The platform file names one space, the study space, and a check runs in
it as it is. With a baseline-off study space the `listing` check reports unknown on an empty root. The `content_type`
check reports unknown whenever it measured nothing: with no REST data path, as against the templates here, which set no
`[sandbox]` table, and with one when the configured `[sandbox] content_check_file` is not there. The `health` check
composes it. With a REST data path and no file it is unknown, saying why, unless the support set differs from the
declared one, which fails it; a content type that was measured wrong fails it too. With no REST data path nothing is
refused, and its verdict is the support set's alone. The live content test (`EXPCORE_LIVE_CONTENT_PATH`) skips on a
file the space does not hold, so set that variable only where the study space keeps `hello.txt`. `reachability` and
`tokens` read no file of the space, and `read_only` and `round_trip` work on a scratch file of their own: all four are
the same on any space. There is no setting to point one check at another space; a pass that needs the baseline files
runs against a layout whose study space keeps them.

## Running the core against it

`platform.toml` sits in the root, with every path relative to it. For the live tests:

```sh
EXPCORE_LIVE_PLATFORM=/mnt/volume2/expcore-federation/platform.toml EXPCORE_LIVE_CONTENT_PATH=hello.txt \
EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_SLOW=1 uv run pytest tests/live/test_contract.py
uv run python -m expcore_onedata.day0 /mnt/volume2/expcore-federation/platform.toml --out <directory>
```

## The day-0 outage check against it

`provider_outage` (#108, O28) takes a provider down through the fault injector of #17 and reads and calls through
what the agent meets, so it runs against a platform file the root's is not. The file needs a space whose data provider
is not the agent's local provider, with a third provider beside them; a sanctioned `[faults]` table (the injector's,
whose journal and Compose project are required); a `[sandbox]` table with a level that maps to the mount and the
sidecar on the federation's network, derived as `tests/support/mount_live.py` derives the mount test's; `[slots]`, whose
users the sandbox and the tool server lease, each slot's `toolserver` declaring the rights its user holds (`rights`: the
condition's below, since the check leases from the condition's group, O31); and a `[toolserver]` condition whose
allow-list has `get_file_distribution`, `list_space_providers`, `schedule_file_replication` and `get_transfer`, with its
frozen manifest and the rights to schedule a replication. In the default layout `pine` owns the study space and is the local provider, so the root's file
makes the check unknown, saying so. Derive a file with `birch` local, `pine` taken down and `cedar` the mirror (no
value below is a secret; every path in `[faults]` is relative to the file):

```toml
support = { providers = ["pine", "birch", "cedar"], local = "birch" }
placement = { holder = "pine" }

[faults]
sanctioned = true
journal = "fault-journal"              # the injection journal and the federation's lock, beside this file
compose_file = "outage.compose.yaml"   # federation.py compose > <root>/outage.compose.yaml, beside this file
project = "expcore-fed"
command = ["docker", "compose", "--project-directory", "<absolute path of deploy/compose-federation>"]
settle_timeout_s = 600                 # a provider takes minutes to answer again; above the 120 s stop grace
command_timeout_s = 300                # a stop waits for its grace period, 120 s

[faults.services]
pine = "pine"                          # provider name -> Compose service
```

```sh
EXPCORE_FED_ROOT=/mnt/volume2/expcore-federation EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_FAULTS=1 EXPCORE_LIVE_DOCKER=1 \
  uv run python -m expcore_onedata.day0 <derived platform.toml> --only provider_outage --out <directory> \
  [--tool-condition <condition>]
```

Run it outside any sandbox, after `up`, with nothing else using the federation. For each space the check holds the
federation's lock exclusively, starts a sandbox in mount mode and the condition's tool server on a leased slot, and
stops `pine` through the injector: the injection is journalled before the stop, and every run, tool server and mount
that reaches `pine` stops answering until the injector restores it, in a `finally`, counted only by the container's
new identity and `pine` answering REST again. A process killed in between leaves the injection open in the journal:
the next injector start, or the platform's health check, settles it, and `federation.py up` starts what is stopped.
The live test is `tests/live/test_contract.py -k provider_outage` with `EXPCORE_LIVE_PLATFORM` naming the derived file
and `EXPCORE_LIVE_MUTATE=1`, `EXPCORE_LIVE_FAULTS=1` and `EXPCORE_LIVE_DOCKER=1`; it passes only with the mount read
and the four tools called in every space.

**What the live run must measure**, in the terms of #108. Every value is a fact of the report (`facts` of the check),
with `seconds` against `bound_s` and an `outcome`; where a value cannot be had the report says unknown and why, and
nothing is filled in.

| Step of #108 | Where in the facts | What it must record |
|---|---|---|
| What the agent meets | `agent`, `mount`, `knowledge` | That the sandbox started in mount mode and the tool server started, or why not; a declared mount or a knowledge tool not measured makes the check unknown. |
| 1. Baseline, all providers up | `baseline.rest`, `baseline.mount`, `baseline.knowledge` | A file read over REST through the target, the mirror and, cold, the local provider, and through the sandbox's mount: the bytes, whether the hash is the one written, the seconds; the listing; each knowledge tool's answer. Each must serve, or nothing is stopped. |
| The scenario | `held_before` | By the local provider's own block map, who holds how much of each file: the target all of every file, the mirror all of the replicated ones, the local provider nothing. |
| 2. The target stopped | `outage`, `admission` | Through the injector: the stop sent, the identity before (a digest), the service seen down, the federation held exclusively, and that the target answers no REST call. |
| A file with a replica at the second provider | `during.rest.replicated`, `during.mount.replicated` | Whether the read succeeds, the hash against the baseline, how slowly against the baseline, and from where (`during.knowledge.from_where`: the agent's `get_file_distribution` of the very file read, called while the provider is down). |
| A file with no other replica | `during.rest.owner_only`, `during.mount.owner_only` | Whether it errors (REST: status and error id; mount: the errno by name and number and its exception), times out (`no answer`, or `blocked` once it took the bound's blocked fraction; a mount's ETIMEDOUT, Python's `TimeoutError`, keeps its errno) or blocks (the mount's `blocked: true`, and whether the kill took), and for how long up to the bound. This gives a study its read-timeout setting. |
| A directory listing | `during.rest.listing`, `during.mount.listing` | Whole, partial (a listing cut after some pages keeps what it got) or different against the baseline, and the seconds. |
| `get_file_distribution` | `during.knowledge.get_file_distribution` | Per tool call: the outcome, seconds, whether it timed out, its attempts, and the error's wording and the answer's text each as a digest (a SHA-256 prefix of it with every known name and id-like token replaced), its length and the error terms it holds, never as text; what each provider holds when the answer is a REST distribution. |
| `list_space_providers` | `during.knowledge.list_space_providers` | The same, and which of the three providers the answer names. |
| `schedule_file_replication`, to local | `during.knowledge.schedule_file_replication` | Whether the job is accepted for a file with a replica and for one without, or the digest and error terms of the tool's wording when it is not. |
| `get_transfer` | the same, under `get_transfer` | The states the job goes through and when, whether it ends within the bound, and the number of calls; each call classified by its own timing. |
| 3. The restore | `restore`, `after` | The injection cleared and the restore verified (`how` in the injector's words, the new identity's digest, `ready_after_s` until the injector found it serving), the seconds until the target answers REST, the reads of step 2 again through REST and the mount, which must return what was written; the transfers scheduled during the outage; the scratch gone (`cleanup_verified`). |

The result feeds a study's choice between a design that relies on the platform failing over transparently and one
that keeps a replica elsewhere, and its read-timeout setting. Not measured by this check: a provider other than the
data provider going down, an outage that lasts longer than one run of the check, and a tool whose required argument
its schema does not let the check fill (unmeasured, with the reason). If a first live run finds that `docker compose
start` alone does not bring a provider's services back, the report shows a restore that is not verified, the injection
stays open in the journal, and `federation.py up` brings the federation back.

## The sandbox's mount path against it

The sandbox starts its containers outside this project, and inside a container `127.0.0.1` is the container itself, so
no container reaches the providers at the loopback ports `platform.toml` names. The mount path crosses on the
project's own network instead, through `[sandbox.federation]`
([study guide](../../docs/study-guide.md#how-the-sandbox-reaches-the-federation)):

- the oneclient sidecar starts on `expcore-fed_fed`, and on no other network, with `/dev/fuse` and `SYS_ADMIN`;
- it dials the local provider as `pine.expcore-fed.test` on port 443, its domain on that network, which Docker's
  resolver there answers and its certificate names; TLS is verified against `ca/federation-ca.pem`;
- the agent's container stays on the sandbox's own internal network, with no route out, no capability and no FUSE, and
  reads the space only through the read-only mount;
- `pine` fetches what `birch` or `cedar` holds over the project's network, on demand;
- the sandbox never changes or removes `expcore-fed_fed`; its teardown removes the sidecar, which takes it off.

`platform.toml` has no `[sandbox]` table, since its images are pinned on each host and its address pool is the host's
own. The live test of the mount path (`tests/live/test_mount_path.py`) writes a copy with one into a directory of its
own, and needs a pool the host routes nowhere:

```sh
EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_PLATFORM=/mnt/volume2/expcore-federation/platform.toml \
EXPCORE_LIVE_SANDBOX_POOL=<a private IPv4 range> EXPCORE_LIVE_FACTS_OUT=<file> uv run pytest tests/live/test_mount_path.py
```

Run it outside any sandbox, after up: it needs Docker, `sudo` for the sandbox's mounts, and `python:3.12-slim` and
`onedata/oneclient:25.0` on the host (never pulled). It writes seven files into `alpha-space` and deletes them again;
a reset removes anything an interrupted run leaves there. The test docstring lists what it asserts. It also captures
what the sidecar can show of the agent's reads, per file (oneclient's options, its logs, the FUSE connection's
entries); `EXPCORE_LIVE_ONECLIENT_ARGS` passes oneclient further arguments, such as a trace option the first run found.

The other sandbox tests (`tests/live/test_sandbox.py`) need a platform file with a `[sandbox]` table, which this one
lacks, so they are not run against it here.

## The sandbox's REST path against it

The REST path's data gate crosses on the project's network too, through the same table with the zone's name added
(`zone`, #203):

- the gate starts on `expcore-fed_fed` and is then connected to the sandbox's internal network, where the agent reaches
  it, and to no other network;
- it serves and dials the zone as `zone.expcore-fed.test` and the local provider as `pine.expcore-fed.test`, on port
  443, the domains `facts.json` gives as `domain_on_project_network`; its upstream verifies them against
  `ca/federation-ca.pem`, since each service's certificate names its domain;
- the agent resolves those two names to the gate, trusts the run's own CA alone and never joins `expcore-fed_fed`; a
  client that follows the zone to `pine`'s registered domain, which is its name on the project's network, comes back to
  the gate;
- the gate's TLS keys are root's and owner-only on the host, which the gate, root with every capability dropped but
  `NET_BIND_SERVICE`, can read;
- the sandbox never changes or removes `expcore-fed_fed`; its teardown removes the gate, which takes it off.

The live test of the REST path (`tests/live/test_rest_path.py`) writes a copy of `platform.toml` with such a
`[sandbox]` table into a directory of its own and reads one provisioned file through the gate, by its hash; it writes
nothing to the federation:

```sh
EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_PLATFORM=/mnt/volume2/expcore-federation/platform.toml \
EXPCORE_LIVE_CONTENT_PATH=hello.txt EXPCORE_LIVE_SANDBOX_POOL=<a private IPv4 range> uv run pytest tests/live/test_rest_path.py
```

Run it outside any sandbox, after up: it needs Docker, `sudo` for the sandbox's mounts and the gate's keys, and
`python:3.12-slim` on the host (never pulled), the agent's image and the gate's unless `EXPCORE_LIVE_AGENT_IMAGE` and
`EXPCORE_LIVE_GATE_IMAGE` name others.

`day0/` holds the dated reports: the day-0 checks' own (`day0-<UTC>.md` and `.json`) and the facts they do not
measure (`<date>-federation-facts.md`): releases, live test results, the fault test, what listens on the host, the
commands' timings and what is unknown. A new facts file starts from `day0/federation-facts.template.md`: its
deployment table is what `describe` prints (the layout, the root's parent, each service's memory limit), and its
memory table holds each service's peak as `memory` and down read it. A later run on the same date adds its own facts
file, named by what it measured again (`<date>-federation-facts-<what>.md`), and leaves the earlier one as it was.

Scripts beside it measure what the facts files record. `measure_partial_file.py` measures how a file at its final
size with only a prefix written reads back through each provider. `measure_transfer_shapes.py` records, as field names
and types with no value, the calls a per-run reset depends on: an eviction's request and answer, an ended transfer's
record, the space's transfer listing by state with its paging, and whether evicting a directory empties what a mirror
holds of the files in it. `measure_dir_distribution.py` reads the space root's distribution through each provider,
compares it with the sum of its files' physical sizes across a placement and an eviction, and times one file's
distribution read. `measure_granularity_and_eviction.py` records what one range read through the study space's first
mirror places there, and whether an eviction empties it again; `measure_read_windows.py`, built on it, what a read
places when it starts inside bytes already held, is longer than 1 MiB, or follows another. Each takes `--layout PATH`,
works in the study space and leaves it as it found it, and prints one JSON line per step.

`rehearse_shards.py --out DIR [--layout PATH]` rehearses a sharded campaign (#154) on a federation that is up: it
ingests a small dataset into the study space through the platform layer and reads it back, runs shard 1 of 2 of the
example study, runs `reset --clean`, ingests the same data again and runs shard 2, then merges the two shards, which
must accept. It then runs `reset --clean` once more, ingests the data with one file changed by one byte, runs shard 2
again, and merges that with shard 1, which must refuse for the environment identity. The identity is names mapped to
digests (the platform's configuration, the layout file, the study space's ingest manifest); the script never runs `up`
or `down`, writes only below `DIR` and into the study space, prints no secret, and ends with `reset --clean`. `--help`
lists the steps.
