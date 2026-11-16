# shellcheck shell=bash
# shellcheck disable=SC2034  # the lists are read by the scripts and tests that source this file
# ports.sh: the bundle's port policy, sourced by lib.sh. Source it; never run it.
#
# The node's network is shared and there is no network namespace, so the shim's rule is default-deny: every
# socket a service binds is on loopback, TCP or UDP, any port, except the public ones below (portshim.c
# carries the same public list, compiled in; a test compares the two). The node itself is shared with other
# users too (--exclusive is optional), so the shim also refuses a loopback connection to or from a socket of
# another uid, on every port but the public ones (portshim-owner.h).
# Only two ports stay on the node's interfaces, as Onedata's hardened setup lists them
# (docs/admin-guide/oneprovider/prerequisites.md, "Public IP and ports"):
#   8443  from 443: REST and GUI; provider-to-zone (oz_rest_port, graph_sync_port 443) and
#         provider-to-provider HTTPS
#   6665  rtransfer: provider-to-provider data (the provider only)

HPCOP_PUBLIC_PORTS='8443 6665'
# The programs whose loopback listeners the owner check covers: each takes connections through the libc
# accept() the shim interposes (both beam.smp, both epmd, memcached and moxi; README, Exposure model, item 2).
# The live check fails on a loopback listener of ours held by any other program, except the known residual
# below: goxdcr (Go: its own system calls) on 9998.
HPCOP_OWNER_CHECKED_PROGRAMS='beam.smp epmd memcached moxi'
HPCOP_UNCHECKED_LISTENERS='goxdcr:9998'
HPCOP_EPMD_RANGE='21300-21999'               # this job's epmd port (ERL_EPMD_PORT) is drawn from here
HPCOP_DIST_RANGES='9100-9139 21100-21299'    # Erlang distribution: Onedata's VMs, Couchbase's VMs
HPCOP_DIST_MIN_FREE=8                        # each VM takes the first free port of its range
HPCOP_ERLANG_ENV=/etc/hpcop-erlang-env       # in the rootfs; sourced by every patched VM start path

public_ports() { if [[ $1 == op ]]; then echo '8443 6665'; else echo 8443; fi; }   # <comp>

dialled_ports() {  # <comp> [epmd port]: private ports that clients reach by number, not through epmd
  # Not 4369: the shim sends every 4369 of ours to the job's epmd port, and on a shared node another
  # user's epmd on 4369 is normal.
  local p='8080 8091 8092 9443 9998 9999 11209 11210 11211'
  if [[ $1 == oz ]]; then p+=' 8053'; fi
  printf '%s %s\n' "$p" "${2:-}"
}

