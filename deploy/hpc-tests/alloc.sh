#!/usr/bin/env bash
# alloc.sh: the login-node side of the HPC test runner (README.md): the standing allocation's submit, listing, release
# and renewal. It lives in the login directory (0700, HPCT_LOGIN_DIR) beside login.env (what sbatch needs, written by
# `hpctests.py install` through ssh's stdin, never on a command line) and the ledger of the job ids it submitted.
#
#   alloc.sh submit [--begin <epoch>]    one allocation: `exec sleep infinity` with --no-requeue and a --time; what
#                                        Slurm recorded is read back, and exactly that job is cancelled if it differs
#   alloc.sh list                        NOW <epoch>, a JOB line per ledger job alive under our name, a RENEWER line
#   alloc.sh release <id>                cancel one allocation of ours: in the ledger and alive under our name
#   alloc.sh renew --until <epoch> [--lead s] [--overlap s] [--poll s] [--max n] [--once]
#                                        keep an allocation of ours alive until <epoch> (the loop; README, "Renewal")
#   alloc.sh renew-start <renew options> start that loop detached (setsid nohup), from a frozen copy of this script
#   alloc.sh renew-stop                  ask the loop to stop at its next poll
#
# "Ours" is the ledger AND the name: a job of the account that this ledger does not list (another study's, another
# lane's) is never listed, renewed or cancelled, and no command here cancels by user. The clock is read (date +%s) at
# every decision; Slurm's times, printed in the login node's zone, are converted with date -d on this same node.
set -u
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LEDGER=$DIR/ledger
STATE=$DIR/renew.state
LOCK=$DIR/renew.lock
STOP=$DIR/renew.stop

die() { echo "alloc.sh: $1" >&2; exit "${2:-2}"; }
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
is_count() { case "$1" in "" | *[!0-9]* | 0?*) return 1 ;; *) return 0 ;; esac; }

load_env() {
  ACCOUNT="" PARTITION="" NAME="" CPUS="" MEM="" HOURS="" MAX_ALIVE=2
  [ -f "$DIR/login.env" ] || die "no login.env beside alloc.sh (run hpctests.py install)"
  local key value
  while IFS='=' read -r key value; do
    case "$key" in
      HPCT_ACCOUNT) ACCOUNT=$value ;;
      HPCT_PARTITION) PARTITION=$value ;;
      HPCT_JOB_NAME) NAME=$value ;;
      HPCT_CPUS) CPUS=$value ;;
      HPCT_MEM) MEM=$value ;;
      HPCT_HOURS) HOURS=$value ;;
      HPCT_MAX_ALIVE) MAX_ALIVE=$value ;;
      "") ;;
      *) die "login.env has an unknown key $key" ;;
    esac
  done < "$DIR/login.env"
  for value in "$ACCOUNT" "$PARTITION" "$NAME"; do
    case "$value" in "" | -* | *[!A-Za-z0-9_.-]*) die "login.env: account, partition and name are [A-Za-z0-9_.-]" ;; esac
  done
  for value in "$CPUS" "$HOURS" "$MAX_ALIVE"; do is_count "$value" && [ "$value" != 0 ] || die "login.env: bad count $value"; done
  [ "$HOURS" -le 72 ] || die "login.env: HPCT_HOURS is at most 72"
  case "$MEM" in [1-9]*[MG]) is_count "${MEM%[MG]}" || die "login.env: bad memory $MEM" ;; *) die "login.env: bad memory $MEM" ;; esac
}

to_epoch() { # a Slurm time (YYYY-MM-DDTHH:MM:SS, the login node's zone) in seconds since the epoch; - for N/A
  case "$1" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]) date -d "$1" +%s ;;
    *) echo - ;;
  esac
}

to_mb() { # 30G, 2048M, 1T as megabytes
  case "$1" in
    *T) echo $((${1%T} * 1048576)) ;; *G) echo $((${1%G} * 1024)) ;; *M) echo "${1%M}" ;; *) echo "" ;;
  esac
}

