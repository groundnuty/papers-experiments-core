# The HPC host mount against the Compose sidecar, forced proxy (slot 7 for #179), 16 Nov 2026

Does the HPC host mount, which runs the bundled oneclient on a batch node, read exactly as the Compose federation's
mount sidecar does when oneclient is forced to proxy I/O? The same six files were read whole by the same script through
each, from two federations of one layout family. **Outcome: proxy equivalence holds for these six files.** Every field
compared is equal for 6 of 6 files, and both halves report the same effective I/O mode and the same oneclient build.
The result is narrow: six small files, one read each, proxy mode only, no timing comparison (see "What the result does
not show").

Providers and spaces are named by role, in the order of the tables of `deploy/hpc-federation/layouts/pilot-shape.toml`:
`providers[0]` is the reading provider (the agent's local provider, the one the mount dials), `providers[1]` owns
`space-1` (the layout's `study_space`), `providers[2]` owns `space-2` and is `space-1`'s second mirror, `providers[3]`
owns `space-3`, and `providers[4]` supports none of the three. File names are relative to their space. "Unknown" and
"not compared" mean not measured, and a provider that does not support a space reads `unknown`, never 0. No host,
address, account, grant, partition, job id, site path, space id or file id is recorded here, and no token. The record
beside this file, `2026-11-16-slot-7-proxy-equivalence.json`, holds both halves with the site values replaced by roles
or removed; the live-run notes and the two drivers are kept privately, since they name the site.

**Where each claim stands.** The per-file figures, the I/O mode, the oneclient build, the options, the token lifetime and
the step timings are in the record, copied from the two halves' own records. The comparison in the record is computed
from them. The code commits, the Python tags, the layout edits, the manifest counts and the teardown proofs come from
the run notes, not from the record; each says so below.

## Method

- **The same read.** One stdlib script, `read_files.py`, SHA-256
  `1dd0e95d607b10e0e77363d897cd28d78dbb00d97a4960e35f022c5ed65be80c`: it reads each path given as an argument whole, in
  requests of up to 1 MiB on an unbuffered file, and prints one JSON line per file with the bytes read, their SHA-256,
  the seconds from open to the end of the loop (hashing included) and the error, if any. Each half ran it once, as
  `python -I /opt/expcore-read/read_files.py <the same six paths, in the same order>` (`python3` on HPC, under a 240 s
  kill bound; a 240 s bound inside the cell on Compose), in one cell. The Compose record holds the hash of the script as
  read inside the agent, equal to the file's. The HPC record holds no hash of the script; its copy kept with the run
  notes hashes to the same value, and the notes say that file is the one that ran.
- **The same data manifest, from the same layout function.** The HPC `up` writes each space's manifest from the layout's
  baseline plan (`hpcdata.baseline_manifest` over `fedlayout.baseline_plan`); the Compose half built the manifests with
  those two functions from its copy of the layout (notes). By the notes the manifests hold 1,204 files in 2
  directories for `space-1`, 1 file each for `space-2` and `space-3`, and none in the fourth data space and the probe
  space. The six entries read here (size and SHA-256) are equal across the halves: 6 of 6, from the two records.
- **The same before-maps.** The Compose half refused to mount unless every provider's block map of every file, before
  the read, equalled the HPC record's, the `unknown` strings included. Its own record said so (`before_equal_to_hpc`
  true, no difference; `comparison.before_maps_equal_recorded_by_compose_half` in this file's record), and the
  comparison there recomputes it from the two halves' own maps. A fresh `up` of the layout gave them without any
  placement or eviction first (notes): `providers[0]` held `replicated.bin` whole (the baseline's replica) and nothing
  of the other five files, and each owner held its files whole.
- **Per file, what each half measured:** the bytes the script served, their SHA-256, equality with the manifest, and
  each provider's block map before and after the read, read through `providers[0]` with the admin credential and
  settled (two consecutive equal polls, both before and after). Gained and lost bytes are the maps' difference.
  "Served" is the reader's own byte count, not a gate's or a provider's counter.
- **The effective `io_mode`,** read from oneclient's own log by the sandbox's instrument: declared `proxy`, effective
  `proxy`, agree true, with the log lines `Forced direct IO: 0` and `Forced proxy IO: 1`, no contradicting line.
- **The HPC half** (about 20:11Z): the core's own `HostMount` ran oneclient from the pinned bundle on a node of a small
  held job of its own (4 cores, 8 GB, no Onedata service on its node, as the agent's node would have), beside the
  federation's held job. An Apptainer cell (`--containall --cleanenv --no-home`, the mount bound read-only) ran the
  script. The mount is the user's own and read-only, with no `allow_other`.
- **The Compose half** (about 22:02Z): the core's own `DockerSandbox` in mount mode started a sidecar container that
  holds `/dev/fuse` and `SYS_ADMIN` and runs oneclient, and an agent container with neither, which read the
  share. The script was delivered as the sandbox's own read-only input.
- **Both halves** minted a data-path token for the data spaces and the `oneclient` interface, gave it to oneclient from
  a file that was gone once oneclient started (0600 on HPC), and revoked it at the end (generation 1 to 2). No log file
  held the token; it was not in oneclient's command line (HPC) or in either container's `docker inspect` (Compose).

## Code and builds

| | HPC half | Compose half |
|---|---|---|
| Core commit | `a50cd83802701814a67d88dfb637e5c221f47816` (main at #333; the site checkout was byte-identical to it, 1,196 of 1,196 files by SHA-256) | `655ea0a66e034390197c62c393209b879961ced0` (main at #336, a detached worktree) |
| Mount code | `HostMount` (`sandbox/apptainer/hostmount.py`) | `DockerSandbox` mount mode, the sidecar |
| oneclient | 25.0, FUSE 3.2, build id `a2f1d9345615360e465c1e43d24046df407c6254`, from the relocatable bundle, tar SHA-256 `e959d9aa63c2889e4c1839a60c1471696136da2ac86f935810b57e9b2014a6af` | 25.0, FUSE 3.2, the same build id, from the image `sha256:aff60c5ff78c99f4cb0c5d0acb5392b37a1fdd3cdd73b73bca9b640d067a51d8` (its id, pinned by digest and never pulled) |
| Agent | a SIF built from `python:3.11-slim` (notes) | `python:3.12-slim`, `sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf` |

- **The same oneclient build.** The two records give version 25.0, FUSE 3.2 and one GNU build id. The HPC build id is
  read by `HostMount` from the bundle's binary; the Compose sandbox's metadata has no `oneclient_build`, so the slot's
  driver read the build id from the binary in the sidecar (16,143,216 bytes, read by `docker exec`) and the version from
  `--version`. That the bundle is built from the Compose image rests on its name (`oneclient-bundle-aff60c5ff78c.tar`,
  the image id's prefix) and its pin (notes), and on the equal build id; the record has no measured image digest for the
  HPC half.
- **The code between the two commits.** Main moved 32 commits from the first to the second. A diff of the files each half
  reads through is empty: under `src/expcore_onedata/`, `sandbox/apptainer/hostmount.py`, `mounthook.py` and
  `oneclient_bundle.py`, `sandbox/oneclient_start.py`, `oneclient_args.py`, `iomode.py`, `oneclient_log.py`,
  `sandbox.py`, `containers.py`, `mountproof.py` and `mountwatch.py`, and `config.py` and `tokens.py`; under `deploy/`,
  `hpc-federation/hpcdata.py`, `oneclient_bundle.py` and `layouts/`, and `compose-federation/fedlayout.py`. Ten files
  under `sandbox/` did change, all on the Apptainer cell's side or in `completion.py`: the cell's `resolv.conf` and
  `nsswitch.conf` binds, the hidden-memory check and the process-visibility instrument (O56). The mount's start, its
  argv, the I/O-mode reading and the Apptainer cell's command-line builder (`argv.py` gained two constants and their
  text) are unchanged, and the HPC cell ran without those binds. None of the changes is on the mount's read path.

## Steps

### 1. The HPC half (the first half of slot 7, 16 Nov, about 20:11Z)

- **The federation:** a zone and five providers on six nodes of one held job (`pilot-shape.toml`'s family, no agent
  node), `up` passed in about 3.5 minutes, and its link check passed: 10 provider pairs, 65,536 B each (notes).
- **The mount job** (7 s in the queue, a step of 18 s): the cell image was built from `python:3.11-slim` in 10 s. The
  mount started in 0.939 s (the wait for the FUSE mount 0.203 s). Host mount options: `ro,nosuid,nodev,relatime`, and
  `ro,user_id=<uid>,group_id=<gid>` (no `allow_other`); the cell's uid is the mounting user's (the record compares
  them). The guard's verdict: clean. oneclient's process held 11 threads and about 49.5 MB resident.
- **The read:** the cell exited 0 in 0.315 s with nothing on stderr; six lines, no error.
- **The stop:** complete (the watch closed, the unmount, oneclient gone, the token gone, the bundle's copy removed): 0
  mounts and 0 tagged processes left. The job was cancelled by its own id.

### 2. The Compose half (the second half of slot 7, 16 Nov, 22:01-22:02Z)

- **The federation:** a copy of `pilot-shape.toml` with only the project and domain, the ports, the memory limit (3 GiB
  per service, since six at 6 GiB would not fit the host's free memory) and the platform template changed (the same
  providers, spaces, owners, mirrors and principals; the template is the HPC half's rendered one in the Compose form,
  with no `[faults]` or `[tokens]` table, and the mount's `[sandbox]` table derived as the live mount test derives it).
  The host was shared with another federation, left untouched. `up` passed in 446 s, and every one of the 10 provider
  pairs transferred (65,536 B each).
- **The sandbox start:** 14.098 s (the mount's own wait 3.189 s). The agent was proven read-only at its root, the mount
  and the input; it holds no capability and no FUSE device. oneclient is the sidecar's first process (17 threads,
  about 54 MB resident).
- **The read:** the cell exited 0 in 2.873 s; the cell's own view of `/data` is a slave of the sidecar's mount, with
  `allow_other` and the user and group 0 of the sidecar's oneclient.
- **The stop:** complete (the unmount, the sidecar stopped and removed, the agent removed): 0 mounts, containers and
  networks left. The host's containers, networks, volumes, images and mounts equalled the snapshot taken before the start,
  by id (notes).

## Result

Both halves: read exit 0, effective `io_mode` proxy, the same build id. Per file, every compared field is equal across
the halves (the manifest entry, the listed size, the served bytes, SHA-256 and error, and for every provider the before
and after maps, the before and after bytes, gained and lost):

| File | Size (B) | Served (B), both halves | SHA-256 = manifest | providers[0] before -> after (B) | Gained (B) | Read s, HPC / Compose | Result |
|---|---|---|---|---|---|---|---|
| space-1/hello.txt | 64 | 64 | `de6f358f3e5d...` | 0 -> 64 | 64 | 0.018 / 0.368 | PASS |
| space-1/replicated.bin | 1,048,576 | 1,048,576 | `17ecc3c48861...` | 1,048,576 -> 1,048,576 | 0 | 0.015 / 0.144 | PASS |
| space-1/samples/sample-1.bin | 65,536 | 65,536 | `3e1b24c9d1fc...` | 0 -> 65,536 | 65,536 | 0.011 / 0.147 | PASS |
| space-1/samples/sample-2.bin | 262,144 | 262,144 | `538348f9a097...` | 0 -> 262,144 | 262,144 | 0.023 / 0.176 | PASS |
| space-2/beta.txt | 64 | 64 | `c86500ab675d...` | 0 -> 64 | 64 | 0.011 / 0.167 | PASS |
| space-3/gamma.txt | 64 | 64 | `36807ddca086...` | 0 -> 64 | 64 | 0.011 / 0.143 | PASS |

- **Placement.** Five of the six files were absent from `providers[0]` and each arrived whole there (its after map is
  `[[0, size]]`) on both halves. The sixth, `replicated.bin`, was already whole on `providers[0]` and gained 0 on both, so
  it shows byte equality and nothing about placement. In every row the provider that held the file before still holds
  it, unchanged: `providers[1]` for `space-1`, `providers[2]` for `space-2`, `providers[3]` for `space-3`. `providers[2]`
  holds 0 of `space-1`'s files before and after; every provider that does not support the file's space reads `unknown`.
  Lost bytes are 0 wherever they are known.
- **The verdict: proxy equivalence holds.** A forced-proxy read through `providers[0]` places each whole file it lacked
  on `providers[0]` and changes nothing on the owners, on the HPC host mount exactly as through the Compose sidecar, with
  the same bytes served and the same SHA-256 as the manifest.

## Differences outside the comparison, each with its cause

1. **Read time.** 0.011-0.023 s per file on HPC, 0.143-0.368 s on Compose; the cell took 0.315 s and 2.873 s.
   **Not compared, and the cause was not measured.** The gap is there for `replicated.bin`, which `providers[0]`
   already held whole (0.015 s against 0.144 s), so no block moved on either half and a transfer is not the whole of it.
   The hosts differ (a shared VM with another federation resident, against nodes of their own), and the Compose path
   crosses Docker's bridge from the sidecar to the provider (notes). The Compose cell is also a launcher that starts the
   script, where the HPC cell runs it directly.
2. **The oneclient options.** HPC `-o ro --force-proxy-io --config /dev/null --disable-log-buffering`; Compose
   `-o ro,allow_other --force-proxy-io --log-dir /var/log/oneclient --config /dev/null --disable-log-buffering`.
   `allow_other` is **by design**: the Compose agent is another container (a uid that is not the sidecar's, root), so
   the sidecar's mount must be visible to it, and the host view in the record shows `allow_other`; the HPC mount is the
   user's own, read by a cell of the same uid, and `HostMount` refuses `allow_other` (the node's FUSE has no
   `user_allow_other`; `hostmount.py`, its module docstring). `--log-dir` is a difference of the record only: `HostMount`
   passes `--log-dir <run>/oneclient-log`, but its record keeps the declared options without that path, while the
   sandbox's record names the container's fixed directory. The mode flag, `--config /dev/null` and
   `--disable-log-buffering` are the same.
3. **The token lifetime:** 6,839 s on HPC and 2,399 s on Compose. A token's lifetime is asked for as the run's whole-time
   ceiling plus `[tokens] margin_s` plus the 300 s mint headroom (`tokens.py`, `config.py`). With the same ceiling of
   1,200 s (notes), the HPC platform's margin of 5,340 s (derived from its stop bound; notes) gives 6,840 s, and the
   900 s default, which the Compose template does not override, gives 2,400 s; the records' figures are one second under
   these sums. Either outlasts the read by orders of magnitude, and the read does not depend on it.
4. **Start times:** 0.939 s on HPC (the wait for the FUSE mount 0.203 s) against 14.098 s on Compose (3.189 s).
   `HostMount` starts oneclient on the node and watches it; the Docker sandbox creates a network, a sidecar and an
   agent, and proves the root, the mount and the input read-only from inside (notes). Not compared.
5. **The agent's Python:** 3.11 on HPC, 3.12 on Compose (the images' tags, notes). The script is stdlib only, and the
   bytes and hashes are equal.
6. **Mapped files.** HPC's oneclient mapped 105 files inside the bundle's copy and 2 outside it, `/var/db/nscd/hosts` and
   `/var/db/nscd/passwd` (the node's name-service cache, read-only); the Compose sidecar's mapped 106 inside
   `/opt/oneclient` and none outside, since the image runs its own libraries in its own container (notes). The counts
   inside were not examined, and the bundle runs the binary through its loader (the process is named for the loader), so
   the two are not like for like.
7. **The mount endpoint's port** in the record is the driver's own field: 8443 on HPC, and on Compose the layout's
   loopback port, which the sandbox replaces by the provider's name on 443 for the sidecar. Both mounts used TLS with the
   federation's CA file.
8. **Record shape.** The per-file keys are identical. The run-level keys differ by what each backend has: the HPC record
   holds the cell's Apptainer command line, the mount guard's verdict and the tagged processes left; the Compose record
   holds the before-map gate, the read script's hashes, and the containers, networks and mounts left.

## What the result does not show

- **Direct I/O.** Neither half ran with `--force-direct-io`; this is proxy mode only. Whether the HPC host mount reads
  the same in direct mode is untested.
- **Timing.** One read per file and half, no repetition, two different hosts: the seconds are recorded and not compared,
  and nothing here supports a claim that the paths are as fast, or slower.
- **Scale.** Six files, the largest 1 MiB, so each fits in the script's first request. Larger files, partial and random
  reads, concurrent readers, and a read that fails are not covered; a whole-file placement is shown for files this small
  and says nothing about a large one.
- **One run per half,** on two federations brought up separately about two hours apart, not one federation read through
  both mounts. The layouts, the before-maps and the data are equal; the hosts and their load are not.
- **The sixth file's placement.** `replicated.bin` was held whole before the read on both halves, so it adds no evidence
  about placement.
- **Any data gate, agent or model.** The cell held no gate and ran no agent: this is the mount's read path alone. The HPC
  mount guard's watcher is reported only by its verdict (clean).
- **The Compose half's own record of the build.** The Compose sandbox does not record which oneclient served a run; the
  build id above was read from the sidecar by the slot's driver, so a Compose run's own record cannot yet show it.

## Findings

1. **The HPC host mount and the Compose sidecar read alike in forced proxy.** Same bytes, same SHA-256 as the manifest,
   same placement and the same effect on the owners for the six files, from the same before-maps, with one oneclient
   build.
2. **`allow_other` is the one argv difference that matters, and it is by design:** it exists only where the reading cell
   is another uid than the mount's.
3. **The bundle's oneclient maps the node's name-service cache** (`/var/db/nscd/hosts`, `/var/db/nscd/passwd`, read-only),
   which, by the notes, a lookup of the provider's name goes through; the Compose sidecar maps nothing outside its
   image. A minor difference in the bundle's claims, not in the data.
4. **Open items:** a timing comparison needs its own slot with repeated reads; direct I/O has not run on the HPC mount;
   the Compose sandbox's metadata should name the oneclient build (`oneclient --version`, the binary's build id and the
   image id) as the HPC mount's record does.
