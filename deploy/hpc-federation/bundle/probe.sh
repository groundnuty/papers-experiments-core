#!/bin/bash
# probe.sh: read-only checks of what this bundle needs on a compute node, beyond the site probe already
# done (hpc-probe.md). Run it in a step of a throwaway holder job, with the same flags the real steps
# use (--export=PATH), so it sees the same environment. It prints key=value lines and changes nothing,
# except a private mktemp directory it removes at once and, with --listen, one short-lived listener.
# Usage: probe.sh                         the node checks
#        probe.sh --listen PORT [SECONDS] accept one TCP connection on PORT, then exit (default 120 s)
#        probe.sh --dial HOST PORT        try one TCP connection to HOST:PORT (cross-node reachability)
source "$(dirname "$0")/lib.sh"
set +e   # report every check, do not stop at the first missing piece
kv() { printf '%s=%s\n' "$1" "$2"; }

case ${1:-} in
  --listen)
    exec "$HPCOP_PY" -I -B -c 'import socket, sys
s = socket.socket(); s.settimeout(float(sys.argv[2])); s.bind(("0.0.0.0", int(sys.argv[1]))); s.listen(1)
try:
    c, a = s.accept(); print("accepted=%s" % a[0]); c.close()
except socket.timeout:
    print("accepted=none")' "${2:?PORT}" "${3:-120}" ;;
  --dial)
    exec "$HPCOP_PY" -I -B -c 'import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 10).close(); print("dial=ok")
except OSError as e:
    print("dial=failed %s" % e)' "${2:?HOST}" "${3:?PORT}" ;;
  "") ;;
  *) die "unknown option '$1'" ;;
esac

kv tmpdir "${TMPDIR:-unset}"
kv tmpdir_link "${HPCOP_TMPDIR_LINK:-none}"   # the job's TMPDIR when it was a link (lib.sh then uses /tmp)
kv home "${HOME:-unset}"
( check_tmpdir ) 2>&1 | sed 's/^/tmpdir_chain_problem=/'
kv tmpdir_chain "$(d=${TMPDIR:-/}; while [[ $d != / ]]; do stat -c '%n:%U:%a' -- "$d"; d=$(dirname -- "$d"); done | tr '\n' ' ')"
if w=$(make_workdir probe 2>/dev/null); then kv private_workdir "ok $(mode_of "$w")"; rm -f -- "$w/owner.pid"; rmdir -- "$w"; else kv private_workdir FAILED; fi
kv fqdn "$(hostname -f)"
kv fqdn_addresses "$(getent ahostsv4 "$(hostname -f)" | awk '{print $1}' | sort -u | tr '\n' ' ')"
kv unprivileged_port_start "$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start)"
kv proc_hidepid "$(findmnt -n -o OPTIONS --mountpoint /proc | tail -n 1 | tr ',' '\n' | grep -E '^(hidepid|gid)=' | tr '\n' ' ' || echo none)"
kv apptainer "$(apptainer --version 2>&1)"
kv python311 "$("$HPCOP_PY" -I -B --version 2>&1)"
kv pidfd "$("$HPCOP_PY" -I -B "$HPCOP_BUNDLE/supervise.py" check 2>&1 | paste -sd ' ' -)"   # the mode the service's stop will use: pidfd, or the start-tick fallback
# Live run 4: a node with IPv6 disabled has no ::1; the shim then keeps memcached and moxi on 127.0.0.1 only.
kv ipv6_loopback "$("$HPCOP_PY" -I -B -c 'import socket
try:
    socket.socket(socket.AF_INET6).bind(("::1", 0)); print("usable")
except OSError as e:
    print("none (%s)" % e.strerror)' 2>&1)"
for t in ss findmnt curl od sha256sum tar getent; do kv "tool_$t" "$(command -v "$t" || echo MISSING)"; done
kv registry_http "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 https://docker.onedata.org/v2/ 2>&1)"
# A listing that could not be read is an error, never "free" (a missing ss once read as no listener at all).
if epmd=$(ss -ltnH 'sport = :4369'); then
  kv epmd_4369 "$([[ -n $epmd ]] && echo listening || echo free)"
else
  kv epmd_4369 "ERROR (ss failed or is missing)"
fi
used=$(used_ports) || { kv ports_busy "ERROR (this node's listeners could not be listed)"; exit 1; }
taken=''
for p in $HPCOP_PUBLIC_PORTS $(dialled_ports oz); do if grep -qx -- "$p" <<<"$used"; then taken+="$p "; fi; done
kv ports_busy "$taken"
for r in $HPCOP_DIST_RANGES $HPCOP_EPMD_RANGE; do kv "free_in_$r" "$(count_free "$r" "$used")"; done
kv exclusive_job "$(scontrol show job "${SLURM_JOB_ID:-0}" 2>/dev/null | grep -oE 'OverSubscribe=[A-Z]+' || echo unknown)"
kv node_cpus "$(scontrol show node "$(hostname -s)" 2>/dev/null | grep -oE 'CPUTot=[0-9]+' || echo unknown)"
kv mem_limit "$(cat "/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.max" 2>/dev/null || echo unknown)"
