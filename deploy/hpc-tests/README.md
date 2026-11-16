# Test runs on an HPC allocation

`hpctests.py` runs a pytest selection of this repository, at a commit, as one Slurm step of a standing allocation of
ours, and brings the results back: pytest's exit status, its summary line, its `-ra` section and its JUnit XML. It is
for a development host that is too small or too busy for the suite. CI stays the definition of green; this runner is
where lanes run their selections in the meantime.

Every site detail (the ssh destination, the account, the partition, the paths) comes from a local `site.env`
(`site.env.example` lists the keys), which git ignores. Nothing of it is printed, and none of it but the job id, the
node's tmpfs root and the step's PATH is on a step's command line.

## How a run goes

1. **Here:** `git rev-parse` resolves the commit (only committed work ships; uncommitted changes are named and left
   out), and its history goes into a git bundle, made in a temporary repository that borrows the objects, so no ref is
   added to the repository. The bundle, `node.sh`, a settings file and the pytest arguments make one tar, the payload.
2. **The login node:** `alloc.sh list` names our allocations (below); the run picks the RUNNING one that ends last and
   that the step fits in before its end.
3. **The step:** one `ssh` runs `srun --jobid=<ours> --exact -N1 -n1 -c <cores> --mem=<size> --time=<minutes>
   --chdir=/ --export=PATH /usr/bin/env -i <allowlist> /bin/bash -c <boot>` with the payload on its stdin. The boot
   (`hpctestlib.BOOT`) makes a 0700 run directory under the node's tmpfs root, after checking that the root is a
   tmpfs, is ours and goes through no link, then checks the payload's SHA-256 and hands over to `node.sh`.
4. **`node.sh run`:** a clone at the commit; `uv sync --frozen` into a venv of the run, with uv's cache and a
   uv-managed Python on the node's tmpfs root (shared by the runs on that node); the **self-check**, an isolated
   interpreter (`python -I`) of that venv importing the core, which must come from the clone's `src/` (exit 97, and no
   test runs, otherwise); pytest with `-ra`, `--junitxml` and a bound. pytest's environment is built there: PATH,
   HOME and TMPDIR inside the run directory, the locale, thread counts set to the step's cores, a git identity.
5. **Back:** the results directory as one archive, base64 between a header with its SHA-256 and size and an end marker
   carrying pytest's exit status. `hpctests.py` checks both, unpacks it into `HPCT_OUT/<run>/`, prints the `-ra` lines
   and the summary, writes `run.json`, and exits with pytest's status. The run directory on the node is removed.

A step writes only under the node's tmpfs root: never under the home directory or a shared filesystem, and never
under Slurm's per-job `TMPDIR` (a link into a shared filesystem with an inode quota on some sites). It reads nothing
of the account's home either, which every agent of the account shares: with HOME unset, uv would fall back to the
passwd entry's home (seen live), so every phase runs with HOME, TMPDIR and the XDG directories inside the run
directory, uv's cache, Python, Python links and tools under the node root (`--no-bin`: no link in any `bin`), pip's
config and git's global config at `/dev/null` (git's system config off), Python's user site off, and Apptainer's and
Singularity's cache and config inside the run directory.

```
uv run python deploy/hpc-tests/hpctests.py run --repo <your worktree> -- tests/test_changelog.py tests/onedata -q
uv run python deploy/hpc-tests/hpctests.py run --rev origin/main --cores 8 --mem 12G --time 90 -- tests -m "not benchmark"
```

