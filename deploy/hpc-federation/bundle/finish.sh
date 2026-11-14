#!/bin/bash
# finish.sh: complete a provider's deployment, from its batch step (provider.sbatch), then prove it is
# hardened. Waits for the stock launcher's fresh-deployment message, adds the posix storage owned by
# the job user (rootUid 0 fails with edom when not root), reads op_worker's /configuration, guards Couchbase
# (cb_guard: this launch's administrator, anonymous admin REST refused) before and after it retires the
# deploy-time Couchbase password, and runs check-defaults.sh --live. Every failure is fatal: a non-2xx
# answer, a storage list still empty after the POST, a failed rotation or a default still in place.
# TLS is verified against the Onedata test CA, for this node's name pinned to 127.0.0.1, so the
# onepanel credentials never go to another user's listener on the same port.
# Usage: finish.sh --workdir W <service-pid>
source "$(dirname "$0")/lib.sh"
component op
[[ ${1:-} == --workdir && -n ${3:-} ]] || die "usage: finish.sh --workdir W <service-pid>"
W=$(find_workdir op "$2"); SVC=$3; R=$W/rootfs; S=$(secrets_dir "$W")
wait_ready "$W" "$SVC"

T=$(mktemp -d "$W/tmp/finish.XXXXXX"); trap 'rm -rf -- "$T"' EXIT
# The guard, first: Couchbase must have this launch's administrator and refuse anonymous admin REST. It runs
# before cb_rotate, whose POST /settings/web would otherwise create the first administrator on a Couchbase
# that has none (live run 3's silent case) and hide it; cb_rotate runs it again after the rotation.
read_secret CBU "$S/couchbase-user"; read_secret CBP "$S/couchbase-password"
cb_guard "$T" "$CBU" "$CBP" "after onepanel's Couchbase setup"; unset CBP
FQDN=$(node_fqdn)
CA=$R/volumes/persistence_backup/etc/op_panel/OnedataTestWebServerCa.pem
[[ -s $CA ]] || die "no Onedata test CA at $CA"
read_secret PW "$S/passphrase"; curl_auth_file "$T/panel.cfg" onepanel "$PW"; unset PW
TLS=(--cacert "$CA" --resolve "$FQDN:9443:127.0.0.1")
B=https://$FQDN:9443/api/v3/onepanel/provider

storage_id() {
  local code
  code=$(http GET "$B/storages" "$T/storages.json" "${TLS[@]}" -K "$T/panel.cfg")
  need_2xx "$code" "list storages" "$T/storages.json"
  jsonget "$T/storages.json" ids.0 --default ""
}
SID=$(storage_id)
if [[ -z $SID ]]; then
  printf '{"posix-local": {"type": "posix", "mountPoint": "/volumes/posix", "rootUid": %d, "rootGid": %d, "lumaFeed": "local", "storagePathType": "canonical"}}\n' \
    "$(id -u)" "$(id -g)" > "$T/storage.json"
  code=$(http POST "$B/storages" "$T/post.out" "${TLS[@]}" -K "$T/panel.cfg" -H 'Content-Type: application/json' --data @"$T/storage.json")
  need_2xx "$code" "add the posix storage" "$T/post.out"
  SID=$(storage_id)
fi
[[ -n $SID ]] || die "the storage list is still empty after a successful POST"
ok "posix storage $SID owned by uid $(id -u)"

code=$(http GET "https://$FQDN:8443/configuration" "$T/conf.json" --cacert "$CA" --resolve "$FQDN:8443:127.0.0.1")
need_2xx "$code" "op_worker /configuration" "$T/conf.json"
ID=$(jsonget "$T/conf.json" providerId); DOM=$(jsonget "$T/conf.json" domain)

cb_rotate "$W" "$T"
bash "$HPCOP_BUNDLE/check-defaults.sh" op --workdir "$W" --live
say "OK provider $ID at $DOM, storage $SID"
