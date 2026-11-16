#!/bin/bash
#SBATCH --job-name=hpcop-provider
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --no-requeue
#SBATCH --time=03:30:00
#SBATCH --output=hpcop-provider-%j.log
# provider.sbatch: one Oneprovider for the whole life of this job, run from the batch step itself:
#   node-init.sh -> configure.sh -> wait for the registration token (deliver-token.sh, piped in through a
#   step) -> run-node.sh in the background -> finish.sh -> wait.
# The node is shared with other users by default. Every port of the provider but 8443 and 6665 listens
# on loopback, and the shim refuses loopback connections to and from other users' sockets. Add
# --exclusive on the sbatch line to keep other users off the node; it is charged as a whole node.
# --time: the run window plus about 30 min of deployment, ending before the zone's; the command line
# overrides it. Submit it just before the run, tear it down right after.
# --no-requeue: a requeued provider would restart with an empty /tmp and nothing would relaunch it.
# No secret is on this command line: only paths, names, an email and peer addresses. The token arrives
# later through a step's stdin.
# Shared imported trees (O59): a provider that owns imported spaces is given one `--import=SPACE=PATH` per space after
# the peers; configure.sh checks each tree and the service binds it read-only (importstorage.py).
# Usage: sbatch --nodelist=<node-B> --time=<t> --mem=32G [--exclusive] provider.sbatch \
#          <bundle-dir> <oneprovider.tar> <provider-name> <zone-fqdn> <admin-email> <zone-ip> [<peer-ip> ...] \
#          [--import=SPACE=PATH ...]
set -euo pipefail
B=${1:?usage: provider.sbatch <bundle-dir> <tar> <name> <zone-fqdn> <admin-email> <zone-ip> [peer-ip ...]}; shift
[[ -f $B/lib.sh ]] || { echo "hpcop: no lib.sh in '$B'" >&2; exit 1; }
source "$B/lib.sh"
private_output   # the batch log: 0600 before anything is written to it
component op
(( $# >= 5 )) || die "usage: provider.sbatch <bundle-dir> <tar> <name> <zone-fqdn> <admin-email> <zone-ip> [peer-ip ...]"
TAR=$1 NAME=$2 ZONE=$3 MAIL=$4; shift 4

export HPCOP_OWNER_PID=$$   # node-init.sh records this batch script as the owner of the work directory
W=$(bash "$B/node-init.sh" op "$TAR")
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
bash "$B/configure.sh" op --workdir "$W" "$NAME" "$ZONE" "$MAIL" "$@"

S=$(secrets_dir "$W")
say "waiting for the registration token (mint-registration-token.sh | deliver-token.sh)"
end=$((SECONDS + ${HPCOP_TOKEN_WAIT:-3600}))
until [[ -s $S/registration-token ]]; do (( SECONDS < end )) || die "no registration token was delivered"; sleep 5; done

bash "$B/run-node.sh" --workdir "$W" &
# supervise.py, once run_service execs it: our uid, visible in /proc; its start time identifies it for every signal
svc=$!; svc_start=$(proc_field "$svc" 22 || true)
if ! bash "$B/finish.sh" --workdir "$W" "$svc"; then
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
# The ready marker, once finish.sh has passed every check: service-verified holds service.pid as it is now
# (status.sh reads "ready" from it; a later launch has another service.pid, so a stale marker never matches).
mark_verified "$W"
say "provider up; holding until teardown or the end of the job"
status=0; wait "$svc" || status=$?
# The job's state says how the service ended: a stop teardown.sh asked for is a success, any other end a
# failure, with the service's own status (1 if it exited 0 by itself).
if [[ -e $W/stop-requested ]]; then
  say "the provider service stopped at teardown.sh's request (status $status)"
  exit 0
fi
say "the provider service exited by itself (status $status): the job fails"
exit $(( status == 0 ? 1 : status ))
