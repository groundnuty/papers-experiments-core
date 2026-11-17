#!/bin/bash
# restart.sh: start a stopped service again over its kept work directory, as the service step of a start
# (hpcfed.py start launches it detached on the service's node; it holds the service for the step's life, as
# provider.sbatch and zone.sbatch do). No node-init.sh, no configure.sh, no token wait: run-node.sh / run-zone.sh on the
# same rootfs, so the same config, cookie, epmd port, ports, address and registration.
#
# It refuses, touching nothing, unless the directory is a valid hpcop one of ours with rootfs.complete, the secrets
# run_service needs, a stop-requested whose first line is `keep` (a stop that kept it), identity.json (the first
# start recorded it), no recorded service process running and no running owner. Then it takes the directory over
# (owner.pid names this script), removes stop-requested, service-verified and the stale service-ready.lock, starts the
# service, and runs the checks a restart must pass (finish.sh / finish-zone.sh --restart: readiness without a fresh
# deployment, the Couchbase guard from the host, the identity unchanged, the password rotated, the live default
# check). Only then does it write service-verified, and wait for the service with the batch scripts' exit-status
# logic: a requested stop is a success (0), any other end the service's own status (1 if it exited 0).
#
# The work directory is never removed unless a stop without --keep asks for it (teardown.sh, hpcfed.py down). A failed
# restart stops the service and writes `keep` into stop-requested, so the directory stays and the start may be tried
# again; a service that ends by itself after a good restart leaves it kept the same way.
# --storage=PATH (O57): a provider's own directory on scratch, which `hpcfed.py start` reads from the federation's state
# and the work directory must record (configure.sh wrote it at the first start); the service binds it again. Without
# one, a work directory that records a storage is refused. Never for the zone.
# Usage: restart.sh <op|oz> --workdir W [--storage=PATH]
source "$(dirname "$0")/lib.sh"
component "${1:-}"; shift
[[ ${1:-} == --workdir && -n ${2:-} ]] && { (( $# == 2 )) || [[ $# == 3 && $3 == --storage=* ]]; } \
  || die "usage: restart.sh <op|oz> --workdir W [--storage=PATH]"
STORAGE=${3:-}
[[ -z $STORAGE || $COMP == op ]] || die "the zone has no posix storage: only a provider binds one"
W=$(find_workdir "$COMP" "$2")

# Every refusal comes before anything is changed.
check_service_files "$W"
kept_stop "$W" || die "$W has no kept stop (stop-requested does not begin with 'keep'): nothing to restart"
[[ -f $W/identity.json && ! -L $W/identity.json && -O $W/identity.json ]] \
  || die "$W has no identity.json: the first start never finished, so there is nothing to restart"
if recorded_service_runs "$W"; then die "the recorded service of $W still runs (pid $REC_PID): stop it first"; fi
if owner_runs "$W"; then die "the process that owns $W still runs: the stop has not finished"; fi
storage_binds "$W" "$STORAGE" > /dev/null || die "the posix storage of $W on scratch cannot be bound: nothing was changed"

case $COMP in op) RUN=run-node.sh FINISH=finish.sh ;; oz) RUN=run-zone.sh FINISH=finish-zone.sh ;; esac
export HPCOP_OWNER_PID=$$
record_owner "$W" "$$"   # this script owns the directory now: no later sweep removes it while this runs
home_in "$W"   # every child's HOME, caches and Apptainer's directories in the work directory, never the account's ~
svc='' svc_start=''
cleanup() {
  local removing=0
  # A stop without --keep (teardown.sh) while this runs wants everything removed, as the batch scripts do.
  if [[ -e $W/stop-requested ]] && ! kept_stop "$W"; then removing=1; fi
  if (( removing )); then rm -rf -- "$(secrets_dir "$W")"; fi
  if [[ -n $svc ]]; then stop_service "$svc" "${HPCOP_STOP_SECONDS:-600}" "$svc_start"; fi
  if (( removing )); then
    remove_workdir "$W"
  elif ! kept_stop "$W"; then
    put_secret "$W/stop-requested" $'keep\n'   # nothing asked for this end (a failed restart, a crash): keep it
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM INT

rm -f -- "$W/stop-requested" "$W/service-verified" "$W/rootfs/root/service-ready.lock"
say "restarting the $COMP service in $W"
bash "$HPCOP_BUNDLE/$RUN" --workdir "$W" ${STORAGE:+"$STORAGE"} &
# supervise.py, once run_service execs it: our uid, visible in /proc; its start time identifies it for every signal
svc=$!; svc_start=$(proc_field "$svc" 22 || true)
if ! bash "$HPCOP_BUNDLE/$FINISH" --restart --workdir "$W" "$svc"; then
  say "RESTART FAILED: the service is stopped and $W kept, to try again or to take down"
  exit 1
fi
mark_verified "$W"
say "the $COMP service is up again; holding until teardown or the end of the job"
status=0; wait "$svc" || status=$?
if [[ -e $W/stop-requested ]]; then
  say "the $COMP service stopped at a stop's request (status $status)"
  exit 0
fi
say "the $COMP service exited by itself (status $status): the step fails"
exit $(( status == 0 ? 1 : status ))
