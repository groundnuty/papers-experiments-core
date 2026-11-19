"""shards.py: one shard of a campaign per HPC federation (#154, milestone M5), from launch to merge.

    <python> -I -B deploy/hpc-federation/shards.py plan    --manifest shards.toml
    <python> -I -B deploy/hpc-federation/shards.py lines   --manifest shards.toml
    <python> -I -B deploy/hpc-federation/shards.py step   --study DIR --index I --count N --label L --tmpfs DIR \
        --results DIR --core-src DIR --job J --step-name S  # inside the step on the sandbox node; `lines` writes it
    <python> -I -B deploy/hpc-federation/shards.py collect --manifest shards.toml --into DIR [--run]
    <python> -I -B deploy/hpc-federation/shards.py collect --roots-only --root ROOT --into DIR  # one root, no plan
    python3 -I -B deploy/hpc-federation/shards.py verify-copy ARCHIVES_MANIFEST LOCAL_DIR --manifest-sha256 HASH
                                                        # on the operator's host, after the pull, before `down`
    <python> -I -B deploy/hpc-federation/shards.py step ... --archive-left  # an out/ a step could not archive
    # a step's line may end in: --pre-step REL --pre-step-sha256 HEX --pre-step-timeout S  (`lines` writes them)
    #   and: --expect-render-identity REL --expect-render-identity-sha256 HEX  (the render the plan expects)
    #   and, for each variable the manifest's pass_env declares: --pass-env NAME, its NAME=VALUE among the env -i words
    <python> -I -B deploy/hpc-federation/shards.py resume-line --manifest M --label L --job J --node N
    <core python> -I -B deploy/hpc-federation/shards.py sweep --tmpfs DIR --root ROOT  # hpcfed.py down runs it

Every Python here runs with `-I -B` (B1, live slot 5: a node step's imports wrote bytecode into the core's interpreter
under the account's shared home; `-I` ignores PYTHONDONTWRITEBYTECODE, so only the flag stops it).

**Where the dispatcher runs.** One shard runs per federation, as one job step on that federation's sandbox node: the
Apptainer sandbox's data gate listens on that node's loopback and its cells are that node's `apptainer exec`s, so the
dispatcher (`python -m expcore run STUDY --shard i/N --shard-label L`) must run there, never on the login node. A step
is launched from the login node alone (srun cannot start one on a compute node), as the federation's own services are
(`DESIGN.md`): `lines` prints each shard's `srun --jobid=<job> [--het-group=<g>] --exact ... --chdir=/ /usr/bin/env
-i <allowlist> <python> -I -B shards.py step ...`, to run detached (`setsid nohup ... &`). The job and the component are
the state file's `sandbox` record's: a component of the federation's heterogeneous job (`het`) or a job of its own
(`separate`). Nothing here runs srun.

**How it reaches its federation.** The step's environment is an allowlist: `PATH`, `LC_ALL`, and the federation's
`EXPCORE_FED_ROOT_PARENT` and `EXPCORE_FED_ROOT`, and no home: `HOME`, the XDG base directories, Apptainer's, uv's and
pip's directories under a path nobody can make, Python's user site off and no git configuration (`home_env`), so no
tool falls back to the account's home, which every job and agent of the account shares. The dispatcher's are in the
step's own directory on the tmpfs, made private there. `step` copies the federation's `<root>/platform.toml`, where its
`up` rendered it with every path absolute into the root (secrets, CA, fault journal) on the filesystem the nodes see,
into its copy of the study beside `study.toml`, where the study's plugin loads it: a copy, never a link out of the
study, and never a path of the federation's in `study.toml`, whose bytes and hash are every shard's. A study that holds
a `platform.toml` of its own is refused. No secret is in the environment: a model's key comes from its key file, which
the operator's `home_files` puts in that home (below). When that file declares the Apptainer sandbox, `step` makes its
`work_root` (mode 0700) before the dispatcher, which the sandbox will not start without; it refuses one that is a
link, not this user's, open to others, or not under `--tmpfs` (the node's tmpfs: never the shared filesystem).

**The operator's key files (`home_files`, O63).** A study's `models.toml` names `key_file = "~/..."` paths, and the
dispatcher's HOME is a private directory on the node's tmpfs (above), so `~` resolves into an empty home and every
model call would fail at its first request. The manifest's optional `home_files = "/abs/path"` names a directory of
the operator's on the shared filesystem laid out as it would sit under a home (`.secrets/<...>/key`). `plan` and
`lines` read it (`read_home_files`) before anything is printed, and each line carries `--home-files PATH` (before any
`--pass-env`), the path alone (as the manifest's other paths ride on it; `plan` prints each line, so the path is in
its `argv`, and says `home_files: true`). The path is absolute with no `.`, `..`, empty or control-character part and
no link on the way (each part is opened with `O_NOFOLLOW`, and a part above the tree only needs search permission, so
the operator gives the resolved path), and the tree is outside the study, which the step copies whole; the directory
and every directory in it is this user's with no group or other bit (mode 0700 or stricter), every other entry is a
regular file of this user's with no group or other bit and no execute bit (0600 or stricter): a link, a file that is
not regular, one of another user's, one open to others, more than 64 files, more than 64 directories, more than 1 MiB
in all, a tree more than 8 directories deep and a tree with no file are refused, and the refusal names `home_files`
and why, never a path or a name inside. `step` reads the tree itself, once, as one of its first acts (the same checks,
on the descriptors it reads the bytes through: nothing is checked and then read again, and nothing is pinned on the
line, so a key the operator rotated since `lines` is the one that arrives), refusing with `PRE_STEP_REFUSED` (73) and
nothing made, and holds the bytes. After its home exists and before the dispatcher starts, it writes them there:
directories 0700 and files 0600 (made under a umask of 077 and `fchmod`ed), with `O_EXCL` and `O_NOFOLLOW` below a
held descriptor of the home; a directory the step already made (`.config`) is merged into, a file that would replace
anything is refused (73, before the dispatcher, the directory removed). A study's `~/...` then resolves to the
operator's files in the dispatcher and its children. Nothing of the files is recorded: `step.json` holds `home_files:
true` and `home_files_count`, and the archive, its manifest, the snapshots, the journals and what the step prints hold
no name, size, hash or content (the step says how many files, a count). The copies live in the step's own directory on
the tmpfs and go with it, and sooner: once the dispatcher and the post-run script have ended, before the archive,
`step` removes the home; on every way out its `finally` removes the home explicitly before the directory
(`remove_tree`), with the stop signals (TERM, INT, HUP, QUIT, USR1, USR2, ALRM: `STOP_SIGNALS`) held back while it
does, so none interrupts it; an archive that cannot be written keeps the directory without the home; and one of those
signals that arrives while the files are staged, before the dispatcher's relay is installed (all but TERM and INT also
while it runs: the dispatcher dies with its step) ends the step through that cleanup (exit 128 + the signal; a stop
that lands before the signals are held has the cleanup finished first). **A cell cannot reach them** (the agent's
cells run as this user too): `apptainer exec --containall --cleanenv --no-home --no-mount hostfs,bind-paths,cwd`
(`argv.py:86`, `NO_MOUNT` at `:41`) gives a cell its own PID, IPC and mount namespaces (`argv.py:16`) and none of the
node's file systems or the driver's home, and every bind a cell gets has a source under the run's own directory
`work_root/<run id>` (`sandbox.py:586-606`; the mount's paths are refused unless they lie inside it,
`sandbox.py:435-443`, and the inputs are copies made there, `sandbox.py:798`), where `work_root` lies strictly under
`--tmpfs` (`make_work_root`) and the dispatcher's home is `--tmpfs/expcore-shard-<label>.<random>/home`, a sibling;
neither it nor the operator's source tree is bound or under a bound path, and the cell's own PID namespace hides the
dispatcher's `/proc/<pid>`. A step killed outright (KILL, the walltime, the node) leaves its directory, and the copies
in it, on the node's private tmpfs, which the site does not clear at the job's end, until `--resume` or `sweep`
removes it, as it leaves everything else; one killed after its dispatcher ended (a kept `archiving` directory, whose
out/ waits for `--archive-left`) has its home, and only its home, removed by `sweep`, by `--resume`'s look at the node
and by `--archive-left`, once its step is shown gone (`Left.gone`, the proof a whole removal takes; a link is
unlinked, never walked). So every campaign ends with `down` (or `shards.py sweep`) on every node a step ran on. With
no `home_files` nothing is read or written and the line, the plan and every record are what they were. Not covered:
the fleet's shard settings (`fleet.py`) do not take the key.

**The hop to the group's LLM fleet (`fleet_hop`, O93).** The manifest's optional `fleet_hop = { login, socket, key,
known_hosts, local_port }` (also a setting of the fleet's, for the elastic worker) makes each step run one ssh child
that forwards 127.0.0.1:`local_port` to the fleet front's unix socket on the login node (`fleethop.py`, which says
exactly what it runs). It is started after the key files and before the dispatcher; if its port does not accept within
60 s the step ends with `FLEET_HOP_FAILED` (76) and a message naming `fleet_hop` only, with no dispatcher. While the
dispatcher runs the hop is started again if it exits (no more than once per 10 s), and it is killed once the
dispatcher has exited and on every way out, in a `finally` with the stop signals held, as the key files are. The line
carries the table as one `--fleet-hop` JSON argument; the plan, the step's owner record and its words say `fleet_hop:
true` and the restart count, never a host, a path or a port. With no `fleet_hop` nothing changes.

**The operator's own variables (`pass_env`, O63).** The one addition to that environment is what the manifest
declares: variables of the operator's the study needs in its dispatcher's environment, such as the operator's half of a
fault sanction (`pass_env = ["NAME", ...]`, names only). `plan`, `lines` and `resume-line` read each name's value from
the operator's environment (`pin_pass_env`) before anything is printed or recorded, and refuse, naming the variable and
never the value, a name that looks like a secret (KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL, AUTH) or is one the
step sets itself (`PASS_ENV_OWN`, anything of EXPCORE_ or PYTHON) or changes how a program is loaded or started
(anything of LD_, DYLD_, PERL5, RUBY or NODE_, GLIBC_TUNABLES, BASH_ENV, ENV, IFS, PS4, SHELLOPTS, PROMPT_COMMAND,
GCONV_PATH, LOCPATH, NLSPATH), a variable that is unset and a value that is neither a plain word (`[A-Za-z0-9._:=,+-]`,
at most 128) nor an absolute path of `[A-Za-z0-9._/-]` with no `..` or `.` part and no `//` (`pass_env_path_ok`). Each
line pins the value as an `env -i` word `NAME=VALUE` and declares it as `--pass-env NAME`; `step` (`check_pass_env`,
its first act) passes the dispatcher exactly those, refusing with `PRE_STEP_REFUSED` a declaration its line gives no
plain value for, and records each name and value in `step.json` and the archive's manifest. The post-run script runs in
the dispatcher's environment and sees them; the guard does not. `step` never reads the manifest, so `collect` refuses a
shard whose manifest lacks a declared name or holds an undeclared one (`pass_env_problem`): a line that dropped a
declaration with its word. The programs that build the study's plugin at plan time, to take its campaign fingerprint
(`plan`, `lines`, `resume-line`, the fleet's `plan`), are given the same pinned variables and nothing else of the
operator's (`pinned_pass_env`), so a plugin that needs one can be fingerprinted.

**One value for a shard's whole life.** A first step that passes variables records each name and value once, in
`<results>/<label>/pass-env.json` (`record_pass_env_origin`: whole, by an exclusive link, mode 0600, once it holds the
shard's running mark; nothing is written when it passes none, and a record over `PASS_ENV_ORIGIN_MAX` bytes, which
its reader refuses, is refused by the writer first, so the step is). Every later step compares what it passes with that
record before anything is made or launched (`check_pass_env_origin`): `step` as its first act, whether or not it is a
`--resume` (a first step killed before its first snapshot leaves the record and no archive), `resume-line` before it
asks Slurm anything or records its node, and the fleet's `step`. A declared variable whose value differs, one the first
step did not pass and one it passed that the line no longer carries are refused, naming the variable and never either
value (status 73 for `step`); a record that cannot be read as the shard's is refused too, never taken for none. With
no record and no results (an archive or a snapshot) the shard has had no first step: nothing is compared. A shard with
results and no record either had a first step that passed nothing or lost the record: its latest archive's manifest says
which (`archived_pass_env`: names and values; none when there is no manifest, as with a snapshot alone or a first
archive killed before its manifest; a refusal when it is there and cannot be read as the shard's), and a step is
compared with that, so a deleted record lifts no rule; a step that passes a variable the manifest does not hold adds
one mid-shard and is refused the same way. `plan` and `lines` print first steps' lines and are not compared.

**One campaign across federations (C06).** A space is the same experimental condition across federations only when
its declared identity and its data identity match, and the campaign fingerprint holds a space's data identity only when
its federation's `platform.toml` declares it (`data_manifest`, which `up` renders and checks): otherwise it holds the
space's Onezone id, which every federation's `up` makes anew, and the shards never merge. So `plan` and `lines` refuse
a manifest of more than one federation whose platform files leave a space's data undeclared, naming each shard and
space, before anything runs.

**The study's own data is verified before a shard is planned (O57).** A space whose data comes from the study's ingest,
after `up`, is checked by `hpcfed.py verify-data` (every file's path, size and SHA-256 read back through its owner and
compared with the manifest it declares), which records a marker in the federation's state file. `plan` and `lines`
refuse a federation whose ingested spaces have no marker, or whose marker is for another manifest than the one `up`
recorded or the platform file names now, naming the shard and the spaces; so does a state with no record of them.

**The study's own refusals: a pre-step guard.** A study that refuses a campaign it must not run (one worker, each
model's cap exactly 1, a federation of its own) can have every step assert that again at its start, before anything is
made. The manifest's `pre_step` names a script of the study, a path relative to the study's root: refused when it is
absolute, has an empty, `.` or `..` part, or leads through a link (a part, or the file itself), is not a regular file or
is over 1 MiB. `plan` and `lines` read it and record its SHA-256 (`plan` prints it with the guard's path and bound) and
each line carries `--pre-step REL --pre-step-sha256 HEX --pre-step-timeout S`; no line is made from a plan that has not
pinned it, and with no `pre_step` the line and the `plan` output are what they were. `step` does this before anything
else, before the core check, before a `--resume` removes a killed step's directory, and before it makes the sandbox's
work root, its own directory, the journals or an archive. It opens the study's root (`--study`, the stamped copy, since
its tmpfs copy does not exist yet) once and holds it: refused unless it is a directory of this user's that no group or
other can write to; reads the guard part by part below that descriptor (no link followed), refuses a file whose
SHA-256 is not the one on its line, and runs those same bytes, never the file read again, as
`<python> -I -B - <label> <campaign.toml>` (the script on the interpreter's stdin), with the held root as its working
directory and `campaign.toml` named through it (a path under `/proc/self/fd`, valid in the guard alone), so a root
renamed after the check is never the one it runs in. Its environment is `ALLOWLIST` and the step's home below a path
nobody can make, and it runs in a session of its own, killed with everything it started when it does not end within
`pre_step_timeout_s` (60 s by default, above 0 and at most 600), or when the step is told to stop. A non-zero exit, a
signal, a timeout, a hash mismatch or a guard that cannot be read refuses the step with `PRE_STEP_REFUSED` (73), naming
the guard and why, and nothing is made. A step that passes records `{path, sha256, exit_status}` as `pre_step` in
its `step.json` and in its archive's manifest. The line carries the guard, so a line run as it was printed runs it; a
hand-edited line that drops the three words runs without it, and `--archive-left`, which starts no work, ignores them.

**The study's own check of its results: a post-run script.** Symmetrical, for the check that needs the live
federation (the study's own owner check of what the shard left in it). The manifest's `post_run` names a script of
the study (the same path rules; `post_run_timeout_s`, 300 s by default, above 0 and at most 1800), `plan` and `lines`
record its SHA-256 and each line also carries `--post-run REL --post-run-sha256 HEX --post-run-timeout S`. `step` reads
and hashes it with the guard, before anything is made (one that is a link, missing or not the plan's hash refuses the
step with 73, so a campaign is never spent on a check that cannot run), holds its bytes, and runs them once the
dispatcher has exited 0, while the federation is up and before the archive is written: `<python> -I -B - <label>
<out/> <dispatcher's exit status>`, the study copy's root on the node's tmpfs as its working directory, in the
dispatcher's own environment (`ALLOWLIST`, the step's home and TMPDIR), in a session of its own, killed at its bound.
Its exit 0 makes the shard's results final. A non-zero exit, a signal, a timeout, or a dispatcher that did not exit 0
(the script is not run) leaves them NOT final: they are archived all the same, so nothing is lost, with `final: false`
and its reason in the manifest, a `NOT-FINAL.json` in the archived `out/`, and the step ends with `POST_RUN_FAILED`
(74) unless the dispatcher's own status was not 0. The record of the script (`post_run`: path, SHA-256, whether it ran,
its exit status, `final`) is in `step.json` and the manifest. `collect` refuses a not-final shard, naming it and its
reason, and, when the plan declares a post-run, a shard whose manifest does not carry that script's hash with `final`
true (a line that dropped the words); the core's `merge` refuses an `out/` holding `NOT-FINAL.json`. `--resume` starts
from such an archive as from any and decides afresh; `--archive-left` archives a kept `out/` as final only when the
step's record says its script passed.

**Which render a shard runs against: the render identity (O74).** `hpcfed.py up` writes `render-identity.json` in the
federation's root (`hpcidentity.py`): mode 0600, canonical JSON, nothing but SHA-256s and a version: the template as it
was read before it was rendered, the rendered platform's fingerprint, each pin the platform holds (the agent's SIF,
the egress shim, the egress collector, oneclient's bundle, the REST layout's 443 mapper), and, when the site declares
`HPC_STUDY_ARTIFACTS`, the digest of that directory's tree (`study_artifacts_tree_sha256`; no path of the directory). A
manifest that declares `expect_render_identity`, a path
of the study (`render-identity.expected.json` at its root, say: a federation's record copied there), makes every line
carry `--expect-render-identity REL --expect-render-identity-sha256 HEX` (`plan` and `lines` read the file by the
guard's path rules, refuse one that is not a well-formed record, and record its hash; with none declared the line and
the plan are what they were). `step` compares first, before the pre-step guard and before anything else is made: it
reads the expected file below the held study root, shows it is the file the plan hashed, reads the federation's own
record once from `$EXPCORE_FED_ROOT` (below that root, no link followed, a root of this user's that no group or other
can write to), and refuses with `PRE_STEP_REFUSED` (73), naming the keys that differ and never a value, when they are
not equal key by key, when either is not well formed, or when the federation has no record, an unreadable one, or no
root named. Whether or not one is expected, a step that has the federation's record writes it, as validated (its
canonical text: the bytes `up` wrote), into its `out/study-state/render-identity.<step id>.json` at its start, once a
resume has restored `out/` and before the dispatcher starts, a file of this step's own (UTC time and four random
bytes), so a snapshot, a step killed mid-shard, the archive and a post-run script all carry it, and a resumed shard
keeps the identity each of its steps ran under; a step that cannot write it does not start. A federation whose `up`
wrote none (before O74, or a layout with no platform template) and a plan with no expectation run as they did, and
write none. A dispatcher that leaves nothing but the record leaves no `out/` to archive. The record
is what `up` wrote: a `platform.toml` edited by hand afterwards is not seen, and a line edited to drop the two words
runs unchecked.

**The node's tmpfs is the node's, not the job's.** Each step's directory there (`expcore-shard-<label>.<random>`)
records its step (`step.json`: its pid and start time, its shard, its federation's root, its results directory, the
archive it started from, and its phase): `running` while its dispatcher runs, `archiving` from the moment it ends
(swapped in by rename from a record written at the start, so a full tmpfs cannot stop it), when its out/ is whole and
the only one until archived. A step killed outright leaves its directory. Only one killed while `running` is ever
removed, and only once its process and every process naming the directory (its dispatcher, which outlives a wrapper
killed alone) are shown gone (the dispatcher dies with its step and records its own end, `DISPATCHER`): `--resume`
removes its own shard's of its own campaign (spec, root and results) and refuses while one runs, and `sweep`, which
`hpcfed.py down` runs on the agent's node, removes its federation's. A directory whose dispatcher ended and that was not
archived (`archiving`; marked `ARCHIVE-FAILED.json` when the mark could be written), or whose record cannot be read, is
never removed: `--resume` refuses while one of its own is there, `sweep` names it, and `step --archive-left` archives it
with the same manifest and that step's exit status, then removes it, but only once that step is gone and while the
published archive is still the one it started from.

**The study and its store.** The study is one stamped copy at the pinned commit on the shared filesystem. `step` copies
it to the node's tmpfs and runs it there, since the campaign's store (SQLite, and its `campaign.lock`) must not live on
a network filesystem; it checks first that the interpreter imports the core it was given (`--core-src`), relays SIGTERM
to the dispatcher, and ends with the dispatcher's exit status.

**The core must name its code (C06).** `expcore run` refuses to start on a core whose commit is unknown (a copy without
`.git` and without a build identity: what `git archive` leaves) or whose installed source differs from its build's
(stale), so a shard on one would reach its node, start and stop there. `plan` and `lines` ask the manifest's `python`
which core it imports (a process of its own, `-I`, under the step's PATH and no home), and `step` reads the identity of
its own interpreter, which the dispatcher is, before anything is made; each refuses an unknown or stale core, saying to
use a git checkout of the core or a wheel built with its build identity (`hatch build` writes
`expcore/_build_identity.json`). A dirty core is not asked here: the run refuses it with its own message.

**How a shard comes back.** Once the dispatcher has exited (its lock released), `step` writes the copy's `out/` whole,
`campaign.lock` included, to `<results>/<label>.out.tar` with a manifest beside it (`<label>.out.json`: its SHA-256,
bytes, files, the shard's spec and the dispatcher's exit status), each written whole and renamed into place, both mode
0600 whatever the umask (the archive holds `out/logs`; a results directory the step creates is 0700), and removes
its tmpfs directory; an archive that cannot be written leaves that directory in place, marked (exit 75), since it holds
the only whole `out/`. A start of a shard that has results is refused unless it is `--resume`, which unpacks that
archive into its copy first, so a repeated launch line never replaces a finished shard. `collect` verifies a private
copy of every shard's archive against its manifest and the manifest against the plan, unpacks that copy into
`<into>/<label>/out`, read-only, and prints (or with `--run` runs, in `<into>`, a 0700 directory of ours) `<python> -I
-B -m expcore merge`. A shard whose archive has no `campaign.lock`, does not verify, or is missing is refused, naming
its label: the merge would refuse it too. A shard with a snapshot newer than its archive (below) holds runs the merge
would leave out: `collect` warns of it, and with `--run` refuses, before any merge, naming the shard and the snapshot.

**The copy back (required, after `collect` and before `down`).** Nothing durable may live only on the cluster's
scratch: a site's scratch policy can remove files 30 days after creation, a job directory 7 days after its job and all
of scratch after 20 days with no job submitted. The cluster cannot push to the operator's host, so the operator PULLS
(rsync or scp, run from their host), once every step has ended. What the pull must hold, for each shard, is what
`collect` lists in `<results>/archives.sha256.json` (`expcore-hpc-archives/1`): the archive `<label>.out.tar`; its
manifest `<label>.out.json`; every file under `<label>/journals/` (a spend journal per step, which holds what a step
killed outright spent and `--resume` counts as `lost_in_kill`); and the newest snapshot that verifies, when it is NEWER
than the archive (`newer_snapshot`: a step resumed from the archive was killed before it archived, so its snapshot names
that archive and holds runs the archive lacks, which the merge does not see); and, per federation, its **root
evidence**: `<label>.root.tar` and `<label>.root.json` (the state, `platform.toml`, `facts.json`, the link check, the
render identity, the whole fault journal, the relay's journal: an allow-list, never a secret, `hpcevidence.py`), which
`down` would remove with the root; and the shard's `<label>/pass-env.json` when there is one (private, like the root
archive). Every other snapshot is resume material,
superseded by the verified archive (a snapshot of the step that wrote an archive names an earlier one or none, and was
taken before it), and is not required. The manifest has the plan's `plan_id` (the SHA-256 of the plan's study and
results directories and each shard's index, count, label and root), then the archives and the other files, each with
its name relative to `<results>`, size and SHA-256 from the bytes `collect` read, sorted, with no path and no timestamp;
`collect` prints the file's own SHA-256 on stderr. `collect` is atomic (G9): it stages every shard in a private
directory beside `--into`, checks it all, and only then moves the trees in, publishes the root archives and writes the
manifest last; a refusal leaves `--into` as it was and writes no manifest. Then it writes `evidence-collected.json`
into each root, which `hpcfed.py down` requires of a root that holds evidence a run left (a step record, which `step`
leaves in `shard-steps/<label>.json`, or a fault-journal line, the relay's journals, a shard claim or the pilot's
wiring, as after Day-0 measurements with no step): `down` refuses one without it, or where a file a step or the study
writes is no longer what the marker names (it holds the hash of each file archived), unless `--discard-evidence`; a
root that holds none (after `up` alone) goes as before, and the state and the link check, which the running federation
rewrites itself, are named by `down` and never refused.
`verify-copy` takes the pulled manifest, the local directory and that
hash (`--manifest-sha256`, required: a manifest pulled with the archives proves nothing about itself), and exits 1
naming each file that is missing, not a regular file, of another size or of another SHA-256 (names and reasons only,
never a path), 2 for a manifest it cannot use (one without the list of files beside the archives is an earlier tip's,
which checked the archives alone), and 0 when every file matches. A file of the directory that the manifest does not
name (an archive, an archive manifest, a journal) is a warning. It uses the standard library alone (Python 3.11 or
later, for `tomllib`), so it runs on a host without the core.

**Faults.** A campaign with `[faults]` runs here too: the platform layer reaches the federation's login-node fault relay
through request and answer files in `<root>/fault-relay/` on the shared root (M4's `HpcDriver`), never a step of its
own, and `EXPCORE_FED_ROOT` is how it finds that root. Outage runs on HPC depend on that relay.

**What a step killed outright leaves, and a resume on another node (W1, W2, live slot 5).** Its sandbox's run, a
dead FUSE mount in it: the next step on the node reaps the work root before it uses it (`reap_work_root`, the core's
Apptainer reaper), and `sweep`, on every node a shard step of the federation ran on, reaps it too and removes the work
root once empty. The shard's running mark on the shared results directory (`claim_running`) is what stops two steps
of one shard on two nodes. `resume-line` (`shardresume.py`, login node only) prints a resume's line for a held job of
the operator's on another node, after its checks, and records the job and the node in the federation's state, so
`down` sweeps that node too.

**Snapshots, and a step killed outright (O62).** The step's `out/` is on the node's tmpfs, so a step killed outright
(walltime, a node's end, an OOM in the step's cgroup) would lose all of it. While the dispatcher runs, `step`
snapshots it into `<results>/<label>/snapshots/` (`expcore.campaign.out_snapshot`): every `--snapshot-every` seconds
(the manifest's `snapshot_every_s`, 900 by default) and after each run's end (a change in the store's count of ended
attempts, read every `POLL_S`), one at a time, and once more after the dispatcher exits, before the archive. A
snapshot copies the store first (the core's online backup) and `runs/` after it, verifies both, and is swapped in by
rename; the newest two are kept. The dispatcher also journals what it spends to `<results>/<label>/journals/`
(`expcore run --spend-journal`, `expcore.campaign.spend_journal`), a new file per step. `--resume` restores the newest
snapshot that verifies, unless the shard's archive is newer (a snapshot names the archive its step started from, and
one taken before the archive was written names an older one or none): then the archive, as before. Either way the
store's attempt directories are pointed at the new copy's `runs/`, and the dispatcher's start counts what the
journals hold that the restored store lacks (attempts begun, top-ups and probe sends after the snapshot) as
`lost_in_kill`, once, against the shard's share of the budget. A start without `--resume` is refused when the shard
has a snapshot, as when it has results. A snapshot or a journal of another shard is refused, naming it. The shard's
step must be sized for the sandbox, the dispatcher and the store together.

The manifest (`shards.toml`, operator-local, never committed, since it names the site's paths):

    study = "/shared/study"           # the stamped study copy
    python = "/shared/venv/bin/python" # the core's interpreter, on the filesystem the nodes see
    core_src = "/shared/core/src"     # what that interpreter must import the core from
    results = "/shared/results"       # where each shard's archive and manifest come back
    tmpfs = "/tmp"                    # the node's tmpfs, never Slurm's per-job TMPDIR (a link into scratch)
    step_path = "/usr/bin:/bin"       # the step's PATH
    cpus = 6
    memory = "18g"
    snapshot_every_s = 900            # optional: seconds between snapshots of a running shard's out/ (also at run ends)
    pre_step = "guards/pre_step.py"   # optional: a script of the study that each step runs first (see above)
    pre_step_timeout_s = 60           # optional, with pre_step: its bound in seconds (above 0, at most 600)
    post_run = "guards/post_run.py"   # optional: a script of the study that each step runs after its dispatcher exits 0
    post_run_timeout_s = 300          # optional, with post_run: its bound in seconds (above 0, at most 1800)
    expect_render_identity = "render-identity.expected.json"  # optional: the render a federation must have (see above)
    home_files = "/shared/operator/keys"  # optional: the operator's key tree, put in the dispatcher's home (see above)
    [[shards]]                        # in order: shard i is the i-th entry
    label = "fed-a"                   # the shard's label (no host name: it travels with the export)
    root = "/shared/fed/a"            # the federation's root, holding hpc-federation.json and platform.toml; one
                                      # shard's own: two shards at one root (by the path it resolves to) are refused
    sandbox_node = "..."              # optional when the state file names the sandbox's node
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import io
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO

import tomllib

FORMAT = "expcore-hpc-shard/1"
STATE_FORMAT = "expcore-hpc-federation/1"
STATE_FILE = "hpc-federation.json"
LINKED = "linked"  # the state's phase once up has finished: every space's data checked, every link
PLACEMENTS = ("het", "separate")  # the sandbox record's placements: a het component, or a held job of its own
LOCK_NAME = "campaign.lock"  # expcore.campaign.lock.LOCK_NAME: the merge refuses a shard without it
ALLOWLIST = ("PATH", "LC_ALL", "EXPCORE_FED_ROOT_PARENT", "EXPCORE_FED_ROOT")
CORE_MISMATCH = 97  # the step's interpreter imports another core than the one it was given
CORE_REMEDY = (  # what an operator does about a core that names no code (`expcore run` refuses to start on one)
    "use a git checkout of the core, or a wheel built with its build identity "
    "(hatch build writes expcore/_build_identity.json)"
)
CORE_PROBE_S = 120  # how long the manifest's interpreter has to say which core it imports
# What the manifest's interpreter runs for `plan` and `lines`: `this_core`, in a process of its own.
CORE_PROBE = """import json
from expcore.provenance import core_identity
found = core_identity.current()
print(json.dumps({"known": found.known, "stale": found.stale, "why": found.why}))
"""
ARCHIVE_FAILED = 75  # the dispatcher ended but its out/ could not be archived: kept on the node's tmpfs
FLEET_HOP_FAILED = (
    76  # the manifest's `fleet_hop` did not open before the dispatcher: nothing run (`fleethop.py`'s too)
)
PRE_STEP_REFUSED = 73  # a script the study declared refused the step, or could not be run as declared, or the
# federation's render identity is not the expected one (or could not be compared with it): nothing made
POST_RUN_FAILED = 74  # the dispatcher exited 0 and the study's post-run script did not: archived, marked not final
PRE_STEP_TIMEOUT_S = 60.0  # a guard's bound when the manifest names none
PRE_STEP_MAX_S = 600.0  # the longest bound a manifest may give a guard
POST_RUN_TIMEOUT_S = 300.0  # a post-run script's bound when the manifest names none
POST_RUN_MAX_S = 1800.0  # the longest bound a manifest may give a post-run script
HOOK_MAX_BYTES = 1 << 20  # a declared script is at most this long
CAMPAIGN = "campaign.toml"  # the study's stamped campaign file: the guard's second argument
NOT_FINAL = "NOT-FINAL.json"  # expcore.campaign.shard.NOT_FINAL_FILE: in a not-final out/, which the merge refuses
NOT_FINAL_FORMAT = "expcore-hpc-shard-not-final/1"
HOOKS = {  # what each declared script is called in a message, its default bound and its longest
    "pre_step": ("pre-step guard", PRE_STEP_TIMEOUT_S, PRE_STEP_MAX_S),
    "post_run": ("post-run script", POST_RUN_TIMEOUT_S, POST_RUN_MAX_S),
}
CHUNK = 1 << 20
HOME_FILES_MAX_FILES = 64  # the operator's key tree (`home_files`): at most this many files,
HOME_FILES_MAX_DIRS = 64  # this many directories,
HOME_FILES_MAX_BYTES = 1 << 20  # this many bytes in all,
HOME_FILES_MAX_DEPTH = 8  # and this many directories deep
# Every signal whose default action ends the process and that a scheduler, a shell or a site can send a step: while the
# operator's key files are in its home, each ends the step through its cleanup instead (`_unwind_on_stop`).
STOP_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2", "SIGALRM")
    if hasattr(signal, name)
)
_O_PATH = getattr(os, "O_PATH", os.O_RDONLY)  # Linux: a descriptor that only names a directory, to search below it
SNAPSHOT_EVERY_S = 900.0  # a running shard's out/ is snapshotted this often, and after each run's end
POLL_S = 30.0  # how often the snapshot thread looks for a run's end in the store
SNAPSHOTS, JOURNALS = "snapshots", "journals"  # under <results>/<label>/
WORK_PREFIX = "expcore-shard-"  # a step's directory on the node's tmpfs: <tmpfs>/expcore-shard-<label>.<random>
OWNER, OWNER_FORMAT = "step.json", "expcore-hpc-shard-step/1"  # in it: the step's process, campaign and federation
OWNER_ENDING = ".step.ended.json"  # written at the start, renamed over step.json when the dispatcher ends
OWNER_STATUS = ".step.status.json"  # the same with the dispatcher's exit status, written when it ends, if it can be
OWNER_POST = ".step.post.json"  # the same with the post-run script's record, written when it ends
RUNNING, ARCHIVING = "running", "archiving"  # the step's phase: its dispatcher runs; it ended and out/ is whole
ARCHIVE_FAILED_MARK = "ARCHIVE-FAILED.json"  # in it: the out/ in it could not be archived, so it is kept
PLATFORM = "platform.toml"  # the federation's, in its root; the step's copy of the study has it beside study.toml
ARCHIVES_FORMAT = "expcore-hpc-archives/1"  # the copy back's manifest: what `collect` read, for `verify-copy`
ARCHIVES_MANIFEST = "archives.sha256.json"  # in <results>, beside the archives
PASS_ENV = "pass-env.json"  # <results>/<label>/: the shard's first-step pass_env names and values; PRIVATE evidence
ROOT_SUFFIXES = (".root.tar", ".root.json")
# `collect --roots-only --root R --into DIR`: ONE federation's root evidence with no plan. Its copy is `<stem>.root.tar`
# and `<stem>.root.json` (the suffixes `expcore package` and `verify-copy` already key on) and an `archives.sha256.json`
# of a form of its own, which lists exactly that pair: the full form's refusal of an empty `archives` list is unchanged.
ROOT_ARCHIVES_FORMAT = "expcore-hpc-root-archives/1"
ROOT_ONLY_STEM = "federation"
ASIDE_NAME = re.compile(
    r"(?P<label>[A-Za-z0-9][A-Za-z0-9-]*)\.[0-9a-f]{12}\.root\.json"
)  # <results>/<label>: a federation's root evidence and its manifest
ASIDE_TAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\.[0-9a-f]{12}\.root\.tar")  # the archive beside such a manifest
# The step's record in its federation's root that `down` reads (`hpcevidence`, the source: this file runs alone on a
# node and keeps its own copy of the two names; a test compares them).
STEP_RECORDS = "shard-steps"
STEP_RECORD_FORMAT = "expcore-hpc-shard-step-record/1"
RUNNING_MARK, RUNNING_FORMAT = "running.json", "expcore-hpc-shard-running/1"  # <results>/<label>/: a step runs it
# The federation's render identity (O74): the record `hpcfed.py up` writes in its root (hpcidentity.py: hashes and a
# version, no address, id, path or secret). This file runs alone on a node, so it keeps its own copy of the names; a
# test compares them with hpcidentity's.
RENDER_IDENTITY = "render-identity.json"
RENDER_IDENTITY_VERSION = "expcore-render-identity/1"
RENDER_IDENTITY_PINS = (
    "sandbox_sif_sha256",
    "egress_shim_sha256",
    "egress_collector_sha256",
    "oneclient_bundle_sha256",
    "gateport_sha256",
)
RENDER_IDENTITY_ARTIFACTS = "study_artifacts_tree_sha256"  # in the record only when the site declares the directory
RENDER_IDENTITY_KEYS = (
    "version",
    "template_sha256",
    "platform_fingerprint_sha256",
    *RENDER_IDENTITY_PINS,
    RENDER_IDENTITY_ARTIFACTS,
)
STUDY_STATE = "study-state"  # in a step's out/: what the step records of the federation it ran against
SHA256_TEXT = re.compile(r"[0-9a-f]{64}")


# The step's HOME, and every directory a tool writes under one, until it has made its directory on the tmpfs: a path
# under a file, which nobody can make (`hpclaunch.NO_HOME`); the dispatcher's are in that directory (`step`).
NO_HOME = "/dev/null/hpcop-home"
HOME_DIRS = (".config", ".cache", ".local", ".local/share", ".local/state", ".apptainer", ".apptainer/cache")


def home_env(home: str) -> dict[str, str]:
    """A step's own home, so that no tool falls back to the account's home, which every job and agent of the account
    shares: as `hpcslurm.home_env` gives it (this file runs on a node by itself, so it keeps its own copy; a test
    compares the two)."""
    return {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
        "APPTAINER_CONFIGDIR": f"{home}/.apptainer",
        "APPTAINER_CACHEDIR": f"{home}/.apptainer/cache",
        "UV_CACHE_DIR": f"{home}/.cache/uv",
        "PIP_CACHE_DIR": f"{home}/.cache/pip",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


# The manifest's `pass_env` (O63): variables of the operator's the study needs in its dispatcher's environment, such as
# the operator's half of a fault sanction. A name is plain upper case, is no secret by its look and is none the step
# sets itself; a value is a short word of plain characters or an absolute path of plain characters with no climbing, so
# that no secret or shell syntax rides on a line.
# An allow-list: upper-case words (letters and digits, the first a letter) joined by single underscores, at
# most 64 characters: none with a leading, trailing or doubled underscore, so none that is not a plain study
# variable.
PASS_ENV_NAME = re.compile(r"(?=.{1,64}\Z)[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
PASS_ENV_VALUE = re.compile(r"[A-Za-z0-9._:=,+-]{0,128}")
PASS_ENV_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,1024}")
PASS_ENV_SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.IGNORECASE)
PASS_ENV_OWN_PREFIXES = ("EXPCORE_", "PYTHON")
# Variables that change how a program is loaded or started, whatever their value, so never declared: the dynamic
# loader's (`LD_PRELOAD` names a library whose constructor runs before Python starts, so before the environment,
# identity and guard checks; `GLIBC_TUNABLES` is read by it too), the interpreters' option and path families (Perl,
# Ruby, Node; Python's are the step's own, `PASS_ENV_OWN_PREFIXES`), the shell's start-up file, field separator, trace
# prompt, options and prompt command, and the locale and character-conversion search paths.
PASS_ENV_LOADER_PREFIXES = ("LD_", "DYLD_", "PERL5", "RUBY", "NODE_")
PASS_ENV_LOADER_NAMES = frozenset({"GLIBC_TUNABLES", "BASH_ENV", "ENV", "IFS", "PS4", "SHELLOPTS", "PROMPT_COMMAND",
                                   "GCONV_PATH", "LOCPATH", "NLSPATH"})  # fmt: skip
PASS_ENV_OWN = frozenset({*ALLOWLIST, "HOME", "TMPDIR", *home_env(NO_HOME)})  # the step's own: never a declared one
# The values a shard's first step passed, kept for its whole life (O63): `<results>/<label>/pass-env.json`, written by
# that step once it holds the shard's running mark, and read by every later step, `resume-line` and the fleet's step.
PASS_ENV_ORIGIN, PASS_ENV_ORIGIN_FORMAT = "pass-env.json", "expcore-hpc-shard-pass-env/1"
PASS_ENV_ORIGIN_MAX = 1 << 16  # a record is a few names and short words: anything longer is not one
ARCHIVE_MANIFEST_MAX = 1 << 20  # an archive manifest holds a few records of a step: anything longer is not one
SNAPSHOT_NAME = re.compile(r"snap-\d{6,}")  # a snapshot's directory (`out_snapshot`'s, which this file loads late)


class ShardsError(Exception):
    """A manifest, a federation's state or a shard's archive that cannot be used; the message names what."""


@dataclass(frozen=True)
class Shard:
    index: int
    count: int
    label: str
    root: Path
    node: str | None = None


@dataclass(frozen=True)
class Plan:
    study: Path
    python: str
    core_src: Path
    results: Path
    tmpfs: Path
    step_path: str
    cpus: int
    memory: str
    shards: tuple[Shard, ...]
    snapshot_every_s: float = SNAPSHOT_EVERY_S
    pre_step: str | None = None  # the study's pre-step guard: a path relative to its root
    pre_step_sha256: str | None = None  # its SHA-256, recorded by `pinned` when `plan` and `lines` read it
    pre_step_timeout_s: float = PRE_STEP_TIMEOUT_S
    post_run: str | None = None  # the study's post-run script: a path relative to its root
    post_run_sha256: str | None = None  # its SHA-256, recorded by `pinned`
    post_run_timeout_s: float = POST_RUN_TIMEOUT_S
    expect_render_identity: str | None = None  # the render identity the plan expects: a path relative to the study
    expect_render_identity_sha256: str | None = None  # its SHA-256, recorded by `pinned`
    pass_env: tuple[str, ...] = ()  # the variables the manifest declares: names only
    pass_env_pins: tuple[tuple[str, str], ...] | None = None  # each name's value, read by `pin_pass_env`
    home_files: str | None = None  # the operator's key tree: an absolute path, put in the dispatcher's home (O63)
    fleet_hop: str | None = None  # the hop to the LLM fleet's front as canonical JSON, kept before the dispatcher (O93)


# ------------------------------------------------------------------------------------------------ the manifest
def _label_ok(index: int, count: int, label: str) -> None:
    from expcore.campaign.shard import ShardError, ShardSpec

    try:
        ShardSpec(index, count, label)
    except ShardError as error:
        raise ShardsError(f"shard {index}: {error}") from None


def pass_env_name_problem(name: Any) -> str | None:
    """Why `name` cannot be a variable the step passes to its dispatcher (the message names it), or None: a plain
    upper-case name (`PASS_ENV_NAME`: words of letters and digits joined by single underscores), none that looks like a
    secret (KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL, AUTH, anywhere in it), none the step sets itself (its
    allowlist, its home and TMPDIR, anything of EXPCORE_ or PYTHON) and none that changes how a program is loaded or
    started (anything of LD_, DYLD_, PERL5, RUBY or NODE_, GLIBC_TUNABLES, BASH_ENV, ENV, IFS, PS4, SHELLOPTS,
    PROMPT_COMMAND, GCONV_PATH, LOCPATH, NLSPATH)."""
    if not isinstance(name, str) or PASS_ENV_NAME.fullmatch(name) is None:
        shown = repr(name[:70] if isinstance(name, str) else name)
        return f"pass_env name {shown} must match ^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$ and be at most 64 characters"
    if PASS_ENV_SECRET.search(name):
        return (f"pass_env name {name} looks like a secret (it contains KEY, TOKEN, SECRET, PASSWORD, PASSWD, "
                "CREDENTIAL or AUTH): a secret never rides on a line")  # fmt: skip
    if name in PASS_ENV_OWN or name.startswith(PASS_ENV_OWN_PREFIXES):
        return f"pass_env name {name} is one the step sets itself"
    if name in PASS_ENV_LOADER_NAMES or name.startswith(PASS_ENV_LOADER_PREFIXES):
        return (f"pass_env name {name} changes how a program is loaded or started (a loader, interpreter or shell "
                "variable)")  # fmt: skip
    return None


def pass_env_path_ok(value: str) -> bool:
    """Whether `value` is an absolute path of plain characters (`^/[A-Za-z0-9._/-]{1,1024}$`) with no `..` or `.`
    part and no `//`: a name of a place a study may use, never a way up out of it."""
    return (
        PASS_ENV_PATH.fullmatch(value) is not None
        and "//" not in value
        and not any(part in (".", "..") for part in value.split("/"))
    )


def pass_env_value_problem(name: str, value: Any) -> str | None:
    """Why `value` cannot be the value of the declared variable `name`, or None: a plain word
    (`^[A-Za-z0-9._:=,+-]{0,128}$`) or an absolute path (`pass_env_path_ok`). The message names the variable and
    never the value."""
    plain = isinstance(value, str) and PASS_ENV_VALUE.fullmatch(value) is not None and value not in (".", "..")
    if plain or (isinstance(value, str) and pass_env_path_ok(value)):  # `.` and `..` are climbing, not words
        return None
    return (f"pass_env variable {name}'s value does not match a plain word (^[A-Za-z0-9._:=,+-]{{0,128}}$) or a plain "
            "absolute path (^/[A-Za-z0-9._/-]{1,1024}$, no `..` or `.` part, no `//`)")  # fmt: skip


def pass_env_names(declared: Any) -> tuple[str, ...]:
    """The manifest's `pass_env`, checked: a list of names, each of `pass_env_name_problem`'s rule and each once."""
    if not isinstance(declared, list):
        raise ShardsError("pass_env must be a list of variable names")
    for name in declared:
        if problem := pass_env_name_problem(name):
            raise ShardsError(problem)
    for name in declared:
        if declared.count(name) > 1:
            raise ShardsError(f"pass_env names {name} twice")
    return tuple(declared)


def pin_pass_env(plan: Plan, environ: Mapping[str, str] | None = None) -> Plan:
    """The plan with the operator's value of each declared variable recorded, from `environ` (the process's own by
    default): `plan` and `lines` and `resume-line` call it before they print or record anything, so that a step's
    line carries each value and cannot change or gain one. `ShardsError` naming the variable, never its value, when
    one is unset or its value is not a plain word. It is not part of `pinned`, which `collect` calls: `collect` reads
    no environment of the operator's. The same plan when none is declared."""
    if not plan.pass_env:
        return plan
    environ = os.environ if environ is None else environ
    pins = []
    for name in plan.pass_env:
        if name not in environ:
            raise ShardsError(f"pass_env variable {name} is not set in the operator's environment")
        if problem := pass_env_value_problem(name, environ[name]):
            raise ShardsError(problem)
        pins.append((name, environ[name]))
    return replace(plan, pass_env_pins=tuple(pins))