port_in() {  # <port> <ports and lo-hi ranges>
  local r
  for r in $2; do
    if [[ $r == *-* ]]; then
      if (( $1 >= ${r%-*} && $1 <= ${r#*-} )); then return 0; fi
    elif (( $1 == r )); then return 0; fi
  done
  return 1
}

is_loopback_addr() { [[ $1 == 127.* || $1 == ::1 || $1 == ::ffff:127.* ]]; }

parse_listeners() {  # stdin: `ss -Hlntuep` lines; stdout: "<proto> <address> <port> <uid> <programs>"
  # (uid 0 if none shown; programs: the comma-separated names in users:((...)), "-" if no process is shown:
  # another uid's, or one this user cannot see)
  awk '{
    loc = $5; uid = 0; progs = ""
    for (i = 6; i <= NF; i++) {
      if ($i ~ /^uid:[0-9]+$/) uid = substr($i, 5)
      if ($i ~ /^users:\(/) {
        u = $i
        while (match(u, /\("[^"]*",/)) { progs = progs (progs == "" ? "" : ",") substr(u, RSTART + 2, RLENGTH - 4); u = substr(u, RSTART + RLENGTH) }
      }
    }
    n = match(loc, /:[0-9]+$/); if (!n) next
    addr = substr(loc, 1, n - 1); port = substr(loc, n + 1)
    gsub(/^\[|\]$/, "", addr); sub(/%.*$/, "", addr)
    print $1, addr, port, uid, (progs == "" ? "-" : progs)
  }'
}

listeners() {  # every listening TCP and bound UDP socket on this node, as parse_listeners prints them. A missing
  # or failing ss dies, naming it: a listing that could not be read is never one with no listener (live: a check
  # whose ss was not on PATH counted no listener, a false pass). Call it as `out=$(listeners) || ...`, never in a
  # pipeline or a process substitution, where its status is lost.
  local out
  need_tools ss
  out=$(ss -Hlntuep) || die "ss failed: cannot list this node's listeners"
  parse_listeners <<<"$out"
}

used_ports() {  # every listener's port, one per line; status 1 when the listeners could not be listed
  local out
  out=$(listeners) || return 1
  awk '{ print $3 }' <<<"$out" | sort -nu
}

count_free() {  # <lo-hi> <used ports, one per line>
  local lo=${1%-*} hi=${1#*-} n
  n=$(awk -v lo="$lo" -v hi="$hi" '$1 >= lo && $1 <= hi { c++ } END { print c + 0 }' <<<"$2")
  echo $(( hi - lo + 1 - n ))
}

ports_free() {  # <comp> [epmd port]: before a start, the ports this service binds by number must be free
  local used p r free busy=()
  used=$(used_ports) || die "this node's listeners could not be listed: the port check fails closed"
  for p in $(public_ports "$1") $(dialled_ports "$1" "${2:-}"); do
    if grep -qx -- "$p" <<<"$used"; then busy+=("$p"); fi
  done
  for r in $HPCOP_DIST_RANGES; do
    free=$(count_free "$r" "$used")
    (( free >= HPCOP_DIST_MIN_FREE )) || busy+=("$r (only $free free)")
  done
  (( ${#busy[@]} == 0 )) || die "ports already in use on $(hostname -s): ${busy[*]}; pick another node"
  ok "ports free: $(public_ports "$1") $(dialled_ports "$1" "${2:-}"), and enough of $HPCOP_DIST_RANGES"
}

pick_epmd_port() {  # print a free port of HPCOP_EPMD_RANGE, starting at a random one
  local lo=${HPCOP_EPMD_RANGE%-*} hi=${HPCOP_EPMD_RANGE#*-} used n start k p
  # `$(...)` clears -e, so a caller's EPMD=$(pick_epmd_port) would go on with no listing: fail here, explicitly
  used=$(used_ports) || die "this node's listeners could not be listed: no epmd port is picked blind"
  n=$(( hi - lo + 1 )); start=$(( $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % n ))
  for (( k = 0; k < n; k++ )); do
    p=$(( lo + (start + k) % n ))
    if ! grep -qx -- "$p" <<<"$used"; then echo "$p"; return; fi
  done
  die "no free port in $HPCOP_EPMD_RANGE for this job's epmd"
}

owner_checked_holder() {  # <programs, comma-separated, or ->: the owner check covers a loopback listener they hold
  local p
  [[ $1 != - ]] || return 1   # no process shown: whether its accept() passes through the shim cannot be told
  for p in ${1//,/ }; do
    [[ " $HPCOP_OWNER_CHECKED_PROGRAMS " == *" $p "* ]] || return 1
  done
}

check_listeners() {  # <comp> <uid> <epmd port>; stdin: parse_listeners lines. After the start (default-deny):
  # every listener of ours is on loopback unless it is a public port; on loopback, every TCP listener of ours
  # is held by a program whose accept() the shim's owner check sees (HPCOP_OWNER_CHECKED_PROGRAMS), except
  # the known residual HPCOP_UNCHECKED_LISTENERS (goxdcr's 9998); our only UDP socket is the zone's DNS on
  # 127.0.0.1:8053 (UDP has no accept to check); a port our clients dial by number belongs to us; our epmd is
  # on this job's port, not 4369; and the public 8443 is there. Every listener of another uid is reported as
  # "info:", so all of them are seen in the log. Prints a FAIL line per problem; returns 1 on any.
  local comp=$1 me=$2 epmd=$3 dialled public proto addr port uid progs at bad=0 seen=0 seen_epmd=0
  dialled=$(dialled_ports "$comp" "$epmd"); public=$(public_ports "$comp")
  while read -r proto addr port uid progs; do
    if [[ $addr == *:* ]]; then at="[$addr]:$port"; else at="$addr:$port"; fi
    if [[ $uid != "$me" ]]; then
      if port_in "$port" "$dialled"; then
        printf 'FAIL: %s port %s, which this service dials by number, belongs to uid %s\n' "$proto" "$port" "$uid" >&2; bad=1
      fi
      if is_loopback_addr "$addr"; then printf 'info: not ours, on loopback: %s %s (uid %s)\n' "$proto" "$at" "$uid" >&2
      else printf 'info: not ours, off loopback: %s %s (uid %s)\n' "$proto" "$at" "$uid" >&2; fi
      continue
    fi
    if [[ $proto == tcp && $port == 8443 ]]; then seen=1; fi
    if [[ $port == "$epmd" ]]; then seen_epmd=1; fi
    if [[ $port == 4369 ]]; then
      printf 'FAIL: an epmd of this service is on the well-known 4369, not on this job port %s\n' "$epmd" >&2; bad=1
    fi
    if [[ $proto == udp ]]; then
      if [[ $comp == oz && $port == 8053 ]] && is_loopback_addr "$addr"; then continue; fi
      printf "FAIL: udp port %s of this service (%s, in %s): the only UDP socket allowed is the zone's DNS on 127.0.0.1:8053\n" "$port" "$at" "${progs:--}" >&2
      bad=1; continue
    fi
    if ! is_loopback_addr "$addr"; then
      if port_in "$port" "$public"; then continue; fi
      printf 'FAIL: %s port %s of this service listens on %s, not on loopback (in %s)\n' "$proto" "$port" "$addr" "${progs:--}" >&2
      bad=1; continue
    fi
    if owner_checked_holder "${progs:--}"; then continue; fi
    if [[ " $HPCOP_UNCHECKED_LISTENERS " == *" ${progs:--}:$port "* ]]; then
      printf 'info: ours, on loopback without the owner check (a known residual): %s %s in %s\n' "$proto" "$at" "$progs" >&2
      continue
    fi
    printf 'FAIL: tcp port %s of this service listens on loopback (%s) in %s, which is not a program whose accept() the owner check sees (%s)\n' \
      "$port" "$at" "${progs:--}" "$HPCOP_OWNER_CHECKED_PROGRAMS" >&2
    bad=1
  done
  if (( !seen )); then printf 'FAIL: no listener of this service on 8443\n' >&2; bad=1; fi
  if (( !seen_epmd )); then printf 'FAIL: no epmd of this service on this job port %s\n' "$epmd" >&2; bad=1; fi
  return "$bad"
}

live_listeners_ok() {  # <comp> <uid> <epmd port>: check_listeners over this node's listeners, read first; a listing
  # that could not be read (ss missing or failing) is a FAIL, never a node with no listener to check
  local out
  if ! out=$(listeners); then
    printf "FAIL: this node's listeners could not be listed (ss missing or failing): nothing was checked\n" >&2
    return 1
  fi
  if [[ -z $out ]]; then check_listeners "$1" "$2" "$3" < /dev/null; else check_listeners "$1" "$2" "$3" <<<"$out"; fi
}

epmd_port_of() {  # <rootfs>: this job's epmd port from the rootfs's erlang env file
  local p
  p=$(sed -n 's/^ERL_EPMD_PORT=\([0-9]\{1,5\}\)$/\1/p' "$1$HPCOP_ERLANG_ENV" 2>/dev/null | head -n 1)
  if [[ -z $p ]] || ! port_in "$p" "$HPCOP_EPMD_RANGE"; then
    die "no ERL_EPMD_PORT in $HPCOP_EPMD_RANGE in $1$HPCOP_ERLANG_ENV: run configure.sh"
  fi
  printf '%s\n' "$p"
}

write_erlang_env() {  # <rootfs> <epmd port>: the Erlang settings every VM start path sources (not secret)
  printf '%s\n' \
    "# hpcop: Erlang settings for every VM of this service, written by configure.sh. Sourced by the patched" \
    "# relx env.sh (the services and nodetool) and couchbase-server (Couchbase's VMs and its epmd start)," \
    "# because \`service\` starts them under env -i. Distribution and epmd on loopback, on this job's port." \
    "ERL_EPMD_PORT=$2" \
    "ERL_EPMD_ADDRESS=127.0.0.1" \
    "ERL_AFLAGS='-kernel inet_dist_use_interface {127,0,0,1}'" \
    "export ERL_EPMD_PORT ERL_EPMD_ADDRESS ERL_AFLAGS" > "$1$HPCOP_ERLANG_ENV"
  chmod 644 -- "$1$HPCOP_ERLANG_ENV"
}
