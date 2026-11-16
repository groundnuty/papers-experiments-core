#!/bin/bash
# configure.sh: write one service's launch settings into its rootfs, after node-init.sh. Every input is
# validated against a narrow character set before it reaches a file. Nothing written here is secret:
#   rootfs/root/.hpcop/config.yaml   ONEPROVIDER_CONFIG or ONEZONE_CONFIG (the token is a separate file)
#   rootfs/root/.hpcop/env           the onepanel switches (test web certificate for this node, batch mode)
#   rootfs/etc/hpcop-peers           peer addresses for the port shim: connect() to 443 there goes to 8443.
#                                    Read once per process, so list every peer before the service starts:
#                                    the zone's address on each provider, and every provider on the zone.
#   rootfs/etc/hpcop-erlang-env      this job's epmd port (a random free one of 21300-21999), epmd and the Erlang
#                                    distribution on loopback; sourced by every VM start path and read by
#                                    the shim, which sends 4369 there.
#   W/imports                        a provider's shared imported trees (O59), when it owns imported spaces: each
#                                    --import=SPACE=PATH is checked by importstorage.py, its empty mountpoint made in
#                                    the rootfs, and run_service binds the tree there read-only. Never for the zone.
# Usage: configure.sh op --workdir W <provider-name> <zone-fqdn> <admin-email> [peer-ip ...] [--import=SPACE=PATH ...]
#        configure.sh oz --workdir W <zone-name> [peer-ip ...]
source "$(dirname "$0")/lib.sh"
component "${1:-}"; shift
need_tools ss od   # this job's epmd port is picked from the node's listeners (pick_epmd_port), up front
[[ ${1:-} == --workdir ]] || die "usage: configure.sh <op|oz> --workdir W ..."
W=$(find_workdir "$COMP" "${2:-}"); shift 2
R=$W/rootfs; S=$(secrets_dir "$W")
[[ -e $W/rootfs.complete ]] || die "$W has no complete rootfs: run node-init.sh first"

FQDN=$(node_fqdn); SHORT=${FQDN%%.*}; DOMAIN=${FQDN#*.}
split_imports IMPORTS ARGS "$@"; set -- "${ARGS[@]}"
if [[ $COMP == oz && ${#IMPORTS[@]} -gt 0 ]]; then die "the zone mounts no import: only a provider holds an imported space"; fi
if [[ $COMP == op ]]; then
  (( $# >= 3 )) || die "usage: configure.sh op --workdir W <provider-name> <zone-fqdn> <admin-email> [peer-ip ...]"
  NAME=$1 ZONE=$2 MAIL=$3; shift 3
  valid name "$NAME"; valid fqdn "$ZONE"; valid mail "$MAIL"
  tpl=$(<"$HPCOP_BUNDLE/oneprovider-config.template.yaml")
  tpl=${tpl//@PROVIDER_NAME@/$NAME}; tpl=${tpl//@ZONE_FQDN@/$ZONE}; tpl=${tpl//@ADMIN_EMAIL@/$MAIL}
else
  (( $# >= 1 )) || die "usage: configure.sh oz --workdir W <zone-name> [peer-ip ...]"
  NAME=$1; shift
  valid name "$NAME"
  tpl=$(<"$HPCOP_BUNDLE/onezone-config.template.yaml")
  tpl=${tpl//@ZONE_NAME@/$NAME}
fi
tpl=${tpl//@NODE_DOMAIN@/$DOMAIN}; tpl=${tpl//@NODE_SHORT@/$SHORT}; tpl=${tpl//@NODE_FQDN@/$FQDN}
if [[ $tpl =~ @[A-Z_]+@ ]]; then die "unfilled placeholder ${BASH_REMATCH[0]} left in the $CONFIG_VAR template"; fi
printf '%s\n' "$tpl" | grep -v '^[[:space:]]*#' > "$S/config.yaml"; chmod 600 -- "$S/config.yaml"

printf '%s\n' ONEPANEL_BATCH_MODE=true ONEPANEL_LOG_LEVEL=info ONEPANEL_DEBUG_MODE=false \
  ONEPANEL_GENERATE_TEST_WEB_CERT=true "ONEPANEL_GENERATED_CERT_DOMAIN=$FQDN" ONEPANEL_TRUST_TEST_CA=true > "$S/env"
chmod 600 -- "$S/env"

{ printf '# hpcop peers for %s on %s, written %s\n' "$COMP" "$FQDN" "$(date -u +%FT%TZ)"
  for ip in "$@"; do valid ip "$ip"; printf '%s\n' "$ip"; done; } > "$R/etc/hpcop-peers"
chmod 644 -- "$R/etc/hpcop-peers"

if (( ${#IMPORTS[@]} > 0 )); then
  "$HPCOP_PY" -I "$HPCOP_BUNDLE/importstorage.py" prepare --workdir "$W" "${IMPORTS[@]}" \
    || die "a shared imported tree was refused: nothing was prepared for it (see above)"
fi

EPMD=$(pick_epmd_port)
write_erlang_env "$R" "$EPMD"
[[ $(epmd_port_of "$R") == "$EPMD" ]] || die "the Erlang env file does not hold epmd port $EPMD"
ok "configured $COMP '$NAME' on $FQDN with $# peer(s) and ${#IMPORTS[@]} shared import(s); epmd on 127.0.0.1:$EPMD"
