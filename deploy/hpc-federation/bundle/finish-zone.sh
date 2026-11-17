#!/bin/bash
# finish-zone.sh: complete the zone's deployment, from its batch step (zone.sbatch), then prove it is
# hardened. Waits for the fresh-deployment message, reads /api/v3/onezone/configuration, checks that the
# admin (password: the zone panel's emergency passphrase, from its 0600 file) can read GET /user,
# guards Couchbase (cb_guard: this launch's administrator, anonymous admin REST refused) before and after it
# retires the deploy-time Couchbase password, and runs check-defaults.sh --live. The zone has no storage.
# Every non-2xx answer is fatal. TLS: the Onedata test CA, this node's name pinned to 127.0.0.1.
# A first start records the identity (identity.json: the zone's domain and the admin's user id) once every check has
# passed. --restart (restart.sh, hpcfed.py start) is the same proof for a zone started again over its existing
# deployment: wait_ready in its restart mode, the guard from the host after the restart, the domain and the admin's
# user id compared with identity.json (a change fails the start), then the rotation and the live check as ever.
# Usage: finish-zone.sh [--restart] --workdir W <service-pid>
source "$(dirname "$0")/lib.sh"
component oz
RESTART=0
if [[ ${1:-} == --restart ]]; then RESTART=1; shift; fi
[[ ${1:-} == --workdir && -n ${3:-} ]] || die "usage: finish-zone.sh [--restart] --workdir W <service-pid>"
W=$(find_workdir oz "$2"); SVC=$3; R=$W/rootfs; S=$(secrets_dir "$W")
if (( RESTART )); then wait_ready "$W" "$SVC" 1800 restart; else wait_ready "$W" "$SVC"; fi

T=$(mktemp -d "$W/tmp/finish.XXXXXX"); trap 'rm -rf -- "$T"' EXIT
# The guard, first: Couchbase must have this launch's administrator and refuse anonymous admin REST. It runs
# before cb_rotate, whose POST /settings/web would otherwise create the first administrator on a Couchbase
# that has none (live run 3's silent case) and hide it; cb_rotate runs it again after the rotation.
read_secret CBU "$S/couchbase-user"; read_secret CBP "$S/couchbase-password"
if (( RESTART )); then WHEN='after a restart'; else WHEN="after onepanel's Couchbase setup"; fi
cb_guard "$T" "$CBU" "$CBP" "$WHEN"; unset CBP
FQDN=$(service_name "$W")   # the name the service is registered under (its certificate's): the node's own, or its declared one
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
DOM=$(jsonget "$T/conf.json" domain --default "$FQDN")   # the zone's own name for itself; the node's name if it reports none
if (( RESTART )); then check_identity "$W" kind oz adminUserId "$ADMIN_ID" domain "$DOM"; fi

cb_rotate "$W" "$T"
bash "$HPCOP_BUNDLE/check-defaults.sh" oz --workdir "$W" --live
if (( ! RESTART )); then write_identity "$W" oz adminUserId "$ADMIN_ID" domain "$DOM"; fi
say "OK zone '$(jsonget "$T/conf.json" name --default '?')' at https://$FQDN:8443 (admin $ADMIN_ID)"