def pinned_pass_env(plan: Plan) -> tuple[tuple[str, str], ...]:
    """The name and value of each variable the plan declares in `pass_env`, as `pin_pass_env` read them: what a step's
    line carries as `env -i` words (`step_argv`) and what each program that builds the study's plugin at plan time is
    given (`shardresume.study_fingerprint`), so a plugin sees the variables, with the values, its step will. Empty
    when none is declared; `ShardsError` for a plan that declares some and has not pinned them, so that nothing is run
    or made from a plan that has not read their values."""
    if not plan.pass_env:
        return ()
    if plan.pass_env_pins is None or tuple(name for name, _ in plan.pass_env_pins) != plan.pass_env:
        raise ShardsError("the declared pass_env variables are not pinned: nothing is run or made from a plan that has "
                          "not read their values")  # fmt: skip
    return plan.pass_env_pins


def read_pass_env_origin(results: Path, spec: Mapping[str, Any]) -> dict[str, str] | None:
    """The name and value of each variable the shard's first step passed (`record_pass_env_origin`), or None when it
    recorded none: no file, which is a shard not started yet or one whose first step passed nothing. A file that is
    there and cannot be read as this shard's (empty, not JSON, of another shard, holding a name or a value a variable
    may not carry, too long, a link) is `ShardsError` naming the file, never a value: it is never taken for none, so
    that emptying or replacing it does not lift the rule."""
    label = str(spec["label"])
    path = results / label / PASS_ENV_ORIGIN
    if not os.path.lexists(path):
        return None
    unreadable = (f"shard {label}: its first step's record of the variables it passed ({PASS_ENV_ORIGIN}) cannot be "
                  "read as this shard's")  # fmt: skip
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:  # no link is followed
            raw = handle.read(PASS_ENV_ORIGIN_MAX + 1) if stat.S_ISREG(os.fstat(handle.fileno()).st_mode) else b""
        record = json.loads(raw) if len(raw) <= PASS_ENV_ORIGIN_MAX else None
    except (OSError, ValueError):
        raise ShardsError(unreadable) from None
    values = record.get("pass_env") if isinstance(record, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("format") != PASS_ENV_ORIGIN_FORMAT
        or record.get("shard") != dict(spec)
        or not isinstance(values, dict)
        or any(pass_env_name_problem(name) or pass_env_value_problem(name, value) for name, value in values.items())
    ):
        raise ShardsError(unreadable)
    return dict(values)


def shard_has_results(results: Path, label: str) -> bool:
    """Whether the shard has an archive or a snapshot: what a start without `--resume` is refused for (`step`)."""
    if any(os.path.lexists(results / f"{label}.out.{kind}") for kind in ("tar", "json")):
        return True
    try:
        return any(SNAPSHOT_NAME.fullmatch(entry.name) for entry in (results / label / SNAPSHOTS).iterdir())
    except OSError:
        return False


def archived_pass_env(results: Path, spec: Mapping[str, Any]) -> dict[str, str] | None:
    """The name and value of each variable the shard's latest archive says its step passed (the `pass_env` of
    `<label>.out.json`, which `archive` writes: none in a manifest of a step that passed nothing), or None when the
    shard has no archive manifest (a snapshot alone, nothing, or a first archive killed after its tar was written and
    before its manifest was, which resumes from its snapshots as it did). The manifest is read as a regular file,
    never through a link, and up to `ARCHIVE_MANIFEST_MAX` bytes. `ShardsError` naming the manifest, never a value,
    when it is there and cannot be read as this shard's, or holds a name or a value a variable may not carry: with
    the shard's own record gone it is the only one, so what it cannot say is never taken for "nothing was passed"."""
    label = str(spec["label"])
    manifest = results / f"{label}.out.json"
    if not os.path.lexists(manifest):
        return None
    unreadable = ShardsError(
        f"shard {label}: its archive manifest ({manifest.name}) cannot be read as this shard's, so the variables its "
        f"first step passed cannot be checked ({PASS_ENV_ORIGIN} is gone)"
    )
    try:
        with _open_plain(manifest, label) as handle:
            raw = handle.read(ARCHIVE_MANIFEST_MAX + 1)
        record = json.loads(raw) if len(raw) <= ARCHIVE_MANIFEST_MAX else None
    except (OSError, ValueError):
        raise unreadable from None
    values = record.get("pass_env", {}) if isinstance(record, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("shard") != dict(spec)
        or not isinstance(values, dict)
        or any(pass_env_name_problem(name) or pass_env_value_problem(name, value) for name, value in values.items())
    ):
        raise unreadable
    return dict(values)


def check_pass_env_origin(results: Path, spec: Mapping[str, Any], given: Mapping[str, str]) -> None:
    """A shard's declared variables keep the values its first step passed, for the shard's whole life: `given` (the
    name and value of each variable a later step would pass: a resume, a plain launch after a first step killed before
    it archived anything, a fleet's step) must be exactly what that step recorded. `ShardsError` naming each variable
    that is not, one that is added or dropped included, and never a value, before anything is launched. A shard with
    no record (`read_pass_env_origin`) and no results (`shard_has_results`) has had no first step: nothing is compared.
    One with results and no record either had a first step that passed nothing, or has lost the record: its latest
    archive's manifest says which (`archived_pass_env`), and `given` is compared with what that records, values
    included, so that deleting the record lifts no rule. A manifest that cannot be read as the shard's refuses a step
    that passes nothing, naming the manifest (nothing says what the first step passed); a step that passes variables is
    refused as it was, for adding them. With a snapshot alone and no archive, the first step passed nothing as far as
    anything says, so a variable it now passes is one added mid-shard: refused the same way."""
    label = spec["label"]
    recorded = read_pass_env_origin(results, spec)
    if recorded is not None:
        why = f"recorded in {PASS_ENV_ORIGIN}"
    elif shard_has_results(results, str(label)):
        try:
            recorded = archived_pass_env(results, spec) or {}
        except ShardsError:
            if not given:  # nothing says what the first step passed, and this one passes none: refused, naming it
                raise
            recorded = {}  # a step that passes variables is refused below, as it was before the manifest was read
        why = (f"recorded in the shard's archive manifest, {PASS_ENV_ORIGIN} being gone" if recorded
               else f"the shard has results and no record of any variable ({PASS_ENV_ORIGIN})")  # fmt: skip
    else:
        return
    changed = sorted(name for name in {*recorded, *given} if recorded.get(name) != given.get(name))
    if changed:
        many = len(changed) > 1
        raise ShardsError(
            f"shard {label}: pass_env variable{'s' if many else ''} {', '.join(changed)} "
            f"{'are' if many else 'is'} not what its first step passed ({why}): a shard's declared variables keep "
            "one value across its whole life"
        )


