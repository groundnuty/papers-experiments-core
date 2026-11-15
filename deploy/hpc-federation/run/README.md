# The run: steps and evidence

These steps drive a standing allocation of five nodes through one run of the bundle's federation (a zone Z, three
providers P1 to P3) and the agent's cell on a fifth node S, which hosts no Onedata service. They were written from the
first standing run's driver (`../facts/2026-11-14-standing-1.md`), which printed `"token_in_log": False` as a literal: its
gate-log token scan never ran, and the summary passed it. Nothing of that driver is copied: every site value comes from
`../site.env` (see `../site.env.example` and the bundle's README, "Site configuration"), and nodes are named by role.

**These steps have not run as written.** The unit tests (`tests/deploy/test_hpc_run_evidence.py`, `test_hpc_run_steps.py`)
cover what they write and how the summary reads it, on invented data. The first live run is the next one.

## The evidence rule

1. **Every step writes its raw evidence to files** under the run's evidence directory `$E`, one subdirectory per step
   (0700), files 0600: HTTP status codes and the SHA-256 and size of each response body, connect results per host and
   port, every command's exit status, the counts of lines a log shows, and the scans' match counts. `record.py` writes
   them; no step writes a verdict.
2. **The summary is derived from those files by one function**, `evidence.py`'s `summary(dir)`. Each summary item is
   built by one constructor from its name, its evidence files and a parser, and by nothing else: there is no way to put
   a literal `True` into the summary. Each item names the files it was parsed from, with their SHA-256 and sizes.
3. **An item whose file is missing, empty or unparsable fails.** A parser that raises fails its item. The verdict is
   `PASS` only when every item of the catalog parsed and every item's own check passed; a step that was skipped is a
   failed item, never an absent one.
4. **The evidence is this run's.** The first step (`zone-up.sh`) needs an empty `$E` and writes `run.json` (a fresh run
   id, the allocation's job id, the start); every other step refuses an `$E` without it or of another allocation. A
   step's directory is made once per run, so a step cannot run again into the same run; the cell is the one step
   repeated (`cell.sh`), and its earlier attempt is moved aside to `sandbox.attempt-<n>`, which the summary does not
   read. The summary names the run id and the start, never the job id.

```bash
python3 -I run/evidence.py "$E"     # prints the summary as JSON; exit 0 on PASS, 1 on FAIL
```

## Steps

`$E` is the run's evidence directory, on a filesystem every node sees: the sandbox step writes into it from S. Every
`srun` of these steps uses `--jobid --overlap --export=PATH`, so no environment passes, and none of them takes a token
on a command line: tokens move only through pipes and 0600 files.

```bash
# 0. Once: site.env filled in; the bundle's two rootfs tarballs (bundle README, step 0), rebuilt from this bundle: the
#    Couchbase guard (hpcop-cb-init.py) is baked into the rootfs, and a tarball from before the guard printed its status
#    codes has no `hpcop-evidence` lines, so its services fail their `_couchbase_guard` items; and the provider image's archive
#    $SH/oneprovider-docker.tar with its .sha256; gate.py, gatepolicy.py and gateboundary.py of the core
#    (src/expcore_onedata/sandbox/) in $GATE_DIR; the allocation: five nodes, one job, named hpcop-<anything>, held by sleep,
#    so that the final check can tell a job of ours from another of the same user's.
sbatch --job-name=hpcop-standing --no-requeue --time=08:00:00 -N5 --ntasks-per-node=1 --cpus-per-task=8 --mem=20G \
  --nodelist=<the five nodes> --chdir=/ --output=/dev/null --wrap 'sleep 28740'
# put the job id it prints, and the five nodes, into site.env (the steps read it themselves)
source deploy/hpc-federation/site.env && export E=$(mktemp -d "$L/evidence.XXXXXX")
bash run/zone-up.sh          # the zone on Z, as a detached step; waits for its verdict
bash run/providers-up.sh     # three providers on P1-P3, a registration token piped into each; waits for three online
bash run/roundtrip.sh        # one space on P1-P3, 1 MiB written on P1, read on P2 and P3; then the agent's cell on S
bash run/cell.sh             # (optional) the cell again, with a fresh gate token, from the round trip's evidence
bash run/teardown-all.sh     # providers, then the zone; a read-only check step on all five nodes
bash run/release.sh          # scancel by job id (never -u), then the queue as it is
python3 -I run/evidence.py "$E"
```

| Script | Where it runs | Purpose |
|---|---|---|
| `common.sh` | sourced | `site.env`, the evidence directory, `st` (a step on a role's node), `detached`, the log-count patterns |
| `zone-up.sh`, `providers-up.sh` | login node | the services as detached steps, their verdicts, the registration tokens through a pipe |
| `roundtrip.sh`, `cell.sh` | login node | the federation round trip; the cell's step, its gate token through a pipe |
| `sandbox.sh` | step on S | the cell, the gate, the egress shim and collector, the token scan |
| `teardown-all.sh`, `release.sh` | login node | teardown and the per-node check; the release and the final queue |
| `fed_zone.py`, `fed_provider.py`, `pick.py` | steps on Z and P | the REST calls with the admin's and the space's tokens; what one step needs of a result |
| `agent.py` | in the cell | the probes the agent makes; it holds no token |
| `nodecheck.py` | step on every node | the read-only check of what is left on a node |
| `record.py`, `detach.sh` | everywhere | how evidence files are written; a detached step's exit status, recorded when it ends |
| `evidence.py` | login node | the summary |

## What each step writes, and what the summary checks

| Evidence (under `$E`) | Written by | Summary item and what fails it |
|---|---|---|
| `run.json` | `zone-up.sh` (common.sh's `begin_run`) | `run_identity`: no run id of 32 hex digits, no allocation, no start |
| `<role>/up.json` | `zone-up.sh`, `providers-up.sh` | `<role>_up`: no verdict line, a `FAIL`, `ERROR` or `FINISH FAILED` line, or a missing guard, default, port or hashed-value `ok:` line, in the service's batch log |
| `<role>/cb-guard-*.json` | the same | `<role>_couchbase_guard`: one `hpcop-evidence` line of the log per file; every request's status must equal its expected one (administrator 200, anonymous 401), at the start of the service, after onepanel's setup and after the rotation |
| `P<n>/registration.json` | `providers-up.sh` | `P<n>_registration`: a mint or delivery step with a non-zero exit status |
| `providers/online.*` | `providers-up.sh` | `providers_online`: fewer than three online in the zone |
| `roundtrip/prepare.json`, `support-P<n>.*`, `write.*` | `roundtrip.sh` | `space_prepared`, `support_P<n>`, `write_P1`: a failed step; a read-back SHA-256 that is not the written one |
| `roundtrip/read-P<n>.*` | `roundtrip.sh` | `read_P<n>`: the SHA-256 read is not the expected one, or the expected one is not the writer's |
| `roundtrip/http-*.jsonl`, `providers/http-zone.jsonl` | `fed_zone.py`, `fed_provider.py` | `fact_http_calls`: each call's method, path, status and body size and SHA-256 (no header, no body), raw; fewer than the zone's log and one per provider, or a line in another shape |
| `sandbox/agent.json` | `sandbox.sh` (the cell's output) | `gate_read` (the two SHA-256 strings compared, and the byte count), `gate_lookup`, `gate_listing`, `gate_write_refused` (a 4xx), `gate_strips_client_token` (a bogus client token answered as the control request is), `direct_no_token` (401 or 403), `reach_matrix` (no internal port open, the public ones open as the control), `cell_clean` (no token-like variable, three files in `/cell`) |
| `sandbox/gate-requests.jsonl`, `gate-end.json` | `sandbox.sh` | `gate_log`: no `teardown_complete` line, a forwarded write (a request other than a GET or a POST to one of the policy's two lookups, unless the gate's own decision was `deny` with a 4xx), no served content read |
| `sandbox/token-scan-*.json`, and the six scanned logs | `sandbox.sh` | `gate_token_scan`: a count above 0, a grep error (exit 2), an empty pattern or a blank line in it, a missing scan of any of the six files, a scanned log that is missing or not the bytes its scan recorded (its size and SHA-256), an empty gate log |
| `sandbox/egress.jsonl`, `egress-collector.json`, `canary.json` | `sandbox.sh` | `egress`: anything the core reads as damaged (a line of no known kind or not in the collector's shape, another format, not this launch's nonce, a gap in `seq` or a clock that goes back, no end line or end counts that are not the log's, a malformed, unwritten or dropped record, a record from another uid than the collector's), a first record that is not the canary's, a collector that did not exit 0 |
| `<role>/teardown.json`, `service-end.json`, `service-step.rc.json` | `teardown-all.sh`, `detach.sh` | `<role>_teardown`: no `service stopped` or `work directory removed`, a service that ended by itself, a service step with a non-zero exit status |
| `teardown/steps-left.json`, `nodecheck-<role>.json` | `teardown-all.sh` | `steps_left`, `nodes_clean`: a step left besides the allocation's own, a leftover on any of the five nodes, a node not checked, a node whose `ps` or `ss` did not exit 0 |
| `release/scancel.rc.json`, `squeue-final.json` | `release.sh` | `release_scancel`, `release_squeue`: a job whose name starts with `hpcop-` in `squeue -u $USER -h -o '%i %j'` after the release; the total is counted but another job of the same user is not ours |

Items of kind `fact` (the internet's port 443, the sandbox node's listener counts) are recorded and fail only when
their file is missing, empty or unparsable: the internet being reachable from the cell is the PI's ruling (detection,
not prevention), not a failure.

## The gate-log token scan

`sandbox.sh` is the only place the gate token exists in a file. It writes two 0600 files in its private directory: the
gate's own copy, which it removes once the gate is ready, and the scan's copy, kept until the cell has exited. It
asserts the scan's file is exactly one non-blank line before starting anything (an empty pattern file matches nothing; a
blank line matches every line), stops the gate and the collector, copies each log into `$E/sandbox/`, and runs
`grep -c -F -f <token file> <file>` once per file, over the gate's request log, its stdout and stderr, the agent's stdout
and stderr, and the egress log. Exit status 1 is a count of 0, and 2 an error, which fails the run. Each count and exit
status is saved with the scanned file's size and SHA-256, and the summary reads the six files too, so a log removed or
changed after its scan voids its count; the token is never printed; the scan's copy is removed after. **A count above 0
fails the run.** The
sandbox step's own stderr (its messages) is not scanned: it prints no token, and the token reaches no command line.

## Egress detection

`sandbox.sh` builds `egresslog.so` inside the cell's own image with the image's gcc, starts `egress-collector.py` outside
the cell with a fresh nonce (saved as evidence) and a fresh, empty 0700 socket directory made just before it, and runs the
cell with the socket directory, the shim directory and an `/etc/ld.so.preload` file bound read-only. All of the cell's
binds (`/cell`, `/hpcop-egress`, `/hpcop-shim`, `/etc/ld.so.preload`) are one bash array, `BINDS`, which both execs use.

**The canary.** After the collector is ready and the gate is up, and before `agent.py`, one exec in the cell (with
`BINDS`) opens a TCP connection to the gate on 127.0.0.1 and closes it. Its endpoint, exit status and result are saved as
`sandbox/canary.json`. Nothing else in the cell connects between the collector's start and the canary, so the egress log's
first record must be a `connect` to that endpoint: a log that opens with anything else, or with a malformed line, is
unknown (the shim may never have loaded), and the `egress` item fails it. The canary must also have succeeded in the cell.

After the cell exits, `sandbox.sh` stops the collector and saves its exit status and log. The summary checks only what it
can without the core, as the core reads it: every line is of a kind the collector writes and in its shape, the start
line is in the collector's format with this launch's nonce, the first record is the canary's, `seq` has no gap and the
clock never goes back, the end line is the last and its counts are the log's, and there is no malformed, unwritten or
dropped record and none from another uid than the collector's. It counts the distinct (address, port, type)
destinations, and how many are loopback, but names no address: the summary is what gets pasted. Classifying them as
gate, loopback or outside is `expcore.egress`'s job, not this step's.

**What the cell can and cannot do to the log.** The cell runs with `--containall`, so in its own PID namespace: the agent
cannot see or signal the collector or the gate, and the directory of the log file is never bound into it. Under one uid
the agent can still send forged datagrams to the collector's socket: they add lines to the log, never remove one, and a
malformed one makes the log unknown. It can also bypass the shim with a static binary, raw system calls or io_uring, which
the log does not see. If the collector dies mid-run, the shim refuses every later connect with EACCES (the agent's data
path fails too), and if it stops reading, so it does after waiting at most 2 s for room; a log without its end line
reads as unknown. Detection, not prevention.

## What the checks leave out

- The node check counts processes of our uid besides the allocation's holding script and `ps` (see `nodecheck.py`); a
  node whose own tooling shows another name fails its item and names the process, so the list is adjusted by evidence.
- `jobtmp` needs the step's `TMPDIR`: `--export=PATH` should not remove the one the site sets for a step, and a step
  without one reports `null`, which fails the run rather than passing it.
- The guard's status codes come from the batch logs of the services (`hpcop-evidence` lines, printed by the guard and by
  `cb_guard` in `lib.sh`); the check-defaults checks' own statuses stay `ok:` lines, counted in `up.json`.

## Next live run (written, not run)

1. **Scan the gate's log for the run token.** `sandbox.sh` does it as described above. Read `sandbox/token-scan-*.json`
   and `gate_token_scan` in the summary; the first run with this scan is the first that verifies the gate's log, and a
   count above 0 would be a finding about the gate, not a flake. Never print the token; to repeat the scan by hand,
   put the token in a 0600 file and run `grep -c -F -f <that file> <log>`, and remove the file.
2. **Probe the listing and token-stripping routes the gate allows.** `agent.py` looks up the space root by the space's
   name (`POST /lookup-file-id/<name>`), lists that root (`GET /data/<id>/children`), and asks `GET /data/<id>` twice,
   once with a bogus `X-Auth-Token` header and once without. The gate's policy lists all three routes
   (`gatepolicy.py`). Read `gate_lookup`, `gate_listing` and `gate_strips_client_token`: a bogus client token must
   answer as the control does (the gate strips it and sends its own); a 401 would mean the client's token reached the
   platform.
3. **A oneclient mount run (the M1 option of issue #179: `apptainer exec --fusemount`).** Planned, with nothing of it
   written yet. In the cell on S, mount the space through the image's oneclient with a read-only token passed in a 0600
   file (never in the argv or the environment), read the file under the mount, and try to write it. It must record, as
   files under `$E/mount/` that a new item reads:
   - the exact argv of the Apptainer call, with the token's file path and no token, and the version of Apptainer and of
     oneclient;
   - the mount's exit status and, verbatim after sanitising, its message if it failed (whether `--fusemount` works on a
     batch node without user namespaces is unknown, so a refusal is a result);
   - the mount's listing (entry count), the file's bytes and SHA-256 read through it beside the expected one, and the
     exit status and errno of the write (it must be refused);
   - the egress log of that cell, which will show oneclient's connections to the providers directly, not through the
     gate: the destinations are the point of the run, and the classification is the core's;
   - the token scan over oneclient's log and the cell's stdout and stderr, as in step 1 above.

   Through the mount, oneclient 25.0 names no file in its logs and `--io-trace-log` crashes it on the first read, so
   per-file served bytes exist only on the REST path through the gate; the mount run records the totals it can and says
   so.
