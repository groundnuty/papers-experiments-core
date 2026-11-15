#!/bin/bash
# finish-zone.sh: complete the zone's deployment, from its batch step (zone.sbatch), then prove it is
# hardened. Waits for the fresh-deployment message, reads /api/v3/onezone/configuration, checks that the
# admin (password: the zone panel's emergency passphrase, from its 0600 file) can read GET /user,
# guards Couchbase (cb_guard: this launch's administrator, anonymous admin REST refused) before and after it
# retires the deploy-time Couchbase password, and runs check-defaults.sh --live. The zone has no storage.
# Every non-2xx answer is fatal. TLS: the Onedata test CA, this node's name pinned to 127.0.0.1.
# Usage: finish-zone.sh --workdir W <service-pid>
source "$(dirname "$0")/lib.sh"
component oz
[[ ${1:-} == --workdir && -n ${3:-} ]] || die "usage: finish-zone.sh --workdir W <service-pid>"
W=$(find_workdir oz "$2"); SVC=$3; R=$W/rootfs; S=$(secrets_dir "$W")
wait_ready "$W" "$SVC"

T=$(mktemp -d "$W/tmp/finish.XXXXXX"); trap 'rm -rf -- "$T"' EXIT
# The guard, first: Couchbase must have this launch's administrator and refuse anonymous admin REST. It runs
# before cb_rotate, whose POST /settings/web would otherwise create the first administrator on a Couchbase
# that has none (live run 3's silent case) and hide it; cb_rotate runs it again after the rotation.
read_secret CBU "$S/couchbase-user"; read_secret CBP "$S/couchbase-password"
cb_guard "$T" "$CBU" "$CBP" "after onepanel's Couchbase setup"; unset CBP
FQDN=$(node_fqdn)
CA=$R/volumes/persistence_backup/etc/oz_panel/OnedataTestWebServerCa.pem
[[ -s $CA ]] || die "no Onedata test CA at $CA"
TLS=(--cacert "$CA" --resolve "$FQDN:8443:127.0.0.1")
OZ=https://$FQDN:8443/api/v3/onezone

code=$(http GET "$OZ/configuration" "$T/conf.json" "${TLS[@]}")
need_2xx "$code" "zone /configuration" "$T/conf.json"
read_secret PW "$S/passphrase"; curl_auth_file "$T/admin.cfg" admin "$PW"; unset PW
code=$(http GET "$OZ/user" "$T/user.json" "${TLS[@]}" -K "$T/admin.cfg")
need_2xx "$code" "zone admin GET /user" "$T/user.json"
ADMIN_ID=$(jsonget "$T/user.json" userId)

cb_rotate "$W" "$T"
bash "$HPCOP_BUNDLE/check-defaults.sh" oz --workdir "$W" --live
say "OK zone '$(jsonget "$T/conf.json" name --default '?')' at https://$FQDN:8443 (admin $ADMIN_ID)"
