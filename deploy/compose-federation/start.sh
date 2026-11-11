#!/bin/bash
# The services' entrypoint: trust the federation's own CA, then become the image's stock start script.
#
# The panels and workers read the CA from the cacerts directories that up seeds into each persistence directory. The
# providers' transfer service is C++ and reads the system store instead, so the CA is added there as well. The stock
# script refuses to run unless it is PID 1, hence exec. Nothing here turns certificate verification off.
#
# The stock script marks readiness with /root/service-ready.lock and never removes it, so a container stopped and
# started again would look healthy before it is: the marker is removed here, before every start.
set -euo pipefail

install -m 0644 /expcore/federation-ca.pem /usr/local/share/ca-certificates/expcore-federation-ca.crt
update-ca-certificates > /dev/null
rm -f /root/service-ready.lock

exec "$@"
