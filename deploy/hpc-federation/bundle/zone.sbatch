#!/bin/bash
#SBATCH --job-name=hpcop-zone
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --no-requeue
#SBATCH --time=04:00:00
#SBATCH --output=hpcop-zone-%j.log
# zone.sbatch: one Onezone for the whole life of this job, run from the batch step itself, so it does
# not depend on a login-node srun and costs no job steps:
#   node-init.sh -> configure.sh -> run-zone.sh in the background -> finish-zone.sh -> wait.
# The node is shared with other users by default. Every port of the zone but 8443 listens on loopback,
# and the shim refuses loopback connections to and from other users' sockets (README, "Exposure model").
# Add --exclusive on the sbatch line to keep other users off the node; it is charged as a whole node.
# --time: the run window plus about 30 min of deployment, and the longest of the federation (users cannot
# extend it; the command line overrides the default above). Submit it just before the run, tear it down
# right after: the service is exposed only while it runs.
# --no-requeue: a requeued zone would come back empty, as a new identity.
# tmpfs counts against --mem: the rootfs (~4 GB) plus Couchbase and the Erlang VMs.
# No secret is on this command line: only paths, a name and peer addresses.
# A federation reached by the names its layout declares (O70) adds `--declared=NAME` (the zone's own) and one
# `--host=NAME=ADDRESS` per service of the federation, itself included, after the peers: configure.sh registers the zone
# under NAME and writes the hosts file its cell is given (see configure.sh).
# Usage: sbatch --nodelist=<node-A> --time=<t> --mem=32G [--exclusive] zone.sbatch \
#          <bundle-dir> <onezone.tar> <zone-name> [<provider-ip> ...] [--declared=NAME --host=NAME=ADDRESS ...]
set -euo pipefail
B=${1:?usage: zone.sbatch <bundle-dir> <onezone.tar> <zone-name> [provider-ip ...]}; shift
[[ -f $B/lib.sh ]] || { echo "hpcop: no lib.sh in '$B'" >&2; exit 1; }
source "$B/lib.sh"
private_output   # the batch log: 0600 before anything is written to it
component oz
(( $# >= 2 )) || die "usage: zone.sbatch <bundle-dir> <onezone.tar> <zone-name> [provider-ip ...]"
TAR=$1 NAME=$2; shift 2

export HPCOP_OWNER_PID=$$   # node-init.sh records this batch script as the owner of the work directory
W=$(bash "$B/node-init.sh" oz "$TAR")
home_in "$W"   # every child's HOME, caches and Apptainer's directories in the work directory, never the account's ~
svc='' svc_start=''
cleanup() {
  if kept_stop "$W"; then   # teardown.sh --keep (hpcfed.py stop): stop what still runs, remove nothing
    if [[ -n $svc ]]; then stop_service "$svc" "${HPCOP_STOP_SECONDS:-600}" "$svc_start"; fi
    return 0
  fi
  rm -rf -- "$(secrets_dir "$W")"   # not needed to stop; gone even if SIGKILL cuts the stop short
  if [[ -n $svc ]]; then stop_service "$svc" "${HPCOP_STOP_SECONDS:-600}" "$svc_start"; fi
  remove_workdir "$W"
}
trap cleanup EXIT
trap 'exit 143' TERM INT
say "work directory $W"
bash "$B/configure.sh" oz --workdir "$W" "$NAME" "$@"
bash "$B/run-zone.sh" --workdir "$W" &
# supervise.py, once run_service execs it: our uid, visible in /proc; its start time identifies it for every signal
svc=$!; svc_start=$(proc_field "$svc" 22 || true)
if ! bash "$B/finish-zone.sh" --workdir "$W" "$svc"; then
  # Stop the unverified service and delete every secret, then keep the logs and work directory for a
  # bounded time, to read from a step (srun --overlap ... cat <workdir>/run.log), until teardown.sh.
  stop_service "$svc" "${HPCOP_STOP_SECONDS:-600}" "$svc_start"; svc=
  scrub_secrets "$W"
  hold=${HPCOP_INSPECT_SECONDS:-1800}
  say "FINISH FAILED: service stopped, secrets removed; $W kept for inspection for ${hold}s or until teardown.sh"
  end=$((SECONDS + hold))
  while [[ -e $W ]] && (( SECONDS < end )); do sleep 10; done
  exit 1
fi
# The ready marker, once finish-zone.sh has passed every check: service-verified holds service.pid as it is now
# (status.sh reads "ready" from it; a later launch has another service.pid, so a stale marker never matches).
mark_verified "$W"
say "zone up at https://$(service_name "$W"):8443; holding until teardown or the end of the job"
status=0; wait "$svc" || status=$?
# The job's state says how the service ended: a stop teardown.sh asked for is a success, any other end a
# failure, with the service's own status (1 if it exited 0 by itself).
if [[ -e $W/stop-requested ]]; then
  say "the zone service stopped at teardown.sh's request (status $status)"
  exit 0
fi
say "the zone service exited by itself (status $status): the job fails"
exit $(( status == 0 ? 1 : status ))