limit_s() { # a Slurm time limit, [D-]HH:MM:SS, in seconds; empty for anything else (UNLIMITED, N/A)
  local d=0 rest=$1 h m s
  case "$rest" in *-*) d=${rest%%-*}; rest=${rest#*-} ;; esac
  IFS=: read -r h m s <<< "$rest"
  case "$d:$h:$m:$s" in *[!0-9:]* | *::* | :* | *:) echo ""; return 0 ;; esac
  echo $(((10#$d * 86400) + (10#$h * 3600) + (10#$m * 60) + 10#$s))
}

alive() { # id|name|state|start|end|cpus|mem for every job of the ledger that squeue shows under our name
  local rows ids
  rows=$(squeue -h --me -o '%i|%j|%T|%S|%e|%C|%m') || return 1
  ids=$( [ -f "$LEDGER" ] && awk '$1 ~ /^[0-9]+$/ {print $1}' "$LEDGER" | sort -u | tr '\n' ' ')
  printf '%s\n' "$rows" | awk -F'|' -v name="$NAME" -v ids="$ids" '
    BEGIN { n = split(ids, a, " "); for (i = 1; i <= n; i++) want[a[i]] = 1 }
    ($1 in want) && $2 == name'
}

record() { # record <id> <begin>: append to the ledger, under its lock
  (
    flock 9
    printf '%s %s %s %s\n' "$1" "$NAME" "$(date +%s)" "$2" >> "$LEDGER"
  ) 9> "$DIR/ledger.lock"
}

readback() { # readback <id>: what Slurm recorded for the job, against what was asked; the differences on stdout
  # (a record that cannot be read differs in every field, so the job is cancelled rather than kept unchecked)
  local rec cpus mem
  rec=$(scontrol show job -o "$1" | tr ' ' '\n')
  recorded() { printf '%s\n' "$rec" | sed -n "s/^$1=//p" | head -n 1; }
  [ "$(recorded JobName)" = "$NAME" ] || echo "JobName is not $NAME"
  [ "$(recorded Requeue)" = 0 ] || echo "Requeue is not 0"
  [ "$(recorded Account)" = "$ACCOUNT" ] || echo "the account is not the one asked for"
  [ "$(recorded Partition)" = "$PARTITION" ] || echo "the partition is not the one asked for"
  cpus=$(recorded NumCPUs)
  [ "${cpus%%-*}" = "$CPUS" ] || echo "NumCPUs is $cpus, not $CPUS"
  mem=$(recorded MinMemoryNode)
  [ -n "$mem" ] && [ "$(to_mb "$mem")" = "$(to_mb "$MEM")" ] || echo "MinMemoryNode is $mem, not $MEM"
  [ "$(limit_s "$(recorded TimeLimit)")" = $((HOURS * 3600)) ] || echo "TimeLimit is $(recorded TimeLimit), not $HOURS h"
  case "$(recorded JobState)" in PENDING | RUNNING | CONFIGURING) ;; *) echo "JobState is $(recorded JobState)" ;; esac
}

# Exit: 0 submitted and as asked; 1 not as asked, and that job cancelled; 2 sbatch failed (nothing known submitted);
# 3 refused by the guard (already MAX_ALIVE allocations of ours); 4 not as asked and the cancel failed.
cmd_submit() {
  local begin="" out err id rows count problems
  local -a extra=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --begin) is_count "${2:-}" || die "--begin takes seconds since the epoch"; begin=$2; shift 2 ;;
      *) die "submit: unknown argument $1" ;;
    esac
  done
  load_env
  rows=$(alive) || die "squeue failed: nothing submitted" 2
  count=$(printf '%s\n' "$rows" | grep -c '[0-9]')
  [ "$count" -lt "$MAX_ALIVE" ] || die "$count allocations of ours are alive already (the limit is $MAX_ALIVE): nothing submitted" 3
  [ -z "$begin" ] || extra=(--begin="$(date -d "@$begin" +%Y-%m-%dT%H:%M:%S)")
  err=$(mktemp "$DIR/.sbatch-err.XXXXXX") || die "mktemp failed"
  # The account and partition reach sbatch through its own input variables, so no command line shows them.
  out=$(SBATCH_ACCOUNT=$ACCOUNT SBATCH_PARTITION=$PARTITION sbatch --parsable --job-name="$NAME" --nodes=1 --ntasks=1 \
    --cpus-per-task="$CPUS" --mem="$MEM" --time="$(printf '%02d:00:00' "$HOURS")" --no-requeue --export=NONE \
    --chdir=/ --output=/dev/null --error=/dev/null ${extra[@]+"${extra[@]}"} --wrap='exec sleep infinity' 2> "$err")
  local rc=$?
  [ ! -s "$err" ] || sed 's/^/alloc.sh: sbatch said: /' "$err" >&2
  rm -f "$err"
  [ "$rc" = 0 ] || die "sbatch failed (exit $rc): nothing submitted" 2
  id=${out%%;*}
  is_count "$id" || die "sbatch printed '$out', not a job id" 2
  record "$id" "${begin:--}"
  problems=$(readback "$id")
  if [ -n "$problems" ]; then
    printf 'alloc.sh: job %s is NOT AS ASKED: %s\n' "$id" "$problems" >&2
    # The one cancel of a submit: the id sbatch has just printed to this process, never another.
    if scancel "$id"; then echo "alloc.sh: job $id cancelled; nothing is left of this submit" >&2; exit 1; fi
    echo "alloc.sh: COULD NOT CANCEL job $id: cancel it by its id now" >&2
    exit 4
  fi
  echo "SUBMITTED $id${begin:+ begin=$begin}"
}

