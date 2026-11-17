#!/bin/bash
# cell.sh (login node): the agent's cell again, with a fresh read-only gate token from the zone, piped into the sandbox
# step on S. The space, the root, the file and its SHA-256 come from roundtrip/write.out.json, the round trip's evidence.
# Usage: E=<evidence dir> bash cell.sh
source "$(dirname "$0")/common.sh"
ZW=$(wlog "$(svc_log Z)")
[[ -n $ZW ]] || die "no zone work directory in its log"
cd /
say "=== agent cell on S at $(date -u +%T)"
st Z -c1 --mem=300M python3.11 -I -B "$RUN/fed_zone.py" --workdir "$ZW" gate-token < /dev/null 2> "$L/gate-token.err" \
  | python3 -I -B "$RUN/pick.py" gate | cell_step
