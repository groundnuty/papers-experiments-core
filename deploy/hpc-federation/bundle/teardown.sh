#!/bin/bash
# teardown.sh: stop one service gracefully and wipe its work directory, in a step of its own job.
# SIGTERM goes only to the service process this launch recorded (service.pid: PID and start time, stat
# field 22): run-node.sh / run-zone.sh, now supervise.py, which forwards it to apptainer's starter, its own child.
# Under hidepid=invisible the setuid starter is hidden from us in /proc (live run 4), supervise.py is not, so its
# start time can be read; the signal goes through a pidfd opened before that check (signal_ours), so a reused PID
# is never signalled. The stock launcher traps it and stops the panel, worker, cluster manager and Couchbase. The
# batch script then removes the work directory (rootfs, secrets, Couchbase data, logs) and the job ends; whatever
# is left is removed here. Providers first, then the zone; then scancel both jobs.
#
# --keep [--keep-until EPOCH] is a stop that removes nothing (hpcfed.py stop; deploy/hpc-federation/DESIGN.md, section
# 7): `keep` is written as the first line of stop-requested, and the epoch (seconds; the allocation's end) into
# keep-until, both before the SIGTERM, so the batch script's cleanup and every later sweep leave the work directory,
# its secrets, the Couchbase data and the registration alone. Then the recorded service gets up to TIMEOUT seconds to
# end, the escalation of stop_service (SIGUSR1, 30 s, SIGKILL) after that, and the one line this script prints on
# stdout says how it ended: hpcop-stop {"ended": "exit"|"kill"|"none", "seconds": <float>}. none: no running service
# of this launch, nothing was signalled (a repeat of a stop is a success). Exit status 0 once the process is gone, 1
# when it is still there after the SIGKILL (no line is printed then).
# Usage: teardown.sh <op|oz> [--workdir W] [--timeout SECONDS] [--keep [--keep-until EPOCH]]
source "$(dirname "$0")/lib.sh"
component "${1:-}"; shift
WD='' TIMEOUT=600 KEEP=0 UNTIL=''
while (( $# )); do
  case $1 in
    --workdir) WD=${2:?}; shift 2 ;;
    --timeout) TIMEOUT=${2:?}; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --keep-until) UNTIL=${2:?}; shift 2 ;;
    *) die "unknown option '$1'" ;;
  esac
done
[[ $TIMEOUT =~ ^[0-9]+$ ]] || die "--timeout takes seconds"
[[ -z $UNTIL || ( $KEEP == 1 && $UNTIL =~ ^[0-9]{1,15}$ ) ]] || die "--keep-until takes an epoch in seconds, and only with --keep"
W=$(find_workdir "$COMP" "$WD")
pid='' start=''
if [[ -s $W/service.pid ]]; then read -r pid start < "$W/service.pid" || true; fi
ours() {  # the process run_service recorded (supervise.py): same PID, same start time (stat field 22), our uid, alive
  [[ $pid =~ ^[0-9]+$ && $start =~ ^[0-9]+$ && -O /proc/$pid ]] && running_ours "$pid" "$start"
}
if (( KEEP )); then
  # Marked first, whether or not the service still runs: the work directory is kept either way, and restart.sh asks
  # for the mark. keep-until before stop-requested, so a reader of `keep` always finds the time too.
  if [[ -n $UNTIL ]]; then put_secret "$W/keep-until" "$UNTIL"$'\n'; fi
  put_secret "$W/stop-requested" $'keep\n'
  rc=0
  stop_kept_service "$pid" "$start" "$TIMEOUT" || rc=$?
  (( rc == 0 )) || die "the service (pid $pid) is still running after the SIGKILL: scancel its step"
  say "kept stop ended: $STOP_ENDED after $STOP_SECONDS s; $W and its secrets are kept"
  printf 'hpcop-stop {"ended": "%s", "seconds": %s}\n' "$STOP_ENDED" "$STOP_SECONDS"
  exit 0
fi
# The batch script reads this after the service ends: a stop asked for here is the job's success.
if ours; then : > "$W/stop-requested"; fi
if ours && signal_ours "$pid" "$start" TERM; then
  say "SIGTERM sent to the service (pid $pid, started at tick $start); waiting up to ${TIMEOUT}s for a graceful stop"
  end=$((SECONDS + TIMEOUT))
  while ours; do (( SECONDS < end )) || die "the service is still running after ${TIMEOUT}s: scancel the job"; sleep 5; done
  ok "service stopped"
else
  say "no running service of this launch in $W (pid '$pid' is gone or is now another process: not signalled)"
fi
end=$((SECONDS + (TIMEOUT < 60 ? TIMEOUT : 60)))
while [[ -e $W ]] && (( SECONDS < end )); do sleep 2; done   # the batch script's own cleanup
remove_workdir "$W"
ok "work directory $W removed"
