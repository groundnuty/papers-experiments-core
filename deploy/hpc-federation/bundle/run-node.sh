#!/bin/bash
# run-node.sh: the Oneprovider itself, in the foreground: provider.sbatch starts it from the job's batch
# step, so it lives exactly as long as the job (not as long as a login-node srun). Output goes to
# <workdir>/run.log (0600). Inside the container, /root/hpcop-start.sh loads the settings and the
# passphrase from /root/.hpcop and execs the stock start script without its PID-1 check.
# --storage=PATH (O57): the provider's own directory on scratch, which the work directory must record (configure.sh),
# bound onto /volumes/posix; provider.sbatch and restart.sh pass the one their job or `hpcfed.py start` was given.
# Usage: run-node.sh [--workdir W [--storage=PATH]]
source "$(dirname "$0")/lib.sh"
component op
[[ -z ${1:-} || $1 == --workdir ]] || die "usage: run-node.sh [--workdir W [--storage=PATH]]"
[[ -z ${3:-} || $3 == --storage=* ]] && (( $# <= 3 )) || die "usage: run-node.sh [--workdir W [--storage=PATH]]"
run_service "$(find_workdir op "${2:-}")" "${3:-}"
