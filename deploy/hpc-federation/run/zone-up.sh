#!/bin/bash
# zone-up.sh (login node): the zone as a detached step on Z; wait for its verdict; write Z's evidence.
#   Z/up.json          the counts of the service log (its verdict, the guard, the live checks, FAIL and ERROR lines)
#   Z/cb-guard-*.json  every `hpcop-evidence` line of the log: the Couchbase guard's status codes
#   Z/service-step.rc.json   written when the service step ends (teardown-all.sh): how the service ended
# Usage: E=<evidence dir> bash zone-up.sh
source "$(dirname "$0")/common.sh"
LOG=$(svc_log Z)
ev Z
rm -f -- "$LOG"
cd /
detached "$E/Z/service-step.rc.json" service_step \
  srun --jobid="$HPC_JOB" --overlap --export=PATH -w "$NODE_Z" -N1 -n1 -c"$SVC_CPUS" --mem="$SVC_MEM" --output="$LOG" \
  bash "$B/zone.sbatch" "$B" "$SH/onezone.tar" "$ZONE_NAME" "$ADDR_P1" "$ADDR_P2" "$ADDR_P3"
say "zone step launched $(date -u +%T)"
verdict=0
wait_log "$LOG" 'hpcop: OK zone|FINISH FAILED|hpcop: ERROR' 420 || verdict=1
say "verdict at $(date -u +%T)"
grep -E "work directory|Couchbase guard|administrator set|ok: all default checks|every listener|FAIL|ERROR|OK zone|info: ours|shipped|tarball" "$LOG" 2> /dev/null \
  | sed -E 's/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/<ip>/g' | cut -c1-220
record_up Z
cat "$E/Z/up.json"
squeue -s -j "$HPC_JOB" -h -o "%i %j %M"
grep -q 'hpcop: OK zone' "$LOG" || verdict=1
exit "$verdict"
