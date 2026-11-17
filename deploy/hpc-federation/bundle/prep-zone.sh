#!/bin/bash
# prep-zone.sh: patch an Apptainer sandbox of docker.onedata.org/onezone:ID-ba7a778696 so it starts as
# the job user, with no user or network namespace: the Onezone counterpart of prep.sh, with the same
# verified patches (its own Couchbase, the oz_* relx env.sh files, /root/onezone.sh without the PID-1
# check, the /etc/oz_* link targets, the 80/443 shim) plus three zone-only overlays:
#   - ports: oz_worker listens on 8443, onepanel checks it there;
#   - DNS: oz_worker's erldns binds 0.0.0.0:53 (UDP and TCP), which non-root cannot do. It is moved to
#     127.0.0.1:8053 and subdomain delegation is switched off; the shim is NOT used for 53, because a
#     connect() remap would also catch the node's own resolver on 127.0.0.x:53;
#   - GUI: gui_package_verification off, as in the study's federation (development-build providers).
# Usage: prep-zone.sh <rootfs> <portshim.so>
source "$(dirname "$0")/lib.sh"
source "$HPCOP_BUNDLE/patch.sh"
component oz
R=${1:?usage: prep-zone.sh <rootfs> <portshim.so>}; SHIM=${2:?portshim.so}

prep_common "$SHIM"

W_CFG=$R/var/lib/oz_worker/app.config; P_CFG=$R/var/lib/oz_panel/app.config
need_key "$W_CFG" '{https_server_port, 443},' "oz_worker https port key"
need_key "$W_CFG" '[{name, inet_all_1}, {address, "0.0.0.0"}, {port, 53}, {family, inet}, {processes, 2}]' "erldns server on 53"
need_key "$W_CFG" '{subdomain_delegation_supported, true},' "subdomain delegation key"
need_key "$W_CFG" '{gui_package_verification, true},' "GUI package verification key"
need_key "$P_CFG" '{worker_https_server_port, 443},' "onepanel worker port key"
need_key "$P_CFG" '{oz_worker_nagios_port, 443},' "onepanel nagios port key"
write_overlay oz_worker 60-hpcop-ports.config '% hpcop: non-root, no binding below 1024.
[{oz_worker, [{https_server_port, 8443}]}].'
write_overlay oz_worker 61-hpcop-dns.config '% hpcop: non-root cannot bind 53; no subdomain delegation, so serve DNS on loopback only.
[{erldns, [{servers, [[{name, inet_all_1}, {address, "127.0.0.1"}, {port, 8053}, {family, inet}, {processes, 2}]]}]},
 {oz_worker, [{subdomain_delegation_supported, false}]}].'
write_overlay oz_worker 62-hpcop-gui-verification.config '% hpcop: development-build providers ship GUI packages the zone would refuse.
[{oz_worker, [{gui_package_verification, false}]}].'
write_overlay oz_panel 60-hpcop-ports.config '% hpcop: oz_worker listens on 8443.
[{onepanel, [{worker_https_server_port, 8443}, {oz_worker_nagios_port, 8443}]}].'
say "prepared oz rootfs $R"
