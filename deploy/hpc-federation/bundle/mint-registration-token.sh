#!/bin/bash
# mint-registration-token.sh: mint a short-lived provider registration token (a registerOneprovider
# invite token) from the zone. Run it in a step of the ZONE's job: the zone's 8443 is reachable there,
# and the admin password (the zone panel's emergency passphrase) is read from its 0600 file in that job,
# never from the caller's environment or command line. curl gets the credentials from a -K config file
# and the request body from a file. The token goes to stdout only: pipe it into deliver-token.sh.
# Defaults: valid 1 hour, usable once. TLS is verified: by default against the Onedata test CA that
# signed the zone's generated test certificate; --cacert names another CA file.
# Usage: mint-registration-token.sh <provider-name> [--cacert FILE] [--ttl SECONDS] [--usage-limit N|infinity] [--workdir W]
source "$(dirname "$0")/lib.sh"
component oz
NAME=${1:?usage: mint-registration-token.sh <provider-name> [--cacert FILE] [--ttl S] [--usage-limit N] [--workdir W]}; shift
valid name "$NAME"
CA='' TTL=3600 LIMIT=1 WD=''
while (( $# )); do
  case $1 in
    --cacert) CA=${2:?--cacert FILE}; shift 2 ;;
    --ttl) TTL=${2:?--ttl SECONDS}; shift 2 ;;
    --usage-limit) LIMIT=${2:?--usage-limit N}; shift 2 ;;
    --workdir) WD=${2:?--workdir W}; shift 2 ;;
    *) die "unknown option '$1'" ;;
  esac
done
W=$(find_workdir oz "$WD"); R=$W/rootfs; S=$(secrets_dir "$W")
CA=${CA:-$R/volumes/persistence_backup/etc/oz_panel/OnedataTestWebServerCa.pem}
[[ -f $CA && -r $CA ]] || die "cannot read the CA file $CA"
[[ -e $R/root/service-ready.lock ]] || die "the zone in $W is not ready yet"

FQDN=$(node_fqdn)
OZ=https://$FQDN:8443/api/v3/onezone
TLS=(--cacert "$CA" --resolve "$FQDN:8443:127.0.0.1")
T=$(mktemp -d "$W/tmp/mint.XXXXXX"); trap 'rm -rf -- "$T"' EXIT
read_secret PW "$S/passphrase"; curl_auth_file "$T/admin.cfg" admin "$PW"; unset PW

code=$(http GET "$OZ/user" "$T/user.json" "${TLS[@]}" -K "$T/admin.cfg")
need_2xx "$code" "zone admin GET /user" "$T/user.json"
ADMIN_ID=$(jsonget "$T/user.json" userId)
"$HPCOP_PY" -I "$HPCOP_BUNDLE/tokenbody.py" "$NAME" "$ADMIN_ID" "$TTL" "$LIMIT" > "$T/body.json"
code=$(http POST "$OZ/user/tokens/named" "$T/token.json" "${TLS[@]}" -K "$T/admin.cfg" \
  -H 'Content-Type: application/json' --data @"$T/body.json")
need_2xx "$code" "mint the registration token" "$T/token.json"
jsonget "$T/token.json" token
say "registration token for '$NAME' minted (valid ${TTL}s, usage limit $LIMIT)"
