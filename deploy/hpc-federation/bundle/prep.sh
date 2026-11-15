#!/bin/bash
# prep.sh: patch an Apptainer sandbox of docker.onedata.org/oneprovider:ID-765309c84f so it starts as
# the job user, with no user or network namespace. Idempotent and self-checking: every patch is
# verified after it is applied, and any mismatch with the image stops the script with an error.
# Runs on the compute node (host side), from build-rootfs.sh and again from node-init.sh after the untar,
# because the config.d overlays live under /volumes/persistence, which the tarball leaves out.
# Usage: prep.sh <rootfs> <portshim.so> [--audit]
source "$(dirname "$0")/lib.sh"
source "$HPCOP_BUNDLE/patch.sh"
component op
R=${1:?usage: prep.sh <rootfs> <portshim.so> [--audit]}; SHIM=${2:?portshim.so}; AUDIT=${3:-}
[[ -z $AUDIT || $AUDIT == --audit ]] || die "unknown option '$AUDIT'"

prep_common "$SHIM"

# Ports: non-root cannot bind 443, so op_worker listens on 8443 and onepanel checks it there.
need_key "$R/var/lib/op_worker/app.config" '{https_server_port, 443},' "op_worker https port key"
need_key "$R/var/lib/op_panel/app.config" '{worker_https_server_port, 443},' "onepanel worker port key"
need_key "$R/var/lib/op_panel/app.config" '{op_worker_nagios_port, 443},' "onepanel nagios port key"
write_overlay op_worker 60-hpcop-ports.config '% hpcop: non-root, no binding below 1024.
[{op_worker, [{https_server_port, 8443}]}].'
write_overlay op_panel 60-hpcop-ports.config '% hpcop: op_worker listens on 8443.
[{onepanel, [{worker_https_server_port, 8443}, {op_worker_nagios_port, 8443}]}].'

if [[ $AUDIT == --audit ]]; then   # optional: op_worker file-access audit log, logger burst limit off
  write_overlay op_worker 50-hpcop-file-access-audit.config '[
  {op_worker, [{file_access_audit_log_enabled, true}]},
  {ctool, [{logger_base_config, #{
    burst_limit_enable => false,
    burst_limit_max_count => 200,
    sync_mode_qlen => 500,
    drop_mode_qlen => 1000,
    flush_qlen => 2000
  }}]}
].'
fi
say "prepared op rootfs $R"
