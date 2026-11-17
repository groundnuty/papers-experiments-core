#!/bin/bash
# roundtrip.sh (login node): one space, supported by P1-P3; 1 MiB written through P1's REST; read back through P2 and P3
# (their blocks come from P1 over the providers' link); then the agent's cell on S, through the gate to P3.
# All tokens stay in this shell's memory and move only through pipes (pick.py): none reaches a file of the login node,
# a command line or a log. Evidence (roundtrip/): prepare.json, support-P<n>.out.json and .rc.json, write.out.json and
# .rc.json, read-P<n>.out.json and .rc.json (the SHA-256 read beside the expected one), http-*.jsonl (each HTTP call's
# status and body size and SHA-256); then sandbox.sh's (sandbox/).
# Usage: E=<evidence dir> bash roundtrip.sh
source "$(dirname "$0")/common.sh"
ZW=$(wlog "$(svc_log Z)")
declare -a PW
for n in 1 2 3; do PW[n]=$(wlog "$(svc_log "P$n")"); done
[[ -n $ZW && -n ${PW[1]} && -n ${PW[2]} && -n ${PW[3]} ]] || die "a service's work directory is not in its log"
ev roundtrip
cd /
t0=$(date +%s)
PREP=$(st Z -c1 --mem=500M python3.11 -I -B "$RUN/fed_zone.py" --workdir "$ZW" --evidence "$E/roundtrip" prepare-n "$SPACE_NAME" 3 \
  < /dev/null 2> "$L/rt-prepare.err")
rc=$?
rec kv "$E/roundtrip/prepare.json" "rc=int:$rc" "stdout_bytes=int:${#PREP}"
say "prepare: exit status $rc; $(grep -v -i token "$L/rt-prepare.err" | tr '\n' ' ' | cut -c1-200)"
[[ $rc == 0 && -n $PREP ]] || exit 1
for n in 1 2 3; do
  printf '%s\n' "$PREP" | python3 -I -B "$RUN/pick.py" support "$((n - 1))" \
    | st "P$n" -c1 --mem=500M python3.11 -I -B "$RUN/fed_provider.py" --workdir "${PW[n]}" --bundle "$B" --evidence "$E/roundtrip" support \
      > "$E/roundtrip/support-P$n.out.json" 2> "$L/rt-support-$n.err"
  pipe=("${PIPESTATUS[@]}")
  rec rc "$E/roundtrip/support-P$n.rc.json" support "${pipe[2]}"
  say "P$n support: exit status ${pipe[2]} at $(date -u +%T)"
done
printf '%s\n' "$PREP" | python3 -I -B "$RUN/pick.py" access \
  | st P1 -c1 --mem=500M python3.11 -I -B "$RUN/fed_provider.py" --workdir "${PW[1]}" --evidence "$E/roundtrip" write \
    > "$E/roundtrip/write.out.json" 2> "$L/rt-write.err"
pipe=("${PIPESTATUS[@]}")
rec rc "$E/roundtrip/write.rc.json" write "${pipe[2]}"
say "write on P1: exit status ${pipe[2]}"
FID=$(rec field "$E/roundtrip/write.out.json" file_id) && SHA=$(rec field "$E/roundtrip/write.out.json" sha256) \
  || { say "no usable write evidence"; exit 1; }
for n in 2 3; do
  printf '%s\n' "$PREP" | python3 -I -B "$RUN/pick.py" access \
    | st "P$n" -c1 --mem=500M python3.11 -I -B "$RUN/fed_provider.py" --workdir "${PW[n]}" --evidence "$E/roundtrip" read "$FID" "$SHA" \
      > "$E/roundtrip/read-P$n.out.json" 2> "$L/rt-read-$n.err"
  pipe=("${PIPESTATUS[@]}")
  rec rc "$E/roundtrip/read-P$n.rc.json" read "${pipe[2]}"
  say "read on P$n: exit status ${pipe[2]}"
done
say "round trip done in $(($(date +%s) - t0)) s"
say "=== agent cell on S at $(date -u +%T)"
printf '%s\n' "$PREP" | python3 -I -B "$RUN/pick.py" gate | cell_step
PREP=''