cmd_list() {
  local rows id name state start end cpus mem
  load_env
  echo "NOW $(date +%s)"
  rows=$(alive) || die "squeue failed"
  while IFS='|' read -r id name state start end cpus mem; do
    [ -n "$id" ] || continue
    echo "JOB $id $name $state $(to_epoch "$start") $(to_epoch "$end") $cpus $mem"
  done <<< "$rows"
  echo "RENEWER $(renewer_status)"
}

renewer_status() {
  local words="" heartbeat="" poll=300 status=none
  [ ! -f "$STATE" ] || words=$(head -n 1 "$STATE")
  heartbeat=$(printf '%s\n' "$words" | tr ' ' '\n' | sed -n 's/^heartbeat=//p')
  poll=$(printf '%s\n' "$words" | tr ' ' '\n' | sed -n 's/^poll=//p')
  if [ -d "$LOCK" ]; then
    status=stale
    if is_count "$heartbeat" && is_count "$poll" && [ $(($(date +%s) - heartbeat)) -lt $((3 * poll + 60)) ]; then status=running; fi
  elif [ -n "$words" ]; then
    status=ended
  fi
  echo "status=$status $(printf '%s\n' "$words" | tr ' ' '\n' | grep -v -E '^(pid|status)=' | tr '\n' ' ')"
}

cmd_release() {
  local id=${1:-} rows
  is_count "$id" || die "release takes a job id"
  load_env
  [ -f "$LEDGER" ] && awk -v id="$id" '$1 == id {f = 1} END {exit !f}' "$LEDGER" \
    || die "job $id is not in this ledger: it is not ours, and nothing was cancelled" 3
  rows=$(alive) || die "squeue failed: nothing cancelled"
  printf '%s\n' "$rows" | awk -F'|' -v id="$id" '$1 == id {f = 1} END {exit !f}' \
    || die "job $id is not alive under the name $NAME: nothing cancelled" 3
  # The one other cancel: a job of the ledger, alive under our name, by its id.
  scancel "$id" || die "scancel $id failed"
  echo "RELEASED $id"
}

# ---- renewal -------------------------------------------------------------------------------------------------------
UNTIL="" LEAD=10800 OVERLAP=900 POLL=300 MAX=4 ONCE=0
parse_renew() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --until) UNTIL=${2:-}; shift 2 ;;
      --lead) LEAD=${2:-}; shift 2 ;;
      --overlap) OVERLAP=${2:-}; shift 2 ;;
      --poll) POLL=${2:-}; shift 2 ;;
      --max) MAX=${2:-}; shift 2 ;;
      --once) ONCE=1; shift ;;
      *) die "renew: unknown argument $1" ;;
    esac
  done
  for value in "$UNTIL" "$LEAD" "$OVERLAP" "$POLL" "$MAX"; do is_count "$value" || die "renew: bad number '$value'"; done
  [ "$POLL" -ge 1 ] && [ "$MAX" -ge 1 ] || die "renew: --poll and --max are at least 1"
  [ "$OVERLAP" -lt "$LEAD" ] || die "renew: --overlap must be shorter than --lead"
}

write_state() { # write_state <status> <submitted>
  printf 'pid=%s heartbeat=%s until=%s submitted=%s max=%s poll=%s status=%s\n' \
    "$$" "$(date +%s)" "$UNTIL" "$2" "$MAX" "$POLL" "$1" > "$STATE.new" && mv -f "$STATE.new" "$STATE"
}

take_lock() {
  local heartbeat
  if mkdir "$LOCK" 2> /dev/null; then echo "$$" > "$LOCK/owner"; return 0; fi
  heartbeat=$( [ -f "$STATE" ] && tr ' ' '\n' < "$STATE" | sed -n 's/^heartbeat=//p')
  if is_count "$heartbeat" && [ $(($(date +%s) - heartbeat)) -lt $((3 * POLL + 60)) ]; then
    die "a renewer is running (heartbeat $(($(date +%s) - heartbeat)) s ago): not started" 3
  fi
  log "the renew lock is stale (heartbeat ${heartbeat:-none}): taking it over"
  rm -rf "$LOCK"
  mkdir "$LOCK" 2> /dev/null || die "cannot take the renew lock" 3
  echo "$$" > "$LOCK/owner"
}

drop_lock() { [ "$(cat "$LOCK/owner" 2> /dev/null)" != "$$" ] || rm -rf "$LOCK"; }

