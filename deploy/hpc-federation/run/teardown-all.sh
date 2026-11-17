#!/bin/bash
# teardown-all.sh (login node): stop the three providers, then the zone, each with teardown.sh in a step on its node;
# wait for each service step to end; then a read-only check step on all five nodes (Z, P1-P3, S). Evidence, per service
# role: teardown.json (the counts of teardown.sh's log and its exit status), service-end.json (how the service step's
# log ended: at the stop requested, or by itself), service-step.rc.json (the service step's own exit status, written by
# detach.sh); teardown/steps-left.json (the steps still on the allocation); teardown/nodecheck-<role>.json.
# Usage: E=<evidence dir> bash teardown-all.sh
source "$(dirname "$0")/common.sh"
ev teardown
cd /
declare -a pids
for n in 1 2 3; do
  PW=$(wlog "$(svc_log "P$n")")
  st "P$n" -c1 --mem=300M bash "$B/teardown.sh" op --workdir "$PW" < /dev/null > "$L/teardown-prov$n.log" 2>&1 &
  pids[n]=$!
done
for n in 1 2 3; do
  wait "${pids[n]}"
  rec logscan "$E/P$n/teardown.json" "$L/teardown-prov$n.log" --rc "$?" "${TEARDOWN_COUNTS[@]}"
done
say "provider teardowns returned at $(date -u +%T)"
st Z -c1 --mem=300M bash "$B/teardown.sh" oz --workdir "$(wlog "$(svc_log Z)")" < /dev/null > "$L/teardown-zone.log" 2>&1
rec logscan "$E/Z/teardown.json" "$L/teardown-zone.log" --rc "$?" "${TEARDOWN_COUNTS[@]}"
say "zone teardown returned at $(date -u +%T)"
end=$((SECONDS + 300))
while (( SECONDS < end )); do
  left=$(squeue -s -j "$HPC_JOB" -h -o '%j' | grep -c -v -E '^(batch|extern)$' || true)
  [[ $left == 0 ]] && break
  sleep 5
done
for role in Z P1 P2 P3; do   # a service step's status is written when its srun returns: a moment after its job step ends
  end=$((SECONDS + 60))
  while [[ ! -s $E/$role/service-step.rc.json ]] && (( SECONDS < end )); do sleep 2; done
  rec logscan "$E/$role/service-end.json" "$(svc_log "$role")" "${END_COUNTS[@]}"
  say "$role: $(grep -E '^ok:|ERROR|SIGTERM' "$L/teardown-$([[ $role == Z ]] && echo zone || echo "prov${role#P}").log" | tr '\n' ' ' | cut -c1-260)"
  say "$role: $(tail -n 2 "$(svc_log "$role")" | tr '\n' ' ' | cut -c1-260)"
done
out=$(squeue -s -j "$HPC_JOB" -h -o '%j')
rc=$?
printf '%s\n' "$out" | rec lines "$E/teardown/steps-left.json" steps_left "$rc"
for role in Z P1 P2 P3 S; do   # read-only: it changes nothing on the node
  st "$role" -c1 --mem=200M python3.11 -I -B "$RUN/nodecheck.py" "$role" < /dev/null > "$E/teardown/nodecheck-$role.json"
  say "node check $role: exit status $?"
done
sacct -j "$HPC_JOB" -n -P -o JobID,JobName,State,Elapsed,MaxRSS | cut -c1-120
