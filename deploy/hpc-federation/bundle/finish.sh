#!/bin/bash
# finish.sh: complete a provider's deployment, from its batch step (provider.sbatch), then prove it is
# hardened. Waits for the stock launcher's fresh-deployment message, adds the posix storage owned by
# the job user (rootUid 0 fails with edom when not root), reads op_worker's /configuration, guards Couchbase
# (cb_guard: this launch's administrator, anonymous admin REST refused) before and after it retires the
# deploy-time Couchbase password, and runs check-defaults.sh --live. Every failure is fatal: a non-2xx
# answer, a storage list still empty after the POST, a failed rotation or a default still in place.
# TLS is verified against the Onedata test CA, for this node's name pinned to 127.0.0.1, so the
# onepanel credentials never go to another user's listener on the same port.
# A first start records the identity (identity.json: provider id and domain) once every check has passed.
# --restart (restart.sh, hpcfed.py start) is the same proof for a service started again over its existing
# deployment: wait_ready in its restart mode (that start prints no 'Congratulations! New'), the guard from the host
# after the restart, no storage to add, the provider id and domain compared with identity.json (a change fails the
# start), then the rotation and the live check as ever; identity.json is not rewritten.
# op_worker answers the domain from its Onezone connection, so /configuration can say null for a moment after the REST
# listener answers: both starts wait for a domain that is neither null nor empty (wait_domain), inside the readiness
# budget (HPCOP_READY_SECONDS, 1800 s), before anything is compared or recorded. A domain that stays null fails the
# start as that; a different non-null domain still fails it as a changed identity.
# Usage: finish.sh [--restart] --workdir W <service-pid>
source "$(dirname "$0")/lib.sh"
component op
RESTART=0
if [[ ${1:-} == --restart ]]; then RESTART=1; shift; fi
[[ ${1:-} == --workdir && -n ${3:-} ]] || die "usage: finish.sh [--restart] --workdir W <service-pid>"
W=$(find_workdir op "$2"); SVC=$3; R=$W/rootfs; S=$(secrets_dir "$W")
READY_S=$(ready_budget); READY_END=$((SECONDS + READY_S))   # one budget (1800 s) for wait_ready and the domain wait
if (( RESTART )); then wait_ready "$W" "$SVC" "$READY_S" restart; else wait_ready "$W" "$SVC" "$READY_S"; fi

T=$(mktemp -d "$W/tmp/finish.XXXXXX"); trap 'rm -rf -- "$T"' EXIT
# The guard, first: Couchbase must have this launch's administrator and refuse anonymous admin REST. It runs
# before cb_rotate, whose POST /settings/web would otherwise create the first administrator on a Couchbase
# that has none (live run 3's silent case) and hide it; cb_rotate runs it again after the rotation.
read_secret CBU "$S/couchbase-user"; read_secret CBP "$S/couchbase-password"
if (( RESTART )); then WHEN='after a restart'; else WHEN="after onepanel's Couchbase setup"; fi
cb_guard "$T" "$CBU" "$CBP" "$WHEN"; unset CBP
FQDN=$(node_fqdn)
CA=$R/volumes/persistence_backup/etc/op_panel/OnedataTestWebServerCa.pem
[[ -s $CA ]] || die "no Onedata test CA at $CA"
if (( ! RESTART )); then
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
fi

# The domain first: op_worker says null until its Onezone connection is up (wait_domain), and a null is neither
# compared with the recorded identity nor recorded as one. The wait is the same for a first start and a restart.
wait_domain "$T" "$FQDN" "$CA" "$SVC" "$READY_END" "$READY_S"
ID=$(jsonget "$T/conf.json" providerId) || die "op_worker's /configuration names no providerId (null or missing)"
DOM=$(jsonget "$T/conf.json" domain) || die "op_worker's /configuration names no domain (null or missing)"
if (( RESTART )); then check_identity "$W" kind op providerId "$ID" domain "$DOM"; fi

cb_rotate "$W" "$T"
bash "$HPCOP_BUNDLE/check-defaults.sh" op --workdir "$W" --live
if (( RESTART )); then
  say "OK provider $ID at $DOM, restarted over its existing deployment"
else
  write_identity "$W" op providerId "$ID" domain "$DOM"
  say "OK provider $ID at $DOM, storage $SID"
fi
