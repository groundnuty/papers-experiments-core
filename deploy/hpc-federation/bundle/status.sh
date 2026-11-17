#!/bin/bash
# status.sh: whether a service runs, from a control step on its node (hpcfed.py status). Read-only: it signals
# nothing and writes nothing. Prints one JSON line on stdout, no secret:
#   {"alive": <bool>, "pid": <int or null>, "start": <string or null>, "ready": <bool>[, "domain_wait_s": <number>]}
# alive: the process service.pid records (supervise.py: PID and start tick, stat field 22) runs, our uid, not a zombie
# (running_ours, which holds under hidepid). pid and start: that record, when it is well formed, whether or not the
# process still runs (null when there is none). ready: alive, and service-verified holds exactly the bytes of
# service.pid (the batch script or restart.sh wrote it once this launch passed its checks), so a marker an earlier
# launch left never reads as this one's. domain_wait_s: only for a ready service, the seconds this launch's start
# waited for op_worker's domain (finish.sh wrote it to domain-wait, restart.sh removed an earlier launch's first):
# 0 when no wait was needed, and left out when the record is missing, a link, not ours or not a number of the form
# finish.sh writes, never a guess. A stopped service is alive false, exit status 0; the exit status is not 0
# only when the work directory is not a valid hpcop one of ours.
# Usage: status.sh <op|oz> --workdir W
source "$(dirname "$0")/lib.sh"
component "${1:-}"; shift
[[ ${1:-} == --workdir && -n ${2:-} && $# == 2 ]] || die "usage: status.sh <op|oz> --workdir W"
W=$(find_workdir "$COMP" "$2")
home_at "$W"   # every tool's HOME in the work directory, never the account's ~; nothing made: status writes nothing
alive=false ready=false pid=null start=null
if recorded_service_runs "$W"; then alive=true; fi
if [[ -n $REC_PID ]]; then pid=$REC_PID start=\"$REC_START\"; fi
if [[ $alive == true && -f $W/service-verified && ! -L $W/service-verified && -O $W/service-verified ]] \
    && cmp -s -- "$W/service-verified" "$W/service.pid"; then
  ready=true
fi
waited=''
if [[ $ready == true && -f $W/domain-wait && ! -L $W/domain-wait && -O $W/domain-wait ]]; then
  IFS= read -r waited < "$W/domain-wait" || true
  [[ $waited =~ ^(0|[1-9][0-9]{0,5})\.[0-9]{3}$ ]] || waited=''
fi
if [[ -n $waited ]]; then
  printf '{"alive": %s, "pid": %s, "start": %s, "ready": %s, "domain_wait_s": %s}\n' "$alive" "$pid" "$start" "$ready" "$waited"
else
  printf '{"alive": %s, "pid": %s, "start": %s, "ready": %s}\n' "$alive" "$pid" "$start" "$ready"
fi
