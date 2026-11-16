# shellcheck shell=bash
# common.sh: what every step of the run shares. Source it; never run it.
#
# The site's values (the allocation's job id, the nodes by role, their addresses, the paths) come from ../site.env,
# which is never committed; nothing site-specific is written in this repository. Nodes are named by role: Z is the
# zone, P1 to P3 the providers, S the agent's sandbox, which hosts no Onedata service. Every step writes raw evidence
# under $E (one subdirectory per step, 0700, files 0600), and run/evidence.py derives the summary from those files.
# shellcheck disable=SC2034  # the variables set here are read by the scripts that source this file

set -uo pipefail
umask 077
export LC_ALL=C

RUN=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SITE_ENV=${SITE_ENV:-$RUN/../site.env}

say() { printf 'run: %s\n' "$*" >&2; }
die() { printf 'run: ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f $SITE_ENV ]] || die "no site.env (copy site.env.example to site.env and fill it in)"
# shellcheck source=/dev/null
source "$SITE_ENV"
for key in HPC_JOB B SH L GATE_DIR NODE_Z NODE_P1 NODE_P2 NODE_P3 NODE_S ADDR_Z ADDR_P1 ADDR_P2 ADDR_P3; do
  [[ -n ${!key:-} && ${!key} != *'<'* ]] || die "site.env: $key is not filled in (see site.env.example)"
done
SVC_CPUS=${SVC_CPUS:-8}        # a service step: its cores and memory (overlap steps may sum past the job's memory)
SVC_MEM=${SVC_MEM:-19G}
ZONE_NAME=${ZONE_NAME:-expcore-zone}
SPACE_NAME=${SPACE_NAME:-run-space}
ADMIN_MAIL=${ADMIN_MAIL:-admin@example.org}
HPC_USER=${USER:-$(id -un)}

: "${E:?E must name the run evidence directory, a 0700 directory on a filesystem every node sees (run/README.md)}"
[[ -d $E && ! -L $E && -O $E && $(stat -c %a -- "$E") == 700 ]] || die "E is not a private (0700) directory of yours"

rec() { python3 -I "$RUN/record.py" "$@"; }                # how evidence files are written (record.py)
# <step>: that step's evidence directory, made once per run: a step that ran already cannot run again into this run,
# where its earlier files would stand beside the new ones (the cell is the one step repeated: fresh_step_dir).
ev() {
  mkdir -m 700 -- "$E/$1" 2> /dev/null \
    || die "the evidence of step $1 is already in this run: a step's evidence is written once (start a new run)"
}
fresh_step_dir() {  # <step>: an earlier attempt's directory moved aside, kept as raw evidence the summary does not read
  local n=1
  if [[ -e $E/$1 ]]; then
    while [[ -e $E/$1.attempt-$n ]]; do n=$((n + 1)); done
    mv -- "$E/$1" "$E/$1.attempt-$n"
  fi
  ev "$1"
}
# The run's identity. Its first step (zone-up.sh, STEP_BEGINS_RUN=1) needs an empty $E and writes run.json; every
# other step checks that run.json is this allocation's. So no file of an earlier run stands for a step of this one.
begin_run() {
  [[ -z $(ls -A -- "$E") ]] || die "E is not empty: a run starts in a fresh directory (mktemp -d), never an earlier run's"
  rec kv "$E/run.json" "run_id=str:$(rec nonce)" "job=str:$HPC_JOB" "started=str:$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
in_run() {
  local job
  job=$(rec field "$E/run.json" job 2> /dev/null) || die "E holds no run.json: start the run with zone-up.sh in a fresh E"
  [[ $job == "$HPC_JOB" ]] || die "E holds a run of another allocation: start a new run in a fresh E"
}
if [[ ${STEP_BEGINS_RUN:-} == 1 ]]; then begin_run; else in_run; fi
node_of() { local v=NODE_$1; printf '%s' "${!v}"; }         # <role>: the node's name
addr_of() { local v=ADDR_$1; printf '%s' "${!v}"; }         # <role>: the node's address on the providers' network
svc_log() {                                                 # <role>: the batch log of that role's service step
  case $1 in Z) printf '%s/hpcop-zone.log' "$L" ;; P[123]) printf '%s/hpcop-prov%s.log' "$L" "${1#P}" ;; *) die "no such role: $1" ;; esac
}
# The work directory and the FQDN a service printed into its log; neither is secret, and neither is written anywhere.
wlog() { sed -n 's/^hpcop: work directory \(\/[^ ]*\)$/\1/p' "$1" | head -1; }
fqdn_of() { sed -n "s/^ok: configured o[zp] '[^']*' on \([A-Za-z0-9.-]*\) with .*/\1/p" "$1" | head -1; }

