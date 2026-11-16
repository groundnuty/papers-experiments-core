#!/bin/bash
# run-zone.sh: the Onezone itself, in the foreground: zone.sbatch starts it from the job's batch step,
# so it lives exactly as long as the job. All zone state (Couchbase, registrations, its identity) is in
# the job's tmpfs: when this job ends the federation ends, and a new zone is a new identity. Output goes
# to <workdir>/run.log (0600); /root/hpcop-start.sh exports ONEZONE_CONFIG and the passphrase from files.
# Usage: run-zone.sh [--workdir W]
source "$(dirname "$0")/lib.sh"
component oz
[[ -z ${1:-} || $1 == --workdir ]] || die "usage: run-zone.sh [--workdir W]"
run_service "$(find_workdir oz "${2:-}")"
