#!/bin/bash
# providers-up.sh (login node): three providers as detached steps on P1-P3; a registration token minted in a zone step
# and piped into each provider's delivery step (1 GB: a live run's finding); wait for each provider's verdict, then for
# three online in the zone. Evidence, per provider role: up.json, cb-guard-*.json, service-step.rc.json (as zone-up.sh),
# registration.json (the mint and delivery steps' exit statuses); providers/online.out.json and .rc.json (the zone's
# listing and its exit status). Tokens move only through the pipe: none reaches a file, a command line or a log.
# Usage: E=<evidence dir> bash providers-up.sh
source "$(dirname "$0")/common.sh"
ZLOG=$(svc_log Z)
ZW=$(wlog "$ZLOG")
ZF=$(fqdn_of "$ZLOG")
[[ -n $ZW && -n $ZF ]] || die "no zone work directory or FQDN in its log (run zone-up.sh first)"
say "zone fqdn found; work directory found"
ev providers
cd /
for n in 1 2 3; do
  role=P$n
  ev "$role"
  rm -f -- "$(svc_log "$role")"
  peers=()
  for other in P1 P2 P3; do [[ $other == "$role" ]] || peers+=("$(addr_of "$other")"); done
  detached "$E/$role/service-step.rc.json" service_step \
    srun --jobid="$HPC_JOB" --overlap --export=PATH --chdir=/ -w "$(node_of "$role")" -N1 -n1 -c"$SVC_CPUS" --mem="$SVC_MEM" --output="$(svc_log "$role")" \
    bash "$B/provider.sbatch" "$B" "$SH/oneprovider.tar" "prov$n" "$ZF" "$ADMIN_MAIL" "$ADDR_Z" "${peers[@]}"
done
say "provider steps launched $(date -u +%T)"
for n in 1 2 3; do
  role=P$n
  log=$(svc_log "$role")
  wait_log "$log" 'waiting for the registration token|hpcop: ERROR' 420
  PW=$(wlog "$log")
  [[ -n $PW ]] || { say "$role: no work directory"; grep -E "ERROR|FAIL" "$log" | head -5; continue; }
  st Z -c1 --mem=200M bash "$B/mint-registration-token.sh" "prov$n" --workdir "$ZW" < /dev/null 2> "$L/mint-$n.err" \
    | st "$role" -c1 --mem=1G bash "$B/deliver-token.sh" --workdir "$PW" 2> "$L/deliver-$n.err"
  pipe=("${PIPESTATUS[@]}")
  rec kv "$E/$role/registration.json" "mint_rc=int:${pipe[0]}" "deliver_rc=int:${pipe[1]}"
  say "$role: mint and delivery exit statuses ${pipe[*]} at $(date -u +%T)"
done
for n in 1 2 3; do
  role=P$n
  log=$(svc_log "$role")
  wait_log "$log" 'hpcop: OK provider|FINISH FAILED|hpcop: ERROR' 600
  say "== $role at $(date -u +%T)"
  grep -E "Couchbase guard|administrator set|ok: all default checks|every listener|FAIL|ERROR|OK provider|info: ours|shipped|tarball|posix storage" "$log" \
    | sed -E 's/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/<ip>/g' | cut -c1-220
  record_up "$role"
  cat "$E/$role/up.json"
done
st Z -c1 --mem=300M python3.11 -I "$RUN/fed_zone.py" --workdir "$ZW" --evidence "$E/providers" online 3 --wait 120 < /dev/null \
  > "$E/providers/online.out.json"
rec rc "$E/providers/online.rc.json" online "${PIPESTATUS[0]}"
squeue -s -j "$HPC_JOB" -h -o "%i %j %M"
