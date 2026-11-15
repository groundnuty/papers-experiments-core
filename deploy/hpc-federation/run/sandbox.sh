#!/bin/bash
# sandbox.sh: the agent's cell on its own node S, in one step of the allocation. S hosts no Onedata service.
#   stdin: the read-only gate token, one line (pick.py gate). It goes only into 0600 files in a private directory: the
#   gate's own copy (removed once the gate is ready) and the scan's copy, kept until the cell has exited and the logs
#   are scanned. This script is the only place the gate token exists in a file.
# Usage: sandbox.sh <run-dir> <bundle-dir> <gate-dir> <oneprovider-docker.tar> <evidence-dir> <space-name> <space-id> \
#          <root-id> <file-id> <sha256> <zone-fqdn> <p1-fqdn> <p2-fqdn> <p3-fqdn>
# 1. a SIF of the pinned provider image from its archive (the cell's image; the pristine image, no shim);
# 2. the egress shim, built inside that SIF with the image's gcc; the collector, outside the cell, started first;
# 3. a run CA and a leaf for P3's name; the image's Onedata test CA as the upstream CA;
# 4. the core's data gate (gate.py, stdlib only, on the node's python3.11) on 127.0.0.1:<free port>, holding the token;
# 5. a canary: one exec in the cell that connects to the gate and nothing else, so the egress log opens with its connect
#    (a log that does not is unknown: the shim may never have loaded), then agent.py in the cell: setuid Apptainer
#    --containall --cleanenv --no-home, /cell bound read-only, the shim preloaded; both execs use ONE array of binds;
# 6. the gate and the collector stopped, every log copied into <evidence-dir>/sandbox/, and each scanned for the token:
#    grep -c -F -f <token file> <file>, one file at a time, its exit status and count saved; the token is never printed.
# The step's exit status says it ran to the end. Whether the cell, the scans and the egress log passed is the summary's
# to say (evidence.py), from the files this writes.
set -euo pipefail
umask 077
RUN=$1 BUNDLE=$2 GATE=$3 ARCH=$4 E=$5 SNAME=$6 SID=$7 ROOT=$8 FID=$9 SHA=${10} ZF=${11}
shift 11
(( $# == 3 )) || { echo "sandbox: ERROR: wants the three providers' FQDNs after the zone's" >&2; exit 2; }
PF=$3   # the gate's upstream: P3, which did not write the file, so its blocks came over the providers' link
rec() { python3.11 -I "$RUN/record.py" "$@"; }
for f in gate.py gatepolicy.py gateboundary.py; do
  [[ -f $GATE/$f ]] || { echo "sandbox: ERROR: no $f in the gate directory" >&2; exit 2; }
done
for f in egresslog.c egress-collector.py; do
  [[ -f $BUNDLE/$f ]] || { echo "sandbox: ERROR: no $f in the bundle" >&2; exit 2; }
done
[[ -d $E/sandbox && -O $E/sandbox ]] || { echo "sandbox: ERROR: no evidence directory for this step" >&2; exit 2; }
base=${TMPDIR:-/tmp}; [[ -L $base ]] && base=/tmp
D=$(mktemp -d "$base/hpcop-sbx.XXXXXX"); chmod 700 -- "$D"
[[ -n $D && $D == */hpcop-sbx.?????? ]] || { echo "sandbox: ERROR: no private directory" >&2; exit 2; }
gpid='' cpid=''
cleanup() {
  [[ -n $gpid ]] && kill "$gpid" 2> /dev/null
  [[ -n $cpid ]] && kill "$cpid" 2> /dev/null
  wait 2> /dev/null
  chmod -R u+rwX -- "${D:?}" 2> /dev/null || true
  rm -rf -- "${D:?}"
  echo "sandbox: private directory removed: $([[ -e $D ]] && echo NO || echo yes)" >&2
}
trap cleanup EXIT
mkdir -m 700 "$D/gate" "$D/scan" "$D/tls" "$D/cell" "$D/cache" "$D/tmp" "$D/shim-build" "$D/shim"
IFS= read -r tok; [[ -n $tok ]] || { echo "sandbox: ERROR: no gate token on stdin" >&2; exit 1; }
printf '%s\n' "$tok" > "$D/gate/token"; printf '%s\n' "$tok" > "$D/scan/token"; tok=''
chmod 600 "$D/gate/token" "$D/scan/token"
# An empty pattern file matches nothing, and a blank line in it matches everything: either would be a false pass.
[[ -s $D/scan/token && $(grep -c . "$D/scan/token") == 1 && $(wc -l < "$D/scan/token") == 1 ]] \
  || { echo "sandbox: ERROR: the token file is not exactly one non-blank line" >&2; exit 1; }
echo "sandbox: start $(date -u +%T)" >&2

t0=$SECONDS
want=$(awk '{print $1; exit}' "$ARCH.sha256"); got=$(sha256sum < "$ARCH" | cut -d' ' -f1)
[[ $got == "$want" ]] || { echo "sandbox: ERROR: archive sha256 mismatch" >&2; exit 1; }
APPTAINER_CACHEDIR=$D/cache APPTAINER_TMPDIR=$D/tmp apptainer -q build "$D/cell.sif" "docker-archive:$ARCH" < /dev/null
rm -rf -- "${D:?}/cache" "${D:?}/tmp"
echo "sandbox: SIF built in $((SECONDS - t0)) s ($(du -h "$D/cell.sif" | cut -f1))" >&2

# The egress shim, built in the cell's own image with its gcc: it compiles in the socket path /hpcop-egress/sock.
cp "$BUNDLE/egresslog.c" "$D/shim-build/egresslog.c"
build=0
apptainer exec --containall --no-home -B "$D/shim-build:/build" --pwd /build "$D/cell.sif" \
  gcc -shared -fPIC -O2 -fno-stack-protector -Wall -Wextra -Werror -o egresslog.so egresslog.c < /dev/null || build=$?
rec rc "$E/sandbox/shim-build.rc.json" shim_build "$build"
[[ $build == 0 ]] || { echo "sandbox: ERROR: the egress shim did not build (exit status $build)" >&2; exit 1; }
cp "$D/shim-build/egresslog.so" "$D/shim/egresslog.so"; chmod 755 "$D/shim"; chmod 644 "$D/shim/egresslog.so"
printf '%s\n' '/hpcop-shim/egresslog.so' > "$D/preload"; chmod 644 "$D/preload"
# The collector, outside the cell: it gets a fresh empty 0700 socket directory (nothing else in it) and a log file that
# does not exist, outside every path the cell sees.
nonce=$(rec nonce)
EGRESS=$(mktemp -d "$D/egress.XXXXXX"); chmod 700 -- "$EGRESS"
python3.11 -I "$BUNDLE/egress-collector.py" "$EGRESS" "$D/egress.jsonl" "$nonce" > "$D/collector.stdout" 2> "$D/collector.stderr" &
cpid=$!
for _ in $(seq 100); do
  grep -q '^egress collector ready$' "$D/collector.stdout" 2> /dev/null && break
  kill -0 "$cpid" 2> /dev/null || break
  sleep 0.1
done
grep -q '^egress collector ready$' "$D/collector.stdout" \
  || { echo "sandbox: ERROR: the egress collector did not start: $(tail -c 400 "$D/collector.stderr")" >&2; exit 1; }
echo "sandbox: egress collector ready" >&2

apptainer exec --containall --no-home --pwd / "$D/cell.sif" cat /volumes/persistence_backup/etc/op_panel/OnedataTestWebServerCa.pem \
  > "$D/gate/upstream-ca.pem" < /dev/null
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$D/gate/ca.key" -out "$D/cell/ca.pem" -days 1 -subj "/CN=hpcop run CA" 2> /dev/null
lc=${PF,,}
openssl req -newkey rsa:2048 -nodes -keyout "$D/tls/$lc.key" -out "$D/gate/leaf.csr" -subj "/CN=$lc" 2> /dev/null
printf 'subjectAltName=DNS:%s\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' "$lc" > "$D/gate/ext"
openssl x509 -req -in "$D/gate/leaf.csr" -CA "$D/cell/ca.pem" -CAkey "$D/gate/ca.key" -CAserial "$D/gate/ca.srl" -CAcreateserial \
  -out "$D/tls/$lc.pem" -days 1 -extfile "$D/gate/ext" 2> /dev/null
rm -f -- "${D:?}/gate/ca.key"
port=$(python3.11 -I -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
python3.11 -I - "$D/gate/config.json" "$lc" "$PF" "$D" "$port" <<'PY'
import json, secrets, sys
path, lc, pf, d, port = sys.argv[1:]
json.dump({"run_key": secrets.token_hex(8), "verify_ssl": True, "upstream_ca_file": d + "/gate/upstream-ca.pem",
           "tls_dir": d + "/tls", "log": d + "/gate/requests.jsonl", "token_file": d + "/gate/token",
           "listen_host": "127.0.0.1", "listen_port": int(port),
           "hosts": {lc: {"role": "oneprovider", "addr": pf, "port": 8443, "tls": True}}}, open(path, "w"))
PY
python3.11 -I "$GATE/gate.py" "$D/gate/config.json" > "$D/gate/stdout" 2> "$D/gate/stderr" &
gpid=$!
for _ in $(seq 100); do
  grep -q 'gate ready' "$D/gate/stdout" 2> /dev/null && break
  kill -0 "$gpid" 2> /dev/null || break
  sleep 0.1
done
grep -q 'gate ready' "$D/gate/stdout" || { echo "sandbox: ERROR: the gate did not start: $(tail -c 400 "$D/gate/stderr")" >&2; exit 1; }
rm -f -- "${D:?}/gate/token"   # the gate keeps its token in memory; the scan's copy stays until the cell has exited
echo "sandbox: gate ready on 127.0.0.1:$port (the gate's token file removed; held in the gate's memory)" >&2
total=$(ss -Hltn | wc -l); ours=$(ss -Hltnp 2> /dev/null | grep -c 'pid=' || true)
rec kv "$E/sandbox/node.json" "tcp_listeners_total=int:$total" "tcp_listeners_ours=int:$ours"

cp "$RUN/agent.py" "$D/cell/agent.py"
printf '%s\n' "$PF" "$ZF" "$@" > "$D/cell/hosts.txt"
chmod 755 "$D/cell"; chmod 644 "$D/cell/"*
# Every bind of the cell, in one array that the canary and the agent both use, so they run under identical binds.
BINDS=(-B "$D/cell:/cell:ro" -B "$EGRESS:/hpcop-egress:ro" -B "$D/shim:/hpcop-shim:ro" -B "$D/preload:/etc/ld.so.preload:ro")
# The canary: the first connect through the shim, to the gate and to nothing else (the collector's start and this are
# the only steps between: nothing else in the cell runs before it). The egress log must open with its record.
canary=0
apptainer exec --containall --cleanenv --no-home --pwd / "${BINDS[@]}" "$D/cell.sif" python3 -I -c 'import json, socket, sys
try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), 10).close()
    result = "open"
except OSError as e:
    result = "error: %s" % type(e).__name__
print(json.dumps({"connect": result}))' "$port" < /dev/null > "$D/canary.out" 2> "$D/canary.err" || canary=$?
rec canary "$E/sandbox/canary.json" 127.0.0.1 "$port" "$canary" "$D/canary.out"
echo "sandbox: the egress canary (a connect to the gate) exited $canary" >&2
t1=$SECONDS
agent=0
apptainer exec --containall --cleanenv --no-home --pwd / "${BINDS[@]}" "$D/cell.sif" \
  python3 /cell/agent.py "$port" "$lc" "$SNAME" "$SID" "$ROOT" "$FID" "$SHA" "$ZF" "$@" \
  < /dev/null > "$D/agent.json" 2> "$D/agent.err" || agent=$?
echo "sandbox: the cell ran $((SECONDS - t1)) s, exit status $agent" >&2
rec rc "$E/sandbox/agent.rc.json" agent "$agent"

# After the cell: the gate stops (its log gets its teardown line), then the collector (its log gets its end line).
gate=0
kill -TERM "$gpid"; wait "$gpid" 2> /dev/null || gate=$?; gpid=''
rec rc "$E/sandbox/gate-end.json" gate "$gate"
coll=0
kill -TERM "$cpid"; wait "$cpid" 2> /dev/null || coll=$?; cpid=''
ready=$(grep -c '^egress collector ready$' "$D/collector.stdout" || true)
rec kv "$E/sandbox/egress-collector.json" "nonce=str:$nonce" "ready_lines=int:$ready" "rc=int:$coll"

# The evidence files, then the scans of exactly those files.
cp "$D/gate/requests.jsonl" "$E/sandbox/gate-requests.jsonl"
cp "$D/gate/stdout" "$E/sandbox/gate-stdout.txt"; cp "$D/gate/stderr" "$E/sandbox/gate-stderr.txt"
cp "$D/agent.json" "$E/sandbox/agent.json"; cp "$D/agent.err" "$E/sandbox/agent.err"
cp "$D/egress.jsonl" "$E/sandbox/egress.jsonl"
[[ -s $D/scan/token ]] || { echo "sandbox: ERROR: the token file is empty at the scan" >&2; exit 1; }
scan() {  # <name> <file>: grep -c -F -f token file; exit status 1 is 0 matches, 2 an error (the summary fails it)
  local count='' rc=0
  count=$(grep -c -F -f "$D/scan/token" -- "$2") || rc=$?
  rec scan "$E/sandbox/token-scan-$1.json" "$1" "$D/scan/token" "$2" "$rc" "$count"
  echo "sandbox: token scan $1: grep exit status $rc, count ${count:-none}" >&2
}
scan gate-requests "$E/sandbox/gate-requests.jsonl"
scan gate-stdout "$E/sandbox/gate-stdout.txt"
scan gate-stderr "$E/sandbox/gate-stderr.txt"
scan agent-stdout "$E/sandbox/agent.json"
scan agent-stderr "$E/sandbox/agent.err"
scan egress-log "$E/sandbox/egress.jsonl"
rm -f -- "${D:?}/scan/token"
echo "sandbox: gate log lines: $(wc -l < "$E/sandbox/gate-requests.jsonl"); egress log lines: $(wc -l < "$E/sandbox/egress.jsonl")" >&2