# <role> <srun options> <command...>: one step on that role's node. No environment is passed (--export=PATH); a step
# whose stdin is not the payload ends in </dev/null at the call.
st() {
  local node
  node=$(node_of "$1"); shift
  srun --jobid="$HPC_JOB" --overlap --export=PATH -w "$node" -N1 -n1 "$@"
}

# <rc file> <name> <command...>: a step that outlives this shell (setsid nohup), whose exit status is recorded when it
# ends. The srun inside it keeps the service for the job's life; teardown-all.sh ends it.
detached() {
  local rcfile=$1 name=$2
  shift 2
  setsid nohup bash "$RUN/detach.sh" "$rcfile" "$name" "$@" < /dev/null > /dev/null 2>&1 &
}

wait_log() {  # <log> <extended regex> <seconds>: until a line of the log matches; 0 if it did
  local end=$((SECONDS + $3))
  while (( SECONDS < end )); do
    grep -q -E "$2" "$1" 2> /dev/null && return 0
    sleep 5
  done
  return 1
}

# What a service's log must show, counted by record.py (the counts are the evidence; the verdict is evidence.py's).
UP_COUNTS=(
  'verdict_ok=hpcop: OK (zone|provider)'
  'ok_lines=^ok:'
  'fail_lines=^FAIL'
  'error_lines=hpcop: ERROR'
  'finish_failed=FINISH FAILED'
  'guard_passed=^ok: deployment finished; Couchbase guard passed'   # wait_ready's excerpt of run.log's own line
  'defaults_ok=^ok: all default checks passed \(live\)'
  'port_check_ok=^ok: every listener of this service off loopback is public'
  'live_hash_ok=^ok: live Couchbase cookies and memcached password are not the shipped ones'
)
TEARDOWN_COUNTS=(
  'service_stopped=^ok: service stopped'
  'workdir_removed=^ok: work directory .* removed'
  'errors=hpcop: ERROR'
)
END_COUNTS=(
  "stopped_at_request=service stopped at teardown\\.sh's request"
  'exited_by_itself=service exited by itself'
)

record_up() {  # <role>: the counts of the service's log, and the guard's status codes, one file each (the role's
  local log     # directory is the step's own, made when it launched the service)
  log=$(svc_log "$1")
  rec logscan "$E/$1/up.json" "$log" "${UP_COUNTS[@]}"
  rec guard-lines "$log" "$E/$1" > /dev/null
}

# The agent's cell on S. stdin: the gate token, one line (pick.py gate). Everything else comes from the saved evidence
# of the round trip and from the services' logs, never from a value typed here.
cell_step() {
  local w=$E/roundtrip/write.out.json sid root fid sha zf p1 p2 p3 rc
  sid=$(rec field "$w" space_id) && root=$(rec field "$w" root_id) && fid=$(rec field "$w" file_id) \
    && sha=$(rec field "$w" sha256) || die "no usable roundtrip/write.out.json: run roundtrip.sh first"
  zf=$(fqdn_of "$(svc_log Z)") p1=$(fqdn_of "$(svc_log P1)") p2=$(fqdn_of "$(svc_log P2)") p3=$(fqdn_of "$(svc_log P3)")
  [[ -n $zf && -n $p1 && -n $p2 && -n $p3 ]] || die "a service's FQDN is not in its log"
  fresh_step_dir sandbox   # cell.sh repeats the cell: the summary reads only this attempt's files
  srun --jobid="$HPC_JOB" --overlap --export=PATH -w "$NODE_S" -N1 -n1 -c4 --mem=16G \
    bash "$RUN/sandbox.sh" "$RUN" "$B" "$GATE_DIR" "$SH/oneprovider-docker.tar" "$E" "$SPACE_NAME" "$sid" "$root" "$fid" "$sha" \
    "$zf" "$p1" "$p2" "$p3" 2>&1 | tee "$L/sandbox.out"
  rc=${PIPESTATUS[0]}
  rec rc "$E/sandbox/step.rc.json" sandbox_step "$rc"
  say "sandbox step: exit status $rc at $(date -u +%T)"
}