# One decision, from the clock and squeue read now. Prints SUBMIT-FAILED/STOP on stdout for the loop; 0 otherwise.
renew_once() {
  local now rows pending end begin out rc
  now=$(date +%s)
  rows=$(alive) || { log "squeue failed: no decision this round"; return 0; }
  pending=$(printf '%s\n' "$rows" | awk -F'|' '$3 == "PENDING" || $3 == "CONFIGURING" {n++} END {print n + 0}')
  if [ "$pending" -gt 0 ]; then return 0; fi
  end=$(printf '%s\n' "$rows" | awk -F'|' '$3 == "RUNNING" {print $5}' | while read -r t; do to_epoch "$t"; done | grep -E '^[0-9]+$' | sort -n | tail -n 1)
  if [ -z "$end" ]; then
    begin=$now
    log "no allocation of ours is alive: submitting one now"
  elif [ $((end - now)) -gt "$LEAD" ]; then
    return 0
  else
    begin=$((end - OVERLAP))
    [ "$begin" -gt "$now" ] || begin=$now
    log "the allocation of ours ends in $((end - now)) s: submitting its successor to begin at $(date -u -d "@$begin" +%FT%TZ)"
  fi
  [ "$begin" -lt "$UNTIL" ] || { log "a successor would begin at or after --until: none submitted"; return 0; }
  out=$(cmd_submit --begin "$begin" 2>&1)
  rc=$?
  printf '%s\n' "$out" | sed 's/^/  /'
  case "$rc" in
    0) echo "SUBMITTED" ;;
    2) echo "SUBMIT-FAILED" ;;
    3) ;;
    *) echo "STOP" ;;
  esac
}

NAP="" SUBMITTED_COUNT=0
on_signal() {
  [ -z "$NAP" ] || kill "$NAP" 2> /dev/null
  write_state ended:signal "$SUBMITTED_COUNT"
  log "renewer ended: a signal, $SUBMITTED_COUNT submitted"
  exit 143
}

cmd_renew() {
  parse_renew "$@"
  load_env
  local submitted=0 failures=0 decision reason=""
  if [ "$ONCE" = 0 ]; then
    take_lock
    trap 'drop_lock' EXIT
    trap 'on_signal' TERM HUP INT
    log "renewer started: until $(date -u -d "@$UNTIL" +%FT%TZ), lead $LEAD s, overlap $OVERLAP s, poll $POLL s, at most $MAX submits"
  fi
  while :; do
    write_state running "$submitted"
    if [ -e "$STOP" ]; then reason=stop-file; break; fi
    if [ "$(date +%s)" -ge "$UNTIL" ]; then reason=until; break; fi
    decision=$(renew_once)
    printf '%s\n' "$decision" | grep -v -E '^(SUBMITTED|SUBMIT-FAILED|STOP)$' | sed '/^$/d'
    case "$decision" in
      *SUBMITTED) submitted=$((submitted + 1)); failures=0 ;;
      *SUBMIT-FAILED) failures=$((failures + 1)) ;;
      *STOP) reason=submit-not-as-asked; break ;;
    esac
    SUBMITTED_COUNT=$submitted
    if [ "$failures" -ge 3 ]; then reason=three-failed-submits; break; fi
    if [ "$submitted" -ge "$MAX" ]; then reason=max-submits; break; fi
    if [ "$ONCE" = 1 ]; then reason=once; break; fi
    sleep "$POLL" &
    NAP=$!
    wait "$NAP"
    NAP=""
  done
  write_state "ended:$reason" "$submitted"
  [ "$ONCE" = 1 ] || log "renewer ended: $reason, $submitted submitted"
  case "${BASH_SOURCE[0]}" in "$DIR"/.renew-*.sh) rm -f "${BASH_SOURCE[0]}" ;; esac
}

cmd_renew_start() {
  parse_renew "$@"
  [ "$ONCE" = 0 ] || die "renew-start runs the loop; --once is for renew"
  load_env
  rm -f "$STOP"
  # bash reads a script as it runs: the loop runs a frozen copy, so installing a new alloc.sh never changes it.
  local frozen
  frozen=$DIR/.renew-$(date +%s)-$$.sh
  cp "$DIR/alloc.sh" "$frozen" && chmod 0700 "$frozen" || die "cannot freeze a copy of alloc.sh"
  { setsid nohup bash "$frozen" renew "$@" >> "$DIR/renew.log" 2>&1 < /dev/null & }
  sleep 2
  tail -n 3 "$DIR/renew.log"
  echo "RENEWER $(renewer_status)"
}

cmd_renew_stop() {
  : > "$STOP"
  echo "the renewer stops at its next poll (renew.stop written)"
}

[ $# -ge 1 ] || die "usage: alloc.sh submit|list|release|renew|renew-start|renew-stop ..."
umask 077
sub=$1
shift
case "$sub" in
  submit) cmd_submit "$@" ;;
  list) cmd_list ;;
  release) cmd_release "$@" ;;
  renew) cmd_renew "$@" ;;
  renew-start) cmd_renew_start "$@" ;;
  renew-stop) cmd_renew_stop ;;
  *) die "unknown subcommand $sub" ;;
esac