def record_pass_env_origin(results: Path, spec: Mapping[str, Any], passed: Mapping[str, str]) -> None:
    """The shard's first step records what it passed, in `<results>/<label>/pass-env.json`: made whole in a draft of
    its own and put in place by an exclusive link, so a reader never sees a part of it and a later step never replaces
    it. Written only when the step passed a variable (with none, the results directory is what it was). A record that
    is already there is compared instead (`check_pass_env_origin`), so two first steps that raced agree or one is
    refused. `ShardsError` when it differs, cannot be read or cannot be written: a step that cannot record its values
    does not start. It compares first, so a step that passes variables is refused when the shard has results and no
    record (a first step that passed nothing archived meanwhile): a first step records only into a shard with none."""
    label = str(spec["label"])
    check_pass_env_origin(results, spec, passed)
    if not passed:
        return
    path = results / label / PASS_ENV_ORIGIN
    draft = path.with_name(f".{PASS_ENV_ORIGIN}.{secrets.token_hex(8)}.draft")
    record = {"format": PASS_ENV_ORIGIN_FORMAT, "shard": dict(spec), "pass_env": dict(passed)}
    payload = (json.dumps(record, sort_keys=True) + "\n").encode()
    if len(payload) > PASS_ENV_ORIGIN_MAX:  # what the reader refuses: a record no later step could be compared with
        raise ShardsError(
            f"shard {label}: the variables its first step passes make a record of more than {PASS_ENV_ORIGIN_MAX} "
            f"bytes ({PASS_ENV_ORIGIN}), which no later step could read: fewer or shorter values, or the step is "
            "refused"
        )
    try:
        _private_dirs(path.parent)
        with os.fdopen(os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(draft, path)
        except FileExistsError:
            check_pass_env_origin(results, spec, passed)  # another first step won the link: it must have passed these
    except OSError as error:
        raise ShardsError(
            f"shard {label}: its first step's variables could not be recorded in {PASS_ENV_ORIGIN} "
            f"({type(error).__name__}): a step that cannot record them does not start"
        ) from None
    finally:
        draft.unlink(missing_ok=True)


def one_root_each(shards: Sequence[Shard]) -> None:
    """Refuse two shards whose roots are one directory, naming both labels and no path (O63). They would share one
    federation's `down` gate: `collect --roots-only` for one shard writes the marker `down` reads for both, after which
    the full collect, which archives the root for each shard, refuses for ever. A root is told by the path it resolves
    to (a link, a `..` part, a trailing slash); one that cannot be resolved against the file system is told by the path
    as written."""
    seen: dict[str, str] = {}
    for shard in shards:
        try:
            where = os.path.realpath(shard.root)
        except (OSError, ValueError, RuntimeError):
            where = os.path.normpath(str(shard.root))
        if where in seen:
            raise ShardsError(
                f"shards {seen[where]} and {shard.label} have one federation root between them: "
                "each shard needs a federation of its own"
            )
        seen[where] = shard.label


def load_plan(path: Path) -> Plan:
    """Read and check the manifest: every key present, absolute paths, at least two shards, labels by the core's rule
    and each once. The optional `pass_env` names the operator's variables a step passes to its dispatcher (names
    only; their values are read by `pin_pass_env`)."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ShardsError(f"the manifest could not be read ({type(error).__name__})") from None
    keys = {"study", "python", "core_src", "results", "tmpfs", "step_path", "cpus", "memory", "shards"}
    optional = {"snapshot_every_s", "pre_step", "pre_step_timeout_s", "post_run", "post_run_timeout_s",
                "expect_render_identity", "pass_env", "home_files", "fleet_hop"}  # fmt: skip
    if not keys <= set(data) <= keys | optional:
        raise ShardsError(
            f"the manifest's keys must be: {', '.join(sorted(keys))}, and perhaps {', '.join(sorted(optional))}"
        )
    every = data.get("snapshot_every_s", SNAPSHOT_EVERY_S)
    if isinstance(every, bool) or not isinstance(every, int | float) or not 60 <= every < float("inf"):
        raise ShardsError("snapshot_every_s must be a number of seconds, at least 60")
    bounds = {}
    for key, (_, default, maximum) in HOOKS.items():
        if key in data and (problem := guard_path_problem(data[key])):
            raise ShardsError(f"{key} {problem}")
        if f"{key}_timeout_s" in data and key not in data:
            raise ShardsError(f"{key}_timeout_s is the bound of a {key}, which the manifest does not declare")
        bounds[key] = data.get(f"{key}_timeout_s", default)
        if problem := bound_problem(bounds[key], maximum):
            raise ShardsError(f"{key}_timeout_s {problem}")
    if "expect_render_identity" in data and (problem := guard_path_problem(data["expect_render_identity"])):
        raise ShardsError(f"expect_render_identity {problem}")
    declared = pass_env_names(data["pass_env"]) if "pass_env" in data else ()
    if "home_files" in data and (problem := home_files_path_problem(data["home_files"])):
        raise ShardsError(f"home_files {problem}")
    if "fleet_hop" in data and (problem := _sibling("fleethop").hop_problem(data["fleet_hop"])):
        raise ShardsError(f"fleet_hop {problem}")
    for key in sorted({"study", "python", "core_src", "results", "tmpfs"}):  # a step starts in / (`--chdir=/`)
        if not isinstance(data[key], str) or not data[key].startswith("/"):
            raise ShardsError(f"{key} must be an absolute path")
    if not isinstance(data["cpus"], int) or isinstance(data["cpus"], bool) or data["cpus"] < 1:
        raise ShardsError("cpus must be a whole number of at least 1")
    if not isinstance(data["memory"], str) or not data["memory"][:-1].isdigit() or data["memory"][-1] not in "mgMG":
        raise ShardsError("memory must be a size in m or g, such as 18g")
    entries = data["shards"]
    if not isinstance(entries, list) or len(entries) < 2:
        raise ShardsError("a sharded campaign needs at least two [[shards]], one per federation")
    shards = []
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, Mapping) or not {"label", "root"} <= set(entry) <= {"label", "root", "sandbox_node"}:
            raise ShardsError(f"shard {n}: an entry has label, root and perhaps sandbox_node")
        _label_ok(n, len(entries), entry["label"])
        if not isinstance(entry["root"], str) or not entry["root"].startswith("/"):
            raise ShardsError(f"shard {n}: root must be an absolute path")
        shards.append(Shard(n, len(entries), entry["label"], Path(entry["root"]), entry.get("sandbox_node")))
    labels = [s.label for s in shards]
    if len(set(labels)) != len(labels):
        raise ShardsError("a label is given to two shards")
    one_root_each(shards)
    return Plan(Path(data["study"]), str(data["python"]), Path(data["core_src"]), Path(data["results"]),
                Path(data["tmpfs"]), str(data["step_path"]), data["cpus"], data["memory"], tuple(shards),
                float(every), data.get("pre_step"), None, float(bounds["pre_step"]), data.get("post_run"), None,
                float(bounds["post_run"]), data.get("expect_render_identity"), pass_env=declared,
                home_files=data.get("home_files"),
                fleet_hop=json.dumps(data["fleet_hop"], sort_keys=True, separators=(",", ":"))
                if "fleet_hop" in data else None)  # fmt: skip


def federation(shard: Shard) -> dict[str, Any]:
    """Where the shard's step goes, from the federation's state as its `up` wrote it: the job, the heterogeneous
    component, the step name's prefix and the sandbox's node.

    The state's `sandbox` record says how the sandbox node was placed: `het`, a component of the federation's own
    heterogeneous job (the step needs its `--het-group`: the site's Slurm refuses one that names none), or
    `separate`, a held job of its own (its `job`). A state with no `sandbox` record is the federation's job alone, its
    node the `sandbox` role `up` placed there (`services.sandbox`), or the manifest's. Either way the manifest's
    `sandbox_node`, when given, must be the state's."""
    try:
        state = json.loads((shard.root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ShardsError(f"shard {shard.label}: its federation's state file could not be read") from None
    if not isinstance(state, Mapping) or state.get("format") != STATE_FORMAT:
        raise ShardsError(f"shard {shard.label}: its federation's state file is not {STATE_FORMAT}")
    allocation = state.get("allocation") or {}
    job, name = allocation.get("job"), allocation.get("name")
    if not (_job_id(job) and isinstance(name, str) and name):
        raise ShardsError(f"shard {shard.label}: its federation has no held job on record")
    if state.get("phase") != LINKED:  # an up that failed (its data check, its link check) left a federation not ready
        raise ShardsError(f"shard {shard.label}: its federation's up has not finished (phase {state.get('phase')})")
    check_ingested(shard, state)
    record = state.get("sandbox")
    placement, group = "allocation", None
    if record is not None:
        if not isinstance(record, Mapping) or record.get("placement") not in PLACEMENTS:
            raise ShardsError(f"shard {shard.label}: its sandbox record names no placement of {', '.join(PLACEMENTS)}")
        placement = record["placement"]
        if placement == "het":
            group = record.get("het_group")
            if isinstance(group, bool) or not isinstance(group, int) or group < 1:
                raise ShardsError(f"shard {shard.label}: its sandbox is a het component with no het_group above 0")
        else:
            job, name = record.get("job"), record.get("name") or name
            if not _job_id(job):
                raise ShardsError(f"shard {shard.label}: its separate sandbox has no held job on record")
    if record is not None:  # the record's own node: never one placed in another job, nor the manifest's alone
        node = record.get("node")
    else:  # the federation's own job: the sandbox role its up placed there (`services.sandbox`), or the manifest's
        held = (state.get("services") or {}).get("sandbox") or {}
        node = (held.get("node") if held.get("kind") == "sandbox" else None) or shard.node
    if not isinstance(node, str) or not node:
        raise ShardsError(f"shard {shard.label}: no sandbox node: the state file names none and the manifest neither")
    if shard.node and node != shard.node:
        raise ShardsError(f"shard {shard.label}: the manifest's sandbox_node is not the one its state file names")
    listed = (record.get("component_job") or job) if placement == "het" else job  # `squeue -s`'s id for its steps
    return {"job": job, "name": name, "node": node, "placement": placement, "het_group": group, "listed": listed}


def platform_ids(root: Path) -> dict[str, str]:
    """For each space of the federation's `platform.toml` that names its Onezone ID (`id`), that ID: the one the
    study's runs would read and write, which the core's phase rewrote from the zone it last found (a space made again
    by name is under a new one)."""
    data = tomllib.loads((root / PLATFORM).read_text(encoding="utf-8"))
    entries = data.get("spaces") if isinstance(data.get("spaces"), list) else [data.get("space")]
    return {
        entry["name"]: entry["id"]
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str) and isinstance(entry.get("id"), str)
    }


def platform_data(root: Path) -> dict[str, str | None]:
    """For each space of the federation's `platform.toml` that declares its data (`data_manifest`), the content hash of
    the manifest it names, as the dispatcher's fingerprint will take it (`Manifest.digest()`), None for a file that
    cannot be read or is no manifest. A relative path is the root's, where the file lies."""
    from expcore.config.loading import ConfigError
    from expcore_onedata.importmanifest import load_manifest

    data = tomllib.loads((root / PLATFORM).read_text(encoding="utf-8"))
    entries = data.get("spaces") if isinstance(data.get("spaces"), list) else [data.get("space")]
    found: dict[str, str | None] = {}
    for entry in entries:
        if not (isinstance(entry, Mapping) and isinstance(entry.get("name"), str) and entry.get("data_manifest")):
            continue
        try:
            found[entry["name"]] = load_manifest(root / str(entry["data_manifest"])).digest()
        except (ConfigError, OSError, ValueError):
            found[entry["name"]] = None
    return found


def check_ingested(shard: Shard, state: Mapping[str, Any]) -> None:
    """Refuse a federation whose ingested spaces (those the study fills after `up`, which its state records) have no
    verified marker (O57): data nothing has shown to be the declared data is data a shard would spend on under a
    fingerprint that says otherwise. A marker is `hpcfed.py verify-data`'s pass, bound to the digest of the manifest it
    compared with; it counts only while that is the manifest `up` recorded and the one the platform file names now (a
    manifest edited after the verification changes the fingerprint and leaves the pass for another experiment). It is
    bound to the space it read as well, by its Onezone ID: the ID in the marker, in the record `up` made and in the
    platform file must be one, so a space deleted and made again by name (empty, under a new ID) after the pass is
    refused, naming it, whatever the marker still says. A state with no record at all, from an `up` that predates it,
    cannot be shown verified, and nor can a marker or a record with no ID."""
    data = state.get("data")
    ingested = data.get("ingested") if isinstance(data, Mapping) else None
    if not isinstance(ingested, Mapping):
        raise ShardsError(
            f"shard {shard.label}: its federation's state has no record of its ingested spaces (its up predates it): "
            "down, then up again"
        )
    if not ingested:
        return
    try:
        declared, current = platform_data(shard.root), platform_ids(shard.root)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ShardsError(f"shard {shard.label}: its federation's {PLATFORM} could not be read") from None
    unverified, moved = [], []
    for space, record in ingested.items():
        record = record if isinstance(record, Mapping) else {}
        verified = record.get("verified")
        marker = verified.get("manifest") if isinstance(verified, Mapping) else None
        recorded = record.get("manifest")
        read = verified.get("space_id") if isinstance(verified, Mapping) else None
        if not (isinstance(marker, str) and marker == recorded and declared.get(space, recorded) == recorded):
            unverified.append(str(space))
        elif not (isinstance(read, str) and isinstance(record.get("space_id"), str)):
            unverified.append(str(space))  # no ID to bind the pass to: an older `up`'s, or a driver that records none
        elif not read == record["space_id"] == current.get(space):
            moved.append(str(space))
    if unverified:
        spaces = ", ".join(sorted(unverified))
        raise ShardsError(
            f"shard {shard.label}: its federation's ingested space(s) have no verified data: {spaces} "
            "(run `hpcfed.py verify-data` once the ingest is done)"
        )
    if moved:
        spaces = ", ".join(sorted(moved))
        raise ShardsError(
            f"shard {shard.label}: its federation's ingested space(s) are not the ones verified: {spaces} "
            "(a space made again since the pass holds none of its data: down, then up)"
        )


def undeclared_data(plan: Plan) -> list[str]:
    """For a campaign on more than one federation, each shard whose federation's `platform.toml` leaves a space's data
    undeclared (`data_manifest`, C06), as `<label>: <space>, ...`; empty on one federation, or when every space of every
    federation declares it. A space is one experimental condition across federations only by its data identity, so
    such shards would never merge: refused before anything runs, never found at the merge after the spend."""
    if len({shard.root for shard in plan.shards}) < 2:
        return []
    found = [undeclared_spaces(shard.label, shard.root) for shard in plan.shards]
    return [entry for entry in found if entry is not None]


def undeclared_spaces(label: str, root: Path) -> str | None:
    """One federation's part of `undeclared_data`: `<label>: <space>, ...` for each space its `platform.toml` leaves
    without `data_manifest`, or that the file could not be read; None when every space declares its data. `fleet.py`
    asks it of every federation it gives a shard, one at a time."""
    try:
        data = tomllib.loads((root / PLATFORM).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return f"{label}: its federation's {PLATFORM} could not be read"
    entries = data.get("spaces") if isinstance(data.get("spaces"), list) else [data.get("space")]
    missing = [str(e.get("name")) if isinstance(e, Mapping) else "[space]" for e in entries
               if not (isinstance(e, Mapping) and e.get("data_manifest"))]  # fmt: skip
    return f"{label}: {', '.join(missing)}" if missing else None


def check_one_campaign(plan: Plan, only: Sequence[str] | None = None) -> None:
    """`ShardsError` unless the study's campaign fingerprint, taken beside each shard's federation's `platform.toml`
    (`only`: the labels of the shards a line is made for, all by default), is one: the shards would never merge, and
    `merge` finds it after the spend (O63; `shardresume.check_one_campaign`). `plan` and `lines` call it before they
    print a line, `fleet.py plan` before it assigns a shard."""
    _sibling("shardresume").check_one_campaign(sys.modules[__name__], plan, only)


def _job_id(job: Any) -> bool:
    return isinstance(job, str) and job.isdigit()


def step_name(fed: Mapping[str, Any], shard: Shard) -> str:
    """The name of every step of the shard, wherever it runs (`lines`, `resume-line`): the one name a search of every
    step of this user's finds it by."""
    return f"{fed['name']}-shard-{shard.index}"


def step_argv(
    plan: Plan, shard: Shard, fed: Mapping[str, Any], script: Path, *, resume: bool = False, over: str | None = None
) -> list[str]:
    """The login node's `srun` of one shard's step (the module's docstring): `--exact`, never `--overlap`, in `/` on
    the node (`--chdir=/`); an environment of the allowlist alone, set on the node by `env -i`, with no secret in it.
    The step is told the id its step is listed under and its name, which its running mark records (`claim_running`);
    `resume` and `over` are `resume-line`'s."""
    env = [f"PATH={plan.step_path}", "LC_ALL=C", f"EXPCORE_FED_ROOT_PARENT={shard.root.parent}",
           f"EXPCORE_FED_ROOT={shard.root}", *(f"{k}={v}" for k, v in home_env(NO_HOME).items())]  # fmt: skip
    # the operator's variables the manifest declares: each name once, with its pinned value
    env += [f"{name}={value}" for name, value in pinned_pass_env(plan)]
    step = [plan.python, "-I", "-B", str(script), "step", "--study", str(plan.study), "--index", str(shard.index),
            "--count", str(shard.count), "--label", shard.label, "--tmpfs", str(plan.tmpfs),
            "--results", str(plan.results), "--core-src", str(plan.core_src),
            "--snapshot-every", f"{plan.snapshot_every_s:g}",
            "--job", str(fed.get("listed") or fed["job"]), "--step-name", step_name(fed, shard),
            *(["--resume"] if resume else []), *(["--over", over] if over else [])]  # fmt: skip
    if plan.pre_step is not None:  # the study's guard rides on the line: `step` runs it first (`run_pre_step`)
        if plan.pre_step_sha256 is None:
            raise ShardsError(
                "the pre-step guard is not pinned: no line is made from a plan that has not recorded its hash"
            )
        step += ["--pre-step", plan.pre_step, "--pre-step-sha256", plan.pre_step_sha256,
                 "--pre-step-timeout", f"{plan.pre_step_timeout_s:g}"]  # fmt: skip
    if plan.post_run is not None:  # and the study's post-run script: `step` runs it before it archives (`run_post_run`)
        if plan.post_run_sha256 is None:
            raise ShardsError("the post-run script is not pinned: no line is made from a plan that has not hashed it")
        step += ["--post-run", plan.post_run, "--post-run-sha256", plan.post_run_sha256,
                 "--post-run-timeout", f"{plan.post_run_timeout_s:g}"]  # fmt: skip
    if plan.expect_render_identity is not None:  # and the record the federation's render must be: `step` compares first
        if plan.expect_render_identity_sha256 is None:
            raise ShardsError("the expected render identity is not pinned: no line is made from a plan that has not "
                              "hashed it")  # fmt: skip
        step += ["--expect-render-identity", plan.expect_render_identity, "--expect-render-identity-sha256",
                 plan.expect_render_identity_sha256]  # fmt: skip
    if plan.home_files is not None:  # the operator's key tree: `step` reads it itself, so the path alone rides here
        step += ["--home-files", plan.home_files]
    if plan.fleet_hop is not None:  # the hop to the LLM fleet: `step` runs it before its dispatcher (`fleethop.py`)
        step += ["--fleet-hop", plan.fleet_hop]
    for name in plan.pass_env:  # the declaration: `step` passes a name only when its line says so, with its value
        step += ["--pass-env", name]
    return [
        "srun", f"--jobid={fed['job']}", *([f"--het-group={fed['het_group']}"] if fed.get("het_group") else []),
        "--exact", "-N1", "-n1", "-w", fed["node"], "-c", str(plan.cpus),
        f"--mem={plan.memory}", "-J", step_name(fed, shard), "--export=PATH", "--chdir=/",
        "/usr/bin/env", "-i", *env, *step,
    ]  # fmt: skip


# ------------------------------------------------------------------------------------------------ the pre-step guard
def guard_path_problem(path: Any) -> str | None:
    """Why `path` cannot name a file of the study (a plain relative path: no empty, `.` or `..` part), or None."""
    if isinstance(path, bool) or not isinstance(path, str) or not path or "\0" in path:
        return "must be a path, a string, relative to the study's root"
    if path.startswith("/"):
        return "must be relative to the study's root, never absolute"
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "must name a file inside the study: no empty, `.` or `..` part"
    return None


def bound_problem(bound: Any, maximum: float = PRE_STEP_MAX_S) -> str | None:
    """Why `bound` is not a declared script's bound (seconds above 0 and at most `maximum`), or None."""
    if isinstance(bound, bool) or not isinstance(bound, int | float) or not 0 < bound <= maximum:
        return f"must be a number of seconds above 0 and at most {maximum:g}"
    return None


def read_below(root_fd: int, path: str, name: str) -> bytes:
    """The bytes of the file `path` below the directory `root_fd` holds: each part opened with `O_NOFOLLOW` from the
    directory before it, so that a link anywhere on the way, or a part swapped for one after a check, never leads out of
    the study; a regular file of at most `HOOK_MAX_BYTES`. `ShardsError` naming `name`, never the study's own path."""
    if problem := guard_path_problem(path):
        raise ShardsError(f"{name} {problem}")
    parts, fd = path.split("/"), os.dup(root_fd)
    try:
        for n, part in enumerate(parts):
            last = n == len(parts) - 1  # the file itself may be a FIFO that blocks: never waited on, and refused below
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_NONBLOCK if last else os.O_DIRECTORY)
            below = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = below
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ShardsError(f"{name} is not a regular file")
        chunks, size = [], 0
        while chunk := os.read(fd, CHUNK):
            size += len(chunk)
            if size > HOOK_MAX_BYTES:
                raise ShardsError(f"{name} is over {HOOK_MAX_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        why = {errno.ENOENT: "it does not exist", errno.ELOOP: "it is, or lies behind, a link",
               errno.ENOTDIR: "a part of its path is a link or not a directory"}  # fmt: skip
        raise ShardsError(f"{name} cannot be read: {why.get(error.errno, type(error).__name__)}") from None
    finally:
        os.close(fd)


def read_in_study(study: Path, key: str, path: str) -> bytes:
    """The bytes of the study's declared script `path` (`key` says which), read below the study's root."""
    return read_study_file(study, f"{HOOKS[key][0]} {path}", path)


def read_study_file(study: Path, name: str, path: str) -> bytes:
    """The bytes of the file `path` of the study (`name` says what it is in a message), read below the study's root."""
    try:
        fd = os.open(study, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise ShardsError(f"{name} cannot be read: the study's root cannot be opened") from None
    try:
        return read_below(fd, path, name)
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------ the key files
def home_files_path_problem(path: Any) -> str | None:
    """Why `path` cannot be the manifest's `home_files` (an absolute path of a plain string: no empty, `.` or `..`
    part), or None."""
    if isinstance(path, bool) or not isinstance(path, str) or not path or "\0" in path or not path.startswith("/"):
        return "must be an absolute path, a string"
    if any(part in ("", ".", "..") for part in path[1:].split("/")):
        return "must be a plain absolute path: no empty, `.` or `..` part, no trailing slash"
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return "must be a plain absolute path: no control character (it rides on a line)"
    return None


def home_files_overlap_problem(path: str, study: Path) -> str | None:
    """Why the key tree at `path` and the study cannot both be: the step copies the whole study to its tmpfs, so a tree
    inside it would be copied a second time into a directory a failed archive keeps, and a study inside the tree is no
    private tree. None when they are apart."""
    tree, where = Path(path), Path(os.path.realpath(study))
    if tree == where or tree.is_relative_to(where) or where.is_relative_to(tree):
        return "and the study overlap: the key tree must be outside the study, which the step copies whole"
    return None


@dataclass(frozen=True, repr=False)
class HomeTree:
    """The operator's key tree as `read_home_files` read it: its directories (parents first) and its files, the
    paths below its root and each file's bytes (empty unless kept). Names and bytes are secret: nothing prints one,
    and the representation says a count."""

    dirs: tuple[tuple[str, ...], ...]
    files: tuple[tuple[tuple[str, ...], bytes], ...]

    def __repr__(self) -> str:
        return f"HomeTree({len(self.files)} files)"


def _home_refusal(why: str, hint: str = "") -> ShardsError:
    """The refusal of a key tree: the manifest's key and the reason, never a path or a name of the tree."""
    return ShardsError(f"home_files {why}{hint}")


def _home_os_error(error: OSError) -> ShardsError:
    """An OS error on the key tree as a refusal: the errno's name only (a message holds the file's name)."""
    code = errno.errorcode.get(error.errno or 0, "an error")
    if error.errno in (errno.ELOOP, errno.ENOTDIR):  # Linux says either for a link met with O_NOFOLLOW | O_DIRECTORY
        return _home_refusal("is not a directory, or is, or lies behind, a link: give the resolved path")
    if error.errno == errno.ENOENT:
        return _home_refusal("does not exist, or changed while it was read")
    return _home_refusal(f"could not be read ({code})")


def _home_dir_ok(info: os.stat_result, uid: int, subject: str) -> None:
    """Refuse a directory that is not this user's or is open to group or other; `subject` begins the message."""
    if not stat.S_ISDIR(info.st_mode):
        raise _home_refusal(f"{subject} not a directory")
    if info.st_uid != uid:
        raise _home_refusal(f"{subject} not this user's")
    if info.st_mode & 0o077:
        raise _home_refusal(f"{subject} open to group or other (mode 0700 or stricter)")


def _home_scan(fd: int, parts: tuple[str, ...], found: dict[str, Any], uid: int, keep: bool) -> None:
    """Every entry of the directory `fd` holds: each classified by `lstat`, then opened with `O_NOFOLLOW` and checked
    again on the descriptor it is read through, so that a name swapped between the two is refused, never followed."""
    for entry in sorted(os.listdir(fd)):
        kid = (*parts, entry)
        listed = os.stat(entry, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(listed.st_mode):
            raise _home_refusal("holds a link")
        if stat.S_ISDIR(listed.st_mode):
            if len(kid) > HOME_FILES_MAX_DEPTH:
                raise _home_refusal(f"is more than {HOME_FILES_MAX_DEPTH} directories deep")
            if len(found["dirs"]) >= HOME_FILES_MAX_DIRS:
                raise _home_refusal(f"holds more than {HOME_FILES_MAX_DIRS} directories")
            sub = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                info = os.fstat(sub)
                if (info.st_dev, info.st_ino) != (listed.st_dev, listed.st_ino):
                    raise _home_refusal("changed while it was read")
                _home_dir_ok(info, uid, "holds a directory that is")
                found["dirs"].append(kid)
                _home_scan(sub, kid, found, uid, keep)
            finally:
                os.close(sub)
        elif stat.S_ISREG(listed.st_mode):
            if len(found["files"]) >= HOME_FILES_MAX_FILES:
                raise _home_refusal(f"holds more than {HOME_FILES_MAX_FILES} files")
            # a FIFO swapped in since the `lstat` must not block the open: it is refused below, never waited on
            file_fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
            try:
                info = os.fstat(file_fd)
                if (info.st_dev, info.st_ino) != (listed.st_dev, listed.st_ino) or not stat.S_ISREG(info.st_mode):
                    raise _home_refusal("changed while it was read")
                if info.st_uid != uid:
                    raise _home_refusal("holds a file that is not this user's")
                if info.st_mode & 0o177:
                    raise _home_refusal("holds a file open to group or other, or executable (mode 0600 or stricter)")
                found["bytes"] += info.st_size
                if found["bytes"] > HOME_FILES_MAX_BYTES:
                    raise _home_refusal(f"holds more than {HOME_FILES_MAX_BYTES} bytes")
                data = b""
                if keep:  # up to one byte past its size: a file that grew since the `fstat` is refused
                    chunks, left = [], info.st_size + 1
                    while left and (chunk := os.read(file_fd, left)):
                        chunks.append(chunk)
                        left -= len(chunk)
                    data = b"".join(chunks)
                    if len(data) != info.st_size:
                        raise _home_refusal("changed while it was read")
                found["files"].append((kid, data))
            finally:
                os.close(file_fd)
        else:
            raise _home_refusal("holds something that is neither a file nor a directory")


def read_home_files(path: str, *, keep: bool = True, uid: int | None = None) -> HomeTree:
    """The operator's key tree at `path` (the manifest's `home_files`), read once through descriptors: each part of the
    path opened with `O_NOFOLLOW` from `/` (a link on the way is refused, so the operator gives the resolved path),
    the directory and every directory in it this user's with no group or other bit, every other entry a regular file of
    this user's with no group, other or execute bit, within the bounds (`HOME_FILES_MAX_*`) and at least one file. The
    checks are made on the descriptors the bytes are read through (`keep`: held in the result; without it only the
    shape is checked, as `plan` and `lines` do), so nothing is checked and then read again. `ShardsError` naming
    `home_files` and the reason, never a path or a name of the tree."""
    if problem := home_files_path_problem(path):
        raise _home_refusal(problem)
    uid = os.geteuid() if uid is None else uid
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    found: dict[str, Any] = {"dirs": [], "files": [], "bytes": 0}
    try:
        parts = path[1:].split("/")
        for n, part in enumerate(parts):  # a directory on the way needs search permission only: O_PATH, not a read
            how = os.O_RDONLY if n == len(parts) - 1 else _O_PATH
            below = os.open(part, how | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = below
        _home_dir_ok(os.fstat(fd), uid, "is")
        _home_scan(fd, (), found, uid, keep)
    except OSError as error:
        raise _home_os_error(error) from None
    finally:
        os.close(fd)
    if not found["files"]:
        raise _home_refusal("holds no file")
    return HomeTree(tuple(found["dirs"]), tuple(found["files"]))


def _dir_below(root_fd: int, parts: Sequence[str]) -> int:
    """A descriptor of the directory `parts` below `root_fd`, each part opened with `O_NOFOLLOW`."""
    fd = os.dup(root_fd)
    try:
        for part in parts:
            below = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = below
        return fd
    except BaseException:
        os.close(fd)
        raise


def stage_home_files(tree: HomeTree, home: Path) -> int:
    """Write `tree` into the dispatcher's `home` (a directory the step made, 0700): directories 0700 and files 0600
    (set with `fchmod`, whatever the umask), made below a descriptor of `home` with `O_EXCL` and `O_NOFOLLOW`, so a
    link is never followed and nothing is overwritten. A directory the step made already (`.config`) is merged into
    (checked: this user's, 0700); a file that would replace anything is refused. The number of files written;
    `ShardsError` naming `home_files` otherwise, never a path or a name."""
    root = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    umask = os.umask(0o077)  # what this makes is the operator's alone, whatever umask the step was started under
    try:
        _home_dir_ok(os.fstat(root), os.geteuid(), "cannot be staged: the step's home is")
        for parts in tree.dirs:
            parent = _dir_below(root, parts[:-1])
            try:
                with contextlib.suppress(FileExistsError):  # one the step made (`HOME_DIRS`): checked below
                    os.mkdir(parts[-1], 0o700, dir_fd=parent)
                made = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                try:
                    os.fchmod(made, 0o700)
                    _home_dir_ok(os.fstat(made), os.geteuid(), "cannot be staged: a directory of the step's home is")
                finally:
                    os.close(made)
            finally:
                os.close(parent)
        for parts, data in tree.files:
            parent = _dir_below(root, parts[:-1])
            try:
                file_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o600, dir_fd=parent)  # fmt: skip
                try:
                    os.fchmod(file_fd, 0o600)
                    view = memoryview(data)
                    while view:
                        view = view[os.write(file_fd, view) :]
                finally:
                    os.close(file_fd)
            finally:
                os.close(parent)
    except FileExistsError:
        raise _home_refusal("holds a file where the step's own home has an entry") from None
    except OSError as error:
        raise _home_os_error(error) from None
    finally:
        os.umask(umask)
        os.close(root)
    return len(tree.files)


@contextlib.contextmanager
def _stop_held() -> Iterator[None]:
    """The stop signals (`STOP_SIGNALS`) held back (blocked, not ignored) while the key files are removed: one that
    arrives waits until they are gone, and is then delivered as it would have been."""
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set(STOP_SIGNALS))
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)


def _unwind_on_stop() -> Callable[[], None]:
    """Make each stop signal (`STOP_SIGNALS`) end this process through its `finally` blocks (exit 128 + the signal)
    until something else takes one over (`run_dispatcher`'s relay takes TERM and INT; the others stay ours while the
    dispatcher runs, which dies with its step), and return the function that gives back the handlers it replaced when
    they are still ours. A step that has staged the operator's key files would otherwise leave them on the node if it
    were told to stop before its dispatcher is running, or hung up on while it runs. Once stopped it ignores all of
    them, so a second signal cannot interrupt the cleanup."""
    signums = STOP_SIGNALS
    previous = {sig: signal.getsignal(sig) for sig in signums}
    fired = []

    def unwind(signum: int, _frame: Any) -> None:
        fired.append(signum)
        for sig in signums:
            signal.signal(sig, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    for sig in signums:
        signal.signal(sig, unwind)

    def disarm() -> None:
        for sig, old in previous.items():
            if old is not None and (fired or signal.getsignal(sig) is unwind):
                signal.signal(sig, old)

    return disarm


def pinned(plan: Plan) -> Plan:
    """The plan with the SHA-256 of each declared script (its pre-step guard, its post-run script) and of the expected
    render identity recorded, from the study as it is now: `plan` and `lines` call it, so that a file that leaves the
    study, is a link or is missing, or an expected record that is not well formed, refuses them before a line is
    printed. The same plan when none is declared."""
    found = {}
    for key in HOOKS:
        path = getattr(plan, key)
        if path is not None:
            found[f"{key}_sha256"] = hashlib.sha256(read_in_study(plan.study, key, path)).hexdigest()
    if plan.expect_render_identity is not None:
        name = expected_identity_name(plan.expect_render_identity)
        raw = read_study_file(plan.study, name, plan.expect_render_identity)
        parse_render_identity(raw, name)
        found["expect_render_identity_sha256"] = hashlib.sha256(raw).hexdigest()
    if plan.home_files is not None:  # a tree the step would refuse refuses `plan` and `lines` first
        if problem := home_files_overlap_problem(plan.home_files, plan.study):
            raise _home_refusal(problem)
        read_home_files(plan.home_files, keep=False)
    return replace(plan, **found)


# ------------------------------------------------------------------------------------------------ archives
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _measured(path: Path) -> tuple[int, str]:
    """The size and the SHA-256 of `path`'s bytes, from one pass over them: the size is what was read, not a stat's."""
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _open_plain(path: Path, label: str) -> BinaryIO:
    """`path` opened for reading as a regular file and nothing else, as evidence is: a link is never followed
    (`O_NOFOLLOW`), a pipe is never waited on (`O_NONBLOCK`) and what was opened must be a regular file.
    `ShardsError` naming the shard otherwise; an absent file is `FileNotFoundError`, as `open` says it."""
    not_plain = ShardsError(f"shard {label}: {path.name} is not a regular file: a link is never followed")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):  # a link (EMLINK where the system says it so)
            raise not_plain from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise not_plain
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _write_whole(path: Path, write: Callable[[Path], None]) -> None:
    """Write `path` through a draft beside it, fsynced and renamed: a reader sees the old file or the whole new one."""
    draft = path.with_name(path.name + ".part")
    draft.unlink(missing_ok=True)
    write(draft)
    with open(draft, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(draft, path)


def _private_dirs(directory: Path) -> None:
    """`directory` and each parent of it that is not there yet, made mode 0700 whatever the umask: `mkdir(mode=,
    parents=True)` gives a parent the umask's mode, so each missing one is made on its own. A directory that exists is
    left as it was found, since the operator may share it; the files a step writes into it are private themselves.
    Steps of other shards race to make the same directory: one that appears meanwhile is theirs, made the same way."""
    for missing in reversed([path for path in (directory, *directory.parents) if not path.exists()]):
        with contextlib.suppress(FileExistsError):
            missing.mkdir(mode=0o700)


def not_final_mark(reason: str) -> bytes:
    """The bytes of `NOT-FINAL.json`, which the core's merge refuses (`expcore.campaign.shard.NOT_FINAL_FILE`)."""
    return (json.dumps({"format": NOT_FINAL_FORMAT, "final": False, "reason": reason}, sort_keys=True) + "\n").encode()


def archive(
    out: Path,
    results: Path,
    spec: Mapping[str, Any],
    exit_status: int,
    pre_step: Mapping[str, Any] | None = None,
    post_run: Mapping[str, Any] | None = None,
    pass_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Write `out/` whole to `<results>/<label>.out.tar` and its manifest; the manifest, as written. `pre_step`, the
    record of the guard the step ran (`start_hooks`), and `post_run`, the record of the post-run script, are in it only
    when there is one; with a post-run record it also says `final`, whether that script passed. `pass_env` is the
    name and value of each variable the step passed to its dispatcher (`check_pass_env`), in it when there is one."""
    label = str(spec["label"])
    _private_dirs(results)
    tar_path = results / f"{label}.out.tar"
    files = sum(1 for p in out.rglob("*") if p.is_file() and not p.is_symlink())

    final = post_run is None or post_run.get("final") is True
    mark = None if final else not_final_mark(str(post_run.get("reason") or "the post-run script did not pass"))

    def write_tar(draft: Path) -> None:
        # the draft is made 0600 and the rename keeps it so: the archive carries `out/logs`, the text of a start
        # diagnostic withheld from every record, to the operator, who pulls it with `rsync -a`, which keeps modes
        descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle, tarfile.open(
            fileobj=handle, mode="w", format=tarfile.PAX_FORMAT
        ) as tar:
            # a shard not final carries the mark in its archive, written here from memory so that no full tmpfs can
            # leave it out; a file of that name in out/ is replaced by it, and in a final shard's it is left, for
            # `collect` and the merge to refuse
            def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
                return None if mark is not None and info.name == f"out/{NOT_FINAL}" else info

            tar.add(out, arcname="out", recursive=True, filter=keep)
            if mark is not None:
                info = tarfile.TarInfo(f"out/{NOT_FINAL}")
                info.size, info.mode, info.mtime = len(mark), 0o644, int(time.time())
                tar.addfile(info, io.BytesIO(mark))

    _write_whole(tar_path, write_tar)
    manifest = {
        "format": FORMAT,
        "label": label,
        "shard": dict(spec),
        "sha256": _sha256(tar_path),
        "bytes": tar_path.stat().st_size,
        "files": files,
        "lock": (out / LOCK_NAME).is_file(),
        "exit_status": exit_status,
        **({"pre_step": dict(pre_step)} if pre_step is not None else {}),
        **({"post_run": dict(post_run), "final": post_run.get("final") is True} if post_run is not None else {}),
        **({"pass_env": dict(pass_env)} if pass_env else {}),
    }
    _write_whole(results / f"{label}.out.json", _private_bytes((json.dumps(manifest, sort_keys=True) + "\n").encode()))
    return manifest


def verified(results: Path, shard: Shard, scratch: Path) -> tuple[Path, dict[str, Any]]:
    """A shard's archive, as a private copy in `scratch` whose bytes are the ones hashed, and its manifest, shown to be
    this shard's and whole; `ShardsError` naming the label otherwise. The copy, never the original, is what is
    unpacked, so an archive replaced after its check is never taken for the one checked. The archive and its manifest
    are read as regular files and never through a link (`_open_plain`): a link among the evidence refuses, as
    `evidence_entries` says, since a pull that keeps links could not reproduce it."""
    tar_path, json_path = results / f"{shard.label}.out.tar", results / f"{shard.label}.out.json"
    try:
        with _open_plain(json_path, shard.label) as handle:
            manifest = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        raise ShardsError(f"shard {shard.label}: no readable manifest of its out/") from None
    spec = manifest.get("shard") if isinstance(manifest, Mapping) else None
    if manifest.get("format") != FORMAT or not isinstance(spec, Mapping):
        raise ShardsError(f"shard {shard.label}: its manifest is not {FORMAT}")
    if (spec.get("index"), spec.get("count"), spec.get("label")) != (shard.index, shard.count, shard.label):
        raise ShardsError(
            f"shard {shard.label}: its archive is of another shard ({spec.get('index')}/{spec.get('count')})"
        )
    if manifest.get("lock") is not True:
        raise ShardsError(f"shard {shard.label}: its out/ has no {LOCK_NAME}, which the merge needs")
    copy = scratch / f".{shard.label}.out.tar"
    digest = hashlib.sha256()
    try:
        with _open_plain(tar_path, shard.label) as source, open(copy, "xb") as target:
            while chunk := source.read(CHUNK):
                digest.update(chunk)
                target.write(chunk)
    except FileNotFoundError:
        copy.unlink(missing_ok=True)
        raise ShardsError(f"shard {shard.label}: its archive is missing") from None
    if digest.hexdigest() != manifest.get("sha256"):
        copy.unlink()
        raise ShardsError(f"shard {shard.label}: its archive is not the one its manifest names")
    return copy, manifest


def _in_out(path: str) -> bool:
    return path == "out" or path.startswith("out/")


def _link_stays_in_out(member: tarfile.TarInfo) -> bool:
    """A link's target is relative, has no `..` after a name (`a/../b` is `b` only while `a` is not a link) and lands in
    `out/`: from the link's directory for a symbolic link, from the archive's root for a hard link."""
    target = member.linkname
    names = [part for part in target.split("/") if part not in ("", ".")]
    ascent = next((n for n, part in enumerate(names) if part != ".."), len(names))  # the leading `..` parts
    if not target or target.startswith("/") or ".." in names[ascent:]:
        return False
    base = (
        "" if member.islnk() else posixpath.dirname(posixpath.normpath(member.name))
    )  # `tarfile` drops a trailing `/`
    return _in_out(posixpath.normpath(posixpath.join(base, target)))


def archive_problem(members: Sequence[tarfile.TarInfo]) -> str | None:
    """Why an archive is not an `out/` and nothing else, as a short class that carries nothing the archive wrote, or
    None. `tarfile`'s data filter judges a member against the directory unpacked into, so it lets `out/../x` and a link
    to `../x` land beside `out/`; here `out/` is the boundary. A member's name has no `..` and is `out` (a directory)
    or under `out/`, a link stays in `out/` (`_link_stays_in_out`), and no member's path (a hard link's target too)
    passes through a link of the archive, in whatever order, so that every link sits in a real directory. A hard link's
    target is no link of the archive either, a symbolic link or another hard link: extraction would make the hard link
    a symbolic link (CPython 3.12 and 3.13) and a member written under its name goes through it, beside `out/`. A
    hard link made by `tarfile` or GNU tar names the first member of its file, which is a regular file."""
    if not members:
        return "holds something besides out/"
    links = {posixpath.normpath(m.name) for m in members if m.issym() or m.islnk()}

    def through_a_link(path: str) -> bool:
        parent = posixpath.dirname(posixpath.normpath(path))
        while parent and parent not in links:
            parent = posixpath.dirname(parent)
        return bool(parent)

    for member in members:
        if ".." in member.name.split("/"):
            return "has a member name with a '..' component"
        if not _in_out(member.name) or (posixpath.normpath(member.name) == "out" and not member.isdir()):
            return "holds something besides out/"
        if (member.issym() or member.islnk()) and not _link_stays_in_out(member):
            return "has a link that leaves out/"
        if through_a_link(member.name) or (member.islnk() and through_a_link(member.linkname)):
            return "has a member written through a link"
        if member.islnk() and posixpath.normpath(member.linkname) in links:
            return "has a member written through a link"
    return None


def unpack(tar_path: Path, into: Path, *, label: str | None = None) -> Path:
    """Unpack an archive's `out/` into `into` (new). An archive with a member that leaves `out/` (`archive_problem`) is
    refused whole, before `into` is made or anything is extracted, naming the shard `label` and the kind of problem,
    never a member. `tarfile`'s data filter stays on, for the rest it refuses (special files, absolute links)."""
    with tarfile.open(tar_path) as tar:
        if problem := archive_problem(tar.getmembers()):
            raise ShardsError(f"shard {label}: its archive {problem}" if label else f"an archive {problem}")
        into.mkdir(parents=True)
        tar.extractall(into, filter="data")
    _private_logs(into / "out")
    return into / "out"


LOCAL_LOGS = "logs"  # the campaign's local logs under out/ (`expcore.campaign.defect_log.LOG_DIR`)


def _private_logs(out: Path) -> None:
    """`out/logs/` made the operator's alone, whatever the archive recorded and whatever the umask is: the directory
    0700 and each file in it 0600. The local log holds the text of a start diagnostic that the core withheld from every
    record, and the archive carries it here as private evidence; `tarfile`'s data filter keeps a file's recorded mode
    (a node's loose umask included) and gives a directory the umask's, so neither is left to it. A link is never
    followed or changed."""
    logs = out / LOCAL_LOGS
    if logs.is_symlink() or not logs.is_dir():
        return
    for base, _dirs, files in os.walk(logs):
        os.chmod(base, 0o700)
        for name in files:
            path = Path(base, name)
            if not path.is_symlink():
                os.chmod(path, 0o600)


def _read_only(root: Path) -> None:
    for base, dirs, files in os.walk(root, topdown=False):
        for name in (*files, *dirs):
            path = Path(base, name)
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


def plan_id(plan: Plan) -> str:
    """The SHA-256 of the plan as its manifest declares it: the study and results directories, and each shard's index,
    count, label and federation root, in order. It names the plan, not the campaign: a shard's own manifest carries only
    an index, a count and a label, and the campaign's fingerprint travels in each shard's export, which the merge
    compares (C36). Labels alone would not tell plans apart, since a fleet's shards are always `shard-1` to `shard-N`.
    It is a hash, so no path is in the manifest, and every `collect` of one plan gives the same."""
    declared = {
        "study": str(plan.study),
        "results": str(plan.results),
        "shards": [[s.index, s.count, s.label, str(s.root)] for s in plan.shards],
    }
    return hashlib.sha256(json.dumps(declared, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _files_under(root: Path, results: Path, label: str) -> list[str]:
    """Every regular file under `root`, by its name relative to `results` (POSIX, sorted); none when `root` is not
    there. A link, or anything that is neither a regular file nor a directory, is `ShardsError` naming the shard: a
    link is never followed, and a pull of a tree holding one would not be of the bytes hashed."""
    try:
        top = root.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(top.st_mode):
        raise ShardsError(f"shard {label}: {root.name} is not a plain directory")

    def raise_it(error: OSError) -> None:
        raise error

    found: list[str] = []
    for base, dirs, files in os.walk(root, onerror=raise_it):
        for name in (*dirs, *files):
            if not stat.S_ISREG(mode := os.lstat(os.path.join(base, name)).st_mode) and not stat.S_ISDIR(mode):
                raise ShardsError(f"shard {label}: {name}, under {root.name}/, is not a regular file or a directory")
        found += [Path(base, name).relative_to(results).as_posix() for name in files]
    return sorted(found)


def _file_when_present(results: Path, name: str, label: str) -> list[str]:
    """`[name]` (relative to `results`) when it is there as a regular file, `[]` when it is not there; `ShardsError`
    naming the shard for a link or anything else, which is never followed."""
    try:
        mode = (results / name).lstat().st_mode
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(mode):
        raise ShardsError(f"shard {label}: {name.rsplit('/', 1)[-1]} is not a regular file: a link is never followed")
    return [name]


def newer_snapshot(plan: Plan, shard: Shard, archive_sha: str, scratch: Path) -> Path | None:
    """The snapshot of `shard` that is newer than its archive (`archive_sha`: what the verified archive's manifest
    names) and that `--resume` would restore, or None. A snapshot names the archive its step started from (`base`), so
    one that names this archive was taken by a step that began from it and did not archive: it holds the runs that step
    reached and the archive lacks (the merge does not have them). Every older one (a snapshot of the step that wrote
    the archive names an earlier archive or none) holds nothing the archive lacks. The pick is `restore_newest`'s, with
    this archive as the base: restored into a private directory of `scratch`, which is removed, so that it is the
    newest that verifies (every file against its hash, the store's integrity, every ended attempt's marker)."""
    try:
        with os.scandir(plan.results / shard.label / SNAPSHOTS) as listing:
            if not any(entry.name.startswith("snap-") for entry in listing):
                return None
    except FileNotFoundError:
        return None
    spec = {"index": shard.index, "count": shard.count, "label": shard.label}
    check = Path(tempfile.mkdtemp(prefix=".snapshot-check-", dir=scratch))
    try:
        found = restore_newest(plan.results, shard.label, spec, archive_sha, check / "out")
    finally:
        remove_tree(check)
    return found


def evidence_entries(
    plan: Plan, found: Sequence[tuple[Path, Mapping[str, Any]]], scratch: Path
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """What lies beside each verified archive and would be lost with the scratch: its archive manifest
    (`<label>.out.json`), every file under `<label>/journals/` (a spend journal per step: what a step killed outright
    spent), and the snapshot newer than the archive when there is one (`newer_snapshot`), each as name (relative to the
    results directory), size and SHA-256 of the bytes read, sorted by name. The archive manifest is shown to name the
    archive that was verified, so the two listed are a pair. A link among them refuses the collect; so does a file that
    cannot be read. `<label>/pass-env.json`, the shard's first-step `pass_env` (private), is listed when it is there.
    Snapshots older than the archive are resume material and not listed (README). Returns the entries
    and, for each shard that has a snapshot newer than its archive, its (label, snapshot name)."""
    entries: list[dict[str, Any]] = []
    ahead: list[tuple[str, str]] = []
    for shard, (_, manifest) in zip(plan.shards, found, strict=True):
        try:
            with _open_plain(plan.results / f"{shard.label}.out.json", shard.label) as handle:
                own = handle.read()
            try:
                named = json.loads(own).get("sha256")
            except (ValueError, AttributeError):
                named = None
            if named != manifest["sha256"]:
                raise ShardsError(f"shard {shard.label}: its manifest no longer names the archive that was verified")
            entries.append({"name": f"{shard.label}.out.json", "bytes": len(own),
                            "sha256": hashlib.sha256(own).hexdigest()})  # fmt: skip
            names = _files_under(plan.results / shard.label / JOURNALS, plan.results, shard.label)
            names += _file_when_present(plan.results, f"{shard.label}/{PASS_ENV}", shard.label)
            snapshot = newer_snapshot(plan, shard, manifest["sha256"], scratch)
            if snapshot is not None:
                names += _files_under(snapshot, plan.results, shard.label)
                ahead.append((shard.label, snapshot.name))
            for name in names:
                size, digest = _measured(plan.results / name)
                entries.append({"name": name, "bytes": size, "sha256": digest})
        except OSError as error:
            raise ShardsError(f"shard {shard.label}: its evidence could not be read ({type(error).__name__})") from None
    return sorted(entries, key=lambda entry: entry["name"]), ahead


def archive_entries(plan: Plan, found: Sequence[tuple[Path, Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Each verified archive's name, size and SHA-256 (`found`, in the plan's order). The size is the private copy's,
    whose
    bytes were hashed, never a step's claim; `verified` raised unless the SHA-256 of those bytes is the manifest's, so
    that value is theirs. Taken before the copies are unpacked and removed."""
    return [
        {"name": f"{shard.label}.out.tar", "bytes": copy.stat().st_size, "sha256": manifest["sha256"]}
        for shard, (copy, manifest) in zip(plan.shards, found, strict=True)
    ]


def archives_payload(plan: Plan, entries: Sequence[Mapping[str, Any]], evidence: Sequence[Mapping[str, Any]]) -> bytes:
    """The bytes of `archives.sha256.json`: the plan's id, each archive's entry and the `evidence` beside them
    (`evidence_entries`, and each federation's root archive). Names are relative and there is no timestamp:
    collecting one plan twice, with nothing changed in its results, writes the same bytes."""
    document = {
        "format": ARCHIVES_FORMAT,
        "plan_id": plan_id(plan),
        "archives": list(entries),
        "evidence": list(evidence),
    }
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()


def write_archives_manifest(
    plan: Plan, found: Sequence[tuple[Path, Mapping[str, Any]]], evidence: Sequence[Mapping[str, Any]]
) -> Path:
    """`<results>/archives.sha256.json`, written whole beside the archives `verified` read (`found`, in the plan's
    order): `archives_payload` of `archive_entries` and `evidence`. `collect` writes it itself, last."""
    path = plan.results / ARCHIVES_MANIFEST
    payload = archives_payload(plan, archive_entries(plan, found), evidence)
    try:
        _write_whole(path, lambda draft: draft.write_bytes(payload))
    except OSError as error:
        raise ShardsError(f"the archives manifest could not be written ({type(error).__name__})") from None
    return path


def final_problem(shard: Shard, manifest: Mapping[str, Any], wanted: str | None) -> str | None:
    """Why a verified shard's results are not to be merged, or None: its manifest says they are not final (the
    study's post-run script did not pass), or, with `wanted` (the plan's SHA-256 of the post-run script), does not say
    that this script passed (a step whose line dropped it)."""
    record = manifest.get("post_run")
    if manifest.get("final") is False:
        reason = record.get("reason") if isinstance(record, Mapping) else None
        return f"shard {shard.label}: its results are not final ({reason or 'its post-run script did not pass'})"
    if wanted is not None and not (
        manifest.get("final") is True and isinstance(record, Mapping) and record.get("sha256") == wanted
    ):
        return (
            f"shard {shard.label}: its step did not run the plan's post-run script to a pass: its results are not final"
        )
    return None


def pass_env_problem(shard: Shard, manifest: Mapping[str, Any], declared: Sequence[str]) -> str | None:
    """Why a verified shard's results are not to be merged for the variables its step was given, or None: the plan's
    `pass_env` names exactly the variables its manifest records, each with a value a variable may carry. `step` never
    reads the plan, so a line that dropped a declared name (or gained one) is shown here, as a line that dropped the
    post-run script is (`final_problem`). The message names the variable, never its value."""
    record = manifest.get("pass_env")
    given = record if isinstance(record, Mapping) else {}
    for name in declared:
        if name not in given:
            return (f"shard {shard.label}: its step was not given {name}, which the plan declares in pass_env (a line "
                    "that dropped it): its results are not taken")  # fmt: skip
    for name, value in given.items():
        if name not in declared:
            return f"shard {shard.label}: its step was given {name}, which the plan does not declare in pass_env"
        if pass_env_value_problem(str(name), value):
            return f"shard {shard.label}: its manifest records a value of {name} that pass_env may not carry"
    return None


def _evidence() -> Any:
    """`hpcevidence`, loaded by its path (the root's allow-list, its archive and its marker)."""
    return _sibling("hpcevidence")


def root_archives(plan: Plan) -> list[Any]:
    """Each federation's root evidence as bytes (`hpcevidence.archive_root`), in the plan's order; nothing is written. A
    root that is gone is shown by the root archive an earlier collect wrote (`earlier_archive`), or refused.
    `ShardsError`
    naming the shard and the file when a required file is missing, one is a link or holds a secret, or the root
    cannot be read."""
    module, found = _evidence(), []
    for shard in plan.shards:
        if (plan.results / shard.label / RUNNING_MARK).is_file():  # a step runs, or was killed: its end is not in
            print(f"shards: WARNING: shard {shard.label}: a step of it is marked running ({RUNNING_MARK}): its "
                  "root's evidence is archived as it is now, and a step that starts after the collect makes `down` "
                  "refuse again", file=sys.stderr)  # fmt: skip
        try:
            try:
                found.append(module.archive_root(shard.root, shard.label, root_id_of(shard)))
            except module.RootGone:
                found.append(module.earlier_archive(plan.results, shard.label, root_id_of(shard)))
                print(
                    f"shards: WARNING: shard {shard.label}: its root is gone: the root archive of an earlier collect "
                    "is listed as it is",
                    file=sys.stderr,
                )
        except module.EvidenceError as error:
            raise ShardsError(f"shard {shard.label}: {error}") from None
        except OSError as error:
            raise ShardsError(
                f"shard {shard.label}: its root's evidence could not be read ({type(error).__name__})"
            ) from None
    return found


def root_id_of(shard: Shard) -> str:
    """A hash of the shard's federation's root path, which the root archive's manifest records (no path): the root
    archives of one shard label that were made of two federations (a shard that moved) are told apart by it."""
    return root_id_for(shard.root)


def root_id_for(root: Path) -> str:
    return hashlib.sha256(f"expcore-root-id:{root}".encode()).hexdigest()


def pair_entries(stem: str, held: Any) -> list[dict[str, Any]]:
    """A root archive and its manifest, as `<stem>.root.tar` and `<stem>.root.json`: name, size and SHA-256 of the bytes
    that are written (or, kept, were)."""
    return [
        {"name": f"{stem}{suffix}", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for suffix, payload in ((".root.tar", held.tar), (".root.json", held.manifest))
    ]


def root_entries(plan: Plan, archives: Sequence[Any]) -> list[dict[str, Any]]:
    """Each root archive and its manifest as name, size and SHA-256 of the bytes that are written (or, kept, were)."""
    entries: list[dict[str, Any]] = []
    for shard, held in zip(plan.shards, archives, strict=True):
        entries += pair_entries(shard.label, held)
    return entries


def _fsync_dir(path: Path) -> None:
    """The directory's entries made durable, where the filesystem lets a directory be synced: one that says it cannot
    (`EINVAL`, `ENOTSUP`) is left as it is, as it always was. Every other error, an I/O error above all, is raised
    (`OSError`) with the open's too: a collect that could not make its manifest durable must not mark its roots
    collected, so that `down` cannot remove evidence a crash could still lose."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(descriptor)


def _make_parents(directory: Path) -> list[Path]:
    """The directories above `into` that did not exist, made (outermost first) and returned, so that a refused collect
    can remove them."""
    made = []
    for missing in reversed([p for p in (directory, *directory.parents) if not p.exists()]):
        missing.mkdir()
        made.append(missing)
    return made


def _put_back(into: Path, existed: bool) -> None:
    """`into` as `collect` found it, after its trees were moved in: gone, or an empty private directory if it was."""
    with contextlib.suppress(OSError):
        remove_tree(into)
        if existed:
            into.mkdir(mode=0o700)


def _verify_shards(plan: Plan, stage: Path, refuse_newer: bool) -> tuple[list[tuple[Path, Mapping[str, Any]]], list]:
    """Every shard's archive verified into a private copy in `stage`, its results final, and the files beside it listed
    (`evidence_entries`); `ShardsError` naming the shard otherwise, and for a snapshot newer than its archive when the
    merge follows (`refuse_newer`)."""
    wanted = None
    if plan.post_run is not None:  # the script the plan declares, as the study holds it now
        wanted = pinned(replace(plan, pre_step=None, expect_render_identity=None, home_files=None)).post_run_sha256
    found = []
    for shard in plan.shards:
        found.append(verified(plan.results, shard, stage))
        if problem := final_problem(shard, found[-1][1], wanted) or pass_env_problem(
            shard, found[-1][1], plan.pass_env
        ):
            raise ShardsError(problem)
    evidence, ahead = evidence_entries(plan, found, stage)
    if ahead and refuse_newer:
        who = "; ".join(f"shard {label}: snapshot {name}" for label, name in ahead)
        raise ShardsError(
            f"a snapshot is newer than its archive ({who}), so the merge would leave out the runs it holds: resume "
            "the shard so that they reach a new archive, or run collect without --run and merge the line it prints "
            "yourself, knowing they are left out"
        )
    for label, name in ahead:
        print(
            f"shards: WARNING: shard {label}: snapshot {name} is newer than its archive (a step resumed from the "
            "archive was killed before it archived): it holds runs the merge leaves out; it is in the "
            f"{ARCHIVES_MANIFEST} to pull and check, and `--resume` continues from it",
            file=sys.stderr,
        )
    return found, evidence


def _unpack_shards(plan: Plan, found: Sequence[tuple[Path, Mapping[str, Any]]], stage: Path) -> None:
    """Each verified private copy unpacked into `stage/<label>/out`, checked and made read-only; the copy removed."""
    for shard, (tar_path, _) in zip(plan.shards, found, strict=True):
        try:
            out = unpack(tar_path, stage / shard.label, label=shard.label)  # its own refusal names the shard
        except (tarfile.TarError, OSError) as error:  # a refusal of the tar module
            raise ShardsError(
                f"shard {shard.label}: its archive could not be unpacked ({type(error).__name__})"
            ) from None
        tar_path.unlink()
        if not (out / LOCK_NAME).is_file():
            raise ShardsError(f"shard {shard.label}: its unpacked out/ has no {LOCK_NAME}")
        if os.path.lexists(out / NOT_FINAL):
            raise ShardsError(f"shard {shard.label}: its out/ is marked not final ({NOT_FINAL})")
        _read_only(out)


def _private_bytes(payload: bytes) -> Callable[[Path], None]:
    """What `_write_whole` calls with its draft: `payload` written there, mode 0600 (the evidence is private)."""

    def write(draft: Path) -> None:
        descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)

    return write


def _set_aside(plan: Plan, shard: Shard, up_id: str) -> None:
    """The root archive of another federation under this shard's label (the shard moved) put aside, not overwritten:
    both files renamed to `<label>.<12 hex digits of the archive's SHA-256>.root.*`, which `aside_entries` lists."""
    stem = _evidence().stem_of(plan.results, shard.label, root_id_of(shard), up_id)
    if stem is None:
        return
    for suffix in ROOT_SUFFIXES:
        if os.path.lexists(plan.results / f"{shard.label}{suffix}"):
            os.replace(plan.results / f"{shard.label}{suffix}", plan.results / f"{shard.label}.{stem}{suffix}")


def aside_entries(plan: Plan) -> list[dict[str, Any]]:
    """The root archives an earlier collect put aside (`_set_aside`: `<label>.<12 hex digits>.root.tar` and its
    manifest), each pair shown to be one another's, as name, size and SHA-256, so that `verify-copy` checks what a moved
    shard's first federation left: `ShardsError` for a pair that is not, and for an archive with no manifest beside it
    (it cannot be verified, and the listing is made from the manifests)."""
    module, entries = _evidence(), []
    for path in sorted(plan.results.glob("*.root.tar")):  # one with no manifest beside it is listed by none
        if ASIDE_TAR.fullmatch(path.name) and not os.path.lexists(
            path.with_name(path.name.removesuffix(".tar") + ".json")
        ):
            raise ShardsError(
                f"{path.name} is a root archive put aside for a shard that moved with no manifest beside it, so it "
                "cannot be verified: restore its manifest, or move the archive out of the results directory"
            )
    for path in sorted(plan.results.glob("*.root.json")):
        found = ASIDE_NAME.fullmatch(path.name)
        if found is None:
            continue
        try:
            held = module.verified_pair(plan.results, path.name.removesuffix(".root.json"), found["label"])
        except module.EvidenceError as error:
            raise ShardsError(f"{error}: the root archive put aside for a shard that moved cannot be used") from None
        stem = path.name.removesuffix(".root.json")
        for suffix, payload in ((".root.tar", held.tar), (".root.json", held.manifest)):
            entries.append(
                {"name": f"{stem}{suffix}", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            )
    return entries


def _publish(plan: Plan, archives: Sequence[Any]) -> None:
    """Each root archive and its manifest written whole into the results directory, mode 0600 (an earlier one, kept
    because its root is gone, is not); the archive of another federation under the same label put aside first."""
    try:
        for shard, held in zip(plan.shards, archives, strict=True):
            if held.reused:
                continue
            _set_aside(plan, shard, held.up_id)
            _write_whole(plan.results / f"{shard.label}.root.tar", _private_bytes(held.tar))
            _write_whole(plan.results / f"{shard.label}.root.json", _private_bytes(held.manifest))
    except OSError as error:
        raise ShardsError(
            f"a root evidence archive or the archives manifest could not be written ({type(error).__name__})"
        ) from None


def _mark_roots(plan: Plan, archives: Sequence[Any]) -> None:
    """The collected-evidence marker into each archived root (`hpcevidence.write_marker`): what `down` reads. The
    collect is done by now (the trees are in place and the manifest is written), so a marker that cannot be written
    is a warning naming the shard, never a refusal: that root stays locked against `down` until a collect writes it."""
    module = _evidence()
    for shard, held in zip(plan.shards, archives, strict=True):
        if held.reused:  # its root is gone: there is nothing to mark
            continue
        try:
            module.write_marker(shard.root, shard.label, held.sha256, held.steps_sha256, held.hashes)
        except (OSError, module.EvidenceError) as error:
            print(
                f"shards: WARNING: shard {shard.label}: the collected-evidence marker could not be written in its root "
                f"({type(error).__name__}); the results are collected and the manifest is written, but `down` refuses "
                "that root until a collect writes it (or --discard-evidence)",
                file=sys.stderr,
            )


def _install(
    plan: Plan,
    stage: Path,
    into: Path,
    existed: bool,
    entries: Sequence[Mapping[str, Any]],
    listed: Sequence[Mapping[str, Any]],
) -> bytes:
    """The trees into `into` (one rename of `stage` onto it, which may exist only if empty), then
    `archives.sha256.json` as the last write (a draft, fsynced, renamed), then the results directory synced so that its
    rename is durable (`_fsync_dir`); its bytes. Anything that goes wrong after the trees are in, whatever it is, puts
    `into` back as it was found, and the markers that unlock `down` are written only after this returns."""
    try:
        os.rename(stage, into)  # onto nothing, or onto the empty directory: one step, never a partial one
    except OSError as error:
        raise ShardsError(f"the unpacked trees could not be moved into place ({type(error).__name__})") from None
    try:
        payload = archives_payload(plan, entries, listed)
        _write_whole(plan.results / ARCHIVES_MANIFEST, lambda draft: draft.write_bytes(payload))
    except BaseException as error:
        _put_back(into, existed)
        if isinstance(error, OSError):
            raise ShardsError(f"the archives manifest could not be written ({type(error).__name__})") from None
        raise
    try:
        _fsync_dir(plan.results)
    except BaseException as error:
        _put_back(into, existed)
        if isinstance(error, OSError):
            kind = errno.errorcode.get(error.errno or 0, type(error).__name__)
            raise ShardsError(
                f"the results directory could not be synced ({kind}): the manifest is not known to be durable, so "
                "the collect is refused and no root is marked collected"
            ) from None
        raise
    return payload


def collect_hashed(plan: Plan, into: Path, refuse_newer: bool = False) -> tuple[list[Path], str]:
    """`collect`'s outs and the SHA-256 of the archives manifest it wrote (the bytes written, not a file read again).

    Atomic (G9). Everything is made in a private directory beside `into` (0700, in its parent): every archive verified
    into a private copy, every shard unpacked and checked there, every root's evidence read. Only when all of it holds
    is anything published, in this order: each root archive and its manifest into the results directory, the trees into
    `into`, and `archives.sha256.json` last. Then the collected-evidence marker in each root, which unlocks `down` and
    so comes after the manifest. A refusal before the manifest leaves `into` as it was found (new, or an empty private
    directory) and no marker, and the results directory as it was apart from the root archives (a refusal at the rename
    leaves them, which the next collect overwrites); a manifest that cannot be written puts `into` back. A marker that
    cannot be written is a warning naming the shard, not a refusal: the collect is done and `down` stays
    locked for that root."""
    if into.exists() and any(into.iterdir()):
        raise ShardsError("the directory to collect into must be new or empty")
    existed = into.exists()
    if existed:
        seen = into.lstat()
        if not stat.S_ISDIR(seen.st_mode) or seen.st_uid != os.getuid() or seen.st_mode & 0o077:
            raise ShardsError("the directory to collect into must be a private directory of this user (0700)")
    made: list[Path] = []
    stage: Path | None = None
    done = False
    try:
        made += _make_parents(into.parent)
        try:
            stage = Path(tempfile.mkdtemp(prefix=".collect-", dir=into.parent))  # 0700, ours, beside `into`
        except OSError as error:
            raise ShardsError(
                f"a private directory beside the one to collect into could not be made ({type(error).__name__})"
            ) from None
        found, evidence = _verify_shards(plan, stage, refuse_newer)
        entries = archive_entries(plan, found)
        archives = root_archives(plan)
        _unpack_shards(plan, found, stage)
        _publish(plan, archives)
        listed = sorted([*evidence, *root_entries(plan, archives), *aside_entries(plan)], key=lambda e: e["name"])
        payload = _install(plan, stage, into, existed, entries, listed)
        done = True
        _mark_roots(plan, archives)
    finally:
        if stage is not None and stage.exists():
            remove_tree(stage)
        if not done:  # the directories this collect made above `into`, once empty (rmdir refuses the others)
            for directory in reversed(made):
                with contextlib.suppress(OSError):
                    directory.rmdir()
    return [into / shard.label / "out" for shard in plan.shards], hashlib.sha256(payload).hexdigest()


def collect(plan: Plan, into: Path, refuse_newer: bool = False) -> list[Path]:
    """Every shard's out/, verified and unpacked read-only into `<into>/<label>/out`; refused whole if any fails, when
    any is not final (`final_problem`) and when any step was not given the plan's `pass_env` (`pass_env_problem`),
    naming it. Atomic: see `collect_hashed`, which also gives the hash of the
    archives manifest. Beside each shard's archive it writes the federation's root evidence for the copy back
    (`<label>.root.tar`: an allow-list of what `down` removes with the root, never a secret) and a marker in the
    root, and
    lists them, the journals and the archive manifests in `archives.sha256.json` for `verify-copy`. Run it once every
    step has ended and while the federation is up: a journal still being written is listed as it was read, and a pull
    of it later would differ. A shard with a snapshot newer than its archive (a step resumed from the archive was killed
    before it archived) holds runs the merge would omit: `collect` warns, naming the shard and the snapshot, and lists
    the snapshot for the copy back; with `refuse_newer` (the merge follows at once: `--run`) it refuses instead, since
    the merge has no flag to leave a shard short and would analyse an incomplete campaign."""
    return collect_hashed(plan, into, refuse_newer)[0]


def collect_roots(plan: Plan, label: str | None = None) -> list[tuple[str, str]]:
    """Only the roots' evidence, of every shard or of `label`'s: each archived (`root_archives`) and published, and
    marked collected, as `collect` does, without verifying any shard's archive, unpacking or writing
    `archives.sha256.json`. For a shard about to move to another federation (`fleet.py down --member`), whose first
    federation's root `down` would otherwise refuse, when the other shards are not final and a full `collect` is
    refused.
    Each shard's (label, root archive SHA-256). The next full collect lists the archive (and an archive of a root since
    set aside) for `verify-copy`."""
    chosen = tuple(shard for shard in plan.shards if label in (None, shard.label))
    if not chosen:
        raise ShardsError(f"the plan has no shard {label}")
    only = replace(plan, shards=chosen)
    archives = root_archives(only)
    _publish(only, archives)
    _mark_roots(only, archives)
    return [(shard.label, held.sha256) for shard, held in zip(chosen, archives, strict=True)]


# ------------------------------------------------------------------------------------------------ one root, no plan
def federation_root(root: Path) -> Path:
    """`root` as it is given, made absolute, shown to be a federation's root: a real directory (a link is never
    followed) that holds its state file as a regular file. `ShardsError` naming what is wrong otherwise."""
    path = Path(os.path.abspath(root))
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise ShardsError("the root is not there") from None
    except OSError as error:
        raise ShardsError(f"the root cannot be read ({type(error).__name__})") from None
    if stat.S_ISLNK(info.st_mode):
        raise ShardsError("the root is a link, which is never followed: give the federation's own directory")
    if not stat.S_ISDIR(info.st_mode):
        raise ShardsError("the root is not a directory")
    try:
        state = os.lstat(path / STATE_FILE)
    except OSError:
        state = None
    if state is None or not stat.S_ISREG(state.st_mode):
        raise ShardsError(f"the root is not a federation root: {STATE_FILE} is missing there, or is not a plain file")
    return path


def copy_directory(root: Path, into: Path) -> Path:
    """`into` made absolute and shown fit to hold a root's copy: not the root, inside it or above it (`down` removes
    the root with whatever is in it, so a copy there is no copy), not a link, and, where it exists, a directory that
    is private to this user. Whether it is empty is for the caller. `ShardsError` otherwise."""
    path = Path(os.path.abspath(into))
    here, there = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    if here == there or here.is_relative_to(there):
        raise ShardsError("the directory to collect into is the root or inside it: `down` removes it with the root")
    if there.is_relative_to(here):
        raise ShardsError(
            "the directory to collect into holds the root: the copy would sit beside the evidence it copies"
        )
    try:
        seen = os.lstat(path)
    except FileNotFoundError:
        return path
    except OSError as error:
        raise ShardsError(f"the directory to collect into cannot be read ({type(error).__name__})") from None
    if stat.S_ISLNK(seen.st_mode):
        raise ShardsError("the directory to collect into is a link, which is never followed")
    if not stat.S_ISDIR(seen.st_mode) or seen.st_uid != os.getuid() or seen.st_mode & 0o077:
        raise ShardsError("the directory to collect into must be a private directory of this user (0700)")
    return path


def holds_copy(into: Path, archive_sha256: str | None) -> str | None:
    """The SHA-256 of the archives manifest in `into`, when `into` holds exactly the copy a root-only collect makes
    (the manifest, the archive and its manifest and no other file), the copy verifies against the manifest and the
    archive is the one the root's marker names (`archive_sha256`); None otherwise, and when there is no marker. The
    archive and its manifest must also be one another's, as `hpcevidence.verified_pair` shows them (the manifest names
    the archive's hash and size and lists exactly the files it holds): the manifest of `archives.sha256.json` only says
    what its bytes are, so a manifest swapped for another and its line there updated would otherwise pass for the
    copy. `ShardsError` naming the manifest when they are not: the copy is not what the root's marker vouches for."""
    if archive_sha256 is None or {entry.name for entry in into.iterdir()} != {
        *(f"{ROOT_ONLY_STEM}{s}" for s in ROOT_SUFFIXES),
        ARCHIVES_MANIFEST,
    }:
        return None
    try:
        raw = (into / ARCHIVES_MANIFEST).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        document = json.loads(raw)
        if (
            document.get("format") != ROOT_ARCHIVES_FORMAT
            or verify_copy(into / ARCHIVES_MANIFEST, into, digest).problems
        ):
            return None
        archive = {entry["name"]: entry["sha256"] for entry in document["evidence"]}[f"{ROOT_ONLY_STEM}.root.tar"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ShardsError):
        return None
    if archive != archive_sha256:
        return None
    module = _evidence()
    try:
        module.verified_pair(into, ROOT_ONLY_STEM, ROOT_ONLY_STEM)
    except module.EvidenceError as error:
        raise ShardsError(f"{error}: the copy in the directory is not what the root's marker vouches for") from None
    return digest


def root_only_payload(held: Any) -> bytes:
    """The bytes of `archives.sha256.json` of a root-only collect: the format and the `evidence` list, which names the
    archive and its manifest by size and SHA-256 (no path, no time: the same root, the same bytes)."""
    document = {
        "format": ROOT_ARCHIVES_FORMAT,
        "evidence": sorted(pair_entries(ROOT_ONLY_STEM, held), key=lambda entry: entry["name"]),
    }
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()


def read_root(module: Any, root: Path) -> Any:
    """`hpcevidence.archive_root` of the root as `ROOT_ONLY_STEM`, its refusals as `ShardsError` (a file named, never a
    value); a warning on stderr when the root holds step records, which this collect does not archive the shards of."""
    try:
        held = module.archive_root(root, ROOT_ONLY_STEM, root_id_for(root))
    except module.RootGone:
        raise ShardsError("the root is not there") from None
    except module.EvidenceError as error:
        raise ShardsError(str(error)) from None
    except OSError as error:
        raise ShardsError(f"the root's evidence could not be read ({type(error).__name__})") from None
    if any(name.startswith(f"{STEP_RECORDS}/") for name in held.names):
        print(
            "shards: WARNING: the root holds shard step records: this collects its root evidence only, never a shard's "
            "archive (`collect --manifest`, which needs the plan, does)",
            file=sys.stderr,
        )
    return held


def collect_root(root: Path, into: Path) -> tuple[str, str]:
    """ONE federation's root evidence with no plan (`collect --roots-only --root R --into DIR`): the root archive and
    its manifest (`hpcevidence.archive_root`: the same allow-list, the same refusal of a link and of a file holding a
    secret) and an `archives.sha256.json` of the root-only form for `verify-copy`, all mode 0600, in `into`; then, last,
    the collected-evidence marker in the root, which is what lets `down` go on. Returns the SHA-256 of that manifest and
    what was done: `collected`; `again`, the root was marked and unchanged but its copy was not in `into`; `already`,
    it was and the copy is in `into`, so nothing is written (a retry).

    `into` must be new, or empty and private, and a copy is never replaced: the evidence of a root that changed since
    is collected into another directory, the first copy left as it was. Everything is made in a private directory beside
    `into`, checked against its own manifest, and moved in with one rename. **The copy is never removed once it is in
    place.** A marker that cannot be written (it unlocks `down`) fails the collect, but the error can come after the
    marker landed (the directory sync that follows its rename), and a copy deleted then would leave a current marker and
    no evidence: so the copy stays, complete, and the same command run again says `already` (exit 0) if the marker
    landed; otherwise the evidence is collected into a new directory."""
    root = federation_root(root)
    into = copy_directory(root, into)
    module = _evidence()
    marked = module.collected_hash(root)
    if into.exists() and any(into.iterdir()):
        digest = holds_copy(into, marked)
        if digest is None:
            raise ShardsError("the directory to collect into must be new or empty (an earlier copy is never replaced)")
        return digest, "already"
    made: list[Path] = []
    stage: Path | None = None
    done = False
    try:
        made += _make_parents(into.parent)
        stage = Path(tempfile.mkdtemp(prefix=".collect-", dir=into.parent))  # 0700, ours, beside `into`
        held = read_root(module, root)
        payload = root_only_payload(held)
        digest = hashlib.sha256(payload).hexdigest()
        for name, data in ((f"{ROOT_ONLY_STEM}.root.tar", held.tar), (f"{ROOT_ONLY_STEM}.root.json", held.manifest),
                           (ARCHIVES_MANIFEST, payload)):  # fmt: skip
            _write_whole(stage / name, _private_bytes(data))
        if verify_copy(stage / ARCHIVES_MANIFEST, stage, digest).problems:
            raise ShardsError("the copy written does not match its own manifest (nothing is kept)")
        os.rename(stage, into)  # onto nothing, or onto the empty directory: one step, never a partial one
        _fsync_dir(into)  # its entries durable before the marker says they are there
        _fsync_dir(into.parent)
        try:
            module.write_marker(root, ROOT_ONLY_STEM, held.sha256, held.steps_sha256, held.hashes)
        except (OSError, module.EvidenceError) as error:
            raise ShardsError(
                f"the collected-evidence marker could not be written in the root ({type(error).__name__}), but the "
                "copy is complete and stays in the directory; the marker may or may not be in place: run the same "
                "command again (it says `already collected` and exits 0 if it is) or collect into a new directory"
            ) from None
        done = True
    except OSError as error:
        raise ShardsError(f"the root's copy could not be written ({type(error).__name__})") from None
    finally:
        if stage is not None and stage.exists():
            remove_tree(stage)
        if not done:  # the directories this collect made above `into`, once empty (rmdir refuses the others)
            for directory in reversed(made):
                with contextlib.suppress(OSError):
                    directory.rmdir()
    return digest, "again" if marked is not None else "collected"


def run_collect_root(args: argparse.Namespace) -> int:
    """`collect --roots-only --root R --into DIR`: `collect_root`, its manifest's hash alone on stdout (what
    `verify-copy` takes), and what was done on stderr. `ShardsError` for flags that do not go together (`main` reports
    it as 2)."""
    if args.manifest is not None or args.shard is not None or args.run:
        raise ShardsError(
            "--root takes no --manifest, --shard or --run (a plan's shards are collected with --manifest)"
        )
    if not args.roots_only:
        raise ShardsError("--root belongs to `collect --roots-only` (one federation's root, no plan)")
    if args.into is None:
        raise ShardsError("collect --roots-only --root needs --into (the new or empty directory that holds the copy)")
    digest, done = collect_root(args.root, args.into)
    said = {
        "collected": "the root's evidence is archived and marked collected",
        "again": "the root's evidence was collected before and has not changed, but its copy is not in this directory: "
        "it is collected again here",
        "already": "the root's evidence was already collected and has not changed: nothing is rewritten",
    }[done]
    print(f"shards: {said} (archives manifest {ARCHIVES_MANIFEST} sha256 {digest})", file=sys.stderr)
    if done != "already":
        print("shards: pull the directory and run verify-copy with that hash before down (README); evidence the root "
              "writes after this needs another collect, into a new directory", file=sys.stderr)  # fmt: skip
    print(digest)
    return 0


def merge_argv(plan: Plan, outs: Sequence[Path]) -> list[str]:
    """`python -I -B -m expcore merge`: isolated, no bytecode written: no module of the directory it is run from is
    imported."""
    return [plan.python, "-I", "-B", "-m", "expcore", "merge", str(plan.study / "out"), *map(str, outs)]


# ------------------------------------------------------------------------------------------------ the copy back
# Run on the operator's host, possibly without the core: nothing here may import it (a test runs it under `-I -S`).
@dataclass(frozen=True)
class CopyCheck:
    archives: int  # the archives the manifest names
    evidence: int  # the files beside them that it names (archive manifests, journals, a snapshot)
    problems: tuple[str, ...]  # "<name>: <what is wrong>" for each file missing or differing
    extras: tuple[str, ...]  # files of the directory that look like what the manifest names, and it does not


Entry = tuple[str, int, str]  # a file the manifest names: its name, size in bytes and SHA-256


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _plain_part(part: str) -> bool:
    return part not in ("", ".", "..") and not any(c in part for c in "\\\0")


def _entries(items: object, noun: str, nested: bool, seen: set[str]) -> list[Entry]:
    """The files of one list of the manifest as (name, bytes, sha256), shown well formed; `ShardsError` otherwise. Every
    name must be a plain file name, or with `nested` a relative path of plain parts (no empty part, `.`, `..`, absolute
    form, backslash or NUL), and is refused before any file is read; one is never echoed back. `seen` holds the names of
    the lists before, so that no name is given twice."""
    if not isinstance(items, list) or not items:
        raise ShardsError(f"the manifest names no {noun}, so it would verify nothing of them")
    entries: list[Entry] = []
    for n, item in enumerate(items, 1):
        entry = item if isinstance(item, Mapping) else {}
        name, size, digest = entry.get("name"), entry.get("bytes"), entry.get("sha256")
        parts = name.split("/") if isinstance(name, str) else []
        if not parts or not all(_plain_part(part) for part in parts) or (len(parts) > 1 and not nested):
            raise ShardsError(f"the manifest's {noun} {n} has no plain {'relative path' if nested else 'file name'}")
        if name in seen:
            raise ShardsError(f"the manifest names {name} twice")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ShardsError(f"the manifest gives {name} no size in bytes")
        if not _is_sha256(digest):
            raise ShardsError(f"the manifest gives {name} no SHA-256")
        seen.add(name)
        entries.append((name, size, digest))
    return entries


def _root_only_entries(document: Mapping[str, Any]) -> list[Entry]:
    """The evidence of a root-only manifest (`ROOT_ARCHIVES_FORMAT`): exactly a `.root.tar` and the `.root.json` of the
    same stem, each well formed, and no other key, so that nothing the manifest says goes unchecked."""
    if set(document) - {"format", "evidence"}:
        raise ShardsError("the root-only manifest has a key that nothing checks, so what it says would go unverified")
    evidence = _entries(document.get("evidence"), "evidence file", False, set())
    by_suffix = {suffix: [name for name, _, _ in evidence if name.endswith(suffix)] for suffix in ROOT_SUFFIXES}
    tar, info = (by_suffix[suffix] for suffix in ROOT_SUFFIXES)
    stems = {name.removesuffix(suffix) for suffix, names in by_suffix.items() for name in names}
    if len(evidence) != 2 or len(tar) != 1 or len(info) != 1 or len(stems) != 1:
        raise ShardsError("the root-only manifest must name exactly a .root.tar and the .root.json of the same stem")
    return evidence


def _manifest_entries(document: object) -> tuple[list[Entry], list[Entry]]:
    """The manifest's archives and its evidence (the files beside them), each shown well formed. A manifest with no
    evidence is one of an earlier tip, which checked the archives alone: it is refused, not passed on them. A root-only
    manifest (`collect --roots-only --root`) has no archives, and says so by its format: it is exactly the pair of
    files of one root's evidence."""
    if isinstance(document, Mapping) and document.get("format") == ROOT_ARCHIVES_FORMAT:
        return [], _root_only_entries(document)
    if not isinstance(document, Mapping) or document.get("format") != ARCHIVES_FORMAT:
        raise ShardsError(f"the manifest is not {ARCHIVES_FORMAT}")
    seen: set[str] = set()
    archives = _entries(document.get("archives"), "archive", False, seen)
    return archives, _entries(document.get("evidence"), "evidence file", True, seen)


def _copy_problem(local: Path, name: str, size: int, digest: str) -> str | None:
    """What is wrong with one pulled file, or None. Only a regular file is one, under directories that are directories:
    a link or a directory of the name is never followed, nor a link on its way, and a pipe is never read."""
    here = local
    *directories, last = name.split("/")
    try:
        for part in directories:
            here = here / part
            if not stat.S_ISDIR(here.lstat().st_mode):
                return "not under a plain directory"
        info = (here / last).lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_size != size:
        return "size differs"
    try:
        return None if _sha256(here / last) == digest else "SHA-256 differs"
    except OSError:
        return "unreadable"


def _is_archive_name(name: str) -> bool:
    """Whether `name` is a shard's archive as the manifest lists it: `<label>.out.tar`, a name of the directory."""
    return name.endswith(".out.tar") and "/" not in name and len(name) > len(".out.tar")


def _extras(local: Path, named: set[str]) -> tuple[str, ...]:
    """What `local` holds that looks like what the manifest names and it does not: an archive or an archive manifest
    (`*.out.tar`, `*.out.json`) in the directory, and a file in the `journals/` of any shard whose archive the manifest
    names (a step that ran after `collect`, whether or not the shard had a journal then). Only a warning: another
    campaign's files, or a later step's, are not this pull's to fail."""
    found = [
        f.name
        for f in local.iterdir()
        if f.name.endswith((".out.tar", ".out.json", *ROOT_SUFFIXES)) and f.name not in named
    ]
    journal_dirs = {"/".join(parts[:2]) for parts in (name.split("/") for name in named) if parts[1:2] == [JOURNALS]}
    # a shard collected with no journal has none named, and a step that ran after the collect has left one: so the
    # journals directory of every shard with an archive in the manifest is searched, not only those named by a file
    journal_dirs |= {f"{name.removesuffix('.out.tar')}/{JOURNALS}" for name in named if _is_archive_name(name)}
    for directory in sorted(journal_dirs):  # `<label>/journals`, once, whatever lies under it
        base = local / directory
        if base.is_symlink() or not base.is_dir():
            continue
        for root, _, files in os.walk(base):
            found += [relative for f in files if (relative := Path(root, f).relative_to(local).as_posix()) not in named]
    return tuple(sorted(found))


def verify_copy(manifest: Path, local: Path, manifest_sha256: str) -> CopyCheck:
    """Whether `local` holds every file `manifest` names (the archives, and what lies beside them: archive manifests,
    journals, a snapshot), each with its size and SHA-256, and whether `manifest` is the file whose hash `collect`
    printed (`manifest_sha256`); `ShardsError` when it is not, or cannot be used, before any file is read. Every file is
    checked, so one run names them all."""
    expected = manifest_sha256.lower()
    if not _is_sha256(expected):
        raise ShardsError("the hash given for the manifest is not a SHA-256 (64 hex digits)")
    try:
        raw = manifest.read_bytes()
    except OSError as error:
        raise ShardsError(f"the manifest could not be read ({type(error).__name__})") from None
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ShardsError("the manifest's SHA-256 is not the one given: this is not the manifest collect printed")
    try:
        document = json.loads(raw)
    except ValueError:
        raise ShardsError("the manifest is not JSON") from None
    archives, evidence = _manifest_entries(document)
    if not local.is_dir():
        raise ShardsError("the local directory does not exist")
    problems = [
        f"{name}: {why}"
        for name, size, digest in (*archives, *evidence)
        if (why := _copy_problem(local, name, size, digest))
    ]
    try:
        extras = _extras(local, {name for name, _, _ in (*archives, *evidence)})
    except OSError as error:
        raise ShardsError(f"the local directory could not be listed ({type(error).__name__})") from None
    return CopyCheck(len(archives), len(evidence), tuple(problems), extras)


def run_verify_copy(args: argparse.Namespace) -> int:
    """0 when every file matches (warnings for extras), 1 naming each that does not; a manifest that cannot be used
    is `ShardsError`, which `main` reports as 2."""
    check = verify_copy(args.manifest, args.local_dir, args.manifest_sha256)
    for name in check.extras:
        print(f"shards: verify-copy: WARNING: {name} is not named by the manifest", file=sys.stderr)
    if check.problems:
        print(f"shards: verify-copy: FAILED: {len(check.problems)} of {check.archives + check.evidence} files",
              file=sys.stderr)  # fmt: skip
        for problem in check.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    evidence = f"{check.evidence} evidence {'file' if check.evidence == 1 else 'files'}"
    if check.archives == 0:  # a root-only manifest: the root evidence archive and its manifest, nothing else
        print(f"shards: verify-copy: ok: the root's evidence archive and manifest ({evidence}), each with the size "
              "and SHA-256 the manifest names")  # fmt: skip
        return 0
    archives = f"{check.archives} {'archive' if check.archives == 1 else 'archives'}"
    print(f"shards: verify-copy: ok: {archives} and {evidence}, each with the size and SHA-256 the manifest names")
    return 0


# ------------------------------------------------------------------------------------------------ the step
def remove_tree(path: Path) -> None:
    """Remove a tree the run left read-only: directories made writable first (never through a link), then removed."""
    for base, dirs, _ in os.walk(path, topdown=True, followlinks=False):
        for name in (base, *(os.path.join(base, d) for d in dirs)):
            info = os.lstat(name)
            if stat.S_ISDIR(info.st_mode):
                os.chmod(name, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
    shutil.rmtree(path)


def imports_core_from(core_src: Path, find: Callable[[], str] | None = None) -> bool:
    """Whether this interpreter imports `expcore` from `core_src` (lane H's trap: a step that silently ran an
    installed core)."""
    if find is None:
        import expcore

        def find() -> str:
            return expcore.__file__

    try:
        return Path(find()).resolve().is_relative_to(core_src.resolve())
    except (OSError, ValueError):
        return False


def this_core() -> dict[str, Any]:
    """The core this interpreter imports, as `CORE_PROBE` reports it: whether its commit is known, whether an installed
    core is stale (its source differs from its build's), and why (`expcore.provenance.core_identity`)."""
    from expcore.provenance import core_identity

    found = core_identity.current()
    return {"known": found.known, "stale": found.stale, "why": found.why}


def core_refusal(report: Mapping[str, Any]) -> str | None:
    """Why `expcore run` would refuse to start on the core a report describes, else None: its commit is unknown (a copy
    without `.git` and without a build identity, which is what `git archive` leaves) or its install is stale
    (`provenance.core_check.require_core`). Dirt is not asked here: the run's own message names it."""
    if not report["known"]:
        state = "unknown"
    elif report["stale"]:
        state = "stale"
    else:
        return None
    return f"the core is {state} ({report.get('why')}), and `expcore run` refuses to start on it: {CORE_REMEDY}"


def identify_core(plan: Plan) -> dict[str, Any]:
    """What the manifest's interpreter says of the core it imports: `CORE_PROBE` run as the step runs, `-I -B`, under
    the step's PATH and the no-home environment (a checkout's identity is read with git, so a login node's own PATH and
    git configuration must not decide it). A refusal, naming why, when it cannot say."""
    env = {"PATH": plan.step_path, "LC_ALL": "C", **home_env(NO_HOME)}
    cannot = "the manifest's python could not report the core's identity"
    try:
        done = subprocess.run([plan.python, "-I", "-B", "-c", CORE_PROBE], capture_output=True, text=True,
                              errors="replace", env=env, timeout=CORE_PROBE_S, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError) as error:
        raise ShardsError(f"{cannot} ({type(error).__name__})") from None
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no message"])[-1][:200]
        raise ShardsError(f"{cannot} (exit {done.returncode}: {last})")
    try:
        report = json.loads(done.stdout.strip().splitlines()[-1])
        ok = isinstance(report["known"], bool) and isinstance(report["stale"], bool)
    except (IndexError, KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise ShardsError(f"{cannot} (it printed something else)")
    return report


# The dispatcher's first words, in its own process. It asks the kernel to kill it when its step dies
# (PR_SET_PDEATHSIG) and, when that cannot be asked or the step is already gone, it never starts (exit 71): a step
# killed alone never leaves it running on. It then runs `expcore` itself (as `python -m expcore` would) and, when that
# ends by any path but a kill, swaps the step's record to `archiving` before the process exits, in its working
# directory, the step's: so a dispatcher that ended is never under a record saying `running`, which a cleanup would
# take for a step killed mid-run. Any other command is exec'd as it is.
DISPATCHER = f"""import ctypes, os, runpy, signal, sys
try:
    tied = ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) == 0
except (AttributeError, OSError):
    tied = False
if not tied or os.getppid() != int(sys.argv[1]):
    sys.stderr.write("shards: ERROR: the dispatcher could not be tied to its step, so it does not start\\n")
    os._exit(71)
argv = sys.argv[2:]
if argv[1:5] != ["-I", "-B", "-m", "expcore"]:
    os.execv(argv[0], argv)
sys.argv = ["expcore", *argv[5:]]
try:  # its SystemExit, its message and its exit status pass as `python -m expcore`'s own
    runpy.run_module("expcore", run_name="__main__", alter_sys=True)
finally:
    try:
        os.replace("{OWNER_ENDING}", "{OWNER}")
    except OSError:
        pass
"""
NOT_TIED = 71  # the dispatcher could not be tied to its step: it never started


def run_dispatcher(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> int:
    """Run the dispatcher, relaying SIGTERM and SIGINT to it, and return its exit status (128 + N for a signal N). It
    dies with this step, and records its own end in the step's record (`DISPATCHER`); `cwd` is the step's directory."""
    words = [sys.executable, "-I", "-B", "-c", DISPATCHER, str(os.getpid()), *argv]
    child = subprocess.Popen(words, env=dict(env), cwd=cwd, stdin=subprocess.DEVNULL)

    def relay(signum: int, _frame: Any) -> None:
        child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, relay)
    code = child.wait()
    return 128 - code if code < 0 else code


class Snapshots:
    """When a running shard's `out/` is snapshotted (O62; `snapshot` is what a snapshot is).

    `take(out)` is called from a thread of its own while the dispatcher runs: every `every_s` seconds, and, given
    `progress` (a count that changes when a run ends, None while there is no reading, which counts as 0), within
    `poll_s` of a change, one snapshot at a time; and once after the dispatcher has exited, before the archive. One
    that raises is reported and never ends the run: the last good snapshot stands."""

    def __init__(
        self,
        take: Callable[[Path], None],
        every_s: float = SNAPSHOT_EVERY_S,
        *,
        progress: Callable[[], Any] | None = None,
        poll_s: float = POLL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.take, self.every_s, self.progress, self.clock = take, every_s, progress, clock
        self.poll_s = min(poll_s, every_s) if progress is not None else every_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _once(self, out: Path) -> None:
        try:
            self.take(out)
        except Exception as error:  # a snapshot that failed leaves the last good one: reported, the run goes on
            print(f"shards: a snapshot of out/ failed ({type(error).__name__}: {error})", file=sys.stderr)

    def _seen(self) -> Any:
        try:
            return None if self.progress is None else self.progress()
        except Exception:  # a reading that fails says nothing ended: the clock still takes the next one
            return None

    def start(self, out: Path) -> None:
        def loop() -> None:
            last, seen = self.clock(), self._seen()
            while not self._stop.wait(self.poll_s):
                now = self._seen()
                # no reading yet (None: out/ has no runs/ until its first attempt begins) is a count of 0, so that
                # attempt's beginning is no run's end: the first snapshot comes at the first run's end, or the clock's
                ended = now is not None and now != (0 if seen is None else seen)
                if (ended or self.clock() - last >= self.every_s) and out.is_dir():
                    self._once(out)
                    last, seen = self.clock(), now if now is not None else seen

        self._thread = threading.Thread(target=loop, daemon=True, name="shard-snapshots")
        self._thread.start()

    def stop(self, out: Path) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if out.is_dir():
            self._once(out)


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def taker(snapdir: Path, scratch: Path, facts: Mapping[str, Any], live: threading.Event) -> Callable[[Path], None]:
    """The default snapshot of `out/` into `snapdir` (`expcore.campaign.out_snapshot.take`): its store read in place
    while `live` is set (the dispatcher holds it), from a private copy once it is not."""

    def take(out: Path) -> None:
        from expcore.campaign import out_snapshot

        made = out_snapshot.take(out, snapdir, scratch=scratch, facts=facts, live=live.is_set(), now=_stamp())
        print(f"shards: snapshot {made.name} of shard {facts['shard']['label']} taken", file=sys.stderr)

    return take


def settle_store(out: Path) -> None:
    """Close the store as its dispatcher's own last close does, when a snapshot's read in place, made as the
    dispatcher exited, left its -shm (and -wal) beside it: one connection, opened, read and closed, checkpoints the
    -wal into the store and removes both, so the archive holds what the dispatcher left. Nothing for a store without
    a -shm, which a dispatcher that closed its store leaves none of."""
    import sqlite3

    store = out / "state.sqlite"
    if not store.is_file() or not store.with_name(store.name + "-shm").exists():
        return
    connection = sqlite3.connect(store)
    try:
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    finally:
        connection.close()


def settle_quietly(out: Path) -> None:
    """`settle_store`, where a failure is said and never ends the step: the archive that follows holds `out/` as it is
    (its store with its -wal and -shm, which a reader recovers from a private copy), and nothing here may lose it."""
    try:
        settle_store(out)
    except Exception as error:
        print(f"shards: ERROR: the store could not be settled ({type(error).__name__}); out/ is archived as it is",
              file=sys.stderr)  # fmt: skip


def archive_named(results: Path, label: str) -> str | None:
    """The SHA-256 the shard's archive manifest names: None when there is none; a value no snapshot names when it
    cannot be read, so that the archive, which then fails its check, is never passed over for an older snapshot."""
    path = results / f"{label}.out.json"
    if not path.exists():
        return None
    try:
        named = json.loads(path.read_text(encoding="utf-8")).get("sha256")
    except (OSError, ValueError, AttributeError):
        named = None
    return named if isinstance(named, str) else "unreadable"


def restore_newest(results: Path, label: str, spec: Mapping[str, Any], base: str | None, out: Path) -> Path | None:
    """Restore into `out` the newest snapshot of the shard that verifies (`out_snapshot.restore`: every file against
    its hash, the store's integrity, every ended attempt's marker) and return it; None when there is none, or when the
    archive is newer (a snapshot that does not name `base`, the archive's SHA-256, was taken before it). A snapshot of
    another shard is `ShardsError`. `resume_into` restores what this picks, and `resume-line` checks the same pick."""
    from expcore.campaign import out_snapshot

    for snapshot in out_snapshot.snapshots(results / label / SNAPSHOTS):
        try:
            facts = out_snapshot.manifest_of(snapshot)["facts"]
        except out_snapshot.OutSnapshotError as error:
            print(f"shards: snapshot {snapshot.name} is not used: {error}", file=sys.stderr)
            continue
        if facts.get("shard") != dict(spec):
            raise ShardsError(f"shard {label}: snapshot {snapshot.name} is of shard {facts.get('shard')}")
        if base is not None and facts.get("base") != base:
            return None  # taken before the archive: the archive is newer than it and every older one
        try:
            out_snapshot.restore(snapshot, out)
        except out_snapshot.OutSnapshotError as error:
            print(f"shards: snapshot {snapshot.name} is not used: {error}", file=sys.stderr)
            continue
        return snapshot
    return None


def resume_into(args: argparse.Namespace, spec: Mapping[str, Any], base: str | None, work: Path, out: Path) -> None:
    """Put the shard's latest `out/` at `out`: the newest snapshot that verifies, unless the archive is newer (a
    snapshot that does not name it was taken before it), else the archive, else nothing (a fresh start, only when the
    shard has neither). Either way the store's attempt directories are pointed at `out/runs`. A snapshot of another
    shard, and a shard whose every snapshot fails its check and that has no archive, are `ShardsError`: its results
    would otherwise be dropped for a fresh start."""
    from expcore.campaign import out_snapshot
    from expcore.campaign.snapshot import rebase_run_dirs

    found = out_snapshot.snapshots(args.results / args.label / SNAPSHOTS)
    snapshot = restore_newest(args.results, args.label, spec, base, out)
    if snapshot is not None:
        print(f"shards: shard {args.label} resumes from snapshot {snapshot.name}", file=sys.stderr)
        return
    if found and not (args.results / f"{args.label}.out.tar").exists():
        raise ShardsError(
            f"shard {args.label}: no snapshot of its {len(found)} verifies and it has no archive; a fresh start would "
            "drop its results, so the resume is refused"
        )
    if (args.results / f"{args.label}.out.tar").exists():
        shard = Shard(args.index, args.count, args.label, Path("/"))
        tar_path, _ = verified(args.results, shard, work)
        shutil.move(unpack(tar_path, work / "previous", label=args.label), out)
        tar_path.unlink()
        if (out / out_snapshot.STORE).is_file():
            rebase_run_dirs(out / out_snapshot.STORE, out / out_snapshot.RUNS)
        print(f"shards: shard {args.label} resumes from its archive", file=sys.stderr)


# ------------------------------------------------------------------------------------------------ the agent's node
def sandbox_work_root(root: str | None) -> Path | None:
    """The Apptainer sandbox's `work_root` that the federation's `platform.toml` names (relative to that file, as the
    platform layer reads it), or None: no federation named, no platform file, or no Apptainer sandbox in it."""
    if not root:
        return None
    platform = Path(root) / PLATFORM
    try:
        data = tomllib.loads(platform.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ShardsError(f"the federation's platform.toml could not be read ({type(error).__name__})") from None
    sandbox = data.get("sandbox")
    if not isinstance(sandbox, Mapping) or sandbox.get("backend") != "apptainer":
        return None
    value = sandbox.get("work_root")
    if not isinstance(value, str) or not value:
        raise ShardsError("the federation's platform.toml names no [sandbox] work_root")
    return Path(os.path.normpath(platform.parent / value))


def make_work_root(path: Path, tmpfs: Path) -> None:
    """Make the sandbox's work root on this node, mode 0700, before the dispatcher (the sandbox refuses a start
    without it); an existing one must be a directory of this user's, not a link, with no access for others. It must lie
    under `--tmpfs`, the node's tmpfs: never on the shared filesystem, where the runs' private files would be seen
    from every node. `ShardsError` names what is wrong, never the path."""
    node_tmp = Path(os.path.realpath(tmpfs))
    where = Path(os.path.realpath(path.parent)) / path.name
    if where == node_tmp or not where.is_relative_to(node_tmp):
        raise ShardsError("[sandbox] work_root is not on the node's tmpfs (under --tmpfs): it is never shared")
    try:
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)
    except FileExistsError:
        pass
    except FileNotFoundError:
        raise ShardsError("[sandbox] work_root's parent is not a directory on this node") from None
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ShardsError("[sandbox] work_root is a link or not a directory")
    if info.st_uid != os.getuid():
        raise ShardsError("[sandbox] work_root is not this user's")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ShardsError("[sandbox] work_root gives others access: it must be 0700")


def _private_work_root(path: Path) -> None:
    """The work root is a directory of this user's, not a link, with no access for others: anything else is never
    reaped or removed (a link would take the reaper out of it). `ShardsError` names what is wrong, never the path."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ShardsError("[sandbox] work_root is a link or not a directory")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ShardsError("[sandbox] work_root is not this user's alone (0700)")


def reap_work_root(work_root: Path, node: Any = None) -> dict[str, int]:
    """The Apptainer run reaper (`expcore_onedata.sandbox.apptainer.runs.reap`) over the sandbox's work root on this
    node (W2, live slot 5): a run whose driver is shown gone, as a step killed outright leaves it, has its token files
    removed, its helpers and every process carrying its tag ended, each mount in its directory unmounted (`fusermount3
    -u`, then `-uz` for a dead FUSE mount, "Transport endpoint is not connected"), shown gone by the mount table, then
    its directory and its record removed. A run whose driver lives, or cannot be told gone, is never touched. The
    reaper's counts; `ShardsError` when a dead run's leftovers could not all be removed, or its records could not be
    read: never "nothing to reap". It needs the core (`node`: the node it acts on, `LocalNode` by default)."""
    if not os.path.lexists(work_root):
        return {"runs": 0, "processes": 0, "left": 0}
    _private_work_root(work_root)
    from expcore_onedata.sandbox.apptainer import runs

    if node is None:
        from expcore_onedata.sandbox.apptainer.node import LocalNode

        node = LocalNode()
    found = runs.reap(node, work_root, own="")
    if found.get("error") or found.get("left"):
        raise ShardsError(
            f"the sandbox's work root on this node holds a killed run's leftovers that could not all be removed "
            f"({found.get('error') or str(found.get('left')) + ' run(s) left'})"
        )
    return found


def remove_empty_work_root(work_root: Path) -> bool:
    """The work root removed once nothing is left in it but an empty run-record directory (W1: the step makes it and
    nothing else removes it, so every node a shard step ran on kept one): `rmdir` alone, never a removal of anything
    in it. False when anything is left (a run of a live driver, say) or it is not there."""
    from_runs = work_root / "_runs"  # expcore_onedata.sandbox.apptainer.runs.RUNS
    try:
        _private_work_root(work_root)
        if os.path.lexists(from_runs):
            os.rmdir(from_runs)
        os.rmdir(work_root)
    except (OSError, ShardsError):
        return False
    return True


def _start_of(pid: int) -> str | None:
    """A process's start time, field 22 of /proc/<pid>/stat (as `apptainer.node.LocalNode` reads it); None without."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    fields = text.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def alive(pid: Any, start: Any) -> bool | None:
    """Whether the process that started at `start` still holds `pid`, as M5's reaper tells (`LocalNode.alive`): None
    when that cannot be told, which is never gone."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1 or not isinstance(start, str):
        return None
    now = _start_of(pid)
    if now is not None:
        return now == start
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return None


def naming(path: Path) -> bool | None:
    """Whether a live process of this node names `path` (or a path in it) in its argv, as the step's dispatcher names
    its copy of the study (`run <dir>/study`) and any process it starts does: None when /proc cannot be listed, which is
    never none. A wrapper killed alone leaves its dispatcher running in the directory, so the wrapper's pid is not
    enough."""
    text = str(path).encode()
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit() and int(name) != os.getpid()]
    except OSError:
        return None
    for pid in pids:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # gone since, or another user's that hidepid hides: none of this user's steps
        if any(word == text or word.startswith(text + b"/") for word in argv):
            return True
    return False


@dataclass(frozen=True)
class Left:
    """A step's directory on the node's tmpfs: its owner record (`step.json`, None when it cannot be read) and its
    archive-failed mark (informational: the record's phase is what decides)."""

    path: Path
    owner: Mapping[str, Any] | None
    failed: Mapping[str, Any] | None

    @property
    def phase(self) -> str | None:
        return None if self.owner is None else self.owner.get("phase")

    @property
    def removable(self) -> bool:
        """Only a step killed while its dispatcher ran (`running`: its out/ was never whole, and the snapshots and the
        journals hold what it reached), whose wrapper and every process naming its directory are shown gone. A step
        whose dispatcher ended (`archiving`) holds the only whole out/ until it is archived, and one whose record
        cannot be read could be either: never removable."""
        return self.phase == RUNNING and self.gone

    @property
    def gone(self) -> bool:
        """Whether its step is shown gone: its wrapper no longer holds its pid and no live process of this node names
        its directory (never when that cannot be told)."""
        return (
            self.owner is not None
            and alive(self.owner.get("pid"), self.owner.get("start")) is False
            and naming(self.path) is False
        )

    def ours(self, spec: Mapping[str, Any], root: str | None, results: Path) -> bool:
        """Whether it is a step of this shard of this campaign: its whole spec, its federation's root and its results
        directory as this step's. The node's tmpfs is the node's: another campaign may use the same labels."""
        return self.owner is not None and (
            self.owner.get("shard"),
            self.owner.get("root"),
            self.owner.get("results"),
        ) == (dict(spec), root, str(results))


def _record(path: Path) -> Mapping[str, Any] | None:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, Mapping) else None


def leftovers(tmpfs: Path, label: str | None = None) -> list[Left]:
    """The steps' directories under `tmpfs` (named for one shard's label, or every one), never a link."""
    prefix = f"{WORK_PREFIX}{label}." if label is not None else WORK_PREFIX
    found = []
    for path in sorted(tmpfs.glob(f"{prefix}*")):
        if not path.is_symlink() and path.is_dir():
            found.append(Left(path, _record(path / OWNER), _record(path / ARCHIVE_FAILED_MARK)))
    return found


def scrub_kept_home(left: Left) -> bool:
    """Remove the operator's key copies (`home/`) from a kept step directory: one whose dispatcher ended (`archiving`,
    its out/ waiting for `--archive-left`), whose record says the step staged `home_files`, and whose step is shown
    gone (`Left.gone`, the proof a removal of the whole directory takes). A step killed after the dispatcher ended, and
    before it removed the home itself, leaves them on the node's tmpfs, which the site does not clear at the job's end;
    nothing after the dispatcher needs them. A link in the home's place is unlinked, never walked; the out/ and the
    record are not touched. True when something was removed; never a name or a path in what it prints."""
    if left.owner is None or left.phase == RUNNING or left.owner.get("home_files") is not True or not left.gone:
        return False
    if not os.path.lexists(left.path / "home"):
        return False
    _clean_step(left.path, True, True)  # keep=True: the home alone, never the directory
    print("shards: the key copies of a kept step directory are removed (its out/ is not touched)", file=sys.stderr)
    return True


def clear_leftovers(tmpfs: Path, spec: Mapping[str, Any], root: str | None, results: Path) -> None:
    """Before a resume: remove each earlier step's directory of this shard of this campaign whose step was killed while
    its dispatcher ran and is shown gone; refuse while one holds an out/ its dispatcher ended with and that was not
    archived (`--archive-left` archives it), or while one still runs or cannot be shown gone. A directory of another
    campaign or federation, or whose record cannot be read, is kept, and said."""
    label = spec["label"]
    for left in leftovers(tmpfs, label):
        if not left.ours(spec, root, results):
            print(f"shards: {left.path.name} is not a step of this shard of this campaign, or names none: kept",
                  file=sys.stderr)  # fmt: skip
            continue
        if left.phase != RUNNING:
            scrub_kept_home(left)
            raise ShardsError(
                f"shard {label}: an earlier step's out/ was not archived and is kept on this node in "
                f"{left.path.name}; archive it with `shards.py step --archive-left` before resuming"
            )
        if not left.removable:
            raise ShardsError(f"shard {label}: a step of it still runs on this node, or cannot be shown gone")
        remove_tree(left.path)
        print(f"shards: shard {label}: an earlier step's directory on this node is removed", file=sys.stderr)


def archive_left(args: argparse.Namespace) -> int:
    """Archive the out/ an earlier step of this shard of this campaign ended with and did not archive (`archiving`,
    with an `ARCHIVE_FAILED` mark or not), as that step would have: `<results>/<label>.out.tar` and its manifest, with
    that step's exit status; then remove its directory. Refused unless the published archive is still the one that
    step started from: a later one would be replaced by an older out/."""
    spec = {"index": args.index, "count": args.count, "label": args.label}
    root = os.environ.get("EXPCORE_FED_ROOT")
    found = [left for left in leftovers(args.tmpfs, args.label) if left.ours(spec, root, args.results)
             and left.phase == ARCHIVING]  # fmt: skip
    if not found:
        raise ShardsError(
            f"shard {args.label}: no out/ of this shard of this campaign waits on this node to be archived"
        )
    left = found[0]
    if alive(left.owner.get("pid"), left.owner.get("start")) is not False or naming(left.path) is not False:
        raise ShardsError(f"shard {args.label}: the step of {left.path.name} still runs (it archives it itself)")
    scrub_kept_home(left)  # its step is gone: no key stays on the node should the archive below fail
    if archive_named(args.results, args.label) != left.owner.get("base"):
        raise ShardsError(
            f"shard {args.label}: an archive was published after the step of {left.path.name} started; its out/ is "
            "not archived over it (remove the directory by hand once its out/ is shown superseded)"
        )
    out = left.path / "study" / "out"
    if not out.is_dir():
        raise ShardsError(f"shard {args.label}: {left.path.name} holds no out/")
    settle_store(out)
    status = left.owner.get("exit_status", (left.failed or {}).get("exit_status", 1))
    guard, post = left.owner.get("pre_step"), left.owner.get("post_run")  # what that step ran; this one runs none
    post = dict(post) if isinstance(post, Mapping) else None
    if post is not None and post.get("final") is not True:  # its script did not pass, or the step ended before it did
        post["reason"] = post.get("reason") or "the step ended before its post-run script passed"
    passed = left.owner.get("pass_env")  # the variables that step was given, which its manifest records
    manifest = archive(out, args.results, spec, int(status), guard if isinstance(guard, Mapping) else None, post,
                       passed if isinstance(passed, Mapping) else None)  # fmt: skip
    remove_tree(left.path)
    print(f"shards: shard {args.label}'s kept out/ archived: {manifest['files']} files, sha256 {manifest['sha256']}")
    return 0


def sweep(tmpfs: Path, root: str, node: Any = None, live_fails: bool = False) -> int:
    """On a node a shard step of this federation ran on, at the federation's down: remove every step's directory of
    this federation (its owner record names `root`) whose step was killed while its dispatcher ran and is shown gone;
    name each one kept (an out/ not archived, a step still running). Then the sandbox's work root that the federation's
    `platform.toml` names: its killed runs reaped (`reap_work_root`, W2), and the root itself removed once empty (W1),
    unless a step directory of ours on this node is kept (its step may still use it). Exit 1 when the work root could
    not be reaped (its leftovers, or a Python that cannot import the core's reaper: down's `HPC_CORE_PYTHON`), so down
    never takes a work root it could not reap for a clean one. With `live_fails` (down's sweep of a node in a job it
    does not cancel, once it cancelled this federation's shard steps there), a step of this federation that still runs
    there, or cannot be shown gone, is exit 3: down never takes it for clean."""
    removed, kept, live = 0, [], 0
    for left in leftovers(tmpfs):
        if left.owner is None or left.owner.get("root") != root:
            continue  # not this federation's, or not to be told
        if left.phase != RUNNING:
            scrub_kept_home(left)
            kept.append(f"{left.path.name} (its out/ was not archived: `shards.py step --archive-left`)")
        elif not left.removable:
            kept.append(f"{left.path.name} (its step still runs, or cannot be shown gone)")
            live += 1
        else:
            remove_tree(left.path)
            removed += 1
    if live_fails and live:
        print(f"shards: sweep: ERROR: {live} step(s) of this federation still run here, or cannot be shown gone; "
              f"kept: {'; '.join(kept)}")  # fmt: skip
        return 3
    try:
        work_root = sandbox_work_root(root)
        reaped = reap_work_root(work_root, node) if work_root is not None else {"runs": 0}
    except ImportError:
        print("shards: sweep: ERROR: this Python cannot import the core's reaper, so the sandbox's work root here is "
              "not reaped (down runs the sweep with HPC_CORE_PYTHON)")  # fmt: skip
        return 1
    except (ShardsError, OSError) as error:
        print(f"shards: sweep: ERROR: the sandbox's work root here could not be reaped ({error})")
        return 1
    busy = bool(leftovers(tmpfs))  # a step directory still here: its step may yet use the work root
    root_gone = work_root is not None and not busy and remove_empty_work_root(work_root)
    if work_root is None:
        state = "none declared"
    elif root_gone:
        state = "removed"
    elif not os.path.lexists(work_root):
        state = "none here"
    else:
        state = "kept (a live run, or a step directory on this node, may use it)"
    print(f"shards: sweep: {removed} step directory(ies) removed; kept: {'; '.join(kept) or 'none'}; "
          f"killed sandbox run(s) reaped: {reaped['runs']}; the sandbox's work root: {state}")  # fmt: skip
    return 0


def _ended(work: Path, status: int) -> bool:
    """Record that the step's dispatcher ended (`archiving`), with its exit status: from here its out/ is whole and the
    only one until archived, so no cleanup removes it. The dispatcher swapped in the record written at the start as
    it ended (`DISPATCHER`); a runner of another kind did not, and it is swapped in here. The record with the status is
    written whole into a file of its own, read back, and swapped in by rename, which needs no space; should that fail
    (a full tmpfs), the record without the status stands. False when no record says the dispatcher ended, said."""
    if (work / OWNER_ENDING).exists():
        try:
            os.replace(work / OWNER_ENDING, work / OWNER)
        except OSError as error:
            print(f"shards: ERROR: the step's end could not be recorded ({type(error).__name__})", file=sys.stderr)
            return False
    current = _record(work / OWNER)
    if current is None or current.get("phase") != ARCHIVING:
        print("shards: ERROR: the step's record does not say its dispatcher ended", file=sys.stderr)
        return False
    record = {**current, "exit_status": status}
    try:
        (work / OWNER_STATUS).write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        if _record(work / OWNER_STATUS) == record:
            os.replace(work / OWNER_STATUS, work / OWNER)
    except OSError:
        pass  # the record without the status stands, whole
    return True


def _mark_path(args: argparse.Namespace) -> Path:
    return args.results / args.label / RUNNING_MARK


def taking_over(found: Mapping[str, Any] | None, args: argparse.Namespace, spec: Mapping[str, Any]) -> bool:
    """Whether a `--resume` may take over the running mark an earlier step of the shard left: only when its step is
    shown gone, by `resume-line` on the login node (it found no step of the shard listed anywhere and passed this
    mark's nonce as `--over`), or here, the mark naming this very node and a process of it that has ended. Anything
    else (another node with no `--over`, a mark that cannot be read, of another shard) may be a step that still runs:
    never taken over."""
    if found is None or found.get("format") != RUNNING_FORMAT or found.get("shard") != dict(spec):
        return False
    if not isinstance(found.get("nonce"), str) or not re.fullmatch(r"[0-9a-f]{32}", found["nonce"]):
        return False  # it names its claim's file: never anything but a nonce of ours
    over = getattr(args, "over", None)
    if over:
        return found.get("nonce") == over
    return found.get("node") == os.uname().nodename and alive(found.get("pid"), found.get("start")) is False


def claim_running(args: argparse.Namespace, spec: Mapping[str, Any]) -> dict[str, Any]:
    """The shard's running mark, `<results>/<label>/running.json`, taken for this step before anything else (O62):
    which node, process, job and step run the shard, and a nonce. A step's directory on one node's tmpfs cannot be
    seen from another (`clear_leftovers`), so this mark on the shared results directory is what stops two steps of
    one shard on two nodes. It is removed when the step ends by itself (`release_running`): a step killed outright
    leaves it. The mark is never absent while a step holds it, so it is made whole in a draft of this step's and put
    in place by an exclusive link (a start: refused when one is there) or, for a `--resume` that may take one over
    (`taking_over`), by a rename over it once this step made the one claim of that mark, `.running.json.<its
    nonce>.taken`, exclusively: of any number of steps taking one mark over, one alone makes the claim, and the claim
    is never removed, so a later step can never claim that mark again. `ShardsError` on any refusal; nothing of the
    shard is touched then."""
    path = _mark_path(args)
    _private_dirs(path.parent)
    mark = {"format": RUNNING_FORMAT, "shard": dict(spec), "nonce": secrets.token_hex(16),
            "node": os.uname().nodename, "pid": os.getpid(), "start": _start_of(os.getpid()),
            "job": getattr(args, "job", None), "step": getattr(args, "step_name", None),
            "root": os.environ.get("EXPCORE_FED_ROOT"), "started": _stamp()}  # fmt: skip
    draft = path.with_name(f".{RUNNING_MARK}.{mark['nonce']}.draft")
    descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(mark, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if not os.path.lexists(path):
            try:
                os.link(draft, path)  # exclusive, and whole: a reader never sees a part of it
            except FileExistsError:
                raise ShardsError(f"shard {args.label}: another step of it started meanwhile") from None
            return mark
        found = _record(path)
        if not args.resume:
            raise ShardsError(f"shard {args.label}: a step of it runs, or was killed outright (its running mark is "
                              "there): --resume continues it")  # fmt: skip
        if not taking_over(found, args, spec):
            raise ShardsError(f"shard {args.label}: a step of it may still run on another node (its running mark "
                              "names it); `shards.py resume-line` shows it gone and continues it")  # fmt: skip
        claim = path.with_name(f".{RUNNING_MARK}.{found['nonce']}.taken")
        try:
            os.close(os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        except FileExistsError:
            raise ShardsError(
                f"shard {args.label}: another step took its running mark over first (if no step of the shard runs "
                f"anywhere, a takeover was killed between its claim and its mark: remove {claim.name} by hand)"
            ) from None
        if _record(path) != found:  # only the claim's maker replaces that mark: never another step's, kept
            raise ShardsError(f"shard {args.label}: its running mark changed while it was taken over")
        os.replace(draft, path)
        return mark
    finally:
        draft.unlink(missing_ok=True)


def release_running(args: argparse.Namespace, mark: Mapping[str, Any]) -> None:
    """Remove the shard's running mark at the step's own end, only while it is still this step's (its nonce)."""
    path = _mark_path(args)
    found = _record(path)
    if found is not None and found.get("nonce") == mark["nonce"]:
        path.unlink(missing_ok=True)


class StepRefused(Exception):
    """A script the study declared refused the step, or could not be run as declared; the message names it."""


@dataclass(frozen=True)
class Hook:
    """A script of the study that this step's line declared (`--pre-step`, `--post-run`), its bytes read and hashed at
    the step's start: what runs is these bytes, never the file read again."""

    key: str  # `pre_step` or `post_run`
    path: str
    sha256: str
    timeout: float
    source: bytes

    @property
    def name(self) -> str:
        return f"{HOOKS[self.key][0]} {self.path}"


def pre_step_env() -> dict[str, str]:
    """The guard's environment: the step's allowlist as this step was given it, and the home its line gives (below a
    path nobody can make: its own directory does not exist yet), nothing else."""
    return {k: os.environ[k] for k in ALLOWLIST if k in os.environ} | home_env(NO_HOME)


def run_hook(
    source: bytes,
    args: Sequence[str],
    cwd: str | Path,
    env: Mapping[str, str],
    timeout: float,
    *,
    pass_fds: Sequence[int] = (),
) -> int | None:
    """Run a script's `source` as this interpreter's `-I -B -` (its script on its stdin) with `args`, in `cwd`, in a
    session of its own, and return its exit status (negative for a signal). None when it did not end within `timeout`
    seconds. Past the bound, or when this step is told to stop (SIGTERM, SIGINT) while it runs, the script's whole
    process group is killed, whether its leader is alive still or not: the leader is reaped only afterwards, so its
    pid names its group until then. Its stdout and stderr are this step's."""
    child = subprocess.Popen([sys.executable, "-I", "-B", "-", *args], stdin=subprocess.PIPE, env=dict(env), cwd=cwd,
                             start_new_session=True, pass_fds=tuple(pass_fds))  # fmt: skip

    def kill_group(*_: Any) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)

    previous: dict[int, Any] = {}
    with contextlib.suppress(ValueError):  # only the main thread sets handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, kill_group)
    try:
        try:
            child.communicate(source, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group()
            child.wait()
            return None
        except BaseException:
            kill_group()
            child.wait()
            raise
        return child.returncode
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def read_hook(args: argparse.Namespace, key: str, root_fd: int) -> Hook | None:
    """The script this step's line declares under `key`, its bytes read below the study's root `root_fd` and shown to
    be the file the plan hashed; None when the line declares none. `StepRefused` naming it for a declaration that is
    incomplete or not a plain path of the study, a file that cannot be read, or a hash that is not the plan's."""
    label, default, maximum = HOOKS[key]
    flag = key.replace("_", "-")
    path, wanted, bound = (getattr(args, f"{key}{suffix}", None) for suffix in ("", "_sha256", "_timeout"))
    if path is None:
        if wanted is not None or bound is not None:
            raise StepRefused(f"--{flag}-sha256 and --{flag}-timeout belong to a --{flag}, which is not given")
        return None
    name = f"{label} {path}"
    if problem := guard_path_problem(path):
        raise StepRefused(f"{name} {problem}")
    if not isinstance(wanted, str) or not re.fullmatch(r"[0-9a-f]{64}", wanted):
        raise StepRefused(f"{name} has no SHA-256 from the plan: --{flag}-sha256 is 64 hex digits")
    bound = default if bound is None else bound
    if problem := bound_problem(bound, maximum):
        raise StepRefused(f"{name}: --{flag}-timeout {problem}")
    try:
        source = read_below(root_fd, path, name)
    except ShardsError as error:
        raise StepRefused(str(error)) from None
    digest = hashlib.sha256(source).hexdigest()
    if digest != wanted:
        raise StepRefused(f"{name} is not the file the plan hashed: its sha256 is {digest}, the plan's {wanted}")
    return Hook(key, path, wanted, float(bound), source)


def run_guard(guard: Hook, label: str, root_fd: int) -> dict[str, Any]:
    """Run the pre-step guard: in the study's root as `root_fd` holds it (a directory of this user's that no group or
    other can write to), as its working directory and the base of the `campaign.toml` it is given, so a root renamed
    since is never the one it runs in; the record `{path, sha256, exit_status: 0}` when it exits 0, else `StepRefused`
    naming the guard and why (a root that is not ours, no campaign.toml of its own, a guard that cannot start, does not
    end in its bound or does not exit 0)."""
    info = os.fstat(root_fd)
    if info.st_uid != os.getuid():
        problem = "the study's root is not this user's"
    elif stat.S_IMODE(info.st_mode) & 0o022:
        problem = "the study's root can be written by its group or others"
    else:
        problem = None
    if problem:
        raise StepRefused(f"{guard.name} cannot run: {problem}; its working directory must be this user's alone")
    try:
        found = stat.S_ISREG(os.lstat(CAMPAIGN, dir_fd=root_fd).st_mode)
    except OSError:
        found = False
    if not found:
        raise StepRefused(f"{guard.name} cannot run: the study's {CAMPAIGN} is missing, a link or not a file")
    held = f"/proc/self/fd/{root_fd}"
    try:
        status = run_hook(guard.source, [label, f"{held}/{CAMPAIGN}"], held, pre_step_env(), guard.timeout,
                          pass_fds=(root_fd,))  # fmt: skip
    except OSError as error:
        raise StepRefused(f"{guard.name} could not be started ({type(error).__name__})") from None
    if status is None:
        raise StepRefused(f"{guard.name} did not end within {guard.timeout:g} s: it was killed")
    if status < 0:
        raise StepRefused(f"{guard.name} refused the step: it was killed by signal {-status}")
    if status != 0:
        raise StepRefused(f"{guard.name} refused the step: exit status {status}")
    return {"path": guard.path, "sha256": guard.sha256, "exit_status": status}


def start_hooks(args: argparse.Namespace) -> tuple[dict[str, Any] | None, Hook | None]:
    """The step's first act (the module's docstring): the record `{path, sha256, exit_status: 0}` of the pre-step guard
    that passed (None when the line declares none) and the post-run script, read and shown to be the plan's, for the
    step to run after its dispatcher (None when none). Both are read before the guard runs, so that a campaign is never
    started that cannot be checked at its end. `StepRefused` for any that is not as declared; nothing is made."""
    keys = [
        key for key in HOOKS if any(getattr(args, f"{key}{s}", None) is not None for s in ("", "_sha256", "_timeout"))
    ]
    if not keys:
        return None, None
    try:
        root_fd = os.open(os.path.abspath(args.study), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        names = ", ".join(f"{HOOKS[key][0]} {getattr(args, key)}" for key in keys)
        raise StepRefused(f"the study's root cannot be opened, so {names} cannot be read") from None
    try:
        guard, post = read_hook(args, "pre_step", root_fd), read_hook(args, "post_run", root_fd)
        return (None if guard is None else run_guard(guard, args.label, root_fd)), post
    finally:
        os.close(root_fd)


def run_post_run(hook: Hook, label: str, copy: Path, out: Path, env: Mapping[str, str], status: int) -> dict[str, Any]:
    """The post-run script's turn, once the dispatcher has exited with `status`: its record `{path, sha256, ran,
    exit_status, final}`, `final` only when the script ran and exited 0, with a `reason` when it is not. The script
    gets the shard's label, `out` and `status`, runs in `copy` (the study's copy, on the node's tmpfs), and gets the
    dispatcher's own environment. It is not run for a dispatcher that did not exit 0."""
    record: dict[str, Any] = {"path": hook.path, "sha256": hook.sha256, "ran": False, "exit_status": None,
                              "final": False}  # fmt: skip
    if status != 0:
        return {**record, "reason": f"the dispatcher exited {status}: the post-run script was not run"}
    try:
        code = run_hook(hook.source, [label, str(out), str(status)], copy, env, hook.timeout)
    except OSError as error:
        return {**record, "reason": f"the post-run script could not be started ({type(error).__name__})"}
    record["ran"] = True
    if code is None:
        return {**record, "reason": f"the post-run script did not end within {hook.timeout:g} s: it was killed"}
    record["exit_status"] = code
    if code < 0:
        return {**record, "reason": f"the post-run script was killed by signal {-code}"}
    if code != 0:
        return {**record, "reason": f"the post-run script exited with status {code}"}
    return {**record, "final": True}


def note_post_run(work: Path, post: Mapping[str, Any]) -> None:
    """Put the post-run script's record in the step's record (`step.json`), written whole into a file of its own, read
    back and swapped in by rename, as `_ended` does; should that fail (a full tmpfs) the record written at the start
    stands, which says the script did not pass."""
    current = _record(work / OWNER)
    if current is None:
        return
    record = {**current, "post_run": dict(post)}
    try:
        (work / OWNER_POST).write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        if _record(work / OWNER_POST) == record:
            os.replace(work / OWNER_POST, work / OWNER)
    except OSError as error:
        print(f"shards: the post-run record could not be written to the step's record ({type(error).__name__})",
              file=sys.stderr)  # fmt: skip


# ------------------------------------------------------------------------------------------------ the render identity
def expected_identity_name(path: str) -> str:
    return f"the expected render identity {path}"


def identity_problem(record: Any) -> str | None:
    """Why `record` is not a render identity (hpcidentity.py), or None: a JSON object with the version, the template's
    and the platform fingerprint's hashes, perhaps some of the pins' hashes and the study's artifacts digest, and
    nothing else. It says what kind of thing is wrong and never what the record holds."""
    if not isinstance(record, dict):
        return "it is not a JSON object"
    if not {"version", "template_sha256", "platform_fingerprint_sha256"} <= set(record):
        return "an entry it must hold is missing"
    if not set(record) <= set(RENDER_IDENTITY_KEYS):
        return "it holds an entry that is not declared"
    for key, value in record.items():
        if key == "version":
            if not isinstance(value, str) or not re.fullmatch(r"expcore-render-identity/[0-9]{1,6}", value):
                return "its version is not one"
        elif not isinstance(value, str) or not SHA256_TEXT.fullmatch(value):
            return "an entry is no SHA-256"
    return None


def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`json.loads`'s `object_pairs_hook`: a key given twice is refused, since the last would be all a check saw."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is given twice")
    return dict(pairs)


def canonical_render_identity(record: Mapping[str, str]) -> bytes:
    """A render identity's canonical text, as hpcidentity.py writes it: sorted keys, no spaces, one newline."""
    return (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def parse_render_identity(raw: bytes, name: str) -> dict[str, str]:
    """The render identity `raw` holds, `ShardsError` naming `name` if it is not one: JSON with no key given twice, an
    object of exactly the declared keys' kinds (`identity_problem`). The error says nothing of the content."""
    try:
        record = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    except (ValueError, RecursionError):
        raise ShardsError(f"{name} is not a well-formed render identity: it is not JSON") from None
    if problem := identity_problem(record):
        raise ShardsError(f"{name} is not a well-formed render identity: {problem}")
    return record


def read_federation_identity(root: str) -> bytes | None:
    """The bytes of the render identity `up` wrote in the federation's root `root`: None when the root holds none, and
    `ShardsError` when it cannot be taken: a root that cannot be opened, is not this user's or can be written by its
    group or others, or a record that is a link, not a regular file, over the size a record can be, or unreadable.
    It is read below the held root, no link followed, once."""
    name = f"the federation's render identity record ({RENDER_IDENTITY})"
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise ShardsError(f"{name} cannot be read: the federation's root cannot be opened") from None
    try:
        info = os.fstat(root_fd)
        if info.st_uid != os.getuid():
            raise ShardsError(f"{name} cannot be trusted: the federation's root is not this user's")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise ShardsError(f"{name} cannot be trusted: the federation's root can be written by its group or others")
        try:
            os.lstat(RENDER_IDENTITY, dir_fd=root_fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise ShardsError(f"{name} cannot be read") from None
        return read_below(root_fd, RENDER_IDENTITY, name)
    finally:
        os.close(root_fd)


def check_render_identity(args: argparse.Namespace) -> bytes | None:
    """The step's first act, before its pre-step guard and everything else: the canonical text of the federation's own
    render identity (`EXPCORE_FED_ROOT`'s record, as validated: the bytes `up` wrote, whatever else the file held is
    never copied), for the step to put into its out/ (`note_render_identity`); None when the federation has none or its
    record cannot be taken, which is said on stderr and nothing more, as a federation `up` rendered before the record
    existed is run as it was.

    When the line gives the expected record (`--expect-render-identity REL --expect-render-identity-sha256 HEX`): the
    file is read below the study's root, shown to be the bytes the plan hashed and a well-formed record; the
    federation's record must exist, be well formed and be the expected one, key by key. `StepRefused` otherwise,
    naming the keys that differ (never a value, a path or a hash) or what could not be read."""
    path, wanted = (getattr(args, f"expect_render_identity{s}", None) for s in ("", "_sha256"))
    root = os.environ.get("EXPCORE_FED_ROOT")
    if path is None:
        if wanted is not None:
            raise StepRefused(
                "--expect-render-identity-sha256 belongs to an --expect-render-identity, which is not given"
            )
        if not root:
            return None
        try:
            raw = read_federation_identity(root)
            if raw is None:
                return None
            return canonical_render_identity(
                parse_render_identity(raw, f"the federation's render identity record ({RENDER_IDENTITY})")
            )
        except ShardsError as error:
            print(f"shards: the federation's render identity is not archived: {error}", file=sys.stderr)
            return None
    name = expected_identity_name(path)
    if problem := guard_path_problem(path):
        raise StepRefused(f"{name} {problem}")
    if not isinstance(wanted, str) or not SHA256_TEXT.fullmatch(wanted):
        raise StepRefused(f"{name} has no SHA-256 from the plan: --expect-render-identity-sha256 is 64 hex digits")
    try:
        study_fd = os.open(os.path.abspath(args.study), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise StepRefused(f"the study's root cannot be opened, so {name} cannot be read") from None
    try:
        raw = read_below(study_fd, path, name)
    except ShardsError as error:
        raise StepRefused(str(error)) from None
    finally:
        os.close(study_fd)
    if hashlib.sha256(raw).hexdigest() != wanted:
        raise StepRefused(f"{name} is not the file the plan hashed")
    try:
        expected = parse_render_identity(raw, name)
        if not root:
            raise ShardsError(
                f"EXPCORE_FED_ROOT names no federation, so its render identity cannot be compared with {name}"
            )
        held = read_federation_identity(root)
        if held is None:
            raise ShardsError(
                f"the federation has no render identity record ({RENDER_IDENTITY}) in its root to compare with {name}: "
                "its up wrote none"
            )
        actual = parse_render_identity(held, f"the federation's render identity record ({RENDER_IDENTITY})")
    except ShardsError as error:
        raise StepRefused(str(error)) from None
    if differing := [key for key in RENDER_IDENTITY_KEYS if expected.get(key) != actual.get(key)]:
        raise StepRefused(
            f"the federation's render identity is not the expected one ({path}): it differs in {', '.join(differing)}"
        )
    return canonical_render_identity(actual)


def note_render_identity(out: Path, record: bytes, step_id: str) -> None:
    """Put the federation's render identity, the text `check_render_identity` gave, into the step's out/ as
    `study-state/render-identity.<step_id>.json` (mode 0600, made new, no link followed): a file of this step's own, so
    a resumed shard keeps the identity each of its steps ran under. `out` need not exist yet. `ShardsError` when it
    cannot be written: a step that cannot carry the proof of its render does not start."""
    try:
        folder = out / STUDY_STATE
        folder.mkdir(parents=True, exist_ok=True)
        if folder.is_symlink() or not folder.is_dir():
            raise OSError(errno.ENOTDIR, "not a directory")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(folder / f"render-identity.{step_id}.json", flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(record)
    except OSError as error:
        raise ShardsError(
            f"the federation's render identity could not be written into out/{STUDY_STATE}/ ({type(error).__name__}): "
            "a step that cannot carry it does not start"
        ) from None


def check_pass_env(args: argparse.Namespace) -> dict[str, str]:
    """The variables this step's line declares (`--pass-env NAME`, once each) with the value its `env -i` words gave
    each, checked again as `lines` checked them: a name that is plain, looks like no secret and is none the step sets
    itself, set in this step's environment, with a value of plain characters. These, and no other variable of the
    node's, go to the dispatcher. `StepRefused` naming the variable, never its value; a line that declares none gives
    an empty record."""
    passed: dict[str, str] = {}
    for name in getattr(args, "pass_env", None) or []:
        if problem := pass_env_name_problem(name):
            raise StepRefused(problem)
        if name in passed:
            raise StepRefused(f"pass_env names {name} twice")
        if name not in os.environ:
            raise StepRefused(f"pass_env variable {name} is not set in the step's environment: its line gave no value")
        if problem := pass_env_value_problem(name, os.environ[name]):
            raise StepRefused(problem)
        passed[name] = os.environ[name]
    return passed


def check_home_files(args: argparse.Namespace) -> HomeTree | None:
    """The operator's key tree the line names (`--home-files`), read once with its bytes (`read_home_files`): None when
    the line names none. `StepRefused` naming `home_files` and why, never a path or a name of the tree."""
    path = getattr(args, "home_files", None)
    if path is None:
        return None
    try:
        if problem := home_files_path_problem(path) or home_files_overlap_problem(path, args.study):
            raise _home_refusal(problem)
        return read_home_files(path)
    except ShardsError as error:
        raise StepRefused(str(error)) from None


def hidden_hop(line: Sequence[str]) -> list[str]:
    """A step line as `plan` shows it: the `--fleet-hop` table (the login, the paths, the port) replaced by `<hidden>`;
    `lines` prints the whole line, which is what is run."""
    out = list(line)
    out[out.index("--fleet-hop") + 1] = "<hidden>"
    return out


def check_fleet_hop(args: argparse.Namespace) -> dict[str, Any] | None:
    """The hop to the LLM fleet the line names (`--fleet-hop`, JSON), as `fleethop.parse_hop` reads it: None when the
    line names none. `StepRefused` naming `fleet_hop` and never a value."""
    text = getattr(args, "fleet_hop", None)
    if text is None:
        return None
    try:
        return _sibling("fleethop").parse_hop(text)
    except _sibling("fleethop").HopRefused as error:
        raise StepRefused(str(error)) from None


def check_pass_env_stable(args: argparse.Namespace, passed: Mapping[str, str]) -> None:
    """The variables this step passes are the ones the shard's first step passed, each with the same value
    (`check_pass_env_origin`, on the shard's directory of `--results`): `StepRefused` naming each that is not, never a
    value, before anything is made. A label the core does not accept is left to `step`'s own refusal of it, which
    comes later and is what it always was (the label names a directory here, and is looked up only once it is one)."""
    spec = {"index": args.index, "count": args.count, "label": args.label}
    try:
        _label_ok(args.index, args.count, args.label)
    except (ShardsError, ImportError):  # a core that cannot be imported is `step`'s own refusal (CORE_MISMATCH), next
        return
    try:
        check_pass_env_origin(args.results, spec, passed)
    except ShardsError as error:
        raise StepRefused(str(error)) from None


def note_step_started(root: str | None, spec: Mapping[str, Any]) -> None:
    """Leave `<root>/shard-steps/<label>.json` in the federation's root: a step of this shard started against it, and
    how many have (`starts`, up by one each time, a resume included). `down` cannot see the results directory, so
    this is
    how it learns that a step ran, and a collect's marker names the digest of these records, so a step that starts after
    the collect makes it stale (`hpcevidence`). The record holds the shard's spec and the count: no path, host or
    secret.
    Written whole (a draft, fsynced, renamed), the directory 0700. Nothing is done without a federation root that is a
    directory (a study with no federation). `ShardsError` when it cannot be written, or an earlier record cannot be
    read: a step that cannot say it ran does not start, since `down` would then remove evidence it cannot know of."""
    if not root or not Path(root).is_dir():
        return
    path = Path(root) / STEP_RECORDS / f"{spec['label']}.json"
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise OSError(errno.ENOTDIR, "not a directory")
        starts = 1
        if os.path.lexists(path):
            earlier = json.loads(path.read_text(encoding="utf-8"))["starts"]
            if isinstance(earlier, bool) or not isinstance(earlier, int) or earlier < 0:
                raise ValueError("starts")
            starts = earlier + 1
        record = {"format": STEP_RECORD_FORMAT, "shard": dict(spec), "starts": starts}
        _write_whole(path, lambda draft: draft.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ShardsError(
            f"the step's record in its federation's root ({STEP_RECORDS}/{spec['label']}.json) could not be written or "
            f"read ({type(error).__name__}): a step that cannot say it ran does not start"
        ) from None


def step(
    args: argparse.Namespace,
    *,
    runner: Callable[[Sequence[str], Mapping[str, str], Path], int] = run_dispatcher,
    core_found: Callable[[Path], bool] = imports_core_from,
    snapshots: Snapshots | None = None,
    node: Any = None,
    hop_keeper: Callable[[Mapping[str, Any]], Any] | None = None,
) -> int:
    """One shard on its node (the module's docstring): copy, restore, run, archive, clean up; the dispatcher's exit
    status. `snapshots` says when `out/` is snapshotted; by default every `--snapshot-every` seconds and at each run's
    end, into `<results>/<label>/snapshots/`. `hop_keeper` makes the keeper of the line's `--fleet-hop`
    (`fleethop.HopKeeper` by default). `node` is what the reaper of the sandbox's work root acts on (the
    Apptainer backend's `NodeOps`; `LocalNode` by default). Its first acts are the check of the variables its line
    declares (`check_pass_env`), the check of the federation's render identity against the one the line expects
    (`check_render_identity`) and then the study's pre-step guard, when its line declares one (`start_hooks`): nothing
    is made, removed or read before them, and `PRE_STEP_REFUSED` ends a step they refuse. The variables the line
    declares go to the dispatcher, and so to a post-run script, which runs in its environment; the step's own
    environment holds them as it holds its allowlist, and the guard is given the allowlist alone.
    The federation's render identity is written into `out/study-state/` before the dispatcher starts,
    after a resume has restored its `out/`, in a file of this step's own (`note_render_identity`). A declared post-run
    script runs after the dispatcher exits 0 and before the archive (`run_post_run`): a step whose script does not
    pass archives its results marked not final and ends with `POST_RUN_FAILED`."""
    try:
        passed = check_pass_env(args)  # what the line declares, before anything of the study's runs
        check_pass_env_stable(args, passed)  # and what the shard's first step passed: one value for its whole life
        identity = check_render_identity(args)  # the federation's render is the plan's, before the study's own guard
        guard, post_hook = start_hooks(args)
        home = check_home_files(args)  # the operator's key tree, read once: held, and written after the home exists
        hop = check_fleet_hop(args)  # the hop to the LLM fleet: read here, started just before the dispatcher
    except StepRefused as error:
        print(f"shards: ERROR: {error}; the step is refused, with nothing made", file=sys.stderr)
        return PRE_STEP_REFUSED
    if passed:
        print(f"shards: passed {', '.join(passed)} to shard {args.label}'s dispatcher (names only)", file=sys.stderr)
    if guard is not None:
        print(f"shards: pre-step guard {guard['path']} (sha256 {guard['sha256']}) passed for shard {args.label}",
              file=sys.stderr)  # fmt: skip
    if home is not None:
        print(f"shards: {len(home.files)} file(s) of home_files go to shard {args.label}'s dispatcher (a count: no "
              "name or content is shown)", file=sys.stderr)  # fmt: skip
    if not core_found(args.core_src):
        print("shards: ERROR: this interpreter does not import the core from --core-src", file=sys.stderr)
        return CORE_MISMATCH
    refusal = core_refusal(this_core())  # the dispatcher is this interpreter's: it would stop at its start on this core
    if refusal:
        raise ShardsError(refusal)
    from expcore.campaign import out_snapshot

    if args.tmpfs.is_symlink() or not args.tmpfs.is_dir():
        print("shards: ERROR: --tmpfs must be a directory, not a link", file=sys.stderr)
        return 2
    if (args.study / "out").exists():
        print("shards: ERROR: the study copy holds an out/ of its own", file=sys.stderr)
        return 2
    if os.path.lexists(args.study / PLATFORM):  # the step places its federation's there
        raise ShardsError(f"the study copy holds a {PLATFORM} of its own: the step places its federation's beside it")
    spec = {"index": args.index, "count": args.count, "label": args.label}
    _label_ok(args.index, args.count, args.label)
    published = [args.results / f"{args.label}.out.{kind}" for kind in ("tar", "json")]
    snapdir = args.results / args.label / SNAPSHOTS
    if not args.resume and (any(path.exists() for path in published) or out_snapshot.snapshots(snapdir)):
        print(f"shards: ERROR: shard {args.label} has results or a snapshot already: --resume continues it",
              file=sys.stderr)  # fmt: skip
        return 2
    if args.resume:  # an earlier step killed outright left its directory here: removed, or the resume refused
        clear_leftovers(args.tmpfs, spec, os.environ.get("EXPCORE_FED_ROOT"), args.results)
    mark = claim_running(args, spec)  # no other step of the shard runs anywhere from here until this one ends
    try:
        try:  # the shard's first step records what it passed; a later one, or a racing first one, is compared again
            record_pass_env_origin(args.results, spec, passed)
        except ShardsError as error:
            print(f"shards: ERROR: {error}; the step is refused before its dispatcher starts", file=sys.stderr)
            return PRE_STEP_REFUSED
        return _run_step(args, spec, runner, snapshots, node, identity, guard, post_hook, passed, home, hop, hop_keeper)
    finally:
        release_running(args, mark)


def _run_step(
    args: argparse.Namespace,
    spec: Mapping[str, Any],
    runner: Callable[[Sequence[str], Mapping[str, str], Path], int],
    snapshots: Snapshots | None,
    node: Any,
    identity: bytes | None,
    guard: Mapping[str, Any] | None,
    post_hook: Hook | None,
    passed: Mapping[str, str],
    home: HomeTree | None = None,
    hop: Mapping[str, Any] | None = None,
    hop_keeper: Callable[[Mapping[str, Any]], Any] | None = None,
) -> int:
    """`step` once its checks passed and it holds the shard's running mark: its directory on the tmpfs first (so a
    sweep finds a step of ours here before its work root is used), the sandbox's work root made and its killed runs
    reaped, then copy, restore, run, archive, clean up. `identity`, `guard`, `post_hook` and `passed` are what the
    step's first acts found (the federation's render identity as validated, the pre-step guard that passed, the
    post-run script declared, the variables its line gave the dispatcher with their values); `home` is the operator's
    key tree it read (`check_home_files`), written into the dispatcher's home just before the dispatcher starts and
    removed once the dispatcher and the post-run script have ended. `hop` is the line's hop to the LLM fleet (O93):
    kept, after the key files and before the dispatcher, until the dispatcher exits, and killed on every way out."""
    from expcore.campaign import out_snapshot

    snapdir = args.results / args.label / SNAPSHOTS
    base = archive_named(args.results, args.label)  # the archive this step starts from: its snapshots name it
    work = Path(tempfile.mkdtemp(prefix=f"{WORK_PREFIX}{args.label}.", dir=args.tmpfs))
    keep = False
    disarm: Callable[[], None] | None = None
    keeper: Any = None
    try:
        work.chmod(0o700)
        owner = {"format": OWNER_FORMAT, "shard": spec, "pid": os.getpid(), "start": _start_of(os.getpid()),
                 "root": os.environ.get("EXPCORE_FED_ROOT"), "results": str(args.results), "base": base,
                 "phase": RUNNING, **({"pre_step": guard} if guard is not None else {}),
                 **({"pass_env": dict(passed)} if passed else {}),
                 **({"home_files": True, "home_files_count": len(home.files)} if home is not None else {}),
                 **({"fleet_hop": True} if hop is not None else {}),
                 **({"post_run": {"path": post_hook.path, "sha256": post_hook.sha256, "ran": False, "exit_status": None,
                                  "final": False}} if post_hook is not None else {})}  # fmt: skip
        (work / OWNER).write_text(json.dumps(owner, sort_keys=True) + "\n", encoding="utf-8")
        (work / OWNER_ENDING).write_text(json.dumps({**owner, "phase": ARCHIVING}, sort_keys=True) + "\n", "utf-8")
        work_root = sandbox_work_root(os.environ.get("EXPCORE_FED_ROOT"))
        if work_root is not None:  # the Apptainer sandbox's, which it refuses to start without
            make_work_root(work_root, args.tmpfs)
            reaped = reap_work_root(work_root, node)  # W2: what a step killed here left, before any use
            if reaped["runs"]:
                print(f"shards: shard {args.label}: {reaped['runs']} killed sandbox run(s) on this node reaped "
                      "(mounts, directories, records)", file=sys.stderr)  # fmt: skip
        copy = work / "study"
        shutil.copytree(args.study, copy, symlinks=True)
        platform = Path(os.environ.get("EXPCORE_FED_ROOT") or "/nonexistent") / PLATFORM
        if platform.is_file():  # a copy, never a link out of the study; its paths are the root's, absolute (`up`)
            shutil.copyfile(platform, copy / PLATFORM)
            (copy / PLATFORM).chmod(0o600)
        if args.resume:
            resume_into(args, spec, base, work, copy / "out")
            (copy / "out" / NOT_FINAL).unlink(
                missing_ok=True
            )  # an archive not final is resumed; this step decides afresh
        if identity is not None:  # the proof of the render is in out/ from the start: in every snapshot, and a kill's
            note_render_identity(copy / "out", identity, f"{_stamp()}-{secrets.token_hex(4)}")
        for name in ("home", "tmp", *(f"home/{sub}" for sub in HOME_DIRS)):
            (work / name).mkdir(mode=0o700)
        env = {k: os.environ[k] for k in ALLOWLIST if k in os.environ}
        env |= passed  # the line's declared variables; the step's own below can never be one of them
        env |= {**home_env(str(work / "home")), "TMPDIR": str(work / "tmp")}  # never the account's ~
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # B1: for a Python the dispatcher's children start without -I -B
        journals = args.results / args.label / JOURNALS
        _private_dirs(journals)
        journal = journals / f"{_stamp()}-{os.getpid()}-{secrets.token_hex(4)}.jsonl"
        argv = [sys.executable, "-I", "-B", "-m", "expcore", "run", str(copy), "--spend-journal", str(journal),
                "--shard", f"{args.index}/{args.count}", "--shard-label", args.label]  # fmt: skip
        out = copy / "out"
        live = threading.Event()
        live.set()
        if snapshots is None:
            facts = {"shard": spec, "base": base}
            snapshots = Snapshots(
                taker(snapdir, work / "snapshot-scratch", facts, live),
                getattr(args, "snapshot_every", SNAPSHOT_EVERY_S),
                progress=lambda: out_snapshot.ended_attempts(out),
            )
        if home is not None or hop is not None:  # a stop before the dispatcher runs still ends through the cleanup
            disarm = _unwind_on_stop()
        if home is not None:  # the operator's key files, last before the dispatcher; a stop in between cleans up
            try:
                stage_home_files(home, work / "home")
            except ShardsError as error:
                print(f"shards: ERROR: {error}; the step is refused before its dispatcher starts", file=sys.stderr)
                return PRE_STEP_REFUSED
        if (
            hop is not None
        ):  # the hop to the LLM fleet, after the key files: its port accepts before the dispatcher starts
            fleethop = _sibling("fleethop")
            keeper = (hop_keeper or fleethop.HopKeeper)(hop)
            try:
                keeper.start()
            except fleethop.HopRefused as error:
                print(f"shards: ERROR: {error}; the step is refused before its dispatcher starts", file=sys.stderr)
                return FLEET_HOP_FAILED
        note_step_started(os.environ.get("EXPCORE_FED_ROOT"), spec)  # `down` learns that a step ran from this
        snapshots.start(out)
        try:
            status = runner(argv, env, work)
            _ended(work, status)  # its out/ is whole: no cleanup removes it until it is archived
        finally:
            live.clear()  # the dispatcher has exited: the last snapshot reads a private copy of the store
            snapshots.stop(out)
            if keeper is not None:  # the dispatcher no longer needs the hop: killed, with the stop signals held
                with _stop_held():
                    keeper.stop()
                print(f"shards: shard {args.label}: {keeper.status()}", file=sys.stderr)
        settle_quietly(out)
        if not out.is_dir() or {entry.name for entry in out.iterdir()} <= {STUDY_STATE}:  # the record alone is none
            print(f"shards: shard {args.label} left no out/ to archive (exit status {status})", file=sys.stderr)
            return status
        post = None
        if post_hook is not None:  # the dispatcher has exited and the federation is up: the study's own check
            try:
                post = run_post_run(post_hook, args.label, copy, out, env, status)
            except Exception as error:  # whatever the check does, the results are archived: not final
                post = {"path": post_hook.path, "sha256": post_hook.sha256, "ran": False, "exit_status": None,
                        "final": False, "reason": f"the post-run step failed ({type(error).__name__})"}  # fmt: skip
            note_post_run(work, post)
            if post["final"]:
                print(f"shards: {post_hook.name} passed for shard {args.label}: its results are final", file=sys.stderr)
            else:
                print(f"shards: ERROR: {post['reason']}; shard {args.label}'s results are archived, NOT final",
                      file=sys.stderr)  # fmt: skip
            settle_quietly(out)  # a script that read the store may leave its -shm
        if home is not None:  # the dispatcher and the post-run script have ended: no key stays for the archive's sake
            remove_tree(work / "home")
        try:
            manifest = archive(out, args.results, spec, status, guard, post, passed)
        except (OSError, tarfile.TarError) as error:  # the only whole out/ is the tmpfs copy: kept, never removed
            keep = True
            mark = {"shard": spec, "exit_status": status, "error": type(error).__name__}
            (work / ARCHIVE_FAILED_MARK).write_text(json.dumps(mark, sort_keys=True) + "\n", encoding="utf-8")
            print(f"shards: ERROR: shard {args.label}'s out/ could not be archived ({type(error).__name__}); it is "
                  f"kept at {out} until `shards.py step --archive-left` archives it", file=sys.stderr)  # fmt: skip
            return ARCHIVE_FAILED
        print(f"shards: shard {args.label} archived: {manifest['files']} files, sha256 {manifest['sha256']}")
        if post is not None and not post["final"]:
            return status or POST_RUN_FAILED
        return status
    finally:
        try:
            held = home is not None or hop is not None
            try:
                with _stop_held() if held else contextlib.nullcontext():
                    if keeper is not None:
                        keeper.stop()  # on every way out (a no-op once the dispatcher has ended)
                    _clean_step(work, keep, held)
            except SystemExit:
                if not held:
                    raise
                # a stop that landed before the signals were held: its handler now ignores them all, so the cleanup
                # that it cut short is made whole here, and the stop goes on
                if keeper is not None:
                    keeper.stop()
                _clean_step(work, keep, True)
                raise
        finally:
            if disarm is not None:
                disarm()  # the handlers the step found, unless a runner has taken them over; a held stop lands here


def _clean_step(work: Path, keep: bool, staged: bool) -> None:
    """What a step removes on its way out, each part only if it is still there (a second pass is a no-op): the key
    copies, explicitly and by name (a link in their place is unlinked, never walked), then, unless the directory is kept
    for `--archive-left`, the step's directory."""
    home = work / "home"
    if staged and os.path.lexists(home):
        if home.is_symlink():
            home.unlink()
        else:
            remove_tree(home)
    if not keep and (not staged or os.path.lexists(work)):
        remove_tree(work)


# ------------------------------------------------------------------------------------------------ the command line
def _sibling(name: str) -> Any:
    """A module beside this file, by its path, without putting this directory on `sys.path` (`-I` leaves it off)."""
    import importlib.util

    loaded = sys.modules.get(f"hpc_{name}")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(f"hpc_{name}", Path(__file__).resolve().parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shards.py", description="One campaign shard per HPC federation.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "lines", "collect"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", type=Path, required=name != "collect")  # collect: or --root, below
        if name == "collect":
            command.add_argument("--into", type=Path, help="the new or empty directory to unpack into")
            command.add_argument("--run", action="store_true")
            command.add_argument("--roots-only", dest="roots_only", action="store_true",
                                 help="only each root's evidence (a plan: no --into): for a shard about to move, or "
                                 "with --root and --into, one federation's")  # fmt: skip
            command.add_argument("--shard", help="with --roots-only: the one shard's root")
            command.add_argument("--root", type=Path, help="with --roots-only and --into, no --manifest: ONE root")
    one = sub.add_parser("step")
    for key in ("study", "tmpfs", "results", "core_src"):
        one.add_argument(f"--{key.replace('_', '-')}", dest=key, type=Path, required=True)
    one.add_argument("--index", type=int, required=True)
    one.add_argument("--count", type=int, required=True)
    one.add_argument("--label", required=True)
    one.add_argument("--resume", action="store_true")
    one.add_argument("--archive-left", dest="archive_left", action="store_true")
    one.add_argument("--snapshot-every", dest="snapshot_every", type=float, default=SNAPSHOT_EVERY_S)
    one.add_argument("--job", help="the id squeue -s lists this step under (its running mark records it)")
    one.add_argument("--step-name", dest="step_name", help="this step's name (its running mark records it)")
    one.add_argument("--over", help="the nonce of the running mark resume-line showed the step of gone")
    one.add_argument("--pre-step", dest="pre_step", default=None)  # the study's guard: `lines` writes all three
    one.add_argument("--pre-step-sha256", dest="pre_step_sha256", default=None)
    one.add_argument("--pre-step-timeout", dest="pre_step_timeout", type=float, default=None)
    one.add_argument("--post-run", dest="post_run", default=None)  # the study's post-run script: all three
    one.add_argument("--post-run-sha256", dest="post_run_sha256", default=None)
    one.add_argument("--post-run-timeout", dest="post_run_timeout", type=float, default=None)
    one.add_argument("--expect-render-identity", dest="expect_render_identity", default=None)  # with its hash: both
    one.add_argument("--expect-render-identity-sha256", dest="expect_render_identity_sha256", default=None)
    one.add_argument("--home-files", dest="home_files", default=None)  # the operator's key tree: `lines` writes it
    one.add_argument(
        "--fleet-hop", dest="fleet_hop", default=None
    )  # the hop to the LLM fleet's front: `lines` writes it
    one.add_argument("--pass-env", dest="pass_env", action="append", default=None, metavar="NAME",
                     help="a variable the manifest declares: its value is among the line's env words")  # fmt: skip
    left = sub.add_parser("sweep")
    left.add_argument("--tmpfs", type=Path, required=True)
    left.add_argument("--root", required=True)
    left.add_argument("--live-fails", dest="live_fails", action="store_true")
    again = sub.add_parser("resume-line")
    again.add_argument("--manifest", type=Path, required=True)
    again.add_argument("--label", required=True)
    again.add_argument("--job", required=True)
    again.add_argument("--node", required=True)
    copy = sub.add_parser("verify-copy", help="check the results pulled to this host against collect's manifest")
    copy.add_argument("manifest", type=Path, help="the pulled archives.sha256.json")
    copy.add_argument("local_dir", type=Path, help="the directory the archives were pulled into")
    copy.add_argument("--manifest-sha256", dest="manifest_sha256", required=True, help="the hash collect printed")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify-copy":  # before the manifest of the plan: this runs where the core may not be
            return run_verify_copy(args)
        if args.command == "sweep":
            return sweep(args.tmpfs, args.root, live_fails=args.live_fails)
        if args.command == "resume-line":
            line = _sibling("shardresume").resume_line(sys.modules[__name__], args.manifest, args.label, args.job,
                                                       args.node, script=Path(__file__).resolve())  # fmt: skip
            print(shlex.join(line))
            return 0
        if args.command == "step":
            return archive_left(args) if args.archive_left else step(args)
        if args.command == "collect" and args.root is not None:  # one root, no plan to load
            return run_collect_root(args)
        if args.command == "collect" and args.manifest is None:
            raise ShardsError("collect takes --manifest (a plan), or --roots-only --root and --into (no plan)")
        plan = load_plan(args.manifest)
        if args.command == "collect" and args.roots_only:
            if args.into is not None or args.run:
                raise ShardsError("--roots-only takes neither --into nor --run")
            for done, digest in collect_roots(plan, args.shard):
                print(
                    f"shards: shard {done}: root evidence archived ({done}.root.tar sha256 {digest}) and marked",
                    file=sys.stderr,
                )
            print("shards: archives.sha256.json is not rewritten: a full collect lists these archives; pull the "
                  "results directory and check each <label>.root.json before down", file=sys.stderr)  # fmt: skip
            return 0
        if args.command == "collect":
            if args.into is None or args.shard is not None:
                raise ShardsError("collect takes --into (and --shard only with --roots-only)")
            outs, manifest_hash = collect_hashed(plan, args.into, refuse_newer=args.run)
            line = merge_argv(plan, outs)
            print(shlex.join(line))  # first, and alone on stdout: callers read it as the merge line
            print(f"shards: archives manifest {ARCHIVES_MANIFEST} sha256 {manifest_hash}", file=sys.stderr)
            print("shards: each root's evidence is archived (<label>.root.tar) and marked collected; down refuses a "
                  "root that holds evidence a run left (a step record, a journal line) and has no current mark, "
                  "unless --discard-evidence",
                  file=sys.stderr)  # fmt: skip
            print("shards: pull the results directory and run verify-copy with that hash before down (README)",
                  file=sys.stderr)  # fmt: skip
            return subprocess.run(line, check=False, cwd=args.into).returncode if args.run else 0
        missing = undeclared_data(plan)
        if missing:
            raise ShardsError(
                f"a campaign on {len(plan.shards)} federations is one only where each declares every space's data "
                f"(data_manifest in its platform.toml, C06); undeclared: {'; '.join(missing)}"
            )
        plan = pin_pass_env(pinned(plan))  # each declared script hashed, each declared variable read, before any line
        check_one_campaign(plan)  # every shard's campaign fingerprint shown to be one (O63), by a plugin given the pins
        script = Path(__file__).resolve()
        built = []
        for shard in plan.shards:
            line = step_argv(plan, shard, federation(shard), script)
            shown = (
                hidden_hop(line) if plan.fleet_hop is not None else line
            )  # a plan is read and kept: no host, path or port
            record = {"shard": shard.label, "argv": shown}
            if plan.pre_step is not None:
                record["pre_step"] = {"path": plan.pre_step, "sha256": plan.pre_step_sha256,
                                      "timeout_s": plan.pre_step_timeout_s}  # fmt: skip
            if plan.post_run is not None:
                record["post_run"] = {"path": plan.post_run, "sha256": plan.post_run_sha256,
                                      "timeout_s": plan.post_run_timeout_s}  # fmt: skip
            if plan.expect_render_identity is not None:
                record["expect_render_identity"] = {"path": plan.expect_render_identity,
                                                    "sha256": plan.expect_render_identity_sha256}  # fmt: skip
            if plan.pass_env_pins:
                record["pass_env"] = dict(plan.pass_env_pins)
            if plan.home_files is not None:
                record["home_files"] = True  # the line holds the path; the plan says only that the tree was read
            if plan.fleet_hop is not None:
                record["fleet_hop"] = True  # the line holds the table; the plan says only that there is a hop
            built.append((line, record))
        refusal = core_refusal(identify_core(plan))  # last: it runs the manifest's interpreter, as the step will
        if refusal:
            raise ShardsError(refusal)
        for line, record in built:
            print(json.dumps(record) if args.command == "plan" else shlex.join(line))
        return 0
    except ShardsError as error:
        print(f"shards: ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