`run` options: `--repo` (default `.`), `--rev` (default `HEAD`), `--cores` (4), `--mem` (6G), `--time` (60 minutes:
the step's limit; pytest gets one minute less), `--job` (default: the running allocation of ours that ends last),
`--out` (default `HPCT_OUT`), `--keep` (leave the run directory on the node). `hpctests.py` needs nothing beyond the
standard library, so `python3 deploy/hpc-tests/hpctests.py` works too.

Exit statuses: pytest's own (0 to 5) when results came back; 64 a setting or an argument; 69 no allocation of ours
fits the step; 70 no results came back; 71 the results failed their SHA-256 or size; 72 the step refused to start
(the boot's reason is printed); 73 the clone failed; 74 the environment failed (`env.log` is printed); 97 the
self-check failed.

### Sharing the allocation

Several lanes run at once: each run is its own step, its own directory and its own venv. With `--exact` each step
gets cores and memory of its own, and a step that does not fit waits for one that ends (the run waits up to an hour
for that before giving up). `--overlap` is never used: on a site where it was, every step landed on the same cores.
Ask for what the selection needs: most of the suite is single-threaded, so 2 to 4 cores and 4 to 8 GB are plenty.

### What can differ from this host

- `/proc` on the nodes may hide other users' processes (`hidepid`): a test that needs pid 1 or another user's process
  fails there; it is a host difference, not a regression.
- The step has no network beyond what the node has, no Docker and no login shell's environment (no `SSH_AUTH_SOCK`,
  no `HPCT_*`, no site variable); `uv` is not on the tests' PATH.
- The clone has the commit's history and `origin/main` (when this repository has it), nothing else.

### When something goes wrong

- **A dropped ssh loses the run:** srun is killed with it, and the step's EXIT trap removes its directory. A step
  killed outright (by its time limit, say) can leave its directory: `hpctests.py clean --stale 60` runs a small step
  that removes every run directory of ours that no live `node.sh` holds and that is older than 60 minutes;
  `clean --all` also removes the uv cache and the Python when no run is live.
- **The srun client of a finished step can hang** while the step's results are already out (seen on a site). The run
  ends ssh 30 s after the end marker, and keeps the results.

## The allocation

One held job, `exec sleep infinity` with `--no-requeue` and a `--time`, on the standard partition: the steps run in it.
Billing is per allocated hour whether the steps run or not, so size it to the lanes' work and release it when idle.

```
uv run python deploy/hpc-tests/hpctests.py install       # alloc.sh and login.env into the login directory (0700)
uv run python deploy/hpc-tests/hpctests.py submit        # one allocation; what Slurm recorded is read back
uv run python deploy/hpc-tests/hpctests.py jobs          # ours, their ends in UTC, the renewer's state
uv run python deploy/hpc-tests/hpctests.py release <id>  # cancel one of ours
```

**Ours** is a job the login directory's **ledger** holds (every id `alloc.sh submit` was given) **and** that squeue
shows under our job name. The cluster account may be shared with other studies and other lanes: a job the ledger does
not hold is never listed, picked, renewed or cancelled, whatever its name, and nothing cancels by user. There are two
cancels, each of one id: `submit` cancels the job sbatch has just printed when Slurm recorded it otherwise than asked
(name, requeue, account, partition, cores, memory, time limit, state; an unreadable record counts as different), and
`release` cancels a job of the ledger that is alive under our name. The account and partition reach sbatch through
its own input variables (`SBATCH_ACCOUNT`, `SBATCH_PARTITION`), never its command line. `submit` refuses once
`HPCT_MAX_ALIVE` (2) allocations of ours are alive, under a lock held from that count to the ledger's entry, so two
submits cannot both pass it.

## Renewal

Renewal is a deliberate step, taken only while lanes need the allocation: billing runs idle or busy. The standard
partition queues a job behind a running one, so the next allocation is requested before the current one ends, with
`--begin` so that it cannot start (and bill) early, and only while the login directory's marker says lanes need it:

```
uv run python deploy/hpc-tests/hpctests.py renew want --until 2026-11-18T18:00Z   # lanes need it until then
uv run python deploy/hpc-tests/hpctests.py renew unwant                          # they don't: no successor
uv run python deploy/hpc-tests/hpctests.py renew start --until 2026-11-20T18:00Z  # the loop, detached on the login node
uv run python deploy/hpc-tests/hpctests.py renew stop                             # it stops at its next poll
uv run python deploy/hpc-tests/hpctests.py renew once --until ...                 # one decision, now
```

Without the marker (`renew.wanted`), or once its time has passed, the loop submits nothing and the running
allocation ends at its time limit; `release` ends it sooner.

**The idle release:** every poll, the loop also releases a RUNNING allocation of ours that has run no test step for
`--idle-hours` (3; from its start when it never ran one), by its id through `release`'s own ledger and name checks.
It reads the job's steps from `sacct`, so a step shorter than a poll still counts; a record it cannot read, or one
that does not list the job's batch step yet, counts as busy; and right before releasing it asks the controller for
the job's live steps (`squeue -s`), so a step a lane has just started keeps the allocation. It clears `renew.wanted` with it, so the next poll does not submit another, and releases a successor of
ours still queued.

`renew start` runs `alloc.sh renew` with `setsid nohup` on the login node, from a frozen copy of `alloc.sh` (bash reads a
script as it runs, so a later `install` never changes a running loop), so it outlives the ssh session and any agent
session. Every `--poll` (300 s) it reads the clock (`date +%s`) and squeue, never trusting a timer, and:

- does nothing unless `renew.wanted` names a time still ahead, or while a successor of ours is queued;
- when our running allocation ends within `--lead` (3 h), submits its successor to begin `--overlap` (15 min) before
  that end, or now if that is past;
- when no allocation of ours is alive (the loop was down, or the queue was slow), submits one to begin now, so a gap
  lasts one poll;
- submits nothing that would begin at or after `--until` or the marker's time, nothing beyond `--max` (4) submits, nothing past
  `HPCT_MAX_ALIVE`; it stops at `--until`, on `renew.stop`, after a submit that was not as asked (that job cancelled), or
  after three failed submits in a row.

It writes `renew.state` (heartbeat, until, submits) every poll and logs to `renew.log`; `jobs` shows its state and
warns when the allocation ends within 2 h with no successor queued and no renewer running. A second loop is refused
while the first one's heartbeat is fresh. A successor once queued is Slurm's, so the loop dying after a submit still
leaves the next allocation in place.

**By hand** (no loop), the same rule: `jobs`, then within 3 h of the end `submit --begin <end less 15 min>`; release
the old one early only when it is idle.

## Files

| File | Runs on | What |
|---|---|---|
| `hpctests.py` | this host | the CLI: run, clean, install, submit, jobs, release, renew |
| `hpctestlib.py` | this host | the settings, the step's argv and allowlist, the boot, the node settings |
| `hpctestparse.py` | this host | the stream a step sends back, pytest's summary and JUnit XML, the login node's listing |
| `node.sh` | the node, in a step | run and clean (it travels in every payload) |
| `alloc.sh` | the login node | submit, list, release, renew (put there by `install`) |

Tests: `tests/deploy/test_hpc_tests_lib.py` (the pure parts), `tests/deploy/test_hpc_tests_alloc.py` (`alloc.sh`
against a fake Slurm and clock) and `tests/deploy/test_hpc_tests_run.py` (a whole run through a fake ssh, srun and
uv, on a real tmpfs), with the fakes in `tests/support/hpct_fakes.py`.
